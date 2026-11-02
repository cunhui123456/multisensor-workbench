"""Round hints and budget scheduling use real, independent IQ without device access."""

import io
import json
import math
from pathlib import Path
from queue import Queue
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from sspp_experiments.material_sweep import acquisition, logic


RATE = 250000.


def tone(frequency=2000., seconds=4., amplitude=.01):
    return (amplitude * np.exp(2j * np.pi * frequency * np.arange(round(seconds * RATE)) / RATE)).astype(np.complex64)


def needs_one_second(frequency=2000.):
    # Two failed 20 ms and two failed 100 ms windows lead to the 1 s level.
    return np.concatenate((tone(12000., .22), tone(frequency, 4.)))


class BudgetReplay(logic.SweepRunner):
    def __init__(self, sequences, cfg=None, budgets=None, chunk_size=5000):
        cfg = cfg or logic.SweepConfig(num_points=1, sweep_repeat=len(sequences), skip_invalid_points=True)
        super().__init__(SimpleNamespace(rate=RATE, rx_rate_actual=RATE), cfg, None, None, threading.Event())
        self.sequences = sequences
        self.budgets = budgets or [8.] * len(sequences)
        self.chunk_size = chunk_size
        self.index = -1
        self.records = []
        self.progress = []
        self.elapsed_points = []
        self.stop_after_round = None

    def _set_freq_and_tone(self, frequency, selected_tone):
        if self.index >= 0:
            self.elapsed_points.append(self.elapsed)
        self.index += 1
        self.samples = self.sequences[self.index]
        self.offset = 0
        self.elapsed = 0.
        self.budget = self.budgets[self.index]

    def _clear_queue(self):
        pass

    def remaining_point_seconds(self):
        return max(0., self.budget-self.elapsed)

    def point_deadline_expired(self):
        return self.elapsed > self.budget

    def _fetch_available_frames(self):
        count = min(self.chunk_size, len(self.samples)-self.offset,
                    max(0, round((self.budget-self.elapsed)*RATE)))
        if not count:
            self.elapsed = self.budget
            raise logic.PointQualityTimeout('original absolute replay deadline')
        data = self.samples[self.offset:self.offset+count]
        self.offset += count
        self.elapsed = self.offset/RATE
        return [data], data

    def on_quality_attempt(self, record):
        self.records.append(dict(record))

    def on_point_progress(self, record):
        self.progress.append(dict(record))

    def on_round_finished(self, repeat, complete):
        if self.stop_after_round == repeat:
            self.stop_event.set()


