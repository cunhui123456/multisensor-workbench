"""Pressure UI validation and fake-serial end-to-end integration; no hardware."""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from PySide6 import QtCore, QtWidgets

from sspp_desktop.pressure_widget import PressureCalibrationWidget
from sspp_desktop.temperature_widget import TemperaturePressureWidget
from sspp_desktop.qt_cleanup import close_plot_widgets
from sspp_experiments.temperature_pressure import SerialCaptureSession
from test_temperature_capture import FakeReader
from test_temperature_ui import FakePreparation, PORT


class PressureUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.widgets = []

    def tearDown(self):
        for widget in reversed(self.widgets):
            widget.close()
            close_plot_widgets(widget)
            widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def controls(self):
        widget = PressureCalibrationWidget()
        self.widgets.append(widget)
        widget.source.setText('force-reference-1')
        widget.pressure_channel.setCurrentIndex(1)
        widget.update_state({}, True, False)
        return widget

    def capture_widget(self, reader=None):
        reader = reader or FakeReader([])
        session = SerialCaptureSession(self.temporary.name, Mock(return_value=reader))
        with patch('sspp_desktop.temperature_widget.list_serial_ports', return_value=[PORT]):
            widget = TemperaturePressureWidget(Path(self.temporary.name), session=session, preparation=FakePreparation())
        self.widgets.append(widget)
        widget.timer.stop()
        widget.set_calibration_mode(False)
        widget.set_pressure_calibration_mode(True)
        widget.protocol.setCurrentIndex(widget.protocol.findData('pressure_v1'))
        widget.sample_name.setText('synthetic-pressure')
        return widget

    def pump(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail('fake serial timed out')

    @staticmethod
    def prepare(widget):
        widget.start_preparation(upload=False)
        widget.preparation.finish()
        widget._poll()

    def test_entering_pressure_does_not_choose_channel_or_open_hardware(self):
        widget = self.capture_widget()
        self.assertFalse(widget.pressure_controls.isHidden())
        self.assertFalse(widget.pressure_results.isHidden())
        self.assertTrue(widget.protocol.isEnabled())
        self.assertIsNone(widget.pressure_controls.pressure_channel.currentData())
        self.assertTrue(widget.plateau_controls.isHidden())
        widget.session.transport_factory.assert_not_called()
        self.assertEqual(widget.preparation.calls, [])
        self.prepare(widget)
        widget.start_capture()
        self.assertIn('力敏通道', widget.status.text())
        widget.session.transport_factory.assert_not_called()

    def test_protocol_changes_clear_channel_selection_without_preparing_firmware(self):
        widget = self.capture_widget()
        widget.pressure_controls.pressure_channel.setCurrentIndex(1)
        widget.protocol.setCurrentIndex(widget.protocol.findData('temperature_v1'))
        self.assertIsNone(widget.pressure_controls.pressure_channel.currentData())
        self.assertEqual(widget.pressure_controls._channels, ('S6', 'S7'))
        widget.pressure_controls.pressure_channel.setCurrentIndex(2)
        self.assertEqual(widget.pressure_controls.selected_channel(), 'S7')
        widget.session.transport_factory.assert_not_called()
        self.assertEqual(widget.preparation.calls, [])

    def test_interval_inputs_and_explicit_units_are_validated_without_losing_error(self):
        widget = self.controls()
        calls = []
        widget.requested.connect(lambda op, values: calls.append((op, values)))
        for area in ('0', '-2', 'nan', 'inf'):
            widget.area.setText(area)
            widget.begin()
            widget.update_state({}, True, False)
            self.assertIn('面积', widget.feedback.text())
            self.assertFalse(calls)
        widget.area.setText('20')
        widget.voltage_limit.setText('25')
        widget.begin()
        self.assertFalse(calls)
        self.assertIn('电压', widget.feedback.text())
        widget.voltage_unit.setCurrentIndex(1)
        widget.direction.setCurrentIndex(1)
        widget.cycle.setValue(2)
        widget.begin()
        self.assertEqual(calls[0][0], 'begin')
        values = calls[0][1]
        self.assertEqual(values['pressure_channel'], 'A2')
        self.assertEqual(values['contact_area_mm2'], 20)
        self.assertEqual(values['direction'], 'unloading')
        self.assertEqual(values['cycle'], 2)
        self.assertEqual(values['settings']['voltage_rate_limit_v_per_min'], .025)
        self.assertTrue(widget.feedback.isHidden())

    def test_reference_is_real_force_and_missing_area_is_not_invented(self):
        widget = self.controls()
        calls = []
        widget.requested.connect(lambda op, values: calls.append((op, values)))
        widget.begin()
        self.assertIsNone(calls[0][1]['contact_area_mm2'])
        for value in ('', 'NaN', '-1'):
            widget.reference.setText(value); widget.add_reference()
        self.assertEqual(len(calls), 1)
        widget.reference.setText('0'); widget.add_reference()
        self.assertEqual(calls[-1], ('reference', {'force_n': 0}))
        self.assertTrue(widget.feedback.isHidden())
        self.assertFalse(widget.reference.text())

    def test_fake_capture_preserves_force_points_and_mode_switch_isolation(self):
        reader = FakeReader([b'100,2.7\n'])
        widget = self.capture_widget(reader)
        controls = widget.pressure_controls
        controls.pressure_channel.setCurrentIndex(1)
        controls.source.setText('force-reference-1')
        controls.area.setText('10')
        self.prepare(widget)
        widget.start_capture()
        self.pump(lambda: widget.session.snapshot()['rows'] == 1)
        widget._poll()
        self.assertTrue(controls.begin_button.isEnabled())
        self.assertFalse(widget.config_group.isEnabled())
        controls.begin_button.click()
        self.pump(lambda: widget.session.snapshot()['plateau'].get('active'))
        widget._poll()
        self.assertFalse(controls.pressure_channel.isEnabled())
        self.assertTrue(controls.reference_button.isEnabled())
        controls.reference.setText('1.25'); controls.reference_button.click()
        reader.chunks.append(b'200,2.8\n300,2.9\n')
        self.pump(lambda: widget.session.snapshot()['rows'] == 3 and not widget.session.snapshot()['plateau_pending'])
        widget._poll(); controls.end_button.click()
        self.pump(lambda: len(widget.session.snapshot()['plateau'].get('points', [])) == 1)
        widget._poll()
        self.assertEqual(controls.table.item(0, 2).text(), '1.25')
        self.assertEqual(controls.table.item(0, 3).text(), '125')
        self.assertIn('待复核', controls.table.item(0, 6).text())
        self.assertIn('尚无足够', controls.fit_summary.toPlainText())
        widget.stop_capture()
        self.pump(lambda: not widget.session.is_running)
        widget._poll()
        manifest = json.loads((widget.session.run_path / 'manifest.json').read_text())
        self.assertIn('pressure_calibration', manifest)
        self.assertNotIn('temperature_calibration', manifest)
        saved = json.loads((widget.session.run_path / 'calibration_points.json').read_text())
        self.assertEqual(saved['schema'], 'sspp.pressure_plateaus.v1')
        self.assertEqual(saved['points'][0]['pressure_channel'], 'A2')
        self.assertTrue(reader.closed)
        widget.set_calibration_mode(True); widget.set_pressure_calibration_mode(False)
        widget._poll()
        self.assertFalse(widget.protocol.isEnabled())
        self.assertEqual(widget.plateau_controls.table.rowCount(), 0)
        self.assertTrue(widget.pressure_results.isHidden())

    def test_candidate_fit_is_descriptive_only_and_response_includes_review_points(self):
        widget = self.controls()
        points = [dict(id=1, cycle=1, direction='loading', pressure_channel='A2',
                       reference_mean_n=1., reference_mean_kpa=None, duration_s=30., status='saved',
                       reference_status='within_limits', quality_flags=['voltage_data_gap'],
                       channels={'A2': dict(mean_v=2., sample_sd_v=.01)})]
        fit = dict(channel='A2', direction='loading', cycle=1, status='candidate', point_count=3,
                   slope_n_per_v=-2., intercept_n=5., rmse_n=.01, voltage_min_v=1., voltage_max_v=2.)
        widget.update_state({'points': points, 'candidate_fits': {'group_1': fit}}, False, False)
        self.assertEqual(widget.table.item(0, 3).text(), '未定义面积')
        self.assertIn('待复核', widget.table.item(0, 6).text())
        self.assertIn('F(N) = -2', widget.fit_summary.toPlainText())
        self.assertIn('不自动换算', widget.fit_summary.toPlainText())
        curves = widget.response_plot.listDataItems()
        self.assertEqual(len(curves), 1)
        self.assertEqual(list(curves[0].xData), [1.])
        self.assertFalse(widget.begin_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
