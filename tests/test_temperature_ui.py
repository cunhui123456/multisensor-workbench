"""Qt 温压工作流：显式固件准备、数据验证、采集门控和生命周期；仅 fake 设备。"""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import Mock, patch

from PySide6 import QtCore, QtWidgets
from sspp_desktop.temperature_widget import TemperaturePressureWidget
from sspp_experiments.temperature_pressure import SerialCaptureSession
from test_temperature_capture import FakeReader

PORT = "/dev/cu.usbserial-FAKE"


class FakePreparation:
    def __init__(self):
        self.is_running = False
        self.result = None
        self.status = "idle"
        self.calls = []
        self.write_active = False
        self.stop_requested = False

    def start(self, port, protocol, board="nano_old", upload=True):
        self.calls.append((port, protocol, board, upload))
        self.status, self.is_running, self.result = "checking", True, None
        self.target = dict(port=port, protocol=protocol, board=board)

    def finish(self, status="ready"):
        self.status, self.is_running = status, False
        if status == "ready":
            self.result = dict(self.target, upload=self.calls[-1][3], preparation_key="device_preparations/FAKE",
                               artifacts=[], firmware_sha256=None, firmware_identity="protocol_only")

    def matches(self, port, protocol, board="nano_old"):
        return self.status == "ready" and all(self.result.get(k) == v for k, v in dict(port=port, protocol=protocol, board=board).items())

    def snapshot(self):
        return {"status": self.status, "running": self.is_running, "message": "数据验证通过" if self.status == "ready" else "准备固件 " + self.status,
                "log": ["fake 设备日志"], "result": self.result, "write_active": self.write_active}

    def invalidate(self):
        self.result = None
        if self.is_running:
            self.request_stop()
        else:
            self.status = "idle"

    def request_stop(self):
        self.stop_requested = True
        if not self.write_active:
            self.status, self.is_running = "cancelled", False

    def stop(self, timeout_s=2):
        if self.is_running:
            self.request_stop()
        return not self.is_running


class TemperatureUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.ports = patch("sspp_desktop.temperature_widget.list_serial_ports", return_value=[PORT])
        self.ports.start()
        self.widgets = []

    def tearDown(self):
        for widget in reversed(self.widgets):
            widget.close()
            widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.ports.stop()
        self.temporary.cleanup()

    def widget(self, reader=None):
        preparation = FakePreparation()
        session = SerialCaptureSession(self.temporary.name, Mock(return_value=reader or FakeReader([])))
        widget = TemperaturePressureWidget(Path(self.temporary.name), session=session, preparation=preparation)
        self.widgets.append(widget)
        return widget

    def pump(self, predicate, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Qt fake serial condition timed out")

    def prepare(self, widget):
        widget.confirm.setChecked(True)
        widget.start_preparation(upload=False)
        self.assertTrue(widget.is_running)
        widget.preparation.finish()
        widget._poll()
        self.assertTrue(widget.preparation_ready)
        self.assertFalse(widget.is_running)

    def test_construct_and_mode_change_never_open_or_upload(self):
        widget = self.widget()
        self.assertFalse(widget.start_button.isEnabled())
        self.assertEqual(widget.preparation.calls, [])
        widget.protocol.setCurrentIndex(1)
        widget.session.transport_factory.assert_not_called()
        self.assertEqual(widget.preparation.calls, [])
        self.assertEqual(widget.channel.text(), "A1 / S6,S7")
        self.assertFalse(widget.preparation_ready)

    def test_global_open_check_is_not_data_ready_and_preparation_requires_it(self):
        widget = self.widget()
        widget.set_environment_required(True)
        widget.confirm.setChecked(True)
        self.assertFalse(widget.upload_button.isEnabled())
        widget.start_preparation(upload=False)
        self.assertIn("初始化实验台", widget.status.text())
        self.assertEqual(widget.preparation.calls, [])
        widget.set_environment_ready(True)
        self.assertTrue(widget.verify_button.isEnabled())
        self.assertFalse(widget.start_button.isEnabled())
        widget.start_capture()
        self.assertIn("固件准备", widget.status.text())
        widget.session.transport_factory.assert_not_called()
        self.prepare(widget)
        self.assertTrue(widget.start_button.isEnabled())
        widget.set_environment_ready(False)
        self.assertFalse(widget.preparation_ready)
        self.assertFalse(widget.start_button.isEnabled())

    def test_upload_requires_hardware_confirmation_and_review_dialog(self):
        widget = self.widget()
        with patch.object(widget, "_confirm_upload", return_value=False) as confirm:
            widget.start_preparation()
            confirm.assert_not_called()
            widget.confirm.setChecked(True)
            widget.start_preparation()
            confirm.assert_called_once()
            self.assertEqual(widget.preparation.calls, [])
        with patch.object(widget, "_confirm_upload", return_value=True):
            widget.start_preparation()
        self.assertEqual(widget.preparation.calls, [(PORT, "pressure_v1", "nano_old", True)])
        self.assertTrue(widget.is_running)
        self.assertFalse(widget.config_group.isEnabled())
        self.assertTrue(widget.stop_button.isEnabled())
        self.assertFalse(widget.start_button.isEnabled())
        self.assertEqual(widget.stop_button.text(), "取消准备")
        widget.stop_capture()
        widget._poll()
        self.assertFalse(widget.is_running)
        self.assertFalse(widget.preparation_ready)

    def test_preparation_failure_cancel_and_external_busy_do_not_enable_capture(self):
        widget = self.widget()
        widget.confirm.setChecked(True)
        widget.set_external_busy(True)
        widget.start_preparation(upload=False)
        self.assertEqual(widget.preparation.calls, [])
        widget.set_external_busy(False)
        events, errors = [], []
        widget.busy_changed.connect(events.append)
        widget.error.connect(errors.append)
        widget.start_preparation(upload=False)
        widget.preparation.finish("failed")
        widget._poll()
        self.assertEqual(events, [True, False])
        self.assertTrue(errors)
        self.assertFalse(widget.start_button.isEnabled())
        self.assertTrue(widget.verify_button.isEnabled())
        self.assertIn("fake 设备日志", widget.event_text.toPlainText())

    def test_cancelling_write_keeps_busy_until_backend_finishes(self):
        widget = self.widget()
        widget.confirm.setChecked(True)
        with patch.object(widget, "_confirm_upload", return_value=True):
            widget.start_preparation()
        widget.preparation.write_active = True
        widget.stop_capture()
        self.assertTrue(widget.is_running)
        self.assertFalse(widget.stop_button.isEnabled())
        self.assertIn("请勿拔线", widget.status.text())
        self.assertFalse(widget.shutdown(.01))
        widget.preparation.write_active = False
        widget.preparation.finish("cancelled")
        widget._poll()
        self.assertFalse(widget.is_running)
        self.assertFalse(widget.start_button.isEnabled())

    def test_new_port_or_protocol_requires_new_preparation(self):
        widget = self.widget()
        self.prepare(widget)
        widget.port.addItem("other", "/dev/cu.usbserial-OTHER")
        widget.port.setCurrentIndex(1)
        self.assertFalse(widget.preparation_ready)
        self.assertFalse(widget.confirm.isChecked())
        self.assertFalse(widget.start_button.isEnabled())
        self.prepare(widget)
        widget.protocol.setCurrentIndex(1)
        self.assertFalse(widget.preparation_ready)
        self.assertFalse(widget.start_button.isEnabled())

    def test_requires_sample_then_saves_bound_preparation_and_emits_once(self):
        reader = FakeReader([b"100,2.7\n150,2.8\n"])
        widget = self.widget(reader)
        self.prepare(widget)
        widget.start_capture()
        self.assertIn("样品名称", widget.status.text())
        widget.session.transport_factory.assert_not_called()
        completed, busy, messages = [], [], []
        widget.run_completed.connect(completed.append)
        widget.busy_changed.connect(busy.append)
        widget.message.connect(messages.append)
        widget.sample_name.setText("sample")
        widget.start_capture()
        self.assertTrue(widget.is_running)
        self.assertFalse(widget.config_group.isEnabled())
        self.pump(lambda: widget.session.snapshot()["rows"] == 2)
        widget.stop_capture()
        self.pump(lambda: bool(completed))
        widget._poll()
        self.assertEqual(len(completed), 1)
        self.assertEqual(busy, [True, False])
        self.assertIn("", messages)
        self.assertTrue(widget.config_group.isEnabled())
        self.assertIn("A2", widget.lines)
        self.assertIn("已停止并保存", widget.status.text())
        self.assertTrue(widget.preparation_ready)
        manifest = json.loads((Path(self.temporary.name) / "Data" / completed[0] / "manifest.json").read_text())
        self.assertEqual(manifest["preparation"]["preparation_key"], "device_preparations/FAKE")
        self.assertTrue(reader.closed)

    def test_failed_capture_invalidates_both_readiness_before_busy_release(self):
        reader = FakeReader([b"100,2.7\n", OSError("fake cable disconnected")])
        widget = self.widget(reader)
        widget.set_environment_required(True)
        widget.set_environment_ready(True)
        self.prepare(widget)
        widget.sample_name.setText("sample")
        completed, events = [], []
        widget.run_completed.connect(completed.append)
        widget.environment_invalidated.connect(lambda: events.append(("invalidated", widget.start_button.isEnabled())))
        widget.busy_changed.connect(lambda busy: events.append(("busy", busy)))
        widget.start_capture()
        self.pump(lambda: bool(completed))
        widget._poll()
        self.assertEqual(events, [("busy", True), ("invalidated", False), ("busy", False)])
        self.assertFalse(widget.preparation_ready)
        self.assertFalse(widget.start_button.isEnabled())
        self.assertTrue(reader.closed)

    def test_live_wait_and_diagnostic_are_not_labelled_receiving(self):
        widget = self.widget()
        self.prepare(widget)
        widget.sample_name.setText("sample")
        widget.start_capture()
        self.pump(lambda: widget.session.snapshot()["status"] == "running")
        snapshot = widget.session.snapshot()
        snapshot["diagnostic"] = "已打开串口，但未收到任何原始字节"
        with patch.object(widget.session, "snapshot", return_value=snapshot):
            widget._poll()
        self.assertIn("等待首个有效样本", widget.status.text())
        self.assertIn("未收到任何原始字节", widget.status.text())
        widget.stop_capture()
        self.pump(lambda: not widget.is_running)

    def test_temperature_channels_draw_both_units_in_visible_side_by_side_plots(self):
        reader = FakeReader([b"100,640,3.10,139,670,3.28\n280,642,3.12,319,690,3.38\n"])
        widget = self.widget(reader)
        sidebar = QtWidgets.QWidget()
        self.widgets.append(sidebar)
        QtWidgets.QVBoxLayout(sidebar).addWidget(widget.take_settings_widget())
        widget.resize(720, 550)
        widget.protocol.setCurrentIndex(widget.protocol.findData("temperature_v1"))
        widget.show()
        self.prepare(widget)
        widget.sample_name.setText("synthetic two-channel")
        widget.start_capture()
        self.pump(lambda: widget.session.snapshot()["rows"] == 4)
        widget._poll()
        self.app.processEvents()
        self.assertEqual(set(widget.lines), {"S6", "S7"})
        self.assertEqual(list(widget.lines["S7"][0].yData), [3.28, 3.38])
        self.assertEqual(list(widget.lines["S7"][1].yData), [670., 690.])
        self.assertFalse(widget.adc_plot.isHidden())
        self.assertLessEqual(widget.voltage_plot.geometry().right(), widget.adc_plot.geometry().left())
        self.assertLessEqual(widget.adc_plot.geometry().right(), widget.plot_panel.width())
        widget.stop_capture()
        self.pump(lambda: not widget.is_running)

    def test_validation_preserves_ready_but_session_start_failure_invalidates(self):
        widget = self.widget()
        self.prepare(widget)
        widget.start_capture()
        self.assertTrue(widget.preparation_ready)
        widget.sample_name.setText("sample")
        with patch.object(widget.session, "start", side_effect=RuntimeError("fake worker failed")):
            widget.start_capture()
        self.assertFalse(widget.preparation_ready)
        self.assertFalse(widget.start_button.isEnabled())
        widget.session.transport_factory.assert_not_called()

    def test_bluetooth_port_is_never_offered_for_sensor_preparation(self):
        with patch("sspp_desktop.temperature_widget.list_serial_ports", return_value=["/dev/cu.ULTFIELD1", "/dev/cu.Bluetooth-Incoming-Port"]):
            widget = self.widget()
        self.assertEqual(widget.port.count(), 0)
        self.assertFalse(widget.upload_button.isEnabled())
        self.assertIn("USB 串口", widget.status.text())

    def test_close_during_capture_stops_worker_and_releases_plots(self):
        from shiboken6 import isValid
        reader = FakeReader([b"100,2.7\n"])
        widget = self.widget(reader)
        self.prepare(widget)
        widget.sample_name.setText("sample")
        widget.start_capture()
        self.pump(lambda: widget.session.snapshot()["rows"] == 1)
        plot = widget.voltage_plot
        widget.close()
        widget.deleteLater()
        self.widgets.remove(widget)
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertTrue(reader.closed)
        self.assertFalse(isValid(plot))

    def test_settings_reparent_preserves_controls(self):
        widget = self.widget()
        sidebar = QtWidgets.QWidget()
        self.widgets.append(sidebar)
        QtWidgets.QVBoxLayout(sidebar).addWidget(widget.take_settings_widget())
        self.assertIs(widget.take_settings_widget(), widget.settings_widget)
        self.prepare(widget)
        self.assertTrue(widget.start_button.isEnabled())

    def test_valid_voltage_is_visible_after_previous_offscreen_range(self):
        from sspp_experiments.temperature_pressure.protocol import LineParser
        parser = LineParser("temperature_v1")
        points = []
        # Same shape as the actual 2026-10-31 file: two valid voltages around 4.15/2.72 V.
        for index in range(12):
            values, _ = parser.feed(f"{177 + index * 180},849.5,4.152,{216 + index * 180},556.06,2.718\n".encode(),
                                    host_time_utc="2026-10-31T08:25:10Z", host_elapsed_s=1.7 + index * .18)
            points.extend(values)
        widget = self.widget()
        widget.timer.stop()
        widget.protocol.setCurrentIndex(1)
        widget.voltage_plot.setXRange(-1000, -900, padding=0)
        widget.voltage_plot.setYRange(10, 20, padding=0)
        widget.adc_plot.setYRange(-10, -5, padding=0)
        widget._plot_points(points)
        self.app.processEvents()
        vx, vy = widget.voltage_plot.viewRange()
        ax, ay = widget.adc_plot.viewRange()
        self.assertLessEqual(vx[0], 1.7)
        self.assertGreaterEqual(vx[1], 3.68)
        self.assertLess(vy[0], 2.718)
        self.assertGreater(vy[1], 4.152)
        self.assertLess(ay[0], 556.06)
        self.assertGreater(ay[1], 849.5)
        self.assertIn("CH1 / S6：4.1520 V", widget.voltage_readout.text())
        self.assertIn("CH2 / S7：2.7180 V", widget.voltage_readout.text())
        self.assertTrue(widget.lines["S6"][0].isVisible())
        widget.display_channel.setCurrentIndex(1)
        self.assertTrue(widget.lines["S6"][0].isVisible())
        self.assertFalse(widget.lines["S7"][0].isVisible())
        widget.session.transport_factory.assert_not_called()

    def test_manual_plot_zoom_pauses_follow_and_full_range_recovers(self):
        widget = self.widget()
        widget.timer.stop()
        points = [dict(channel="A2", host_elapsed_s=t, voltage_v=2.5 + .01*t,
                       adc="", time_segment=0, line_number=i) for i, t in enumerate((1, 2, 3))]
        widget._plot_points(points)
        widget.voltage_plot.plotItem.vb.sigRangeChangedManually.emit([True, True])
        self.assertFalse(widget.auto_follow.isChecked())
        widget.voltage_plot.setXRange(100, 110, padding=0)
        widget._plot_points(points)
        self.assertEqual(widget.voltage_plot.viewRange()[0], [100, 110])
        widget.full_range_button.click()
        self.assertTrue(widget.auto_follow.isChecked())
        self.assertLess(widget.voltage_plot.viewRange()[0][0], 1)
        self.assertGreater(widget.voltage_plot.viewRange()[0][1], 3)

    def test_qt_process_exits_cleanly_without_opening_devices(self):
        program = textwrap.dedent("""
            import tempfile
            from pathlib import Path
            from unittest.mock import patch
            from PySide6 import QtCore, QtWidgets
            from sspp_desktop.temperature_widget import TemperaturePressureWidget
            app = QtWidgets.QApplication([])
            with tempfile.TemporaryDirectory() as root, patch('sspp_desktop.temperature_widget.list_serial_ports', return_value=[]):
                for _ in range(3):
                    widget = TemperaturePressureWidget(Path(root))
                    widget.close()
                    widget.deleteLater()
                    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
                    app.processEvents()
            print('temperature Qt cleanup passed')
        """)
        result = subprocess.run([sys.executable, "-B", "-X", "faulthandler", "-c", program],
                                cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("temperature Qt cleanup passed", result.stdout)
        self.assertNotIn("shared QObject was deleted", result.stderr)
        self.assertNotIn("AttributeError", result.stderr)

    def test_platform_capture_needs_no_room_temperature_or_90c_endpoint(self):
        widget = self.widget(FakeReader([b"100,600,2.933,140,650,3.177\n"]))
        widget.set_calibration_mode(True)
        widget.sample_name.setText("platforms")
        self.assertFalse(hasattr(widget, "room_temperature"))
        self.assertFalse(hasattr(widget, "endpoint_button"))
        self.prepare(widget)
        widget.start_capture()
        self.pump(lambda: widget.session.snapshot()["rows"] == 2)
        widget._poll()
        self.assertTrue(widget.plateau_controls.begin_button.isEnabled())
        self.assertIn("ΔV", widget.voltage_readout.text())
        widget.stop_capture()
        self.pump(lambda: not widget.session.is_running)
        widget._poll()
        manifest=json.loads((widget.session.run_path / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "complete")
        self.assertNotIn("temperature_ramp", manifest)
        self.assertEqual(manifest["temperature_calibration"]["mode"], "platform_intervals")
        self.assertEqual(manifest["stop_reason"], "operator_stop")

    def test_plateau_controls_remain_enabled_during_capture_and_save_statistics(self):
        reader=FakeReader([b"100,600,2.900,140,650,3.100\n"])
        widget=self.widget(reader)
        widget.set_calibration_mode(True)
        widget.sample_name.setText("platform_test")
        self.prepare(widget)
        widget.start_capture()
        self.pump(lambda:widget.session.snapshot()["rows"]==2)
        widget._poll()
        controls=widget.plateau_controls
        self.assertFalse(widget.config_group.isEnabled())
        self.assertTrue(controls.begin_button.isEnabled())
        controls.source.setText("reference-thermometer-1")
        controls.voltage_limit.setText("25")
        controls.begin_button.click()
        widget._poll()
        self.assertIsNone(widget.session.snapshot()['plateau'].get('active'))
        self.assertFalse(controls.feedback.isHidden())
        self.assertIn('10',controls.feedback.text())
        controls.voltage_unit.setCurrentIndex(controls.voltage_unit.findData(.001))
        controls.begin_button.click()
        self.pump(lambda:widget.session.snapshot()["plateau"].get("active") is not None)
        widget._poll()
        self.assertFalse(controls.begin_button.isEnabled())
        self.assertTrue(controls.reference_button.isEnabled())
        controls.reference.setText("49.7")
        controls.reference_button.click()
        reader.chunks.append(b"200,600,2.920,240,650,3.120\n")
        self.pump(lambda:widget.session.snapshot()["rows"]==4 and not widget.session.snapshot()["plateau_pending"])
        widget._poll()
        controls.end_button.click()
        self.pump(lambda:len(widget.session.snapshot()["plateau"].get("points",[]))==1)
        widget._poll()
        self.assertTrue(widget.session.is_running)
        self.assertEqual(controls.table.rowCount(),1)
        self.assertEqual(controls.table.item(0,1).text(),"49.700")
        saved=json.loads((widget.session.run_path/'calibration_points.json').read_text())
        self.assertEqual(saved['points'][0]['settings']['voltage_rate_limit_v_per_min'],.025)
        self.assertIn("参考记录不足",controls.table.item(0,4).text())
        self.assertTrue(controls.begin_button.isEnabled())
        widget.stop_capture()
        self.pump(lambda:not widget.session.is_running)
        widget._poll()
        self.assertFalse(controls.begin_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
