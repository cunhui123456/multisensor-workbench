"""本机 SSPP 实验工作台：设备参数、实时观察、历史回放与离线诊断。"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import shutil
import time
import threading
from PySide6 import QtCore, QtGui, QtWidgets

from sspp_lab.controller import DEFAULTS
from .plots import DiagnosticPlot, LivePlots, PreviewPlots
from .workers import TaskRunner
from .qt_cleanup import close_plot_widgets
from .experiments.material_sweep import MaterialSweepWidget
from .experiments.finger_displacement import FingerDisplacementWidget
from .comparison_widget import ComparisonDialog
from .temperature_widget import TemperaturePressureWidget
from .analysis_workbench_widget import AnalysisWorkbenchWidget
from .liquid_temperature_widget import LiquidTemperatureWidget
from .environment_dialog import EnvironmentDialog
from .temperature_sweep_widget import TemperatureSweepWidget
from .unified_sweep_widget import UnifiedSweepSettings
from .serial_archive_widget import SerialArchiveWidget
from .archive_conditions import archive_conditions
from .experiment_help import ExperimentHelpDialog
from .archive_trash_dialog import ArchiveDeleteDialog, ArchiveTrashDialog

BUSY = {"starting", "running", "stopping", "saving"}
STATUS = {"complete_with_gaps":"已结束（含无效频点）", "starting": "正在初始化", "running": "正在采集", "stopping": "正在停止", "saving": "正在保存",
          "complete": "已完成", "stopped": "已停止并保存", "failed": "实验失败", "initialization_failed": "初始化失败",
          "stop_unconfirmed": "停止状态待确认", "initializing": "初始化中", "failed_or_incomplete": "失败或未完成", "unknown": "未知"}
GROUPS = {"diagnostic": "接收诊断", "sweep": "扫频分析", "phase": "幅相分析", "quality": "采集质量"}
SSPP_DEFAULTS = {"freq_start_ghz": 5.0, "freq_stop_ghz": 5.3, "points": 30,
                 "rx_gain": 45, "tone_start": 20000}
CATEGORIES = {"pressure_calibration":"压力传感器标定", "temperature_calibration":"温度标定", "temperature_pressure":"温压采集", "temperature_sweep": "SSPP 温度同步扫频", "material_sweep": "材料逐点扫频", "finger_displacement": "手指位移",
              "phase_history": "历史连续幅相", "teacher_sweep": "老师原版扫频",
              "liquid_temperature_sweep": "液体温度扫频", "liquid_temperature_phase": "液体温度与位移相位"}

EXPERIMENTS = {
    "material": ("SSPP 扫频", "样品响应 · 可选液体条件与温度同步记录", {"material_sweep", "teacher_sweep", "liquid_temperature_sweep", "temperature_sweep"}),
    "finger": ("手指位移", "连续幅相 · 尺量标定与位移验证", {"finger_displacement", "phase_history", "liquid_temperature_phase"}),
    "temperature": ("温压采集", "串口采集 · 电压 / ADC 与温压标定", {"temperature_pressure"}),
    "temperature_calibration": ("温度标定", "多温度平台 · 区间电压统计与参考温度记录", {"temperature_calibration"}),
    "pressure_calibration": ("压力传感器标定", "参考力与接触面积 · 分级加载、卸载与标定分析", {"pressure_calibration"}),
}


def label(text, role=None):
    result = QtWidgets.QLabel(text)
    result.setWordWrap(True)
    if role:
        result.setObjectName(role)
    return result


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, service):
        super().__init__()
        self.service = service
        self.runner = TaskRunner(self)
        self.fields = {}
        self.snapshot = {}
        self._liquid_setup = None
        self.experiment_id = "material"
        self._experiment_drafts = {}
        self._navigation_ready = False
        self._navigating = False
        self.viewing_key = None
        self.history_rows = []
        self._history_all_rows = []
        self._selected_history_key = None
        self.comparison_keys = set()
        self.comparison_dialog = None
        self._comparison_generation = 0
        self._comparison_busy = False
        self._archive_busy = False
        self._archive_epoch = 0
        self._archive_trash_refresh_requested = False
        self.archive_trash_dialog = None
        self.selected_details = None
        self._displayed_history_details = None
        self.diagnostic_model = {}
        self._diagnostic_publication_directories = {}
        self.replay = None
        self.replay_running = False
        self.replay_elapsed = 0.0
        self._replay_tick = time.monotonic()
        self._history_generation = 0
        self._history_refresh_requested = False
        self._history_refresh_select_key = None
        self._history_refresh_revision = 0
        self._history_correction_refresh_key = None
        self._history_summary_request = None
        self._history_filter_pending = False
        self._live_revision = None
        self._last_preview = 0.0
        self._close_requested = False
        self._shutdown_done = False
        self._start_pending = False
        self._environment_pending = False
        self._environment_cancel_event = None
        self.environment_dialog = None
        self._shutdown_submitted = False
        self._auxiliary_shutdown_done = False
        self._close_timer = QtCore.QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self._finish_close)
        self.setWindowTitle("多传感器实验台")
        self.resize(1480, 1000)
        self.setMinimumSize(1080, 740)
        self._build_ui()
        self._set_active(False)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(service.settings.ui_interval_ms)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.runner.idle.connect(self._finish_close)
        self.runner.idle.connect(self._refresh_navigation_enabled)
        self.runner.idle.connect(self._flush_history_refresh)
        self.runner.idle.connect(self._flush_history_summary)
        self._load_profiles()
        self.refresh_history()
        self._tick()

    def _build_ui(self):
        # 全局设备模型不属于任何实验表单；弹窗只在确认后提交选择。
        self.profile = QtWidgets.QComboBox(self)
        self.profile.setObjectName("globalDeviceProfile")
        self.profile.hide()
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 12)
        navigation = QtWidgets.QHBoxLayout()
        self.initialize_button = QtWidgets.QPushButton("初始化实验台")
        self.initialize_button.setObjectName("primary")
        self.initialize_button.setToolTip("分别选择 B210 与 Arduino；只连接一方也可开始对应实验。")
        self.initialize_button.clicked.connect(self.open_environment_dialog)
        navigation.addWidget(self.initialize_button)
        navigation.addWidget(label("多传感器实验台", "title"))
        self.experiment_nav = QtWidgets.QTabBar()
        self.experiment_nav.setObjectName("experimentNav")
        self.experiment_nav.setExpanding(True)
        self.experiment_nav.setDocumentMode(True)
        self.experiment_nav.setDrawBase(False)
        for key, (name, description, _) in EXPERIMENTS.items():
            index = self.experiment_nav.addTab(name)
            self.experiment_nav.setTabData(index, key)
            self.experiment_nav.setTabToolTip(index, description)
        self.experiment_nav.setCurrentIndex(0)
        self.experiment_nav.currentChanged.connect(self._experiment_changed)
        navigation.addWidget(self.experiment_nav, 1)
        layout.addLayout(navigation)
        heading = QtWidgets.QHBoxLayout()
        self.experiment_description = label("", "muted")
        heading.addWidget(self.experiment_description, 1)
        self.archive_button = QtWidgets.QPushButton("实验档案")
        self.archive_button.setToolTip("查看所有射频实验；温压记录从研究分析的数据源进入。")
        self.archive_button.clicked.connect(self.open_archive)
        heading.addWidget(self.archive_button)
        self.analysis_button = QtWidgets.QPushButton("研究分析与模型")
        self.analysis_button.clicked.connect(self.open_workbench)
        heading.addWidget(self.analysis_button)
        self.status_label = label("待开始", "status")
        self.status_detail = label("启动软件不会自动收发。", "muted")
        self.status_detail.setMaximumWidth(340)
        self.status_detail.setVisible(False)
        self.environment_label = label("设备未初始化", "muted")
        self.environment_label.setMinimumWidth(200)
        self.environment_label.setMaximumWidth(220)
        self.environment_label.setToolTip("启动时不连接设备。初始化检查结束后释放设备，开始实验时由采集进程重新打开。")
        heading.addWidget(self.environment_label)
        heading.addWidget(self.status_label)
        self.stop_button = QtWidgets.QPushButton("停止并保存")
        self.stop_button.setObjectName("stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_current_experiment)
        heading.addWidget(self.stop_button)
        layout.addLayout(heading)
        self.help_dialog = ExperimentHelpDialog(self)
        self.notice = label("", "notice")
        self.notice.hide()
        layout.addWidget(self.notice)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.sidebar = self._build_sidebar()
        splitter.addWidget(self.sidebar)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_live(), "实时实验")
        self.tabs.addTab(self._build_history(), "实验结果与回放")
        self.tabs.addTab(self._build_diagnostics(), "离线分析")
        self.logs = QtWidgets.QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(250)
        self.tabs.addTab(self.logs, "运行日志")
        self.finger_workspace = FingerDisplacementWidget()
        self.finger_tab = self.tabs.addTab(self.finger_workspace, "手指标定与验证")
        self.finger_workspace.begin_requested.connect(self._finger_begin)
        self.finger_workspace.cancel_requested.connect(self._finger_cancel)
        self.finger_workspace.action_requested.connect(self._finger_action)
        self.finger_workspace.analyze_requested.connect(self._finger_analyze)
        self.finger_workspace.current_requested.connect(self._finger_current)
        self.finger_workspace.export_requested.connect(lambda path: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path)))
        self.temperature_workspace = TemperaturePressureWidget(Path(self.service.settings.root))
        self.temperature_tab = self.tabs.addTab(self.temperature_workspace, "温压采集")
        self.serial_archive_workspace=SerialArchiveWidget()
        self.serial_archive_tab=self.tabs.addTab(self.serial_archive_workspace,"温压档案详情")
        self.serial_archive_workspace.calibration_delete_requested.connect(self._delete_calibration_points)
        self.serial_archive_workspace.calibration_restore_requested.connect(lambda payload: self._delete_calibration_points(payload,restore=True))
        self.temperature_workspace.run_completed.connect(self._temperature_complete)
        self.temperature_workspace.error.connect(self._error)
        self.temperature_workspace.message.connect(self._message)
        self.temperature_workspace.preparation_changed.connect(lambda: self._temperature_busy_changed(False))
        self.temperature_workspace.busy_changed.connect(self._temperature_busy_changed)
        self.temperature_workspace.capture_state_changed.connect(self._temperature_busy_changed)
        self.temperature_workspace.set_environment_required(True)
        self.temperature_workspace.environment_invalidated.connect(lambda: self._invalidate_environment("serial"))
        self.temperature_workspace.port.currentIndexChanged.connect(self._serial_target_changed)
        self.profile.currentIndexChanged.connect(self._global_profile_changed)
        analysis_project = Path(getattr(self.service.settings, "analysis_project", "../rf-analysis")).expanduser()
        if not analysis_project.is_absolute():
            analysis_project = Path(self.service.settings.root) / analysis_project
        self.analysis_workspace = AnalysisWorkbenchWidget(
            root=Path(self.service.settings.root), analysis_project=analysis_project.resolve(),
            python_executable=getattr(self.service.settings, "analysis_python", None))
        self.analysis_tab = self.tabs.addTab(self.analysis_workspace, "研究分析与模型")
        self.analysis_workspace.busy_changed.connect(self._refresh_navigation_enabled)
        if hasattr(self.service, 'bind_archive_activity'):
            self.service.bind_archive_activity(lambda: self.temperature_workspace.session,
                                               lambda: self.temperature_workspace.preparation,
                                               lambda: self.analysis_workspace.bridge)
        self.liquid_workspace = LiquidTemperatureWidget(self.service)
        self.liquid_tab = self.tabs.addTab(self.liquid_workspace, "液体温度影响")
        self.liquid_workspace.prepare_requested.connect(self._prepare_liquid)
        self.liquid_workspace.mode.currentIndexChanged.connect(self._liquid_conditions_edited)
        for field in self.liquid_workspace.fields.values():
            field.textChanged.connect(self._liquid_conditions_edited)
        self.liquid_workspace.error.connect(self._error)
        self.liquid_workspace.busy_changed.connect(self._temperature_busy_changed)
        self.temperature_sweep_workspace = TemperatureSweepWidget(Path(self.service.settings.root))
        self.temperature_sweep_tab = self.tabs.addTab(self.temperature_sweep_workspace, "扫频与温度")
        self.temperature_sweep_workspace.set_port(self.temperature_workspace.port.currentData() or "")
        self.temperature_sweep_workspace.port_changed.connect(self._joint_port_changed)
        self.temperature_sweep_workspace.configuration_changed.connect(self._joint_conditions_changed)
        self.temperature_sweep_workspace.prepare_temperature_requested.connect(self._prepare_joint_temperature)
        self.temperature_sweep_settings = self.temperature_sweep_workspace.take_settings_widget()

        self.tabs.setObjectName("workspaceTabs")
        self.tabs.setUsesScrollButtons(True)
        self.finger_settings = self.finger_workspace.take_settings_widget()
        self.sidebar_config_layout.insertWidget(1, self.finger_settings)
        self.liquid_settings = self.liquid_workspace.take_settings_widget()
        self.sweep_options = UnifiedSweepSettings(self.temperature_sweep_workspace, self.liquid_workspace)
        self.condition_form.addRow(self.sweep_options)
        self.sweep_options.changed.connect(self._joint_conditions_changed)
        self.material_workspace.material_name.textChanged.connect(self._refresh_navigation_enabled)
        self.fields['label'].textChanged.connect(self._refresh_navigation_enabled)
        self.temperature_settings = self.temperature_workspace.take_settings_widget()
        self.sidebar_config_layout.insertWidget(0, self.temperature_settings)
        self.tabs.currentChanged.connect(self._tab_changed)
        self._default_draft = self._capture_draft()
        self._navigation_ready = True
        self._show_experiment()
        self._mode_changed()
        splitter.addWidget(self.tabs)
        splitter.setSizes([340, 1100])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.statusBar().showMessage(f"数据保存在 {Path(self.service.settings.root) / 'Data'}   ·   rf-gnuradio")
        self.setStyleSheet("""
            QMainWindow {background: #eef3f5;} QWidget {font-size: 13px; color: #243e50;}
            QLabel#title {font-size: 25px; font-weight: 700; color: #113644;}
            QLabel#muted {color: #657e8b;} QLabel#status {font-size: 20px; font-weight: 650; color: #087f85;}
            QLabel#notice {background: #fff5d9; color: #654a18; border: 1px solid #ead19c; padding: 9px; border-radius: 5px;}
            QLabel#viewBanner {background: #e4f1f0; color: #075e66; padding: 8px; border-radius: 5px; font-weight: 600;}
            QGroupBox {background: #ffffff; border: 1px solid #d7e1e6; border-radius: 6px; margin-top: 12px; padding: 13px 8px 8px;}
            QGroupBox::title {subcontrol-origin: margin; left: 10px; padding: 0 4px; font-weight: 600;}
            QPushButton {border: 1px solid #bdd0d9; border-radius: 5px; background: #ffffff; padding: 7px 10px;}
            QPushButton:hover {background: #e5f1f2;} QPushButton:disabled {color: #8c9ca5; background: #eff2f3; border-color: #dce2e5;}
            QPushButton#primary {color: white; background: #087f85; border-color: #087f85; font-weight: 600;}
            QPushButton#stop {color: #a03e42; border-color: #d7a6a7; background: #fff3f2; font-weight: 600;}
            QPushButton#primary:disabled, QPushButton#stop:disabled {color: #8c9ca5; background: #eff2f3; border-color: #dce2e5;}
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {background: #ffffff; border: 1px solid #bdcdd5; border-radius: 4px; padding: 5px;}
            QTabWidget::pane {border: 1px solid #d3e0e5; background: #ffffff;} QTabBar::tab {padding: 9px 12px;}
            QTabBar#experimentNav::tab {padding: 14px 16px; font-size: 16px; font-weight: 600; border-bottom: 3px solid transparent;}
            QTabBar#experimentNav::tab:selected {color: #087f85; border-bottom-color: #087f85; background: #ffffff;}
            QLabel#sidebarTitle {font-size: 18px; font-weight: 650; padding: 6px 0;}
            QTabBar::tab:selected {background: #ffffff; color: #087f85;} QTableWidget {border: 1px solid #dbe5e9; gridline-color: #eef2f4;}
            QHeaderView::section {background: #edf3f5; border: none; padding: 8px; font-weight: 600;}
            QCheckBox {spacing: 7px;} QScrollArea {border: none; background: transparent;}
        """)

    def _environment_target(self, kind=None):
        kind = kind or ("serial" if self.experiment_id in ("temperature", "temperature_calibration", "pressure_calibration") else "usrp")
        if kind == "serial" and self._sweep_requires_serial():
            return {"kind": "serial", "port": self.temperature_sweep_workspace.port.currentData() or "",
                    "protocol": "temperature_v1"}
        if kind == "serial":
            return {"kind": "serial", "port": self.temperature_workspace.port.currentData() or "",
                    "protocol": self.temperature_workspace.protocol.currentData()}
        return {"kind": "usrp", "profile": self.profile.currentData()}

    def _environment_snapshot(self, kind=None):
        return self.service.environment_snapshot(kind)

    def _environment_busy(self):
        return self._environment_pending or self._environment_snapshot().get("state") == "initializing"

    def _environment_ready(self, kind=None):
        target = self._environment_target(kind)
        state = self._environment_snapshot(target["kind"])
        if target["kind"] == "serial":
            return (not self._environment_busy() and state.get("state") == "ready"
                    and (state.get("target") or {}).get("port") == target["port"]
                    and (not self._sweep_requires_serial() or (state.get("target") or {}).get("protocol") == "temperature_v1"))
        return (not self._environment_busy() and state.get("state") == "ready"
                and state.get("target") == target)

    def _invalidate_environment(self, kind=None):
        self.service.invalidate_environment(kind)
        self._temperature_busy_changed(False)

    def _sweep_requires_serial(self):
        return self.experiment_id == "material" and hasattr(self, "sweep_options") and self.sweep_options.requires_serial

    def _joint_conditions_changed(self, *_):
        if not self._navigating and hasattr(self, "sweep_options"):
            self._mode_changed()
            self._refresh_navigation_enabled()

    def _joint_port_changed(self, port):
        serial = self.temperature_workspace
        index = serial.port.findData(port)
        if port and index < 0:
            serial.port.addItem(port, port)
            index = serial.port.count() - 1
        serial.port.setCurrentIndex(index)
        self._invalidate_environment("serial")

    def _prepare_joint_temperature(self):
        if not self.select_experiment("temperature"):
            return
        serial = self.temperature_workspace
        serial.protocol.setCurrentIndex(serial.protocol.findData("temperature_v1"))
        self._message("选择温度 v1，验证已有固件；需要更换固件时使用“烧录并验证”。完成后返回 SSPP 扫频。")

    def _serial_target_changed(self, *_):
        if not self._navigation_ready or self._navigating:
            return
        if hasattr(self, "temperature_sweep_workspace"):
            self.temperature_sweep_workspace.set_port(self.temperature_workspace.port.currentData() or "")
        self._invalidate_environment("serial")

    def _global_profile_changed(self, *_):
        if self._navigation_ready:
            self._invalidate_environment("usrp")

    def open_environment_dialog(self):
        """仅打开设备确认弹窗；接受选择后才提交后台初始化。"""
        if self._navigation_blocked():
            self._message("请先完成当前采集、检查或结果处理，再初始化实验台。")
            return
        if self.environment_dialog is not None:
            self.environment_dialog.raise_()
            self.environment_dialog.activateWindow()
            return
        profiles = [{"id": self.profile.itemData(index), "name": self.profile.itemText(index)}
                    for index in range(self.profile.count())]
        serial_target = self._environment_target("serial")
        dialog = EnvironmentDialog(profiles, self.profile.currentData(), serial_target=serial_target, parent=self)
        self.environment_dialog = dialog

        def accepted():
            if self._navigation_blocked():
                self._message("当前设备正在忙，请完成后重新打开初始化弹窗。")
                return
            targets = dialog.selected_targets()
            if not any(targets.values()):
                self._message("请至少选择一个要检查的设备。")
                return
            if targets["usrp"] is not None:
                index = self.profile.findData(dialog.selected_profile())
                if index < 0:
                    self._message("所选 B210 配置已不可用，请检查本机配置后重试。")
                    return
                self.profile.setCurrentIndex(index)
            if targets["serial"] is not None:
                previous = self._environment_target("serial")
                serial = self.temperature_workspace
                previous_protocol = serial.protocol.currentData()
                with QtCore.QSignalBlocker(serial.port), QtCore.QSignalBlocker(serial.protocol):
                    index = serial.port.findData(targets["serial"]["port"])
                    if index < 0:
                        serial.port.addItem(targets["serial"]["port"], targets["serial"]["port"])
                        index = serial.port.count() - 1
                    serial.port.setCurrentIndex(index)
                    serial.protocol.setCurrentIndex(serial.protocol.findData(targets["serial"]["protocol"]))
                self.temperature_sweep_workspace.set_port(targets["serial"]["port"])
                if previous_protocol != targets["serial"]["protocol"]:
                    serial._protocol_changed()
                if previous != self._environment_target("serial"):
                    self._invalidate_environment("serial")
            self.initialize_environment(targets)

        def finished(_):
            if self.environment_dialog is dialog:
                self.environment_dialog = None
            dialog.deleteLater()

        dialog.accepted.connect(accepted)
        dialog.finished.connect(finished)
        dialog.open()

    def initialize_environment(self, targets=None):
        if self._navigation_blocked():
            self._message("请先完成当前采集、检查或结果处理，再初始化实验环境。")
            return
        if targets is None:
            targets = {"usrp": self._environment_target("usrp") if self.profile.currentData() else None,
                       "serial": self._environment_target("serial") if self.temperature_workspace.port.currentData() else None}
        if not any(targets.values()):
            self._message("当前没有可检查的设备；请在初始化弹窗选择设备或刷新串口。")
            return
        cancellation = threading.Event()
        self._environment_cancel_event = cancellation
        self._environment_pending = True
        if targets.get("serial") is not None:
            self.temperature_workspace.invalidate_preparation()
        self._set_active(False)
        self._message("正在初始化所选设备，详细过程实时显示在启动器终端；可点击顶部取消初始化。")
        def done(result):
            self._environment_pending = False
            self._temperature_busy_changed(False)
            if not self._close_requested:
                message = result.get("message") or "初始化检查完成；填写参数后可直接开始实验。"
                if cancellation.is_set():
                    message = "已取消剩余设备检查；已完成的就绪状态保留。 " + message
                if result.get("state") == "partial":
                    message = "部分设备就绪，已就绪设备可开始实验。 " + message
                message = " ".join(str(message).split())
                if len(message) > 240:
                    message = message[:240] + "…"
                log_path = result.get("log_path") or self._environment_snapshot().get("log_path")
                if log_path:
                    message += f"\n详细过程已输出到启动器终端；日志：{log_path}"
                self._message(message)
        def failed(error):
            self._environment_pending = False
            self._temperature_busy_changed(False)
            self._error(error)
        if not self._submit("initialize_environment", lambda: self.service.initialize_workbench(targets, cancel_event=cancellation), done, "control", failed):
            self._environment_pending = False
            self._temperature_busy_changed(False)

    def _cancel_environment(self):
        if self._environment_cancel_event is not None:
            self._environment_cancel_event.set()
        self.stop_button.setEnabled(False)
        self._message("正在取消设备初始化，等待设备检查进程退出。")
        self._submit("cancel_environment", self.service.cancel_environment_initialization,
                     lambda _: self._temperature_busy_changed(False), "urgent")

    def _refresh_navigation_enabled(self):
        self._temperature_busy_changed(False)

    def _navigation_blocked(self):
        return (self._archive_busy or self._environment_busy() or self._parameter_reset_blocked() or self.temperature_workspace.is_running
                or self.liquid_workspace.is_busy or self._comparison_busy
                or "comparison_export" in self.runner.pending or "figure_export" in self.runner.pending
                or "calibration_correction" in self.runner.pending)

    def _experiment_changed(self, index):
        if self._navigation_ready and not self._navigating:
            self.select_experiment(self.experiment_nav.tabData(index))

    def select_experiment(self, key):
        """Switch only idle experiment context; never prepare or start hardware here."""
        legacy = key if key in ("liquid", "temperature_sweep") else None
        key = "material" if legacy else key
        if key not in EXPERIMENTS:
            return False
        if self._navigation_blocked():
            with QtCore.QSignalBlocker(self.experiment_nav):
                self.experiment_nav.setCurrentIndex(list(EXPERIMENTS).index(self.experiment_id))
            self._message("当前采集、参数检查或结果处理尚未完成；停止并保存后可切换实验。")
            return False
        if key == self.experiment_id:
            if legacy == "liquid":
                self.sweep_options.liquid_enabled.setChecked(True)
            elif legacy == "temperature_sweep":
                self.sweep_options.temperature_mode.setCurrentIndex(1)
            return True
        self._experiment_drafts[self.experiment_id] = self._capture_draft()
        draft = deepcopy(self._experiment_drafts.get(key, self._default_draft))
        if key not in self._experiment_drafts:
            draft["values"]["mode"] = "phase" if key == "finger" else "sweep"
            draft["values"]["label"] = "finger_session" if key == "finger" else ""
            draft["material_name"] = ""
            draft["liquid_setup"] = None
            if key == "material":
                draft["values"].update(SSPP_DEFAULTS)
            elif key == "finger":
                # 首次手指草稿不能继承 SSPP 面板的扫频默认值。
                draft["values"].update({name: DEFAULTS[name] for name in SSPP_DEFAULTS})
        self._navigating = True
        try:
            self.experiment_id = key
            self.temperature_workspace.set_calibration_mode(key == "temperature_calibration")
            self.temperature_workspace.set_pressure_calibration_mode(key == "pressure_calibration")
            with QtCore.QSignalBlocker(self.experiment_nav):
                self.experiment_nav.setCurrentIndex(list(EXPERIMENTS).index(key))
            self._restore_draft(draft)
            if legacy == "liquid":
                self.sweep_options.liquid_enabled.setChecked(True)
            elif legacy == "temperature_sweep":
                self.sweep_options.temperature_mode.setCurrentIndex(1)
            self._clear_history_details()
            self.clear_comparison_selection()
            self.analysis_plot.display({})
            self.diagnostic_model = {}
            self.analysis_title.setText("选择历史记录后加载完整诊断")
            self._update_history_type_options()
            self.history_type.setCurrentIndex(0)
            self.history_status.setCurrentIndex(0)
            with QtCore.QSignalBlocker(self.history_search):
                self.history_search.clear()
            self._render_history_rows()
            self._show_experiment()
            self._mode_changed()
            self._live_revision = None
            self._render_live({"mode": self.mode.currentData(), "points": []})
            self._message("")
            self._set_active(False)
        finally:
            self._navigating = False
        self._temperature_busy_changed(False)
        return True

    def _capture_draft(self):
        values = {}
        for key, widget in self.fields.items():
            if isinstance(widget, QtWidgets.QComboBox):
                values[key] = widget.currentData()
            elif isinstance(widget, QtWidgets.QCheckBox):
                values[key] = widget.isChecked()
            elif isinstance(widget, QtWidgets.QLineEdit):
                values[key] = widget.text()
            else:
                values[key] = widget.value()
        return {"values": values, "material_name": self.material_workspace.material_name.text(),
                "advanced": self.advanced_toggle.isChecked(), "liquid_setup": None,
                "sweep_options": self.sweep_options.draft()}

    def _restore_draft(self, draft):
        if 'weak_point_extra_seconds' not in draft['values']:
            with QtCore.QSignalBlocker(self.fields['weak_point_extra_seconds']):
                self.fields['weak_point_extra_seconds'].setValue(DEFAULTS['weak_point_extra_seconds'])
        for key, value in draft["values"].items():
            # 旧草稿的设备选择和已移除的扫频时限均不覆盖当前运行规则。
            if key in ("profile", "max_seconds", "auto_sweep_budget", "setup_confirmed"):
                continue
            widget = self.fields[key]
            with QtCore.QSignalBlocker(widget):
                if isinstance(widget, QtWidgets.QComboBox):
                    index = widget.findData(value)
                    if index >= 0:
                        widget.setCurrentIndex(index)
                elif isinstance(widget, QtWidgets.QCheckBox):
                    widget.setChecked(bool(value))
                elif isinstance(widget, QtWidgets.QLineEdit):
                    widget.setText(value)
                else:
                    widget.setValue(value)
        self.material_workspace.material_name.setText(draft["material_name"])
        self.advanced_toggle.setChecked(draft["advanced"])
        self._liquid_setup = None
        self.sweep_options.restore_draft(draft.get("sweep_options"))

    def _show_experiment(self):
        key = self.experiment_id
        name, description, _ = EXPERIMENTS[key]
        self.sidebar_title.setText(name + " · 参数设置")
        self.experiment_description.setText(description)
        self.archive_button.setText("实验档案")
        self.archive_button.setToolTip("查看当前实验的已保存记录；勾选单条分析，或勾选多条同类记录组合分析。")
        self._update_history_type_options()
        self.config_container.setVisible(key not in ("temperature", "temperature_calibration", "pressure_calibration"))
        self.rf_actions.setVisible(key not in ("temperature", "temperature_calibration", "pressure_calibration"))
        self.temperature_settings.setVisible(key in ("temperature", "temperature_calibration", "pressure_calibration"))
        self.finger_settings.setVisible(key == "finger")
        self.sidebar_scroll.verticalScrollBar().setValue(0)
        visible = {0, 1, 2, 3, self.analysis_tab}
        if key == "finger":
            visible.add(self.finger_tab)
        if key in ("material", "finger"):
            visible.add(self.liquid_tab)
        if key == "material" and self.sweep_options.temperature_mode.currentData() != "none":
            visible.add(self.temperature_sweep_tab)
        elif key in ("temperature", "temperature_calibration", "pressure_calibration"):
            visible = {self.temperature_tab, self.analysis_tab, 1}
        self.tabs.blockSignals(True)
        for index in range(self.tabs.count()):
            self.tabs.setTabVisible(index, index in visible)
        self.tabs.setCurrentIndex(self.temperature_tab if key in ("temperature", "temperature_calibration", "pressure_calibration") else
                                  self.temperature_sweep_tab if key == "material" and self.sweep_options.temperature_mode.currentData() != "none" else 0)
        self.tabs.blockSignals(False)
        self.tabs.setTabText(self.temperature_tab, "实时曲线")
        self.tabs.setTabText(self.liquid_tab, "温度记录与比较")
        self.tabs.setTabText(1, "实验档案")
        self.tabs.setTabText(2, "曲线诊断")
        self.tabs.setTabText(self.analysis_tab, "研究分析")
        if key in ("material", "finger") and hasattr(self.service, "liquid_history"):
            self.liquid_workspace.refresh_history()

    def _stop_current_experiment(self):
        if self._environment_busy():
            self._cancel_environment()
        elif self.temperature_workspace.is_running:
            self.temperature_workspace.stop_capture()
        else:
            self.stop_experiment()

    def open_archive(self):
        for field in (self.history_type, self.history_status):
            with QtCore.QSignalBlocker(field):
                field.setCurrentIndex(0)
        with QtCore.QSignalBlocker(self.history_search):
            self.history_search.clear()
        self._filter_history()
        self.refresh_history()
        self.tabs.setTabVisible(1, True)
        self.tabs.setCurrentIndex(1)

    @staticmethod
    def _job_experiment(job):
        config = (job or {}).get("config", {})
        return {"phase": "finger", "sweep": "material"}.get(config.get("mode"))

    def _show_tab(self, index):
        self.tabs.setTabVisible(index, True)
        self.tabs.setCurrentIndex(index)

    def _tab_changed(self, index):
        if index == self.liquid_tab and hasattr(self.service, "liquid_history"):
            self.liquid_workspace.refresh_history()

    def _build_sidebar(self):
        side = QtWidgets.QWidget()
        side.setMinimumWidth(300)
        side.setMaximumWidth(425)
        layout = QtWidgets.QVBoxLayout(side)
        layout.setContentsMargins(0, 0, 8, 0)
        self.sidebar_title = label("", "sidebarTitle")
        title_row = QtWidgets.QHBoxLayout()
        title_row.addWidget(self.sidebar_title, 1)
        self.experiment_help_button = QtWidgets.QToolButton()
        self.experiment_help_button.setText("?")
        self.experiment_help_button.setAccessibleName("采集流程帮助")
        self.experiment_help_button.setToolTip("查看采集流程与说明")
        self.experiment_help_button.setFixedSize(26, 26)
        self.experiment_help_button.clicked.connect(self.open_experiment_help)
        title_row.addWidget(self.experiment_help_button)
        layout.addLayout(title_row)
        scroll = QtWidgets.QScrollArea()
        self.sidebar_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QtWidgets.QWidget()
        config_layout = QtWidgets.QVBoxLayout(holder)
        self.sidebar_config_layout = config_layout
        config_layout.setContentsMargins(0, 0, 0, 0)
        self.config_container = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(self.config_container)
        self.rf_config_layout = cl
        cl.setContentsMargins(0, 0, 0, 0)
        self.mode = self._combo("mode", [("手指位移 · 固定频率", "phase"), ("材料响应 · 逐点扫频", "sweep")])
        self.mode.setParent(self.config_container)
        self.mode.setCurrentIndex(self.mode.findData("sweep"))
        self.mode.hide()
        self.material_workspace = MaterialSweepWidget()
        self.condition_form = self.material_workspace.layout()
        self.conditions_group = self.material_workspace
        self.material_workspace.layout().labelForField(self.material_workspace.material_name).setText("样品／条件名称 *")
        self.material_workspace.material_name.setPlaceholderText("例如 水样_30C、空瓶、材料A_重复1")
        self.material_workspace.instructions.hide()
        cl.addWidget(self.material_workspace)
        self._combo("condition", [("手指距离实验", "finger"), ("材料响应实验", "material")]).hide()
        self.fields["label"] = QtWidgets.QLineEdit()
        self.fields["label"].setMaxLength(80)
        self.fields["label"].setToolTip("文字、数字、下划线和连字符")
        self.session_label = label("样品／会话标签 *")
        self.condition_form.addRow(self.session_label, self.fields["label"])
        self.fields["sensor"] = QtWidgets.QLineEdit()
        self.fields["sensor"].setMaxLength(80)
        self.fields["sensor"].setPlaceholderText("选填：传感器名称／版本")
        self.condition_form.addRow("传感器", self.fields["sensor"])
        self.phase_group = QtWidgets.QGroupBox("手指实验 · 固定频率连续记录")
        pf = QtWidgets.QFormLayout(self.phase_group)
        self._number(pf, "carrier_ghz", "载频 GHz", .07, 6, 3, .001)
        self._number(pf, "duration", "记录时长 s", 10, 180, 1, 10)
        self._number(pf, "tone", "基带单音 Hz", 1, 50000, 0, 100)
        cl.addWidget(self.phase_group)
        self.sweep_group = QtWidgets.QGroupBox("材料实验 · 扫频计划")
        sf = QtWidgets.QFormLayout(self.sweep_group)
        for key, name, lo, hi, decimals, step in [
            ("freq_start_ghz", "起点 GHz", .07, 6, 3, .001), ("freq_stop_ghz", "终点 GHz", .07, 6, 3, .001),
            ("points", "频点数", 2, 100, 0, 1), ("repeat", "重复轮数", 1, 20, 0, 1),
            ("point_timeout", "单点基础等待 s", 1, 30, 1, .5),
            ("weak_point_extra_seconds", "弱点额外等待 s", 0, 30, 1, .5),
            ("tone_start", "首个单音 Hz", 1, 50000, 0, 100), ("tone_step", "单音步进 Hz", 0, 20000, 0, 100)]:
            self._number(sf, key, name, lo, hi, decimals, step)
        self.sweep_completion_hint = label("按指定轮数结束 · 无总时限", "muted")
        self.sweep_completion_hint.setWordWrap(True)
        sf.addRow(self.sweep_completion_hint)
        self.sweep_weak_budget_hint = label("", "muted")
        self.sweep_weak_budget_hint.setWordWrap(True)
        sf.addRow(self.sweep_weak_budget_hint)
        self.fields['weak_point_extra_seconds'].setToolTip('仅在至少1秒的完整验证窗通过后启用一次；0表示关闭。整场实验仍无总时限，质量门槛不变。')
        self._update_weak_budget_hint()
        for key in ('points', 'repeat', 'point_timeout', 'weak_point_extra_seconds'):
            self.fields[key].valueChanged.connect(self._sweep_parameters_changed)
        cl.addWidget(self.sweep_group)
        advanced = QtWidgets.QGroupBox("高级参数")
        al = QtWidgets.QVBoxLayout(advanced)
        self.advanced_toggle = QtWidgets.QCheckBox("展开高级参数")
        self.advanced_toggle.setToolTip("单点质量等待、单音、采样率、数字幅值与增益")
        al.addWidget(self.advanced_toggle)
        advanced_fields = QtWidgets.QWidget()
        af = QtWidgets.QFormLayout(advanced_fields)
        for key, name, lo, hi, decimals, step in [
            ("rate", "采样率 S/s", 200000, 2000000, 0, 1000), ("amplitude", "数字幅值", .001, .3, 3, .001),
            ("tx_gain", "TX gain dB", 0, 30, 1, .5), ("rx_gain", "RX gain dB", 0, 76, 1, .5)]:
            self._number(af, key, name, lo, hi, decimals, step)
        af.addRow(label("可调载频 70 MHz–6 GHz；数字幅值不是实测射频功率。", "muted"))
        al.addWidget(advanced_fields)
        advanced_fields.hide()
        self.advanced_toggle.toggled.connect(advanced_fields.setVisible)
        for key in ('point_timeout', 'weak_point_extra_seconds', 'tone_start', 'tone_step'):
            row = sf.takeRow(self.fields[key])
            af.addRow(row.labelItem.widget(), row.fieldItem.widget())
        af.addRow(self.sweep_weak_budget_hint)
        cl.addWidget(advanced)
        config_layout.addWidget(self.config_container)
        config_layout.addStretch()
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)
        for key, value in SSPP_DEFAULTS.items():
            self.fields[key].setValue(value)
        self.rf_actions = QtWidgets.QWidget()
        actions = QtWidgets.QVBoxLayout(self.rf_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        self.start_readiness = label("先初始化所需设备", "muted")
        actions.addWidget(self.start_readiness)
        self.reset_button = QtWidgets.QPushButton("当前实验参数复位")
        self.reset_button.setToolTip("恢复当前实验的全部采集参数和高级参数，不影响其他实验草稿；保留设备、材料名称、会话标签、传感器与手指标定记录。")
        self.reset_button.clicked.connect(self.reset_parameters)
        actions.addWidget(self.reset_button)
        buttons = QtWidgets.QHBoxLayout()
        self.check_button = QtWidgets.QPushButton("检查参数")
        self.check_button.clicked.connect(self.preflight)
        self.start_button = QtWidgets.QPushButton("开始实验")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start_experiment)
        buttons.addWidget(self.check_button)
        buttons.addWidget(self.start_button)
        actions.addLayout(buttons)
        layout.addWidget(self.rf_actions)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self._mode_changed()
        for form in side.findChildren(QtWidgets.QFormLayout):
            form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        return side

    def _sweep_parameters_changed(self,*_):
        self._update_weak_budget_hint()
        if getattr(self,'_navigating',False):return

    def _update_weak_budget_hint(self):
        base = self.fields['point_timeout'].value()
        extra = self.fields['weak_point_extra_seconds'].value()
        self.sweep_weak_budget_hint.setText(
            f'长验证窗通过后最多额外等待 {extra:g} 秒；每点最多 {base+extra:g} 秒。仍不合格则保留缺测，整场无总时限。'
            if extra else f'弱点额外等待已关闭；每点最多 {base:g} 秒，整场无总时限。')

    def _combo(self, key, items):
        combo = QtWidgets.QComboBox()
        for title, value in items:
            combo.addItem(title, value)
        self.fields[key] = combo
        return combo

    def _number(self, form, key, title, lo, hi, decimals, step):
        widget = QtWidgets.QDoubleSpinBox() if decimals else QtWidgets.QSpinBox()
        if decimals:
            widget.setDecimals(decimals)
        widget.setRange(lo, hi)
        widget.setSingleStep(step)
        widget.setValue(DEFAULTS[key])
        widget.setKeyboardTracking(False)
        form.addRow(title, widget)
        self.fields[key] = widget

    def _mode_changed(self):
        phase = self.mode.currentData() == "phase"
        options = getattr(self, 'sweep_options', None)
        if options:
            options.set_phase(phase)
        liquid = bool(options and options.liquid_enabled.isChecked())
        self.phase_group.setVisible(phase)
        self.sweep_group.setVisible(not phase)
        self.material_workspace.setVisible(True)
        self.material_workspace.setTitle("样品与条件")
        self.condition_form.setRowVisible(self.material_workspace.material_name, not phase)
        self.fields["condition"].setCurrentIndex(self.fields["condition"].findData("finger" if phase else "material"))
        self.condition_form.setRowVisible(self.fields["label"], phase)
        self.start_button.setText("开始液体相位记录" if phase and liquid else "开始手指实验" if phase else "开始扫频")
        self.phase_group.setTitle("固定频率 · 连续幅相")
        self.sweep_group.setTitle("扫频设置")
        continuous = bool(options and self.experiment_id == 'material' and options.requires_serial)
        self.fields['repeat'].setEnabled(not continuous)
        self.sweep_completion_hint.setText("持续循环 · 点击停止并保存" if continuous else "按指定轮数结束 · 无总时限")
        self._update_experiment_help()
        self._update_weak_budget_hint()
        if hasattr(self, "tabs") and hasattr(self, "finger_tab"):
            self.tabs.setTabEnabled(self.finger_tab, phase and not liquid)
            if options and self.experiment_id == 'material':
                self.tabs.setTabVisible(self.temperature_sweep_tab, options.temperature_mode.currentData() != 'none')
        if hasattr(self, "live_plots"):
            self.workflow_shortcut.setVisible(phase and not liquid)
            self.mark_group.setVisible(phase and not liquid and self.viewing_key is None and self.replay is None)
            if not self.snapshot.get("job") and self.viewing_key is None:
                self._render_live({"mode": "phase" if phase else "sweep", "points": []})
                self._live_revision = None

    def _update_experiment_help(self):
        key = self.experiment_id
        if key == 'material':
            text = ('初始化 B210 → 填写样品与扫频设置 → 开始扫频 → 完成后在实验档案查看与比较。'
                    '\n温度可不记录、手填参考值或由 Arduino 连续记录；手填不需要 Arduino，连续记录需温度固件验证。'
                    '液体条件随采集保存，结束后可在“温度记录与比较”补录终温。'
                    '\n保持样品、接触位置和线缆固定。有限轮按轮数完成，连续记录手动停止；扫频不设整场时限。')
        elif key == 'finger':
            text = ('初始化 B210 → 填写样品／会话标签 → 开始固定频率记录 → 静置约 7 秒，通过参考检查后再标记。'
                    '\n手指标定仅在同一次连续记录内使用。添加液体条件后改为液体相位记录，'
                    '每个稳定温度分别记录参考位置与位移位置，不跨记录相减绝对相位。')
        elif key == 'pressure_calibration':
            text = ('初始化 Arduino → 验证所选固件 → 选择实际压力通道 → 开始记录。'
                    '\n静置后手动置零；在参考力区间之间可切换通道，切换会清除基线。'
                    '参考力、接触面积与区间标签需实际测量，软件拟合不会自动激活。')
        elif key == 'temperature_calibration':
            text = ('初始化 Arduino → 验证温度固件 → 开始记录 → 每个稳定平台填写参考温度并标记区间。'
                    '\n原始电压与人工温度标签分别保存；在实验档案查看、组合分析或删除误标区间。')
        else:
            text = ('初始化 Arduino → 验证所选固件 → 填写样品与通道 → 开始采集 → 停止并保存。'
                    '\n原始 ADC／电压始终保留；未经标定的电压不直接视为温度或压力。')
        self.help_dialog.set_content(EXPERIMENTS[key][0], text)

    def open_experiment_help(self):
        self._update_experiment_help()
        self.help_dialog.show()
        self.help_dialog.raise_()
        self.help_dialog.activateWindow()

    def _build_live(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        top = QtWidgets.QHBoxLayout()
        self.view_banner = label("当前实验 · 等待真实采集", "viewBanner")
        top.addWidget(self.view_banner, 1)
        self.return_live_button = QtWidgets.QPushButton("返回当前实验")
        self.return_live_button.clicked.connect(self.return_live)
        top.addWidget(self.return_live_button)
        self.current_result_button = QtWidgets.QPushButton("本次结果与导出 →")
        self.current_result_button.setEnabled(False)
        self.current_result_button.clicked.connect(self._open_current_result)
        top.addWidget(self.current_result_button)
        layout.addLayout(top)
        self.workflow_shortcut = QtWidgets.QPushButton("手指研究：采集稳定窗口、标定与验证 →")
        self.workflow_shortcut.clicked.connect(lambda: self.tabs.setCurrentIndex(self.finger_tab))
        layout.addWidget(self.workflow_shortcut)
        self.metrics = label("幅度 —     相位 —     阶段 —", "muted")
        layout.addWidget(self.metrics)
        self.live_plots = LivePlots()
        layout.addWidget(self.live_plots, 3)
        self.diagnostic_toggle = QtWidgets.QCheckBox("展开实时 RX 诊断（时域 / 频谱 / IQ）")
        self.diagnostic_toggle.setChecked(False)
        self.preview_plots = PreviewPlots()
        self.preview_plots.hide()
        self.diagnostic_toggle.toggled.connect(self._toggle_preview)
        layout.addWidget(self.diagnostic_toggle)
        layout.addWidget(self.preview_plots, 2)
        self.replay_note = label("", "muted")
        self.replay_note.hide()
        layout.addWidget(self.replay_note)
        self.live_layout = layout
        self.preview_summary = label("等待真实 RX 样本；显示抽取窗口，完整 IQ 独立保存。", "muted")
        layout.addWidget(self.preview_summary)
        self.mark_group = QtWidgets.QGroupBox("手动阶段标记 · 静止参考通过后可用")
        ml = QtWidgets.QVBoxLayout(self.mark_group)
        shortcuts = QtWidgets.QHBoxLayout()
        for title, value in [("靠近", "finger_near"), ("远离", "finger_far"), ("材料已加载", "material_loaded"), ("材料已移除", "material_removed")]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda checked=False, stage=value: self.mark(stage))
            shortcuts.addWidget(button)
        ml.addLayout(shortcuts)
        row = QtWidgets.QHBoxLayout()
        self.mark_label = QtWidgets.QLineEdit()
        self.mark_label.setPlaceholderText("自定义阶段，例如 water_2of3")
        self.mark_label.setMaxLength(80)
        row.addWidget(self.mark_label, 1)
        row.addWidget(label("过渡 s"))
        self.settle = QtWidgets.QDoubleSpinBox()
        self.settle.setRange(0, 30)
        self.settle.setSingleStep(.5)
        self.settle.setValue(3)
        row.addWidget(self.settle)
        self.contact = QtWidgets.QCheckBox("接触传感器或线缆")
        row.addWidget(self.contact)
        mark_button = QtWidgets.QPushButton("记录阶段")
        mark_button.clicked.connect(lambda: self.mark(self.mark_label.text()))
        row.addWidget(mark_button)
        ml.addLayout(row)
        self.mark_state = label("按钮记录操作时刻；实际距离需另行测量。", "muted")
        ml.addWidget(self.mark_state)
        self.mark_group.setEnabled(False)
        layout.addWidget(self.mark_group)
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _build_history(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(label("已保存实验 · 单行查看详情，勾选后分析或删除", "viewBanner"), 1)
        refresh = QtWidgets.QPushButton("刷新记录")
        refresh.clicked.connect(self.refresh_history)
        top.addWidget(refresh)
        layout.addLayout(top)
        filters = QtWidgets.QHBoxLayout()
        self.history_type = QtWidgets.QComboBox()
        self.history_status = QtWidgets.QComboBox()
        self.history_status.addItem("全部状态", "")
        for key, name in STATUS.items():
            self.history_status.addItem(name, key)
        self.history_search = QtWidgets.QLineEdit()
        self.history_search.setPlaceholderText("搜索样品、传感器、温度、液体或记录")
        self.history_scope_label = label("", "muted")
        filters.addWidget(self.history_scope_label)
        filters.addWidget(self.history_type)
        filters.addWidget(self.history_status)
        filters.addWidget(self.history_search, 1)
        self.history_type.currentIndexChanged.connect(self._filter_history)
        self.history_status.currentIndexChanged.connect(self._filter_history)
        self._history_filter_timer = QtCore.QTimer(self)
        self._history_filter_timer.setSingleShot(True)
        self._history_filter_timer.setInterval(180)
        self._history_filter_timer.timeout.connect(self._filter_history)
        self.history_search.textChanged.connect(self._schedule_history_filter)
        self.history_search.returnPressed.connect(self._filter_history)
        layout.addLayout(filters)
        selection = QtWidgets.QHBoxLayout()
        self.comparison_count = label("已勾选 0 / 8 条", "muted")
        selection.addWidget(self.comparison_count, 1)
        self.comparison_clear = QtWidgets.QPushButton("清空勾选")
        self.comparison_clear.clicked.connect(self.clear_comparison_selection)
        selection.addWidget(self.comparison_clear)
        self.delete_records_button = QtWidgets.QPushButton('删除所选…')
        self.delete_records_button.clicked.connect(self.delete_selected_records)
        selection.addWidget(self.delete_records_button)
        self.deleted_records_button = QtWidgets.QPushButton('已删除记录…')
        self.deleted_records_button.clicked.connect(self.open_deleted_records)
        selection.addWidget(self.deleted_records_button)
        self.compare_button = QtWidgets.QPushButton("分析所选记录")
        self.compare_button.setObjectName("primary")
        self.compare_button.clicked.connect(self.compare_selected)
        selection.addWidget(self.compare_button)
        layout.addLayout(selection)
        self.history_table = QtWidgets.QTableWidget(0, 7)
        self.history_table.setHorizontalHeaderLabels(["选择", "开始时间（本机时区）", "样品／条件", "类型", "状态", "传感器", "温度／液体条件"])
        self.history_table.setWordWrap(False)
        self.history_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.history_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.history_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.itemSelectionChanged.connect(self._select_history)
        self.history_table.itemChanged.connect(self._comparison_item_changed)
        layout.addWidget(self.history_table, 2)
        row = QtWidgets.QGridLayout()
        self.view_history_button = QtWidgets.QPushButton("查看已保存曲线")
        self.view_history_button.clicked.connect(self._view_saved_curve)
        row.addWidget(self.view_history_button, 0, 0)
        self.analyze_button = QtWidgets.QPushButton("加载完整诊断")
        self.analyze_button.clicked.connect(self.load_diagnostics)
        row.addWidget(self.analyze_button, 0, 1)
        self.workbench_button = QtWidgets.QPushButton("研究分析 / 训练标定")
        self.workbench_button.clicked.connect(self.open_workbench)
        row.addWidget(self.workbench_button, 0, 2)
        self.export_button = QtWidgets.QPushButton("打包导出实验…")
        self.export_button.clicked.connect(self.export_bundle)
        row.addWidget(self.export_button, 1, 0)
        self.open_run_button = QtWidgets.QPushButton("打开实验目录")
        self.open_run_button.clicked.connect(self.open_run)
        row.addWidget(self.open_run_button, 1, 1)
        layout.addLayout(row)
        export_options = QtWidgets.QHBoxLayout()
        self.include_raw = QtWidgets.QCheckBox("包含原始 IQ（文件较大）")
        self.include_raw.setToolTip("勾选后加入 cf32 / npz 原始 IQ；参数、JSONL/结果、重要图表始终包含。")
        export_options.addWidget(self.include_raw)
        self.finger_history_button = QtWidgets.QPushButton("手指记录：标注区间 / 标定验证")
        self.finger_history_button.setEnabled(False)
        self.finger_history_button.clicked.connect(self._finger_load_history)
        export_options.addWidget(self.finger_history_button)
        export_options.addStretch()
        layout.addLayout(export_options)
        playback = QtWidgets.QGroupBox("历史回放 · 只读数据，不启动硬件")
        pl = QtWidgets.QHBoxLayout(playback)
        self.replay_button = QtWidgets.QPushButton("载入并播放")
        self.replay_button.clicked.connect(self.play_replay)
        row.addWidget(self.replay_button, 1, 2)
        self.pause_button = QtWidgets.QPushButton("暂停")
        self.pause_button.clicked.connect(self.pause_replay)
        pl.addWidget(self.pause_button)
        self.replay_speed = QtWidgets.QComboBox()
        self.replay_speed.addItem("1×", 1)
        self.replay_speed.addItem("4×", 4)
        pl.addWidget(self.replay_speed)
        self.replay_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.replay_slider.setRange(0, 1000)
        self.replay_slider.sliderMoved.connect(self.seek_replay)
        pl.addWidget(self.replay_slider, 1)
        self.replay_position = label("尚未载入")
        pl.addWidget(self.replay_position)
        self.replay_controls = playback
        self.live_layout.insertWidget(2, playback)
        self.live_layout.insertWidget(3, self.replay_note)
        playback.hide()
        bottom = QtWidgets.QSplitter()
        self.metadata = QtWidgets.QPlainTextEdit()
        self.metadata.setReadOnly(True)
        self.metadata.setPlaceholderText("选择实验查看状态、停止证据与采集质量。")
        bottom.addWidget(self.metadata)
        files_widget = QtWidgets.QWidget()
        fl = QtWidgets.QVBoxLayout(files_widget)
        fl.setContentsMargins(8, 0, 0, 0)
        fl.addWidget(label("实验文件 · 双击打开", "muted"))
        self.file_list = QtWidgets.QListWidget()
        self.file_list.itemDoubleClicked.connect(lambda item: self.open_file(item.text()))
        fl.addWidget(self.file_list, 1)
        save = QtWidgets.QPushButton("将所选文件另存为…")
        save.clicked.connect(self.save_file)
        fl.addWidget(save)
        bottom.addWidget(files_widget)
        layout.addWidget(bottom, 2)
        self._history_buttons(False)
        self._update_comparison_controls()
        return page

    def _build_diagnostics(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        self.analysis_title = label("选择历史记录后加载完整诊断", "viewBanner")
        layout.addWidget(self.analysis_title)
        selectors = QtWidgets.QHBoxLayout()
        self.plot_group = QtWidgets.QComboBox()
        for key, name in GROUPS.items():
            self.plot_group.addItem(name, key)
        self.plot_group.currentIndexChanged.connect(self._select_plot_group)
        self.plot_choice = QtWidgets.QComboBox()
        self.plot_choice.currentIndexChanged.connect(self._select_plot)
        selectors.addWidget(self.plot_group)
        selectors.addWidget(self.plot_choice, 1)
        self.diagnostic_figure_export_button = QtWidgets.QPushButton("导出分析图")
        self.diagnostic_figure_export_button.clicked.connect(self._export_diagnostic_figure)
        selectors.addWidget(self.diagnostic_figure_export_button)
        self.diagnostic_figure_directory_button = QtWidgets.QPushButton("打开图片文件夹")
        self.diagnostic_figure_directory_button.clicked.connect(self._open_diagnostic_figure_directory)
        selectors.addWidget(self.diagnostic_figure_directory_button)
        refresh = QtWidgets.QPushButton("重新加载")
        refresh.clicked.connect(self.load_diagnostics)
        selectors.addWidget(refresh)
        layout.addLayout(selectors)
        self.analysis_plot = DiagnosticPlot()
        layout.addWidget(self.analysis_plot, 1)
        self.quality_facts = label("", "muted")
        self.quality_facts.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.quality_facts)
        self._update_diagnostic_export_controls()
        return page

    def config(self):
        result = {"liquid_temperature": None, "temperature_sweep": None,
                  "profile": self.profile.currentData(), "max_seconds": None,
                  "setup_confirmed": self._environment_ready("usrp"),
                  "setup_confirmation_source": "global_initialization"}
        for key, widget in self.fields.items():
            if isinstance(widget, QtWidgets.QComboBox):
                result[key] = widget.currentData()
            elif isinstance(widget, QtWidgets.QCheckBox):
                result[key] = widget.isChecked()
            elif isinstance(widget, QtWidgets.QLineEdit):
                result[key] = widget.text().strip()
            else:
                result[key] = widget.value()
        if result["mode"] == "sweep":
            result["label"] = self.material_workspace.name()
        result["condition"] = "finger" if result["mode"] == "phase" else "material"
        result.update(self.sweep_options.config(result['label']))
        if result['liquid_temperature']:
            result['condition'] = 'material'
        return result

    def _liquid_conditions_edited(self, *_):
        if not self._navigating and hasattr(self, 'sweep_options'):
            self._refresh_navigation_enabled()

    def _prepare_liquid(self, request):
        """Compatibility for callers of the old apply action; no hardware actions."""
        if self._navigation_blocked():
            return
        if not self.select_experiment('finger' if request.get('mode') == 'phase' else 'material'):
            return
        for key, field in self.liquid_workspace.fields.items():
            field.setText(str(request.get('liquid_temperature', {}).get(key, '')))
        name = request.get('liquid_temperature', {}).get('sample_id', '')
        (self.fields['label'] if request.get('mode') == 'phase' else self.material_workspace.material_name).setText(name)
        self.sweep_options.liquid_enabled.setChecked(True)
        self._mode_changed()

    def _clear_liquid(self):
        self._liquid_setup = None
        # Preserve current form values after finishing a record; no hidden apply state.
        self._mode_changed()

    def _valid_workspace(self):
        try:
            if self.mode.currentData() == "sweep":
                self.material_workspace.validate()
            self.config()
            return True
        except ValueError as exc:
            self._error(exc)
            self.start_readiness.setText(str(exc))
            return False

    def _finger_failure(self, error):
        self.finger_workspace.show_error(error)
        self._error(error)

    def _finger_begin(self, request):
        if not self._submit("finger_window", lambda: self.service.finger_begin_window(**request),
                            self.finger_workspace.capture_result, "control", self._finger_failure):
            self.finger_workspace.set_request_busy(False)

    def _finger_action(self, request):
        def done(snapshot):
            self.finger_workspace.set_request_busy(False)
            self.finger_workspace.apply_snapshot(snapshot)
        if not self._submit("finger_action", lambda: self.service.finger_mark_action(**request), done,
                            "control", self._finger_failure):
            self.finger_workspace.set_request_busy(False)

    def _finger_cancel(self):
        self.finger_workspace.set_request_busy(True)
        def done(snapshot):
            self.finger_workspace.set_request_busy(False)
            self.finger_workspace.apply_snapshot(snapshot)
        if not self._submit("finger_cancel", self.service.finger_cancel_window, done, failure=self._finger_failure):
            self.finger_workspace.set_request_busy(False)

    def _finger_analyze(self, request):
        target_key = self.finger_workspace.run_key
        # Bind the source before entering a background queue; a new run cannot retarget this request.
        request = dict(request, key=target_key)
        def done(analysis):
            if self.finger_workspace.run_key == target_key:
                self.finger_workspace.apply_analysis(analysis)
            self._message(f"手指研究分析已保存：{analysis.get('export_path', '')}")
        if not self._submit("finger_analysis", lambda: self.service.finger_analyze(**request), done,
                            failure=self._finger_failure):
            self.finger_workspace.show_error("后台队列忙，请稍后重新分析。")

    def _finger_current(self):
        if not self.select_experiment("finger"):
            return
        self.finger_workspace.use_current()
        self.tabs.setTabEnabled(self.finger_tab, True)
        self.tabs.setCurrentIndex(self.finger_tab)
        self._tick()

    def _finger_load_history(self):
        if not self.selected_details:
            return
        if self._has_liquid_conditions(self.selected_details):
            self._message("请打开“温度记录与比较”分析液体记录。")
            return
        key = self.selected_details["key"]
        if not key.startswith("phase_runs/"):
            self._message("只有固定频率幅相记录可以标定手指距离。")
            return
        self._ensure_selected_curve(self._open_finger_history)

    def _open_finger_history(self, details):
        key = details['key']
        curve = details.get('curve') or {}
        if not self.select_experiment("finger"):
            return
        generation = self._history_generation
        self._message("正在读取手指研究记录…")
        def done(payload):
            if generation != self._history_generation:
                return
            self.finger_workspace.load_history(payload, curve)
            self.tabs.setTabEnabled(self.finger_tab, True)
            self.tabs.setCurrentIndex(self.finger_tab)
            self._message(payload.get("message", "历史记录仅用于离线标注与分析，不启动硬件。"))
        self._submit("finger_history", lambda: self.service.finger_load(key), done)

    def _message(self, text):
        self.notice.setText(str(text))
        self.notice.setVisible(bool(text))

    def _error(self, error):
        self._message(str(error))

    def _submit(self, key, operation, success=None, lane="analysis", failure=None):
        accepted = self.runner.submit(key, operation, success, failure or self._error, lane)
        self._refresh_navigation_enabled()
        if not accepted:
            self._message("此任务正在处理，或后台队列已满，请稍候。")
        return accepted

    def _load_profiles(self):
        def loaded(profiles):
            previous = self.profile.currentData()
            selected = previous or getattr(self.service.settings, "default_profile", "own")
            with QtCore.QSignalBlocker(self.profile):
                self.profile.clear()
                for profile in profiles:
                    self.profile.addItem(profile["name"] + (f" · CH {profile['channel']}" if "channel" in profile else ""), profile["id"])
                found = self.profile.findData(selected)
                if found >= 0:
                    self.profile.setCurrentIndex(found)
            if previous is not None and previous != self.profile.currentData():
                self._global_profile_changed()
            else:
                self._temperature_busy_changed(False)
            if not profiles:
                self._message("未找到有效设备配置。可查看历史数据；请先补齐本地设备 profile。")
        self._submit("profiles", self.service.refresh_profiles, loaded)

    def preflight(self):
        if self._archive_busy:
            self._message('请等待档案删除或恢复完成，再检查参数。')
            return
        if self.temperature_workspace.is_running:
            self._message("请等待 Arduino 固件准备、采集或保存完成，再检查实验参数。")
            return
        if self._environment_busy():
            self._message("请等待设备初始化完成，再检查实验参数。")
            return
        if not self._valid_workspace():
            return
        config = self.config()
        self.check_button.setEnabled(False)
        self.reset_button.setEnabled(False)
        def done(result):
            self.check_button.setEnabled(True)
            self.reset_button.setEnabled(not self._parameter_reset_blocked())
            self._message("参数检查通过，未打开硬件。\n" + result.get("summary", ""))
        def failed(error):
            self.check_button.setEnabled(True)
            self.reset_button.setEnabled(not self._parameter_reset_blocked())
            self._error(error)
        if not self._submit("preflight", lambda: self.service.preflight(config), done, "control", failed):
            self.check_button.setEnabled(True)
            self.reset_button.setEnabled(not self._parameter_reset_blocked())

    def _parameter_reset_blocked(self):
        return (self._archive_busy or self._close_requested or self._start_pending or self._environment_busy()
                or bool(getattr(getattr(self, 'temperature_workspace', None), 'is_running', False))
                or bool(self.snapshot.get("preparing"))
                or (self.snapshot.get("job") or {}).get("status") in BUSY
                or "start" in self.runner.pending or "preflight" in self.runner.pending
                or not self.config_container.isEnabled())

    def reset_parameters(self):
        """仅复位待采集表单；包含隐藏模式，避免留下无效的参数组合。"""
        if self._parameter_reset_blocked():
            self._message("实验启动、采集、停止保存或参数检查期间不能复位，请等待完成。")
            return
        defaults = {**DEFAULTS, **(SSPP_DEFAULTS if self.experiment_id == "material" else {})}
        for key, widget in self.fields.items():
            if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                widget.setValue(defaults[key])
        self._message("当前实验的采集参数及高级参数已恢复默认值，其他实验草稿不变。设备、名称和手指标定记录保留；初始化就绪状态保持。")

    def start_experiment(self):
        if self.experiment_id in ("temperature", "temperature_calibration", "pressure_calibration"):
            self._message("温压实验请先完成固件验证，再使用左侧“开始记录”。")
            return
        if self._navigation_blocked():
            self._message("请等待当前采集、检查或结果处理完成，再开始实验。")
            return
        if self.temperature_workspace.is_running:
            self._message("温压正在采集或保存，请完成后再开始射频实验。")
            return
        if not self._valid_workspace():
            return
        if not self._environment_ready("usrp"):
            self._message("请先点击导航最左侧的“初始化实验台”，完成所选设备检查。")
            return
        if self._sweep_requires_serial() and not self._environment_ready("serial"):
            self._message("请在初始化实验台中同时检查 B210 与温度 USB 串口。")
            return
        if self._sweep_requires_serial() and not self.temperature_workspace.preparation.matches(
                self.temperature_sweep_workspace.port.currentData() or '', 'temperature_v1', 'nano_old'):
            self._message("Arduino端口已检查，但温度固件尚未验证；请先完成温度固件准备。")
            return
        config = self.config()
        if config["mode"] == "phase" and not config.get("liquid_temperature"):
            self.finger_workspace.use_current()
        self._start_pending = True
        self.return_live()
        self._set_active(True)
        self.status_label.setText("检查参数并准备启动")
        self._message("")
        def done(snapshot):
            self._start_pending = False
            self._apply_status(snapshot)
        def failed(error):
            self._start_pending = False
            self._invalidate_environment("usrp")
            self._set_active(False)
            self._error(error)
        if not self._submit("start", lambda: self.service.start(config), done, "control", failed):
            self._start_pending = False
            self._set_active(False)

    def stop_experiment(self):
        self.status_detail.setText("已请求停止，等待收发退出与数据保存。")
        self._submit("stop", self.service.stop, self._apply_status, "urgent")

    def mark(self, stage):
        if self.finger_workspace._pending:
            self._message("请先完成或取消当前稳定窗口，再标记其他动作。")
            return
        settle, contact = self.settle.value(), self.contact.isChecked()
        self._submit("mark", lambda: self.service.mark(stage, settle, contact),
                     lambda value: self._message(f"已提交阶段：{stage}；过渡 {settle:g} 秒；接触报告：{'是' if contact else '否'}。"), "control")

    def _set_active(self, active):
        temperature_active = hasattr(self, "temperature_workspace") and self.temperature_workspace.is_running
        comparison_work = (self._archive_busy or self._comparison_busy or "comparison_export" in self.runner.pending
                           or "figure_export" in self.runner.pending or "calibration_correction" in self.runner.pending)
        liquid_work = hasattr(self, "liquid_workspace") and self.liquid_workspace.is_busy
        initializing = self._environment_busy()
        ready = self.experiment_id not in ("temperature", "temperature_calibration", "pressure_calibration") and self._environment_ready("usrp")
        if self._sweep_requires_serial():
            ready = ready and self._environment_ready("serial") and self.temperature_workspace.preparation.matches(
                self.temperature_sweep_workspace.port.currentData() or '', 'temperature_v1', 'nano_old')
        locked = active or initializing
        self.sweep_options.setEnabled(not locked and not temperature_active and not comparison_work and not self._close_requested)
        if initializing:
            reason = '正在初始化设备…'
        elif not self._environment_ready('usrp'):
            reason = '需要初始化 B210；点击顶部“初始化实验台”。'
        elif self._sweep_requires_serial() and not self._environment_ready('serial'):
            reason = '连续温度记录还需要初始化 Arduino 串口。'
        elif self._sweep_requires_serial() and not ready:
            reason = 'Arduino端口已检查；请先验证温度固件。'
        elif locked or temperature_active:
            reason = '正在采集／保存；结束后可修改参数。'
        else:
            reason = '设备已就绪，填写参数后直接开始。'
        self.start_readiness.setText(reason)
        self.config_container.setEnabled(not locked and not self._close_requested)
        self.temperature_sweep_workspace.set_external_busy(locked or temperature_active or comparison_work or self._close_requested)
        self.finger_settings.setEnabled(not initializing and not self._close_requested)
        self.temperature_workspace.config_group.setEnabled(not locked and not temperature_active and not self._close_requested)
        serial_state = self._environment_snapshot("serial")
        # A USRP-only check temporarily locks actions, but does not invalidate
        # previously verified Arduino firmware. Rechecking Arduino is explicit.
        self.temperature_workspace.set_environment_ready(
            serial_state.get("state") == "ready"
            and (serial_state.get("target") or {}).get("port") == self._environment_target("serial")["port"])
        self.reset_button.setEnabled(not active and not self._parameter_reset_blocked())
        self.start_button.setEnabled(ready and not locked and not temperature_active and not comparison_work and not liquid_work and not self._close_requested)
        self.check_button.setEnabled(not locked and not self._archive_busy and not temperature_active and "preflight" not in self.runner.pending and not self._close_requested)
        self.stop_button.setEnabled((active or temperature_active or initializing) and not self._close_requested)
        self.experiment_nav.setEnabled(not self._navigation_blocked())
        preparing_serial = self.temperature_workspace.preparation.is_running
        self.stop_button.setText("取消初始化" if initializing else "取消固件准备" if preparing_serial else "停止并保存")
        self.stop_button.setToolTip("取消当前设备初始化" if initializing else
                                   "等待正在进行的写入与校验完成后取消，请勿拔线" if preparing_serial else
                                   "停止当前采集并等待数据保存")
        self.initialize_button.setText("正在初始化…" if initializing else "初始化实验台")
        self.initialize_button.setEnabled(not self._navigation_blocked())
        state = self._environment_snapshot()
        global_name = self.profile.currentText() or "尚未选择 B210"
        device_states = {kind: self._environment_snapshot(kind) for kind in ("usrp", "serial")}
        titles = {"ready": "就绪", "initializing": "检查中", "failed": "不可用 · 可重试",
                  "cancelled": "已取消", "skipped": "本次跳过"}
        serial_title = ("数据已验证" if self.temperature_workspace.preparation_ready else
                        "端口已检查 · 待验证固件" if device_states['serial'].get('state') == 'ready' else
                        titles.get(device_states['serial'].get('state'), '未初始化'))
        if preparing_serial:
            serial_title = "固件准备中"
        self.environment_label.setText(
            f"{global_name.split(' · CH')[0]} · {titles.get(device_states['usrp'].get('state'), '未初始化')}\n"
            f"Arduino · {serial_title}")
        details = ["B210 与 Arduino 分别记录状态；温压须在端口检查后完成固件与数据验证。"]
        for kind, name in (("usrp", global_name), ("serial", "Arduino 温压串口")):
            device = device_states[kind]
            details.append(f"{name}：{device.get('message', '')}")
            if device.get("log_path"):
                details.append(f"日志：{device['log_path']}")
        environment_tip = "\n".join(details)
        self.environment_label.setToolTip(environment_tip)
        self.start_button.setToolTip("开始本次实验" if ready else "先点击顶部‘初始化实验台’")
        self.status_label.setToolTip(self.status_detail.text())
        self.export_button.setEnabled(not active and self.selected_details is not None and "export" not in self.runner.pending and not comparison_work)
        self._update_comparison_controls(active)
        if hasattr(self, "temperature_workspace"):
            self.temperature_workspace.set_external_busy(locked or comparison_work or liquid_work or self._close_requested)
        if hasattr(self, "analysis_workspace"):
            self.analysis_workspace.set_acquisition_busy(locked or temperature_active or self._archive_busy or self._close_requested)
        if hasattr(self, "liquid_workspace"):
            self.liquid_workspace.set_external_busy(locked or temperature_active or comparison_work or self._close_requested)

    def _temperature_busy_changed(self, busy):
        active = ((self.snapshot.get("job") or {}).get("status") in BUSY
                  or self._start_pending or bool(self.snapshot.get("preparing")))
        self._set_active(active)
        if self._archive_trash_refresh_requested and not self._archive_operation_blocked():
            QtCore.QTimer.singleShot(0, self._flush_deleted_records_refresh)
        if self.temperature_workspace.is_running:
            self.status_label.setText("Arduino 固件准备 / 验证" if self.temperature_workspace.preparation.is_running
                                      else "温压正在采集 / 保存")

    def _temperature_complete(self, key):
        # 让当前封存信号处理完成，再刷新新的分析来源。
        QtCore.QTimer.singleShot(0, self.analysis_workspace.refresh_sources)
        self.refresh_history(select_key=key)
        self.statusBar().showMessage(f"温压记录已保存：{key}；可从当前实验档案查看曲线和标定点。")

    def open_workbench(self):
        self.tabs.setCurrentIndex(self.analysis_tab)
        self.analysis_workspace.refresh_sources()
        if self.selected_details and hasattr(self.analysis_workspace, "select_source"):
            self.analysis_workspace.select_source(
                str(Path(self.service.settings.root) / "Data" / self.selected_details["key"]))

    def _tick(self):
        if self._close_requested:
            self._finish_close()
            return
        if self._archive_busy:
            return
        if self.replay is not None:
            now = time.monotonic()
            if self.replay_running:
                self.replay_elapsed = min(self.replay.duration_s, self.replay_elapsed + (now-self._replay_tick)*self.replay_speed.currentData())
                if self.replay_elapsed >= self.replay.duration_s:
                    self.replay_running = False
                    self.pause_button.setText("继续")
                self._render_replay()
            self._replay_tick = now
        def poll():
            return self.service.snapshot(), self.service.live_snapshot(), (self.service.finger_snapshot() if hasattr(self.service, "finger_snapshot") else {})
        epoch = self._archive_epoch
        self.runner.submit("poll", poll,
                           lambda result:self._poll_done(result) if epoch == self._archive_epoch and not self._archive_busy else None,
                           lambda error:self._error(error) if epoch == self._archive_epoch and not self._archive_busy else None,
                           "polling")

    def _poll_done(self, result):
        if self._archive_busy:
            return
        snapshot, live, *research = result
        self._apply_status(snapshot)
        job = snapshot.get('job') or {}
        if job.get('archive_trashed_output') and not job.get('output'):
            return
        job_experiment = self._job_experiment(snapshot.get("job"))
        liquid_job = bool((snapshot.get('job') or {}).get('config', {}).get('liquid_temperature'))
        if research and job_experiment == "finger" and not liquid_job:
            self.finger_workspace.apply_snapshot(research[0])
            if self.finger_workspace._pending:
                self.mark_group.setEnabled(False)
        if self.finger_workspace.source_key is None and job_experiment == "finger" and not liquid_job:
            self.finger_workspace.update_curve(live)
        if self.viewing_key is None and self.replay is None and job_experiment in (None, self.experiment_id):
            if live.get("revision") != self._live_revision or live.get("revision") is None:
                self._render_live(live)
                if self.experiment_id == "material" and self.sweep_options.temperature_mode.currentData() != "none":
                    self.temperature_sweep_workspace.update_snapshot(live)
                self._live_revision = live.get("revision")
        text = "\n".join(snapshot.get("logs") or []) if job_experiment in (None, self.experiment_id) else ""
        if self.logs.toPlainText() != text:
            self.logs.setPlainText(text)
            self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())

    def _apply_status(self, snapshot):
        previous = (self.snapshot.get("job") or {}).get("status")
        previous_failure = (self.snapshot.get("job") or {}).get("failure_summary")
        self.snapshot = snapshot or {}
        job = self.snapshot.get("job") or {}
        status = job.get("status")
        active = status in BUSY or self._start_pending or bool(self.snapshot.get("preparing"))
        if (previous in BUSY and status in {"initialization_failed", "stop_unconfirmed", "failed"}
                and self._environment_snapshot("usrp").get("state") == "ready"):
            self.service.invalidate_environment("usrp")
        self._set_active(active)
        self.status_label.setText("正在准备" if self.snapshot.get("preparing") or self._start_pending else STATUS.get(status, "待开始"))
        if job.get("failure_summary"):
            self.status_detail.setText(job["failure_summary"])
            if job["failure_summary"] != previous_failure:
                self._message(job["failure_summary"])
        elif job.get("errors"):
            self.status_detail.setText("；".join(job["errors"][-2:]))
        elif job:
            self.status_detail.setText(("收发停止已确认 · " if job.get("rf_stopped") else "") + (job.get("output") or "等待设备准备"))
        else:
            self.status_detail.setText("启动软件不会自动收发。")
        same_experiment = self._job_experiment(job) == self.experiment_id
        if job and not same_experiment and not active:
            self.status_label.setText("待开始")
            self.status_detail.setText("当前实验尚未开始采集。")
        can_mark = same_experiment and active and status == "running" and job.get("config", {}).get("mode") == "phase" and bool(job.get("baseline"))
        liquid = (same_experiment and bool(job.get("config", {}).get("liquid_temperature"))) or (self.experiment_id == "finger" and self.sweep_options.liquid_enabled.isChecked())
        self.finger_workspace.set_acquisition(active, can_mark and not liquid and not self._close_requested)
        self.workflow_shortcut.setVisible(self.mode.currentData() == "phase" and not liquid)
        self.current_result_button.setEnabled(not active and bool(job.get("output"))
                                              and self._job_experiment(job) == self.experiment_id)
        if self.temperature_workspace.is_running:
            self.status_label.setText("Arduino 固件准备 / 验证" if self.temperature_workspace.preparation.is_running
                                      else "温压正在采集 / 保存")
        self.status_label.setToolTip(self.status_detail.text())
        self.mark_group.setVisible(self.mode.currentData() == "phase" and not liquid and self.viewing_key is None and self.replay is None)
        self.mark_group.setEnabled(can_mark and not liquid and self.viewing_key is None and self.replay is None)
        events = self.snapshot.get("events") or []
        if events:
            last = events[-1]
            self.mark_state.setText(f"最近标记：{last.get('label', '')} · {last.get('event', '')}    操作时刻不等于实测位置。")
        else:
            self.mark_state.setText("静止参考已通过，可以标记阶段。" if can_mark else "等待静止参考通过；标记仅适用于连续幅相。")
        if previous in BUSY and status not in BUSY:
            self.refresh_history()
            if job.get("config", {}).get("liquid_temperature"):
                self._clear_liquid()
                if job.get("output"):
                    key = str(job["output"]).removeprefix("Data/")
                    self.liquid_workspace.set_current(key)

    def _toggle_preview(self, shown):
        self.preview_plots.setVisible(shown)
        self._last_preview = 0
        self._live_revision = None
        if shown and self.replay is not None:
            self._render_replay()

    def _render_live(self, live, force_preview=False):
        if not live.get("mode"):
            live = dict(live, mode=self.mode.currentData())
        self.live_plots.update_snapshot(live)
        points = live.get("points") or []
        if live.get("mode") == "phase" and points:
            last = points[-1]
            def formatted(value):
                return f"{value:.3f}" if isinstance(value, (float, int)) else "—"
            self.metrics.setText(f"相对幅度 {formatted(last.get('amplitude'))} dB     相对相位 {formatted(last.get('phase'))}°     阶段 {last.get('label', '—')}     时间 {formatted(last.get('x'))} s")
        elif points:
            self.metrics.setText(f"已测 {len(points)} 点     当前第 {points[-1].get('repeat', '—')} 轮     频点 {points[-1].get('point', '—')}")
        else:
            self.metrics.setText("等待本次扫频结果；每个真实测量点完成后显示。" if live.get("mode") == "sweep" else "等待真实数据；连续幅相需先建立静止参考。")
        now = time.monotonic()
        if force_preview or (self.diagnostic_toggle.isChecked() and now-self._last_preview >= self.service.settings.preview_interval_ms/1000):
            preview = live.get("preview")
            self.preview_plots.update_preview(preview)
            if preview:
                self.preview_summary.setText(f"RX 抽取窗口 · 载频 {float(preview.get('carrier_hz') or 0)/1e9:g} GHz · 采样率 {float(preview.get('rate') or 0):g} S/s · 单音 {float(preview.get('tone_hz') or 0):g} Hz · 记录时刻 {float(preview.get('t_s') or 0):.3f} s · 跳过预览 {preview.get('dropped_total', 0)} 帧")
            else:
                self.preview_summary.setText("尚无可用 RX 预览；完整原始 IQ 独立保存。")
            self._last_preview = now

    def return_live(self):
        self.replay_running = False
        self.replay = None
        self.replay_elapsed = 0
        self.replay_controls.hide()
        self.replay_note.hide()
        self.viewing_key = None
        self._history_generation += 1
        self._live_revision = None
        self.live_plots.clear()
        self.preview_plots.update_preview(None)
        self.view_banner.setText("当前实验 · 实时数据")
        self._render_live({"mode": self.mode.currentData(), "points": []})
        if self.experiment_id == "material" and self.sweep_options.temperature_mode.currentData() != "none":
            self.temperature_sweep_workspace.clear()
            self._show_tab(self.temperature_sweep_tab)
        elif self.experiment_id in ("temperature", "temperature_calibration", "pressure_calibration"):
            self._show_tab(self.temperature_tab)
        else:
            self._show_tab(0)

    def refresh_history(self, select_key=None, *, corrected_key=None):
        if self._close_requested:
            return
        self._history_correction_refresh_key = corrected_key if corrected_key == select_key else None
        if self._history_correction_refresh_key is None:
            # The same record may still be growing or may just have completed.
            self._displayed_history_details = None
            if self.selected_details is not None:
                self.selected_details = {**self.selected_details, 'curve_loaded':False}
        self._history_refresh_requested = True
        self._history_refresh_revision += 1
        # Button clicks pass False; ordinary refreshes must not erase a pending
        # request to select a newly saved record.
        if isinstance(select_key, str):
            self._history_refresh_select_key = select_key
        if hasattr(self, "analysis_workspace"):
            self.analysis_workspace.refresh_sources()
        self._flush_history_refresh()

    def _flush_history_refresh(self):
        if self._close_requested:
            self._history_refresh_requested = False
            self._history_refresh_select_key = None
            return
        if not self._history_refresh_requested or "history" in self.runner.pending:
            return
        self._history_refresh_requested = False
        revision = self._history_refresh_revision
        select_key = self._history_refresh_select_key
        correction_key = self._history_correction_refresh_key

        def loaded(rows):
            try:
                if self._close_requested or revision != self._history_refresh_revision:
                    return
                self._history_refresh_select_key = None
                self._history_all_rows = rows
                self._render_history_rows(select_key if isinstance(select_key, str) else self._selected_history_key)
                if self.history_table.currentRow() >= 0:
                    self._select_history(force=True, reuse_sealed_curve=bool(correction_key) and correction_key == self._selected_history_key)
            finally:
                # TaskRunner removes the completed key before invoking callbacks.
                self._flush_history_refresh()

        def failed(error):
            try:
                if not self._close_requested and revision == self._history_refresh_revision:
                    self._history_refresh_select_key = None
                    self._error(error)
            finally:
                self._flush_history_refresh()

        if not self._submit("history", self.service.history, loaded, failure=failed):
            # A full analysis queue retries on runner.idle, without polling.
            self._history_refresh_requested = True

    @staticmethod
    def _history_category(row):
        return row.get("category") or {"sweep": "material_sweep", "phase": "phase_history", "teacher": "teacher_sweep"}.get(row.get("mode"), "")

    @staticmethod
    def _local_time(value):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        except (ValueError, TypeError):
            return str(value or "—")

    def _filter_history(self, *_):
        self._history_filter_timer.stop()
        self._history_filter_pending = False
        self._render_history_rows(self._selected_history_key)
        if self._selected_history_key and self.selected_details is None and self.history_table.currentRow() >= 0:
            self._select_history(force=True)

    def _schedule_history_filter(self, *_):
        # Rendering is delayed, but obsolete reads/reports become invalid now.
        self._comparison_generation += 1
        self._history_generation += 1
        self._history_summary_request = None
        self._history_filter_pending = True
        self.selected_details = None
        self._history_buttons(False)
        self.metadata.setPlainText('正在筛选记录…')
        self.file_list.clear()
        self._history_filter_timer.start()
        self._update_comparison_controls()

    def _update_history_type_options(self):
        """The archive always belongs to the active experiment, including subtypes."""
        name, _, allowed = EXPERIMENTS[self.experiment_id]
        selected = self.history_type.currentData()
        with QtCore.QSignalBlocker(self.history_type):
            self.history_type.clear()
            if len(allowed) > 1:
                self.history_type.addItem("当前实验全部记录", "")
            for key, title in CATEGORIES.items():
                if key in allowed:
                    self.history_type.addItem(title, key)
            self.history_type.setCurrentIndex(max(0, self.history_type.findData(selected)))
        self.history_type.setVisible(len(allowed) > 1)
        self.history_scope_label.setText(f"当前实验：{name}")

    def _render_history_rows(self, selected=None):
        category, status = self.history_type.currentData(), self.history_status.currentData()
        query = self.history_search.text().strip().casefold()
        allowed = EXPERIMENTS[self.experiment_id][2]
        self.history_rows = [row for row in self._history_all_rows
                             if self._history_category(row) in allowed
                             and (not category or self._history_category(row) == category)
                             and (not status or row.get("status") == status)
                             and (not query or query in " ".join(str(row.get(field, "")) for field in ("label", "sensor", "key", "condition_search")).casefold())]
        visible = {row["key"] for row in self.history_rows}
        removed = self.comparison_keys - visible
        if removed:
            self.comparison_keys.intersection_update(visible)
            self._comparison_generation += 1
            self._message(f"筛选或刷新后已清除 {len(removed)} 条不可见记录的勾选；分析仅使用当前可见的勾选项。")
        self.history_table.blockSignals(True)
        self.history_table.clearSelection()
        self.history_table.setCurrentItem(None)
        self.history_table.setRowCount(len(self.history_rows))
        selected_row = None
        for index, row in enumerate(self.history_rows):
            checked = QtWidgets.QTableWidgetItem()
            checked.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsUserCheckable | QtCore.Qt.ItemFlag.ItemIsSelectable)
            checked.setCheckState(QtCore.Qt.CheckState.Checked if row["key"] in self.comparison_keys else QtCore.Qt.CheckState.Unchecked)
            checked.setData(QtCore.Qt.ItemDataRole.UserRole, row["key"])
            checked.setToolTip("勾选记录可批量删除；分析限 1–8 条同类合格记录。高亮一行查看详情。")
            self.history_table.setItem(index, 0, checked)
            values = [self._local_time(row.get("started_utc")), row.get("sample_name", row.get("label", "—")), CATEGORIES.get(self._history_category(row), "未知"),
                      STATUS.get(row.get("status"), row.get("status", "")), row.get("sensor", ""), row.get("condition_summary", "—")]
            for column, value in enumerate(values, 1):
                item = QtWidgets.QTableWidgetItem(str(value))
                item.setData(QtCore.Qt.ItemDataRole.UserRole, row["key"])
                item.setToolTip(f"{row['key']}\n原始 UTC：{row.get('started_utc', '未知')}\n{row.get('condition_details', '')}")
                self.history_table.setItem(index, column, item)
            if row["key"] == selected:
                selected_row = index
        if selected_row is not None:
            self.history_table.selectRow(selected_row)
        self.history_table.blockSignals(False)
        if selected and selected_row is None:
            self._clear_history_details()
        self._update_comparison_controls()

    def _clear_history_details(self):
        self._history_generation += 1
        self._history_summary_request = None
        self._history_filter_timer.stop()
        self._history_filter_pending = False
        self._selected_history_key = None
        self.selected_details = None
        self._displayed_history_details = None
        self.tabs.setTabVisible(self.serial_archive_tab,False)
        self._history_buttons(False)
        self.metadata.clear()
        self.file_list.clear()
        if self.viewing_key is not None:
            self.viewing_key = None
            self.replay_running = False
            self.replay = None
            self.replay_controls.hide()
            self.replay_note.hide()
            self.live_plots.clear()
            self.preview_plots.update_preview(None)
            self._live_revision = None
            self.view_banner.setText("当前实验 · 实时数据")

    def _comparison_blocked(self):
        return (self._archive_busy or (self.snapshot.get("job") or {}).get("status") in BUSY or self._start_pending
                or bool(self.snapshot.get("preparing")) or self._close_requested or self._environment_busy()
                or (hasattr(self, "temperature_workspace") and self.temperature_workspace.is_running)
                or (hasattr(self, "liquid_workspace") and self.liquid_workspace.is_busy)
                or "export" in self.runner.pending or "comparison_export" in self.runner.pending
                or "figure_export" in self.runner.pending
                or "calibration_correction" in self.runner.pending
                or "finger_analysis" in self.runner.pending)

    def _archive_operation_blocked(self):
        pending = self.runner.pending
        return (self._comparison_blocked() or self._comparison_busy or self._history_filter_pending
                or bool(getattr(getattr(self, 'temperature_workspace', None), 'is_running', False))
                or bool(getattr(getattr(self, 'analysis_workspace', None), 'is_running', False))
                or any(key in pending for key in ('preflight', 'diagnostics', 'replay_load', 'finger_history', 'start',
                                                  'save_file', 'open_file', 'open_run'))
                or any(key.startswith('history_curve:') for key in pending))

    def _comparison_selection_issue(self, keys):
        if not 1 <= len(keys) <= 8:
            return '分析限 1–8 条；删除可批量选择。'
        lookup = {row['key']:row for row in self._history_all_rows}
        rows = [lookup.get(key, {}) for key in keys]
        categories = {self._history_category(row) for row in rows}
        if any(self._has_liquid_conditions(row) for row in rows):
            return '液体分析请打开“温度记录与比较”；所选记录仍可删除。'
        if 'teacher_sweep' in categories:
            return '老师原版记录暂不支持此分析；可查看、导出或删除。'
        if len(categories) != 1 or not categories.issubset(CATEGORIES):
            return '分析须选择同一种记录类型；删除可跨类型。'
        if any(row.get('status') not in ('complete', 'complete_with_gaps', 'stopped') for row in rows):
            return '分析须选择已结束的合格记录；失败或不完整记录仍可删除。'
        return ''

    def _invalidate_archive_async(self):
        self._archive_epoch += 1
        self._history_generation += 1
        self._comparison_generation += 1
        self._history_refresh_revision += 1
        self._history_refresh_requested = False
        self._history_refresh_select_key = None
        self._history_summary_request = None
        self.replay_running = False

    def delete_selected_records(self):
        if self._archive_operation_blocked():
            self._message('请等待采集、初始化、参数检查或分析导出完成，再删除记录。')
            return
        rows = [row for row in self.history_rows if row['key'] in self.comparison_keys]
        if not rows:
            self._message('请先勾选要删除的记录。')
            return
        dialog = ArchiveDeleteDialog(rows, self)
        accepted = dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
        dialog.deleteLater()
        if not accepted:
            return
        if self._archive_operation_blocked():
            self._message('工作状态已变化，请待当前操作结束后重新删除。')
            return
        keys = [row['key'] for row in rows]
        def completed(result):
            deleted = set(result.get('deleted_keys', []))
            self._forget_archive_records(deleted)
            self._message(f'已删除 {len(deleted)} 条记录，可从“已删除记录”恢复。')
            if self.archive_trash_dialog is not None:
                self._archive_trash_refresh_requested = True
            self.refresh_history()
            self.liquid_workspace.refresh_history()
        self._run_archive_task('archive_delete', lambda:self.service.trash_records(keys), completed, mutation=True)

    def open_deleted_records(self):
        if self._archive_operation_blocked():
            self._message('请等待当前操作结束后查看已删除记录。')
            return
        if self.archive_trash_dialog is None:
            dialog = self.archive_trash_dialog = ArchiveTrashDialog(self)
            dialog.refresh_requested.connect(self._refresh_deleted_records)
            dialog.restore_requested.connect(self.restore_deleted_records)
        self.archive_trash_dialog.show()
        self.archive_trash_dialog.raise_()
        self._refresh_deleted_records()

    def _refresh_deleted_records(self):
        if self.archive_trash_dialog is None or self._archive_operation_blocked():
            return
        def loaded(rows):
            self.archive_trash_dialog.set_records([
                {**row, 'category_label':CATEGORIES.get(row.get('category'), '未知'),
                 'deleted_local':self._local_time(row.get('deleted_utc'))} for row in rows])
        self._run_archive_task('archive_trash_list', self.service.trashed_records, loaded)

    def _flush_deleted_records_refresh(self):
        if self._archive_trash_refresh_requested and not self._archive_operation_blocked():
            self._archive_trash_refresh_requested = False
            self._refresh_deleted_records()

    def restore_deleted_records(self, trash_ids):
        if self._archive_operation_blocked():
            self._message('请等待采集、初始化、参数检查或分析导出完成，再恢复记录。')
            return
        if self.archive_trash_dialog is None or not trash_ids:
            return
        available = {self.archive_trash_dialog.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
                     for row in range(self.archive_trash_dialog.table.rowCount())}
        if not set(trash_ids).issubset(available):
            self._message('已删除记录列表已变化，请刷新后重新选择。')
            return
        def completed(result):
            self._clear_history_details()
            restored = set(result.get('restored_keys', []))
            self.archive_trash_dialog.set_records([row for row in self.archive_trash_dialog.records if row['key'] not in restored])
            self._message(f"已恢复 {len(restored)} 条记录。")
            self.refresh_history()
            self.liquid_workspace.refresh_history()
        self._run_archive_task('archive_restore', lambda:self.service.restore_records(list(trash_ids)), completed, mutation=True)

    def _run_archive_task(self, key, operation, success, *, mutation=False):
        if self._archive_operation_blocked():
            return False
        self._archive_busy = True
        if mutation:
            self._invalidate_archive_async()
        self._set_active(False)
        def completed(result):
            if mutation:
                self._invalidate_archive_async()
            self._archive_busy = False
            self._apply_status(self.snapshot)
            success(result)
            self._apply_status(self.snapshot)
            if self._close_requested:
                self._finish_close()
        def failed(error):
            if mutation:
                self._invalidate_archive_async()
            self._archive_busy = False
            self._apply_status(self.snapshot)
            if mutation:
                deleted = set(getattr(error, 'deleted_keys', []) or [])
                if deleted:
                    self._forget_archive_records(deleted)
                if getattr(error, 'restored_keys', None):
                    self._clear_history_details()
                self.refresh_history()
                self.liquid_workspace.refresh_history()
                if self.archive_trash_dialog is not None:
                    self._archive_trash_refresh_requested = True
            self._error(error)
            if self.archive_trash_dialog is not None:
                self.archive_trash_dialog.status.setText(str(error))
            if self._close_requested:
                self._finish_close()
        if not self._submit(key, operation, completed, failure=failed):
            self._archive_busy = False
            self._apply_status(self.snapshot)
            return False
        return True

    def _forget_archive_records(self, keys):
        self.comparison_keys.difference_update(keys)
        self._history_all_rows = [row for row in self._history_all_rows if row['key'] not in keys]
        if self._selected_history_key in keys or self.viewing_key in keys:
            self._clear_history_details()
            self.temperature_sweep_workspace.clear()
            self.serial_archive_workspace._details_key = None
            self.serial_archive_workspace._calibration_points = {}
            self.serial_archive_workspace._calibration_editable = False
            self.serial_archive_workspace.table.setRowCount(0)
            self.serial_archive_workspace.voltage.clear()
            self.serial_archive_workspace.adc.clear()
            self.serial_archive_workspace.summary.setText('该记录已删除，可从“已删除记录”恢复。')
            self.serial_archive_workspace.set_edit_busy(True)
        if self.comparison_dialog is not None and set((self.comparison_dialog.report or {}).get('keys', [])).intersection(keys):
            self.comparison_dialog.close()
        if self.diagnostic_model.get('key') in keys:
            self.diagnostic_model = {}
            self.analysis_plot.display({})
            self.analysis_title.setText('记录已删除，请选择其他记录。')
            self.quality_facts.clear()
        for key in keys:
            self._diagnostic_publication_directories.pop(key, None)
        if self.finger_workspace.source_key in keys or self.finger_workspace.run_key in keys:
            self.finger_workspace.use_current()
        liquid = self.liquid_workspace
        if liquid.current_key in keys:
            liquid.current_key = liquid.current_mode = None
            liquid.annotations = []
            liquid._dirty = False
            liquid.report_path = liquid._publication_report = liquid._figure_directory = None
            liquid.window_table.setRowCount(0)
            liquid.report_text.clear()
            liquid.chart_choice.clear()
            liquid.loaded_label.setText('记录已删除，请重新选择。')
            liquid._update_enabled()
        analysis = self.analysis_workspace
        analysis.result = None
        analysis.chart_choice.clear()
        analysis.plot.display({})
        analysis.details.clear()
        analysis.artifacts.clear()
        analysis.open_result_button.setEnabled(False)
        job = self.snapshot.get('job') or {}
        output = str(job.get('output') or '')
        if output.removeprefix('Data/') in keys:
            job['archive_trashed_output'] = output
            job['output'] = None
            self._live_revision = None
            self.live_plots.clear()
            self.preview_plots.update_preview(None)
            self.current_result_button.setEnabled(False)
        self._render_history_rows(self._selected_history_key)

    def _update_comparison_controls(self, active=None):
        if not hasattr(self, "compare_button"):
            return
        blocked = self._comparison_blocked() or bool(active)
        actions_blocked = blocked or self._history_filter_pending
        count = len(self.comparison_keys)
        issue = self._comparison_selection_issue(self.comparison_keys) if count else ''
        self.comparison_count.setText(f"已勾选 {count} 条" + (' · ' + issue if issue else ' · 分析限 1–8 条同类记录'))
        self.compare_button.setText("组合分析所选记录" if count > 1 else "分析所选记录")
        self.compare_button.setToolTip(issue or "勾选 1 条记录进行分析，或勾选 2–8 条同类记录组合分析。")
        self.compare_button.setEnabled(bool(count) and not issue and not actions_blocked and not self._comparison_busy)
        self.comparison_clear.setEnabled(bool(self.comparison_keys) and not actions_blocked)
        archive_blocked = self._archive_operation_blocked() or bool(active)
        self.delete_records_button.setEnabled(bool(count) and not archive_blocked and hasattr(self.service, 'trash_records'))
        self.deleted_records_button.setEnabled(not archive_blocked and hasattr(self.service, 'trashed_records'))
        if self.archive_trash_dialog is not None:
            self.archive_trash_dialog.set_busy(self._archive_busy, blocked=archive_blocked)
        changed_flags = []
        for row in range(self.history_table.rowCount()):
            item = self.history_table.item(row, 0)
            if item:
                flags = item.flags()
                target_flags = flags & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable if blocked else flags | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                if target_flags != flags:
                    changed_flags.append((item, target_flags))
        if changed_flags:
            updates_enabled = self.history_table.updatesEnabled()
            self.history_table.setUpdatesEnabled(False)
            try:
                with QtCore.QSignalBlocker(self.history_table):
                    for item, flags in changed_flags:
                        item.setFlags(flags)
            finally:
                self.history_table.setUpdatesEnabled(updates_enabled)
        if self.comparison_dialog is not None and not self.comparison_dialog.is_closed:
            self.comparison_dialog.set_busy(self._comparison_busy or "comparison_export" in self.runner.pending
                                            or "figure_export" in self.runner.pending, blocked=actions_blocked)
        if hasattr(self, "serial_archive_workspace"):
            self.serial_archive_workspace.set_edit_busy(actions_blocked or self._comparison_busy)
        self._update_diagnostic_export_controls()

    def _comparison_item_changed(self, item):
        if item.column() != 0:
            return
        key = item.data(QtCore.Qt.ItemDataRole.UserRole)
        checked = item.checkState() == QtCore.Qt.CheckState.Checked
        rows = {row["key"]: row for row in self.history_rows}
        error = ""
        if self._history_filter_pending:
            error = '正在筛选记录，请稍候。'
        elif self._comparison_blocked():
            error = "采集或导出处理中，暂不能变更分析勾选。"
        if error:
            self.history_table.blockSignals(True)
            item.setCheckState(QtCore.Qt.CheckState.Checked if key in self.comparison_keys else QtCore.Qt.CheckState.Unchecked)
            self.history_table.blockSignals(False)
            self._message(error)
            return
        if checked:
            self.comparison_keys.add(key)
        else:
            self.comparison_keys.discard(key)
        self._comparison_generation += 1
        self._update_comparison_controls()

    def clear_comparison_selection(self):
        if self._comparison_blocked():
            return
        self.comparison_keys.clear()
        self._comparison_generation += 1
        self._render_history_rows(self._selected_history_key)

    def compare_selected(self):
        if self._history_filter_pending:
            self._filter_history()
        if self._comparison_blocked() or self._comparison_busy:
            self._message("请等待采集、分析或导出结束，再分析所选记录。")
            return
        keys = [row["key"] for row in self.history_rows if row["key"] in self.comparison_keys]
        issue = self._comparison_selection_issue(keys)
        if issue:
            self._message(issue)
            return
        self._request_comparison(keys)

    def _request_comparison(self, keys, reference_key=None, dialog=None):
        if self._history_filter_pending or self._comparison_blocked() or self._comparison_busy:
            return
        issue = self._comparison_selection_issue(keys)
        if issue:
            self._message(issue)
            return
        self._comparison_generation += 1
        generation = self._comparison_generation
        self._comparison_busy = True
        if dialog is not None:
            dialog.set_busy(True, "正在重新计算参考实验…")
        self._set_active(False)
        self._message("正在后台读取所选记录并分析参数与结果…" if len(keys) == 1 else
                      "正在后台读取所选记录并组合分析参数与结果…")

        def discard_unusable(target):
            # A partially rendered report must never remain exportable. A
            # constructor can fail after creating its parent-owned QDialog.
            if self.comparison_dialog is target:
                self.comparison_dialog = None
            target.report = None
            try:
                target.hide()
                target.close()
            finally:
                target.deleteLater()

        def completed(report):
            if generation != self._comparison_generation or self._close_requested or (dialog is not None and dialog.is_closed):
                self._comparison_busy = False
                if dialog is not None and not dialog.is_closed and dialog.report:
                    dialog.reference.blockSignals(True)
                    dialog.reference.setCurrentIndex(dialog.reference.findData(dialog.report.get("reference_key")))
                    dialog.reference.blockSignals(False)
                    dialog.status.setText("选择已变更，保留上一次完整分析结果。")
                self._apply_status(self.snapshot)
                return
            target = dialog
            previous_dialog = self.comparison_dialog
            previous_report = dialog.report if dialog is not None else None
            previous_directory = dialog.publication_directory if dialog is not None else None
            previous_chart = (dialog.chart_choice.currentData() or {}).get("id") if dialog is not None else None
            previous_tab = dialog.tabs.currentIndex() if dialog is not None else 0
            previous_data_tab = dialog.data_tabs.currentIndex() if dialog is not None else 0
            existing_children = set(self.findChildren(QtWidgets.QDialog, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly))
            try:
                if target is None:
                    target = ComparisonDialog(self)
                    target.reference_requested.connect(lambda key: self._request_comparison(list(target.report["keys"]), key, target))
                    target.export_requested.connect(lambda: self._export_comparison(target))
                    target.figure_export_requested.connect(lambda chart_id: self._export_publication_figure(target, chart_id))
                    target.figure_save_as_requested.connect(lambda chart_id: self._export_analysis_figure(target, chart_id))
                    target.open_figure_directory_requested.connect(lambda: self._open_publication_directory(target))
                target.apply_report(report)
                target.show()
                target.raise_()
                if dialog is None:
                    self.comparison_dialog = target
                    target.closed.connect(lambda: self._comparison_closed(target))
                    if previous_dialog is not None and not previous_dialog.is_closed:
                        previous_dialog.close()
            except Exception as error:
                recovery = ""
                if dialog is not None:
                    try:
                        if dialog.is_closed or previous_report is None:
                            raise RuntimeError("旧结果窗口不可恢复")
                        dialog.apply_report(previous_report)
                        dialog.set_publication_directory(previous_directory)
                        for index in range(dialog.chart_choice.count()):
                            if (dialog.chart_choice.itemData(index) or {}).get("id") == previous_chart:
                                dialog.chart_choice.setCurrentIndex(index)
                                break
                        dialog.tabs.setCurrentIndex(previous_tab)
                        dialog.data_tabs.setCurrentIndex(previous_data_tab)
                        recovery = "；已恢复上一次完整结果"
                    except Exception:
                        try:
                            discard_unusable(dialog)
                        except Exception:
                            # Cleanup must not hide the original display error;
                            # the report is detached and deleteLater was requested.
                            pass
                        recovery = "；旧结果无法恢复，已关闭结果窗口，可重新分析"
                else:
                    candidates = set(self.findChildren(QtWidgets.QDialog, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly)) - existing_children
                    if target is not None:
                        candidates.add(target)
                    for candidate in candidates:
                        try:
                            discard_unusable(candidate)
                        except Exception:
                            pass
                failed(RuntimeError(f"分析结果显示失败：{error}{recovery}"))
                return
            self._comparison_busy = False
            self._message("分析已完成；原始实验记录未修改。" if len(keys) == 1 else
                          "组合分析已完成；原始实验记录未修改。")
            self._apply_status(self.snapshot)
        def failed(error):
            self._comparison_busy = False
            if dialog is not None and not dialog.is_closed and dialog.report:
                # Even an obsolete failed request must not leave its uncomputed
                # reference selected above a previous report.
                dialog.reference.blockSignals(True)
                dialog.reference.setCurrentIndex(dialog.reference.findData(dialog.report.get("reference_key")))
                dialog.reference.blockSignals(False)
            if generation == self._comparison_generation:
                if dialog is not None and not dialog.is_closed:
                    dialog.show_error(error)
                self._error(error)
            self._apply_status(self.snapshot)
        if not self._submit("comparison", lambda: self.service.compare_runs(keys, reference_key=reference_key), completed, failure=failed):
            self._comparison_busy = False
            self._apply_status(self.snapshot)

    def _comparison_closed(self, dialog):
        if self.comparison_dialog is dialog:
            self.comparison_dialog = None
            self._comparison_generation += 1

    def _export_comparison(self, dialog):
        if self._comparison_blocked() or self._comparison_busy or dialog.is_closed or not dialog.report:
            return
        report = dialog.report
        serial = report.get("kind") in ("temperature_calibration", "temperature_pressure", "pressure_calibration")
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(
            dialog, "保存论文图与数据" if serial else "保存实验比较压缩包",
            str(Path.home() / "Downloads" / ("pressure_calibration.zip" if report.get("kind") == "pressure_calibration" else "temperature_publication.zip" if serial else "sspp_comparison.zip")),
            "ZIP 压缩包 (*.zip)")
        if not destination:
            return
        if not destination.lower().endswith(".zip"):
            destination += ".zip"
        dialog.set_busy(True, "正在生成比较图表并打包来源、参数与结果…")
        def done(result):
            message = f"比较包已保存：{result['path']}\n共 {result.get('count', '—')} 个文件。"
            if result.get("warnings"):
                message += "\n" + "\n".join(str(item) for item in result["warnings"])
            if not dialog.is_closed:
                dialog.status.setText(message)
            self._message(message)
            self._apply_status(self.snapshot)
        def failed(error):
            if not dialog.is_closed:
                dialog.show_error(error)
            self._error(error)
            self._apply_status(self.snapshot)
        if self._submit("comparison_export", lambda: self.service.export_comparison(report, destination), done, failure=failed):
            self._apply_status(self.snapshot)
        else:
            self._update_comparison_controls()

    def _export_publication_figure(self, dialog, chart_id):
        if self._comparison_blocked() or self._comparison_busy or dialog.is_closed or not dialog.report:
            return
        report = dialog.report
        if not isinstance(chart_id, str) or not any(chart.get("id") == chart_id for chart in report.get("charts", [])):
            self._message("当前分析图已变化，请重新选择后导出。")
            return
        dialog.set_busy(True, "正在后台生成论文图、数据与来源说明…")

        def done(result):
            message = f"论文图已保存：{result['directory']}"
            if result.get("warnings"):
                message += "\n" + "\n".join(str(item) for item in result["warnings"])
            if not dialog.is_closed and dialog.report is report:
                dialog.set_publication_directory(result['directory'])
                dialog.status.setText(message)
            self._message(message)
            self._apply_status(self.snapshot)

        def failed(error):
            if not dialog.is_closed:
                dialog.show_error(error)
            self._error(error)
            self._apply_status(self.snapshot)

        if self._submit("figure_export", lambda: self.service.export_publication_figure(report, chart_id),
                        done, failure=failed):
            self._apply_status(self.snapshot)
        else:
            self._update_comparison_controls()

    def _open_publication_directory(self, dialog):
        if self._comparison_blocked() or self._comparison_busy or dialog.is_closed:
            return
        directory = getattr(dialog, 'publication_directory', None)
        if not directory:
            return
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(directory))):
            self._message("无法打开论文图文件夹：" + str(directory))

    def _export_analysis_figure(self, dialog, chart_id):
        if self._comparison_blocked() or self._comparison_busy or dialog.is_closed or not dialog.report:
            return
        report = dialog.report
        if not isinstance(chart_id, str) or not any(chart.get("id") == chart_id for chart in report.get("charts", [])):
            self._message("当前分析图已变化，请重新选择后导出。")
            return
        formats = {"PDF 矢量图 (*.pdf)": ".pdf", "SVG 矢量图 (*.svg)": ".svg",
                   "PNG 位图（600 dpi） (*.png)": ".png"}
        stem = "".join(character if character.isalnum() or character in "-_" else "_"
                       for character in chart_id)[:80] or "figure"
        destination, selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            dialog, "导出当前分析图", str(Path.home() / "Downloads" / ("analysis_" + stem + ".pdf")),
            ";;".join(formats), "PDF 矢量图 (*.pdf)")
        if not destination:
            return
        # An explicit supported suffix determines the format; the selected filter
        # supplies a suffix only when the user entered a filename without one.
        suffix = Path(destination).suffix.lower()
        if not suffix:
            destination += formats.get(selected_filter, ".pdf")
        elif suffix not in formats.values():
            self._error("请选择 PDF、SVG 或 PNG 文件扩展名。")
            return
        # The modal picker may process a late callback or a window close.
        if dialog.is_closed or dialog.report is not report or self._comparison_blocked() or self._comparison_busy:
            return
        dialog.set_busy(True, "正在后台导出当前分析图…")

        def done(result):
            message = f"分析图已保存：{result['path']}"
            if result.get("warnings"):
                message += "\n" + "\n".join(str(item) for item in result["warnings"])
            if not dialog.is_closed:
                dialog.status.setText(message)
            self._message(message)
            self._apply_status(self.snapshot)

        def failed(error):
            if not dialog.is_closed:
                dialog.show_error(error)
            self._error(error)
            self._apply_status(self.snapshot)

        if self._submit("figure_export", lambda: self.service.export_analysis_figure(report, chart_id, destination),
                        done, failure=failed):
            self._apply_status(self.snapshot)
        else:
            self._update_comparison_controls()

    def _update_diagnostic_export_controls(self):
        if not hasattr(self, 'diagnostic_figure_export_button'):
            return
        chart = self.plot_choice.currentData() or {}
        key = self.diagnostic_model.get('key')
        blocked = self._comparison_blocked() or self._comparison_busy
        self.diagnostic_figure_export_button.setEnabled(
            not blocked and isinstance(key, str) and bool(key)
            and isinstance(chart.get('id'), str) and bool(chart['id']))
        self.diagnostic_figure_directory_button.setEnabled(
            not blocked and bool(self._diagnostic_publication_directories.get(key)))

    def _export_diagnostic_figure(self):
        if self._comparison_blocked() or self._comparison_busy:
            return
        # The visible diagnostic model owns the source, even when another archive
        # row has since been highlighted. Capture both IDs before queuing work.
        key = self.diagnostic_model.get('key')
        chart_id = (self.plot_choice.currentData() or {}).get('id')
        if not isinstance(key, str) or not key or not isinstance(chart_id, str) or not chart_id:
            self._message('请先加载并选择一张已保存记录的诊断图。')
            return

        def export():
            report = self.service.publication_analysis(key)
            return self.service.export_publication_figure(report, chart_id)

        def done(result):
            self._diagnostic_publication_directories[key] = result['directory']
            message = f"论文图已保存：{result['directory']}"
            if result.get('warnings'):
                message += '\n' + '\n'.join(str(item) for item in result['warnings'])
            self._message(message)
            self._apply_status(self.snapshot)

        def failed(error):
            self._error(error)
            self._apply_status(self.snapshot)

        self._message('正在后台生成当前诊断图的论文图、数据与来源说明…')
        if self._submit('figure_export', export, done, failure=failed):
            self._apply_status(self.snapshot)
        else:
            self._update_comparison_controls()

    def _open_diagnostic_figure_directory(self):
        if self._comparison_blocked() or self._comparison_busy:
            return
        directory = self._diagnostic_publication_directories.get(self.diagnostic_model.get('key'))
        if directory and not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(directory))):
            self._message('无法打开论文图文件夹：' + str(directory))

    def _delete_calibration_points(self,payload,restore=False):
        if self._comparison_blocked() or self._comparison_busy:
            self._message("请等待采集、分析或导出结束再修正标定点。")
            return
        key=payload.get('key')
        if not self.selected_details or key!=self.selected_details['key']:
            self._message("档案选择已变化，请重新选择标定点。")
            return
        self._comparison_generation+=1
        if self.comparison_dialog is not None and not self.comparison_dialog.is_closed:
            self.comparison_dialog.close()
        def completed(result):
            self._message(f"已{'恢复' if restore else '删除'} {len(result['point_ids'])} 个标定点；原始采样保留，可在档案中恢复。重新组合分析后使用最新结果。")
            self.refresh_history(select_key=key, corrected_key=key)
            self._apply_status(self.snapshot)
        def failed(error):
            self._error(error)
            self._apply_status(self.snapshot)
        if self._submit('calibration_correction',lambda:self.service.correct_calibration_points(
                key,payload.get('point_ids'),payload.get('reason'),restore=restore),completed,failure=failed):
            self._apply_status(self.snapshot)
        else:
            self._update_comparison_controls()

    def _open_current_result(self):
        output = (self.snapshot.get("job") or {}).get("output")
        if not output:
            return
        path = Path(output)
        if path.is_absolute():
            try:
                key = str(path.relative_to(Path(self.service.settings.root) / "Data"))
            except ValueError:
                return
        else:
            key = str(path).removeprefix("Data/")
        self.tabs.setCurrentIndex(1)
        if not any(row["key"] == key for row in self.history_rows):
            for widget in (self.history_type, self.history_status, self.history_search):
                widget.blockSignals(True)
            self.history_type.setCurrentIndex(0)
            self.history_status.setCurrentIndex(0)
            self.history_search.clear()
            for widget in (self.history_type, self.history_status, self.history_search):
                widget.blockSignals(False)
            self._render_history_rows(self._selected_history_key)
        for index, row in enumerate(self.history_rows):
            if row["key"] == key:
                self.history_table.selectRow(index)
                return
        self.refresh_history(select_key=key)

    def _view_saved_curve(self):
        self._ensure_selected_curve(self._display_saved_curve)

    def _ensure_selected_curve(self, callback):
        """Read samples only for an explicit action, bound to the selected row."""
        if self._archive_busy or not self.selected_details:
            return
        if self.selected_details.get('curve_loaded') and self._history_curve_is_sealed(self.selected_details):
            callback(self.selected_details)
            return
        key = self.selected_details['key']
        generation = self._history_generation
        task_key = f'history_curve:{generation}'
        if task_key in self.runner.pending:
            return
        self.view_history_button.setEnabled(False)
        self._message('正在读取所选记录的已保存曲线…')

        def read():
            details = self.service.details(key, include_curve=True)
            if details.get('context', {}).get('experiment') == 'temperature_sweep' and hasattr(self.service, 'temperature_history'):
                details['_temperature_history'] = self.service.temperature_history(key)
            return details

        def loaded(details):
            if generation != self._history_generation or key != self._selected_history_key:
                return
            self.selected_details = details
            self.view_history_button.setEnabled(True)
            self._message('')
            callback(details)

        def failed(error):
            if generation == self._history_generation:
                self.view_history_button.setEnabled(True)
                self._error(error)

        if not self._submit(task_key, read, loaded, failure=failed):
            self.view_history_button.setEnabled(True)

    def _display_saved_curve(self, details):
        key = details['key']
        self._displayed_history_details = details
        self.replay_running = False
        self.replay = None
        self.replay_controls.hide()
        self.replay_note.hide()
        self.viewing_key = key
        self.view_banner.setText(f'历史静态记录 · {key} · 非实时采集')
        self.preview_plots.update_preview(None)
        if key.startswith('temperature_pressure_runs/'):
            self.serial_archive_workspace.display(details)
            category = details.get('context', {}).get('experiment')
            self.tabs.setTabText(self.serial_archive_tab, '压力标定档案详情' if category == 'pressure_calibration' else
                                '温度标定档案详情' if category == 'temperature_calibration' else '温压档案详情')
            self._show_tab(self.serial_archive_tab)
        else:
            self.live_plots.update_snapshot(details.get('curve') or {})
            if '_temperature_history' in details:
                self.temperature_sweep_workspace.update_snapshot({**(details.get('curve') or {}), **details['_temperature_history']})
                self._show_tab(self.temperature_sweep_tab)
            else:
                self._show_tab(0)
        self.metrics.setText(f'历史已保存曲线 · {key}')
        self.preview_summary.setText('历史静态记录；点击回放可按时间重现 RX 预览。')
        self._apply_status(self.snapshot)

    def _history_buttons(self, enabled):
        for widget in (self.view_history_button, self.analyze_button, self.export_button, self.open_run_button, self.replay_button):
            widget.setEnabled(enabled)
        self.pause_button.setEnabled(self.replay is not None)
        self.replay_slider.setEnabled(self.replay is not None)
        if hasattr(self, "finger_history_button"):
            self.finger_history_button.setEnabled(enabled and bool(self.selected_details) and self.selected_details["key"].startswith("phase_runs/")
                and not self._has_liquid_conditions(self.selected_details))

    @staticmethod
    def _history_curve_is_sealed(details):
        metadata = details.get('metadata') or {}
        if details.get('key', '').startswith('temperature_pressure_runs/'):
            return metadata.get('status') == 'complete' and all(
                metadata.get(field) is True for field in ('serial_closed', 'workers_stopped', 'data_files_closed'))
        return metadata.get('status') in ('complete', 'complete_with_gaps', 'stopped') and bool(metadata.get('ended_utc'))

    @staticmethod
    def _has_liquid_conditions(record):
        context = (record or {}).get("context") or {}
        category = (record or {}).get("category") or context.get("experiment", "")
        return bool((record or {}).get("has_liquid_conditions")
                    or str(category).startswith("liquid_temperature_")
                    or isinstance((context.get("parameters") or {}).get("liquid_temperature"), dict))

    def _select_history(self, force=False, *, reuse_sealed_curve=False):
        if self._archive_busy or self._history_filter_pending:
            return
        row = self.history_table.currentRow()
        item = self.history_table.item(row, 0) if row >= 0 else None
        if item is None:
            return
        key = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not force and key == self._selected_history_key and self.selected_details is not None:
            return
        self._selected_history_key = key
        self._history_generation += 1
        generation = self._history_generation
        self.selected_details = None
        self._history_buttons(False)
        summary = next((record for record in self.history_rows if record['key'] == key), {})
        conditions = summary.get('condition_details')
        self.metadata.setPlainText('实验条件\n' + conditions if conditions else '正在读取实验条件…')
        self.file_list.clear()
        self._history_summary_request = (key, generation, reuse_sealed_curve)
        self._flush_history_summary()

    def _flush_history_summary(self):
        if self._close_requested or not self._history_summary_request or 'history_summary' in self.runner.pending:
            return
        key, generation, reuse_sealed_curve = self._history_summary_request

        def loaded(details):
            if generation == self._history_generation and key == self._selected_history_key:
                self.selected_details = details
                conditions = details.get('condition_details') or archive_conditions(details)['condition_details']
                self.metadata.setPlainText('实验条件\n' + conditions + '\n\n采集元数据\n'
                                           + json.dumps(details.get('metadata', {}), ensure_ascii=False, indent=2))
                self.file_list.clear()
                self.file_list.addItems(details.get('files', []))
                serial = key.startswith('temperature_pressure_runs/')
                shown = self._displayed_history_details
                if (reuse_sealed_curve and serial and key == self.viewing_key and shown and shown.get('key') == key
                        and self._history_curve_is_sealed(shown) and self._history_curve_is_sealed(details)):
                    # Corrections change annotations, never immutable raw samples.
                    updated = {**details, 'curve':shown.get('curve', {}), 'curve_loaded':True}
                    self.selected_details = self._displayed_history_details = updated
                    self.serial_archive_workspace.display(updated)
                self._history_buttons(True)
                self.replay_button.setEnabled(not serial and not key.startswith('teacher_runs/'))
                self.analyze_button.setText('查看标定点／电压' if serial else '加载完整诊断')
                self._apply_status(self.snapshot)
            self._flush_history_summary()

        def failed(error):
            if generation == self._history_generation:
                self._error(error)
            self._flush_history_summary()

        if self.runner.submit('history_summary', lambda: self.service.details(key, include_curve=False), loaded, failed):
            self._history_summary_request = None
            self._refresh_navigation_enabled()

    def load_diagnostics(self):
        if self._archive_busy:
            return
        key = self.selected_details.get("key") if self.selected_details else None
        if not key:
            self._message("请先在历史记录中选择实验。")
            return
        if key.startswith('temperature_pressure_runs/'):
            self._view_saved_curve()
            return
        self.analysis_title.setText(f"正在读取 {key} 的真实记录…")
        self._show_tab(2)
        generation = self._history_generation
        def loaded(model):
            if generation != self._history_generation:
                return
            self.diagnostic_model = model
            self.analysis_title.setText(f"{key} · {model.get('chart_count', 0)} 张诊断图")
            self.quality_facts.setText("    |    ".join(f"{fact['name']}：{fact['value']}" for fact in model.get("facts", [])))
            self._select_plot_group()
            if not model.get("chart_count"):
                self.analysis_plot.note.setText(model.get("sample", {}).get("reason", "无可用图形"))
        self._submit("diagnostics", lambda: self.service.diagnostic_view(key), loaded)

    def _select_plot_group(self):
        models = self.diagnostic_model.get("groups", {}).get(self.plot_group.currentData(), [])
        self.plot_choice.blockSignals(True)
        self.plot_choice.clear()
        for model in models:
            self.plot_choice.addItem(model["title"], model)
        self.plot_choice.blockSignals(False)
        self._select_plot()

    def _select_plot(self):
        self.analysis_plot.display(self.plot_choice.currentData() or {})
        self._update_diagnostic_export_controls()

    def export_diagnostics(self):
        if self._archive_busy:
            return
        key = self.selected_details.get("key") if self.selected_details else None
        if not key:
            return
        self.export_button.setEnabled(False)
        self._message("正在后台生成 PNG 与 PDF 图册…")
        def done(result):
            self._message(f"图册已生成：{result.get('count', 0)} 张 PNG。可在实验文件中打开或另存。")
            self._select_history(force=True)
        def failed(error):
            self._error(error)
            self._apply_status(self.snapshot)
        self._submit("export", lambda: self.service.export_diagnostics(key), done, failure=failed)

    def export_bundle(self):
        if self._archive_busy or not self.selected_details:
            return
        job = self.snapshot.get("job") or {}
        if job.get("status") in BUSY or self._start_pending or self.snapshot.get("preparing"):
            self._message("请先停止收发并完成保存，再打包导出。")
            return
        key = self.selected_details["key"]
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "保存实验压缩包", str(Path.home() / "Downloads" / (key.replace("/", "_") + ".zip")),
            "ZIP 压缩包 (*.zip)")
        if not destination:
            return
        if not destination.lower().endswith(".zip"):
            destination += ".zip"
        include_raw = self.include_raw.isChecked()
        self.export_button.setEnabled(False)
        self._message("正在生成图表并打包参数、结果与分析文件…")
        def done(result):
            warnings = "\n".join(str(item) for item in result.get("warnings", []))
            self._message(f"实验包已保存：{result['path']}\n共 {result.get('count', '—')} 个文件。" + (f"\n{warnings}" if warnings else ""))
            self._apply_status(self.snapshot)
        def failed(error):
            self._error(error)
            self._apply_status(self.snapshot)
        if not self._submit("export", lambda: self.service.export_bundle(key, destination, include_raw=include_raw), done, failure=failed):
            self._apply_status(self.snapshot)
        else:
            self._update_comparison_controls()

    def open_run(self):
        if not self._archive_busy and self.selected_details:
            key = self.selected_details["key"]
            self._submit("open_run", lambda: self.service.run_path(key), lambda path: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path))))

    def open_file(self, name):
        if not self._archive_busy and self.selected_details:
            key = self.selected_details["key"]
            self._submit("open_file", lambda: self.service.download(key, name), lambda path: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path))))

    def save_file(self):
        if self._archive_busy:
            return
        item = self.file_list.currentItem()
        if not item or not self.selected_details:
            self._message("请先选择要另存的文件。")
            return
        key, name = self.selected_details["key"], item.text()
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(self, "另存实验文件", str(Path.home() / "Downloads" / name))
        if not destination:
            return
        def copy():
            source = Path(self.service.download(key, name))
            if source.resolve() != Path(destination).resolve():
                shutil.copy2(source, destination)
            return destination
        self._submit("save_file", copy, lambda path: self._message(f"文件已保存：{path}"))

    def play_replay(self):
        if self._archive_busy or not self.selected_details:
            return
        key = self.selected_details["key"]
        mode = self.selected_details.get("curve", {}).get("mode")
        if mode not in ("phase", "sweep"):
            self._message("此记录没有可回放的曲线数据。")
            return
        self._message("正在只读载入历史回放…")
        generation = self._history_generation
        def load():
            from .replay import ReplaySession
            return ReplaySession(self.service.run_path(key), mode)
        def ready(replay):
            if generation != self._history_generation:
                return
            self.replay = replay
            self.replay_note.setText(replay.timing_note)
            self.replay_note.show()
            self.replay_controls.show()
            self.viewing_key = key
            self.replay_elapsed = 0
            self.replay_running = True
            self._replay_tick = time.monotonic()
            self.view_banner.setText(f"历史回放 · {key} · 不启动硬件")
            self.pause_button.setText("暂停")
            self.pause_button.setEnabled(True)
            self.replay_slider.setEnabled(True)
            self.live_plots.clear()
            self._show_tab(0)
            self._message("")
            self._render_replay()
        self._submit("replay_load", load, ready)

    def _render_replay(self):
        if self.replay is None:
            return
        self._render_live(self.replay.snapshot(self.replay_elapsed), force_preview=not self.replay_running)
        self.replay_slider.blockSignals(True)
        self.replay_slider.setValue(round(1000*self.replay_elapsed/max(self.replay.duration_s, .001)))
        self.replay_slider.blockSignals(False)
        self.replay_position.setText(f"{self.replay_elapsed:.1f} / {self.replay.duration_s:.1f} s")
        self.view_banner.setText(f"历史回放 · {'播放中' if self.replay_running else '已暂停/结束'} · {self.replay_elapsed:.1f} / {self.replay.duration_s:.1f} s · {self.replay_speed.currentText()} · 不启动硬件")

    def pause_replay(self):
        if self.replay is None:
            return
        self.replay_running = not self.replay_running
        self._replay_tick = time.monotonic()
        self.pause_button.setText("暂停" if self.replay_running else "继续")
        self._render_replay()

    def seek_replay(self, position):
        if self.replay is not None:
            self.replay_elapsed = self.replay.duration_s*position/1000
            self._replay_tick = time.monotonic()
            self._render_replay()

    def closeEvent(self, event):
        if self._shutdown_done and self._auxiliary_shutdown_done and not self.runner.pending:
            self.timer.stop()
            self._close_timer.stop()
            self.temperature_workspace.timer.stop()
            if hasattr(self.service, 'bind_archive_activity'):
                self.service.bind_archive_activity()
            self.help_dialog.close()
            if self.archive_trash_dialog is not None:
                self.archive_trash_dialog.close()
            if self.comparison_dialog is not None and not self.comparison_dialog.is_closed:
                self.comparison_dialog.close()
            close_plot_widgets(self)
            event.accept()
            return
        event.ignore()
        if self._close_requested:
            return
        job = self.snapshot.get("job") or {}
        active = (job.get("status") in BUSY or self._start_pending
                  or self.snapshot.get("preparing") or self.temperature_workspace.is_running)
        if active:
            answer = QtWidgets.QMessageBox.question(self, "停止实验并退出", "实验仍在采集、初始化或保存。停止收发并等待保存完成后退出？",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No)
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self._close_requested = True
        if self.environment_dialog is not None:
            self.environment_dialog.reject()
        if self._environment_cancel_event is not None:
            self._environment_cancel_event.set()
        self.replay_running = False
        self.runner.closing = True
        self._set_active(False)
        self._message("正在停止后台工作并保存，完成后自动关闭。")
        self._finish_close()

    def _finish_close(self):
        if not self._close_requested:
            return
        if self._archive_busy:
            self._close_timer.start(100)
            return
        if not self._auxiliary_shutdown_done:
            serial_done = self.temperature_workspace.shutdown(timeout_s=0.05)
            analysis_done = self.analysis_workspace.shutdown()
            liquid_done = self.liquid_workspace.request_close()
            self._auxiliary_shutdown_done = serial_done and analysis_done and liquid_done
            if not self._auxiliary_shutdown_done:
                self._close_timer.start(100)
        if not self._shutdown_submitted:
            def done(value):
                self._shutdown_done = True
                self._close_timer.start(0)
            def failed(error):
                self._close_requested = False
                self._shutdown_submitted = False
                self.runner.closing = False
                self._error(f"关闭时发生错误，请检查设备状态：{error}")
            self._shutdown_submitted = self.runner.submit("shutdown", self.service.shutdown, done, failed, "urgent")
        if self._shutdown_done and self._auxiliary_shutdown_done and not self.runner.pending:
            self.close()
