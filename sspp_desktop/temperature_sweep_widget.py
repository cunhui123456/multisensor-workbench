"""SSPP 温度影响扫频：双传感器视图和参数，设备操作由主窗统一管理。"""
from __future__ import annotations

import math

from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

from sspp_experiments.temperature_pressure import list_serial_ports
from .plots import COLORS, make_plot, numbers
from .qt_cleanup import close_plot_widgets
from .serial_ports import arduino_serial_ports


class TemperatureSweepWidget(QtWidgets.QWidget):
    """构造、设置和刷新仅列串口名称；不初始化、烧录或打开设备。"""

    port_changed = QtCore.Signal(str)
    configuration_changed = QtCore.Signal()
    prepare_temperature_requested = QtCore.Signal()

    def __init__(self, root=None, parent=None):
        super().__init__(parent)
        self.root = root
        self._external_busy = False
        self._snapshot = {}
        self.sweep_items = {}
        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel("SSPP 温度影响 · 扫频与温度传感器联合记录")
        title.setStyleSheet("font-size: 18px; font-weight: 600; color: #087f85")
        layout.addWidget(title)
        self.status = QtWidgets.QLabel("等待联合采集；每个温度平台分别记录一次扫频。")
        self.status.setObjectName("viewBanner")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._build_settings()
        self._settings_scroll = QtWidgets.QScrollArea()
        self._settings_scroll.setWidgetResizable(True)
        self._settings_scroll.setMaximumHeight(260)
        self._settings_scroll.setWidget(self.settings_widget)
        layout.addWidget(self._settings_scroll)
        self.readout = QtWidgets.QLabel("温度传感器尚未标定：以电压（V）记录。")
        self.readout.setWordWrap(True)
        layout.addWidget(self.readout)
        plot_tools = QtWidgets.QHBoxLayout()
        self.auto_follow = QtWidgets.QCheckBox("自动跟随实时数据")
        self.auto_follow.setChecked(True)
        self.auto_follow.toggled.connect(lambda enabled: self._fit_plots() if enabled else None)
        plot_tools.addWidget(self.auto_follow)
        self.voltage_view = QtWidgets.QComboBox()
        self.voltage_view.addItem("只看所选温度通道", "selected")
        self.voltage_view.addItem("两路电压", "both")
        self.voltage_view.setToolTip("默认放大所选温度通道的小幅变化；两路原始数据始终保存。")
        self.voltage_view.currentIndexChanged.connect(lambda *_: self.update_snapshot(self._snapshot))
        plot_tools.addWidget(self.voltage_view)
        plot_tools.addStretch()
        self.full_range_button = QtWidgets.QPushButton("恢复全图")
        self.full_range_button.clicked.connect(self._restore_plot_range)
        plot_tools.addWidget(self.full_range_button)
        layout.addLayout(plot_tools)
        self.sweep_plot = make_plot("SSPP · 各轮扫频响应", "载频（GHz）", "幅度（dBFS 估计）")
        self.sweep_legend = self.sweep_plot.addLegend(offset=(12, 12))
        self.voltage_plot = make_plot("温度传感器 · 原始电压", "主机接收经过时间（s）", "电压（V）")
        self.voltage_plot.addLegend(offset=(12, 12))
        self.voltage_lines = {
            channel: self.voltage_plot.plot(name=label, pen=pg.mkPen(COLORS[i], width=1.5), connect="finite")
            for i, (channel, label) in enumerate((("S6", "CH1 · S6"), ("S7", "CH2 · S7")))
        }
        self.temperature_plot = make_plot("所选通道 · 标定换算温度", "主机接收经过时间（s）", "温度（℃）")
        self.temperature_plot.setXLink(self.voltage_plot)
        self.temperature_line = self.temperature_plot.plot(pen=pg.mkPen(COLORS[2], width=1.5), connect="finite")
        self.temperature_plot.hide()
        for plot in (self.sweep_plot, self.voltage_plot, self.temperature_plot):
            plot.plotItem.vb.sigRangeChangedManually.connect(lambda *_: self.auto_follow.setChecked(False))
        plot_row = QtWidgets.QHBoxLayout()
        plot_row.addWidget(self.sweep_plot, 1)
        plot_row.addWidget(self.voltage_plot, 1)
        layout.addLayout(plot_row, 1)
        layout.addWidget(self.temperature_plot, 1)
        hint = QtWidgets.QLabel(
            "保持样品、SSPP 与线缆位置固定，温度稳定后再开始。两路数据按主机时间关联，"
            "并非硬件同步；原始电压始终保存。SSPP 为相对数字幅度，未经 S21 标定。")
        hint.setWordWrap(True)
        self.hint=hint
        layout.addWidget(hint)
        self.round_table=QtWidgets.QTableWidget(0,6)
        self.round_table.setHorizontalHeaderLabels(['扫频轮次','区间时长 s','平均电压 V','电压跨度 V','换算温度 ℃','轮次质量'])
        self.round_table.setMaximumHeight(130)
        self.round_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.round_table)
        self.ranking_label=QtWidgets.QLabel('连续变温实验结束后，输出频点敏感性候选排名；未标定时单位为dB/V。每轮平均值是代表标签，轮内并非恒温。')
        self.ranking_label.setWordWrap(True);layout.addWidget(self.ranking_label)
        self.sensitivity_plot=make_plot('候选敏感频段 · 斜率绝对值','载频（GHz）','|幅度斜率|（dB/V）')
        self.sensitivity_line=self.sensitivity_plot.plot(pen=pg.mkPen(COLORS[3],width=1.5),symbol='o')
        self.sensitivity_plot.setMaximumHeight(170);self.sensitivity_plot.hide()
        layout.addWidget(self.sensitivity_plot)
        self.refresh_ports()
        self._experiment_mode_changed()

    @staticmethod
    def _form(group):
        form = QtWidgets.QFormLayout(group)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        return form

    def _build_settings(self):
        self.settings_widget = QtWidgets.QWidget()
        self.settings_widget.setObjectName("temperatureSweepSettings")
        layout = QtWidgets.QVBoxLayout(self.settings_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        defaults = QtWidgets.QLabel("默认扫频：5.0–5.3 GHz，30 个等间隔频点，RX 45 dB，首个单音 20000 Hz。参数可在扫频设置与高级参数中调整。")
        defaults.setWordWrap(True)
        layout.addWidget(defaults)
        self.experiment_mode=QtWidgets.QComboBox()
        self.experiment_mode.addItem("手填温度 · 每个平台一次实验", "manual")
        self.experiment_mode.addItem("连续变温 · 循环扫频与电压", "continuous")
        layout.addWidget(self.experiment_mode)
        self.config_group = QtWidgets.QGroupBox("本次温度平台与传感器")
        form = self._form(self.config_group)
        self.settings_form=form
        self.condition_label = QtWidgets.QLineEdit("温度平台 1")
        self.condition_label.setMaxLength(160)
        self.condition_label.setPlaceholderText("例如 室温、加热平台 1、降温平台 2")
        form.addRow("温度条件名称 *", self.condition_label)
        self.reference_temperature = QtWidgets.QLineEdit()
        self.reference_temperature.setPlaceholderText("选填：独立参考温度计实测值")
        self.reference_temperature.setToolTip("只保存人工参考值，不作为传感器标定或自动温度读数。")
        form.addRow("参考温度（℃）", self.reference_temperature)
        self.port = QtWidgets.QComboBox()
        self.port.setMinimumContentsLength(10)
        self.port.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.refresh_button = QtWidgets.QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh_ports)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.port, 1)
        row.addWidget(self.refresh_button)
        form.addRow("温度 USB 串口", row)
        self.port_row=row
        self.channel = QtWidgets.QComboBox()
        self.channel.addItem("CH1 · S6", "S6")
        self.channel.addItem("CH2 · S7", "S7")
        self.channel.setToolTip("选择温度传感器实际接入的支路。两通道原始电压均会保存。")
        form.addRow("温度传感器通道", self.channel)
        note = QtWidgets.QLabel("使用温度 v1 固件（9600 波特，S6/S7）。请先在温压采集页验证已有固件。")
        note.setWordWrap(True)
        form.addRow(note)
        self.sensor_note=note
        self.prepare_button = QtWidgets.QPushButton("前往温度固件准备")
        self.prepare_button.clicked.connect(lambda *_: self.prepare_temperature_requested.emit())
        form.addRow(self.prepare_button)
        layout.addWidget(self.config_group)
        self.calibration_enabled = QtWidgets.QCheckBox("使用已完成标定的温度换算")
        self.calibration_enabled.setToolTip("未标定时保持关闭。启用后仍保留全部原始电压，超出标定范围不外推。")
        layout.addWidget(self.calibration_enabled)
        self.calibration_group = QtWidgets.QGroupBox("线性标定 · T = k × V + b")
        calibration_form = self._form(self.calibration_group)
        self.calibration_id = QtWidgets.QLineEdit()
        self.calibration_id.setMaxLength(160)
        self.calibration_id.setPlaceholderText("必填：可追溯的标定记录编号")
        calibration_form.addRow("标定编号", self.calibration_id)
        self.calibration_fields = {}
        for key, label, placeholder in (
            ("slope_c_per_v", "斜率 k（℃/V）", "标定所得系数"),
            ("intercept_c", "截距 b（℃）", "标定所得系数"),
            ("voltage_min", "有效电压下限（V）", "标定覆盖的最小电压"),
            ("voltage_max", "有效电压上限（V）", "标定覆盖的最大电压"),
        ):
            field = QtWidgets.QLineEdit()
            field.setPlaceholderText(placeholder)
            self.calibration_fields[key] = field
            calibration_form.addRow(label, field)
        self.calibration_group.setEnabled(False)
        self.calibration_group.hide()
        layout.addWidget(self.calibration_group)
        layout.addStretch()
        self.experiment_mode.currentIndexChanged.connect(self._experiment_mode_changed)
        self.calibration_enabled.toggled.connect(self._calibration_toggled)
        self.port.currentIndexChanged.connect(self._port_changed)
        self.channel.currentIndexChanged.connect(lambda *_: self.configuration_changed.emit())
        for field in [self.condition_label, self.reference_temperature, self.calibration_id, *self.calibration_fields.values()]:
            field.textChanged.connect(lambda *_: self.configuration_changed.emit())
        self._experiment_mode_changed()

    @property
    def requires_serial(self):
        return self.experiment_mode.currentData()=='continuous'

    def _experiment_mode_changed(self,*_):
        continuous=self.requires_serial
        self.settings_form.setRowVisible(self.reference_temperature,not continuous)
        for field in (self.port_row,self.channel,self.sensor_note,self.prepare_button):
            self.settings_form.setRowVisible(field,continuous)
        self.calibration_enabled.setVisible(continuous)
        self.calibration_group.setVisible(continuous and self.calibration_enabled.isChecked())
        if hasattr(self,'voltage_plot'):
            self.voltage_plot.setVisible(continuous)
            self.readout.setText('等待温度传感器有效电压。' if continuous else '手填温度模式：每个温度平台独立记录，无需温度串口。')
            self.round_table.setVisible(continuous)
            self.ranking_label.setVisible(continuous)
            self.hint.setText('允许温度持续变化；每轮平均电压关联整轮曲线，同时保留轮内电压跨度。软件时间关联，未经S21标定。' if continuous else
                              '每调整一个温度，填写实测温度并单独扫频；保持样品、SSPP与线缆位置固定。无需Arduino。')
        self.configuration_changed.emit()

    def take_settings_widget(self):
        """供主窗移到全局侧栏，重复调用返回同一控件。"""
        if self._settings_scroll is not None:
            scroll, self._settings_scroll = self._settings_scroll, None
            scroll.takeWidget()
            self.layout().removeWidget(scroll)
            scroll.hide()
            scroll.deleteLater()
            self.settings_widget.setParent(None)
        return self.settings_widget

    def _calibration_toggled(self, enabled):
        self.calibration_group.setEnabled(enabled and not self._external_busy)
        self.calibration_group.setVisible(enabled and self.requires_serial)
        self.configuration_changed.emit()

    def _port_changed(self, *_):
        self.port_changed.emit(self.port.currentData() or "")
        self.configuration_changed.emit()

    def refresh_ports(self):
        if self._external_busy:
            return
        previous = self.port.currentData()
        with QtCore.QSignalBlocker(self.port):
            self.port.clear()
            for port in arduino_serial_ports(list_serial_ports()):
                self.port.addItem(port.removeprefix("/dev/cu."), port)
                self.port.setItemData(self.port.count() - 1, port, QtCore.Qt.ItemDataRole.ToolTipRole)
            index = self.port.findData(previous)
            if index >= 0:
                self.port.setCurrentIndex(index)
        if previous != self.port.currentData():
            self._port_changed()

    def set_port(self, port):
        """同步全局所选端口，不探测或打开；空值明确清除选择。"""
        with QtCore.QSignalBlocker(self.port):
            index = self.port.findData(port)
            if port and index < 0:
                self.port.addItem(str(port).removeprefix("/dev/cu."), port)
                index = self.port.count() - 1
            self.port.setCurrentIndex(index)

    def set_external_busy(self, busy):
        self._external_busy = bool(busy)
        self.experiment_mode.setEnabled(not busy)
        self.config_group.setEnabled(not busy)
        self.calibration_enabled.setEnabled(not busy)
        self.calibration_group.setEnabled(not busy and self.calibration_enabled.isChecked())

    @staticmethod
    def _number(text, name):
        try:
            value = float(text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"请填写{name}的有效数值") from exc
        if not math.isfinite(value):
            raise ValueError(f"{name}必须是有限数值")
        return value

    def config(self):
        condition = self.condition_label.text().strip()
        if not condition:
            raise ValueError("请填写本次温度条件名称")
        port = self.port.currentData() or ""
        if self.requires_serial and not port:
            raise ValueError("请选择已连接的温度 USB 串口")
        reference_text = self.reference_temperature.text().strip()
        reference = self._number(reference_text, "参考温度") if reference_text and not self.requires_serial else None
        if not self.requires_serial and reference is None:
            raise ValueError("请填写本次平台实测温度")
        calibration = None
        if self.requires_serial and self.calibration_enabled.isChecked():
            identity = self.calibration_id.text().strip()
            if not identity:
                raise ValueError("启用温度换算前请填写标定编号")
            calibration = {"id": identity, **{
                key: self._number(field.text().strip(), field.placeholderText())
                for key, field in self.calibration_fields.items()}}
            if not 0 <= calibration["voltage_min"] < calibration["voltage_max"] <= 5.1:
                raise ValueError("标定有效电压范围须满足 0 ≤ 下限 < 上限 ≤ 5.1 V")
            if calibration["slope_c_per_v"] == 0:
                raise ValueError("标定斜率不能为零")
        return {"mode":self.experiment_mode.currentData(), "protocol": "temperature_v1", "port": port, "channel": self.channel.currentData(),
                "condition_label": condition, "reference_temperature_c": reference, "calibration": calibration}

    def restore_config(self, config):
        """只恢复用户参数草稿；不改变全局初始化或固件就绪状态。"""
        config = config or {}
        self.experiment_mode.setCurrentIndex(max(0,self.experiment_mode.findData(config.get("mode","continuous"))))
        self.condition_label.setText(str(config.get("condition_label", "温度平台 1")))
        reference = config.get("reference_temperature_c")
        self.reference_temperature.setText("" if reference is None else str(reference))
        if "port" in config:
            self.set_port(config["port"])
        index = self.channel.findData(config.get("channel", "S6"))
        self.channel.setCurrentIndex(max(0, index))
        calibration = config.get("calibration") or {}
        self.calibration_id.setText(str(calibration.get("id", "")))
        for key, field in self.calibration_fields.items():
            field.setText(str(calibration[key]) if key in calibration else "")
        self.calibration_enabled.setChecked(bool(calibration))

    @staticmethod
    def _timeline(rows, value_key):
        """重启、重复 MCU 时间或串口缺口不连接成连续曲线。"""
        x, y, previous_segment = [], [], None
        for row in rows:
            segment = row.get("time_segment")
            if ((previous_segment is not None and segment != previous_segment)
                    or row.get("event") in ("mcu_reset", "duplicate_mcu_time", "display_gap")):
                x.append(row.get("host_elapsed_s")); y.append(None)
            x.append(row.get("host_elapsed_s")); y.append(row.get(value_key))
            previous_segment = segment
        return numbers(x), numbers(y)

    def clear(self):
        self.auto_follow.setChecked(True)
        self.update_snapshot({"points": [], "temperature_sweep": {"voltage_rows": []}})

    def _fit_plots(self):
        for plot in (self.sweep_plot, self.voltage_plot):
            plot.enableAutoRange(x=True, y=True)
            plot.plotItem.vb.updateAutoRange()
        self.temperature_plot.enableAutoRange(y=True)
        self.temperature_plot.plotItem.vb.updateAutoRange()

    def _restore_plot_range(self, *_):
        self.auto_follow.setChecked(True)
        self._fit_plots()

    def update_snapshot(self, snapshot):
        """显示真实快照或历史快照；不从电压自行臆测温度。"""
        self._snapshot = snapshot or {}
        job_config = (self._snapshot.get("job") or {}).get("config") or {}
        data = self._snapshot.get("temperature_sweep") or job_config.get("temperature_sweep") or {}
        rows = self._snapshot.get("temperature_points") or data.get("voltage_rows") or []
        groups = {}
        for point in self._snapshot.get("points") or []:
            groups.setdefault(point.get("repeat", 1), []).append(point)
        groups=dict(sorted(groups.items())[-32:])
        for repeat in set(self.sweep_items) - set(groups):
            self.sweep_legend.removeItem(f"第 {repeat} 轮")
            self.sweep_plot.removeItem(self.sweep_items.pop(repeat))
        for repeat, points in groups.items():
            if repeat not in self.sweep_items:
                self.sweep_items[repeat] = self.sweep_plot.plot(
                    name=f"第 {repeat} 轮", pen=pg.mkPen(COLORS[(int(repeat) - 1) % len(COLORS)], width=1.7),
                    symbol="o", symbolSize=4, connect="finite")
            self.sweep_items[repeat].setData(
                numbers([point.get("carrier_hz") for point in points]) / 1e9,
                numbers([point.get("amplitude_dB") for point in points]), connect="finite")
        selected = data.get("channel") or self.channel.currentData()
        for channel, line in self.voltage_lines.items():
            line.setVisible(self.voltage_view.currentData() == "both" or channel == selected)
            line.setData(*self._timeline([row for row in rows if row.get("channel") == channel], "voltage_v"), connect="finite")
        selected = data.get("channel") or self.channel.currentData()
        selected_rows = [row for row in rows if row.get("channel") == selected]
        calibrated = bool(data.get("calibration")) or any(row.get("temperature_c") is not None for row in selected_rows)
        self.temperature_plot.setVisible(calibrated)
        self.temperature_line.setData(*self._timeline(selected_rows, "temperature_c"), connect="finite")
        if selected_rows:
            row = selected_rows[-1]
            voltage = row.get("voltage_v")
            voltage_text = "—" if voltage is None else f"{voltage:.5f}"
            text = f"{selected}：{voltage_text} V"
            if row.get("temperature_c") is not None:
                text += f"　|　标定换算：{row['temperature_c']:.3f} ℃"
            elif calibrated:
                text += "　|　温度换算无有效值（检查标定范围与数据状态）"
            else:
                text += "　|　未标定，记录电压"
            self.readout.setText(text)
        else:
            if data.get('mode')=='manual':
                self.readout.setText(f"本次手填参考温度：{data.get('reference_temperature_c')} ℃；每个平台独立实验。")
            else:
                self.readout.setText("等待温度传感器有效数据；尚无可显示的电压或温度。")
        count = sum(len(points) for points in groups.values())
        invalid=sum(p.get("amplitude_dB") is None for points in groups.values() for p in points)
        prefix = "历史记录" if self._snapshot.get("historical") else "联合采集"
        condition = data.get("condition_label") or ""
        self.status.setText(f"{prefix}{' · ' + condition if condition else ''}　|　SSPP 有效 {count-invalid} 点／无效 {invalid} 点　|　温度电压显示 {len(rows)} 条")
        rounds=self._snapshot.get('temperature_rounds',[])
        self.round_table.setRowCount(min(len(rounds),100))
        for i,r in enumerate(rounds[-100:]):
            for j,v in enumerate([r['repeat'],r.get('duration_s'),r.get('voltage_mean_v'),r.get('voltage_span_v'),r.get('temperature_c'),
                                  '已扫完（含无效频点）' if r.get('complete') and r.get('invalid_point_count') else '完整' if r.get('complete') and not r.get('quality_flags') else '部分／待复核']):
                self.round_table.setItem(i,j,QtWidgets.QTableWidgetItem('—' if v is None else f'{v:.5f}' if isinstance(v,float) else str(v)))
        ranking=self._snapshot.get('frequency_ranking',[])
        self.sensitivity_plot.setVisible(bool(ranking))
        if ranking:
            sorted_ranking=sorted(ranking,key=lambda r:r['carrier_hz'])
            self.sensitivity_line.setData([r['carrier_hz']/1e9 for r in sorted_ranking],[abs(r['slope']) for r in sorted_ranking])
            self.sensitivity_plot.setLabel('left','|幅度斜率|（'+ranking[0]['slope_unit']+'）')
            self.ranking_label.setText('候选敏感频点（需独立验证）：'+ '；'.join(f"{r['carrier_hz']/1e9:.6f} GHz：{r['slope']:+.4g} {r['slope_unit']}，R²={r['r_squared']:.3f}" for r in ranking[:5]))
        if self.auto_follow.isChecked():
            self._fit_plots()

    def closeEvent(self, event):
        close_plot_widgets(self)
        super().closeEvent(event)
