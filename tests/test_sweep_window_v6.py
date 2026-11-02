"""Retain v6 window-ladder, independent-window and hard-fault contracts under v7."""
from pathlib import Path
from queue import Queue
import hashlib
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from sspp_experiments.material_sweep import logic
from sspp_experiments.material_sweep.acquisition import make_runner
from test_sweep_quality import RATE, ReplayRunner, tone
from test_weak_signal_gaps import Replay


class SweepWindowV6Tests(unittest.TestCase):
    def run_quietly(self, runner):
        with patch('builtins.print'),patch.object(logic.time,'sleep'):
            return runner.run(enable_plot=False)

    def assert_independent_success_windows(self, rows):
        passed=[row for row in rows if row['passed']]
        for previous,current in zip(passed,passed[1:]):
            self.assertGreaterEqual(current['window_start'],previous['window_start']+previous['window_count'])
        self.assertTrue(all(row['retained_prefix_count']==0 for row in passed))

    def test_default_four_level_identity_and_explicit_legacy_windows(self):
        self.assertEqual(logic.SWEEP_ALGORITHM_VERSION,'sspp.sweep.target_confirmed.v8')
        self.assertEqual(logic.SweepConfig().adaptive_rejections_per_level,2)
        self.assertEqual(logic.SweepConfig().adaptive_window_seconds,(.02,.1,1.,2.))
        for levels in ((.02,.1,1.,2.),(.02,.05,.1,.25,.5,1.,2.)):
            cfg=logic.SweepConfig(adaptive_window_seconds=levels)
            cfg.validate(RATE)
            policy=logic.sweep_algorithm_policy(cfg)
            self.assertEqual(policy['adaptive_rejections_per_level'],2)
            self.assertEqual(policy['verify_consecutive_windows'],2)
            self.assertEqual(policy['min_tone_snr_db'],6)
            self.assertEqual(policy['verify_tolerance_hz'],200)
            self.assertEqual(policy['adaptive_window_seconds'],list(levels))
            self.assertEqual(policy['adaptive_carryover_hold_seconds'],.25)
        for value in (0,1,3,True):
            with self.subTest(value=value),self.assertRaises(ValueError):
                logic.SweepConfig(adaptive_rejections_per_level=value).validate(RATE)

    def test_two_failures_jump_to_100ms_without_reusing_passed_windows(self):
        samples=np.concatenate([tone(3000,10000),tone(2000,150000)])
        runner=ReplayRunner([samples])
        result=self.run_quietly(runner)
        first,second,extended=runner.quality[:3]
        self.assertFalse(first['passed'])
        self.assertEqual(first['level_rejections_after'],1)
        self.assertEqual(first['next_adaptive_level_index'],0)
        self.assertEqual(first['retained_prefix_count'],0)
        self.assertFalse(second['passed'])
        self.assertEqual(second['level_rejections_after'],2)
        self.assertEqual(second['next_adaptive_level_index'],1)
        self.assertEqual(second['retained_prefix_count'],5000)
        self.assertEqual(extended['window_start'],second['window_start'])
        self.assertEqual(extended['window_count'],25000)
        self.assertEqual(extended['verified_windows_before'],0)
        self.assertTrue(extended['passed'])
        self.assert_independent_success_windows(runner.quality)
        accepted=result['all_quality_records'][0][0]
        self.assertEqual(accepted['window_start'],55000)
        self.assertEqual(accepted['window_count'],25000)
        recalculated=logic.quality_measurement(samples[55000:80000],RATE,2000,None,runner.cfg)
        self.assertAlmostEqual(accepted['amplitude_dB'],recalculated['amplitude_dB'])

    def test_measure_failure_counts_as_second_rejection_and_requires_two_fresh_verifications(self):
        samples=np.concatenate([tone(3000,5000),tone(2000,10000),tone(3000,10000),tone(2000,150000)])
        runner=ReplayRunner([samples])
        self.run_quietly(runner)
        failed=runner.quality[3]
        self.assertEqual(failed['state'],'MEASURE')
        self.assertFalse(failed['passed'] or failed['accepted'])
        self.assertEqual(failed['next_adaptive_level_index'],1)
        following=runner.quality[4]
        self.assertEqual(following['state'],'VERIFY')
        self.assertEqual(following['verified_windows_before'],0)
        self.assertEqual(following['window_start'],failed['window_start'])
        final=runner.quality[-3:]
        self.assertEqual([row['state'] for row in final],['VERIFY','VERIFY','MEASURE'])
        self.assertEqual([row['verified_windows_before'] for row in final],[0,1,2])
        self.assertTrue(all(row['passed'] for row in final))
        self.assertTrue(final[-1]['accepted'])
        self.assert_independent_success_windows(runner.quality)
        self.assertEqual(runner.retunes,1)

    def test_strong_signal_keeps_original_20_20_40ms_fast_path_each_point(self):
        runner=ReplayRunner([tone(2000,20000),tone(3000,20000)])
        result=self.run_quietly(runner)
        self.assertEqual(len(result['all_amp_records'][0]),2)
        for point in (1,2):
            rows=[row for row in runner.quality if row['point']==point]
            self.assertEqual([row['state'] for row in rows],['VERIFY','VERIFY','MEASURE'])
            self.assertEqual([row['window_duration_s'] for row in rows],[.02,.02,.04])
            self.assertEqual([row['window_start'] for row in rows],[0,5000,10000])
            self.assertTrue(all(row['adaptive_level_index']==0 for row in rows))
            self.assert_independent_success_windows(rows)

    def test_v6_recovers_both_historical_weak_targets_with_independent_success_windows(self):
        base=Path('/Users/cunhui/Git-Projects/Academic_Research/multisensor-workbench/Data/runs')
        cases=(('20261102T023947_939461Z','iq_point_037.npz',38000),
               ('20261102T023813_655557Z','iq_point_038.npz',19000))
        for run,filename,target in cases:
            source=base/run/filename
            if not source.is_file():self.skipTest('saved historical IQ unavailable')
            with self.subTest(target=target):
                before=hashlib.sha256(source.read_bytes()).hexdigest()
                with np.load(source) as archive:samples=archive['iq'].copy()
                cfg=logic.SweepConfig(num_points=2,sweep_repeat=1,tone_start=target-1000,
                                      tone_step=1000,skip_invalid_points=True)
                runner=Replay([tone(target-1000,20000,noise=0),samples],cfg)
                result=self.run_quietly(runner)
                self.assertIsNotNone(result['all_amp_records'][0][1])
                self.assertLessEqual(runner.clock-runner.started,8)
                rows=[row for row in runner.records if row['point']==2]
                successful=rows[-3:]
                self.assertEqual([row['state'] for row in successful],['VERIFY','VERIFY','MEASURE'])
                self.assertEqual([row['verified_windows_before'] for row in successful],[0,1,2])
                self.assertTrue(successful[-1]['accepted'])
                self.assert_independent_success_windows(rows)
                for row in successful:
                    self.assertTrue(row['passed'])
                    self.assertGreaterEqual(row['target_snr_db'],6)
                    self.assertLess(abs(row['measured_tone_hz']-target),200)
                    # Independent halves are required only when the wide-band
                    # peak is outside the target band, exactly as in v5.
                    confirmation=row['target_confirmation']
                    if confirmation['required']:
                        self.assertTrue(confirmation['confirmed'])
                        self.assertTrue(all(half['target_snr_db']>=6 for half in confirmation['halves']))
                    if row['previous_tone_present']:
                        self.assertGreaterEqual(row['previous_tone_margin_db'],6)
                    start,count=row['window_start'],row['window_count']
                    checked=logic.quality_measurement(samples[start:start+count],RATE,target,target-1000,cfg)
                    self.assertTrue(checked['passed'])
                    self.assertAlmostEqual(checked['amplitude_dB'],row['amplitude_dB'])
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)

    def test_stop_during_final_quality_processing_never_publishes_passed_window(self):
        runner=ReplayRunner([tone(2000,20000)])
        original=logic.quality_measurement
        calls=[]
        def measured(*args):
            value=original(*args)
            calls.append(value)
            if len(calls)==3: runner.stop_event.set()
            return value
        with patch.object(logic,'quality_measurement',side_effect=measured):
            result=self.run_quietly(runner)
        self.assertEqual(result['all_amp_records'],[[]])
        self.assertTrue(runner.quality[-1]['passed'])
        self.assertTrue(runner.quality[-1]['stop_requested'])
        self.assertFalse(runner.quality[-1]['accepted'])

    def test_post_processing_deadline_never_accepts_good_measurement(self):
        cfg=logic.SweepConfig(num_points=1,sweep_repeat=1,skip_invalid_points=True)
        runner=Replay([tone(2000,20000)],cfg)
        runner.point_deadline_expired=lambda:len(runner.records)>=2
        result=self.run_quietly(runner)
        self.assertEqual(result['all_amp_records'],[[None]])
        self.assertTrue(runner.records[-1]['passed'])
        self.assertTrue(runner.records[-1]['point_deadline_expired'])
        self.assertFalse(runner.records[-1]['accepted'])
        self.assertEqual(result['all_quality_records'][0][0]['reason_code'],'point_quality_timeout')

    def test_eight_second_exhaustion_remains_missing_and_advances_without_reusing_iq(self):
        cfg=logic.SweepConfig(num_points=2,sweep_repeat=1,skip_invalid_points=True)
        runner=Replay([tone(12000,2000000),tone(3000,20000)],cfg)
        result=self.run_quietly(runner)
        self.assertIsNone(result['all_amp_records'][0][0])
        self.assertIsNotNone(result['all_amp_records'][0][1])
        self.assertEqual(runner.started,8)
        self.assertFalse(any(row['accepted'] for row in runner.records if row['point']==1))
        second=[row for row in runner.records if row['point']==2]
        self.assertEqual(second[0]['window_start'],0)
        self.assertTrue(second[-1]['accepted'])

    def test_device_faults_and_stalls_are_not_reclassified_as_weak_signal(self):
        cfg=logic.SweepConfig(num_points=1,sweep_repeat=1,skip_invalid_points=True)
        with tempfile.TemporaryDirectory() as folder:
            failures=[]
            runner=make_runner(SimpleNamespace(rate=RATE,rx_rate_actual=RATE,channel=0),cfg,
                               MagicMock(),Queue(),threading.Event(),None,Path(folder),
                               SimpleNamespace(point_timeout=8),failures)
            for error in ('TX: TX send returned zero samples','RX: ERROR_CODE_OVERFLOW',
                          'RX shutdown: wait_for_ack accum_timeout'):
                failures[:]=[error]
                with self.subTest(error=error),self.assertRaisesRegex(RuntimeError,error):
                    runner._fetch_available_frames()
            failures.clear();runner.last_frame_at=0.;runner.deadline=8.;runner.point_attempts=1
            with patch.object(logic.time,'monotonic',return_value=4.):
                with self.assertRaisesRegex(RuntimeError,'RX data stalled'):
                    runner._fetch_available_frames()
            runner.last_frame_at=9.;runner.point_attempts=0
            with patch.object(logic.time,'monotonic',return_value=9.):
                with self.assertRaisesRegex(RuntimeError,'without sufficient real IQ'):
                    runner._fetch_available_frames()


if __name__=='__main__':
    unittest.main()
