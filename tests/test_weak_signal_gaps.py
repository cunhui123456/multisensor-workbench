from pathlib import Path
import io,json,time,threading
from contextlib import redirect_stdout
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
from sspp_experiments.material_sweep.logic import SweepConfig,SweepRunner,PointQualityTimeout

class Replay(SweepRunner):
 def __init__(self,sequences,cfg):
  super().__init__(SimpleNamespace(rate=250000.,rx_rate_actual=250000.),cfg,None,None,threading.Event())
  self.sequences=sequences;self.index=-1;self.clock=0.;self.records=[]
 def _set_freq_and_tone(self,f,tone):
  self.index+=1;self.data=self.sequences[self.index];self.offset=0;self.started=self.clock
 def _clear_queue(self):pass
 def _fetch_available_frames(self):
  n=min(8192,len(self.data)-self.offset)
  if n<=0 or (self.offset+n)/250000>8:
   self.clock=self.started+8;raise PointQualityTimeout('Replay bounded 8-second deadline')
  x=self.data[self.offset:self.offset+n];self.offset+=n;self.clock=self.started+self.offset/250000
  return [x],x
 def point_deadline_expired(self):return self.clock-self.started>8
 def on_quality_attempt(self,r):self.records.append(dict(r))
 def invalid_quality_details(self):return {'last_attempt':self.records[-1] if self.records else None}

def warmup(tone):
 return (.01*np.exp(2j*np.pi*tone*np.arange(20000)/250000)).astype(np.complex64)


import unittest,csv,hashlib
from sspp_experiments.material_sweep.acquisition import save_outputs,make_runner
from sspp_experiments.temperature_sweep.intervals import frequency_ranking,summarize_round
from sspp_lab.controller import LabController,DEFAULTS
from sspp_desktop.replay import ReplaySession
from unittest.mock import MagicMock
from queue import Queue
import tempfile

