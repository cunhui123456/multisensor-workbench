"""No hardware: establish idle-sleep protection before either device opens."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sspp_experiments.material_sweep import acquisition
from sspp_lab.hardware import SDRConfig
from sspp_lab.sweep_logic import SweepConfig


class SweepPowerLifecycleTests(unittest.TestCase):
    def test_guard_failure_never_opens_rf_or_temperature(self):
        self.check_failure(guard_fails=True)

    def test_device_initialization_failure_releases_guard(self):
        self.check_failure(guard_fails=False)

    def check_failure(self, *, guard_fails):
        calls = []
        class Guard:
            active = False
            def start(self):
                calls.append('guard_start')
                if guard_fails:
                    raise RuntimeError('offline assertion unavailable')
                self.active = True
            def close(self):
                calls.append('guard_close')
                self.active = False
            def snapshot(self):
                return {'active': self.active, 'supported': True}
        guard = Guard()
        manager = Mock()
        def configure():
            self.assertTrue(guard.active)
            calls.append('configure')
            raise RuntimeError('offline device initialization failure')
        manager.configure.side_effect = configure
        companion = Mock(point_count=0)
        def start_temperature():
            self.assertTrue(guard.active)
            calls.append('temperature_start')
        companion.start.side_effect = start_temperature
        companion.close.return_value = {'status': 'failed_or_incomplete'}
        publisher = Mock()
        publisher.stats.return_value = {}
        with tempfile.TemporaryDirectory() as folder:
            thermal = {'mode': 'continuous'}
            opts = SimpleNamespace(command='acquire', temperature_config=json.dumps(thermal),
                headless=True, label='power_lifecycle', output_root=Path(folder), freq_start=5e9,
                freq_stop=5.3e9, points=2, repeat=1, point_timeout=8.)
            with patch.object(acquisition, 'parse_args', return_value=opts), \
                 patch.object(acquisition, 'make_config', return_value=({}, SDRConfig(), SweepConfig())), \
                 patch.object(acquisition, 'RealtimePublisher', return_value=publisher), \
                 patch.object(acquisition, 'IdleSleepGuard', return_value=guard), \
                 patch.object(acquisition.signal, 'signal'), \
                 patch('sspp_lab.hardware.USRPManager', return_value=manager), \
                 patch('sspp_experiments.temperature_sweep.metadata.validate_config', return_value=thermal), \
                 patch('sspp_experiments.temperature_sweep.acquisition.TemperatureSweepCapture', return_value=companion), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = acquisition.main()
            manifest = json.loads(next(Path(folder).glob('*/manifest.json')).read_text())
        self.assertEqual(code, 2)
        self.assertFalse(manifest['idle_sleep_guard']['active'])
        self.assertFalse(manifest['telemetry']['rf_start_attempted'])
        self.assertIsNone(manifest['time_policy']['total_time_limit_seconds'])
        manager.create_streamers.assert_not_called()
        manager.start_rx_stream.assert_not_called()
        if guard_fails:
            manager.configure.assert_not_called()
            companion.start.assert_not_called()
            self.assertEqual(calls, ['guard_start', 'guard_close'])
        else:
            self.assertEqual(calls, ['guard_start', 'temperature_start', 'configure', 'guard_close'])
            companion.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
