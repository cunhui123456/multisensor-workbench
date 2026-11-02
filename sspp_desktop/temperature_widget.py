"""温压串口页面。run_completed 在后台线程退出、文件封存后发相对 run key。"""

from pathlib import Path

from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

from sspp_experiments.temperature_pressure import CaptureConfig, PROTOCOLS, SerialCaptureSession, list_serial_ports
from .plots import COLORS, make_plot
from .qt_cleanup import close_plot_widgets
from .serial_ports import arduino_serial_ports
from .plateau_widget import PlateauWidget
from sspp_experiments.temperature_pressure.firmware import PreparationSession


class TemperaturePressureWidget(QtWidgets.QWidget):
    run_completed = QtCore.Signal(str)
    error = QtCore.Signal(str)
    capture_state_changed = QtCore.Signal(bool)
    busy_changed = QtCore.Signal(bool)
    environment_invalidated = QtCore.Signal()
    message = QtCore.Signal(str)
    preparation_changed = QtCore.Signal()

    def __init__(self, root: Path, parent=None, *, session=None, preparation=None):
        super().__init__(parent)
        self.root = Path(root)
        self.session = session or SerialCaptureSession(self.root)
        self.preparation = preparation or PreparationSession(self.root)
        self._operation = None
        self._preparation_display = ""
        self._published_key = None
        self._was_running = False
        self._last_rows = -1
        self._external_busy = False
        self._environment_required = False
        self._environment_ready = False
        self._plot_points_cache = []
        self.lines = {}
        self.calibration_mode = False
        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel("温压传感器 · 固件准备与实时采集")
        title.setStyleSheet("font-size: 18px; font-weight: 600; color: #087f85")
        layout.addWidget(title)
        note = QtWidgets.QLabel("先初始化 USB 串口，选择实验模式，烧录或验证对应固件；收到匹配数据后才能开始记录。曲线显示电压与 ADC，物理温度／压力仍需标定。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.settings_widget = QtWidgets.QWidget()
        self.settings_widget.setObjectName("temperaturePressureSettings")
        settings_layout = QtWidgets.QVBoxLayout(self.settings_widget)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        self.config_group = QtWidgets.QWidget()
        config_layout = QtWidgets.QVBoxLayout(self.config_group)
        config_layout.setContentsMargins(0, 0, 0, 0)
        config_layout.setSpacing(8)
        self.device_group = QtWidgets.QGroupBox("1 · 设备与实验模式")
        form = QtWidgets.QFormLayout(self.device_group)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.port = QtWidgets.QComboBox()
        self.port.setMinimumContentsLength(8)
        self.port.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.port.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.refresh_button = QtWidgets.QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh_ports)
        port_row = QtWidgets.QHBoxLayout()
        port_row.addWidget(self.port, 1)
        port_row.addWidget(self.refresh_button)
        form.addRow("USB 串口", port_row)
        self.board = QtWidgets.QComboBox()
        self.board.addItem("Nano 328P · 旧引导程序", "nano_old")
        self.board.setMinimumContentsLength(12)
        self.board.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.board.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.board.setToolTip("沿用已验证板型：上传 57600 波特，数据 9600 波特；不根据端口名判断板卡身份。")
        form.addRow("板卡", self.board)
        self.protocol = QtWidgets.QComboBox()
        self.protocol.setMinimumContentsLength(12)
        self.protocol.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.protocol.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        mode_labels = {"pressure_v1": "压力 · A2 · 原程序", "temperature_v1": "温度 · S6/S7 · v1"}
        for key, label in PROTOCOLS.items():
            self.protocol.addItem(mode_labels.get(key, label), key)
            self.protocol.setItemData(self.protocol.count() - 1, label, QtCore.Qt.ItemDataRole.ToolTipRole)
        form.addRow("实验模式", self.protocol)
        self.wiring_note = QtWidgets.QLabel()
        self.wiring_note.setWordWrap(True)
        form.addRow(self.wiring_note)
        self.confirm = QtWidgets.QCheckBox("已确认板卡与接线；\n其他串口工具已关闭")
        form.addRow(self.confirm)
        config_layout.addWidget(self.device_group)
        self.firmware_group = QtWidgets.QGroupBox("2 · 准备固件")
        firmware_layout = QtWidgets.QVBoxLayout(self.firmware_group)
        self.preparation_note = QtWidgets.QLabel("烧录前自动备份当前 Flash。烧录完成后校验写入，并检查实际串口输出。")
        self.preparation_note.setWordWrap(True)
        firmware_layout.addWidget(self.preparation_note)
        prepare_buttons = QtWidgets.QHBoxLayout()
        self.upload_button = QtWidgets.QPushButton("烧录并验证")
        self.upload_button.setObjectName("primary")
        self.upload_button.clicked.connect(lambda: self.start_preparation(upload=True))
        self.verify_button = QtWidgets.QPushButton("验证已有固件")
        self.verify_button.clicked.connect(lambda: self.start_preparation(upload=False))
        self.verify_button.setToolTip("只检查当前固件输出，不写入 Flash；协议匹配不等于确认固件二进制身份。")
        prepare_buttons.addWidget(self.upload_button)
        prepare_buttons.addWidget(self.verify_button)
        firmware_layout.addLayout(prepare_buttons)
        self.preparation_status = QtWidgets.QLabel("尚未准备")
        self.preparation_status.setWordWrap(True)
        firmware_layout.addWidget(self.preparation_status)
        config_layout.addWidget(self.firmware_group)
        self.metadata_group = QtWidgets.QGroupBox("3 · 样品与记录")
        metadata = QtWidgets.QFormLayout(self.metadata_group)
        metadata.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.DontWrapRows)
        metadata.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.sample_name = QtWidgets.QLineEdit()
        self.sample_name.setPlaceholderText("样品名称或编号")
        self.sample_name.setMaxLength(80)
        metadata.addRow("样品名称", self.sample_name)
        self.channel = QtWidgets.QLineEdit("A2")
        self.channel.setMaxLength(80)
        metadata.addRow("实际接线", self.channel)
        self.notes = QtWidgets.QLineEdit()
        self.notes.setMaxLength(1000)
        self.notes.setPlaceholderText("条件、选通位置、基线与参考仪器")
        metadata.addRow("实验说明", self.notes)
        config_layout.addWidget(self.metadata_group)
        settings_layout.addWidget(self.config_group)
        buttons = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("开始记录")
        self.start_button.clicked.connect(self.start_capture)
        self.stop_button = QtWidgets.QPushButton("停止并保存")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_capture)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        settings_layout.addLayout(buttons)
        self.plateau_controls = PlateauWidget()
        self.plateau_controls.hide()
        self.plateau_controls.requested.connect(self._plateau_requested)
        settings_layout.addWidget(self.plateau_controls)
        settings_layout.addStretch()
        self._settings_scroll = QtWidgets.QScrollArea()
        self._settings_scroll.setWidgetResizable(True)
        self._settings_scroll.setMaximumHeight(360)
        self._settings_scroll.setWidget(self.settings_widget)
        layout.addWidget(self._settings_scroll)
        self.workflow_status = QtWidgets.QLabel("① 连接设备 → ② 准备并验证固件 → ③ 记录实时曲线")
        self.workflow_status.setObjectName("viewBanner")
        self.workflow_status.setWordWrap(True)
        layout.addWidget(self.workflow_status)
        self.status = QtWidgets.QLabel("未开始；请先完成设备与固件准备。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.voltage_readout = QtWidgets.QLabel("尚无电压读数；有效数据到达后显示最新值。")
        self.voltage_readout.setWordWrap(True)
        self.voltage_readout.setStyleSheet("font-size: 15px; font-weight: 600; color: #087f85")
        layout.addWidget(self.voltage_readout)
        plot_tools = QtWidgets.QHBoxLayout()
        self.auto_follow = QtWidgets.QCheckBox("自动跟随实时数据")
        self.auto_follow.setChecked(True)
        self.auto_follow.setToolTip("随有效样本自动调整坐标范围。拖动或滚轮缩放会暂停跟随，原始数据仍继续保存。")
        self.auto_follow.toggled.connect(self._follow_changed)
        plot_tools.addWidget(self.auto_follow)
        self.display_channel = QtWidgets.QComboBox()
        self.display_channel.addItem("显示全部通道", "all")
        self.display_channel.addItem("只看 CH1 · S6", "S6")
        self.display_channel.addItem("只看 CH2 · S7", "S7")
        self.display_channel.setToolTip("选择单通道可放大观察电压细微变化；所有通道继续采集和保存。")
        self.display_channel.currentIndexChanged.connect(lambda *_: self._plot_points(self._plot_points_cache))
        plot_tools.addWidget(self.display_channel)
        plot_tools.addStretch()
        self.full_range_button = QtWidgets.QPushButton("恢复全图")
        self.full_range_button.clicked.connect(self._restore_plot_range)
        plot_tools.addWidget(self.full_range_button)
        layout.addLayout(plot_tools)
        self.voltage_plot = make_plot("串口名义电压", "主机接收经过时间（s）", "电压（V）")
        self.voltage_plot.addLegend(offset=(12, 12))
        self.adc_plot = make_plot("固件平均 ADC", "主机接收经过时间（s）", "ADC 码值")
        self.adc_plot.setXLink(self.voltage_plot)
        self.adc_plot.addLegend(offset=(12, 12))
        for plot in (self.voltage_plot, self.adc_plot):
            plot.plotItem.vb.sigRangeChangedManually.connect(lambda *_: self.auto_follow.setChecked(False))
        self.plot_panel = QtWidgets.QWidget()
        plot_layout = QtWidgets.QHBoxLayout(self.plot_panel)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        plot_layout.addWidget(self.voltage_plot, 1)
        plot_layout.addWidget(self.adc_plot, 1)
        layout.addWidget(self.plot_panel, 1)
        self.plateau_results = QtWidgets.QGroupBox("标定区间与稳定性复核")
        result_layout = QtWidgets.QVBoxLayout(self.plateau_results)
        for control in (self.plateau_controls.status, self.plateau_controls.table):
            self.plateau_controls.layout().removeWidget(control)
            result_layout.addWidget(control)
        self.plateau_results.hide()
        layout.addWidget(self.plateau_results)
        self.event_text = QtWidgets.QPlainTextEdit()
        self.event_text.setReadOnly(True)
        self.event_text.setMaximumHeight(130)
        self.event_text.setMaximumBlockCount(300)
        self.event_text.setPlaceholderText("状态、复位、回绕、错误和断连记录")
        layout.addWidget(self.event_text)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        self._settings_focus_timer = QtCore.QTimer(self)
        self._settings_focus_timer.setSingleShot(True)
        self._settings_focus_timer.timeout.connect(self._show_recording_settings)
        self.port.currentIndexChanged.connect(self._target_changed)
        self.protocol.currentIndexChanged.connect(self._protocol_changed)
        self.protocol.currentIndexChanged.connect(self._target_changed)
        self.board.currentIndexChanged.connect(self._target_changed)
        self.confirm.toggled.connect(self._update_start_enabled)
        self.sample_name.textChanged.connect(self._update_start_enabled)
        self.channel.textChanged.connect(self._update_start_enabled)
        self._protocol_changed()
        self.refresh_ports()
        self._update_start_enabled()

    def set_calibration_mode(self, enabled):
        self.calibration_mode = bool(enabled)
        self.plateau_controls.setVisible(enabled)
        self.plateau_results.setVisible(enabled)
        self.event_text.setMaximumHeight(80 if enabled else 130)
        if enabled:
            self.protocol.setCurrentIndex(self.protocol.findData("temperature_v1"))
        self.protocol.setEnabled(not enabled)
        self.adc_plot.setTitle("相对初始电压变化" if enabled else "固件平均 ADC")
        self.adc_plot.setLabel("left", "ΔV（V）" if enabled else "ADC 码值")
        self._plot_points_cache = []
        for pair in self.lines.values():
            for line in pair:
                line.setData([], [])

    def _plateau_requested(self, operation, values):
        try:
            self.session.plateau_command(operation, **values)
            self.plateau_controls.update_state(self.session.snapshot().get("plateau",{}), True, True)
        except ValueError as exc:
            self.plateau_controls.show_error(str(exc))

    def take_settings_widget(self):
        """取出配置及开始/停止控件，供全局侧栏重新父级；不会操作串口。"""
        if self._settings_scroll is not None:
            scroll = self._settings_scroll
            self._settings_scroll = None
            scroll.takeWidget()
            self.layout().removeWidget(scroll)
            scroll.hide()
            scroll.deleteLater()
            self.settings_widget.setParent(None)
        return self.settings_widget

    @property
    def is_running(self):
        return bool(self._operation or self.session.is_running or self.preparation.is_running)

    def refresh_ports(self):
        if self.is_running:
            return
        selected = self.port.currentData()
        with QtCore.QSignalBlocker(self.port):
            self.port.clear()
            for name in arduino_serial_ports(list_serial_ports()):
                self.port.addItem(name.removeprefix("/dev/cu."), name)
                self.port.setItemData(self.port.count() - 1, name, QtCore.Qt.ItemDataRole.ToolTipRole)
            index = self.port.findData(selected)
            if index >= 0:
                self.port.setCurrentIndex(index)
        if self.port.currentData() != selected:
            self.port.currentIndexChanged.emit(self.port.currentIndex())
        if not self.port.count():
            self.status.setText("未发现 Arduino USB 串口。请连接开发板的数据线后刷新；蓝牙与系统端口不用于本实验。")

    def set_external_busy(self, busy):
        """RF 采集时阻止新开串口；已有串口采集仍可停止。"""
        self._external_busy = bool(busy)
        self._update_start_enabled()

    def set_environment_required(self, required: bool):
        """主窗可要求先完成设备初始化；独立使用默认不要求。"""
        self._environment_required = bool(required)
        self._update_start_enabled()

    def set_environment_ready(self, ready: bool):
        """同步当前端口与协议的初始化结果，本方法不打开设备。"""
        lost_ready = self._environment_ready and not ready
        self._environment_ready = bool(ready)
        if lost_ready:
            self.invalidate_preparation()
        self._update_start_enabled()

    @property
    def preparation_ready(self):
        return self.preparation.matches(self.port.currentData() or "", self.protocol.currentData(), self.board.currentData())

    def _update_start_enabled(self, *_):
        idle = not self._external_busy and not self.is_running
        environment = not self._environment_required or self._environment_ready
        available = bool(self.port.currentData())
        ready = self.preparation_ready
        self.upload_button.setEnabled(idle and environment and available)
        self.verify_button.setEnabled(idle and environment and available)
        self.metadata_group.setEnabled(ready)
        self.start_button.setEnabled(idle and environment and ready)
        if not environment:
            hint = "先点击顶部“初始化实验台”检查 Arduino USB 串口，再准备固件。"
        elif not available:
            hint = "未发现 USB 串口，请连接 Arduino 后刷新。"
        elif not ready:
            hint = "请选择实验模式，完成烧录或已有固件验证。端口能打开不代表有传感器数据。"
        else:
            hint = "串口数据已验证；填写样品名称后开始记录。"
        self.start_button.setToolTip(hint)
        self.upload_button.setToolTip(hint if not environment else "先备份现有 Flash，再写入所选固件并验证数据。")
        if not self.is_running and not ready:
            self.preparation_status.setText(hint)

    def invalidate_preparation(self):
        self.preparation.invalidate()
        self._preparation_display = ""
        self._update_start_enabled()
        self.preparation_changed.emit()

    def _target_changed(self, *_):
        self.invalidate_preparation()
        self.confirm.setChecked(False)

    def _check_hardware_action(self):
        if self.is_running:
            return False
        if self._external_busy:
            self._fail("请先结束当前设备操作，再准备温压实验。")
            return False
        if self._environment_required and not self._environment_ready:
            self._fail("请先点击顶部“初始化实验台”，检查所选 Arduino USB 串口。")
            return False
        if not self.port.currentData():
            self._fail("请连接 Arduino USB 串口并刷新端口。")
            return False
        if not self.confirm.isChecked():
            self._fail("请先确认本次板卡、传感器接线及其他串口工具已关闭。")
            return False
        return True

    def _confirm_upload(self):
        text = (f"端口：{self.port.currentData()}\n板卡：{self.board.currentText()}\n"
                f"固件：{self.protocol.currentText()}\n\n"
                "将先读取并保存当前完整 Flash 备份，再覆盖应用程序并验证。"
                "不修改引导程序、熔丝或 EEPROM。请确认该端口连接的是本次传感器 Nano，"
                "并在写入和校验期间保持连接。")
        return QtWidgets.QMessageBox.question(self, "确认烧录实验固件", text,
                    QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
                    QtWidgets.QMessageBox.StandardButton.Cancel) == QtWidgets.QMessageBox.StandardButton.Yes

    def start_preparation(self, *, upload=True):
        if not self._check_hardware_action():
            return
        if upload and not self._confirm_upload():
            return
        try:
            self.preparation.start(self.port.currentData(), self.protocol.currentData(),
                                   board=self.board.currentData(), upload=upload)
        except (ValueError, RuntimeError, OSError) as exc:
            self._fail(str(exc))
            return
        self._operation = "preparation"
        self._preparation_display = ""
        self.config_group.setEnabled(False)
        self.stop_button.setText("取消准备")
        self.stop_button.setEnabled(True)
        self.status.setText("正在准备实验固件；进度同时显示在启动器终端。")
        self.workflow_status.setText("② 正在准备固件 · 备份 / 写入 / 数据验证")
        self.message.emit("")
        self._update_start_enabled()
        self.busy_changed.emit(True)
        self.preparation_changed.emit()

    def _invalidate_environment(self):
        self._environment_ready = False
        self.invalidate_preparation()
        self.environment_invalidated.emit()

    def _protocol_changed(self):
        temperature = self.protocol.currentData() == "temperature_v1"
        if self.channel.text() in ("A2", "A1 / S6,S7"):
            self.channel.setText("A1 / S6,S7" if temperature else "A2")
        self.adc_plot.setVisible(temperature)
        self.display_channel.setVisible(temperature)
        self.display_channel.setCurrentIndex(0)
        self.wiring_note.setText("接线：A1 · S6 / S7，轮流采样。" if temperature else "接线：A2 · 原程序选通支路。")

    def _fail(self, message):
        self.status.setText(message)
        self.error.emit(message)

    def start_capture(self):
        if not self._check_hardware_action():
            return
        if not self.preparation_ready:
            self._fail("请先完成当前端口与实验模式的固件准备，验证通过后再开始记录。")
            return
        try:
            config = CaptureConfig(port=self.port.currentData() or "", protocol=self.protocol.currentData(),
                                   sample_name=self.sample_name.text().strip(), channel=self.channel.text().strip(),
                                   notes=self.notes.text().strip(),
                                   preparation=self.preparation.snapshot()["result"],
                                   temperature_calibration=self.calibration_mode)
            config.validate()
        except ValueError as exc:
            self._fail(str(exc))
            return
        try:
            key = self.session.start(config)
        except (ValueError, RuntimeError, OSError) as exc:
            self._invalidate_environment()
            self._fail(str(exc))
            return
        self._published_key = None
        self._operation = "capture"
        self.plateau_controls.clear_error()
        self._last_rows = -1
        self._plot_points_cache = []
        self.voltage_readout.setText("等待本次采集的首个有效电压；上一轮读数已清空。")
        for pair in self.lines.values():
            for line in pair:
                line.setData([], [])
        self._restore_plot_range()
        self.config_group.setEnabled(False)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status.setText("正在打开所选串口并建立记录：" + key)
        self._was_running = True
        if self.calibration_mode:
            self._settings_focus_timer.start(0)
        self.workflow_status.setText("③ 记录实时曲线 · 保存原始字节、电压与 ADC")
        self.message.emit("")
        self.capture_state_changed.emit(True)
        self.busy_changed.emit(True)

    def stop_capture(self):
        if self._operation == "preparation" or self.preparation.is_running:
            self.preparation.request_stop()
            self.stop_button.setEnabled(False)
            writing = self.preparation.snapshot().get("write_active", False)
            self.status.setText("已请求取消；正在完成写入和校验，请勿拔线。" if writing else "正在取消固件准备并释放串口…")
            return
        self.session.request_stop()
        self.stop_button.setEnabled(False)
        self.status.setText("正在停止串口接收、关闭文件并保存来源哈希…")

    def shutdown(self, timeout_s=2.0):
        """后台结束后才能关闭；固件写入过程中不强制退出。"""
        prepared = self.preparation.stop(timeout_s)
        captured = self.session.stop(timeout_s)
        if prepared and captured:
            self._operation = None
            self.lines.clear()
            for plot in (self.voltage_plot, self.adc_plot):
                if plot.plotItem is not None and plot.plotItem.legend is not None:
                    plot.plotItem.legend.clear()
            # MainWindow also closes child plot scenes directly after shutdown.
            # Unlink first so closing one scene cannot resize its disposed peer.
            if self.adc_plot.plotItem is not None:
                self.adc_plot.setXLink(None)
        return prepared and captured

    def _follow_changed(self, enabled):
        if enabled:
            self._fit_plot_range()

    def _fit_plot_range(self):
        # 上次拖动、滚轮或隐藏页面可能留下不包含数据的范围，显式恢复双轴。
        # X 由电压图主控，ADC 保持链接；Y 各自按可见通道缩放。
        self.voltage_plot.enableAutoRange(x=True, y=True)
        self.adc_plot.enableAutoRange(y=True)
        self.voltage_plot.plotItem.vb.updateAutoRange()
        self.adc_plot.plotItem.vb.updateAutoRange()

    def _restore_plot_range(self, *_):
        self.auto_follow.setChecked(True)
        self._fit_plot_range()

    def _plot_points(self, points):
        self._plot_points_cache = list(points)
        channels = sorted({row["channel"] for row in points})
        chosen = self.display_channel.currentData()
        readouts = []
        for channel, pair in self.lines.items():
            visible = channel in channels and chosen in ("all", channel)
            for line in pair:
                line.setVisible(visible)
        for i, channel in enumerate(channels):
            if channel not in self.lines:
                pen = pg.mkPen(COLORS[i % len(COLORS)], width=1.5)
                self.lines[channel] = (self.voltage_plot.plot(pen=pen, name=channel, symbol="o", symbolSize=3,
                                                            symbolBrush=COLORS[i % len(COLORS)], symbolPen=None, connect="finite"),
                                       self.adc_plot.plot(pen=pen, name=channel, connect="finite"))
            for line in self.lines[channel]:
                line.setVisible(chosen in ("all", channel))
            selected = [row for row in points if row["channel"] == channel]
            times, voltage, adc = [], [], []
            previous = None
            for row in selected:
                if previous and (row["time_segment"] != previous["time_segment"] or row["line_number"] > previous["line_number"] + 1):
                    times.append(row["host_elapsed_s"])
                    voltage.append(float("nan"))
                    adc.append(float("nan"))
                times.append(row["host_elapsed_s"])
                voltage.append(row["voltage_v"])
                adc.append(row.get("delta_voltage_v", float("nan")) if self.calibration_mode
                           else float("nan") if row["adc"] == "" else row["adc"])
                previous = row
            self.lines[channel][0].setData(times, voltage)
            self.lines[channel][1].setData(times, adc)
            latest = selected[-1]
            label = {"S6": "CH1 / S6", "S7": "CH2 / S7"}.get(channel, channel)
            text = f"{label}：{latest['voltage_v']:.4f} V"
            if self.calibration_mode and "delta_voltage_v" in latest:
                text += f"　ΔV {latest['delta_voltage_v']:+.4f} V"
                rate = latest.get("voltage_rate_v_per_min")
                text += f"　变化速率 {rate:+.5f} V/min" if rate is not None else "　变化速率：等待至少5秒数据"
            readouts.append(text)
        if readouts:
            elapsed = max(row["host_elapsed_s"] for row in points)
            self.voltage_readout.setText("　 |　 ".join(readouts) + f"　·　最后样本 {elapsed:.2f} s（未标定）")
        elif not self._operation:
            self.voltage_readout.setText("尚无电压读数；有效数据到达后显示最新值。")
        if self.auto_follow.isChecked():
            self._fit_plot_range()

    def _show_recording_settings(self):
        parent = self.settings_widget.parentWidget()
        while parent is not None:
            if isinstance(parent, QtWidgets.QScrollArea):
                if self.calibration_mode and self._operation == "capture":
                    top = self.plateau_controls.mapTo(parent.widget(), QtCore.QPoint(0, 0)).y()
                    parent.verticalScrollBar().setValue(top)
                else:
                    parent.ensureWidgetVisible(self.start_button, 0, 12)
                break
            parent = parent.parentWidget()

    def _poll(self):
        snap = self.session.snapshot()
        self.plateau_controls.update_state(snap.get("plateau",{}), self.calibration_mode and
                                          self._operation == "capture" and snap.get("status")=="running"
                                          and bool(snap.get("rows")) and self.stop_button.isEnabled(), snap.get("plateau_pending",False))
        prepared = self.preparation.snapshot()
        if self._operation == "preparation":
            message = prepared.get("message", "正在准备固件…")
            self.preparation_status.setText(message)
            self.status.setText(message)
            self.event_text.setPlainText("\n".join(prepared.get("log", [])))
            if prepared.get("running"):
                return
            self._operation = None
            self.config_group.setEnabled(not self._external_busy)
            self.stop_button.setText("停止并保存")
            self.stop_button.setEnabled(False)
            if prepared.get("status") == "ready" and self.preparation_ready:
                self._preparation_display = message or "固件数据验证通过，可以开始记录。"
                self.workflow_status.setText("② 固件数据已验证 → ③ 填写样品并开始记录")
                self.preparation_status.setText(self._preparation_display)
                self.message.emit("")
                self._settings_focus_timer.start(0)
            else:
                self.workflow_status.setText("② 固件准备未完成 · 检查下方日志后重试")
                if prepared.get("status") == "failed":
                    self.error.emit(message)
            self._update_start_enabled()
            self.preparation_changed.emit()
            self.busy_changed.emit(False)
            return
        snapshot = self.session.snapshot()
        if snapshot["rows"] != self._last_rows:
            self._last_rows = snapshot["rows"]
            self._plot_points(snapshot["points"])
        if not snapshot["run_key"]:
            return
        if self._operation != "capture" and self._published_key == snapshot["run_key"]:
            return
        self.event_text.setPlainText("\n".join(f"{row.get('host_elapsed_s', 0):.3f}s · {row['kind']} · {row['text']}" for row in snapshot["events"]))
        running = snapshot["running"]
        labels = {"idle": "未开始", "initializing": "正在打开串口", "running": "正在接收数据" if snapshot["rows"] else "等待首个有效样本",
                  "saving": "正在封存", "complete": "已停止并保存", "failed_or_incomplete": "采集失败或不完整，证据已保留"}
        text = f"{labels.get(snapshot['status'], snapshot['status'])} · {snapshot['rows']} 行样本 · {snapshot['raw_bytes']} 原始字节\n{snapshot['run_key']}"
        if snapshot.get("diagnostic"):
            text += "\n" + snapshot["diagnostic"]
        if snapshot["errors"]:
            text += "\n" + "；".join(snapshot["errors"])
        self.status.setText(text)
        finished_run = (not running and snapshot["status"] in ("complete", "failed_or_incomplete")
                        and self._published_key != snapshot["run_key"])
        if finished_run and (snapshot["status"] == "failed_or_incomplete" or snapshot["errors"]):
            self._invalidate_environment()
        if self._was_running and not running:
            self._was_running = False
            self._operation = None
            self.config_group.setEnabled(not self._external_busy)
            self._update_start_enabled()
            self.stop_button.setEnabled(False)
            self.capture_state_changed.emit(False)
            self.busy_changed.emit(False)
            self.workflow_status.setText("③ 本次记录已封存 · 可在研究分析中查看")
        if finished_run:
            self._published_key = snapshot["run_key"]
            self.run_completed.emit(snapshot["run_key"])
            if snapshot["errors"]:
                self.error.emit("；".join(snapshot["errors"]))

    def closeEvent(self, event):
        if not self.shutdown():
            self._fail("设备操作尚未结束；写入期间请保持连接，等待结束后关闭。")
            event.ignore()
            return
        self.timer.stop()
        if self.adc_plot.plotItem is not None:
            self.adc_plot.setXLink(None)
        close_plot_widgets(self)
        event.accept()
