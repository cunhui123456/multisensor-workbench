"""标定区间、参考读数覆盖、异步保存与原始采集隔离；仅fake串口。"""
import csv
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest
from sspp_experiments.temperature_pressure.plateau import PlateauRecorder, reference_stability, validate_settings
from sspp_experiments.temperature_pressure import CaptureConfig, SerialCaptureSession
from test_temperature_capture import wait_until


def command(op,t,**values):
    return {'operation':op,'host_elapsed_s':t,'host_time_utc':f'2026-11-02T00:00:{t}Z',**values}


def row(t,v,ch='S6',segment=0):
    return {'host_elapsed_s':t,'voltage_v':v,'channel':ch,'time_segment':segment}


class PlateauDomainTests(unittest.TestCase):
    def begin(self, recorder, t=0, settings=None):
        recorder.command(command('begin',t,target_temperature_c=50,reference_source='reference-1',settings=settings or {'window_s':60}))

    def test_single_reading_cannot_claim_stability_or_zero_range(self):
        state=reference_stability([{'host_elapsed_s':60,'temperature_c':50}],60,validate_settings({}))
        self.assertEqual(state['status'],'insufficient_reference')
        self.assertIsNone(state['range_c']);self.assertIsNone(state['rate_c_per_min'])

    def test_coverage_age_range_and_trend_are_independent(self):
        settings=validate_settings({'window_s':60})
        def assess(points,end=60.1):
            return reference_stability([{'host_elapsed_s':t,'temperature_c':v} for t,v in points],end,settings)
        self.assertEqual(assess([(0,50),(30,50.02),(60,50.03)])['status'],'within_limits')
        self.assertEqual(assess([(55,50),(57,50),(60,50)])['status'],'insufficient_reference')
        self.assertEqual(assess([(0,50),(30,50),(50,50)])['status'],'insufficient_reference')
        self.assertEqual(assess([(0,50),(30,50.2),(60,50)])['status'],'outside_limits')
        self.assertEqual(assess([(0,50),(30,50.08),(60,50.16)])['status'],'outside_limits')

    def test_point_statistics_are_per_channel_and_interval_only(self):
        recorder=PlateauRecorder();recorder.add_rows([row(-1,99)])
        self.begin(recorder)
        for t,v in [(0,1),(30,2),(60,3)]:
            recorder.add_rows([row(t,v),row(t,4,'S7')])
            recorder.command(command('reference',t,temperature_c=50))
        recorder.command(command('end',60.1))
        point=recorder.document()['points'][0]
        self.assertEqual(point['channels']['S6']['mean_v'],2)
        self.assertEqual(point['channels']['S6']['sample_sd_v'],1)
        self.assertAlmostEqual(point['channels']['S6']['rate_v_per_min'],2)
        self.assertEqual(point['channels']['S7']['mean_v'],4)
        self.assertEqual(point['reference_stability']['status'],'within_limits')
        self.assertFalse(point['automatic_calibration_activated'])
        recorder.add_rows([row(61,99)])
        self.assertEqual(point['channels']['S6']['count'],3)

    def test_resets_short_intervals_gaps_and_interruption_remain_reviewable(self):
        recorder=PlateauRecorder();self.begin(recorder)
        recorder.add_rows([row(1,2),row(5,2,segment=1)])
        recorder.command(command('interrupt',10))
        point=recorder.points[0]
        self.assertEqual(point['status'],'interrupted')
        self.assertTrue({'mcu_reset_in_interval','short_interval','no_reference','voltage_data_gap'} <= set(point['quality_flags']))
        self.assertTrue(point['requires_manual_review'])

    def test_bad_reference_never_replaced_with_target_temperature(self):
        recorder=PlateauRecorder();self.begin(recorder)
        recorder.add_rows([row(0,2)])
        recorder.command(command('end',60))
        self.assertIsNone(recorder.points[0]['reference_mean_c'])
        self.assertEqual(recorder.points[0]['reference_stability']['status'],'insufficient_reference')
        with self.assertRaises(ValueError):recorder.command(command('reference',61,temperature_c=50))
        self.begin(recorder,62)
        with self.assertRaises(ValueError):recorder.command(command('reference',63,temperature_c=float('nan')))


class FeedReader:
    def __init__(self):
        from queue import Queue
        self.queue=Queue();self.closed=False
    def read(self,timeout=.1):
        from queue import Empty
        try:return self.queue.get(timeout=timeout)
        except Empty:return None
    def close(self):self.closed=True


class PlateauCaptureTests(unittest.TestCase):
    def test_command_file_statistics_and_continuous_raw_capture(self):
        with tempfile.TemporaryDirectory() as root:
            reader=FeedReader();session=SerialCaptureSession(root,lambda *_:reader)
            session.start(CaptureConfig('/dev/cu.FAKE','temperature_v1','sample',temperature_calibration=True))
            reader.queue.put(b'100,600,2.900,140,650,3.100\n')
            wait_until(lambda:session.snapshot()['rows']==2)
            session.plateau_command('begin',target_temperature_c=50,reference_source='ref-1')
            wait_until(lambda:session.snapshot()['plateau'].get('active') is not None)
            session.plateau_command('reference',temperature_c=49.7)
            reader.queue.put(b'200,600,2.920,240,650,3.120\n')
            wait_until(lambda:session.snapshot()['rows']==4)
            session.plateau_command('end')
            wait_until(lambda:len(session.snapshot()['plateau'].get('points',[]))==1)
            self.assertTrue(session.is_running)
            doc=json.loads((session.run_path/'calibration_points.json').read_text())
            point=doc['points'][0]
            self.assertEqual(point['channels']['S6']['count'],1)
            self.assertAlmostEqual(point['channels']['S6']['mean_v'],2.92)
            self.assertIsNone(point['channels']['S6']['sample_sd_v'])
            self.assertAlmostEqual(point['reference_mean_c'],49.7)
            self.assertEqual(point['reference_stability']['status'],'insufficient_reference')
            reader.queue.put(b'300,600,2.940,340,650,3.140\n')
            wait_until(lambda:session.snapshot()['rows']==6)
            session.stop()
            self.assertTrue(reader.closed)
            self.assertEqual(len((session.run_path/'samples.csv').read_text().splitlines()),7)
            manifest=json.loads((session.run_path/'manifest.json').read_text())
            self.assertEqual(manifest['status'],'complete')
            self.assertTrue({'calibration_points.json','calibration_events.jsonl'} <= {f['path'] for f in manifest['files']})
            self.assertEqual(len((session.run_path/'raw_serial.bin').read_bytes().splitlines()),3)

    def test_stop_interrupts_active_interval_and_rejected_command_does_not_stop_capture(self):
        with tempfile.TemporaryDirectory() as root:
            reader=FeedReader();session=SerialCaptureSession(root,lambda *_:reader)
            session.start(CaptureConfig('/dev/cu.FAKE','temperature_v1','sample',temperature_calibration=True))
            reader.queue.put(b'100,600,2.900,140,650,3.100\n')
            wait_until(lambda:session.snapshot()['rows']==2)
            session.plateau_command('end')
            wait_until(lambda:session.snapshot()['plateau'].get('last_error'))
            self.assertTrue(session.is_running)
            session.plateau_command('begin',target_temperature_c=50,reference_source='ref-1')
            wait_until(lambda:session.snapshot()['plateau'].get('active') is not None)
            session.stop()
            doc=json.loads((session.run_path/'calibration_points.json').read_text())
            self.assertEqual(doc['points'][0]['status'],'interrupted')
            self.assertIn('no_voltage_samples',doc['points'][0]['quality_flags'])