class WeakGapTests(unittest.TestCase):
 def run_replay(self,runner):
  with redirect_stdout(io.StringIO()),patch('sspp_experiments.material_sweep.logic.time.sleep'):
   return runner.run(enable_plot=False)

 def gapped(self):
  cfg=SweepConfig(num_points=3,sweep_repeat=1,skip_invalid_points=True)
  r=Replay([warmup(12000).repeat(101),warmup(3000),warmup(4000)],cfg)
  return r,self.run_replay(r)

 def test_deadline_point_stays_none_and_next_frequencies_keep_positions(self):
  r,result=self.gapped()
  self.assertIsNone(result['all_amp_records'][0][0])
  self.assertTrue(all(isinstance(v,float) for v in result['all_amp_records'][0][1:]))
  self.assertEqual(result['all_expected_tone_records'][0],[2000.,3000.,4000.])
  self.assertEqual(result['all_quality_records'][0][0]['measurement_status'],'invalid')
  self.assertEqual(r.index,2)
  json.dumps(result['all_amp_records'],allow_nan=False)

 def test_csv_history_and_replay_keep_missing_points_and_quality_counts(self):
  r,result=self.gapped()
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);out=root/'Data/runs/gaps';out.mkdir(parents=True)
   quality=save_outputs(result,out,r.cfg)
   self.assertFalse(quality['complete']);self.assertTrue(quality['scan_completed'])
   self.assertEqual(quality['valid_counts'],[2]);self.assertEqual(quality['invalid_count'],1)
   with (out/'measurements.csv').open() as f:rows=list(csv.DictReader(f))
   self.assertEqual(rows[0]['peak_amplitude_dBFS_est'],'')
   self.assertEqual(rows[0]['measurement_status'],'invalid')
   self.assertEqual(rows[1]['point'],'2')
   curve=LabController(root).curve('runs/gaps')
   self.assertIsNone(curve['points'][0]['amplitude_dB'])
   replay=ReplaySession(out,"sweep")
   self.assertIsNone(replay.points[0]['amplitude_dB'])
   self.assertEqual(len(replay.points),3)

 def test_post_processing_deadline_cannot_accept_an_out_of_time_window(self):
  cfg=SweepConfig(num_points=1,sweep_repeat=1,skip_invalid_points=True)
  r=Replay([warmup(2000)],cfg)
  r.point_deadline_expired=lambda:len(r.records)>=2
  result=self.run_replay(r)
  self.assertIsNone(result['all_amp_records'][0][0])
  self.assertFalse(any(q['accepted'] for q in r.records))

 def test_hardware_fault_is_not_a_skippable_quality_timeout(self):
  cfg=SweepConfig(num_points=1,sweep_repeat=1,skip_invalid_points=True)
  r=Replay([warmup(2000)],cfg)
  def broken():raise RuntimeError('RX hardware failure')
  r._fetch_available_frames=broken
  with self.assertRaisesRegex(RuntimeError,'hardware failure'):self.run_replay(r)
  self.assertEqual(r.partial_result['all_amp_records'],[[]])

 def test_pure_noise_is_not_accepted_as_a_measurement_across_fixed_seeds(self):
  for seed in range(16):
   with self.subTest(seed=seed):
    rng=np.random.default_rng(seed)
    samples=(.001*(rng.normal(size=2000000)+1j*rng.normal(size=2000000))).astype(np.complex64)
    r=Replay([samples],SweepConfig(num_points=1,sweep_repeat=1,skip_invalid_points=True))
    result=self.run_replay(r)
    self.assertIsNone(result['all_amp_records'][0][0])
    self.assertFalse(any(q['accepted'] for q in r.records))

 def test_real_saved_failure_replay_preserves_source_and_eight_second_budget(self):
  base=Path('/Users/cunhui/Git-Projects/Academic_Research/multisensor-workbench/Data/runs')
  for run,filename,target,valid in [('20261102T023947_939461Z','iq_point_037.npz',38000,False),('20261102T023813_655557Z','iq_point_038.npz',19000,True)]:
   source=base/run/filename
   if not source.is_file():self.skipTest('saved diagnostic IQ not installed')
   before=hashlib.sha256(source.read_bytes()).hexdigest()
   iq=np.load(source)['iq']
   cfg=SweepConfig(num_points=2,sweep_repeat=1,tone_start=target-1000,tone_step=1000,skip_invalid_points=True)
   runner=Replay([warmup(target-1000),iq],cfg);result=self.run_replay(runner)
   self.assertLessEqual(runner.clock-runner.started,8)
   self.assertEqual(result['all_amp_records'][0][1] is not None,valid)
   self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)

 def test_invalid_frequency_is_excluded_without_discarding_other_points(self):
  rounds=[];points=[]
  for i,x in enumerate((1.,2.,3.),1):
   r=summarize_round(i,0,2,[(0,x,0),(2,x,0)],True,None)
   r['quality_flags'].append('invalid_frequency_points');rounds.append(r)
   points.extend([{'repeat':i,'carrier_hz':5e9,'tone_hz':2000,'amplitude_dB':'','measurement_status':'invalid'},
                  {'repeat':i,'carrier_hz':5.1e9,'tone_hz':3000,'amplitude_dB':str(2*x),'measurement_status':'valid'}])
  rank=frequency_ranking(rounds,points,None)
  self.assertEqual(len(rank),1);self.assertEqual(rank[0]['carrier_hz'],5.1e9)

 def test_controller_recognizes_finished_with_gaps_only_with_rf_stop_evidence(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);path=root/'Data/runs/gaps';path.mkdir(parents=True)
   manifest={'status':'complete_with_gaps','quality':{'complete':False,'scan_completed':True,'invalid_count':1},'errors':[],
             'telemetry':{'tx_end_of_burst_sent':True,'rx_stop_command_sent':True,'workers_stopped':True}}
   (path/'manifest.json').write_text(json.dumps(manifest))
   lab=LabController(root)
   proc=MagicMock();proc.stdout=io.StringIO('');proc.wait.return_value=0;proc.poll.return_value=0
   job={'id':'test','config':DEFAULTS,'output':'Data/runs/gaps','errors':[],'rf_stopped':False}
   lab._collect(proc,job)
   self.assertEqual(job['status'],'complete_with_gaps')
   self.assertFalse(job['quality']['complete'])

 def test_all_invalid_export_has_no_numeric_amplitude_or_fabricated_range(self):
  r,result=self.gapped()
  for key in ('all_freq_records','all_amp_records','all_expected_tone_records','all_measured_tone_records','all_quality_records'):
   result[key][0]=result[key][0][:1]
  r.cfg.num_points=1
  with tempfile.TemporaryDirectory() as folder:
   quality=save_outputs(result,Path(folder),r.cfg)
   self.assertEqual(quality['valid_counts'],[0])
   self.assertIsNone(quality['amplitude_range_dBFS_est'])
   self.assertFalse(quality['complete'])
   self.assertTrue(quality['scan_completed'])
   json.dumps(quality,allow_nan=False)
