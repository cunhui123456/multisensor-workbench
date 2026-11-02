"""Window scheduling identity: preserve v5/v6/v7 history and require complete v8 policy."""
from copy import deepcopy
from dataclasses import asdict
import unittest

from sspp_experiments.comparison import (
    build_comparison, WINDOW_EFFICIENT_SWEEP_VERSION, ROUND_AWARE_SWEEP_VERSION,
    PREVIOUS_ROUND_WINDOW_POLICIES, ESCALATION_BUDGET_POLICY,
    WEAK_POINT_BUDGET_SWEEP_VERSION, WEAK_POINT_PREVIOUS_ROUND_WINDOW_POLICIES,
    WEAK_POINT_BUDGET_POLICY, FAILED_POINT_WINDOW_POLICY, WEAK_POINT_EVIDENCE_MIN_SECONDS,
)
from sspp_experiments.material_sweep.logic import SweepConfig, sweep_algorithm_policy
import test_target_quality_comparison as fixture_module


class WindowPolicyComparisonTests(unittest.TestCase):
    setUp = fixture_module.TargetQualityComparisonTests.setUp
    save = fixture_module.TargetQualityComparisonTests.save
    manifest = fixture_module.TargetQualityComparisonTests.manifest
    sweep = fixture_module.TargetQualityComparisonTests.sweep
    quality_sweep = fixture_module.TargetQualityComparisonTests.quality_sweep
    adaptive_sweep = fixture_module.TargetQualityComparisonTests.adaptive_sweep
    target_sweep = fixture_module.TargetQualityComparisonTests.target_sweep
    csv_edit = fixture_module.TargetQualityComparisonTests.csv_edit

    def legacy_v6(self, key, levels=(.02, .1, 1., 2.)):
        # Explicit historical snapshot: never derive v6 semantics from today's producer.
        manifest = self.target_sweep(key)
        manifest['sweep']['adaptive_window_seconds'] = list(levels)
        manifest['sweep_algorithm'].update(version=WINDOW_EFFICIENT_SWEEP_VERSION,
                                          adaptive_window_seconds=list(levels))
        self.save(key, 'manifest.json', manifest)
        self.csv_edit(key, lambda row: row.update(algorithm_version=WINDOW_EFFICIENT_SWEEP_VERSION))
        return manifest

    def legacy_v7(self, key, levels=(.02, .1, 1., 2.), *, reuse=True):
        # Explicit historical v7 contract, independent of the current policy producer.
        manifest = self.legacy_v6(key, levels)
        manifest['sweep']['reuse_previous_round_window'] = reuse
        manifest['sweep_algorithm'].update(
            version=ROUND_AWARE_SWEEP_VERSION, reuse_previous_round_window=reuse,
            previous_round_window_policy=PREVIOUS_ROUND_WINDOW_POLICIES[reuse],
            escalation_budget_policy=ESCALATION_BUDGET_POLICY)
        self.save(key, 'manifest.json', manifest)
        self.csv_edit(key, lambda row: row.update(algorithm_version=ROUND_AWARE_SWEEP_VERSION))
        return manifest

    def current(self, key, levels=(.02, .1, 1., 2.), *, reuse=True, extra=8., base=8.):
        manifest = self.target_sweep(key)
        cfg = SweepConfig(num_points=2, sweep_repeat=2, skip_invalid_points=True,
                          adaptive_window_seconds=levels, reuse_previous_round_window=reuse,
                          weak_point_extra_seconds=extra)
        manifest['sweep'] = asdict(cfg)
        manifest['sweep_algorithm'] = sweep_algorithm_policy(cfg)
        manifest['time_policy'] = {
            'point_timeout_seconds': base, 'weak_point_extra_seconds': extra,
            'point_max_timeout_seconds': base + extra,
            'weak_point_extension_policy': WEAK_POINT_BUDGET_POLICY, 'total_time_limit_seconds': None}
        self.assertEqual(manifest['sweep_algorithm']['version'], WEAK_POINT_BUDGET_SWEEP_VERSION)
        self.save(key, 'manifest.json', manifest)
        self.csv_edit(key, lambda row: row.update(algorithm_version=WEAK_POINT_BUDGET_SWEEP_VERSION))
        return manifest

    def overlay(self, keys=('runs/a', 'runs/b')):
        report = build_comparison(self.root, list(keys))
        self.assertFalse(report['quantitative_difference_allowed'])
        self.assertEqual(report['tables']['sweep_difference']['rows'], [])
        self.assertFalse(any(chart['id']=='sweep_difference' for chart in report['charts']))
        return report

    def test_two_complete_matching_v6_records_can_still_be_compared(self):
        for key in ('runs/a', 'runs/b'):
            self.legacy_v6(key)
        report = build_comparison(self.root, ['runs/a', 'runs/b'])
        self.assertTrue(report['quantitative_difference_allowed'])
        rule = next(row for row in report['compatibility'] if row['parameter']=='adaptive_rejections_per_level')
        self.assertEqual(rule['values'], [2., 2.])
        self.assertTrue(all(source['parameters']['sweep_algorithm_version']==WINDOW_EFFICIENT_SWEEP_VERSION
                            for source in report['sources']))
        self.assertFalse(any(row['parameter']=='reuse_previous_round_window' for row in report['compatibility']))

    def test_v5_and_v6_never_produce_cross_version_difference(self):
        self.target_sweep('runs/a')
        self.legacy_v6('runs/b')
        for keys in (['runs/a', 'runs/b'], ['runs/b', 'runs/a']):
            report = self.overlay(keys)
            self.assertTrue(any('v6' in warning and 'v5' in warning for warning in report['warnings']))

    def test_distinct_window_ladders_inside_v6_only_overlay(self):
        self.legacy_v6('runs/a')
        self.legacy_v6('runs/b', (.02, .05, .1, .25, .5, 1., 2.))
        self.overlay()

    def test_mismatched_v6_saved_policy_is_not_filled_from_current_default(self):
        self.legacy_v6('runs/a')
        manifest = self.legacy_v6('runs/b')
        manifest['sweep_algorithm']['adaptive_rejections_per_level'] = 1
        self.save('runs/b', 'manifest.json', manifest)
        self.overlay()

    def test_v6_csv_rows_must_match_manifest_version_even_for_single_record(self):
        self.legacy_v6('runs/a')
        self.csv_edit('runs/a', lambda row: row.update(algorithm_version='sspp.sweep.target_confirmed.v5'))
        with self.assertRaisesRegex(ValueError, 'algorithm_version'):
            build_comparison(self.root, ['runs/a'])

    def test_two_historical_v7_records_require_complete_matching_policy_enabled_or_disabled(self):
        for reuse in (True, False):
            with self.subTest(reuse=reuse):
                for key in ('runs/a', 'runs/b'):
                    self.legacy_v7(key, reuse=reuse)
                report = build_comparison(self.root, ['runs/a', 'runs/b'])
                self.assertTrue(report['quantitative_difference_allowed'])
                expected = {'reuse_previous_round_window':reuse,
                            'previous_round_window_policy':PREVIOUS_ROUND_WINDOW_POLICIES[reuse],
                            'escalation_budget_policy':ESCALATION_BUDGET_POLICY}
                for parameter, value in expected.items():
                    row = next(r for r in report['compatibility'] if r['parameter']==parameter)
                    self.assertEqual(row['status'], 'same')
                    self.assertEqual(row['values'], [value, value])
                self.assertIn('独立新样本', report['charts'][0]['note'])

    def test_v5_v6_and_v7_only_overlay_in_both_orders_even_with_same_source_hash(self):
        for older, fixture in (('v5', self.target_sweep), ('v6', self.legacy_v6)):
            fixture('runs/a')
            self.legacy_v7('runs/b')
            for keys in (['runs/a', 'runs/b'], ['runs/b', 'runs/a']):
                with self.subTest(older=older, keys=keys):
                    report = self.overlay(keys)
                    self.assertTrue(any('v7' in text and 'v5/v6' in text for text in report['warnings']))
                    row = next(r for r in report['compatibility'] if r['parameter']=='sweep_algorithm_version')
                    self.assertEqual(row['status'], 'different')

    def test_same_v7_ladder_but_different_reuse_policy_only_overlays(self):
        self.legacy_v7('runs/a', reuse=True)
        self.legacy_v7('runs/b', reuse=False)
        for keys in (['runs/a', 'runs/b'], ['runs/b', 'runs/a']):
            report = self.overlay(keys)
            for parameter in ('reuse_previous_round_window', 'previous_round_window_policy'):
                row = next(r for r in report['compatibility'] if r['parameter']==parameter)
                self.assertEqual(row['status'], 'different')
                self.assertTrue(row['blocking'])

    def test_historical_v7_distinct_window_ladders_remain_non_comparable(self):
        self.legacy_v7('runs/a')
        self.legacy_v7('runs/b', (.02, .05, .1, .25, .5, 1., 2.))
        self.overlay()

    def test_v7_reuse_requires_explicit_boolean_mirrors_not_equal_truthy_values(self):
        originals = {key:self.legacy_v7(key) for key in ('runs/a', 'runs/b')}
        for location in ('sweep', 'sweep_algorithm'):
            for value in (None, 0, 1, '', 'true', [], False):
                for both in (False, True):
                    with self.subTest(location=location, value=value, both=both):
                        for key, original in originals.items():
                            manifest = deepcopy(original)
                            if key=='runs/b' or both:
                                if value is None:
                                    manifest[location].pop('reuse_previous_round_window')
                                else:
                                    manifest[location]['reuse_previous_round_window'] = value
                            self.save(key, 'manifest.json', manifest)
                        report = self.overlay()
                        row = next(r for r in report['compatibility'] if r['parameter']=='reuse_previous_round_window')
                        self.assertEqual(row['status'], 'unknown')
        # Even identically invalid mirrors (including 1 == True and 0 == False)
        # cannot be treated as a matching documented policy.
        for value in (0, 1, 'false', None):
            for key, original in originals.items():
                manifest = deepcopy(original)
                for location in ('sweep', 'sweep_algorithm'):
                    manifest[location]['reuse_previous_round_window'] = value
                self.save(key, 'manifest.json', manifest)
            with self.subTest(equal_invalid_mirrors=value):
                self.overlay()

    def test_v7_policy_strings_are_required_recognized_and_consistent_with_reuse(self):
        originals = {key:self.legacy_v7(key) for key in ('runs/a', 'runs/b')}
        for parameter in ('previous_round_window_policy', 'escalation_budget_policy'):
            for value in (None, '', 'unsupported-policy', 'disabled', True, 1):
                for both in (False, True):
                    with self.subTest(parameter=parameter, value=value, both=both):
                        for key, original in originals.items():
                            manifest = deepcopy(original)
                            if key=='runs/b' or both:
                                if value is None:
                                    manifest['sweep_algorithm'].pop(parameter)
                                else:
                                    manifest['sweep_algorithm'][parameter] = value
                            self.save(key, 'manifest.json', manifest)
                        report = self.overlay()
                        row = next(r for r in report['compatibility'] if r['parameter']==parameter)
                        self.assertEqual(row['status'], 'unknown')
        for key in ('runs/a', 'runs/b'):
            manifest = self.legacy_v7(key, reuse=False)
            manifest['sweep_algorithm']['previous_round_window_policy'] = PREVIOUS_ROUND_WINDOW_POLICIES[True]
            self.save(key, 'manifest.json', manifest)
        report = self.overlay()
        row = next(r for r in report['compatibility'] if r['parameter']=='previous_round_window_policy')
        self.assertEqual(row['status'], 'unknown')

    def test_v7_csv_version_must_match_even_for_single_record(self):
        for version in (None, '', WINDOW_EFFICIENT_SWEEP_VERSION, 'sspp.sweep.target_confirmed.v5'):
            self.legacy_v7('runs/a')
            self.csv_edit('runs/a', lambda row: row.pop('algorithm_version') if version is None
                          else row.update(algorithm_version=version))
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'algorithm_version'):
                build_comparison(self.root, ['runs/a'])

    def test_matching_v8_policy_including_zero_extra_and_disabled_reuse_allows_difference(self):
        for reuse in (True, False):
            for extra in (0., 8., 30.):
                with self.subTest(reuse=reuse, extra=extra):
                    for key in ('runs/a', 'runs/b'):
                        self.current(key, reuse=reuse, extra=extra)
                    report = build_comparison(self.root, ['runs/a', 'runs/b'])
                    self.assertTrue(report['quantitative_difference_allowed'], report['compatibility'])
                    expected = {
                        'reuse_previous_round_window': reuse,
                        'previous_round_window_policy': WEAK_POINT_PREVIOUS_ROUND_WINDOW_POLICIES[reuse],
                        'escalation_budget_policy': ESCALATION_BUDGET_POLICY,
                        'weak_point_extra_seconds': extra,
                        'weak_point_evidence_min_seconds': WEAK_POINT_EVIDENCE_MIN_SECONDS,
                        'weak_point_budget_policy': WEAK_POINT_BUDGET_POLICY,
                        'failed_point_window_policy': FAILED_POINT_WINDOW_POLICY,
                        'point_timeout_seconds': 8., 'point_max_timeout_seconds': 8. + extra,
                        'weak_point_extension_policy': WEAK_POINT_BUDGET_POLICY,
                        'total_time_limit_policy': 'unlimited',
                    }
                    for parameter, value in expected.items():
                        row = next(r for r in report['compatibility'] if r['parameter'] == parameter)
                        self.assertEqual(row['values'], [value, value])
                        self.assertEqual(row['status'], 'same')
                    self.assertIn('一次额外时间', report['charts'][0]['note'])
                    self.assertIn('独立新样本', report['charts'][0]['note'])

    def test_v5_v6_v7_and_v8_only_overlay_in_both_orders(self):
        for version, fixture in (('v5', self.target_sweep), ('v6', self.legacy_v6),
                                 ('v7', self.legacy_v7)):
            fixture('runs/a')
            self.current('runs/b')
            for keys in (['runs/a', 'runs/b'], ['runs/b', 'runs/a']):
                with self.subTest(version=version, keys=keys):
                    report = self.overlay(keys)
                    self.assertTrue(any('v8' in text and 'v5/v6/v7' in text for text in report['warnings']))
                    row = next(r for r in report['compatibility'] if r['parameter'] == 'sweep_algorithm_version')
                    self.assertEqual(row['status'], 'different')

    def test_v8_different_extra_reuse_or_ladder_only_overlays(self):
        variants = ({'extra': 0.}, {'extra': 30.}, {'reuse': False}, {'base': 1.}, {'base': 30.},
                    {'levels': (.02, .05, .1, .25, .5, 1., 2.)})
        for variant in variants:
            with self.subTest(variant=variant):
                self.current('runs/a')
                self.current('runs/b', **variant)
                self.overlay()
                self.overlay(('runs/b', 'runs/a'))

    def test_v8_extra_requires_finite_nonboolean_range_and_explicit_matching_mirrors(self):
        originals = {key: self.current(key) for key in ('runs/a', 'runs/b')}
        for location in ('sweep', 'sweep_algorithm', 'time_policy', 'both'):
            for value in (None, True, False, -1, 30.01, float('nan'), float('inf'), '8', []):
                with self.subTest(location=location, value=value):
                    # Identical malformed policies must not become quantitatively comparable.
                    for key, original in originals.items():
                        manifest = deepcopy(original)
                        for where in (('sweep', 'sweep_algorithm', 'time_policy') if location == 'both' else (location,)):
                            if value is None:
                                manifest[where].pop('weak_point_extra_seconds')
                            else:
                                manifest[where]['weak_point_extra_seconds'] = value
                        self.save(key, 'manifest.json', manifest)
                    report = self.overlay()
                    row = next(r for r in report['compatibility'] if r['parameter'] == 'weak_point_extra_seconds')
                    self.assertEqual(row['status'], 'unknown')
        for location in ('sweep', 'sweep_algorithm', 'time_policy'):
            for key, original in originals.items():
                manifest = deepcopy(original)
                manifest[location]['weak_point_extra_seconds'] = 0.
                self.save(key, 'manifest.json', manifest)
            with self.subTest(mismatched_mirror=location):
                self.overlay()

    def test_v8_evidence_threshold_and_trigger_policies_are_explicit_recognized_constants(self):
        originals = {key: self.current(key) for key in ('runs/a', 'runs/b')}
        variants = {
            'weak_point_evidence_min_seconds': (None, True, False, 0., 2., '1', float('nan'), float('inf')),
            'weak_point_budget_policy': (None, '', 'disabled', 'always_extend', True),
            'failed_point_window_policy': (None, '', 'disabled', 'reuse_failed_samples', True),
            'previous_round_window_policy': (None, 'disabled', PREVIOUS_ROUND_WINDOW_POLICIES[True], True),
            'escalation_budget_policy': (None, '', 'unsupported', True),
        }
        for parameter, values in variants.items():
            for value in values:
                for both in (False, True):
                    with self.subTest(parameter=parameter, value=value, both=both):
                        for key, original in originals.items():
                            manifest = deepcopy(original)
                            if key == 'runs/b' or both:
                                if value is None:
                                    manifest['sweep_algorithm'].pop(parameter)
                                else:
                                    manifest['sweep_algorithm'][parameter] = value
                            self.save(key, 'manifest.json', manifest)
                        report = self.overlay()
                        row = next(r for r in report['compatibility'] if r['parameter'] == parameter)
                        self.assertEqual(row['status'], 'unknown')

    def test_v8_reuse_still_requires_boolean_mirrors_and_consistent_policy(self):
        originals = {key: self.current(key) for key in ('runs/a', 'runs/b')}
        for value in (None, 0, 1, 'true', []):
            with self.subTest(value=value):
                for key, original in originals.items():
                    manifest = deepcopy(original)
                    for where in ('sweep', 'sweep_algorithm'):
                        manifest[where]['reuse_previous_round_window'] = value
                    self.save(key, 'manifest.json', manifest)
                report = self.overlay()
                row = next(r for r in report['compatibility'] if r['parameter'] == 'reuse_previous_round_window')
                self.assertEqual(row['status'], 'unknown')
        for key in ('runs/a', 'runs/b'):
            manifest = self.current(key, reuse=False)
            manifest['sweep_algorithm']['previous_round_window_policy'] = WEAK_POINT_PREVIOUS_ROUND_WINDOW_POLICIES[True]
            self.save(key, 'manifest.json', manifest)
        self.overlay()

    def test_v8_csv_version_must_match_manifest_even_for_single_record(self):
        for version in (None, '', WINDOW_EFFICIENT_SWEEP_VERSION, ROUND_AWARE_SWEEP_VERSION):
            self.current('runs/a')
            self.csv_edit('runs/a', lambda row: row.pop('algorithm_version') if version is None
                          else row.update(algorithm_version=version))
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'algorithm_version'):
                build_comparison(self.root, ['runs/a'])

    def test_v8_time_policy_bounds_include_maximum_sixty_seconds(self):
        for base, extra in ((1., 0.), (30., 0.), (30., 30.)):
            with self.subTest(base=base, extra=extra):
                for key in ('runs/a', 'runs/b'):
                    self.current(key, base=base, extra=extra)
                report = build_comparison(self.root, ['runs/a', 'runs/b'])
                self.assertTrue(report['quantitative_difference_allowed'])
                row = next(r for r in report['compatibility'] if r['parameter'] == 'point_max_timeout_seconds')
                self.assertEqual(row['values'], [base + extra, base + extra])

    def test_v8_time_policy_requires_all_explicit_valid_consistent_fields(self):
        originals = {key: self.current(key) for key in ('runs/a', 'runs/b')}
        variants = {
            'point_timeout_seconds': (None, True, False, 0., 30.01, '8', float('nan'), float('inf')),
            'point_max_timeout_seconds': (None, True, False, 0., 60.01, 8., '16', float('nan'), float('inf')),
            'weak_point_extension_policy': (None, '', 'disabled', 'always_extend', True),
            'total_time_limit_seconds': ('missing', 0., 16., False, 'None'),
        }
        for parameter, values in variants.items():
            for value in values:
                for both in (False, True):
                    with self.subTest(parameter=parameter, value=value, both=both):
                        for key, original in originals.items():
                            manifest = deepcopy(original)
                            if key == 'runs/b' or both:
                                if value is None or value == 'missing':
                                    manifest['time_policy'].pop(parameter)
                                else:
                                    manifest['time_policy'][parameter] = value
                            self.save(key, 'manifest.json', manifest)
                        report = self.overlay()
                        name = 'total_time_limit_policy' if parameter == 'total_time_limit_seconds' else parameter
                        row = next(r for r in report['compatibility'] if r['parameter'] == name)
                        self.assertEqual(row['status'], 'unknown')
        for value in (None, {}, [], 'unlimited'):
            for key, original in originals.items():
                manifest = deepcopy(original)
                if value is None:
                    manifest.pop('time_policy')
                else:
                    manifest['time_policy'] = value
                self.save(key, 'manifest.json', manifest)
            with self.subTest(time_policy=value):
                self.overlay()


if __name__ == '__main__':
    unittest.main()
