"""射频质量判定与状态机离线测试：合成 IQ/已保存 IQ，不打开设备。"""

import csv
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

from sspp_experiments.material_sweep.logic import (
    SWEEP_ALGORITHM_VERSION, SweepConfig, SweepRunner, fft_peak, quality_measurement,
)
from sspp_experiments.material_sweep.acquisition import make_runner, save_outputs

RATE = 250000.


def tone(frequency, count, amplitude=.01, seed=42, noise=.0003):
    rng = np.random.default_rng(seed)
    values = amplitude*np.exp(2j*np.pi*frequency*np.arange(count)/RATE)
    return (values + noise*(rng.normal(size=count)+1j*rng.normal(size=count))).astype(np.complex64)


class ReplayRunner(SweepRunner):
    def __init__(self, sequences, cfg=None, fail_after=20, on_accept=None):
        cfg = cfg or SweepConfig(num_points=len(sequences), sweep_repeat=1)
        args = SimpleNamespace(rate=RATE, rx_rate_actual=RATE)
        super().__init__(args, cfg, None, None, threading.Event())
        self.sequences = sequences
        self.quality = []
        self.retunes = 0
        self.empty_reads = 0
        self.fail_after = fail_after
        self.on_accept = on_accept

    def _set_freq_and_tone(self, freq, selected_tone):
        self.chunks = [self.sequences[self.retunes][i:i+8192]
                       for i in range(0, len(self.sequences[self.retunes]), 8192)]
        self.retunes += 1
        self.empty_reads = 0

    def _clear_queue(self):
        pass

    def _fetch_available_frames(self):
        if self.chunks:
            chunk = self.chunks.pop(0)
            return [chunk], chunk
        self.empty_reads += 1
        if self.empty_reads > self.fail_after:
            raise TimeoutError("mock original point deadline")
        return [], None

    def on_quality_attempt(self, record):
        self.quality.append(record)

    def on_measurement(self, repeat, point, freq, expected, measured, amplitude):
        if self.on_accept:
            self.on_accept(self)


class FullWindowQualityTests(unittest.TestCase):
    def test_fft_uses_full_input_even_if_tail_is_different_tone(self):
        x = tone(2000, 10000, noise=0)
        x[-2048:] = tone(1200, 2048, amplitude=.004, noise=0)
        measured, amp = fft_peak(x, RATE, nfft=2048, expected_freq=2000)
        cropped, _ = fft_peak(x[-2048:], RATE, nfft=2048, expected_freq=2000)
        self.assertEqual(measured, 2000.)
        self.assertGreater(abs(cropped-2000), 200)
        self.assertGreater(amp, -43)

    def test_minimum_actual_windows_apply_at_all_tones_and_rates(self):
        for rate in (200000., 250000., 2000000.):
            for frequency in (2000., 21000.):
                runner = SweepRunner(SimpleNamespace(rate=rate, rx_rate_actual=rate), SweepConfig(), None, None, threading.Event())
                verify, measure = runner._calc_samples(frequency)
                self.assertGreaterEqual(verify/rate, .020)
                self.assertGreaterEqual(measure/rate, .040)

    def test_pure_target_accepted_but_noise_wrong_and_previous_tones_rejected(self):
        cfg = SweepConfig()
        good = quality_measurement(tone(2000, 10000), RATE, 2000, 21000, cfg)
        self.assertTrue(good["passed"])
        # 单个噪声窗可能偶然越过工程门限；噪声拒绝另以独立多窗状态机验证。
        for x in (tone(3000, 10000), tone(21000, 10000),
                  tone(2000, 10000, amplitude=.001)+tone(21000, 10000, amplitude=.01)):
            quality = quality_measurement(x, RATE, 2000, 21000, cfg)
            self.assertFalse(quality["passed"], quality)
        self.assertEqual(good["fft_length"], 10000)
        self.assertEqual(good["window_count"], 10000)

    def test_200hz_frequency_tolerance_not_relaxed(self):
        cfg = SweepConfig()
        result = quality_measurement(tone(2200, 10000, noise=0), RATE, 2000, None, cfg)
        self.assertIn("tone_frequency_out_of_tolerance", result["rejection_reasons"])
        self.assertEqual(cfg.verify_tolerance_hz, 200)

    def test_exact_saved_bad_window_recovered_without_rewriting_history(self):
        source = Path(__file__).resolve().parents[1]/"Data/runs/20261031T084134_856931Z/iq_point_021.npz"
        if not source.is_file():
            self.skipTest("optional local historical IQ unavailable")
        with np.load(source) as archive:
            x = archive["iq"].copy()
        result = quality_measurement(x[8192:18192], RATE, 2000, 21000, SweepConfig())
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["measured_tone_hz"], 2000)
        self.assertAlmostEqual(result["amplitude_dB"], -84.66812655628088, places=5)
        verify = quality_measurement(x[:5000], RATE, 2000, 21000, SweepConfig())
        self.assertFalse(verify["passed"])
        self.assertIn("previous_tone_not_sufficiently_rejected", verify["rejection_reasons"])


