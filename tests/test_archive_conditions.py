"""Archive conditions remain searchable, literal and read-only across old schemas."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sspp_desktop.archive_conditions import archive_conditions
from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService


LIQUID = {"liquid":"water", "sample_id":"bottle_A", "container":"glass_1", "geometry":"fixed_jig",
          "thermometer":"reference_1", "repeat_id":"cycle_2", "target_temperature_c":30,
          "temperature_c":29.8, "volume_ml":20, "distance_mm":0,
          "temperature_source":"operator_reference_thermometer"}


class ArchiveConditionFormattingTests(unittest.TestCase):
    def test_current_parameters_take_priority_and_all_conditions_are_searchable(self):
        context = {"sensor":"stale_sensor", "parameters":{
            "label":"water_30C", "sensor":"SSPP_07", "notes":"同一夹具\n独立重复2",
            "temperature_sweep":{"mode":"manual", "condition_label":"platform_30", "reference_temperature_c":30},
            "liquid_temperature":deepcopy(LIQUID)}}
        manifest = {"label":"old_label", "temperature_sweep":{"mode":"manual", "reference_temperature_c":90}}
        before = deepcopy((context, manifest))
        result = archive_conditions({"label":"folder_fallback"}, context, manifest)
        self.assertEqual(result["sample_name"], "water_30C")
        self.assertIn("手填参考 30 °C", result["condition_summary"])
        self.assertIn("起始参考 29.8 °C", result["condition_summary"])
        self.assertIn("距离 0 mm", result["condition_summary"])
        for value in ("SSPP_07", "platform_30", "bottle_A", "glass_1", "fixed_jig", "reference_1", "cycle_2", "独立重复2", "20 mL"):
            self.assertIn(value, result["condition_search"])
            self.assertIn(value, result["condition_details"])
        self.assertNotIn("90 °C", result["condition_details"])
        self.assertNotIn("stale_sensor", result["condition_search"])
        self.assertEqual((context, manifest), before)

    def test_continuous_uncalibrated_archive_shows_voltage_not_invented_temperature(self):
        result = archive_conditions({}, {"parameters":{"temperature_sweep":{
            "mode":"continuous", "channel":"S7", "calibration":None}}})
        self.assertEqual(result["condition_summary"], "连续电压 · S7 · V")
        self.assertIn("参考温度：—", result["condition_details"])
        self.assertIn("温度换算配置：—", result["condition_details"])
        self.assertNotIn("°C", result["condition_details"])

    def test_calibration_configuration_is_distinct_from_an_observed_temperature(self):
        result = archive_conditions({}, {"parameters":{"temperature_sweep":{
            "mode":"continuous", "channel":"S6", "calibration":{
                "id":"cal_2", "slope_c_per_v":25, "intercept_c":-30, "voltage_min":1.5, "voltage_max":4.2}}}})
        self.assertIn("连续电压 · S6 · V", result["condition_summary"])
        self.assertIn("温度换算配置编号：cal_2", result["condition_details"])
        self.assertIn("换算斜率：25 °C/V", result["condition_details"])
        self.assertIn("1.5 V 至 4.2 V", result["condition_details"])
        self.assertIn("参考温度：—", result["condition_details"])

    def test_missing_legacy_fields_remain_unknown_and_explicit_null_disables_fallback(self):
        missing = archive_conditions({"key":"runs/20261105_50C_water", "label":"20261105_50C_water"})
        self.assertEqual(missing["sample_name"], "—")
        self.assertEqual(missing["condition_summary"], "—")
        self.assertEqual(missing["condition_search"], "")
        result = archive_conditions({}, {"parameters":{"temperature_sweep":None, "liquid_temperature":None}},
                                    {"label":"actual_old_label", "temperature_sweep":{"mode":"manual", "reference_temperature_c":50},
                                     "liquid_temperature":LIQUID})
        self.assertEqual(result["sample_name"], "actual_old_label")
        self.assertEqual(result["condition_summary"], "—")
        self.assertNotIn("50 °C", result["condition_details"])

    def test_malformed_optional_fields_do_not_turn_into_numbers_or_crash_history(self):
        for value in (True, float("nan"), float("inf"), "unknown", {"bad":1}):
            with self.subTest(value=value):
                result = archive_conditions({}, {"parameters":{"temperature_sweep":{
                    "mode":"manual", "reference_temperature_c":value}}})
                self.assertEqual(result["condition_summary"], "手填参考 —")
        self.assertEqual(archive_conditions({}, {"parameters":[]}, {"temperature_sweep":[], "liquid_temperature":"bad"})["condition_summary"], "—")

    def test_manifest_only_temperature_config_and_condition_names_remain_readable(self):
        manifest = {"temperature_sweep":{"status":"complete", "config":{
            "mode":"manual", "condition_label":"legacy_platform", "reference_temperature_c":43}}}
        result = archive_conditions({}, manifest=manifest)
        self.assertEqual(result["sample_name"], "legacy_platform")
        self.assertEqual(result["condition_summary"], "手填参考 43 °C")
        disabled = archive_conditions({}, {"parameters":{"temperature_sweep":None}}, manifest)
        self.assertEqual(disabled["condition_summary"], "—")
        for value, display in (("material","材料／样品"), ("finger","手指"), ("custom_sample_A","custom_sample_A")):
            with self.subTest(condition=value):
                result = archive_conditions({}, {"parameters":{"condition":value}})
                self.assertIn("实验条件：" + display, result["condition_details"])
                self.assertIn(value, result["condition_search"])


class ArchiveConditionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = DesktopService(Settings(root=self.root))
        self.addCleanup(self.service.shutdown)

    def record(self, key, manifest, context=None, web=False):
        folder = self.root / "Data" / key
        folder.mkdir(parents=True)
        filename = "feature_report.json" if key.startswith("teacher_runs/") else "manifest.json"
        (folder / filename).write_text(json.dumps(manifest), encoding="utf-8")
        if context is not None:
            (folder / ("web_context.json" if web else "desktop_context.json")).write_text(json.dumps(context), encoding="utf-8")
        return folder

    def test_rf_index_details_and_legacy_web_conditions_match_without_reading_samples(self):
        context = {"experiment":"temperature_sweep", "parameters":{
            "label":"liquid_platform", "sensor":"SSPP_02", "notes":"cooling_repeat",
            "temperature_sweep":{"mode":"manual", "reference_temperature_c":29.8}, "liquid_temperature":LIQUID}}
        self.record("runs/combined", {"label":"original_label", "status":"complete"}, context)
        self.record("phase_runs/web", {"initial_label":"legacy_water", "status":"complete"},
                    {"experiment":"liquid_temperature_phase", "parameters":{"liquid_temperature":LIQUID}}, web=True)
        self.record("teacher_runs/teacher", {"material_input":"empty_bottle", "status":"complete"})
        self.record("runs/missing", {"status":"complete"})
        before = {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob("*.json")}
        with patch.object(self.service, "curve", return_value={}) as curve:
            rows = {row["key"]:row for row in self.service.history()}
            curve.assert_not_called()
            details = self.service.details("runs/combined")
        for key in ("sample_name", "condition_summary", "condition_details", "condition_search"):
            self.assertEqual(rows["runs/combined"][key], details[key])
        self.assertTrue(rows["runs/combined"]["has_liquid_conditions"])
        self.assertEqual(rows["runs/combined"]["category"], "temperature_sweep")
        self.assertIn("cooling_repeat", rows["runs/combined"]["condition_search"])
        self.assertIn("glass_1", rows["phase_runs/web"]["condition_search"])
        self.assertEqual(rows["teacher_runs/teacher"]["sample_name"], "empty_bottle")
        self.assertEqual(rows["runs/missing"]["sample_name"], "—")
        self.assertEqual(before, {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob("*.json")})

    def test_serial_index_and_details_retain_notes_channels_and_voltage_without_device_identity(self):
        self.record("temperature_pressure_runs/serial", {"status":"complete", "sample_name":"ink_3",
                    "notes":"卸载后静置", "protocol":"pressure_v2", "channels":["S1","S3"],
                    "pressure_calibration":{"mode":"force_intervals"},
                    "serial":{"port":"PRIVATE_DEVICE"}, "preparation":{"identity":"PRIVATE"}})
        row = self.service.history()[0]
        details = self.service.details(row["key"])
        for key in ("sample_name", "condition_summary", "condition_search"):
            self.assertIn("ink_3" if key == "sample_name" else "S1 / S3", row[key])
        self.assertIn("卸载后静置", row["condition_search"])
        self.assertIn("V（原始电压／ΔV）", details["condition_summary"])
        self.assertIn("force_intervals", details["condition_details"])
        self.assertNotIn("PRIVATE", json.dumps({key:details[key] for key in ("sample_name", "condition_summary", "condition_details", "condition_search")}))


if __name__ == "__main__":
    unittest.main()
