"""v5 comparison policy snapshots and per-row version provenance; no hardware."""
import csv
import io
import json
import unittest

from sspp_experiments.comparison import (
    ADAPTIVE_QUALITY_FIELDS, SWEEP_QUALITY_FIELDS, TARGET_CONFIRMED_SWEEP_VERSION,
    TARGET_QUALITY_RULES, build_comparison,
)
import test_comparison_domain as fixture_module


class TargetQualityComparisonTests(unittest.TestCase):
    setUp = fixture_module.ComparisonTests.setUp
    save = fixture_module.ComparisonTests.save
    manifest = fixture_module.ComparisonTests.manifest
    sweep = fixture_module.ComparisonTests.sweep
    quality_sweep = fixture_module.ComparisonTests.quality_sweep
    adaptive_sweep = fixture_module.ComparisonTests.adaptive_sweep

    def target_sweep(self, key):
        manifest = self.adaptive_sweep(key)
        sweep = manifest['sweep']
        sweep.update(skip_invalid_points=True, adaptive_window_seconds=[.02, .05, .1, .25, .5, 1., 2.])
        manifest['sweep_algorithm'].update(
            version=TARGET_CONFIRMED_SWEEP_VERSION, **TARGET_QUALITY_RULES,
            target_confirmation_snr_db=sweep['min_tone_snr_db'],
            skip_invalid_points=sweep['skip_invalid_points'],
            adaptive_window_seconds=list(sweep['adaptive_window_seconds']),
            verify_tolerance_hz=sweep['verify_tolerance_hz'], nfft_min=sweep['nfft'])
        self.save(key, 'manifest.json', manifest)
        self.csv_edit(key, lambda row: row.update(algorithm_version=TARGET_CONFIRMED_SWEEP_VERSION,
                     accepted_window_count=10000, accepted_window_duration_s=.04, adaptive_level_index=0))
        return manifest

    def csv_edit(self, key, edit):
        path = self.root/'Data'/key/'measurements.csv'
        rows = list(csv.DictReader(io.StringIO(path.read_text())))
        for row in rows:
            edit(row)
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        path.write_text(output.getvalue())

    def clone(self, value):
        return json.loads(json.dumps(value))

    def assert_overlay(self, keys=('runs/a', 'runs/b')):
        report = build_comparison(self.root, list(keys))
        self.assertFalse(report['quantitative_difference_allowed'])
        self.assertEqual(report['tables']['sweep_difference']['rows'], [])
        return report

    def test_same_complete_v5_policy_allows_difference(self):
        self.target_sweep('runs/a')
        self.target_sweep('runs/b')
        report = build_comparison(self.root, ['runs/a', 'runs/b'])
        self.assertTrue(report['quantitative_difference_allowed'])
        self.assertEqual(len(report['tables']['sweep_difference']['rows']), 2)
        for key, value in TARGET_QUALITY_RULES.items():
            row = next(r for r in report['compatibility'] if r['parameter'] == key)
            self.assertEqual(row['status'], 'same')
            self.assertEqual(row['values'], [value, value])
        self.assertTrue(any('实采窗口时长一致' in warning for warning in report['warnings']))

    def test_current_acquisition_policy_matches_comparison_registry(self):
        # Read the actual policy producer to catch drift between two independently maintained contracts.
        from dataclasses import asdict
        from sspp_experiments.material_sweep.logic import SweepConfig, sweep_algorithm_policy
        for name in ('runs/a', 'runs/b'):
            manifest = self.target_sweep(name)
            cfg = SweepConfig(num_points=2, sweep_repeat=2, skip_invalid_points=True)
            manifest['sweep'] = asdict(cfg)
            manifest['sweep_algorithm'] = sweep_algorithm_policy(cfg)
            self.save(name, 'manifest.json', manifest)
        report = build_comparison(self.root, ['runs/a', 'runs/b'])
        self.assertTrue(report['quantitative_difference_allowed'], report['compatibility'])

    def test_all_old_versions_and_v5_only_overlay_in_both_orders(self):
        self.target_sweep('runs/a')
        variants = [('legacy', self.sweep), ('v2', self.quality_sweep), ('v3', self.adaptive_sweep)]
        for label, fixture in variants:
            fixture('runs/b')
            with self.subTest(version=label):
                self.assert_overlay()
                self.assert_overlay(('runs/b', 'runs/a'))
        for version in ('sspp.sweep.bounded_missing_points.v4', 'sspp.sweep.unknown.future'):
            manifest = self.adaptive_sweep('runs/b')
            manifest['sweep_algorithm']['version'] = version
            self.save('runs/b', 'manifest.json', manifest)
            with self.subTest(version=version):
                self.assert_overlay()
                self.assert_overlay(('runs/b', 'runs/a'))

    def test_missing_changed_or_unknown_target_rules_never_allow_difference(self):
        original_a = self.target_sweep('runs/a')
        original_b = self.target_sweep('runs/b')
        for key in TARGET_QUALITY_RULES:
            for replacement in (None, 'unsupported-rule', '', 1):
                for change_both in (False, True):
                    with self.subTest(key=key, replacement=replacement, both=change_both):
                        for name, original in (('runs/a', original_a), ('runs/b', original_b)):
                            manifest = self.clone(original)
                            if name == 'runs/b' or change_both:
                                if replacement is None:
                                    manifest['sweep_algorithm'].pop(key)
                                else:
                                    manifest['sweep_algorithm'][key] = replacement
                            self.save(name, 'manifest.json', manifest)
                        self.assert_overlay()

    def test_missing_or_conflicting_numeric_mirrors_are_unknown(self):
        self.target_sweep('runs/a')
        original = self.target_sweep('runs/b')
        fields = [*SWEEP_QUALITY_FIELDS, *ADAPTIVE_QUALITY_FIELDS,
                  'verify_tolerance_hz', 'nfft']
        for key in fields:
            mirror = 'nfft_min' if key == 'nfft' else key
            for location, field in (('sweep', key), ('sweep_algorithm', mirror)):
                for missing in (False, True):
                    with self.subTest(key=key, location=location, missing=missing):
                        manifest = self.clone(original)
                        if missing:
                            manifest[location].pop(field)
                        else:
                            manifest[location][field] *= 2
                        self.save('runs/b', 'manifest.json', manifest)
                        report = self.assert_overlay()
                        row = next(r for r in report['compatibility'] if r['parameter'] == key)
                        self.assertEqual(row['status'], 'unknown')

    def test_invalid_equal_numeric_policy_values_do_not_bypass_gate(self):
        originals = {name: self.target_sweep(name) for name in ('runs/a', 'runs/b')}
        invalid = {'verify_min_seconds': .001, 'measure_min_seconds': .02,
            'verify_consecutive_windows': 1, 'min_tone_snr_db': 5,
            'previous_tone_rejection_db': -1, 'previous_tone_presence_db': 5,
            'adaptive_rejections_per_level': 3, 'adaptive_carryover_hold_seconds': 1,
            'verify_tolerance_hz': 0, 'nfft': 1.5}
        for key, value in invalid.items():
            with self.subTest(key=key):
                for name, original in originals.items():
                    manifest = self.clone(original)
                    manifest['sweep'][key] = value
                    manifest['sweep_algorithm']['nfft_min' if key == 'nfft' else key] = value
                    self.save(name, 'manifest.json', manifest)
                if key == 'verify_tolerance_hz':
                    with self.assertRaisesRegex(ValueError, '验证容差'):
                        build_comparison(self.root, ['runs/a', 'runs/b'])
                else:
                    self.assert_overlay()

    def test_confirmation_threshold_must_match_existing_target_snr(self):
        self.target_sweep('runs/a')
        original = self.target_sweep('runs/b')
        for value in (None, 5, 9, float('nan'), True):
            with self.subTest(value=value):
                manifest = self.clone(original)
                if value is None:
                    manifest['sweep_algorithm'].pop('target_confirmation_snr_db')
                else:
                    manifest['sweep_algorithm']['target_confirmation_snr_db'] = value
                self.save('runs/b', 'manifest.json', manifest)
                self.assert_overlay()
        changed = self.clone(original)
        changed['sweep']['min_tone_snr_db'] = 9
        changed['sweep_algorithm'].update(min_tone_snr_db=9, target_confirmation_snr_db=9)
        self.save('runs/b', 'manifest.json', changed)
        self.assert_overlay()

    def test_window_policies_are_complete_ordered_mirrors(self):
        self.target_sweep('runs/a')
        original = self.target_sweep('runs/b')
        for levels in (None, [], [.02, .05, .1, .25, .5], [.02, .1, .05], [.02, 2.], [.02, False]):
            for both_locations in (False, True):
                with self.subTest(levels=levels, both_locations=both_locations):
                    manifest = self.clone(original)
                    manifest['sweep']['adaptive_window_seconds'] = levels
                    if both_locations:
                        manifest['sweep_algorithm']['adaptive_window_seconds'] = levels
                    self.save('runs/b', 'manifest.json', manifest)
                    self.assert_overlay()
        manifest = self.clone(original)
        manifest['sweep_algorithm'].pop('adaptive_window_seconds')
        self.save('runs/b', 'manifest.json', manifest)
        self.assert_overlay()

    def test_two_second_limit_is_v5_only_and_over_limit_stays_invalid(self):
        self.target_sweep('runs/a')
        self.target_sweep('runs/b')
        self.assertTrue(build_comparison(self.root, ['runs/a', 'runs/b'])['quantitative_difference_allowed'])
        for maximum in (2.001, 3.):
            for name in ('runs/a', 'runs/b'):
                manifest = self.target_sweep(name)
                levels = [.02, .05, .1, .25, .5, 1., maximum]
                manifest['sweep']['adaptive_window_seconds'] = levels
                manifest['sweep_algorithm']['adaptive_window_seconds'] = levels
                self.save(name, 'manifest.json', manifest)
            with self.subTest(maximum=maximum):
                self.assert_overlay()
        for name in ('runs/a', 'runs/b'):
            self.adaptive_sweep(name, adaptive_window_seconds=[.02, .1, .25, .5, 1., 2.])
        report = self.assert_overlay()
        row = next(r for r in report['compatibility'] if r['parameter'] == 'adaptive_window_seconds')
        self.assertEqual(row['status'], 'unknown')

    def test_skip_policy_requires_boolean_matching_snapshot(self):
        self.target_sweep('runs/a')
        original = self.target_sweep('runs/b')
        for value in (None, 1, 'true', False):
            for location in ('sweep', 'sweep_algorithm'):
                with self.subTest(value=value, location=location):
                    manifest = self.clone(original)
                    if value is None:
                        manifest[location].pop('skip_invalid_points')
                    else:
                        manifest[location]['skip_invalid_points'] = value
                    self.save('runs/b', 'manifest.json', manifest)
                    self.assert_overlay()

    def test_csv_version_is_required_and_must_match_manifest(self):
        self.target_sweep('runs/a')
        for value in (None, '', 'sspp.sweep.adaptive_coherent_quality.v3', 'sspp.sweep.unknown.future'):
            with self.subTest(value=value):
                self.target_sweep('runs/b')
                self.csv_edit('runs/b', lambda row: row.pop('algorithm_version') if value is None else row.update(algorithm_version=value))
                with self.assertRaisesRegex(ValueError, 'algorithm_version'):
                    build_comparison(self.root, ['runs/a', 'runs/b'])

    def test_actual_window_differences_are_preserved_without_changing_policy(self):
        self.target_sweep('runs/a')
        self.target_sweep('runs/b')
        self.csv_edit('runs/b', lambda row: row.update(accepted_window_count=250000,
            accepted_window_duration_s=1., adaptive_level_index=5))
        report = build_comparison(self.root, ['runs/a', 'runs/b'])
        self.assertTrue(report['quantitative_difference_allowed'])
        rows = report['tables']['sweep_measurements']['rows']
        self.assertEqual({r['accepted_window_duration_s'] for r in rows}, {.04, 1.})

    def test_gap_records_remain_outside_complete_comparison_contract(self):
        self.target_sweep('runs/a')
        manifest = self.target_sweep('runs/b')
        manifest['status'] = 'complete_with_gaps'
        self.save('runs/b', 'manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, '未完成'):
            build_comparison(self.root, ['runs/a', 'runs/b'])


if __name__ == '__main__':
    unittest.main()
