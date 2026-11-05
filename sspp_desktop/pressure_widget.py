"""压力传感器区间标定控件：人工力参考与电压分别保留。"""
import math

from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

from .plateau_widget import LABELS
from .plots import COLORS, make_plot


class PressureCalibrationWidget(QtWidgets.QGroupBox):
    requested = QtCore.Signal(str, dict)

    def __init__(self):
        super().__init__('压力标定区间 · 参考法向接触力')
        self._local_error = ''
        self._backend_error = ''
        self._channels = ()
        form = QtWidgets.QFormLayout(self)
        self.pressure_channel = QtWidgets.QComboBox()
        form.addRow('力敏通道', self.pressure_channel)
        self.target = QtWidgets.QDoubleSpinBox()
        self.target.setDecimals(4); self.target.setRange(0, 1e6); self.target.setSuffix(' N')
        form.addRow('目标载荷（计划值）', self.target)
        self.source = QtWidgets.QLineEdit()
        self.source.setMaxLength(160); self.source.setPlaceholderText('独立测力仪名称／编号')
        form.addRow('参考仪器', self.source)
        self.direction = QtWidgets.QComboBox()
        for label, value in [('加载', 'loading'), ('卸载', 'unloading'), ('保持', 'hold')]:
            self.direction.addItem(label, value)
        form.addRow('加载方向', self.direction)
        self.cycle = QtWidgets.QSpinBox(); self.cycle.setRange(1, 10000)
        form.addRow('循环编号', self.cycle)
        self.area = QtWidgets.QLineEdit()
        self.area.setPlaceholderText('仅填写已确认的接触面积；未知留空')
        form.addRow('接触面积 mm²', self.area)
        self.area_hint = QtWidgets.QLabel('默认标定 F（N）；有可靠接触面积时同时记录 p = 1000 F/A（kPa）。面积不是传感器外形面积。')
        self.area_hint.setWordWrap(True); form.addRow(self.area_hint)
        self.window = QtWidgets.QSpinBox(); self.window.setRange(10, 600); self.window.setValue(30); self.window.setSuffix(' s')
        form.addRow('建议区间／判稳窗', self.window)
        self.range_limit = QtWidgets.QDoubleSpinBox()
        self.range_limit.setDecimals(4); self.range_limit.setRange(.0001, 1e6); self.range_limit.setValue(.1)
        form.addRow('力极差上限 N', self.range_limit)
        self.rate_limit = QtWidgets.QDoubleSpinBox()
        self.rate_limit.setDecimals(4); self.rate_limit.setRange(.0001, 1e6); self.rate_limit.setValue(.1)
        form.addRow('力趋势上限 N/min', self.rate_limit)
        self.voltage_limit = QtWidgets.QLineEdit(); self.voltage_limit.setPlaceholderText('可留空')
        self.voltage_unit = QtWidgets.QComboBox()
        self.voltage_unit.addItem('V/min', 1.0); self.voltage_unit.addItem('mV/min', .001)
        voltage_row = QtWidgets.QHBoxLayout()
        voltage_row.addWidget(self.voltage_limit, 1); voltage_row.addWidget(self.voltage_unit)
        form.addRow('电压漂移上限', voltage_row)
        self.begin_button = QtWidgets.QPushButton('开始标定区间'); self.begin_button.clicked.connect(self.begin)
        form.addRow(self.begin_button)
        self.feedback = QtWidgets.QLabel(); self.feedback.setWordWrap(True)
        self.feedback.setStyleSheet('color: #b42318;'); self.feedback.hide(); form.addRow(self.feedback)
        self.reference = QtWidgets.QLineEdit(); self.reference.setPlaceholderText('此刻独立测力仪实测值 N')
        form.addRow('实测参考力 N', self.reference)
        self.reference_button = QtWidgets.QPushButton('记录此刻参考力'); self.reference_button.clicked.connect(self.add_reference)
        form.addRow(self.reference_button)
        self.end_button = QtWidgets.QPushButton('结束区间并保存标定点'); self.end_button.clicked.connect(self.finish)
        form.addRow(self.end_button)
        note = QtWidgets.QLabel('固定装夹、按压位置和方向，按零载荷→逐级加载→逐级卸载记录。每段在开始、中间、结束录入实测参考力；默认判稳门槛仅为可调整试用值。到时不自动保存，不控制丝杠。')
        note.setWordWrap(True); form.addRow(note)
        self.status = QtWidgets.QLabel('先开始连续采集，再记录力参考与标定区间。')
        self.status.setWordWrap(True); form.addRow(self.status)
        self.table = QtWidgets.QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['点／循环', '方向／通道', '参考均力 N', '平均压强 kPa', '电压均值 ± SD V', '时长 s', '质量'])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(90)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.results_view = QtWidgets.QTabWidget()
        self.results_view.addTab(self.table, '标定点与质量')
        response_page = QtWidgets.QWidget()
        response_layout = QtWidgets.QVBoxLayout(response_page)
        self.response_plot = make_plot('压力传感器标定响应 · 实测区间均值', '参考法向接触力（N）', '电压（V）')
        self.response_plot.setMinimumHeight(180)
        self.response_plot.addLegend(offset=(12, 12)); response_layout.addWidget(self.response_plot, 1)
        self.fit_summary = QtWidgets.QPlainTextEdit('候选线性拟合尚无足够有效点。拟合残差不是独立测量精度，不自动激活换算。')
        self.fit_summary.setReadOnly(True); self.fit_summary.setMaximumHeight(90)
        response_layout.addWidget(self.fit_summary)
        self.results_view.addTab(response_page, '响应图与候选拟合')
        form.addRow(self.results_view)
        self.set_channels(['A2'])
        self.update_state({}, False, False)

    def set_channels(self, channels):
        channels = tuple(channels)
        if channels == self._channels:
            return
        self._channels = channels
        self.pressure_channel.clear()
        self.pressure_channel.addItem('请选择实际力敏通道', None)
        for channel in channels:
            self.pressure_channel.addItem(channel, channel)

    def selected_channel(self):
        channel = self.pressure_channel.currentData()
        if channel not in self._channels:
            raise ValueError('请选择实际连接压力传感器的力敏通道。')
        return channel

    @staticmethod
    def _number(text, label, low, high, *, optional=False):
        if optional and not text.strip():
            return None
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError(f'{label}须为有效数值。') from exc
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{label}须在 {low:g}–{high:g} 范围内。')
        return value

    def begin(self):
        try:
            from sspp_experiments.temperature_pressure.pressure import validate_settings
            source = self.source.text().strip()
            if not source:
                raise ValueError('请填写独立参考测力仪器的名称／编号。')
            channel = self.selected_channel()
            area = self._number(self.area.text(), '接触面积 mm²', 1e-9, 1e9, optional=True)
            voltage = self._number(self.voltage_limit.text(), '电压漂移上限', 0, 1e9, optional=True)
            if voltage is not None:
                voltage *= self.voltage_unit.currentData()
            settings = validate_settings({'window_s': self.window.value(), 'force_range_n': self.range_limit.value(),
                                          'force_rate_n_per_min': self.rate_limit.value(), 'voltage_rate_limit_v_per_min': voltage})
            self.clear_error()
            self.requested.emit('begin', {'target_force_n': self.target.value(), 'reference_source': source,
                                         'pressure_channel': channel, 'direction': self.direction.currentData(),
                                         'cycle': self.cycle.value(), 'contact_area_mm2': area, 'settings': settings})
        except ValueError as exc:
            self.show_error(str(exc))

    def add_reference(self):
        try:
            value = self._number(self.reference.text(), '实测参考力 N', 0, 1e6)
            self.clear_error()
            self.requested.emit('reference', {'force_n': value})
            self.reference.clear()
        except ValueError as exc:
            self.show_error(str(exc))

    def finish(self):
        self.clear_error()
        self.requested.emit('end', {})

    def show_error(self, message):
        self._local_error = str(message)
        self._update_feedback()

    def clear_error(self):
        self._local_error = self._backend_error = ''
        self._update_feedback()

    def _update_feedback(self):
        message = self._local_error or self._backend_error
        self.feedback.setText(message); self.feedback.setVisible(bool(message))

    def update_state(self, data, running, pending):
        active = data.get('active')
        self._backend_error = data.get('last_error') or ''
        self._update_feedback()
        for widget in (self.pressure_channel, self.target, self.source, self.direction, self.cycle, self.area,
                       self.window, self.range_limit, self.rate_limit, self.voltage_limit, self.voltage_unit):
            widget.setEnabled(not active and not pending)
        self.begin_button.setEnabled(running and not active and not pending)
        for widget in (self.reference, self.reference_button, self.end_button):
            widget.setEnabled(running and bool(active) and not pending)
        if pending:
            self.status.setText('正在后台处理标定操作…')
        else:
            texts = []
            if active:
                texts.append(f"区间 {active['id']} · {active['elapsed_s']:.1f} s · {active['reference_count']} 次参考读数")
            reference = data.get('reference', {})
            texts.append(LABELS.get(reference.get('status'), '在区间开始、中间、结束记录实测参考力。'))
            if reference.get('range_n') is not None:
                texts.append(f"参考力极差 {reference['range_n']:.4g} N")
            if reference.get('rate_n_per_min') is not None:
                texts.append(f"参考力趋势 {reference['rate_n_per_min']:+.4g} N/min")
            for channel, value in data.get('channels', {}).items():
                rate = value.get('rate_v_per_min')
                if rate is not None:
                    texts.append(f"{channel}：{rate:+.5f} V/min，{LABELS.get(value.get('status'), value.get('status', ''))}")
            self.status.setText('\n'.join(texts))
        points, fits = data.get('points', []), data.get('candidate_fits', {})
        signature = repr((points, fits))
        if signature == getattr(self, '_points_signature', None):
            return
        self._points_signature = signature
        self.table.setRowCount(len(points))
        groups = {}
        directions = {'loading': '加载', 'unloading': '卸载', 'hold': '保持'}
        for index, point in enumerate(points):
            channel = point.get('pressure_channel', '')
            stats = point.get('channels', {}).get(channel)
            force, pressure = point.get('reference_mean_n'), point.get('reference_mean_kpa')
            voltage = '无数据'
            if stats:
                sd = stats.get('sample_sd_v')
                voltage = f"{stats['mean_v']:.5f}" + (f' ± {sd:.5f}' if sd is not None else '（仅1样本）')
            state = '中断' if point.get('status') == 'interrupted' else LABELS.get(point.get('reference_status'), '待复核')
            if point.get('quality_flags'):
                state += '；质量待复核'
            values = [f"{point['id']} / {point.get('cycle', '')}", f"{directions.get(point.get('direction'), '')} / {channel}",
                      '' if force is None else f'{force:.4g}', '未定义面积' if pressure is None else f'{pressure:.4g}',
                      voltage, f"{point.get('duration_s', 0):.1f}", state]
            for col, value in enumerate(values):
                self.table.setItem(index, col, QtWidgets.QTableWidgetItem(value))
            if force is not None and stats:
                key = (channel, point.get('direction'), point.get('cycle'))
                groups.setdefault(key, []).append((force, stats['mean_v']))
        self.response_plot.clear()
        if self.response_plot.plotItem.legend is not None:
            self.response_plot.plotItem.legend.clear()
        for index, (key, values) in enumerate(groups.items()):
            color = COLORS[index % len(COLORS)]
            label = f'{key[0]} · {directions.get(key[1], key[1])} · 循环 {key[2]}'
            self.response_plot.plot([v[0] for v in values], [v[1] for v in values], pen=None, symbol='o',
                                    symbolSize=7, symbolPen=color, symbolBrush=color, name=label)
        summaries = []
        for fit in fits.values():
            label = f"{fit.get('channel', '')} / {directions.get(fit.get('direction'), '')} / 循环 {fit.get('cycle', '')}"
            if fit.get('slope_n_per_v') is not None:
                summaries.append(f"{label}：F(N) = {fit['slope_n_per_v']:.6g} × U(V) {fit['intercept_n']:+.6g}；"
                                 f"{fit['point_count']} 点，样本内 RMSE {fit['rmse_n']:.4g} N；"
                                 f"范围 {fit['voltage_min_v']:.4g}–{fit['voltage_max_v']:.4g} V。")
        self.fit_summary.setPlainText(('\n'.join(summaries) if summaries else '候选线性拟合尚无足够有效点。') +
                                 '\n按通道、加载方向、循环、面积及参考仪器分别拟合；仅为待验证候选，不自动换算或外推。散点保留待复核点，拟合只使用通过质量检查的区间。')