class SweepQualityStateTests(unittest.TestCase):
    def run_quietly(self, runner):
        with patch("builtins.print"):
            return runner.run(enable_plot=False)

    def test_measurement_failure_reverifies_same_point_and_preserves_exact_windows(self):
        sequence = np.concatenate([tone(2000, 5000), tone(2000, 5000), tone(3000, 10000),
                                   tone(2000, 5000), tone(2000, 5000), tone(2000, 10000)])
        # Preserve the explicit legacy two-rejection schedule and its exact windows.
        runner = ReplayRunner([sequence], SweepConfig(num_points=1, sweep_repeat=1,
                                                     adaptive_rejections_per_level=2,
                                                     adaptive_window_seconds=(.02,.05,.1,.25,.5,1.,2.)))
        result = self.run_quietly(runner)
        self.assertEqual(runner.retunes, 1)
        self.assertEqual(result["all_measured_tone_records"], [[2000.]])
        self.assertEqual([q["state"] for q in runner.quality], ["VERIFY", "VERIFY", "MEASURE"]*2)
        self.assertEqual([q["window_start"] for q in runner.quality], [0,5000,10000,20000,25000,30000])
        self.assertFalse(runner.quality[2]["accepted"])
        self.assertTrue(runner.quality[-1]["accepted"])
        self.assertEqual(result["all_quality_records"][0][0]["window_start"], 30000)
        np.testing.assert_array_equal(sequence[30000:40000], tone(2000,10000))

    def test_one_valid_verify_followed_by_noise_does_not_unlock_measurement(self):
        sequence = np.concatenate([tone(2000,5000), tone(3000,5000),
                                   tone(2000,5000), tone(2000,5000),tone(2000,10000)])
        runner = ReplayRunner([sequence], SweepConfig(num_points=1, sweep_repeat=1,
                                                     adaptive_rejections_per_level=2,
                                                     adaptive_window_seconds=(.02,.05,.1,.25,.5,1.,2.)))
        self.run_quietly(runner)
        self.assertEqual([q["state"] for q in runner.quality], ["VERIFY"]*4+["MEASURE"])
        self.assertFalse(runner.quality[1]["passed"])

    def test_stop_keeps_already_accepted_point_and_excludes_unmeasured_next(self):
        runner = ReplayRunner([tone(2000,20000),tone(3000,20000)],
                              on_accept=lambda r:r.stop_event.set())
        result = self.run_quietly(runner)
        self.assertEqual(len(result["all_amp_records"][0]), 1)
        self.assertEqual(runner.retunes, 1)

    def test_exception_keeps_partial_results_and_invalid_point_out(self):
        runner = ReplayRunner([tone(2000,20000),tone(12000,20000)], fail_after=1)
        with self.assertRaises(TimeoutError):
            self.run_quietly(runner)
        self.assertEqual(len(runner.partial_result["all_amp_records"][0]), 1)
        self.assertEqual(runner.retunes, 2)
        self.assertFalse(any(q["accepted"] for q in runner.quality if q["point"] == 2))

    def test_recorded_runner_stalled_iq_is_hard_failure_and_deadline_not_reset(self):
        with tempfile.TemporaryDirectory() as root:
            args = SimpleNamespace(rate=RATE, rx_rate_actual=RATE, channel=0)
            cfg = SweepConfig(num_points=1, sweep_repeat=1)
            manager = MagicMock()
            manager.usrp.get_tx_freq.return_value = manager.usrp.get_rx_freq.return_value = 5e9
            queue = Queue()
            runner = make_runner(args,cfg,manager,queue,threading.Event(),None,Path(root),
                                 SimpleNamespace(point_timeout=8),[])
            source = tone(12000,100000)
            def fill():
                for start in range(0,len(source),8192):queue.put((start,source[start:start+8192]))
            runner._clear_queue = fill
            clock = [0.]
            def now():
                clock[0] += .5
                return clock[0]
            with patch("sspp_experiments.material_sweep.logic.time.monotonic",side_effect=now), \
                 patch("sspp_experiments.material_sweep.logic.time.sleep"), patch("builtins.print"):
                with self.assertRaisesRegex(RuntimeError, "RX data stalled"):
                    runner.run(enable_plot=False)
            self.assertEqual(manager.set_center_freq.call_count, 1)
            self.assertEqual(runner.deadline, runner.point_started_monotonic_s+8.)
            self.assertEqual(runner.deadline,runner.base_deadline)
            self.assertEqual(runner.point_budget_extensions,[])
            self.assertEqual(runner.partial_result["all_amp_records"], [[]])
            runner.flush_point()
            log = [json.loads(line) for line in (Path(root)/"sweep_quality.jsonl").read_text().splitlines()]
            self.assertTrue(log)
            self.assertTrue(all(not q["accepted"] for q in log))
            with np.load(Path(root)/"iq_point_001.npz") as archive:
                for q in log:
                    segment = archive["iq"][q["window_start"]:q["window_start"]+q["window_count"]]
                    recalculated = quality_measurement(segment,RATE,2000,None,cfg)
                    self.assertEqual(recalculated["rejection_reasons"],q["rejection_reasons"])

    def test_measurement_csv_exports_accepted_window_and_version(self):
        runner = ReplayRunner([tone(2000,20000)])
        result = self.run_quietly(runner)
        with tempfile.TemporaryDirectory() as root:
            quality = save_outputs(result,Path(root),runner.cfg)
            self.assertTrue(quality["complete"])
            with (Path(root)/"measurements.csv").open() as f:row=next(csv.DictReader(f))
            self.assertEqual(row["algorithm_version"],SWEEP_ALGORITHM_VERSION)
            self.assertEqual(row["accepted_window_start"],"10000")
            self.assertEqual(row["accepted_window_count"],"10000")


