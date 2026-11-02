"""Bounded weak-point retry budget: simulated time and queues, never RF hardware."""
import contextlib
import io
import hashlib
import json
from pathlib import Path
from queue import Queue
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from sspp_experiments.material_sweep import acquisition, logic
from sspp_experiments.material_sweep.budget import budget_info
from sspp_lab.controller import DEFAULTS, LabController, LabError, validate_request


class WeakPointBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.output=Path(self.tmp.name);self.clock=100.
        self.cfg=logic.SweepConfig(num_points=1,sweep_repeat=1,skip_invalid_points=True)
        self.queue=Queue();self.stop=threading.Event();self.failures=[]
        manager=MagicMock(); manager.usrp.get_tx_freq.return_value=5e9;manager.usrp.get_rx_freq.return_value=5e9
        self.runner=acquisition.make_runner(SimpleNamespace(rate=250000.,rx_rate_actual=250000.,channel=0),
            self.cfg,manager,self.queue,self.stop,None,self.output,SimpleNamespace(point_timeout=8.),self.failures)
        self.patch=patch.object(acquisition.time,'monotonic',side_effect=lambda:self.clock);self.patch.start();self.addCleanup(self.patch.stop)
        with patch.object(logic.time,'sleep'):self.runner._set_freq_and_tone(5e9,11000.)

    def evidence(self,**changes):
        return dict(state='VERIFY',passed=True,window_duration_s=1.,window_start=0,window_count=250000,
                    quality_attempt_index=1,point_deadline_expired=False,stop_requested=False,
                    rejection_reasons=[],**changes)

    def record(self,record):
        with contextlib.redirect_stdout(io.StringIO()):self.runner.on_quality_attempt(record)

    def test_one_qualified_long_verification_enables_fixed_hard_bound_once(self):
        self.assertEqual((self.runner.base_deadline,self.runner.hard_deadline,self.runner.deadline),(108.,116.,108.))
        self.clock=101.2;row=self.evidence();self.record(row)
        self.assertEqual(self.runner.deadline,116.)
        first=dict(self.runner.point_budget_extension)
        self.clock=115.;self.record(self.evidence())
        self.assertEqual(self.runner.deadline,116.)
        self.assertEqual(self.runner.point_budget_extension,first)
        self.assertEqual(len(self.runner.point_budget_extensions),1)
        self.assertEqual(row['point_budget']['base_seconds'],8.)
        self.assertEqual(row['point_budget']['maximum_seconds'],16.)
        persisted=[json.loads(line) for line in (self.output/'sweep_quality.jsonl').read_text().splitlines()]
        self.assertEqual(persisted[0]['point_budget']['extension'],first)

    def test_short_failed_expired_cancelled_and_measurement_are_not_extension_evidence(self):
        changes=[{'window_duration_s':.1},{'passed':False},{'point_deadline_expired':True},
                 {'stop_requested':True},{'state':'MEASURE'}]
        for change in changes:
            row=self.evidence();row.update(change)
            with self.subTest(change=change):
                self.record(row)
                self.assertEqual(self.runner.deadline,108.)
                self.assertIsNone(self.runner.point_budget_extension)

    def test_late_pass_cannot_resurrect_expired_base_budget(self):
        self.clock=108.001;self.record(self.evidence())
        self.assertEqual(self.runner.deadline,108.)
        self.assertEqual(self.runner.point_budget_extensions,[])

    def test_disabled_extra_and_user_stop_do_not_extend(self):
        self.cfg.weak_point_extra_seconds=0.;self.record(self.evidence())
        self.assertEqual(self.runner.deadline,108.)
        self.cfg.weak_point_extra_seconds=8.;self.stop.set();self.record(self.evidence())
        self.assertEqual(self.runner.deadline,108.)

    def test_no_qualified_evidence_still_times_out_at_base(self):
        row=self.evidence();row.update(passed=False);self.record(row)
        self.clock=108.01;self.runner.last_frame_at=self.clock
        with self.assertRaises(logic.PointQualityTimeout):self.runner._fetch_available_frames()
        self.assertEqual(self.runner.invalid_quality_details()['point_timeout']['budget_seconds'],8.)

    def test_qualified_point_still_times_out_at_fixed_hard_limit(self):
        self.clock=101.;self.record(self.evidence())
        self.clock=116.01;self.runner.last_frame_at=self.clock
        with self.assertRaises(logic.PointQualityTimeout):self.runner._fetch_available_frames()
        d=self.runner.invalid_quality_details()['point_timeout']
        self.assertEqual(d['budget_seconds'],16.)
        self.assertEqual(d['base_budget_seconds'],8.)
        self.assertEqual(d['point_budget_extension']['hard_deadline_monotonic_s'],116.)
        self.assertEqual(self.runner.deadline,116.)

    def test_extension_never_masks_stall_or_transport_error(self):
        self.clock=101.;self.record(self.evidence())
        self.clock=105.;self.runner.last_frame_at=101.
        with self.assertRaisesRegex(RuntimeError,'stalled'):self.runner._fetch_available_frames()
        self.failures[:]=['RX: ERROR_CODE_OVERFLOW']
        with self.assertRaisesRegex(RuntimeError,'OVERFLOW'):self.runner._fetch_available_frames()

    def test_next_point_resets_activation_but_preserves_session_audit(self):
        self.clock=101.;self.record(self.evidence())
        self.clock=110.
        with patch.object(logic.time,'sleep'):self.runner._set_freq_and_tone(5.1e9,12000.)
        self.assertEqual((self.runner.base_deadline,self.runner.hard_deadline,self.runner.deadline),(118.,126.,118.))
        self.assertIsNone(self.runner.point_budget_extension)
        self.assertEqual(len(self.runner.point_budget_extensions),1)


