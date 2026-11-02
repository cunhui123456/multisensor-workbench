"""目标确认回归：强背景、瞬态及晚到单音；不打开射频设备。"""
import unittest
from unittest.mock import patch
import numpy as np
from test_sweep_quality import RATE, ReplayRunner, tone
from sspp_experiments.material_sweep.logic import SweepConfig, quality_measurement


class TargetToneQualityTests(unittest.TestCase):
    def test_stronger_background_does_not_hide_confirmed_target_or_bias_amplitude(self):
        for frequency in (2000., 3000.):
            for seconds in (.02, .05, .1, .25, .5, 1.):
                with self.subTest(frequency=frequency, seconds=seconds):
                    n=round(RATE*seconds)
                    x=tone(frequency,n,amplitude=.01)+tone(frequency-800,n,amplitude=.1,seed=17)
                    q=quality_measurement(x,RATE,frequency,21000,SweepConfig())
                    self.assertTrue(q['passed'],q)
                    self.assertTrue(q['target_confirmation']['required'])
                    self.assertTrue(q['target_confirmation']['confirmed'])
                    self.assertAlmostEqual(q['wide_band_peak_hz'],frequency-800)
                    self.assertAlmostEqual(q['measured_tone_hz'],frequency)
                    self.assertAlmostEqual(q['amplitude_dB'],-40,delta=.1)
                    self.assertGreater(q['wide_band_peak_amplitude_dB']-q['amplitude_dB'],19)

    def test_background_only_and_target_present_in_only_one_half_do_not_get_rescued(self):
        n=25000
        background=tone(1200,n,amplitude=.1)
        for x in (background, background+np.r_[tone(2000,n//2),np.zeros(n-n//2)],
                  background+np.r_[np.zeros(n//2),tone(2000,n-n//2)]):
            with self.subTest():
                q=quality_measurement(x,RATE,2000,None,SweepConfig())
                self.assertFalse(q['passed'],q)
                self.assertTrue(q['target_confirmation']['required'])
                self.assertIn('target_tone_not_confirmed_in_both_halves',q['rejection_reasons'])

    def test_clear_target_keeps_original_path_and_previous_tone_gate_still_applies(self):
        q=quality_measurement(tone(2000,10000),RATE,2000,21000,SweepConfig())
        self.assertTrue(q['passed'])
        self.assertFalse(q['target_confirmation']['required'])
        x=tone(2000,10000)+tone(21000,10000)+tone(1200,10000,amplitude=.1)
        q=quality_measurement(x,RATE,2000,21000,SweepConfig())
        self.assertTrue(q['target_confirmation']['confirmed'])
        self.assertFalse(q['passed'])
        self.assertIn('previous_tone_not_sufficiently_rejected',q['rejection_reasons'])

    def test_transient_previous_tone_is_rejected_even_if_only_one_half_contains_it(self):
        for seconds in (.02,.1,1.):
            n=round(RATE*seconds)
            x=tone(2000,n,noise=.001)+np.r_[tone(1000,n//2,amplitude=.03,noise=0),np.zeros(n-n//2)]
            q=quality_measurement(x,RATE,2000,1000,SweepConfig())
            self.assertTrue(q['previous_tone_present'],q)
            self.assertFalse(q['passed'],q)
            self.assertIn('previous_tone_not_sufficiently_rejected',q['rejection_reasons'])

    def test_float_roundoff_is_not_a_target_above_a_near_zero_noise_floor(self):
        n=500000
        x=tone(12000,20000,noise=0).repeat(101)[:n]
        q=quality_measurement(x,RATE,2000,None,SweepConfig())
        self.assertFalse(q['passed'],q)
        self.assertIn('target_tone_below_numerical_resolution',q['rejection_reasons'])

    def test_late_target_has_time_for_two_verifications_and_measurement_within_eight_seconds(self):
        x=np.r_[tone(1200,round(2*RATE)),tone(2000,round(6*RATE))]
        runner=ReplayRunner([x])
        with patch('builtins.print'):
            result=runner.run(enable_plot=False)
        self.assertEqual(result['all_measured_tone_records'],[[2000.]])
        q=runner.quality[-1]
        self.assertEqual(q['verified_windows_before'],2)
        self.assertLessEqual((q['window_start']+q['window_count'])/RATE,8)
        self.assertLessEqual(max(r['window_duration_s'] for r in runner.quality),2.)
        starts=[r['window_start'] for r in runner.quality]
        self.assertEqual(starts,[0]+[r['window_start']+r['window_count']-r['retained_prefix_count'] for r in runner.quality[:-1]])
        passed=[r for r in runner.quality if r['passed']]
        self.assertTrue(all(a['window_start']+a['window_count']<=b['window_start'] for a,b in zip(passed,passed[1:])))


if __name__ == '__main__':unittest.main()