class AdaptiveSweepTests(unittest.TestCase):
    def run_quietly(self, runner):
        with patch("builtins.print"):
            return runner.run(enable_plot=False)

    def test_isolated_verify_pass_does_not_erase_rejections_and_level_restarts_verification(self):
        sequence=np.concatenate([tone(3000,5000),tone(2000,5000),tone(3000,5000),
                                 tone(2000,25000),tone(2000,25000),tone(2000,25000)])
        runner=ReplayRunner([sequence], SweepConfig(num_points=1, sweep_repeat=1,
                                                   adaptive_rejections_per_level=2,
                                                   adaptive_window_seconds=(.02,.05,.1,.25,.5,1.,2.)))
        result=self.run_quietly(runner)
        self.assertEqual(runner.retunes,1)
        self.assertEqual([q['adaptive_level_index'] for q in runner.quality],[0,0,0,1,1,1])
        self.assertEqual([q['window_count'] for q in runner.quality],[5000]*3+[12500]*3)
        self.assertEqual([q['window_start'] for q in runner.quality],[0,5000,10000,10000,22500,35000])
        self.assertEqual(runner.quality[2]['level_rejections_after'],2)
        self.assertEqual(runner.quality[3]['verified_windows_before'],0)
        self.assertEqual(runner.quality[2]['retained_prefix_count'],5000)
        self.assertTrue(all(q['retained_prefix_count']==0 for q in runner.quality if q['passed']))
        self.assertEqual(result['all_quality_records'][0][0]['window_duration_s'],.05)

    def test_cycle_and_minimum_duration_cannot_bypass_one_second_window_cap(self):
        for config in (SweepConfig(tone_start=10), SweepConfig(measure_min_seconds=1.01),
                       SweepConfig(verify_min_seconds=1.01), SweepConfig(measure_cycles=3000)):
            with self.subTest(config=config), self.assertRaisesRegex(ValueError,'1 秒窗口上限'):
                config.validate(RATE)

    def test_evidenced_initial_carryover_holds_short_windows_only_until_cap(self):
        for old_count, expected_level in ((60000,0),(65000,1)):
            with self.subTest(old_count=old_count):
                second=np.concatenate([tone(2000,old_count),tone(3000,90000)])
                runner=ReplayRunner([tone(2000,20000),second])
                self.run_quietly(runner)
                records=[row for row in runner.quality if row['point']==2]
                held=[row for row in records if row['carryover_hold']]
                self.assertTrue(held)
                self.assertTrue(all(row['previous_tone_present'] and not row['accepted'] for row in held))
                self.assertTrue(all((row['window_start']+row['window_count'])/RATE<=.250 for row in held))
                self.assertEqual(records[-1]['adaptive_level_index'],expected_level)
                if expected_level==0:
                    self.assertGreater(records[-1]['level_rejections_before'],2)
                    self.assertEqual(records[-1]['window_count'],10000)
                else:
                    upgraded=next(row for row in records if row['next_adaptive_level_index']>row['adaptive_level_index'])
                    self.assertFalse(upgraded['carryover_hold'])
                    self.assertGreater((upgraded['window_start']+upgraded['window_count'])/RATE,.250)

    def test_measure_failure_counts_toward_adaptation_and_cannot_be_accepted(self):
        sequence=np.concatenate([tone(3000,5000),tone(2000,10000),tone(3000,10000),
                                 tone(2000,75000)])
        runner=ReplayRunner([sequence], SweepConfig(num_points=1, sweep_repeat=1,
                                                   adaptive_rejections_per_level=2,
                                                   adaptive_window_seconds=(.02,.05,.1,.25,.5,1.,2.)))
        self.run_quietly(runner)
        rejected=[q for q in runner.quality if q['state']=='MEASURE' and not q['passed']]
        self.assertEqual(len(rejected),1)
        self.assertFalse(rejected[0]['accepted'])
        self.assertEqual(rejected[0]['next_adaptive_level_index'],1)
        self.assertTrue(runner.quality[-1]['accepted'])
        self.assertEqual(runner.quality[-1]['window_count'],12500)

    def test_shifted_previous_tone_has_independent_evidence_and_no_half_grid_attenuation_hole(self):
        cfg=SweepConfig()
        for duration in (.02,.1,1):
            count=round(duration*RATE)
            for offset in (-199.9,-151,-99,99,101,149,151,199,199.9):
                with self.subTest(duration=duration,offset=offset):
                    samples=tone(2000,count,noise=0)+tone(21000+offset,count,noise=0)
                    result=quality_measurement(samples,RATE,2000,21000,cfg)
                    self.assertTrue(result['previous_tone_present'],result)
                    self.assertFalse(result['passed'],result)
                    self.assertIn('previous_tone_not_sufficiently_rejected',result['rejection_reasons'])
                    self.assertLess(result['previous_tone_margin_db'],6)

    def test_noise_band_peak_alone_is_not_previous_tone_evidence(self):
        source=Path(__file__).resolve().parents[1]/'Data/runs/20261031T092647_920529Z/iq_point_020.npz'
        if not source.is_file():
            self.skipTest('optional historical IQ unavailable')
        with np.load(source) as archive:samples=archive['iq'][85000:90000].copy()
        result=quality_measurement(samples,RATE,21000,20000,SweepConfig())
        self.assertTrue(result['passed'],result)
        self.assertFalse(result['previous_tone_present'],result)
        self.assertEqual(result['previous_tone_gate_status'],'not_present_above_floor')

    def test_noise_only_is_not_accepted_by_full_adaptive_state_machine_across_seeds(self):
        # 单窗随机突出不等于独立两次验证+正式窗均有真实单音；不换 seed 隐藏单窗假阳性。
        for seed in range(12):
            with self.subTest(seed=seed):
                samples=tone(2000,round(8*RATE),amplitude=0,seed=seed,noise=.001)
                runner=ReplayRunner([samples],fail_after=1)
                with self.assertRaises(TimeoutError):
                    self.run_quietly(runner)
                self.assertFalse(runner.partial_result['all_amp_records'][0])
                self.assertFalse(any(q['accepted'] for q in runner.quality))

    def test_cancellation_while_waiting_longer_window_leaves_no_accepted_measurement(self):
        samples=tone(3000,160000)
        runner=ReplayRunner([samples])
        original=runner.on_quality_attempt
        def stop_on_upgrade(record):
            original(record)
            if record['next_adaptive_level_index']==2:
                runner.stop_event.set()
        runner.on_quality_attempt=stop_on_upgrade
        result=self.run_quietly(runner)
        self.assertEqual(result['all_amp_records'],[[]])
        self.assertEqual(runner.retunes,1)
        self.assertFalse(any(q['accepted'] for q in runner.quality))

    def test_post_processing_deadline_rejects_passed_measurement_but_logs_attempt(self):
        runner=ReplayRunner([tone(2000,20000)])
        runner.point_deadline_expired=lambda:len(runner.quality)>=2
        with self.assertRaisesRegex(TimeoutError,'during window processing'):
            self.run_quietly(runner)
        self.assertEqual(runner.partial_result['all_amp_records'],[[]])
        self.assertTrue(runner.quality[-1]['passed'])
        self.assertTrue(runner.quality[-1]['point_deadline_expired'])
        self.assertFalse(runner.quality[-1]['accepted'])


if __name__ == "__main__":
    unittest.main()
