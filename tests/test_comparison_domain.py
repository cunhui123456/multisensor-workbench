"""Offline comparison contracts: no UHD, devices, Qt or source mutations."""
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from sspp_experiments.comparison import build_comparison, history_category
from sspp_experiments.finger_displacement.comparison import _plot_points


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def save(self, key, name, value):
        path = self.root / "Data" / key / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def manifest(self, key, started="2026-10-10T01:00:00Z"):
        return {"status": "complete", "errors": [], "label": key.rsplit("/", 1)[-1], "started_utc": started,
                "telemetry": {"tx_end_of_burst_sent": True, "rx_stop_command_sent": True, "workers_stopped": True},
                "actual_sdr": {"rx_rate_actual": 250000., "tx_rate_actual": 250000., "channel": 0, "freq": 2.4e9, "tone": 10000.},
                "actual_hardware": {"tx_gain": 20., "rx_gain": 30., "tx_antenna": "TX/RX", "rx_antenna": "RX2", "master_clock_rate": 32e6},
                "requested_sdr": {"amp": .3}, "device_profile": {"serial": "PRIVATE-IDENTITY", "fpga_sha256": "PRIVATE-FPGA"},
                "current_source_hashes": {"logic.py": "algo-hash"}, "phase_source_hashes": {"sspp_lab/dsp.py": "dsp-hash"}}

    def sweep(self, key, values=((-40., -30.), (-42., -32.)), frequencies=(2.4e9, 2.5e9), tones=(2000., 3000.), started="2026-10-10T01:00:00Z"):
        manifest = self.manifest(key, started)
        manifest.update(sweep={"nfft": 2048, "verify_cycles": 20, "measure_cycles": 80, "verify_tolerance_hz": 200.,
                               "switch_settle_time": .1, "sweep_repeat": len(values), "num_points": len(frequencies)},
                        quality={"complete": True, "measurement_tone_failures": 0, "counts": [len(frequencies)]*len(values)}, raw_files=[])
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["repeat", "point", "requested_carrier_GHz", "expected_tone_Hz", "measured_tone_Hz", "peak_amplitude_dBFS_est"])
        for rep, amplitudes in enumerate(values, 1):
            for point, (frequency, tone, amplitude) in enumerate(zip(frequencies, tones, amplitudes), 1):
                writer.writerow([rep, point, frequency/1e9, tone, tone, amplitude])
                manifest["raw_files"].append({"point_number": (rep-1)*len(frequencies)+point,
                    "requested_carrier_hz": frequency, "tx_actual_carrier_hz": frequency,
                    "rx_actual_carrier_hz": frequency, "tone_hz": tone})
        self.save(key, "manifest.json", manifest)
        (self.root / "Data" / key / "measurements.csv").write_text(stream.getvalue())
        return manifest

    def phase(self, key, baseline_end=2., offset=0., count=30, finger=False):
        manifest = self.manifest(key)
        self.save(key, "manifest.json", manifest)
        if finger:
            self.save(key, "desktop_context.json", {"experiment": "finger_displacement"})
        self.save(key, "baseline.json", {"start_s": baseline_end-1, "end_s": baseline_end,
            "reference_amplitude": .01, "reference_phase_unwrapped_deg": 100.,
            "min_coherent_fraction": .95, "amplitude_sd_dB": .03,
            "phase_linear_drift_deg_per_s": .1, "phase_residual_sd_deg": .2})
        rows = [{"t_s": baseline_end+.1*i, "start_sample": i*25000, "count": 25000,
                 "relative_amplitude_dB": None if i == 0 else offset+i*.1,
                 "relative_phase_deg": None if i == 0 else offset+i,
                 "coherent_fraction": .95, "label": "transition_to_sample" if i == 2 else "sample"} for i in range(count)]
        self.write_rows(key, rows)
        return rows

    def write_rows(self, key, rows):
        (self.root / "Data" / key / "amplitude_phase.jsonl").write_text("\n".join(json.dumps(r) for r in rows)+"\n")

    def pair(self):
        self.sweep("runs/a")
        self.sweep("runs/b", ((-37., -25.), (-39., -27.)), started="2026-10-10T02:00:00Z")
        return ["runs/a", "runs/b"]

    def test_temperature_sweeps_keep_category_conditions_and_serial_evidence(self):
        keys = self.pair()
        for index, key in enumerate(keys):
            self.save(key, "desktop_context.json", {"experiment": "temperature_sweep", "parameters": {
                "temperature_sweep": {"condition_label": f"平台 {index}", "channel": "S6", "calibration": None,
                                      "reference_temperature_c": None}}})
            thermal = {"status": "complete", "serial_workers_stopped": True, "serial_closed": True, "data_files_closed": True}
            manifest = json.loads((self.root / "Data" / key / "manifest.json").read_text())
            manifest["temperature_sweep"] = thermal
            self.save(key, "manifest.json", manifest)
            self.save(key, "temperature/manifest.json", thermal)
            root = self.root / "Data" / key
            (root / "temperature/samples.csv").write_text("host_elapsed_s,voltage_v\n0,0.5\n")
            (root / "temperature_measurements.csv").write_text("repeat,point,voltage_v\n1,1,0.5\n")
        report = build_comparison(self.root, keys)
        self.assertEqual(report["kind"], "temperature_sweep")
        self.assertEqual(report["tables"]["temperature_conditions"][0]["condition_label"], "平台 0")
        self.assertTrue(any("平台 0" in curve["label"] for chart in report["charts"] for curve in chart["series"]))
        self.assertTrue(any("未标定电压" in warning for warning in report["warnings"]))
        self.assertIn("temperature/samples.csv", [entry["relative_path"] for entry in report["sources"][0]["files"]])
        self.save(keys[1], "desktop_context.json", {"experiment": "material_sweep"})
        with self.assertRaisesRegex(ValueError, "同一种"):
            build_comparison(self.root, keys)

    def quality_sweep(self, key, **policy_changes):
        manifest = self.sweep(key)
        policy = {"verify_min_seconds": .02, "measure_min_seconds": .04,
                  "verify_consecutive_windows": 2, "min_tone_snr_db": 6.,
                  "previous_tone_rejection_db": 6., **policy_changes}
        manifest["sweep_algorithm"] = {"version": "sspp.sweep.full_window_quality.v2", **policy}
        manifest["sweep"].update(policy)
        self.save(key, "manifest.json", manifest)
        return manifest

    def adaptive_sweep(self, key, **policy_changes):
        manifest = self.quality_sweep(key)
        policy = {"previous_tone_presence_db": 6., "adaptive_rejections_per_level": 2,
                  "adaptive_carryover_hold_seconds": .250,
                  "adaptive_window_seconds": [.02, .1, .25, .5, 1.], **policy_changes}
        manifest["sweep"].update(policy)
        manifest["sweep_algorithm"].update(version="sspp.sweep.adaptive_coherent_quality.v3", **policy)
        self.save(key, "manifest.json", manifest)
        return manifest

    def test_new_and_legacy_estimators_only_overlay_in_both_orders(self):
        self.sweep("runs/old")
        self.quality_sweep("runs/new")
        # 故意保留相同源码哈希夹具，确保版本本身能阻止混合定量差值。
        for keys in (["runs/old", "runs/new"], ["runs/new", "runs/old"]):
            with self.subTest(keys=keys):
                report = build_comparison(self.root, keys)
                self.assertFalse(report["quantitative_difference_allowed"])
                self.assertEqual(report["tables"]["sweep_difference"]["rows"], [])
                self.assertTrue(any("新旧扫频估计器混合" in w for w in report["warnings"]))
                row = next(r for r in report["compatibility"] if r["parameter"] == "sweep_algorithm_version")
                self.assertEqual(row["status"], "different")

    def test_new_estimator_same_policy_allows_difference(self):
        self.quality_sweep("runs/new1")
        self.quality_sweep("runs/new2")
        report = build_comparison(self.root, ["runs/new1", "runs/new2"])
        self.assertTrue(report["quantitative_difference_allowed"])
        self.assertEqual(len(report["tables"]["sweep_difference"]["rows"]), 2)

    def test_new_estimator_different_or_missing_quality_policy_only_overlay(self):
        self.quality_sweep("runs/new1")
        baseline = self.quality_sweep("runs/new2")
        for parameter in ("verify_min_seconds", "measure_min_seconds", "verify_consecutive_windows",
                          "min_tone_snr_db", "previous_tone_rejection_db"):
            for missing in (False, True):
                with self.subTest(parameter=parameter, missing=missing):
                    manifest = json.loads(json.dumps(baseline))
                    if missing:
                        manifest["sweep"].pop(parameter)
                    else:
                        manifest["sweep"][parameter] *= 2
                    self.save("runs/new2", "manifest.json", manifest)
                    report = build_comparison(self.root, ["runs/new1", "runs/new2"])
                    self.assertFalse(report["quantitative_difference_allowed"])
                    self.assertEqual(report["tables"]["sweep_difference"]["rows"], [])
        baseline["sweep_algorithm"] = None
        self.save("runs/new2", "manifest.json", baseline)
        self.assertFalse(build_comparison(self.root, ["runs/new1", "runs/new2"])["quantitative_difference_allowed"])

    def test_adaptive_estimator_same_complete_policy_allows_difference(self):
        self.adaptive_sweep("runs/a")
        self.adaptive_sweep("runs/b")
        report = build_comparison(self.root, ["runs/a", "runs/b"])
        self.assertTrue(report["quantitative_difference_allowed"])
        row = next(r for r in report["compatibility"] if r["parameter"] == "adaptive_window_seconds")
        self.assertEqual(row["values"], [[.02, .1, .25, .5, 1.]] * 2)
        self.assertEqual(row["status"], "same")

    def test_same_adaptive_policy_retains_different_actual_windows(self):
        for key, count, duration, level in (("runs/a", 10000, .04, 0), ("runs/b", 250000, 1., 4)):
            self.adaptive_sweep(key)
            path = self.root / "Data" / key / "measurements.csv"
            rows = list(csv.DictReader(io.StringIO(path.read_text())))
            for row in rows:
                row.update(accepted_window_count=count, accepted_window_duration_s=duration, adaptive_level_index=level)
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            path.write_text(stream.getvalue())
        report = build_comparison(self.root, ["runs/a", "runs/b"])
        self.assertTrue(report["quantitative_difference_allowed"])
        rows = report["tables"]["sweep_measurements"]["rows"]
        self.assertEqual({(r["key"], r["accepted_window_count"], r["accepted_window_duration_s"], r["adaptive_level_index"]) for r in rows},
                         {("runs/a", 10000, .04, 0), ("runs/b", 250000, 1., 4)})
        self.assertTrue(any("实采窗口时长一致" in warning for warning in report["warnings"]))

    def test_adaptive_mixed_versions_only_overlay_in_both_orders(self):
        self.adaptive_sweep("runs/adaptive")
        self.quality_sweep("runs/fixed")
        self.sweep("runs/legacy")
        for other, warning in (("fixed", "扫频质量策略版本混合"), ("legacy", "新旧扫频估计器混合")):
            for keys in (["runs/adaptive", f"runs/{other}"], [f"runs/{other}", "runs/adaptive"]):
                with self.subTest(keys=keys):
                    report = build_comparison(self.root, keys)
                    self.assertFalse(report["quantitative_difference_allowed"])
                    self.assertEqual(report["tables"]["sweep_difference"]["rows"], [])
                    self.assertTrue(any(warning in text for text in report["warnings"]))
                    row = next(r for r in report["compatibility"] if r["parameter"] == "adaptive_window_seconds")
                    self.assertEqual(row["status"], "unknown")

    def test_adaptive_policy_differences_and_missing_values_only_overlay(self):
        self.adaptive_sweep("runs/a")
        baseline = self.adaptive_sweep("runs/b")
        changes = {"previous_tone_presence_db": 9., "adaptive_rejections_per_level": 3,
                   "adaptive_carryover_hold_seconds": .500,
                   "adaptive_window_seconds": [.02, .2, .5, 1.]}
        for parameter, changed in changes.items():
            for missing in (False, True):
                with self.subTest(parameter=parameter, missing=missing):
                    manifest = json.loads(json.dumps(baseline))
                    if missing:
                        manifest["sweep"].pop(parameter)
                    else:
                        manifest["sweep"][parameter] = changed
                    self.save("runs/b", "manifest.json", manifest)
                    for keys in (["runs/a", "runs/b"], ["runs/b", "runs/a"]):
                        report = build_comparison(self.root, keys)
                        self.assertFalse(report["quantitative_difference_allowed"])
                        self.assertEqual(report["tables"]["sweep_difference"]["rows"], [])

    def test_invalid_adaptive_windows_and_unknown_versions_are_not_comparable(self):
        for levels in (None, [], ".02,.1,.25,.5,1", [.02, .1, .1], [.02, .1, False],
                       [.02, .1, float("nan")], [.001, .1], [.02, 2.]):
            with self.subTest(levels=levels):
                self.adaptive_sweep("runs/a", adaptive_window_seconds=levels)
                self.adaptive_sweep("runs/b", adaptive_window_seconds=levels)
                report = build_comparison(self.root, ["runs/a", "runs/b"])
                self.assertFalse(report["quantitative_difference_allowed"])
                row = next(r for r in report["compatibility"] if r["parameter"] == "adaptive_window_seconds")
                self.assertEqual(row["status"], "unknown")
        for key in ("runs/a", "runs/b"):
            manifest = self.quality_sweep(key)
            manifest["sweep_algorithm"]["version"] = "sspp.sweep.unknown.future"
            self.save(key, "manifest.json", manifest)
        self.assertFalse(build_comparison(self.root, ["runs/a", "runs/b"])["quantitative_difference_allowed"])

    def test_explicit_legacy_version_does_not_require_new_policy(self):
        self.sweep("runs/a")
        manifest = self.sweep("runs/b")
        manifest["sweep_algorithm"] = {"version": "legacy.truncated_window_fft.v1"}
        self.save("runs/b", "manifest.json", manifest)
        self.assertTrue(build_comparison(self.root, ["runs/a", "runs/b"])["quantitative_difference_allowed"])

    def test_means_sample_sd_reference_sign_and_privacy(self):
        keys = self.pair()
        report = build_comparison(self.root, keys[::-1])
        self.assertEqual(report["reference_key"], keys[0])
        self.assertTrue(report["quantitative_difference_allowed"])
        rows = report["tables"]["sweep_difference"]["rows"]
        self.assertEqual([r["difference_dB"] for r in rows], [3., 5.])
        self.assertAlmostEqual(rows[0]["combined_repeat_sd_dB"], 2.)
        self.assertEqual(report["tables"]["sweep_summary"]["rows"][0]["mean_amplitude_dB"], -38.)
        self.assertAlmostEqual(report["tables"]["sweep_summary"]["rows"][0]["repeat_sd_dB"], 2**.5)
        self.assertNotIn("PRIVATE-", json.dumps(report))
        self.assertTrue(any("机械位置" in w for w in report["warnings"]))

    def test_explicit_reference_and_single_repeat(self):
        self.sweep("runs/a", ((-40., -30.),))
        self.sweep("runs/b", ((-37., -25.),))
        report = build_comparison(self.root, ["runs/a", "runs/b"], "runs/b")
        self.assertEqual([r["difference_dB"] for r in report["tables"]["sweep_difference"]["rows"]], [-3., -5.])
        self.assertTrue(all(r["repeat_sd_dB"] is None for r in report["tables"]["sweep_summary"]["rows"]))
        self.assertTrue(all(r["combined_repeat_sd_dB"] is None for r in report["tables"]["sweep_difference"]["rows"]))

    def test_parameters_mismatch_and_unknown_allow_only_overlay(self):
        keys = self.pair()
        path = self.root / "Data/runs/b/manifest.json"
        original = json.loads(path.read_text())
        for change in ("gain", "identity", "actual"):
            with self.subTest(change=change):
                m = json.loads(json.dumps(original))
                if change == "gain":
                    m["actual_hardware"]["rx_gain"] += 1
                elif change == "identity":
                    m["device_profile"].pop("serial")
                else:
                    m["raw_files"][0].pop("rx_actual_carrier_hz")
                self.save("runs/b", "manifest.json", m)
                report = build_comparison(self.root, keys)
                self.assertFalse(report["quantitative_difference_allowed"])
                self.assertEqual(len(report["charts"]), 2)
                self.assertEqual(report["tables"]["sweep_difference"]["rows"], [])

    def test_partial_grid_intersection_uses_actual_carrier_and_tone(self):
        self.sweep("runs/a")
        self.sweep("runs/b", frequencies=(2.5e9, 2.6e9), tones=(3000., 4000.))
        report = build_comparison(self.root, ["runs/a", "runs/b"])
        self.assertEqual(len(report["tables"]["sweep_difference"]["rows"]), 1)
        self.assertEqual(report["tables"]["sweep_difference"]["rows"][0]["actual_tx_carrier_hz"], 2.5e9)
        self.assertEqual(len(report["tables"]["sweep_summary"]["rows"]), 4)

    def test_equal_total_rf_does_not_match_different_tones(self):
        self.sweep("runs/a", ((-40.,),), frequencies=(2.4e9,), tones=(2000.,))
        self.sweep("runs/b", ((-40.,),), frequencies=(2.4e9-1000,), tones=(3000.,))
        report = build_comparison(self.root, ["runs/a", "runs/b"])
        self.assertFalse(report["quantitative_difference_allowed"])
        self.assertEqual(report["tables"]["sweep_difference"]["rows"], [])

    def test_sources_hash_exact_bytes(self):
        report = build_comparison(self.root, self.pair())
        for source in report["sources"]:
            for entry in source["files"]:
                content = (self.root / "Data" / source["key"] / entry["relative_path"]).read_bytes()
                self.assertEqual(entry["bytes"], len(content))
                self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())

    def test_selection_limits_duplicates_and_invalid_reference(self):
        for keys, reference in (([], None), (["runs/a"]*2, None),
                                ([f"runs/{i}" for i in range(9)], None), (["runs/a", "runs/b"], "runs/c"),
                                (["runs/a", "../b"], None)):
            with self.subTest(keys=keys, reference=reference), self.assertRaises(ValueError):
                build_comparison(self.root, keys, reference)

    def test_stop_errors_completion_gates(self):
        keys = self.pair()
        original = json.loads((self.root / "Data/runs/b/manifest.json").read_text())
        for status, errors, stop in (("failed", [], True), ("complete", ["overflow"], True), ("complete", [], False)):
            with self.subTest(status=status, errors=errors, stop=stop), self.assertRaises(ValueError):
                m = json.loads(json.dumps(original))
                m.update(status=status, errors=errors)
                m["telemetry"]["workers_stopped"] = stop
                self.save("runs/b", "manifest.json", m)
                build_comparison(self.root, keys)

    def test_invalid_sweep_rows_rejected(self):
        keys = self.pair()
        path = self.root / "Data/runs/b/measurements.csv"
        original = path.read_text()
        variants = [original+original.splitlines()[1]+"\n", "\n".join(original.splitlines()[:-1])+"\n",
                    original.replace("-37.0", "nan"), original.replace("2000.0,2000.0", "2000.0,2500.0", 1)]
        for changed in variants:
            with self.subTest(changed=changed[-70:]), self.assertRaises(ValueError):
                path.write_text(changed)
                build_comparison(self.root, keys)

    def test_actual_point_metadata_mismatch_rejected(self):
        keys = self.pair()
        m = json.loads((self.root / "Data/runs/b/manifest.json").read_text())
        m["raw_files"][0]["tone_hz"] = 9999
        self.save("runs/b", "manifest.json", m)
        with self.assertRaises(ValueError):
            build_comparison(self.root, keys)

    def test_symlink_source_rejected(self):
        keys = self.pair()
        path = self.root / "Data/runs/b/measurements.csv"
        content = path.read_bytes()
        path.unlink()
        outside = self.root / "foreign.csv"
        outside.write_bytes(content)
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            build_comparison(self.root, keys)

    def test_teacher_rejected_and_categories(self):
        self.save("teacher_runs/a", "feature_report.json", {"status": "complete"})
        with self.assertRaisesRegex(ValueError, "老师历史"):
            build_comparison(self.root, ["teacher_runs/a", "teacher_runs/b"])
        self.assertEqual(history_category({"key": "runs/a"}), "material_sweep")
        self.assertEqual(history_category({"key": "phase_runs/a"}), "phase_history")
        self.assertEqual(history_category({"key": "phase_runs/a"}, {"experiment": "finger_displacement"}), "finger_displacement")

    def test_phase_alignment_does_not_change_amplitude_or_phase(self):
        self.phase("phase_runs/a", baseline_end=2.)
        self.phase("phase_runs/b", baseline_end=7., offset=100.)
        report = build_comparison(self.root, ["phase_runs/a", "phase_runs/b"])
        self.assertEqual(report["kind"], "phase_history")
        self.assertFalse(report["quantitative_difference_allowed"])
        plot = report["charts"][1]
        self.assertAlmostEqual(plot["series"][0]["data"][0][0], .1)
        self.assertAlmostEqual(plot["series"][1]["data"][0][0], .1)
        self.assertEqual(plot["series"][0]["data"][0][1], 1.)
        self.assertEqual(plot["series"][1]["data"][0][1], 101.)
        self.assertEqual(report["metrics"][0]["transition_windows"], 1)
        self.assertEqual(len(report["tables"]["phase_curves"]["rows"]), 58)
        self.assertNotIn("estimated_distance_mm", json.dumps(report))

    def test_mixed_kinds_rejected(self):
        self.phase("phase_runs/a")
        self.phase("phase_runs/b", finger=True)
        with self.assertRaisesRegex(ValueError, "同一种"):
            build_comparison(self.root, ["phase_runs/a", "phase_runs/b"])
        self.sweep("runs/a")
        with self.assertRaisesRegex(ValueError, "同一种"):
            build_comparison(self.root, ["phase_runs/a", "runs/a"])

    def test_phase_gaps_missing_and_low_coherence_preserved(self):
        rows = self.phase("phase_runs/a")
        self.phase("phase_runs/b")
        rows[5]["relative_phase_deg"] = None
        rows[8]["coherent_fraction"] = .1
        del rows[12]
        self.write_rows("phase_runs/a", rows)
        report = build_comparison(self.root, ["phase_runs/a", "phase_runs/b"])
        self.assertEqual(report["metrics"][0]["sample_gaps"], 1)
        self.assertEqual(report["metrics"][0]["missing_relative_windows"], 1)
        self.assertEqual(report["metrics"][0]["low_coherence_windows"], 1)
        self.assertEqual(sum(y is None for _, y in report["charts"][1]["series"][0]["data"]), 2)

    def test_phase_invalid_baseline_and_corrupt_rows_rejected(self):
        self.phase("phase_runs/a")
        self.phase("phase_runs/b")
        baseline_path = self.root / "Data/phase_runs/a/baseline.json"
        baseline = json.loads(baseline_path.read_text())
        for field, value in (("amplitude_sd_dB", .6), ("min_coherent_fraction", .4), ("reference_amplitude", 0)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.save("phase_runs/a", "baseline.json", {**baseline, field: value})
                build_comparison(self.root, ["phase_runs/a", "phase_runs/b"])
        self.save("phase_runs/a", "baseline.json", baseline)
        path = self.root / "Data/phase_runs/a/amplitude_phase.jsonl"
        original = path.read_text()
        for changed in (original+"{broken}", original+original.splitlines()[-1]+"\n", original.replace('"relative_phase_deg": 1.0', '"relative_phase_deg": NaN', 1)):
            with self.subTest(changed=changed[-50:]), self.assertRaises(ValueError):
                path.write_text(changed)
                build_comparison(self.root, ["phase_runs/a", "phase_runs/b"])

    def test_bounded_display_keeps_spike_and_gap(self):
        data = [[float(i), 9999. if i == 10000 else (None if 13000 <= i < 14000 else .1)] for i in range(50000)]
        sampled = _plot_points(data)
        self.assertLessEqual(len(sampled), 2500)
        self.assertIn([10000., 9999.], sampled)
        self.assertTrue(any(y is None for _, y in sampled))
        for left, right in zip(sampled, sampled[1:]):
            if left[1] is not None and right[1] is not None:
                self.assertFalse(left[0] < 13000 and right[0] >= 14000)


if __name__ == "__main__":
    unittest.main()
