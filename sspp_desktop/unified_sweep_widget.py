"""Optional conditions shared by all SSPP sweeps; changes never touch hardware."""
from PySide6 import QtCore, QtWidgets


class UnifiedSweepSettings(QtWidgets.QWidget):
    changed = QtCore.Signal()

    def __init__(self, temperature, liquid, parent=None):
        super().__init__(parent)
        self.temperature = temperature
        self.liquid = liquid
        self._phase = False
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.temperature_group = QtWidgets.QWidget()
        tl = QtWidgets.QVBoxLayout(self.temperature_group)
        tl.setContentsMargins(0, 0, 0, 0)
        self.temperature_mode = QtWidgets.QComboBox()
        self.temperature_mode.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.temperature_mode.setMinimumContentsLength(12)
        for name, key in [('不记录', 'none'), ('手填参考温度', 'manual'), ('Arduino 连续记录', 'continuous')]:
            self.temperature_mode.addItem(name, key)
        self.temperature_mode.setToolTip('手填：每个平台单独扫频，不需要 Arduino。连续：同步记录原始电压，循环扫频直到停止。')
        mode_form = QtWidgets.QFormLayout()
        mode_form.setContentsMargins(0, 0, 0, 0)
        mode_form.addRow('温度记录', self.temperature_mode)
        tl.addLayout(mode_form)
        self.temperature_settings = temperature.take_settings_widget()
        # The original settings remain the sole owners of calibration/port state.
        for item in self.temperature_settings.findChildren(QtWidgets.QLabel, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly):
            item.hide()
        temperature.experiment_mode.hide()
        temperature.settings_form.setRowVisible(temperature.condition_label, False)
        self._flatten_group(temperature.config_group)
        temperature.settings_form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        tl.addWidget(self.temperature_settings)
        layout.addWidget(self.temperature_group)
        self.liquid_enabled = QtWidgets.QCheckBox('添加液体条件')
        self.liquid_enabled.setToolTip('记录液体、容器、体积、几何及参考测量；开始采集时一起保存。')
        layout.addWidget(self.liquid_enabled)
        self.liquid_settings = liquid.take_settings_widget()
        self._flatten_group(liquid.setup_group)
        liquid.setup_group.layout().setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        liquid.setup_group.layout().setRowVisible(liquid.mode, False)
        liquid.setup_group.layout().setRowVisible(liquid.fields['sample_id'], False)
        liquid.prepare_button.hide()
        for item in self.liquid_settings.findChildren(QtWidgets.QLabel, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly):
            item.hide()
        layout.addWidget(self.liquid_settings)
        self.temperature_mode.currentIndexChanged.connect(self._changed)
        self.liquid_enabled.toggled.connect(self._changed)
        self._changed()

    @staticmethod
    def _flatten_group(group):
        group.setTitle('')
        group.setStyleSheet('QGroupBox {background: transparent; border: 0; margin: 0; padding: 0;}')
        group.layout().setContentsMargins(0, 0, 0, 0)

    @property
    def requires_serial(self):
        return not self._phase and self.temperature_mode.currentData() == 'continuous'

    def set_phase(self, phase):
        self._phase = bool(phase)
        self.temperature_group.setVisible(not phase)
        self._sync_visibility()

    def _sync_visibility(self):
        mode = self.temperature_mode.currentData()
        self.temperature_settings.setVisible(not self._phase and mode != 'none')
        self.liquid_settings.setVisible(self.liquid_enabled.isChecked())
        self.temperature.experiment_mode.setCurrentIndex(1 if mode == 'continuous' else 0)

    def _changed(self, *_):
        self._sync_visibility()
        self.changed.emit()

    def config(self, sample_name):
        with QtCore.QSignalBlocker(self.temperature.condition_label):
            self.temperature.condition_label.setText(sample_name)
        temperature = (self.temperature.config() if not self._phase and self.temperature_mode.currentData() != 'none' else None)
        liquid = None
        if self.liquid_enabled.isChecked():
            from sspp_experiments.liquid_temperature.metadata import validate_metadata
            values = {key: field.text().strip() for key, field in self.liquid.fields.items()}
            for key, title, low, high in (
                    ('volume_ml', '液体体积', .001, 1e6), ('target_temperature_c', '目标温度', -100, 200),
                    ('temperature_c', '开始参考温度', -100, 200), ('distance_mm', '开始参考距离', 0, 1e6)):
                values[key] = self.liquid._number(values[key], title, low, high)
            liquid = validate_metadata({**values, 'sample_id': sample_name})
        return {'temperature_sweep': temperature, 'liquid_temperature': liquid}

    def draft(self):
        temp = self.temperature
        return {'temperature_mode': self.temperature_mode.currentData(), 'liquid_enabled': self.liquid_enabled.isChecked(),
                'reference_temperature': temp.reference_temperature.text(), 'channel': temp.channel.currentData(),
                'calibration_enabled': temp.calibration_enabled.isChecked(), 'calibration_id': temp.calibration_id.text(),
                'calibration_fields': {key: field.text() for key, field in temp.calibration_fields.items()},
                'liquid': {key: field.text() for key, field in self.liquid.fields.items()}}

    def restore_draft(self, draft):
        draft = draft or {}
        self.temperature_mode.setCurrentIndex(max(0, self.temperature_mode.findData(draft.get('temperature_mode', 'none'))))
        self.liquid_enabled.setChecked(draft.get('liquid_enabled', False))
        temp = self.temperature
        temp.reference_temperature.setText(draft.get('reference_temperature', ''))
        temp.channel.setCurrentIndex(max(0, temp.channel.findData(draft.get('channel', 'S6'))))
        temp.calibration_enabled.setChecked(draft.get('calibration_enabled', False))
        temp.calibration_id.setText(draft.get('calibration_id', ''))
        for key, field in temp.calibration_fields.items():
            field.setText(draft.get('calibration_fields', {}).get(key, ''))
        for key, field in self.liquid.fields.items():
            field.setText(draft.get('liquid', {}).get(key, ''))
        self._sync_visibility()