class RoundWindowBudgetTests(unittest.TestCase):
    def run_quietly(self, runner):
        with redirect_stdout(io.StringIO()), patch.object(logic.time, 'sleep'):
            return runner.run(enable_plot=False)

    def records(self, runner, repeat, point=1):
        return [row for row in runner.records if row['repeat'] == repeat and row['point'] == point]

    def assert_independent_final_chain(self, rows, samples, target=2000., previous=None):
        final = rows[-3:]
        self.assertEqual([r['state'] for r in final], ['VERIFY', 'VERIFY', 'MEASURE'])
        self.assertEqual([r['verified_windows_before'] for r in final], [0, 1, 2])
        self.assertTrue(final[-1]['accepted'])
        for before, after in zip(final, final[1:]):
            self.assertEqual(before['window_start']+before['window_count'], after['window_start'])
        for row in final:
            start, count = row['window_start'], row['window_count']
            checked = logic.quality_measurement(samples[start:start+count], RATE, target, previous, logic.SweepConfig())
            self.assertTrue(checked['passed'])
            self.assertAlmostEqual(row['amplitude_dB'], checked['amplitude_dB'], places=10)

    def test_version_and_policy_distinguish_disabled_hints_and_reject_non_bool_config(self):
        self.assertEqual(logic.SWEEP_ALGORITHM_VERSION, 'sspp.sweep.target_confirmed.v8')
        for enabled, name in ((True, 'previous_round_success_or_qualified_weak_confirmation_level_only'),
                              (False, 'disabled')):
            cfg = logic.SweepConfig(reuse_previous_round_window=enabled)
            cfg.validate(RATE)
            policy = logic.sweep_algorithm_policy(cfg)
            self.assertIs(policy['reuse_previous_round_window'], enabled)
            self.assertEqual(policy['previous_round_window_policy'], name)
            self.assertEqual(policy['escalation_budget_policy'],
                             'require_remaining_time_for_independent_verification_and_measurement_with_rejected_prefix')
        for value in (0, 1, None, 'false', 'true', [], np.bool_(True)):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '布尔'):
                logic.SweepConfig(reuse_previous_round_window=value).validate(RATE)

    def test_chain_budget_counts_rounding_cycle_minima_and_only_available_samples(self):
        cfg = logic.SweepConfig(verify_consecutive_windows=3)
        runner = BudgetReplay([], cfg)
        rate = 250003.
        self.assertEqual(runner._window_chain_samples_needed(1, 6000, 30000, rate,
                                                            available_samples=15000),
                         3*math.ceil(.1*rate)+30000-15000)
        self.assertEqual(runner._window_chain_samples_needed(0, 6000, 30000, rate,
                                                            verified_windows=3, available_samples=12000), 18000)
        self.assertEqual(runner._window_chain_samples_needed(0, 6000, 30000, rate,
                                                            available_samples=1000000), 0)

    def test_same_frequency_previous_round_hint_requires_three_new_windows(self):
        second = tone(seconds=4., amplitude=.02)
        runner = BudgetReplay([needs_one_second(), second])
        result = self.run_quietly(runner)
        first = self.records(runner, 1)
        rows = self.records(runner, 2)
        self.assertEqual(first[-1]['adaptive_level_index'], 2)
        self.assertEqual(rows[0]['point_initial_level_index'], 2)
        self.assertEqual(rows[0]['point_initial_window_seconds'], 1.)
        self.assertEqual(rows[0]['window_source'], 'previous_round_success')
        self.assertEqual(rows[0]['window_hint_source_round'], 1)
        self.assertEqual([r['window_start'] for r in rows], [0, 250000, 500000])
        self.assertEqual([r['window_duration_s'] for r in rows], [1., 1., 1.])
        self.assert_independent_final_chain(rows, second, previous=2000.)
        self.assertAlmostEqual(result['all_amp_records'][1][0], 20*math.log10(.02), places=5)
        self.assertNotAlmostEqual(result['all_amp_records'][0][0], result['all_amp_records'][1][0], places=1)

    def test_invalid_intervening_round_clears_hint_instead_of_reusing_older_success(self):
        runner = BudgetReplay([needs_one_second(), tone(12000., 8.), tone(seconds=1.)])
        result = self.run_quietly(runner)
        self.assertIsNone(result['all_amp_records'][1][0])
        self.assertEqual(self.records(runner, 2)[0]['window_source'], 'previous_round_success')
        third = self.records(runner, 3)[0]
        self.assertEqual(third['point_initial_level_index'], 0)
        self.assertEqual(third['window_source'], 'default')
        self.assertIsNone(third['window_hint_source_round'])
        self.assertIsNotNone(result['all_amp_records'][2][0])

    def test_hints_stay_per_frequency_in_continuous_mode_and_stop_at_operator_request(self):
        seq = [needs_one_second(), tone(3000., 1.), tone(seconds=4.), tone(3000., 1.)]
        cfg = logic.SweepConfig(num_points=2, continuous=True, skip_invalid_points=True)
        runner = BudgetReplay(seq, cfg)
        runner.stop_after_round = 2
        result = self.run_quietly(runner)
        self.assertEqual(len(result['all_amp_records']), 2)
        self.assertEqual(self.records(runner, 2, 1)[0]['point_initial_level_index'], 2)
        second_point = self.records(runner, 2, 2)
        self.assertEqual(second_point[0]['point_initial_level_index'], 0)
        self.assertEqual([r['window_duration_s'] for r in second_point], [.02, .02, .04])
        self.assertEqual(second_point[0]['window_hint_source_round'], 1)
        self.assertEqual(runner.index, 3)

    def test_strong_point_remembers_20ms_level_not_40ms_measurement_duration(self):
        runner = BudgetReplay([tone(seconds=.2), tone(seconds=.2)])
        self.run_quietly(runner)
        second = self.records(runner, 2)
        self.assertEqual(second[0]['point_initial_window_seconds'], .02)
        self.assertEqual([r['window_duration_s'] for r in second], [.02, .02, .04])

    def test_budget_fallback_uses_default_level_without_extending_original_limit(self):
        runner = BudgetReplay([needs_one_second(), tone(seconds=.2)], budgets=[8., .2])
        result = self.run_quietly(runner)
        row = self.records(runner, 2)[0]
        self.assertEqual(row['window_source'], 'budget_fallback')
        self.assertEqual(row['window_hint_source_round'], 1)
        self.assertEqual(row['point_initial_level_index'], 0)
        self.assertEqual(row['point_initial_budget_required_seconds'], 3.)
        self.assertEqual(row['point_initial_budget_available_seconds'], .2)
        self.assertEqual(runner.budget, .2)
        self.assertLessEqual(runner.elapsed, .2)
        self.assertIsNotNone(result['all_amp_records'][1][0])
        self.assertTrue(any(p['window_source'] == 'budget_fallback' for p in runner.progress))

    def test_disabled_hints_and_separate_run_both_start_fresh(self):
        cfg = logic.SweepConfig(num_points=1, sweep_repeat=2, reuse_previous_round_window=False)
        runner = BudgetReplay([needs_one_second(), tone(seconds=.2)], cfg)
        self.run_quietly(runner)
        self.assertEqual(self.records(runner, 2)[0]['window_source'], 'default')
        self.assertEqual(self.records(runner, 2)[0]['point_initial_level_index'], 0)
        runner = BudgetReplay([needs_one_second()])
        self.run_quietly(runner)
        runner.index = -1
        runner.records.clear()
        runner.sequences = [tone(seconds=.2)]
        self.run_quietly(runner)
        self.assertEqual(runner.records[0]['point_initial_level_index'], 0)
        self.assertEqual(runner.records[0]['window_source'], 'default')

    def test_offline_none_budget_allows_hints_without_inventing_a_deadline(self):
        runner = BudgetReplay([needs_one_second(), tone(seconds=4.)])
        runner.remaining_point_seconds = lambda: None
        self.run_quietly(runner)
        row = self.records(runner, 2)[0]
        self.assertEqual(row['window_source'], 'previous_round_success')
        self.assertIsNone(row['point_initial_budget_available_seconds'])

    def test_impossible_escalation_stays_current_level_and_can_finish_fresh_chain(self):
        samples = np.concatenate((tone(12000., .04), tone(seconds=.1)))
        runner = BudgetReplay([samples], budgets=[.13])
        result = self.run_quietly(runner)
        blocked = runner.records[1]
        self.assertEqual(blocked['escalation_candidate_level_index'], 1)
        self.assertEqual(blocked['escalation_blocked_reason'], 'insufficient_remaining_point_budget')
        self.assertAlmostEqual(blocked['escalation_minimum_new_seconds'], .28)
        self.assertAlmostEqual(blocked['escalation_available_seconds'], .09)
        self.assertEqual(blocked['next_adaptive_level_index'], 0)
        self.assertEqual(blocked['retained_prefix_count'], 0)
        self.assertEqual(runner.records[2]['window_start'], 10000)
        self.assert_independent_final_chain(runner.records, samples)
        self.assertEqual(runner.budget, .13)
        self.assertLessEqual(runner.elapsed, .13)
        self.assertIsNotNone(result['all_amp_records'][0][0])
        self.assertEqual(runner.progress[-1]['last_escalation_budget']['quality_attempt_index'], 2)

    def test_buffer_credit_and_failed_prefix_are_counted_once_for_escalation(self):
        samples = np.concatenate((tone(12000., .04), tone(seconds=.5)))
        runner = BudgetReplay([samples], budgets=[.35], chunk_size=50000)
        original_fetch = runner._fetch_available_frames
        fetches = 0
        def no_new_frame_while_processing_backlog():
            nonlocal fetches
            fetches += 1
            return ([], None) if fetches == 2 else original_fetch()
        runner._fetch_available_frames = no_new_frame_while_processing_backlog
        self.run_quietly(runner)
        decision = runner.records[1]
        # 75k new-level chain minus 5k failed prefix minus 40k unused buffer.
        self.assertEqual(decision['escalation_minimum_new_samples'], 30000)
        self.assertAlmostEqual(decision['escalation_minimum_new_seconds'], .12)
        self.assertAlmostEqual(decision['escalation_available_seconds'], .15)
        self.assertIsNone(decision['escalation_blocked_reason'])
        self.assertEqual(decision['next_adaptive_level_index'], 1)
        self.assertEqual(decision['retained_prefix_count'], 5000)
        self.assertEqual(runner.records[2]['window_start'], 5000)

    def test_hinted_measurement_failure_requires_two_fresh_verifications(self):
        samples = np.concatenate((tone(seconds=2.), tone(12000., 1.), tone(seconds=3.)))
        runner = BudgetReplay([needs_one_second(), samples])
        result = self.run_quietly(runner)
        rows = self.records(runner, 2)
        self.assertEqual(rows[2]['state'], 'MEASURE')
        self.assertFalse(rows[2]['passed'])
        self.assertFalse(rows[2]['accepted'])
        self.assertEqual([r['window_start'] for r in rows[3:]], [750000, 1000000, 1250000])
        self.assert_independent_final_chain(rows, samples, previous=2000.)
        self.assertIsNotNone(result['all_amp_records'][1][0])

    def test_blocked_escalation_does_not_end_early_or_accept_wrong_tone(self):
        runner = BudgetReplay([tone(12000., .2)], budgets=[.13])
        result = self.run_quietly(runner)
        self.assertEqual(result['all_amp_records'], [[None]])
        self.assertEqual(runner.elapsed, .13)
        self.assertEqual(runner.budget, .13)
        self.assertFalse(any(r['accepted'] for r in runner.records))
        self.assertTrue(any(r['escalation_blocked_reason'] for r in runner.records))

    def test_hint_does_not_turn_previous_round_success_into_current_acceptance_after_stop(self):
        runner = BudgetReplay([needs_one_second(), tone(seconds=4.)])
        original = logic.quality_measurement
        def stopping_quality(*args):
            value = original(*args)
            if runner.index == 1:
                runner.stop_event.set()
            return value
        with patch.object(logic, 'quality_measurement', side_effect=stopping_quality):
            result = self.run_quietly(runner)
        self.assertEqual(result['all_amp_records'][1], [])
        self.assertEqual(self.records(runner, 2)[0]['window_source'], 'previous_round_success')
        self.assertFalse(self.records(runner, 2)[0]['accepted'])

    def test_hint_successful_measurement_after_deadline_remains_invalid(self):
        runner = BudgetReplay([needs_one_second(), tone(seconds=4.)])
        original = logic.quality_measurement
        def slow_quality(*args):
            value = original(*args)
            if runner.index == 1 and len(self.records(runner, 2)) == 2:
                runner.elapsed = runner.budget+.01
            return value
        with patch.object(logic, 'quality_measurement', side_effect=slow_quality):
            result = self.run_quietly(runner)
        self.assertIsNone(result['all_amp_records'][1][0])
        self.assertTrue(runner.records[-1]['passed'])
        self.assertTrue(runner.records[-1]['point_deadline_expired'])
        self.assertFalse(runner.records[-1]['accepted'])

    def test_hardware_fault_is_never_changed_into_budget_or_quality_timeout(self):
        runner = BudgetReplay([tone()])
        runner._fetch_available_frames = lambda: (_ for _ in ()).throw(RuntimeError('RX hardware failure'))
        with self.assertRaisesRegex(RuntimeError, 'RX hardware failure'):
            self.run_quietly(runner)
        self.assertEqual(runner.partial_result['all_amp_records'], [[]])

    def test_recorded_runner_three_rounds_persists_hints_and_independent_current_iq(self):
        sequences = [needs_one_second(), tone(3000., .2),
                     tone(seconds=4., amplitude=.02), tone(3000., .2),
                     tone(seconds=4., amplitude=.03), tone(3000., .2)]
        cfg = logic.SweepConfig(num_points=2, sweep_repeat=3, skip_invalid_points=True)
        args = SimpleNamespace(rate=RATE, rx_rate_actual=RATE, channel=0)
        manager = MagicMock()
        manager.usrp.get_tx_freq.return_value = 5e9
        manager.usrp.get_rx_freq.return_value = 5e9
        queue, clock, cursor = Queue(), [100.], {'offset':0, 'index':0}
        with tempfile.TemporaryDirectory() as folder:
            runner = acquisition.make_runner(args, cfg, manager, queue, threading.Event(),
                                             None, Path(folder), SimpleNamespace(point_timeout=8.), [])
            original_clear, original_fetch = runner._clear_queue, runner._fetch_available_frames
            def clear_and_select_real_trace():
                original_clear()
                cursor['offset'] = 0
            def provide_next_frame():
                source = sequences[runner.point_number-1]
                data = source[cursor['offset']:cursor['offset']+5000]
                self.assertGreater(len(data), 0, 'Fixture exhausted before independent measurement')
                cursor['offset'] += len(data)
                queue.put((cursor['index'], data.copy()))
                cursor['index'] += len(data)
                clock[0] += len(data)/RATE
                return original_fetch()
            runner._clear_queue = clear_and_select_real_trace
            runner._fetch_available_frames = provide_next_frame
            with patch.object(acquisition.time, 'monotonic', side_effect=lambda: clock[0]):
                result = self.run_quietly(runner)
                runner.flush_point()
            rows = [json.loads(line) for line in (Path(folder)/'sweep_quality.jsonl').read_text().splitlines()]
            self.assertEqual([len(values) for values in result['all_amp_records']], [2, 2, 2])
            self.assertEqual(len(runner.raw_files), 6)
            self.assertEqual(manager.set_center_freq.call_count, 6)
            for repeat in (2, 3):
                one = [r for r in rows if r['repeat'] == repeat and r['point'] == 1]
                self.assertEqual(one[0]['window_source'], 'previous_round_success')
                self.assertEqual(one[0]['window_hint_source_round'], repeat-1)
                self.assertEqual(one[0]['point_initial_level_index'], 2)
                self.assertEqual([r['window_start'] for r in one], [0, 250000, 500000])
                self.assertEqual([r['verified_windows_before'] for r in one], [0, 1, 2])
                self.assert_independent_final_chain(one, sequences[(repeat-1)*2], previous=3000.)
                self.assertEqual(len({r['raw_iq_file'] for r in one}), 1)
                self.assertAlmostEqual(result['all_amp_records'][repeat-1][0],
                                       20*math.log10(.01*repeat), places=5)
                two = [r for r in rows if r['repeat'] == repeat and r['point'] == 2]
                self.assertEqual(two[0]['point_initial_level_index'], 0)
                self.assertEqual([r['window_duration_s'] for r in two], [.02, .02, .04])


if __name__ == '__main__':
    unittest.main()