class WeakPointConfigTests(unittest.TestCase):
    def test_defaults_cli_controller_and_summary_are_consistent(self):
        cfg=validate_request({'max_seconds':120},require_setup=False)
        self.assertEqual(cfg['weak_point_extra_seconds'],8)
        self.assertIsNone(cfg['max_seconds'])
        command=LabController('/tmp/offline').command(cfg,Path('/tmp/profile.json'))
        self.assertEqual(command[command.index('--weak-point-extra-seconds')+1],'8')
        with patch.object(sys,'argv',['run_local.py','plan']):opts=acquisition.parse_args()
        self.assertEqual(opts.weak_point_extra_seconds,8)
        info=budget_info(cfg)
        self.assertEqual(info['point_timeout_seconds'],8.)
        self.assertEqual(info['point_max_timeout_seconds'],16.)
        self.assertIn('最多启用一次',info['summary'])
        self.assertIn('无总时限',info['summary'])

    def test_extra_validates_independently_and_zero_is_respected(self):
        for value in [True,'8',None,float('nan'),float('inf'),-.1,30.1]:
            with self.subTest(value=value),self.assertRaises(LabError):
                validate_request({'weak_point_extra_seconds':value},require_setup=False)
        for value in [0,8,30]:
            cfg=validate_request({'weak_point_extra_seconds':value},require_setup=False)
            self.assertEqual(budget_info(cfg)['point_max_timeout_seconds'],8+value)
        self.assertIn('已关闭',budget_info({**DEFAULTS,'weak_point_extra_seconds':0})['summary'])

    def test_phase_command_does_not_gain_rf_sweep_wait_arguments(self):
        cfg=validate_request({'mode':'phase'},require_setup=False)
        command=LabController('/tmp/offline').command(cfg,Path('/tmp/profile.json'))
        self.assertNotIn('--weak-point-extra-seconds',command)
        self.assertIsNone(budget_info(cfg))

    def test_cli_config_persists_extra_and_rejects_invalid_values_without_hardware(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);image=root/'fake.bin';image.write_bytes(b'offline-only')
            profile=root/'profile.json'
            profile.write_text(json.dumps({'serial':'offline','channel':0,'fpga':str(image),
                'fpga_sha256':hashlib.sha256(image.read_bytes()).hexdigest()}))
            with patch.object(sys,'argv',['run_local.py','plan','--device-config',str(profile),
                                         '--weak-point-extra-seconds','12']):
                opts=acquisition.parse_args()
            _,_,cfg=acquisition.make_config(opts)
            self.assertEqual(cfg.weak_point_extra_seconds,12.)
            self.assertEqual(logic.sweep_algorithm_policy(cfg)['weak_point_extra_seconds'],12.)
            for bad in [-1,31,True,float('nan'),float('inf'),'8']:
                opts.weak_point_extra_seconds=bad
                with self.subTest(bad=bad),self.assertRaises(ValueError):acquisition.make_config(opts)


if __name__=='__main__':unittest.main()
