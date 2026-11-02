"""Fault injection for sweep transport/stop evidence; never opens hardware."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sspp_experiments.material_sweep import acquisition
from sspp_lab.controller import DEFAULTS, LabController
from sspp_lab.hardware import SDRConfig
from sspp_lab.sweep_logic import SweepConfig


class TxEvidenceTests(unittest.TestCase):
    def run_tx(self, counts, *, eob=1):
        stop = threading.Event()
        telemetry = {'tx_samples': 0, 'tx_partial_sends': 0, 'tx_end_of_burst_sent': True}
        failures = []
        values = iter(counts)
        class Stream:
            def send(self, data, metadata, timeout):
                if metadata.end_of_burst:
                    if isinstance(eob, Exception):
                        raise eob
                    return eob
                value = next(values)
                if isinstance(value, Exception):
                    raise value
                if value == len(data):
                    stop.set()
                return value
        acquisition.tx_loop(Stream(), SDRConfig(), stop, telemetry, failures)
        return telemetry, failures

    def test_partial_samples_survive_following_zero(self):
        telemetry, failures = self.run_tx([3000, 2000, 0])
        self.assertEqual(telemetry['tx_samples'], 5000)
        self.assertEqual(telemetry['tx_partial_sends'], 2)
        self.assertTrue(telemetry['tx_end_of_burst_sent'])
        self.assertEqual(failures, ['TX: TX send returned zero samples'])

    def test_partial_samples_survive_following_exception(self):
        telemetry, failures = self.run_tx([3000, RuntimeError('USB I/O error')])
        self.assertEqual(telemetry['tx_samples'], 3000)
        self.assertIn('USB I/O error', failures[0])

    def test_eob_zero_is_not_success(self):
        telemetry, failures = self.run_tx([10000], eob=0)
        self.assertEqual(telemetry['tx_samples'], 10000)
        self.assertFalse(telemetry['tx_end_of_burst_sent'])
        self.assertEqual(len(failures), 1)
        self.assertIn('TX shutdown: TX EOB send accepted 0 of 1 sample', failures[0])

    def test_eob_exception_is_not_success(self):
        telemetry, failures = self.run_tx([10000], eob=RuntimeError('USB disconnected'))
        self.assertFalse(telemetry['tx_end_of_burst_sent'])
        self.assertIn('USB disconnected', failures[0])

    def test_successful_partial_tx_counts_exactly(self):
        telemetry, failures = self.run_tx([3000, 3000, 4000])
        self.assertEqual(telemetry['tx_samples'], 10000)
        self.assertTrue(telemetry['tx_end_of_burst_sent'])
        self.assertFalse(failures)


class StopReportTests(unittest.TestCase):
    def report(self, telemetry, failures):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            event = acquisition.report_stop_status(telemetry, failures)
        self.assertEqual(json.loads(output.getvalue().splitlines()[0]), event)
        return event, output.getvalue()

    def test_each_missing_shutdown_evidence_remains_unconfirmed(self):
        evidence = dict(tx_end_of_burst_sent=True, rx_stop_command_sent=True, workers_stopped=True)
        for key in evidence:
            with self.subTest(key=key):
                telemetry = {**evidence, key: False}
                event, output = self.report(telemetry, [])
                self.assertFalse(event['rf_stopped_confirmed'])
                self.assertFalse(telemetry['rf_stopped_confirmed'])
                self.assertNotIn('RX/TX stopped; saving results.', output)
        event, output = self.report(evidence, [])
        self.assertTrue(event['rf_stopped_confirmed'])
        self.assertIn('RX/TX stopped; saving results.', output)

    def test_no_streams_started_is_not_described_as_unconfirmed_shutdown(self):
        event, output = self.report({'rf_start_attempted': False}, [])
        self.assertIs(event['rf_start_attempted'], False)
        self.assertFalse(event['rf_stopped_confirmed'])
        self.assertIn('RF streams were not started; saving diagnostic results.', output)
        self.assertNotIn('RX/TX stop unconfirmed', output)

    def test_transport_diagnosis_requires_both_evidence_types(self):
        pairs = [(['TX: TX send returned zero samples'], False),
                 (['RX shutdown: wait_for_ack accum_timeout'], False),
                 (['Weak signal quality deadline exceeded'], False),
                 (['TX: TX send returned zero samples', 'RX shutdown: wait_for_ack accum_timeout'], True)]
        for failures, expected in pairs:
            with self.subTest(failures=failures):
                telemetry = {}
                event, output = self.report(telemetry, failures)
                self.assertEqual(bool(event['failure_diagnosis']), expected)
                if expected:
                    self.assertIn('具体物理原因尚未确定', failures[-1])


class ControllerEvidenceTests(unittest.TestCase):
    def collect_failed_run(self, telemetry, *, temperature_mode=None, hardware_ready=False,
                           manifest_status='failed_or_incomplete'):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root/'Data/runs/example'
            output.mkdir(parents=True)
            (output/'manifest.json').write_text(json.dumps({
                'status': manifest_status, 'errors': ['Idle sleep protection unavailable'],
                'telemetry': telemetry}))
            lab = LabController(root)
            job = {'id': 'test', 'config': {**DEFAULTS, 'temperature_sweep':
                {'mode': temperature_mode} if temperature_mode else None}, 'status': 'starting',
                'output': 'Data/runs/example', 'errors': [], 'rf_stopped': False,
                'hardware_ready': hardware_ready}
            class Process:
                stdout = io.StringIO(json.dumps({'event': 'STOP_STATUS',
                    'rf_stopped_confirmed': False})+'\n')
                def wait(self): return 2
                def poll(self): return 2
            with patch('sspp_lab.controller.subprocess.Popen',
                       side_effect=AssertionError('Offline status test must not launch a process')):
                lab._collect(Process(), job)
            saved = json.loads((output/'web_context.json').read_text())
            self.assertEqual(saved['web_status'], job['status'])
            self.assertFalse(saved['rf_stopped_confirmed'])
            return job

    def test_pre_rf_failure_is_initialization_failed_for_all_sweep_modes(self):
        for mode in (None, 'manual', 'continuous'):
            for hardware_ready in (False, True):
                with self.subTest(mode=mode, hardware_ready=hardware_ready):
                    job = self.collect_failed_run({'rf_start_attempted': False},
                        temperature_mode=mode, hardware_ready=hardware_ready)
                    self.assertEqual(job['status'], 'initialization_failed')
                    self.assertIs(job['acquisition_started'], False)
                    self.assertFalse(job['rf_stopped'])
                    self.assertIn('Idle sleep protection unavailable', job['errors'])
                    self.assertIn('采集准备未完成，未启动射频收发', job['errors'])
                    self.assertFalse(any('未取得完整收发停止证据' in item for item in job['errors']))

    def test_attempted_or_unknown_rf_start_keeps_stop_unconfirmed(self):
        for telemetry in ({'rf_start_attempted': True}, {}, {'rf_start_attempted': None},
                          {'rf_start_attempted': 0}, {'rf_start_attempted': 'false'}):
            for mode in (None, 'manual', 'continuous'):
                with self.subTest(telemetry=telemetry, mode=mode):
                    job = self.collect_failed_run(telemetry, temperature_mode=mode)
                    self.assertEqual(job['status'], 'stop_unconfirmed')
                    self.assertFalse(job['rf_stopped'])
                    self.assertTrue(any('未取得完整收发停止证据' in item for item in job['errors']))

    def test_explicit_rf_start_attempt_wins_over_stale_initializing_status(self):
        job = self.collect_failed_run({'rf_start_attempted': True}, manifest_status='initializing')
        self.assertEqual(job['status'], 'stop_unconfirmed')
        self.assertFalse(job['rf_stopped'])

    def test_saving_event_never_claims_rf_stopped(self):
        for event_confirmed, actual_confirmed in [(False, False), (True, False), (True, True)]:
            with self.subTest(event=event_confirmed, actual=actual_confirmed), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                output = root/'Data/runs/example'
                output.mkdir(parents=True)
                (output/'manifest.json').write_text(json.dumps({
                    'status': 'complete', 'errors': [], 'telemetry': {
                        'tx_end_of_burst_sent': actual_confirmed,
                        'rx_stop_command_sent': True, 'workers_stopped': True}}))
                lab = LabController(root)
                job = {'id': 'test', 'config': DEFAULTS, 'status': 'running',
                       'output': 'Data/runs/example', 'errors': [], 'rf_stopped': False}
                test = self
                class Process:
                    stdout = io.StringIO(json.dumps({'event': 'STOP_STATUS',
                        'rf_stopped_confirmed': event_confirmed})+'\n')
                    def wait(self):
                        test.assertEqual(job['status'], 'saving')
                        test.assertFalse(job['rf_stopped'])
                        return 0
                    def poll(self):
                        return 0
                lab._collect(Process(), job)
                self.assertEqual(job['status'], 'complete' if actual_confirmed else 'stop_unconfirmed')
                self.assertEqual(job['rf_stopped'], actual_confirmed)
                stored = json.loads((output/'web_context.json').read_text())
                self.assertEqual(stored['rf_stopped_confirmed'], actual_confirmed)


class SweepShutdownIntegrationTests(unittest.TestCase):
    def test_main_retains_faults_and_emits_truthful_stop_status(self):
        for disconnected in (False, True):
            with self.subTest(disconnected=disconnected), tempfile.TemporaryDirectory() as folder:
                options = SimpleNamespace(command='acquire', temperature_config=None, headless=True,
                    label='fault_injection', output_root=Path(folder), freq_start=5e9, freq_stop=5.3e9,
                    points=2, repeat=1, max_seconds=10., point_timeout=8.)
                sdr = SDRConfig(tx_rate_actual=250000., rx_rate_actual=250000.)
                cfg = SweepConfig(num_points=2, sweep_repeat=1)
                class Tx:
                    calls = 0
                    def send(self, data, metadata, timeout):
                        if metadata.end_of_burst:
                            return 0 if disconnected else len(data)
                        time.sleep(.005)
                        self.calls += 1
                        return (3000 if self.calls == 1 else 0) if disconnected else len(data)
                class Rx:
                    def recv(self, buffer, metadata, timeout):
                        time.sleep(.005)
                        buffer[:] = 0
                        return buffer.shape[-1]
                class Manager:
                    def __init__(self, args):
                        self.usrp = SimpleNamespace(get_master_clock_rate=lambda: 32e6,
                            get_tx_gain=lambda _: 20, get_rx_gain=lambda _: 30,
                            get_tx_antenna=lambda _: 'TX/RX', get_rx_antenna=lambda _: 'RX2',
                            get_tx_subdev_name=lambda _: 'test', get_rx_subdev_name=lambda _: 'test')
                    def configure(self): pass
                    def create_streamers(self): return Tx(), Rx()
                    def start_rx_stream(self): pass
                    def stop_rx_stream(self):
                        if disconnected: raise RuntimeError('wait_for_ack: accum_timeout < _timeout')
                class Publisher:
                    def start(self): pass
                    def stats(self): return {}
                    def close(self): pass
                runner = SimpleNamespace(run=lambda **kwargs: {'test': True}, flush_point=lambda: None,
                    raw_files=[], quality_attempt_count=0, rejected_measurement_count=0)
                output = io.StringIO()
                with patch.object(acquisition, 'parse_args', return_value=options), \
                     patch.object(acquisition, 'make_config', return_value=({}, sdr, cfg)), \
                     patch.object(acquisition, 'RealtimePublisher', Publisher), \
                     patch.object(acquisition, 'make_runner', return_value=runner), \
                     patch.object(acquisition, 'save_outputs', return_value={'complete': True, 'counts': [1]}), \
                     patch('sspp_lab.hardware.USRPManager', Manager), \
                     patch.object(acquisition.signal, 'signal'), contextlib.redirect_stdout(output):
                    code = acquisition.main()
                manifest = json.loads(next(Path(folder).glob('*/manifest.json')).read_text())
                events = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
                status = next(event for event in events if event.get('event') == 'STOP_STATUS')
                self.assertEqual(code, 2 if disconnected else 0)
                self.assertEqual(status['rf_stopped_confirmed'], not disconnected)
                self.assertEqual(manifest['telemetry']['rf_stopped_confirmed'], not disconnected)
                if disconnected:
                    self.assertEqual(manifest['telemetry']['tx_samples'], 3000)
                    self.assertEqual(manifest['telemetry']['failure_diagnosis'], 'usrp_transport_control_unresponsive')
                    self.assertNotIn('RX/TX stopped; saving results.', output.getvalue())
                    self.assertTrue(any('TX EOB send accepted 0' in error for error in manifest['errors']))
                    self.assertTrue(any('wait_for_ack' in error for error in manifest['errors']))


if __name__ == '__main__':
    unittest.main()
