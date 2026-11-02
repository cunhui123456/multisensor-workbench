"""联合采集以 fake 串口验证，不打开 USRP/真实串口。"""

import csv
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sspp_experiments.temperature_sweep.acquisition import TemperatureSweepCapture
from sspp_experiments.temperature_sweep.metadata import validate_config, calibrated_temperature
from sspp_experiments.temperature_pressure.protocol import LineParser


def config(**overrides):
    return {"port": "/dev/cu.FAKE", "condition_label": "升温第一平台", "channel": "S6", **overrides}


class FakeReader:
    def __init__(self, *, continuing=True, malformed=False):
        self.count = 0
        self.closed = False
        self.continuing = continuing
        self.malformed = malformed
        self.disconnected = False

    def read(self, timeout=.1):
        time.sleep(.01)
        if self.disconnected:
            raise EOFError("fake disconnected")
        self.count += 1
        if self.malformed:
            return b"1,2.2\n"
        if self.count > 2 and not self.continuing:
            return None
        millis = self.count * 100
        return f"{millis},400,1.955,{millis+40},600,2.933\n".encode()

    def close(self):
        self.closed = True


def wait_until(check, timeout=2):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        if check():
            return
        time.sleep(.01)
    raise AssertionError("fake wait timed out")


class ConfigTests(unittest.TestCase):
    def test_uncalibrated_has_no_temperature_and_preserves_reference_separately(self):
        cfg = validate_config(config(reference_temperature_c=50))
        self.assertEqual(cfg["reference_temperature_c"], 50)
        self.assertEqual(calibrated_temperature(2.3, cfg["calibration"]), (None, "uncalibrated"))

    def test_linear_calibration_respects_inclusive_voltage_range(self):
        calibration = {"id": "test-20261031", "slope_c_per_v": -20, "intercept_c": 100,
                       "voltage_min": 1, "voltage_max": 3}
        cfg = validate_config(config(calibration=calibration))
        self.assertEqual(calibrated_temperature(2, cfg["calibration"]), (60, "calibrated"))
        self.assertEqual(calibrated_temperature(1, cfg["calibration"]), (80, "calibrated"))
        self.assertEqual(calibrated_temperature(3.1, cfg["calibration"]), (None, "outside_calibration_range"))

    def test_reject_nonfinite_missing_id_wrong_protocol_and_wrong_channel(self):
        for updates in ({"reference_temperature_c": float("nan")}, {"freshness_s": 0},
                        {"protocol": "pressure_v1"}, {"channel": "CH1"},
                        {"calibration": {"slope_c_per_v": 1, "intercept_c": 0,
                                         "voltage_min": 0, "voltage_max": 5}}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                validate_config(config(**updates))


class JointCaptureTests(unittest.TestCase):
    def make(self, root, reader, **opts):
        failures, events, stop = [], [], threading.Event()
        session = TemperatureSweepCapture(root, config(**opts), stop, failures,
                                         transport_factory=lambda port, baud: reader, emit=events.append,
                                         readiness_timeout_s=.3, startup_settle_s=.025)
        return session, failures, events, stop

    def test_voltage_raw_both_channels_point_association_and_hashes(self):
        reader = FakeReader()
        with tempfile.TemporaryDirectory() as root:
            session, failures, events, stop = self.make(root, reader)
            session.start()
            point = session.record_point(repeat=1, point=1, carrier_hz=5e9, tone_hz=2000,
                                         amplitude_dB=-20, point_started_monotonic_s=time.monotonic()-.1,
                                         measurement_quality={"algorithm_version": "adaptive-test",
                                                              "window_start": 500000, "window_count": 250000,
                                                              "window_duration_s": 1.0,
                                                              "raw_iq_file": "iq_point_001.npz"})
            result = session.close()
            self.assertTrue(reader.closed)
            self.assertFalse(failures)
            self.assertEqual(result["status"], "complete")
            self.assertTrue(result["serial_workers_stopped"])
            self.assertTrue(result["data_files_closed"])
            self.assertEqual(point["voltage_v"], 1.955)
            self.assertIsNone(point["temperature_c"])
            self.assertGreaterEqual(point["temperature_sample_age_s"], 0)
            self.assertTrue(any(e["event"] == "TEMP_SAMPLE" for e in events))
            with (Path(root)/"temperature"/"samples.csv").open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(set(row["channel"] for row in rows), {"S6", "S7"})
            with (Path(root)/"temperature_measurements.csv").open() as f:
                paired = list(csv.DictReader(f))
            self.assertEqual(len(paired), 1)
            self.assertEqual(paired[0]["rf_window_duration_s"], "1.0")
            self.assertEqual(paired[0]["rf_window_start"], "500000")
            self.assertEqual(paired[0]["rf_window_count"], "250000")
            self.assertEqual(paired[0]["rf_algorithm_version"], "adaptive-test")
            self.assertEqual(paired[0]["rf_raw_iq_file"], "iq_point_001.npz")
            self.assertEqual(paired[0]["temperature_c"], "")
            for item in result["files"]:
                self.assertEqual(hashlib.sha256((Path(root)/item["path"]).read_bytes()).hexdigest(), item["sha256"])

    def test_selected_channel_calibration_only_and_raw_voltage_retained(self):
        reader = FakeReader()
        calibration = {"id": "C-1", "slope_c_per_v": 10, "intercept_c": 5, "voltage_min": 2, "voltage_max": 3}
        with tempfile.TemporaryDirectory() as root:
            session, failures, events, stop = self.make(root, reader, channel="S7", calibration=calibration)
            try:
                session.start()
                point = session.record_point(repeat=1, point=1, carrier_hz=5e9, tone_hz=2000, amplitude_dB=-20)
                self.assertEqual(point["voltage_v"], 2.933)
                self.assertAlmostEqual(point["temperature_c"], 34.33)
                samples = [e for e in events if e["event"] == "TEMP_SAMPLE"]
                self.assertTrue(samples)
                self.assertTrue(all(e["temperature_c"] is None for e in samples if e["channel"] == "S6"))
            finally:
                session.close()

    def test_stale_voltage_requests_rf_stop_and_cannot_be_associated(self):
        with tempfile.TemporaryDirectory() as root:
            session, failures, events, stop = self.make(root, FakeReader(continuing=False), freshness_s=.1)
            try:
                session.start()
                self.assertTrue(stop.wait(1))
                self.assertIn("停更", failures[0])
                with self.assertRaises(TimeoutError):
                    session.record_point(repeat=1, point=1, carrier_hz=5e9, tone_hz=2000, amplitude_dB=-20)
            finally:
                result = session.close()
            self.assertEqual(result["status"], "failed_or_incomplete")

    def test_disconnect_requests_rf_stop_and_retains_partial_serial(self):
        reader = FakeReader()
        with tempfile.TemporaryDirectory() as root:
            session, failures, events, stop = self.make(root, reader)
            session.start()
            reader.disconnected = True
            self.assertTrue(stop.wait(1))
            result = session.close()
            self.assertEqual(result["status"], "failed_or_incomplete")
            self.assertGreater(result["serial_rows"], 0)
            self.assertTrue(result["serial_closed"])

    def test_bad_protocol_blocks_rf_readiness_and_seals_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            session, failures, events, stop = self.make(root, FakeReader(malformed=True))
            try:
                with self.assertRaises((RuntimeError, TimeoutError)):
                    session.start()
                self.assertFalse(session.ready)
            finally:
                result = session.close()
            self.assertEqual(result["status"], "failed_or_incomplete")
            self.assertGreater((Path(root)/"temperature"/"raw_serial.bin").stat().st_size, 0)

    def test_repeated_mcu_time_blocks_readiness(self):
        reader = FakeReader()
        reader.read = lambda timeout: b"100,400,1.955,140,600,2.933\n"
        with tempfile.TemporaryDirectory() as root:
            session, failures, events, stop = self.make(root, reader)
            try:
                with self.assertRaises(TimeoutError):
                    session.start()
                self.assertFalse(session.ready)
            finally:
                session.close()

    def test_operator_stop_before_readiness_opens_no_rf(self):
        with tempfile.TemporaryDirectory() as root:
            session, failures, events, stop = self.make(root, FakeReader())
            stop.set()
            try:
                with self.assertRaisesRegex(RuntimeError, "未启动射频"):
                    session.start()
                self.assertFalse(session.ready)
            finally:
                result = session.close()
            self.assertEqual(result["status"], "failed_or_incomplete")

    def test_startup_reset_fragment_is_retained_and_ready_after_new_segment(self):
        reader = FakeReader()
        def restarted_read(timeout=.1):
            time.sleep(.01)
            reader.count += 1
            if reader.count == 1:
                return b"716,849.81,4.154,755,555.38,2.714\r\n"
            if reader.count == 2:
                return (b"897,849.94,4.154,935,555.81,2# Temperature_Channel_Check v1; A1\r\n"
                        b"t6_ms,S6_adc,S6_V,t7_ms,S7_adc,S7_V\r\n")
            millis = (reader.count - 2) * 100
            return f"{millis},400,1.955,{millis+40},600,2.933\n".encode()
        reader.read = restarted_read
        with tempfile.TemporaryDirectory() as root:
            session, failures, events, stop = self.make(root, reader)
            session.start()
            point = session.record_point(repeat=1, point=1, carrier_hz=5e9, tone_hz=2000, amplitude_dB=-20)
            result = session.close()
            self.assertEqual(result["status"], "complete")
            self.assertEqual(point["time_segment"], 1)
            self.assertEqual(result["startup_event_counts"]["startup_fragment"], 1)
            serial = json.loads((Path(root)/"temperature"/"manifest.json").read_text())
            self.assertTrue(serial["warnings"])
            self.assertIn(b"2# Temperature_Channel_Check", (Path(root)/"temperature"/"raw_serial.bin").read_bytes())

    def test_midrun_reset_couples_rf_stop(self):
        reader = FakeReader()
        with tempfile.TemporaryDirectory() as root:
            session, failures, events, stop = self.make(root, reader)
            session.start()
            reader.read = lambda timeout: b"# Temperature_Channel_Check v1; A1\n"
            self.assertTrue(stop.wait(1))
            result = session.close()
            self.assertEqual(result["status"], "failed_or_incomplete")


class StartupBoundaryTests(unittest.TestCase):
    def test_exact_embedded_banner_is_recoverable_only_at_startup(self):
        for elapsed, expected in ((1.558, "startup_fragment"), (4.0, "interrupted_line")):
            with self.subTest(elapsed=elapsed):
                parser = LineParser("temperature_v1")
                parser.feed(b"716,849.81,4.154,755,555.38,2.714\n", host_time_utc="time", host_elapsed_s=0)
                rows, events = parser.feed(b"897,849.94,4.154,935,555.81,2# Temperature_Channel_Check v1; A1\n",
                                           host_time_utc="time", host_elapsed_s=elapsed)
                self.assertEqual(rows, [])
                self.assertIn(expected, [e["kind"] for e in events])
                self.assertIn("mcu_reset", [e["kind"] for e in events])
                self.assertEqual(parser.segment, 1)

    def test_unrecognized_corruption_does_not_become_startup_fragment(self):
        parser = LineParser("temperature_v1")
        rows, events = parser.feed(b"897,849.94,4.154,935,555.81,2# unknown firmware\n",
                                  host_time_utc="time", host_elapsed_s=1)
        self.assertEqual(rows, [])
        self.assertEqual(events[0]["kind"], "malformed_line")


class RFReadinessGateTests(unittest.TestCase):
    def test_failed_serial_preflight_never_configures_or_starts_rf(self):
        from sspp_experiments.material_sweep import acquisition as rf
        from sspp_lab.hardware import SDRConfig
        from sspp_lab.sweep_logic import SweepConfig
        with tempfile.TemporaryDirectory() as root:
            opts = SimpleNamespace(temperature_config=json.dumps(config()), headless=True,
                                   command="acquire", label="gate_test", output_root=Path(root),
                                   max_seconds=None, point_timeout=8, freq_start=5e9, freq_stop=5.3e9, points=20, repeat=3)
            manager = MagicMock()
            publisher = MagicMock()
            publisher.stats.return_value = {}
            companion = MagicMock()
            companion.start.side_effect = TimeoutError("serial not ready")
            companion.close.return_value = {"status": "failed_or_incomplete", "serial_closed": True,
                                            "serial_workers_stopped": True, "data_files_closed": True}
            companion.point_count = 0
            with patch.object(rf, "parse_args", return_value=opts), \
                 patch.object(rf, "make_config", return_value=({}, SDRConfig(), SweepConfig())), \
                 patch.object(rf, "ORIGINALS", ()), \
                 patch("signal.signal"), \
                 patch.object(rf, "RealtimePublisher", return_value=publisher), \
                 patch("sspp_lab.hardware.USRPManager", return_value=manager), \
                 patch("sspp_experiments.temperature_sweep.acquisition.TemperatureSweepCapture", return_value=companion), \
                 patch("builtins.print"):
                code = rf.main()
            self.assertEqual(code, 2)
            manager.configure.assert_not_called()
            manager.start_rx_stream.assert_not_called()
            companion.close.assert_called_once()
            manifest = json.loads(next(Path(root).glob("*/manifest.json")).read_text())
            self.assertFalse(manifest["telemetry"]["rf_start_attempted"])
            self.assertEqual(manifest["status"], "failed_or_incomplete")
            self.assertIn("sspp_experiments/temperature_pressure/protocol.py", manifest["current_source_hashes"])


if __name__ == "__main__":
    unittest.main()
