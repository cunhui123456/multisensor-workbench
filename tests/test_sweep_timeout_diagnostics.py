"""Offline deadline explanations must reflect the current state, not a stale rejection."""
import contextlib
import io
import json
from pathlib import Path
from queue import Queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from sspp_experiments.material_sweep import acquisition, logic


class SweepTimeoutDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.args = SimpleNamespace(rate=250000., rx_rate_actual=250000., channel=0)
        self.cfg = logic.SweepConfig(num_points=1, sweep_repeat=1, skip_invalid_points=True)
        self.manager = MagicMock()
        self.manager.usrp.get_tx_freq.return_value = 5e9
        self.manager.usrp.get_rx_freq.return_value = 5e9
        self.queue = Queue()
        self.stop = threading.Event()
        self.runner = acquisition.make_runner(self.args, self.cfg, self.manager, self.queue,
            self.stop, None, self.output, SimpleNamespace(point_timeout=8.), [])

    @staticmethod
    def tone(count):
        return (.01*np.exp(2j*np.pi*2000*np.arange(count)/250000.)).astype(np.complex64)

    def prepare_deadline(self, *, stage='VERIFY', verified=0):
        runner = self.runner
        runner.point_number = 9
        runner.point_started_monotonic_s = 100.
        runner.deadline = 108.
        runner.last_frame_at = 107.9
        runner.point_attempts = 3
        runner.on_point_progress({'state':stage, 'verified_windows':verified,
            'required_verified_windows':2, 'pending_window_count':250000,
            'pending_window_seconds':1., 'buffered_sample_count':10000,
            'completion_samples_needed':240000 if stage=='MEASURE' else 740000,
            'adaptive_level_index':5})
        runner.last_quality_rejection = ['target_tone_below_local_floor_margin']
        runner.last_quality_record = {'state':'VERIFY', 'passed':True, 'window_duration_s':1.,
            'quality_attempt_index':3, 'point_deadline_expired':False,
            'host_processed_monotonic_s':107.5}

    def test_waiting_measurement_explains_two_passes_despite_earlier_rejection(self):
        self.prepare_deadline(stage='MEASURE', verified=2)
        self.queue.put((0, self.tone(4096)))
        with patch.object(acquisition.time, 'monotonic', return_value=108.01):
            with self.assertRaises(logic.PointQualityTimeout) as error:
                self.runner._fetch_available_frames()
            details = self.runner.invalid_quality_details()
        diagnostic = details['point_timeout']
        self.assertIn('两次验证已完成', str(error.exception))
        self.assertIn('最近一次窗口判定通过', details['reason'])
        self.assertNotIn('last rejection=', details['reason'])
        self.assertEqual(diagnostic['classification'], 'measurement_incomplete_after_verification')
        self.assertEqual(diagnostic['deadline_stage'], 'MEASURE')
        self.assertEqual(diagnostic['verified_windows'], 2)
        self.assertEqual(diagnostic['last_rejection_reasons'], ['target_tone_below_local_floor_margin'])
        self.assertTrue(diagnostic['last_attempt_passed'])
        self.assertEqual(diagnostic['buffered_sample_count'], 14096)
        self.assertEqual(diagnostic['completion_samples_needed'], 235904)
        self.assertEqual(diagnostic['budget_seconds'], 8)
        self.assertAlmostEqual(diagnostic['elapsed_seconds'], 8.01)
        self.assertEqual(diagnostic['remaining_seconds'], 0)
        self.assertEqual(self.runner.deadline, 108.)
        self.assertFalse(self.stop.is_set())
        # Reporting later cannot replace the actual deadline snapshot with export time.
        with patch.object(acquisition.time, 'monotonic', return_value=120.):
            self.assertEqual(self.runner.invalid_quality_details()['point_timeout'], diagnostic)
        json.dumps(details, allow_nan=False)

    def test_one_pass_never_claims_verification_complete(self):
        self.prepare_deadline(stage='VERIFY', verified=1)
        with patch.object(acquisition.time, 'monotonic', return_value=108.02):
            details = self.runner.invalid_quality_details()
        self.assertEqual(details['point_timeout']['classification'], 'quality_insufficient_before_deadline')
        self.assertEqual(details['point_timeout']['verified_windows'], 1)
        self.assertNotIn('两次验证已完成', details['reason'])

    def test_budget_planning_uses_original_deadline_and_keeps_reason_in_timeout(self):
        self.prepare_deadline(stage='VERIFY', verified=1)
        planning = {'quality_attempt_index':11,
                    'escalation_candidate_level_index':3,
                    'escalation_minimum_new_samples':1250000,
                    'escalation_minimum_new_seconds':5.,
                    'escalation_available_seconds':2.632,
                    'escalation_blocked_reason':'insufficient_remaining_point_budget'}
        self.runner.point_progress.update(point_initial_level_index=2,
            point_initial_window_seconds=1., window_source='previous_round_success',
            window_hint_source_round=1, last_escalation_budget=planning)
        with patch.object(acquisition.time, 'monotonic', return_value=105.368):
            self.assertAlmostEqual(self.runner.remaining_point_seconds(),2.632)
        with patch.object(acquisition.time, 'monotonic', return_value=108.01):
            self.assertEqual(self.runner.remaining_point_seconds(),0.)
            details=self.runner.invalid_quality_details()
        self.assertEqual(self.runner.deadline,108.)
        schedule=details['point_timeout']['window_schedule']
        self.assertEqual(schedule['window_source'],'previous_round_success')
        self.assertEqual(schedule['window_hint_source_round'],1)
        self.assertEqual(schedule['last_escalation_budget'],planning)
        self.assertIn('长窗升级所需时间超过剩余预算',details['reason'])
        self.assertEqual(details['point_timeout']['classification'], 'quality_insufficient_before_deadline')
        json.dumps(details,allow_nan=False)

    def test_successful_measurement_finishing_late_is_not_reported_as_unfinished(self):
        self.prepare_deadline(stage='MEASURE', verified=2)
        self.runner.last_quality_record.update(state='MEASURE', point_deadline_expired=True,
                                               host_processed_monotonic_s=108.03)
        with patch.object(acquisition.time, 'monotonic', return_value=109.):
            details = self.runner.invalid_quality_details()
        diagnostic = details['point_timeout']
        self.assertEqual(diagnostic['classification'], 'measurement_completed_after_deadline')
        self.assertEqual(diagnostic['deadline_trigger'], 'window_processing')
        self.assertAlmostEqual(diagnostic['elapsed_seconds'], 8.03)
        self.assertEqual(diagnostic['completion_samples_needed'], 0)
        self.assertEqual(diagnostic['pending_window_count'], 0)
        self.assertEqual(diagnostic['last_attempt_window_seconds'], 1.)
        self.assertIn('判定通过', details['reason'])
        self.assertIn('未接纳', details['reason'])

    def test_failed_measurement_at_deadline_does_not_imply_successful_measurement(self):
        self.prepare_deadline(stage='MEASURE', verified=2)
        self.runner.last_quality_record.update(state='MEASURE', passed=False, point_deadline_expired=True,
                                               host_processed_monotonic_s=108.04)
        details = self.runner.invalid_quality_details()
        self.assertEqual(details['point_timeout']['classification'], 'quality_insufficient_before_deadline')
        self.assertFalse(details['point_timeout']['last_attempt_passed'])

    def test_current_state_hook_and_timeout_details_are_persisted_by_real_runner(self):
        clock = [100.]
        self.runner._clear_queue = lambda: self.queue.put((0, self.tone(20000)))
        original = self.runner.on_quality_attempt
        def record_attempt(record):
            original(record)
            if self.runner.point_attempts == 2:
                # Two real non-overlapping VERIFY windows pass. New IQ is still
                # arriving at the deadline, so this is not the independent stall guard.
                clock[0] = 108.01
                self.queue.put((20000, self.tone(200)))
        self.runner.on_quality_attempt = record_attempt
        with patch.object(acquisition.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(logic.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            result = self.runner.run(enable_plot=False)
        record = json.loads((self.output/'sweep_invalid_points.jsonl').read_text())
        quality = record['sweep_quality']
        self.assertEqual(result['all_amp_records'], [[None]])
        self.assertEqual(quality['reason_code'], 'point_quality_timeout')
        self.assertEqual(quality['point_timeout']['deadline_stage'], 'MEASURE')
        self.assertEqual(quality['point_timeout']['verified_windows'], 2)
        self.assertEqual(quality['point_timeout']['pending_window_seconds'], .04)
        self.assertTrue(quality['last_attempt']['passed'])
        self.assertFalse(quality['accepted'])
        self.assertIn('两次验证已完成', quality['reason'])
        self.assertEqual(self.runner.deadline, 108.)
        self.manager.set_center_freq.assert_called_once()
        self.assertFalse(self.stop.is_set())

    def test_no_iq_and_transport_stall_keep_hard_failure_semantics(self):
        self.prepare_deadline()
        self.runner.point_attempts = 0
        with patch.object(acquisition.time, 'monotonic', return_value=108.01):
            with self.assertRaisesRegex(RuntimeError, 'without sufficient real IQ'):
                self.runner._fetch_available_frames()
        self.runner.point_attempts = 1
        self.runner.last_frame_at = 104.
        with patch.object(acquisition.time, 'monotonic', return_value=108.01):
            with self.assertRaisesRegex(RuntimeError, 'RX data stalled'):
                self.runner._fetch_available_frames()
        self.assertIsNone(self.runner.point_timeout_diagnostic)

    def test_real_processing_overrun_is_saved_but_never_accepted(self):
        clock, calls = [100.], [0]
        self.runner._clear_queue = lambda: self.queue.put((0, self.tone(20000)))
        original = logic.quality_measurement
        def delayed_processing(*args, **kwargs):
            result = original(*args, **kwargs)
            calls[0] += 1
            if calls[0] == 3:
                clock[0] = 108.05
            return result
        with patch.object(acquisition.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(logic.time, 'sleep'), patch.object(logic, 'quality_measurement', side_effect=delayed_processing), \
             contextlib.redirect_stdout(io.StringIO()):
            result = self.runner.run(enable_plot=False)
        record = json.loads((self.output/'sweep_invalid_points.jsonl').read_text())
        quality = record['sweep_quality']
        self.assertEqual(result['all_amp_records'], [[None]])
        self.assertEqual(quality['point_timeout']['classification'], 'measurement_completed_after_deadline')
        self.assertEqual(quality['point_timeout']['completion_samples_needed'], 0)
        self.assertAlmostEqual(quality['point_timeout']['elapsed_seconds'], 8.05)
        self.assertEqual(quality['point_timeout']['last_attempt_window_seconds'], .04)
        self.assertTrue(quality['last_attempt']['passed'])
        self.assertTrue(quality['last_attempt']['point_deadline_expired'])
        self.assertFalse(quality['last_attempt']['accepted'])
        self.assertFalse(quality['accepted'])
        self.assertEqual(self.runner.deadline, 108.)


if __name__ == '__main__':
    unittest.main()
