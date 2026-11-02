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
from .serial_archive_widget import SerialArchiveWidget

BUSY = {"starting", "running", "stopping", "saving"}
STATUS = {"complete_with_gaps":"已结束（含无效频点）", "starting": "正在初始化", "running": "正在采集", "stopping": "正在停止", "saving": "正在保存",
          "complete": "已完成", "stopped": "已停止并保存", "failed": "实验失败", "initialization_failed": "初始化失败",
          "stop_unconfirmed": "停止状态待确认", "initializing": "初始化中", "failed_or_incomplete": "失败或未完成", "unknown": "未知"}
GROUPS = {"diagnostic": "接收诊断", "sweep": "扫频分析", "phase": "幅相分析", "quality": "采集质量"}
CATEGORIES = {"temperature_calibration":"温度标定", "temperature_pressure":"温压采集", "temperature_sweep": "SSPP 温度同步扫频", "material_sweep": "材料逐点扫频", "finger_displacement": "手指位移",
              "phase_history": "历史连续幅相", "teacher_sweep": "老师原版扫频",
              "liquid_temperature_sweep": "液体温度扫频", "liquid_temperature_phase": "液体温度与位移相位"}

EXPERIMENTS = {
    "material": ("材料扫频", "逐点扫频 · 材料响应与轮次比较", {"material_sweep", "teacher_sweep"}),
    "finger": ("手指位移", "连续幅相 · 尺量标定与位移验证", {"finger_displacement", "phase_history"}),
    "liquid": ("液体温度影响", "温度条件 · 扫频响应与位移相位", {"liquid_temperature_sweep", "liquid_temperature_phase"}),
    "temperature": ("温压采集", "串口采集 · 电压 / ADC 与温压标定", {"temperature_pressure"}),
    "temperature_calibration": ("温度标定", "多温度平台 · 区间电压统计与参考温度记录", {"temperature_calibration"}),
    "temperature_sweep": ("SSPP 温度扫频", "手填温度平台／连续变温循环扫频", {"temperature_sweep"}),
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
        self.selected_details = None
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
        self.liquid_workspace = LiquidTemperatureWidget(self.service)
        self.liquid_tab = self.tabs.addTab(self.liquid_workspace, "液体温度影响")
        self.liquid_workspace.prepare_requested.connect(self._prepare_liquid)
        self.liquid_workspace.mode.currentIndexChanged.connect(self._liquid_conditions_edited)
        for field in self.liquid_workspace.fields.values():
            field.textChanged.connect(self._liquid_conditions_edited)
        self.liquid_workspace.error.connect(self._error)
        self.liquid_workspace.busy_changed.connect(self._temperature_busy_changed)
        self.temperature_sweep_workspace = TemperatureSweepWidget(Path(self.service.settings.root))
        self.temperature_sweep_tab = self.tabs.addTab(self.temperature_sweep_workspace, "SSPP 与温度电压")
        self.temperature_sweep_workspace.set_port(self.temperature_workspace.port.currentData() or "")
        self.temperature_sweep_workspace.port_changed.connect(self._joint_port_changed)
        self.temperature_sweep_workspace.configuration_changed.connect(self._joint_conditions_changed)
        self.temperature_sweep_workspace.prepare_temperature_requested.connect(self._prepare_joint_temperature)
        self.temperature_sweep_settings = self.temperature_sweep_workspace.take_settings_widget()
        self.sidebar_config_layout.insertWidget(0, self.temperature_sweep_settings)
        self.tabs.setObjectName("workspaceTabs")
        self.tabs.setUsesScrollButtons(True)
        self.finger_settings = self.finger_workspace.take_settings_widget()
        self.sidebar_config_layout.insertWidget(1, self.finger_settings)
        self.liquid_settings = self.liquid_workspace.take_settings_widget()
        self.sidebar_config_layout.insertWidget(0, self.liquid_settings)
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
        kind = kind or ("serial" if self.experiment_id in ("temperature", "temperature_calibration") else "usrp")
        if kind == "serial" and self.experiment_id == "temperature_sweep":
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
                    and (self.experiment_id != "temperature_sweep" or (state.get("target") or {}).get("protocol") == "temperature_v1"))
        return (not self._environment_busy() and state.get("state") == "ready"
                and state.get("target") == target)

    def _invalidate_environment(self, kind=None):
        self.service.invalidate_environment(kind)
        if kind in (None, "usrp"):
            self.confirm.setChecked(False)
        if kind in (None, "serial"):
            self.temperature_workspace.confirm.setChecked(False)
        self._temperature_busy_changed(False)

    def _joint_conditions_changed(self):
        if self.experiment_id == "temperature_sweep" and not self._navigating:
            self.confirm.setChecked(False)
            self._mode_changed()
            self._set_active(False)

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
        self._message("选择温度 v1，验证已有固件；需要更换固件时使用“烧录并验证”。完成后返回 SSPP 温度扫频。")

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
        if targets.get("usrp") is not None:
            self.confirm.setChecked(False)
        if targets.get("serial") is not None:
            self.temperature_workspace.invalidate_preparation()
            self.temperature_workspace.confirm.setChecked(False)
        self._set_active(False)
        self._message("正在初始化所选设备，详细过程实时显示在启动器终端；可点击顶部取消初始化。")
        def done(result):
            self._environment_pending = False
            self._temperature_busy_changed(False)
            if not self._close_requested:
                message = result.get("message") or "初始化检查完成；请确认本次参数和接线后开始实验。"
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
        return (self._environment_busy() or self._parameter_reset_blocked() or self.temperature_workspace.is_running
                or self.liquid_workspace.is_busy or self._comparison_busy
                or "comparison_export" in self.runner.pending or "figure_export" in self.runner.pending
                or "calibration_correction" in self.runner.pending)

    def _experiment_changed(self, index):
        if self._navigation_ready and not self._navigating:
            self.select_experiment(self.experiment_nav.tabData(index))

    def select_experiment(self, key):
        """Switch only idle experiment context; never prepare or start hardware here."""
        if key not in EXPERIMENTS:
            return False
        if key == self.experiment_id:
            return True
        if self._navigation_blocked():
            with QtCore.QSignalBlocker(self.experiment_nav):
                self.experiment_nav.setCurrentIndex(list(EXPERIMENTS).index(self.experiment_id))
            self._message("当前采集、参数检查或结果处理尚未完成；停止并保存后可切换实验。")
            return False
        self._experiment_drafts[self.experiment_id] = self._capture_draft()
        draft = deepcopy(self._experiment_drafts.get(key, self._default_draft))
        if key not in self._experiment_drafts:
            draft["values"]["mode"] = "phase" if key == "finger" else "sweep"
            draft["values"]["label"] = "finger_session" if key == "finger" else ""
            draft["material_name"] = ""
            draft["liquid_setup"] = None
            if key == "temperature_sweep":
                draft["values"].update(freq_start_ghz=5.0, freq_stop_ghz=5.3, points=20)
        self._navigating = True
        try:
            self.experiment_id = key
            self.temperature_workspace.set_calibration_mode(key == "temperature_calibration")
            with QtCore.QSignalBlocker(self.experiment_nav):
                self.experiment_nav.setCurrentIndex(list(EXPERIMENTS).index(key))
            self._restore_draft(draft)
            self.temperature_workspace.confirm.setChecked(False)
            self._clear_history_details()
            self.clear_comparison_selection()
            self.analysis_plot.display({})
            self.diagnostic_model = {}
            self.analysis_title.setText("选择历史记录后加载完整诊断")
            self._update_history_type_options()
            self.history_type.setCurrentIndex(0)
            self.history_status.setCurrentIndex(0)
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
                "advanced": self.advanced_toggle.isChecked(), "liquid_setup": deepcopy(self._liquid_setup)}

    def _restore_draft(self, draft):
        for key, value in draft["values"].items():
            # 旧草稿的设备选择和已移除的扫频时限均不覆盖当前运行规则。
            if key in ("profile", "max_seconds", "auto_sweep_budget"):
                continue
            widget = self.fields[key]
            with QtCore.QSignalBlocker(widget):
                if isinstance(widget, QtWidgets.QComboBox):
                    index = widget.findData(value)
                    if index >= 0:
                        widget.setCurrentIndex(index)
                elif isinstance(widget, QtWidgets.QCheckBox):
                    widget.setChecked(False)  # Confirmation is for the next physical setup only.
                elif isinstance(widget, QtWidgets.QLineEdit):
                    widget.setText(value)
                else:
                    widget.setValue(value)
        self.material_workspace.material_name.setText(draft["material_name"])
        self.advanced_toggle.setChecked(draft["advanced"])
        self._liquid_setup = deepcopy(draft["liquid_setup"])

    def _show_experiment(self):
        key = self.experiment_id
        name, description, _ = EXPERIMENTS[key]
        self.sidebar_title.setText(name + " · 参数设置")
        self.experiment_description.setText(description)
        self.archive_button.setText("实验档案")
        self.archive_button.setToolTip("查看当前实验的已保存记录；勾选单条分析，或勾选多条同类记录组合分析。")
        self._update_history_type_options()
        self.config_container.setVisible(key not in ("temperature", "temperature_calibration"))
        self.rf_actions.setVisible(key not in ("temperature", "temperature_calibration"))
        self.temperature_settings.setVisible(key in ("temperature", "temperature_calibration"))
        self.liquid_settings.setVisible(key == "liquid")
        self.finger_settings.setVisible(key == "finger")
        self.temperature_sweep_settings.setVisible(key == "temperature_sweep")
        self.sidebar_scroll.verticalScrollBar().setValue(0)
        visible = {0, 1, 2, 3, self.analysis_tab}
        if key == "finger":
            visible.add(self.finger_tab)
        elif key == "liquid":
            visible.add(self.liquid_tab)
        elif key == "temperature_sweep":
            visible.add(self.temperature_sweep_tab)
        elif key in ("temperature", "temperature_calibration"):
            visible = {self.temperature_tab, self.analysis_tab, 1}
        self.tabs.blockSignals(True)
        for index in range(self.tabs.count()):
            self.tabs.setTabVisible(index, index in visible)
        self.tabs.setCurrentIndex(self.temperature_tab if key in ("temperature", "temperature_calibration") else
                                  self.temperature_sweep_tab if key == "temperature_sweep" else 0)
        self.tabs.blockSignals(False)
        self.tabs.setTabText(self.temperature_tab, "实时曲线")
        self.tabs.setTabText(self.liquid_tab, "温度记录与比较")
        self.tabs.setTabText(1, "实验档案")
        self.tabs.setTabText(2, "曲线诊断")
        self.tabs.setTabText(self.analysis_tab, "研究分析")
        if key == "liquid" and hasattr(self.service, "liquid_history"):
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
        if config.get("temperature_sweep"):
            return "temperature_sweep"
        if config.get("liquid_temperature"):
            return "liquid"
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
        layout.addWidget(self.sidebar_title)
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
        workspace = QtWidgets.QGroupBox("采集流程")
        wl = QtWidgets.QVBoxLayout(workspace)
        self.mode = self._combo("mode", [("手指位移 · 固定频率", "phase"), ("材料响应 · 逐点扫频", "sweep")])
        self.mode.setCurrentIndex(self.mode.findData("sweep"))
        self.mode.hide()
        wl.addWidget(self.mode)
        self.workspace_hint = label("", "muted")
        wl.addWidget(self.workspace_hint)
        self.research_button = QtWidgets.QPushButton("进入手指标定与验证 →")
        self.research_button.clicked.connect(lambda: self.tabs.setCurrentIndex(self.finger_tab))
        wl.addWidget(self.research_button)
        self.liquid_clear_button = QtWidgets.QPushButton("清除已应用的温度条件")
        self.liquid_clear_button.clicked.connect(self._clear_liquid)
        self.liquid_clear_button.hide()
        wl.addWidget(self.liquid_clear_button)
        cl.addWidget(workspace)
        self.material_workspace = MaterialSweepWidget()
        cl.addWidget(self.material_workspace)
        group = QtWidgets.QGroupBox("实验记录")
        form = QtWidgets.QFormLayout(group)
        self._combo("condition", [("手指距离实验", "finger"), ("材料响应实验", "material")]).hide()
        self.fields["label"] = QtWidgets.QLineEdit()
        self.fields["label"].setMaxLength(80)
        self.fields["label"].setToolTip("文字、数字、下划线和连字符")
        self.session_label = label("会话标签")
        form.addRow(self.session_label, self.fields["label"])
        self.fields["sensor"] = QtWidgets.QLineEdit()
        self.fields["sensor"].setMaxLength(80)
        self.fields["sensor"].setPlaceholderText("传感器名称／版本")
        form.addRow("传感器", self.fields["sensor"])
        cl.addWidget(group)
        self.phase_group = QtWidgets.QGroupBox("手指实验 · 固定频率连续记录")
        pf = QtWidgets.QFormLayout(self.phase_group)
        self._number(pf, "carrier_ghz", "载频 GHz", .07, 6, 3, .001)
        self._number(pf, "duration", "记录时长 s", 10, 180, 1, 10)
        self._number(pf, "tone", "基带单音 Hz", 1, 50000, 0, 100)
        pf.addRow(label("先保持初始状态静止约 7 秒，参考通过后再标记阶段。", "muted"))
        cl.addWidget(self.phase_group)
        self.sweep_group = QtWidgets.QGroupBox("材料实验 · 扫频计划")
        sf = QtWidgets.QFormLayout(self.sweep_group)
        for key, name, lo, hi, decimals, step in [
            ("freq_start_ghz", "起点 GHz", .07, 6, 3, .001), ("freq_stop_ghz", "终点 GHz", .07, 6, 3, .001),
            ("points", "频点数", 2, 100, 0, 1), ("repeat", "重复轮数", 1, 20, 0, 1),
            ("point_timeout", "单点时限 s", 1, 30, 1, .5),
            ("tone_start", "首个单音 Hz", 1, 50000, 0, 100), ("tone_step", "单音步进 Hz", 0, 20000, 0, 100)]:
            self._number(sf, key, name, lo, hi, decimals, step)
        self.sweep_completion_hint = label("按指定轮数完成；也可随时停止并保存。", "muted")
        self.sweep_completion_hint.setWordWrap(True)
        sf.addRow(self.sweep_completion_hint)
        for key in ('points', 'repeat', 'point_timeout'):
            self.fields[key].valueChanged.connect(self._sweep_parameters_changed)
        cl.addWidget(self.sweep_group)
        advanced = QtWidgets.QGroupBox("高级参数")
        al = QtWidgets.QVBoxLayout(advanced)
        self.advanced_toggle = QtWidgets.QCheckBox("展开采样率、幅值和增益")
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
        cl.addWidget(advanced)
        self.confirm = QtWidgets.QCheckBox("已确认接线、线缆固定与初始状态")
        self.confirm.setToolTip("所选设备 TX1/RX1 接在传感器两端；确认本次允许频段、功率与负载。")
        self.fields["setup_confirmed"] = self.confirm
        cl.addWidget(self.confirm)
        config_layout.addWidget(self.config_container)
        config_layout.addStretch()
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)
        self.rf_actions = QtWidgets.QWidget()
        actions = QtWidgets.QVBoxLayout(self.rf_actions)
        actions.setContentsMargins(0, 0, 0, 0)
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
        if getattr(self,'_navigating',False):return
        if hasattr(self,'confirm'):self.confirm.setChecked(False)

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
        if self._liquid_setup and self._liquid_setup["mode"] != self.mode.currentData():
            self._liquid_setup = None
            self.confirm.setChecked(False)
        liquid = self._liquid_setup is not None
        liquid_page = self.experiment_id == "liquid"
        self.liquid_clear_button.setVisible(liquid)
        self.mode.setItemText(0, "液体温度 · 固定频率" if liquid and phase else "手指位移 · 固定频率")
        self.mode.setItemText(1, "液体温度 · 逐点扫频" if liquid and not phase else "材料响应 · 逐点扫频")
        self.phase_group.setVisible(phase)
        self.sweep_group.setVisible(not phase)
        self.material_workspace.setVisible(not phase and not liquid_page and self.experiment_id != "temperature_sweep")
        self.fields["condition"].setCurrentIndex(self.fields["condition"].findData("finger" if phase else "material"))
        self.fields["label"].setVisible(phase)
        self.session_label.setVisible(phase)
        self.research_button.hide()
        self.workspace_hint.setText("连续幅相 → 尺量定位 → 稳定窗口 → 标定与独立验证 → 毫米估计" if phase else "填写材料名称 → 固定条件 → 多轮扫频 → 幅度响应与导出")
        self.start_button.setText("开始手指实验" if phase else "开始材料扫频")
        self.phase_group.setTitle("固定频率 · 连续幅相" if liquid_page else "手指实验 · 固定频率连续记录")
        self.sweep_group.setTitle("液体实验 · 扫频计划" if liquid_page else "材料实验 · 扫频计划")
        if liquid_page and not liquid:
            self.workspace_hint.setText("填写上方温度条件并应用，再检查采集参数。每次采集都需重新应用实测温度。")
            self.start_button.setText("先应用温度条件")
        if liquid:
            info = self._liquid_setup["liquid_temperature"]
            self.workspace_hint.setText(f"液体温度实验：{info['liquid']} · 开始参考 {info['temperature_c']:g} ℃ · 距离 {info['distance_mm']:g} mm")
            self.start_button.setText("开始液体温度相位实验" if phase else "开始液体温度扫频")
        self.fields["repeat"].setEnabled(True)
        self.sweep_completion_hint.setText("按指定轮数完成；也可随时停止并保存。")
        if self.experiment_id == "temperature_sweep":
            self.workspace_hint.setText("填写温度平台 → 确认双设备 → 同时记录扫频与电压 → 切换温度后重复")
            continuous=self.temperature_sweep_workspace.requires_serial
            self.start_button.setText("开始连续变温循环扫频" if continuous else "开始手填温度平台扫频")
            self.fields['repeat'].setEnabled(not continuous)
            self.workspace_hint.setText("持续变温并循环扫频，整轮平均电压关联曲线；点击停止并保存结束" if continuous else "填写本次实测温度；每调整一个温度，单独开始一次实验")
            if continuous:
                self.sweep_completion_hint.setText("持续循环扫频，点击“停止并保存”结束。")
            self.sweep_group.setTitle("SSPP 温度影响 · 扫频计划")
        if hasattr(self, "tabs") and hasattr(self, "finger_tab"):
            self.tabs.setTabEnabled(self.finger_tab, phase and not liquid and not liquid_page)
        if hasattr(self, "live_plots"):
            self.workflow_shortcut.setVisible(phase and not liquid)
            self.mark_group.setVisible(phase and self.viewing_key is None and self.replay is None)
            if not self.snapshot.get("job") and self.viewing_key is None:
                self._render_live({"mode": "phase" if phase else "sweep", "points": []})
                self._live_revision = None

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
        top.addWidget(label("已保存实验 · 单行查看详情，勾选记录进行单条分析或多条组合分析", "viewBanner"), 1)
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
        self.history_search.setPlaceholderText("搜索名称、传感器或实验 key")
        self.history_scope_label = label("", "muted")
        filters.addWidget(self.history_scope_label)
        filters.addWidget(self.history_type)
        filters.addWidget(self.history_status)
        filters.addWidget(self.history_search, 1)
        self.history_type.currentIndexChanged.connect(self._filter_history)
        self.history_status.currentIndexChanged.connect(self._filter_history)
        self.history_search.textChanged.connect(self._filter_history)
        layout.addLayout(filters)
        selection = QtWidgets.QHBoxLayout()
        self.comparison_count = label("已勾选 0 / 8 条", "muted")
        selection.addWidget(self.comparison_count, 1)
        self.comparison_clear = QtWidgets.QPushButton("清空勾选")
        self.comparison_clear.clicked.connect(self.clear_comparison_selection)
        selection.addWidget(self.comparison_clear)
        self.compare_button = QtWidgets.QPushButton("分析所选记录")
        self.compare_button.setObjectName("primary")
        self.compare_button.clicked.connect(self.compare_selected)
        selection.addWidget(self.compare_button)
        layout.addLayout(selection)
        self.history_table = QtWidgets.QTableWidget(0, 6)
        self.history_table.setHorizontalHeaderLabels(["选择", "开始时间（本机时区）", "实验标签", "类型", "状态", "传感器"])
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
                  "profile": self.profile.currentData(), "max_seconds": None}
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
        if self._liquid_setup:
            result["liquid_temperature"] = dict(self._liquid_setup["liquid_temperature"])
            result["condition"] = "material"
        if self.experiment_id == "temperature_sweep":
            result["temperature_sweep"] = self.temperature_sweep_workspace.config()
            result["mode"] = "sweep"
            result["label"] = "".join(c if c.isalnum() or c in "_-" else "_"
                                      for c in result["temperature_sweep"]["condition_label"])[:80].strip("_") or "temperature_sweep"
        return result

    def _liquid_conditions_edited(self, *_):
        if self.experiment_id != "liquid" or self._navigating:
            return
        applied = self._liquid_setup is not None
        self._liquid_setup = None
        self.confirm.setChecked(False)
        self.mode.setCurrentIndex(self.mode.findData(self.liquid_workspace.mode.currentData()))
        self._mode_changed()
        if applied:
            self._message("温度条件已修改，请重新应用本次条件，再确认接线并开始。")

    def _prepare_liquid(self, request):
        if self._parameter_reset_blocked() or self.temperature_workspace.is_running:
            self._message("请先完成当前采集、检查或分析，再应用新的温度条件。")
            return
        from sspp_experiments.liquid_temperature.metadata import validate_metadata
        try:
            metadata = validate_metadata(request.get("liquid_temperature"))
            if request.get("mode") not in ("sweep", "phase"):
                raise ValueError("液体温度实验模式无效")
        except ValueError as exc:
            self._error(exc)
            return
        if not self.select_experiment("liquid"):
            return
        with QtCore.QSignalBlocker(self.liquid_workspace.mode):
            self.liquid_workspace.mode.setCurrentIndex(self.liquid_workspace.mode.findData(request["mode"]))
        for key, field in self.liquid_workspace.fields.items():
            with QtCore.QSignalBlocker(field):
                field.setText(str(metadata[key]))
        self.mode.setCurrentIndex(self.mode.findData(request["mode"]))
        self._liquid_setup = {"mode": request["mode"], "liquid_temperature": metadata}
        name = "liquid_temperature_" + datetime.now().strftime("%H%M%S")
        self.fields["label"].setText(name)
        self.material_workspace.material_name.setText(name)
        self.confirm.setChecked(False)
        self._mode_changed()
        self._show_tab(0)
        self._message("液体温度条件已应用；检查本次采集参数、参考温度及接线后勾选确认开始。")

    def _clear_liquid(self):
        self._liquid_setup = None
        self.confirm.setChecked(False)
        self._mode_changed()

    def _valid_workspace(self):
        try:
            if self.experiment_id == "liquid" and not self._liquid_setup:
                raise ValueError("请先在左侧填写并应用本次液体温度条件。")
            if self.experiment_id == "temperature_sweep":
                self.temperature_sweep_workspace.config()
            elif self.mode.currentData() == "sweep":
                self.material_workspace.validate()
            return True
        except ValueError as exc:
            self._error(exc)
            self.material_workspace.material_name.setFocus()
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
        if self.selected_details.get("context", {}).get("experiment", "").startswith("liquid_temperature_"):
            self._message("液体温度记录请进入液体温度影响页分析。")
            return
        key = self.selected_details["key"]
        if not key.startswith("phase_runs/"):
            self._message("只有固定频率幅相记录可以标定手指距离。")
            return
        curve = self.selected_details.get("curve") or {}
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
        return (self._close_requested or self._start_pending or self._environment_busy()
                or self.temperature_workspace.is_running
                or bool(self.snapshot.get("preparing"))
                or (self.snapshot.get("job") or {}).get("status") in BUSY
                or "start" in self.runner.pending or "preflight" in self.runner.pending
                or not self.config_container.isEnabled())

    def reset_parameters(self):
        """仅复位待采集表单；包含隐藏模式，避免留下无效的参数组合。"""
        if self._parameter_reset_blocked():
            self._message("实验启动、采集、停止保存或参数检查期间不能复位，请等待完成。")
            return
        for key, widget in self.fields.items():
            if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                defaults = {**DEFAULTS, **({"freq_start_ghz": 5.0, "freq_stop_ghz": 5.3, "points": 20}
                                          if self.experiment_id == "temperature_sweep" else {})}
                widget.setValue(defaults[key])
        self.confirm.setChecked(False)
        self._message("当前实验的采集参数及高级参数已恢复默认值，其他实验草稿不变。设备、名称和手指标定记录保留；请重新检查参数并确认接线。")

    def start_experiment(self):
        if self.experiment_id in ("temperature", "temperature_calibration"):
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
        if self.experiment_id == "temperature_sweep" and self.temperature_sweep_workspace.requires_serial and not self._environment_ready("serial"):
            self._message("请在初始化实验台中同时检查 B210 与温度 USB 串口。")
            return
        if not self.confirm.isChecked():
            self._message("请先确认所选设备接线、线缆固定和初始状态。")
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
        comparison_work = (self._comparison_busy or "comparison_export" in self.runner.pending
                           or "figure_export" in self.runner.pending or "calibration_correction" in self.runner.pending)
        liquid_work = hasattr(self, "liquid_workspace") and self.liquid_workspace.is_busy
        initializing = self._environment_busy()
        ready = self.experiment_id not in ("temperature", "temperature_calibration") and self._environment_ready("usrp")
        if self.experiment_id == "temperature_sweep" and self.temperature_sweep_workspace.requires_serial:
            ready = ready and self._environment_ready("serial")
        locked = active or initializing
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
        self.check_button.setEnabled(not locked and not temperature_active and "preflight" not in self.runner.pending and not self._close_requested)
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
            self.analysis_workspace.set_acquisition_busy(locked or temperature_active or self._close_requested)
        if hasattr(self, "liquid_workspace"):
            self.liquid_workspace.set_external_busy(locked or temperature_active or comparison_work or self._close_requested)

    def _temperature_busy_changed(self, busy):
        active = ((self.snapshot.get("job") or {}).get("status") in BUSY
                  or self._start_pending or bool(self.snapshot.get("preparing")))
        self._set_active(active)
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
        self.runner.submit("poll", poll, self._poll_done, self._error, "polling")

    def _poll_done(self, result):
        snapshot, live, *research = result
        self._apply_status(snapshot)
        job_experiment = self._job_experiment(snapshot.get("job"))
        if research and job_experiment == "finger":
            self.finger_workspace.apply_snapshot(research[0])
            if self.finger_workspace._pending:
                self.mark_group.setEnabled(False)
        if self.finger_workspace.source_key is None and job_experiment == "finger":
            self.finger_workspace.update_curve(live)
        if self.viewing_key is None and self.replay is None and job_experiment in (None, self.experiment_id):
            if live.get("revision") != self._live_revision or live.get("revision") is None:
                self._render_live(live)
                if self.experiment_id == "temperature_sweep":
                    self.temperature_sweep_workspace.update_snapshot(live)
                self._live_revision = live.get("revision")
        text = "\n".join(snapshot.get("logs") or []) if job_experiment in (None, self.experiment_id) else ""
        if self.logs.toPlainText() != text:
            self.logs.setPlainText(text)
            self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())

    def _apply_status(self, snapshot):
        previous = (self.snapshot.get("job") or {}).get("status")
        self.snapshot = snapshot or {}
        job = self.snapshot.get("job") or {}
        status = job.get("status")
        active = status in BUSY or self._start_pending or bool(self.snapshot.get("preparing"))
        if previous in BUSY and status in {"initialization_failed", "stop_unconfirmed", "failed"}:
            self.service.invalidate_environment()
        self._set_active(active)
        self.status_label.setText("正在准备" if self.snapshot.get("preparing") or self._start_pending else STATUS.get(status, "待开始"))
        if job.get("errors"):
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
        liquid = (same_experiment and bool(job.get("config", {}).get("liquid_temperature"))) or self.experiment_id == "liquid"
        self.finger_workspace.set_acquisition(active, can_mark and not liquid and not self._close_requested)
        self.workflow_shortcut.setVisible(self.mode.currentData() == "phase" and not liquid)
        self.current_result_button.setEnabled(not active and bool(job.get("output"))
                                              and self._job_experiment(job) == self.experiment_id)
        if self.temperature_workspace.is_running:
            self.status_label.setText("Arduino 固件准备 / 验证" if self.temperature_workspace.preparation.is_running
                                      else "温压正在采集 / 保存")
        self.status_label.setToolTip(self.status_detail.text())
        self.mark_group.setVisible(self.mode.currentData() == "phase" and self.viewing_key is None and self.replay is None)
        self.mark_group.setEnabled(can_mark and self.viewing_key is None and self.replay is None)
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
        if self.experiment_id == "temperature_sweep":
            self.temperature_sweep_workspace.clear()
            self._show_tab(self.temperature_sweep_tab)
        elif self.experiment_id in ("temperature","temperature_calibration"):
            self._show_tab(self.temperature_tab)
        else:
            self._show_tab(0)

    def refresh_history(self, select_key=None):
        if self._close_requested:
            return
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

        def loaded(rows):
            try:
                if self._close_requested or revision != self._history_refresh_revision:
                    return
                self._history_refresh_select_key = None
                self._history_all_rows = rows
                self._render_history_rows(select_key if isinstance(select_key, str) else self._selected_history_key)
                if isinstance(select_key, str) and self.history_table.currentRow() >= 0:
                    self._select_history()
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
        self._render_history_rows(self._selected_history_key)

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
                             and (not query or query in " ".join(str(row.get(field, "")) for field in ("label", "sensor", "key")).casefold())]
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
            checked.setToolTip("勾选 1–8 条同类实验：单条分析，多条组合分析；高亮一行仍可查看详情。")
            self.history_table.setItem(index, 0, checked)
            values = [self._local_time(row.get("started_utc")), row.get("label", ""), CATEGORIES.get(self._history_category(row), "未知"),
                      STATUS.get(row.get("status"), row.get("status", "")), row.get("sensor", "")]
            for column, value in enumerate(values, 1):
                item = QtWidgets.QTableWidgetItem(str(value))
                item.setData(QtCore.Qt.ItemDataRole.UserRole, row["key"])
                item.setToolTip(f"{row['key']}\n原始 UTC：{row.get('started_utc', '未知')}")
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
        self._selected_history_key = None
        self.selected_details = None
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
        return ((self.snapshot.get("job") or {}).get("status") in BUSY or self._start_pending
                or bool(self.snapshot.get("preparing")) or self._close_requested or self._environment_busy()
                or (hasattr(self, "temperature_workspace") and self.temperature_workspace.is_running)
                or (hasattr(self, "liquid_workspace") and self.liquid_workspace.is_busy)
                or "export" in self.runner.pending or "comparison_export" in self.runner.pending
                or "figure_export" in self.runner.pending
                or "calibration_correction" in self.runner.pending
                or "finger_analysis" in self.runner.pending)

    def _update_comparison_controls(self, active=None):
        if not hasattr(self, "compare_button"):
            return
        blocked = self._comparison_blocked() or bool(active)
        count = len(self.comparison_keys)
        self.comparison_count.setText(f"已勾选 {count} / 8 条 · 单条分析，多条组合")
        self.compare_button.setText("组合分析所选记录" if count > 1 else "分析所选记录")
        self.compare_button.setToolTip("勾选 1 条记录进行分析，或勾选 2–8 条同类记录组合分析。")
        self.compare_button.setEnabled(1 <= count <= 8 and not blocked and not self._comparison_busy)
        self.comparison_clear.setEnabled(bool(self.comparison_keys) and not blocked)
        self.history_table.blockSignals(True)
        for row in range(self.history_table.rowCount()):
            item = self.history_table.item(row, 0)
            if item:
                flags = item.flags()
                item.setFlags(flags & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable if blocked else flags | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
        self.history_table.blockSignals(False)
        if self.comparison_dialog is not None and not self.comparison_dialog.is_closed:
            self.comparison_dialog.set_busy(self._comparison_busy or "comparison_export" in self.runner.pending
                                            or "figure_export" in self.runner.pending, blocked=blocked)
        if hasattr(self, "serial_archive_workspace"):
            self.serial_archive_workspace.set_edit_busy(blocked or self._comparison_busy)
        self._update_diagnostic_export_controls()

    def _comparison_item_changed(self, item):
        if item.column() != 0:
            return
        key = item.data(QtCore.Qt.ItemDataRole.UserRole)
        checked = item.checkState() == QtCore.Qt.CheckState.Checked
        rows = {row["key"]: row for row in self.history_rows}
        error = ""
        if self._comparison_blocked():
            error = "采集或导出处理中，暂不能变更分析勾选。"
        elif checked:
            category = self._history_category(rows.get(key, {}))
            if category == "teacher_sweep":
                error = "老师原版记录的数据格式不同，暂不支持跨记录比较；仍可查看、分析与单条打包。"
            elif category.startswith("liquid_temperature_"):
                error = "请进入液体温度影响页比较，那里会检查温度、几何条件与同次相位参考。"
            elif category not in CATEGORIES:
                error = "此记录类型未知，不能加入分析。"
            elif len(self.comparison_keys) >= 8 and key not in self.comparison_keys:
                error = "一次最多分析 8 条实验，请先取消一条勾选。"
            elif any(self._history_category(rows.get(selected, {})) != category for selected in self.comparison_keys):
                error = "请勾选同一种实验类型；切换类型前可先清空勾选。"
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
        if self._comparison_blocked() or self._comparison_busy:
            self._message("请等待采集、分析或导出结束，再分析所选记录。")
            return
        keys = [row["key"] for row in self.history_rows if row["key"] in self.comparison_keys]
        if not 1 <= len(keys) <= 8:
            self._message("请先勾选 1–8 条同类实验记录。")
            return
        self._request_comparison(keys)

    def _request_comparison(self, keys, reference_key=None, dialog=None):
        if self._comparison_blocked() or self._comparison_busy:
            return
        self._comparison_generation += 1
        generation = self._comparison_generation
        self._comparison_busy = True
        if dialog is not None:
            dialog.set_busy(True, "正在重新计算参考实验…")
        self._set_active(False)
        self._message("正在后台读取所选记录并分析参数与结果…" if len(keys) == 1 else
                      "正在后台读取所选记录并组合分析参数与结果…")
        def completed(report):
            self._comparison_busy = False
            if generation != self._comparison_generation or self._close_requested or (dialog is not None and dialog.is_closed):
                if dialog is not None and not dialog.is_closed and dialog.report:
                    dialog.reference.blockSignals(True)
                    dialog.reference.setCurrentIndex(dialog.reference.findData(dialog.report.get("reference_key")))
                    dialog.reference.blockSignals(False)
                    dialog.status.setText("选择已变更，保留上一次完整分析结果。")
                self._apply_status(self.snapshot)
                return
            target = dialog
            if target is None:
                if self.comparison_dialog is not None and not self.comparison_dialog.is_closed:
                    self.comparison_dialog.close()
                target = ComparisonDialog(self)
                self.comparison_dialog = target
                target.reference_requested.connect(lambda key: self._request_comparison(list(target.report["keys"]), key, target))
                target.export_requested.connect(lambda: self._export_comparison(target))
                target.figure_export_requested.connect(lambda chart_id: self._export_publication_figure(target, chart_id))
                target.figure_save_as_requested.connect(lambda chart_id: self._export_analysis_figure(target, chart_id))
                target.open_figure_directory_requested.connect(lambda: self._open_publication_directory(target))
                target.closed.connect(lambda: self._comparison_closed(target))
            target.apply_report(report)
            target.show()
            target.raise_()
            self._message("分析已完成；原始实验记录未修改。" if len(keys) == 1 else
                          "组合分析已完成；原始实验记录未修改。")
            self._apply_status(self.snapshot)
        def failed(error):
            self._comparison_busy = False
            if dialog is not None and not dialog.is_closed:
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
        serial = report.get("kind") in ("temperature_calibration", "temperature_pressure")
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(
            dialog, "保存论文图与数据" if serial else "保存实验比较压缩包",
            str(Path.home() / "Downloads" / ("temperature_publication.zip" if serial else "sspp_comparison.zip")),
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
            self.refresh_history(select_key=key)
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
        if self.selected_details and self.selected_details['key'].startswith('temperature_pressure_runs/'):
            self._show_tab(self.serial_archive_tab)
        else:
            self._show_tab(0)

    def _history_buttons(self, enabled):
        for widget in (self.view_history_button, self.analyze_button, self.export_button, self.open_run_button, self.replay_button):
            widget.setEnabled(enabled)
        self.pause_button.setEnabled(False)
        self.replay_slider.setEnabled(False)
        if hasattr(self, "finger_history_button"):
            self.finger_history_button.setEnabled(enabled and bool(self.selected_details) and self.selected_details["key"].startswith("phase_runs/")
                and not self.selected_details.get("context", {}).get("experiment", "").startswith("liquid_temperature_"))

    def _select_history(self):
        row = self.history_table.currentRow()
        item = self.history_table.item(row, 0) if row >= 0 else None
        if item is None:
            return
        key = item.data(QtCore.Qt.ItemDataRole.UserRole)
        self._selected_history_key = key
        self._history_generation += 1
        generation = self._history_generation
        self.replay_running = False
        self.replay = None
        self.replay_controls.hide()
        self.replay_note.hide()
        self.viewing_key = key
        self.selected_details = None
        self._history_buttons(False)
        self.metadata.setPlainText("正在读取记录…")
        self.file_list.clear()
        self.view_banner.setText(f"历史静态记录 · {key} · 非实时采集")
        self.live_plots.clear()
        self.preview_plots.update_preview(None)
        def loaded(details):
            if generation != self._history_generation:
                return
            self.selected_details = details
            self.metadata.setPlainText(json.dumps(details.get("metadata", {}), ensure_ascii=False, indent=2))
            self.file_list.addItems(details.get("files", []))
            serial=key.startswith('temperature_pressure_runs/')
            if serial:
                self.serial_archive_workspace.display(details)
                self.tabs.setTabVisible(self.serial_archive_tab,True)
                self.tabs.setTabText(self.serial_archive_tab,'温度标定档案详情' if details['context']['experiment']=='temperature_calibration' else '温压档案详情')
            else:
                self.tabs.setTabVisible(self.serial_archive_tab,False)
                self.live_plots.update_snapshot(details.get("curve") or {})
            if details.get("context", {}).get("experiment") == "temperature_sweep" and hasattr(self.service, "temperature_history"):
                def temperature_loaded(data):
                    if generation != self._history_generation:
                        return
                    self.temperature_sweep_workspace.update_snapshot({
                        **(details.get("curve") or {}), **data})
                    self.tabs.setTabVisible(self.temperature_sweep_tab, True)
                self._submit(f"temperature_history:{generation}", lambda: self.service.temperature_history(key), temperature_loaded)
            self.metrics.setText(f"历史已保存曲线 · {key}")
            self.preview_summary.setText("历史静态记录；点击回放可按时间重现 RX 预览。")
            self._history_buttons(True)
            self.replay_button.setEnabled(not serial and not key.startswith("teacher_runs/"))
            self.analyze_button.setText('查看标定点／电压' if serial else '加载完整诊断')
            # Loading the highlighted row must not navigate: checkbox clicks also
            # select a row, and several records may be checked for comparison.
            # The explicit saved-curve / diagnostics buttons open the detail page.
            self._apply_status(self.snapshot)
        self._submit(f"details:{generation}", lambda: self.service.details(key), loaded)

    def load_diagnostics(self):
        key = self.selected_details.get("key") if self.selected_details else None
        if not key:
            self._message("请先在历史记录中选择实验。")
            return
        if key.startswith('temperature_pressure_runs/'):
            self._show_tab(self.serial_archive_tab)
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
        key = self.selected_details.get("key") if self.selected_details else None
        if not key:
            return
        self.export_button.setEnabled(False)
        self._message("正在后台生成 PNG 与 PDF 图册…")
        def done(result):
            self._message(f"图册已生成：{result.get('count', 0)} 张 PNG。可在实验文件中打开或另存。")
            self._select_history()
        def failed(error):
            self._error(error)
            self._apply_status(self.snapshot)
        self._submit("export", lambda: self.service.export_diagnostics(key), done, failure=failed)

    def export_bundle(self):
        if not self.selected_details:
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
        if self.selected_details:
            key = self.selected_details["key"]
            self._submit("open_run", lambda: self.service.run_path(key), lambda path: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path))))

    def open_file(self, name):
        if self.selected_details:
            key = self.selected_details["key"]
            self._submit("open_file", lambda: self.service.download(key, name), lambda path: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path))))

    def save_file(self):
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
        if not self.selected_details:
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
