"""联测服务契约：纯配置、双设备门槛、历史与原始电压导出，不连接硬件。"""
import csv
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_lab.controller import DEFAULTS, LabController, LabError, validate_request
from sspp_experiments.comparison import history_category


class TemperatureSweepServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = DesktopService(Settings(root=self.root, max_display_points=3))
        self.config = {**DEFAULTS, "profile": "own", "freq_start_ghz": 5., "freq_stop_ghz": 5.3,
                       "setup_confirmed": True, "temperature_sweep": {
                           "port": "/dev/cu.usbserial-test", "condition_label": "升温平台 1", "channel": "S6"}}

    def test_request_uses_sweep_and_forbids_wrong_mode(self):
        cfg = validate_request(self.config)
        self.assertEqual(cfg["points"], 20)
        self.assertEqual(cfg["temperature_sweep"]["protocol"], "temperature_v1")
        self.assertIsNone(cfg["temperature_sweep"]["calibration"])
        with self.assertRaisesRegex(LabError, "独立扫频"):
            validate_request(dict(self.config, mode="phase"))
        cmd = LabController(self.root).command(cfg, self.root / "profile.json", "plan")
        self.assertEqual(cmd[cmd.index("--freq-start") + 1], "5000000000.0")
        payload = json.loads(cmd[cmd.index("--temperature-config") + 1])
        self.assertEqual(payload["condition_label"], "升温平台 1")
        self.assertEqual(payload["channel"], "S6")

    def test_both_devices_required_before_launch(self):
        for ready in (set(), {"usrp"}, {"serial"}):
            with self.subTest(ready=ready), patch.object(self.service, "environment_matches", side_effect=lambda t: t["kind"] in ready), \
                 patch("sspp_lab.controller.subprocess.Popen") as launch:
                with self.assertRaises(LabError):
                    self.service.start(self.config)
                launch.assert_not_called()

    def test_serial_target_rechecked_when_constructing_acquire_command(self):
        cfg = validate_request(self.config)
        with patch.object(self.service, "environment_matches", return_value=False):
            with self.assertRaisesRegex(LabError, "温度串口"):
                self.service.command(cfg, self.root / "profile.json")
            self.assertIn("--temperature-config", self.service.command(cfg, self.root / "profile.json", "plan"))

    def test_live_voltage_is_bounded_and_invalid_samples_ignored_without_reading(self):
        self.service.job = {"config": self.config}
        with patch("pathlib.Path.open", side_effect=AssertionError("no file reads")):
            for index in range(6):
                self.service._handle_event({"event": "TEMP_SAMPLE", "host_elapsed_s": index,
                                            "voltage_v": index / 10, "temperature_c": None}, self.service.job)
            self.service._handle_event({"event": "TEMP_SAMPLE", "host_elapsed_s": 8,
                                        "voltage_v": float("nan")}, self.service.job)
            snap = self.service.live_snapshot()
        self.assertEqual([row["host_elapsed_s"] for row in snap["temperature_points"]], [3, 4, 5])
        self.assertEqual(snap["temperature_latest"]["voltage_v"], .5)
        self.assertIsNone(snap["temperature_latest"]["temperature_c"])

    def make_run(self):
        run = self.root / "Data/runs/thermal1"
        (run / "temperature").mkdir(parents=True)
        (run / "manifest.json").write_text(json.dumps({"status": "complete", "errors": [], "temperature_sweep": {"valid_points": 20}}))
        self.service.job = {"id": "test", "output": "Data/runs/thermal1", "config": self.config,
                            "status": "complete", "errors": [], "rf_stopped": True}
        self.service._save_context(self.service.job)
        with (run / "temperature/samples.csv").open("w", newline="") as source:
            writer = csv.DictWriter(source, fieldnames=["host_elapsed_s", "channel", "voltage_v", "temperature_c", "adc", "mcu_time_ms", "time_segment"])
            writer.writeheader()
            for index in range(8):
                writer.writerow({"host_elapsed_s": index, "channel": "S6", "voltage_v": .5 + index / 10,
                                 "temperature_c": None, "adc": index, "mcu_time_ms": index * 1000, "time_segment": 0})
        (run / "temperature/raw_serial.bin").write_bytes(b"unmodified serial bytes\r\n")
        (run / "temperature_measurements.csv").write_text("repeat,point,voltage_v\n1,1,0.5\n")
        return run

    def test_context_history_and_voltage_reload_preserve_classification(self):
        run = self.make_run()
        self.assertEqual(json.loads((run / "desktop_context.json").read_text())["experiment"], "temperature_sweep")
        self.assertEqual(self.service.history()[0]["category"], "temperature_sweep")
        loaded = self.service.temperature_history("runs/thermal1")
        self.assertTrue(loaded["historical"])
        self.assertEqual(loaded["stored_rows"], 8)
        self.assertEqual(loaded["temperature_points"][-1]["host_elapsed_s"], 7)
        self.assertTrue(all(row["temperature_c"] is None for row in loaded["temperature_points"]))
        self.assertNotIn("points", loaded)

    def test_temperature_samples_do_not_fill_controller_logs(self):
        self.make_run()
        process = SimpleNamespace(stdout=io.StringIO(json.dumps({"event": "TEMP_SAMPLE", "host_elapsed_s": 1., "voltage_v": .5}) + "\n"),
                                  wait=lambda: 0, poll=lambda: 0)
        self.service._collect(process, self.service.job)
        self.assertEqual(list(self.service.logs), [])
        self.assertEqual(len(self.service.temperature_points), 1)

    def test_bundle_always_includes_raw_serial_while_iq_is_optional(self):
        run = self.make_run()
        (run / "iq_point_001.npz").write_bytes(b"large-iq")
        target = self.root / "joint.zip"
        with patch("sspp_desktop.bundle.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            self.service.export_bundle("runs/thermal1", str(target), include_raw=False)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.read("results/temperature/raw_serial.bin"), b"unmodified serial bytes\r\n")
            self.assertIn("results/temperature/samples.csv", archive.namelist())
            self.assertIn("results/temperature_measurements.csv", archive.namelist())
            self.assertNotIn("results/iq_point_001.npz", archive.namelist())
            self.assertEqual(json.loads(archive.read("parameters.json"))["experiment"], "temperature_sweep")

    def test_rf_completion_does_not_mask_missing_serial_shutdown(self):
        run = self.make_run()
        manifest = {"status": "complete", "errors": [], "telemetry": {
            "tx_end_of_burst_sent": True, "rx_stop_command_sent": True, "workers_stopped": True},
            "temperature_sweep": {"status": "complete", "serial_closed": False,
                                  "serial_workers_stopped": True, "data_files_closed": True}}
        (run / "manifest.json").write_text(json.dumps(manifest))
        process = SimpleNamespace(stdout=io.StringIO(""), wait=lambda: 0, poll=lambda: 0)
        self.service._collect(process, self.service.job)
        self.assertEqual(self.service.job["status"], "failed")
        self.assertTrue(self.service.job["rf_stopped"])
        self.assertTrue(any("温度采集" in error for error in self.service.job["errors"]))

    def test_joint_readiness_failure_does_not_report_unknown_rf_shutdown(self):
        run = self.make_run()
        (run / "manifest.json").write_text(json.dumps({"status": "failed_or_incomplete",
            "errors": ["temperature not ready"], "telemetry": {"rf_start_attempted": False}}))
        self.service.job["rf_stopped"] = False
        process = SimpleNamespace(stdout=io.StringIO(""), wait=lambda: 2, poll=lambda: 2)
        self.service._collect(process, self.service.job)
        self.assertEqual(self.service.job["status"], "initialization_failed")
        self.assertFalse(self.service.job["acquisition_started"])
        self.assertFalse(self.service.job["rf_stopped"])

    def test_history_temperature_uses_saved_calibration_without_modifying_raw(self):
        run = self.make_run()
        calibration = {"id": "cal-1", "slope_c_per_v": 10., "intercept_c": 2.,
                       "voltage_min": .5, "voltage_max": 1.}
        self.config["temperature_sweep"]["calibration"] = calibration
        self.service._save_context(self.service.job)
        original = (run / "temperature/samples.csv").read_bytes()
        loaded = self.service.temperature_history("runs/thermal1")
        rows = loaded["temperature_points"]
        self.assertEqual(rows[0]["temperature_c"], 7.)
        self.assertIsNone(rows[-1]["temperature_c"])
        self.assertEqual(rows[-1]["calibration_status"], "outside_calibration_range")
        self.assertEqual((run / "temperature/samples.csv").read_bytes(), original)

    def test_history_decimation_keeps_both_interleaved_channels(self):
        run = self.make_run()
        with (run / "temperature/samples.csv").open("w", newline="") as source:
            writer = csv.DictWriter(source, fieldnames=["host_elapsed_s", "channel", "voltage_v"])
            writer.writeheader()
            for index in range(20):
                for channel in ("S6", "S7"):
                    writer.writerow({"host_elapsed_s": index, "channel": channel, "voltage_v": 1.})
        loaded = self.service.temperature_history("runs/thermal1")
        rows = loaded["temperature_points"]
        self.assertEqual({row["channel"] for row in rows}, {"S6", "S7"})
        for channel in ("S6", "S7"):
            self.assertEqual([row for row in rows if row["channel"] == channel][-1]["host_elapsed_s"], 19.)
        self.assertLessEqual(len(rows), self.service.settings.max_display_points + 2)

    def test_temperature_category_not_material(self):
        self.assertEqual(history_category({"key": "runs/x"}, {"experiment": "temperature_sweep"}), "temperature_sweep")
        self.assertEqual(history_category({"key": "runs/x"}), "material_sweep")


if __name__ == "__main__":
    unittest.main()
