"""联合扫频面板的离线行为；不打开传感器。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from unittest.mock import patch

import numpy as np
from PySide6 import QtCore, QtWidgets

from sspp_desktop.temperature_sweep_widget import TemperatureSweepWidget


PORT = "/dev/cu.usbserial-FAKE"


class TemperatureSweepUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.ports = patch("sspp_desktop.temperature_sweep_widget.list_serial_ports",
                           return_value=[PORT, "/dev/cu.Bluetooth-Incoming-Port", "/dev/cu.ULTFIELD1"])
        self.ports.start()
        self.widget = TemperatureSweepWidget()
        self.widget.experiment_mode.setCurrentIndex(self.widget.experiment_mode.findData("continuous"))

    def tearDown(self):
        self.widget.close()
        if self.widget.settings_widget.parent() is None:
            self.widget.settings_widget.deleteLater()
        self.widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.ports.stop()

    def test_default_voltage_only_and_reference_remains_independent(self):
        config = self.widget.config()
        self.assertEqual(config["protocol"], "temperature_v1")
        self.assertEqual(config["channel"], "S6")
        self.assertEqual(config["port"], PORT)
        self.assertEqual(self.widget.port.count(), 1)
        self.assertIsNone(config["calibration"])
        self.assertIsNone(config["reference_temperature_c"])
        self.widget.reference_temperature.setText("43.2")
        self.assertIsNone(self.widget.config()["reference_temperature_c"])
        self.widget.experiment_mode.setCurrentIndex(0)
        config=self.widget.config()
        self.assertEqual(config["reference_temperature_c"],43.2)
        self.assertEqual(config["mode"],"manual")
        self.widget.set_port("")
        self.assertEqual(self.widget.config()["port"],"")
        self.widget.reference_temperature.setText("NaN")
        with self.assertRaisesRegex(ValueError,"有限"):self.widget.config()

    def test_calibration_opt_in_rejects_incomplete_identity_zero_slope_and_range(self):
        self.widget.calibration_enabled.setChecked(True)
        with self.assertRaisesRegex(ValueError, "编号"):
            self.widget.config()
        self.widget.calibration_id.setText("reference-2026-10-31")
        values = dict(slope_c_per_v="-40", intercept_c="125", voltage_min="1", voltage_max="3")
        for key, value in values.items():
            self.widget.calibration_fields[key].setText(value)
        config = self.widget.config()
        self.assertEqual(config["calibration"]["slope_c_per_v"], -40)
        self.widget.calibration_fields["voltage_min"].setText("4")
        with self.assertRaisesRegex(ValueError, "范围"):
            self.widget.config()
        self.widget.calibration_fields["voltage_min"].setText("1")
        self.widget.calibration_fields["slope_c_per_v"].setText("0")
        with self.assertRaisesRegex(ValueError, "斜率"):
            self.widget.config()
        self.widget.calibration_enabled.setChecked(False)
        self.assertIsNone(self.widget.config()["calibration"])

    def test_global_port_sync_does_not_echo_or_reinitialize(self):
        changes = []
        self.widget.port_changed.connect(changes.append)
        self.widget.set_port("/dev/cu.usbserial-OTHER")
        self.assertEqual(changes, [])
        self.assertEqual(self.widget.config()["port"], "/dev/cu.usbserial-OTHER")
        self.widget.port.setCurrentIndex(0)
        self.assertEqual(changes, [PORT])
        self.widget.set_port("")
        with self.assertRaisesRegex(ValueError, "串口"):
            self.widget.config()

    def test_busy_disables_edits_and_preparation_but_settings_can_move(self):
        requests = []
        self.widget.prepare_temperature_requested.connect(lambda: requests.append(True))
        self.widget.prepare_button.click()
        self.assertEqual(requests, [True])
        self.widget.set_external_busy(True)
        self.widget.prepare_button.click()
        self.assertEqual(requests, [True])
        self.assertFalse(self.widget.config_group.isEnabled())
        self.assertFalse(self.widget.calibration_enabled.isEnabled())
        self.assertIs(self.widget.take_settings_widget(), self.widget.settings_widget)
        self.assertIs(self.widget.take_settings_widget(), self.widget.settings_widget)
        self.widget.set_external_busy(False)
        self.assertTrue(self.widget.config_group.isEnabled())
        self.assertFalse(self.widget.calibration_group.isEnabled())

    def test_snapshot_preserves_both_channels_and_does_not_derive_temperature(self):
        snapshot = {"points": [dict(carrier_hz=5e9, amplitude_dB=-22.3, repeat=1),
                               dict(carrier_hz=5.3e9, amplitude_dB=-26, repeat=1)],
                    "temperature_sweep": {"channel": "S6", "condition_label": "加热 1", "voltage_rows": [
                        dict(channel="S6", host_elapsed_s=1, voltage_v=2.1, time_segment=0),
                        dict(channel="S7", host_elapsed_s=1, voltage_v=3.1, time_segment=0),
                        dict(channel="S6", host_elapsed_s=2, voltage_v=2.2, time_segment=0)]}}
        self.widget.update_snapshot(snapshot)
        x, y = self.widget.sweep_items[1].getData()
        np.testing.assert_allclose(x, [5, 5.3])
        np.testing.assert_allclose(y, [-22.3, -26])
        np.testing.assert_allclose(self.widget.voltage_lines["S6"].getData()[1], [2.1, 2.2])
        np.testing.assert_allclose(self.widget.voltage_lines["S7"].getData()[1], [3.1])
        self.assertIn("未标定", self.widget.readout.text())
        self.assertTrue(self.widget.temperature_plot.isHidden())
        self.widget.clear()
        self.assertEqual(self.widget.sweep_items, {})

    def test_calibrated_history_uses_stored_channel_and_breaks_on_reset(self):
        self.widget.channel.setCurrentIndex(0)
        rows = [dict(channel="S7", host_elapsed_s=1, voltage_v=2, temperature_c=45, time_segment=0),
                dict(channel="S7", host_elapsed_s=2, voltage_v=2.1, temperature_c=None, time_segment=1),
                dict(channel="S7", host_elapsed_s=3, voltage_v=2.2, temperature_c=47, time_segment=1)]
        self.widget.update_snapshot({"historical": True, "temperature_sweep": {
            "channel": "S7", "calibration": {"id": "real-cal"}, "voltage_rows": rows}})
        self.assertIn("历史", self.widget.status.text())
        self.assertIn("S7", self.widget.readout.text())
        self.assertIn("47.000", self.widget.readout.text())
        self.assertFalse(self.widget.temperature_plot.isHidden())
        y = self.widget.voltage_lines["S7"].getData()[1]
        self.assertTrue(np.isnan(y[1]))
        self.assertEqual(len(y), 4)

    def test_draft_roundtrip_retains_raw_mode_and_chosen_channel(self):
        self.widget.channel.setCurrentIndex(1)
        self.widget.condition_label.setText("降温 · 平台 2")
        self.widget.reference_temperature.setText("50")
        original = self.widget.config()
        self.widget.restore_config({})
        self.widget.restore_config(original)
        self.assertEqual(self.widget.config(), original)

    def test_live_service_snapshot_uses_bound_configuration(self):
        self.widget.update_snapshot({"points": [], "job": {"config": {"temperature_sweep": {
            "channel": "S7", "condition_label": "已绑定条件"}}}, "temperature_points": [
                dict(channel="S7", voltage_v=2.8, host_elapsed_s=.2, time_segment=0)]})
        self.assertIn("已绑定条件", self.widget.status.text())
        self.assertIn("S7", self.widget.readout.text())
        np.testing.assert_allclose(self.widget.voltage_lines["S7"].getData()[1], [2.8])


if __name__ == "__main__":
    unittest.main()
