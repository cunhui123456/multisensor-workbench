"""Configurable pressure UI; fake devices only, no serial ports opened."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6 import QtCore, QtWidgets
from sspp_desktop.temperature_widget import TemperaturePressureWidget
from sspp_desktop.qt_cleanup import close_plot_widgets
from sspp_experiments.temperature_pressure import SerialCaptureSession
from test_temperature_ui import FakePreparation, PORT


class ConfigurablePreparation(FakePreparation):
    def start(self, port, protocol, board='nano_old', upload=True, pressure_channel='S3'):
        super().start(port, protocol, board, upload)
        self.target['pressure_channel'] = pressure_channel

    def matches(self, port, protocol, board='nano_old', pressure_channel='S3'):
        return (super().matches(port, protocol, board, pressure_channel=pressure_channel)
                and (protocol != 'pressure_v2' or self.result.get('pressure_channel') == pressure_channel))


class PressureConfigurationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.factory = Mock(side_effect=AssertionError('No hardware in UI tests'))
        self.session = SerialCaptureSession(self.temp.name, self.factory)
        self.preparation = ConfigurablePreparation()
        with patch('sspp_desktop.temperature_widget.list_serial_ports', return_value=[PORT]):
            self.widget = TemperaturePressureWidget(Path(self.temp.name), session=self.session, preparation=self.preparation)
        self.widget.timer.stop()
        self.widget.set_pressure_calibration_mode(True)

    def tearDown(self):
        self.widget._operation = None
        self.widget.close()
        close_plot_widgets(self.widget)
        self.widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def prepare(self):
        self.widget.start_preparation(upload=False)
        self.preparation.finish()
        self.widget._poll()

    def test_defaults_identify_real_hardware_selector_without_io(self):
        w = self.widget
        self.assertEqual(w.protocol.currentData(), 'pressure_v2')
        self.assertEqual([w.pressure_sensor.itemData(i) for i in range(5)], ['S1','S2','S3','S4','S5'])
        self.assertEqual(w.pressure_sensor.currentData(), 'S3')
        self.assertEqual(w.pressure_controls.selected_channel(), 'S3')
        self.assertFalse(w.pressure_controls.pressure_channel.isEnabled())
        self.assertFalse(w.zero_button.isEnabled())
        self.factory.assert_not_called()
        self.assertEqual(self.preparation.calls, [])

    def test_selection_invalidates_preparation_and_old_baseline_is_not_relabelled(self):
        w = self.widget
        self.prepare()
        self.assertTrue(w.preparation_ready)
        w.pressure_sensor.setCurrentIndex(4)
        self.assertFalse(w.preparation_ready)
        self.assertEqual(w.pressure_controls.selected_channel(), 'S5')
        self.assertFalse(w.start_button.isEnabled())
        self.prepare()
        self.assertEqual(self.preparation.result['pressure_channel'], 'S5')
        self.factory.assert_not_called()

    def test_capture_passes_hardware_channel_separately_from_wiring_note(self):
        w = self.widget
        w.pressure_sensor.setCurrentIndex(0)
        self.prepare()
        w.sample_name.setText('fake sample')
        w.channel.setText('手工接线说明，非通道命令')
        with patch.object(self.session, 'start', return_value='temperature_pressure_runs/FAKE') as start:
            w.start_capture()
        config = start.call_args.args[0]
        self.assertEqual(config.pressure_channel, 'S1')
        self.assertEqual(config.channel, '手工接线说明，非通道命令')
        self.assertFalse(w.config_group.isEnabled())
        self.factory.assert_not_called()

    def test_manual_zero_requires_confirmed_capture_and_no_active_interval(self):
        w = self.widget
        w._operation = 'capture'
        w.stop_button.setEnabled(True)
        snap = dict(status='running', rows=1, pressure_control={'confirmed_channel':'S3','selection_token':77}, points=[dict(channel='S3',selection_token=77)], plateau={})
        w._update_zero_controls(snap)
        self.assertTrue(w.zero_button.isEnabled())
        with patch.object(self.session, 'pressure_command') as command:
            w.zero_button.click()
            command.assert_called_once_with('zero')
        self.assertFalse(w.zero_button.isEnabled())
        for update in ({'pending':True}, {'confirmed_channel':None}):
            sample = {**snap, 'pressure_control':{**snap['pressure_control'], **update}}
            w._update_zero_controls(sample)
            self.assertFalse(w.zero_button.isEnabled())
        w._update_zero_controls({**snap, 'plateau':{'active':{'id':1}}})
        self.assertFalse(w.clear_zero_button.isEnabled())
        self.assertIn('结束区间', w.zero_status.text())

    def test_live_switch_uses_serial_owner_and_waits_for_ack_without_stopping_run(self):
        w = self.widget
        w._operation = 'capture'
        w.stop_button.setEnabled(True)
        snap = dict(status='running', rows=2, pressure_control={'confirmed_channel':'S3','selection_token':77}, points=[dict(channel='S3',selection_token=77)], plateau={})
        w._update_zero_controls(snap)
        self.assertTrue(w.switch_pressure_button.isEnabled())
        w.live_pressure_sensor.setCurrentIndex(4)
        with patch.object(self.session, 'pressure_command') as command:
            w.switch_pressure_button.click()
            command.assert_called_once_with('select', channel='S5')
        self.assertEqual(w._operation, 'capture')
        self.assertFalse(w.switch_pressure_button.isEnabled())
        self.assertIn('切换通道', w.voltage_readout.text())
        w._update_zero_controls({**snap,'pressure_control':{'confirmed_channel':None,'pending':'select'}})
        self.assertFalse(w.zero_button.isEnabled())
        w._update_zero_controls({**snap,'pressure_control':{'confirmed_channel':'S5','selection_token':78,'pending':None}})
        self.assertFalse(w.zero_button.isEnabled())
        self.assertIn('等待该通道',w.voltage_readout.text())
        w._update_zero_controls({**snap,'pressure_control':{'confirmed_channel':'S5','selection_token':78,'pending':None},
                                 'points':[dict(channel='S5',selection_token=78)]})
        self.assertTrue(w.zero_button.isEnabled())
        self.assertIn('当前 S5',w.zero_status.text())
        w._update_zero_controls({**snap,'plateau':{'active':{'id':1}}})
        self.assertFalse(w.switch_pressure_button.isEnabled())

    def test_completed_previous_channel_baseline_is_not_presented_as_current(self):
        w = self.widget
        old = dict(status='complete',rows=1,pressure_control={'confirmed_channel':'S3'},
                   points=[dict(channel='S3',baseline_v=2.72,zero_id=1)],plateau={})
        w.pressure_sensor.setCurrentIndex(4)
        w._update_zero_controls(old)
        self.assertNotIn('2.72',w.zero_status.text())
        self.assertIn('尚未开始',w.zero_status.text())

    def test_raw_adc_and_manual_delta_display_without_fabricating_zero(self):
        w = self.widget
        row = dict(channel='S3', time_segment=0, line_number=1, host_elapsed_s=1.,
                   voltage_v=2.72, adc=556.51, baseline_v='', delta_voltage_v='', zero_id=0)
        w._plot_points([row])
        self.assertIn('2.7200 V', w.voltage_readout.text())
        self.assertIn('ADC 556.51', w.voltage_readout.text())
        self.assertIn('未置零', w.voltage_readout.text())
        row.update(baseline_v=2.71, delta_voltage_v=.01, zero_id=1)
        w._plot_points([row])
        self.assertIn('ΔV +0.0100 V', w.voltage_readout.text())
        self.assertAlmostEqual(w.lines['S3'][1].yData[0], .01)

    def test_legacy_mode_cannot_expose_hardware_selection_or_zero(self):
        w = self.widget
        w.protocol.setCurrentIndex(w.protocol.findData('pressure_v1'))
        self.assertTrue(w.pressure_sensor.isHidden())
        self.assertTrue(w.zero_panel.isHidden())
        self.assertIn('不能切换硬件', w.wiring_note.text())
        self.assertEqual(w._pressure_options(), {})


if __name__ == '__main__':
    unittest.main()
