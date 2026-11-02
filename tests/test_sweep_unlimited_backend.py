"""Unlimited sweep lifetime with bounded point/initialization/shutdown safeguards.

All devices, subprocesses and long time passages are simulated; no RF access.
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from sspp_experiments.material_sweep import acquisition
from sspp_experiments.material_sweep.budget import budget_info
from sspp_experiments.material_sweep.logic import SweepConfig
from sspp_lab.controller import DEFAULTS, LabController, LabError, validate_request
from sspp_lab.hardware import SDRConfig
from test_sweep_quality import ReplayRunner, tone


class UnlimitedRequestTests(unittest.TestCase):
    def test_default_and_legacy_api_limits_are_ignored(self):
        self.assertIsNone(DEFAULTS['max_seconds'])
        for old_value in (None, 90, 150, 3600, 86400):
            with self.subTest(old_value=old_value):
                cfg = validate_request({'max_seconds': old_value, 'points': 100,
                                        'repeat': 20, 'tone_step': 0}, require_setup=False)
                self.assertIsNone(cfg['max_seconds'])
                self.assertEqual(cfg['point_timeout'], 8)
                plan = budget_info(cfg)
                self.assertIsNone(plan['max_seconds'])
                self.assertIn('无总时限', plan['summary'])
                self.assertIn('按设定轮数完成', plan['summary'])
                self.assertIsNone(plan['warning'])
                self.assertNotIn('recommended_seconds', plan)

    def test_legacy_cli_limits_cannot_reactivate_timer(self):
        for extra in ([], ['--max-seconds', '150'], ['--max-seconds', '3600'], ['--max-seconds', '86400']):
            with self.subTest(extra=extra), patch.object(sys, 'argv', ['run_local.py', 'plan', *extra]):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    opts = acquisition.parse_args()
                self.assertIsNone(opts.max_seconds)
                self.assertEqual(opts.point_timeout, 8)
                if extra:
                    self.assertIn('ignored', output.getvalue())
                    self.assertIn('no total time limit', output.getvalue())

    def test_commands_remove_sweep_limit_and_preserve_phase_duration(self):
        controller = LabController('/tmp/example')
        for mode in ('sweep', 'phase'):
            cfg = validate_request({'mode': mode, 'duration': 70, 'max_seconds': 90}, require_setup=False)
            cmd = controller.command(cfg, Path('/tmp/profile.json'))
            self.assertNotIn('--max-seconds', cmd)
            if mode == 'phase':
                self.assertEqual(cmd[cmd.index('--duration')+1], '70')
                self.assertIsNone(budget_info(cfg))
            else:
                self.assertNotIn('--duration', cmd)
                self.assertEqual(cmd[cmd.index('--point-timeout')+1], '8')
        for value in (0, 9, 181, float('inf')):
            with self.subTest(value=value), self.assertRaises(LabError):
                validate_request({'mode': 'phase', 'duration': value}, require_setup=False)

    def test_point_bounds_remain_independent_for_api_and_cli_config(self):
        for value in (0, .99, 30.01, float('inf'), float('nan')):
            with self.subTest(value=value), self.assertRaises(LabError):
                validate_request({'point_timeout': value}, require_setup=False)
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder)/'fake.bin'
            image.write_bytes(b'offline-profile-only')
            profile = Path(folder)/'profile.json'
            profile.write_text(json.dumps({'serial': 'offline', 'channel': 0,
                'fpga': str(image), 'fpga_sha256': hashlib.sha256(image.read_bytes()).hexdigest()}))
            with patch.object(sys, 'argv', ['run_local.py', 'plan', '--device-config', str(profile)]):
                opts = acquisition.parse_args()
            for value in (1, 30):
                opts.point_timeout = value
                acquisition.make_config(opts)
            for value in (0, 31, float('inf'), float('nan')):
                opts.point_timeout = value
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Point timeout'):
                    acquisition.make_config(opts)

    def test_continuous_plan_has_manual_stop_and_no_global_duration(self):
        cfg = {**DEFAULTS, 'max_seconds': 150, 'temperature_sweep': {'mode': 'continuous'}}
        plan = budget_info(cfg)
        self.assertIsNone(plan['max_seconds'])
        self.assertIn('手动停止', plan['summary'])
        self.assertIn('无总时限', plan['summary'])


class WatchdogAndStopTests(unittest.TestCase):
    def test_sweep_survives_past_old_150_and_3600_second_limits(self):
        for continuous in (False, True):
            with self.subTest(continuous=continuous):
                controller = LabController()
                cfg = {**DEFAULTS, 'max_seconds': 150,
                       'temperature_sweep': {'mode': 'continuous'} if continuous else None}
                job = {'config': cfg, 'hardware_ready': True, 'status': 'running', 'errors': []}
                proc = Mock()
                proc.poll.side_effect = [None, None, None, 0]
                with patch('sspp_lab.controller.time.monotonic', side_effect=[0, 151, 3601, 86401]), \
                     patch('sspp_lab.controller.time.sleep'), patch.object(controller, '_end_process') as end:
                    controller._watchdog(proc, job)
                end.assert_not_called()
                self.assertEqual(job['status'], 'running')
                self.assertEqual(job['errors'], [])

    def test_initialization_timeout_is_retained_without_global_limit(self):
        controller = LabController()
        proc = Mock()
        proc.poll.return_value = None
        job = {'config': {**DEFAULTS}, 'status': 'starting', 'errors': []}
        with patch('sspp_lab.controller.time.monotonic', side_effect=[0, 31]), \
             patch.object(controller, '_end_process') as end:
            controller._watchdog(proc, job)
        end.assert_called_once_with(proc, job)
        self.assertTrue(job['timeout_requested'])
        self.assertIn('初始化', job['errors'][0])

    def test_phase_watchdog_still_respects_duration(self):
        controller = LabController()
        proc = Mock()
        proc.poll.return_value = None
        job = {'config': {**DEFAULTS, 'mode': 'phase', 'duration': 70},
               'hardware_ready': True, 'status': 'running', 'errors': []}
        with patch('sspp_lab.controller.time.monotonic', side_effect=[0, 116]), \
             patch.object(controller, '_end_process') as end:
            controller._watchdog(proc, job)
        end.assert_called_once_with(proc, job)
        self.assertTrue(job['timeout_requested'])

    def test_operator_shutdown_retains_bounded_escalation(self):
        for continuous, first_wait in ((False, 8), (True, 60)):
            with self.subTest(continuous=continuous):
                controller = LabController()
                proc = Mock()
                proc.poll.return_value = None
                proc.wait.side_effect = [subprocess.TimeoutExpired('offline', first_wait),
                                        subprocess.TimeoutExpired('offline', 5), 0]
                job = {'config': {**DEFAULTS,
                    'temperature_sweep': {'mode': 'continuous'} if continuous else None}}
                controller._end_process(proc, job)
                self.assertEqual([call.args[0] for call in proc.send_signal.call_args_list],
                                 [signal.SIGINT, signal.SIGTERM])
                self.assertEqual([call.kwargs['timeout'] for call in proc.wait.call_args_list],
                                 [first_wait, 5, 3])
                proc.kill.assert_called_once()
                self.assertTrue(job['forced_exit'])


class CompletionTests(unittest.TestCase):
    def samples(self, count):
        return [np.concatenate([tone(2000, 5000), tone(2000, 5000), tone(2000, 10000)])
                for _ in range(count)]

    def test_finite_engine_finishes_configured_rounds(self):
        cfg = SweepConfig(num_points=1, sweep_repeat=2)
        runner = ReplayRunner(self.samples(3), cfg)
        with contextlib.redirect_stdout(io.StringIO()):
            result = runner.run(enable_plot=False)
        self.assertEqual(len(result['all_amp_records']), 2)
        self.assertTrue(all(len(row) == 1 for row in result['all_amp_records']))
        self.assertFalse(runner.stop_event.is_set())

    def test_continuous_engine_exceeds_repeat_count_until_operator_stop(self):
        accepted = []
        def on_accept(runner):
            accepted.append(True)
            if len(accepted) == 3:
                runner.stop_event.set()
        cfg = SweepConfig(num_points=1, sweep_repeat=1, continuous=True)
        runner = ReplayRunner(self.samples(4), cfg, on_accept=on_accept)
        with contextlib.redirect_stdout(io.StringIO()):
            result = runner.run(enable_plot=False)
        self.assertEqual(len(result['all_amp_records']), 3)
        self.assertTrue(runner.stop_event.is_set())

    def test_cancel_finite_engine_preserves_completed_partial_rounds(self):
        def on_accept(runner):
            runner.stop_event.set()
        runner = ReplayRunner(self.samples(4), SweepConfig(num_points=1, sweep_repeat=3), on_accept=on_accept)
        with contextlib.redirect_stdout(io.StringIO()):
            result = runner.run(enable_plot=False)
        self.assertEqual(len(result['all_amp_records']), 1)
        self.assertEqual(len(result['all_amp_records'][0]), 1)
        self.assertTrue(runner.stop_event.is_set())


class MainLifetimeTests(unittest.TestCase):
    def test_main_never_constructs_timer_and_saves_completion_policy(self):
        for continuous in (False, True):
            with self.subTest(continuous=continuous), tempfile.TemporaryDirectory() as folder:
                thermal = {'mode': 'continuous'} if continuous else None
                opts = SimpleNamespace(command='acquire', temperature_config=json.dumps(thermal) if thermal else None,
                    headless=True, label='lifetime_offline', output_root=Path(folder), freq_start=5e9,
                    freq_stop=5.3e9, points=2, repeat=2, point_timeout=8., max_seconds=150)
                sdr = SDRConfig(tx_rate_actual=250000., rx_rate_actual=250000.)
                cfg = SweepConfig(num_points=2, sweep_repeat=2)
                handlers = {}
                runner_called = []
                stopped = []
                test = self
                class Manager:
                    def __init__(self, args):
                        self.usrp = SimpleNamespace(get_master_clock_rate=lambda: 32e6,
                            get_tx_gain=lambda _: 20, get_rx_gain=lambda _: 30,
                            get_tx_antenna=lambda _: 'TX/RX', get_rx_antenna=lambda _: 'RX2',
                            get_tx_subdev_name=lambda _: 'offline', get_rx_subdev_name=lambda _: 'offline')
                    def configure(self): pass
                    def create_streamers(self): return None, None
                    def start_rx_stream(self): pass
                    def stop_rx_stream(self): stopped.append(True)
                class Worker:
                    def __init__(self, *, target, args, daemon): self.target, self.args = target, args
                    def start(self): self.target(*self.args)
                    def join(self, timeout): test.assertEqual(timeout, 3)
                    def is_alive(self): return False
                class Publisher:
                    def start(self): pass
                    def stats(self): return {}
                    def close(self): pass
                class Companion:
                    point_count = 2
                    def __init__(self, *args): pass
                    def start(self): pass
                    def ensure_fresh(self): pass
                    def close(self): return {'status': 'complete'}
                def rx(stream, args, stop, queue, telemetry, failures):
                    queue.put((0, np.zeros(2, dtype=np.complex64)))
                    telemetry['rx_samples'] = 2
                def tx(stream, args, stop, telemetry, failures):
                    telemetry.update(tx_samples=2, tx_end_of_burst_sent=True)
                def make_runner(args, cfg, manager, queue, stop, *rest):
                    def run(**kwargs):
                        test.assertFalse(stop.is_set())
                        test.assertEqual(cfg.continuous, continuous)
                        runner_called.append(True)
                        if continuous:
                            handlers[signal.SIGINT](signal.SIGINT, None)
                            test.assertTrue(stop.is_set())
                        return {'offline': True}
                    return SimpleNamespace(run=run, flush_point=lambda: None, raw_files=[],
                        quality_attempt_count=0, rejected_measurement_count=0)
                output = io.StringIO()
                with patch.object(acquisition, 'parse_args', return_value=opts), \
                     patch.object(acquisition, 'make_config', return_value=({}, sdr, cfg)), \
                     patch.object(acquisition, 'RealtimePublisher', Publisher), \
                     patch.object(acquisition, 'make_runner', side_effect=make_runner), \
                     patch.object(acquisition, 'rx_loop', side_effect=rx), \
                     patch.object(acquisition, 'tx_loop', side_effect=tx), \
                     patch.object(acquisition, 'save_outputs', return_value={'complete': True, 'counts': [2]}), \
                     patch.object(acquisition.threading, 'Timer', side_effect=AssertionError('Global sweep timer forbidden')), \
                     patch.object(acquisition.threading, 'Thread', Worker), \
                     patch.object(acquisition.time, 'sleep'), \
                     patch.object(acquisition.time, 'monotonic', return_value=86401), \
                     patch.object(acquisition.signal, 'signal', side_effect=lambda sig, fn: handlers.setdefault(sig, fn)), \
                     patch('sspp_lab.hardware.USRPManager', Manager), \
                     patch('sspp_experiments.temperature_sweep.metadata.validate_config', return_value=thermal), \
                     patch('sspp_experiments.temperature_sweep.acquisition.TemperatureSweepCapture', Companion), \
                     contextlib.redirect_stdout(output):
                    code = acquisition.main()
                self.assertEqual(code, 0, output.getvalue())
                self.assertEqual(runner_called, [True])
                self.assertEqual(stopped, [True])
                manifest = json.loads(next(Path(folder).glob('*/manifest.json')).read_text())
                self.assertEqual(manifest['time_policy'], {
                    'total_time_limit_seconds': None, 'completion': 'operator_stop' if continuous else 'configured_rounds',
                    'point_timeout_seconds': 8., 'weak_point_extra_seconds':8.,
                    'point_max_timeout_seconds':16.,
                    'weak_point_extension_policy':'one_extension_after_qualified_long_verify_before_base_deadline'})
                self.assertEqual(manifest['errors'], [])
                self.assertEqual(manifest['telemetry'].get('stop_reason'), 'operator_stop' if continuous else None)
                self.assertTrue(manifest['telemetry']['rf_stopped_confirmed'])
                self.assertIn('no total time limit', output.getvalue())


if __name__ == '__main__':
    unittest.main()
