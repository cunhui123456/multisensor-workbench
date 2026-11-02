"""升温派生数据、真实终点标记和串口封存契约；无硬件。"""
import csv
import json
from pathlib import Path
import tempfile
import unittest
from test_temperature_capture import FakeReader, wait_until
from sspp_experiments.temperature_pressure import CaptureConfig, SerialCaptureSession
from sspp_experiments.temperature_pressure.ramp import VoltageTrend, validate_ramp

class TemperatureRampTests(unittest.TestCase):
    def test_validation_requires_real_start_and_temperature_protocol(self):
        for start in (None, float('nan'), True, 90):
            with self.assertRaises(ValueError):
                validate_ramp({'start_temperature_c': start, 'target_temperature_c': 90})
        with self.assertRaises(ValueError):
            CaptureConfig('/dev/cu.FAKE', 'pressure_v1', 'sample', temperature_ramp={'start_temperature_c':25,'target_temperature_c':90}).validate()

    def test_trend_channels_and_reset_do_not_mix_baselines(self):
        trend = VoltageTrend()
        def row(t,v,ch='S6',segment=0):
            return dict(host_elapsed_s=t,voltage_v=v,channel=ch,time_segment=segment)
        self.assertEqual(trend.add(row(0,2))['delta_voltage_v'],0)
        self.assertIsNone(trend.add(row(1,2.01))['voltage_rate_v_per_min'])
        result=trend.add(row(6,2.06))
        self.assertAlmostEqual(result['voltage_rate_v_per_min'],.6)
        self.assertAlmostEqual(result['delta_voltage_v'],.06)
        self.assertEqual(trend.add(row(6,3,'S7'))['delta_voltage_v'],0)
        self.assertEqual(trend.add(row(7,2.5,segment=1))['delta_voltage_v'],0)

    def test_capture_preserves_raw_and_distinguishes_confirmed_endpoint(self):
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed), tempfile.TemporaryDirectory() as root:
                raw=b'100,600,2.933,140,650,3.177\n'
                reader=FakeReader([raw])
                session=SerialCaptureSession(root, lambda *_:reader)
                config=CaptureConfig('/dev/cu.FAKE','temperature_v1','ramp',temperature_ramp={'start_temperature_c':24.5,'target_temperature_c':90})
                session.start(config)
                wait_until(lambda:session.snapshot()['rows']==2)
                if confirmed:
                    session.confirm_ramp_endpoint()
                self.assertTrue(session.stop())
                manifest=json.loads((session.run_path/'manifest.json').read_text())
                self.assertEqual(manifest['status'],'complete')
                self.assertEqual(manifest['temperature_ramp']['target_reached_confirmed'],confirmed)
                self.assertEqual((session.run_path/'raw_serial.bin').read_bytes(),raw)
                with (session.run_path/'samples.csv').open() as f:
                    original=list(csv.DictReader(f))
                with (session.run_path/'voltage_response.csv').open() as f:
                    derived=list(csv.DictReader(f))
                self.assertNotIn('delta_voltage_v',original[0])
                self.assertEqual([r['voltage_v'] for r in derived],[r['voltage_v'] for r in original])
                self.assertEqual(len(manifest['files']),6)
                self.assertTrue(all(float(r['delta_voltage_v'])==0 for r in derived))
