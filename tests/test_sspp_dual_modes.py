"""双模式、完整扫频区间统计和敏感频点筛选；仅fake设备。"""
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import numpy as np
from contextlib import redirect_stdout
from unittest.mock import patch
from sspp_experiments.temperature_sweep.metadata import validate_config
from sspp_experiments.temperature_sweep.intervals import summarize_round, frequency_ranking
from sspp_experiments.temperature_sweep.acquisition import TemperatureSweepCapture
from sspp_experiments.material_sweep.logic import SweepConfig
from sspp_desktop.services import DesktopService
from test_temperature_sweep_capture import FakeReader,config
from test_sweep_quality import ReplayRunner, tone

class DualModeTests(unittest.TestCase):
    def test_manual_requires_temperature_but_not_serial_or_calibration(self):
        cfg=validate_config({'mode':'manual','condition_label':'50C','reference_temperature_c':50})
        self.assertEqual(cfg['port'],'')
        self.assertIsNone(DesktopService._temperature_target({'temperature_sweep':cfg}))
        with self.assertRaises(ValueError):validate_config({'mode':'manual','condition_label':'missing'})
        cfg=validate_config(config(mode='continuous',reference_temperature_c=88))
        self.assertIsNone(cfg['reference_temperature_c'])
        self.assertEqual(DesktopService._temperature_target({'temperature_sweep':cfg})['kind'],'serial')

    def test_round_average_excludes_outside_samples_and_uses_mean_voltage_for_temperature(self):
        cal={'id':'test','slope_c_per_v':10,'intercept_c':5,'voltage_min':1,'voltage_max':3}
        summary=summarize_round(1,10,12,[(9,99,0),(10,1,0),(11,2,0),(12,3,0),(13,99,0)],True,cal)
        self.assertEqual(summary['sample_count'],3)
        self.assertEqual(summary['voltage_mean_v'],2)
        self.assertEqual(summary['voltage_sd_v'],1)
        self.assertEqual(summary['temperature_c'],25)
        self.assertEqual(summary['quality_flags'],[])
        out=summarize_round(1,10,12,[(10,.5,0),(12,3.5,0)],True,cal)
        self.assertIsNone(out['temperature_c'])
        self.assertEqual(out['calibration_status'],'outside_calibration_range')

    def test_ranking_selects_known_sensitive_frequency_and_excludes_partial_round(self):
        rounds=[];measurements=[]
        for i,v in enumerate((1.,2.,3.,4.),1):
            rounds.append(summarize_round(i,0,2,[(0,v,0),(2,v,0)],i!=4,None))
            for f,s in [(5e9,1),(5.1e9,-4),(5.2e9,.2)]:
                measurements.append({'repeat':i,'carrier_hz':f,'tone_hz':2000,'amplitude_dB':s*v})
        ranking=frequency_ranking(rounds,measurements,None)
        self.assertEqual(ranking[0]['carrier_hz'],5.1e9)
        self.assertEqual(ranking[0]['slope'],-4)
        self.assertEqual(ranking[0]['observations'],3)
        self.assertEqual(ranking[0]['slope_unit'],'dB/V')
        self.assertEqual(ranking[0]['r_squared'],1)
        self.assertEqual(frequency_ranking(rounds[:2],measurements,None),[])

    def test_continuous_engine_goes_beyond_repeat_count_and_emits_round_hooks(self):
        accepted=[];boundaries=[]
        def on_accept(runner):
            accepted.append(True)
            if len(accepted)==3:runner.stop_event.set()
        cfg=SweepConfig(num_points=1,sweep_repeat=1,continuous=True)
        runner=ReplayRunner([np.concatenate([tone(2000,5000),tone(2000,5000),tone(2000,10000)]) for _ in range(3)],cfg,on_accept=on_accept)
        runner.on_round_started=lambda i:boundaries.append(('start',i))
        runner.on_round_finished=lambda i,complete:boundaries.append(('end',i,complete))
        with redirect_stdout(io.StringIO()):result=runner.run(enable_plot=False)
        self.assertEqual(len(result['all_amp_records']),3)
        self.assertEqual(boundaries[-1],('end',3,True))

    def test_continuous_capture_writes_rounds_ranking_and_raw_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as root:
            events=[];reader=FakeReader();stop=threading.Event();failures=[]
            capture=TemperatureSweepCapture(root,config(mode='continuous'),stop,failures,
                       transport_factory=lambda *_:reader,emit=events.append,readiness_timeout_s=.5,startup_settle_s=.025)
            try:
                capture.start()
                for index in range(1,4):
                    capture.begin_round(index)
                    time.sleep(.05)
                    capture.record_point(repeat=index,point=1,carrier_hz=5e9,tone_hz=2000,amplitude_dB=-20+index)
                    capture.finish_round(index,True)
                capture.begin_round(4)
                time.sleep(.025)
            finally:result=capture.close()
            self.assertEqual(result['status'],'complete')
            self.assertTrue(reader.closed)
            self.assertEqual(len([e for e in events if e['event']=='TEMP_ROUND']),4)
            doc=json.loads((Path(root)/'temperature_intervals.json').read_text())
            self.assertEqual(len(doc['rounds']),4)
            self.assertFalse(doc['rounds'][-1]['complete'])
            self.assertTrue(all(r['temperature_c'] is None for r in doc['rounds']))
            self.assertTrue((Path(root)/'frequency_sensitivity.csv').is_file())
            self.assertTrue((Path(root)/'temperature'/'raw_serial.bin').stat().st_size>0)
            self.assertEqual(failures,[])
