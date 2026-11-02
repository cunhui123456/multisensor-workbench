"""Timeout hints require real long-window evidence and never carry acceptance or IQ."""

import io
import math
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import numpy as np

from sspp_experiments.material_sweep import logic
from test_sweep_window_v7 import BudgetReplay, RATE, needs_one_second, tone


def one_long_pass_then_failure():
    # The 0.12--1.12 s VERIFY contains a strong target, then it disappears.
    return np.concatenate((tone(12000., .22), tone(seconds=.9), tone(12000., 6.88)))


def one_maximum_window_pass_then_failure():
    # Both 1 s windows fail, the extended 1.12--3.12 s window passes, later windows fail.
    return np.concatenate((tone(12000., 2.12), tone(seconds=1.), tone(12000., 4.88)))


class QualifiedWeakWindowHintTests(unittest.TestCase):
    def run_quietly(self, runner):
        with redirect_stdout(io.StringIO()), patch.object(logic.time, 'sleep'):
            return runner.run(enable_plot=False)

    @staticmethod
    def rows(runner, repeat):
        return [r for r in runner.records if r['repeat'] == repeat]

    def test_policy_and_extra_budget_validation_are_explicit(self):
        self.assertEqual(logic.SWEEP_ALGORITHM_VERSION, 'sspp.sweep.target_confirmed.v8')
        self.assertEqual(logic.SweepConfig().weak_point_extra_seconds, 8.)
        for extra in (0, 8., 30.):
            cfg = logic.SweepConfig(weak_point_extra_seconds=extra)
            cfg.validate(RATE)
            policy = logic.sweep_algorithm_policy(cfg)
            self.assertEqual(policy['weak_point_extra_seconds'], extra)
            self.assertEqual(policy['weak_point_evidence_min_seconds'], 1.)
            self.assertEqual(policy['weak_point_budget_policy'],
                             'one_extension_after_qualified_long_verify_before_base_deadline')
            self.assertEqual(policy['failed_point_window_policy'],
                             'next_level_after_long_verified_window_on_timeout')
            self.assertEqual(policy['previous_round_window_policy'],
                             'previous_round_success_or_qualified_weak_confirmation_level_only')
        for value in (True, False, -1, 30.01, float('inf'), float('-inf'), float('nan'), '8', None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'weak_point_extra_seconds'):
                logic.SweepConfig(weak_point_extra_seconds=value).validate(RATE)

    def test_failed_long_confirmation_hints_next_level_but_requires_three_new_windows(self):
        second = tone(seconds=6.2, amplitude=.02)
        runner = BudgetReplay([one_long_pass_then_failure(), second])
        result = self.run_quietly(runner)
        self.assertIsNone(result['all_amp_records'][0][0])
        source = next(r for r in self.rows(runner, 1)
                      if r['state'] == 'VERIFY' and r['passed'] and r['window_duration_s'] == 1.)
        rows = self.rows(runner, 2)
        first = rows[0]
        self.assertEqual(first['window_source'], 'previous_round_weak_confirmation')
        self.assertEqual(first['window_hint_source_kind'], 'previous_round_weak_confirmation')
        self.assertEqual(first['window_hint_source_round'], 1)
        self.assertEqual(first['window_hint_source_attempt'], source['quality_attempt_index'])
        self.assertEqual(first['window_hint_source_state'], 'VERIFY')
        self.assertEqual(first['window_hint_observed_level_index'], 2)
        self.assertEqual(first['window_hint_observed_window_seconds'], 1.)
        self.assertEqual(first['point_initial_level_index'], 3)
        self.assertEqual(first['point_initial_window_seconds'], 2.)
        self.assertEqual([r['window_start'] for r in rows], [0, 500000, 1000000])
        self.assertEqual([r['state'] for r in rows], ['VERIFY', 'VERIFY', 'MEASURE'])
        self.assertEqual([r['verified_windows_before'] for r in rows], [0, 1, 2])
        self.assertEqual([r['accepted'] for r in rows], [False, False, True])
        for r in rows:
            a, n = r['window_start'], r['window_count']
            checked = logic.quality_measurement(second[a:a+n], RATE, 2000., 2000., runner.cfg)
            self.assertTrue(checked['passed'])
            self.assertAlmostEqual(r['amplitude_dB'], checked['amplitude_dB'], places=10)
        self.assertAlmostEqual(result['all_amp_records'][1][0], 20*math.log10(.02), places=5)
        self.assertTrue(any(p['window_hint_source_attempt'] == source['quality_attempt_index']
                            for p in runner.progress if p['window_source'] == 'previous_round_weak_confirmation'))

    def test_maximum_evidenced_window_is_capped_at_highest_configured_level(self):
        runner = BudgetReplay([one_maximum_window_pass_then_failure(), tone(seconds=6.2)])
        result = self.run_quietly(runner)
        self.assertIsNone(result['all_amp_records'][0][0])
        first = self.rows(runner, 2)[0]
        self.assertEqual(first['window_source'], 'previous_round_weak_confirmation')
        self.assertEqual(first['window_hint_observed_level_index'], 3)
        self.assertEqual(first['window_hint_observed_window_seconds'], 2.)
        self.assertEqual(first['point_initial_level_index'], 3)

    def test_highest_qualified_level_wins_over_an_earlier_shorter_confirmation(self):
        first = np.concatenate((tone(12000., .22), tone(seconds=.9), tone(12000., 2.),
                                tone(seconds=1.), tone(12000., 11.88)))
        runner = BudgetReplay([first, tone(seconds=6.2)], budgets=[16., 8.])
        result = self.run_quietly(runner)
        self.assertIsNone(result['all_amp_records'][0][0])
        evidence = [r for r in self.rows(runner, 1)
                    if r['state'] == 'VERIFY' and r['passed'] and r['window_duration_s'] >= 1.]
        self.assertEqual({r['adaptive_level_index'] for r in evidence}, {2, 3})
        next_round = self.rows(runner, 2)[0]
        self.assertEqual(next_round['window_hint_observed_level_index'], 3)
        self.assertEqual(next_round['window_hint_observed_window_seconds'], 2.)
        self.assertEqual(next_round['window_hint_source_attempt'], evidence[-1]['quality_attempt_index'])
        self.assertEqual(next_round['point_initial_level_index'], 3)

    def test_no_target_or_only_short_pass_never_generates_weak_hint(self):
        for first in (tone(12000., 8.), np.concatenate((tone(seconds=.02), tone(12000., 7.98)))):
            with self.subTest(initial_short_pass=not np.all(first[:5000] == tone(12000., .02))):
                runner = BudgetReplay([first, tone(seconds=.2)])
                result = self.run_quietly(runner)
                self.assertIsNone(result['all_amp_records'][0][0])
                self.assertFalse(any(r['passed'] and r['window_duration_s'] >= 1. for r in self.rows(runner, 1)))
                self.assertEqual(self.rows(runner, 2)[0]['window_source'], 'default')
                self.assertIsNone(self.rows(runner, 2)[0]['window_hint_source_attempt'])

    def test_long_pass_after_deadline_does_not_qualify(self):
        runner = BudgetReplay([needs_one_second(), tone(seconds=.2)])
        runner.point_deadline_expired = lambda: runner.index == 0 and len(self.rows(runner, 1)) == 4
        result = self.run_quietly(runner)
        last = self.rows(runner, 1)[-1]
        self.assertTrue(last['passed'])
        self.assertEqual(last['state'], 'VERIFY')
        self.assertEqual(last['window_duration_s'], 1.)
        self.assertTrue(last['point_deadline_expired'])
        self.assertIsNone(result['all_amp_records'][0][0])
        self.assertEqual(self.rows(runner, 2)[0]['window_source'], 'default')

    def test_a_late_long_measurement_without_long_verify_cannot_supply_weak_hint(self):
        cfg = logic.SweepConfig(num_points=1, sweep_repeat=2, skip_invalid_points=True, measure_min_seconds=1.)
        runner = BudgetReplay([tone(seconds=1.1), tone(seconds=1.1)], cfg)
        runner.point_deadline_expired = lambda: runner.index == 0 and len(self.rows(runner, 1)) == 2
        result = self.run_quietly(runner)
        last = self.rows(runner, 1)[-1]
        self.assertEqual(last['state'], 'MEASURE')
        self.assertEqual(last['window_duration_s'], 1.)
        self.assertTrue(last['passed'])
        self.assertIsNone(result['all_amp_records'][0][0])
        self.assertEqual(self.rows(runner, 2)[0]['window_source'], 'default')

    def test_disabled_reuse_disables_failure_and_success_hints(self):
        cfg = logic.SweepConfig(num_points=1, sweep_repeat=2, skip_invalid_points=True,
                                reuse_previous_round_window=False)
        runner = BudgetReplay([one_long_pass_then_failure(), tone(seconds=.2)], cfg)
        self.run_quietly(runner)
        self.assertEqual(self.rows(runner, 2)[0]['window_source'], 'default')
        self.assertEqual(logic.sweep_algorithm_policy(cfg)['previous_round_window_policy'], 'disabled')

    def test_failure_hint_never_survives_an_intervening_round_without_evidence(self):
        runner = BudgetReplay([one_long_pass_then_failure(), tone(12000., 8.), tone(seconds=.2)])
        result = self.run_quietly(runner)
        self.assertIsNone(result['all_amp_records'][0][0])
        self.assertIsNone(result['all_amp_records'][1][0])
        self.assertEqual(self.rows(runner, 2)[0]['window_source'], 'previous_round_weak_confirmation')
        self.assertEqual(self.rows(runner, 3)[0]['window_source'], 'default')

    def test_failure_hint_does_not_cross_run_on_same_instance(self):
        runner = BudgetReplay([one_long_pass_then_failure()])
        self.run_quietly(runner)
        runner.index = -1
        runner.records.clear()
        runner.sequences = [tone(seconds=.2)]
        self.run_quietly(runner)
        self.assertEqual(runner.records[0]['window_source'], 'default')
        self.assertIsNone(runner.records[0]['window_hint_source_round'])

    def test_failure_hint_budget_fallback_retains_evidence_provenance(self):
        runner = BudgetReplay([one_long_pass_then_failure(), tone(seconds=.2)], budgets=[8., .2])
        result = self.run_quietly(runner)
        first = self.rows(runner, 2)[0]
        self.assertEqual(first['window_source'], 'budget_fallback')
        self.assertEqual(first['window_hint_source_kind'], 'previous_round_weak_confirmation')
        self.assertEqual(first['window_hint_observed_window_seconds'], 1.)
        self.assertEqual(first['window_hint_source_round'], 1)
        self.assertEqual(first['point_initial_budget_required_seconds'], 6.)
        self.assertEqual(first['point_initial_budget_available_seconds'], .2)
        self.assertEqual(first['point_initial_level_index'], 0)
        self.assertIsNotNone(result['all_amp_records'][1][0])

    def test_success_keeps_its_level_and_does_not_apply_failed_point_increment(self):
        runner = BudgetReplay([needs_one_second(), tone(seconds=4.)])
        self.run_quietly(runner)
        first = self.rows(runner, 2)[0]
        self.assertEqual(first['window_source'], 'previous_round_success')
        self.assertEqual(first['window_hint_source_state'], 'MEASURE')
        self.assertEqual(first['window_hint_observed_level_index'], 2)
        self.assertEqual(first['point_initial_level_index'], 2)
        self.assertEqual(first['window_hint_source_attempt'], self.rows(runner, 1)[-1]['quality_attempt_index'])

    def test_operator_stop_during_long_verify_does_not_create_failure_measurement_or_cross_run_hint(self):
        runner = BudgetReplay([needs_one_second()])
        original = logic.quality_measurement
        def stop_on_long_verify(*args):
            value = original(*args)
            if len(args[0]) >= RATE:
                runner.stop_event.set()
            return value
        with patch.object(logic, 'quality_measurement', side_effect=stop_on_long_verify):
            result = self.run_quietly(runner)
        self.assertEqual(result['all_amp_records'], [[]])
        self.assertTrue(runner.records[-1]['passed'])
        self.assertTrue(runner.records[-1]['stop_requested'])
        self.assertFalse(runner.records[-1]['accepted'])
        runner.stop_event.clear()
        runner.index = -1
        runner.records.clear()
        runner.sequences = [tone(seconds=.2)]
        self.run_quietly(runner)
        self.assertEqual(runner.records[0]['window_source'], 'default')


if __name__ == '__main__':
    unittest.main()
