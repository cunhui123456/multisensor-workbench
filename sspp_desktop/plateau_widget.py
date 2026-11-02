"""标定区间控件；仅发出意图，不持有串口或写文件。"""
from PySide6 import QtCore, QtWidgets

LABELS = {'insufficient_reference':'参考记录不足，无法判稳', 'within_limits':'已记录读数满足门槛',
          'outside_limits':'超出当前门槛', 'limit_not_set':'未设置电压门槛', 'insufficient_voltage':'电压窗口不足／缺口／复位'}


class PlateauWidget(QtWidgets.QGroupBox):
    requested = QtCore.Signal(str, dict)

    def __init__(self):
        super().__init__('平台标定区间 · 连续采集不中断')
        self._local_error = ''
        self._backend_error = ''
        form = QtWidgets.QFormLayout(self)
        self.target = QtWidgets.QDoubleSpinBox();self.target.setRange(-20,100);self.target.setValue(50);self.target.setSuffix(' ℃')
        form.addRow('平台目标',self.target)
        self.source = QtWidgets.QLineEdit();self.source.setPlaceholderText('参考温度计名称／编号')
        form.addRow('参考仪器',self.source)
        self.window = QtWidgets.QSpinBox();self.window.setRange(10,600);self.window.setValue(30);self.window.setSuffix(' s')
        form.addRow('建议区间／判稳窗',self.window)
        self.range_limit = QtWidgets.QDoubleSpinBox();self.range_limit.setDecimals(3);self.range_limit.setRange(.001,10);self.range_limit.setValue(.2)
        form.addRow('温度极差上限 ℃',self.range_limit)
        self.rate_limit = QtWidgets.QDoubleSpinBox();self.rate_limit.setDecimals(3);self.rate_limit.setRange(.001,10);self.rate_limit.setValue(.1)
        form.addRow('温度趋势上限 ℃/min',self.rate_limit)
        self.voltage_limit = QtWidgets.QLineEdit();self.voltage_limit.setPlaceholderText('可留空')
        self.voltage_unit = QtWidgets.QComboBox()
        self.voltage_unit.addItem('V/min', 1.0)
        self.voltage_unit.addItem('mV/min', .001)
        voltage_row = QtWidgets.QHBoxLayout()
        voltage_row.setContentsMargins(0,0,0,0)
        voltage_row.addWidget(self.voltage_limit,1);voltage_row.addWidget(self.voltage_unit)
        form.addRow('电压漂移上限',voltage_row)
        self.voltage_hint = QtWidgets.QLabel()
        self.voltage_hint.setWordWrap(True)
        form.addRow(self.voltage_hint)
        self.voltage_limit.textChanged.connect(self._update_voltage_hint)
        self.voltage_unit.currentIndexChanged.connect(self._update_voltage_hint)
        self._update_voltage_hint()
        self.begin_button = QtWidgets.QPushButton('开始标定区间')
        self.begin_button.clicked.connect(self.begin)
        form.addRow(self.begin_button)
        self.feedback = QtWidgets.QLabel()
        self.feedback.setWordWrap(True)
        self.feedback.setStyleSheet('color: #b42318;')
        self.feedback.hide()
        form.addRow(self.feedback)
        self.reference = QtWidgets.QLineEdit();self.reference.setPlaceholderText('此刻参考仪器实测温度 ℃')
        form.addRow('实测参考温度',self.reference)
        self.reference_button = QtWidgets.QPushButton('记录此刻参考温度')
        self.reference_button.clicked.connect(self.add_reference)
        form.addRow(self.reference_button)
        self.end_button = QtWidgets.QPushButton('结束区间并保存标定点')
        self.end_button.clicked.connect(self.finish)
        form.addRow(self.end_button)
        self.status = QtWidgets.QLabel('先开始连续采集。平台稳定后开始区间；开始、中间、结束多次记录参考温度。')
        self.status.setWordWrap(True);form.addRow(self.status)
        note = QtWidgets.QLabel('默认30秒、0.2 ℃、0.1 ℃/min均为可调整试用值。判稳至少需3次手工参考读数，覆盖90%窗口、边界余量10%、相邻间隔≤60%窗长；单次读数不判稳，离散读数不代表连续温度稳定。到时只提醒，不自动保存。')
        note.setWordWrap(True);form.addRow(note)
        self.table = QtWidgets.QTableWidget(0,5)
        self.table.setHorizontalHeaderLabels(['点','参考均温 ℃','S6 均值±SD V','S7 均值±SD V','状态'])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMaximumHeight(110);self.table.setMinimumHeight(90)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        form.addRow(self.table)
        self.update_state({},False,False)

    def begin(self):
        try:
            voltage = self._voltage_value()
            from sspp_experiments.temperature_pressure.plateau import validate_settings
            settings = validate_settings({'window_s':self.window.value(),'temperature_range_c':self.range_limit.value(),
                                          'temperature_rate_c_per_min':self.rate_limit.value(),'voltage_rate_limit_v_per_min':voltage})
            source=self.source.text().strip()
            if not source or len(source)>160:
                raise ValueError('请填写参考仪器名称（最多160字）')
            self.clear_error()
            self.requested.emit('begin',{'target_temperature_c':self.target.value(),'reference_source':source,'settings':settings})
        except ValueError as exc:
            self.show_error(str(exc))

    def _voltage_value(self):
        text=self.voltage_limit.text().strip()
        if not text:
            return None
        try:
            return float(text)*self.voltage_unit.currentData()
        except ValueError as exc:
            raise ValueError('电压漂移上限须填写数值或留空；单位在右侧选择。') from exc

    def _update_voltage_hint(self,*_):
        unit=self.voltage_unit.currentText()
        bounds='0.000001–10 V/min' if unit=='V/min' else '0.001–10000 mV/min'
        try:
            value=self._voltage_value()
            import math
            actual=f'当前等于 {value:g} V/min。' if value is not None and math.isfinite(value) else ''
        except ValueError:
            actual=''
        self.voltage_hint.setText(f'{actual}允许 {bounds}；留空表示不设置电压门槛。')

    def show_error(self,message):
        self._local_error=str(message)
        self.status.setText(self._local_error)
        self._update_feedback()

    def clear_error(self):
        self._local_error=''
        self._backend_error=''
        self._update_feedback()

    def _update_feedback(self):
        message=self._local_error or self._backend_error
        self.feedback.setText(message)
        self.feedback.setVisible(bool(message))

    def finish(self):
        self.clear_error()
        self.requested.emit('end',{})

    def add_reference(self):
        try:
            from sspp_experiments.temperature_pressure.plateau import number
            temperature = number(float(self.reference.text()),'参考温度',-20,100)
            self.clear_error()
            self.requested.emit('reference',{'temperature_c':temperature})
            self.reference.clear()
        except ValueError:
            self.show_error('请填写有限的实测参考温度（-20至100 ℃）')

    def update_state(self, data, running, pending):
        active = data.get('active')
        self._backend_error=data.get('last_error') or ''
        self._update_feedback()
        for w in (self.target,self.source,self.window,self.range_limit,self.rate_limit,self.voltage_limit,self.voltage_unit):
            w.setEnabled(not active and not pending)
        self.begin_button.setEnabled(running and not active and not pending)
        self.reference.setEnabled(running and bool(active) and not pending)
        self.reference_button.setEnabled(running and bool(active) and not pending)
        self.end_button.setEnabled(running and bool(active) and not pending)
        if pending:
            self.status.setText('正在后台处理标定操作…')
        else:
            texts = []
            if active:
                elapsed = active['elapsed_s']
                texts.append(f"区间 {active['id']} · {elapsed:.1f} s · {active['reference_count']}次参考读数" +
                             ('；已到建议时长，仍需确认稳定' if elapsed >= self.window.value() else ''))
            ref = data.get('reference',{})
            texts.append(LABELS.get(ref.get('status'),'先开始采集并记录参考温度'))
            if ref.get('range_c') is not None:
                texts.append(f"参考极差 {ref['range_c']:.3f} ℃；趋势 {ref['rate_c_per_min']:.3f} ℃/min" if ref.get('rate_c_per_min') is not None else '参考时间跨度不足')
            for ch,v in data.get('channels',{}).items():
                rate = v.get('rate_v_per_min')
                texts.append(f"{ch}：{rate:+.5f} V/min，{LABELS.get(v['status'],v['status'])}" if rate is not None else f'{ch}：等待电压时间跨度')
            if data.get('last_error'):
                texts.append(data['last_error'])
            self.status.setText('\n'.join(texts))
        points = data.get('points',[])
        signature = repr(points)
        if signature == getattr(self, '_points_signature', None):
            return
        self._points_signature = signature
        self.table.setRowCount(len(points))
        for i,p in enumerate(points):
            voltages = []
            for ch in ('S6','S7'):
                v=p['channels'].get(ch)
                voltages.append('无数据' if not v else f"{v['mean_v']:.5f} ± {v['sample_sd_v']:.5f}" if v['sample_sd_v'] is not None else f"{v['mean_v']:.5f}（仅1样本）")
            state = '中断，需复核' if p['status']=='interrupted' else LABELS.get(p['reference_status'],p['reference_status'])
            if p['quality_flags']:state += '；质量待复核'
            for j,value in enumerate([str(p['id']),'' if p['reference_mean_c'] is None else f"{p['reference_mean_c']:.3f}",*voltages,state]):
                self.table.setItem(i,j,QtWidgets.QTableWidgetItem(value))
