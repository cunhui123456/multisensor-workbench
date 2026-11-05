"""Unified sweep data combinations and initialization gate; no hardware."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_experiments.comparison import build_comparison, history_category
from sspp_lab.controller import DEFAULTS, LabController, LabError, validate_request
import test_comparison_domain as comparison_fixtures
from test_liquid_temperature_service import metadata


class UnifiedSweepBackendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = DesktopService(Settings(root=self.root))
        self.fixture = comparison_fixtures.ComparisonTests()
        self.fixture.root = self.root

    def config(self, mode='manual', liquid=True):
        thermal = {'mode': mode, 'condition_label':'同一样品升温',
                   'reference_temperature_c':35., 'port':'/dev/cu.usbserial-FAKE'}
        return {**DEFAULTS, 'profile':'own', 'setup_confirmed':True,
                'temperature_sweep': thermal,
                'liquid_temperature': metadata(34.) if liquid else None}

    def test_optional_liquid_and_each_temperature_source_are_independent(self):
        self.assertIsNone(validate_request({}, False)['temperature_sweep'])
        for mode in ('manual', 'continuous'):
            for liquid in (False, True):
                with self.subTest(mode=mode, liquid=liquid):
                    cfg = validate_request(self.config(mode, liquid))
                    self.assertEqual(cfg['temperature_sweep']['mode'], mode)
                    self.assertEqual(bool(cfg['liquid_temperature']), liquid)
                    cmd = LabController(self.root).command(cfg,self.root/'fake.json','plan')
                    payload = json.loads(cmd[cmd.index('--temperature-config')+1])
                    self.assertEqual(payload['mode'],mode)
                    if liquid:
                        self.assertEqual(cfg['liquid_temperature']['temperature_c'],34.)
        with self.assertRaises(LabError):
            validate_request({**self.config(), 'mode':'phase'})

    def test_global_initialization_replaces_duplicate_confirmation_only_after_ready(self):
        original = {**DEFAULTS, 'profile':'own', 'setup_confirmed':False}
        saved = deepcopy(original)
        with patch.object(self.service,'environment_matches',return_value=False), \
             patch.object(LabController,'start') as start:
            with self.assertRaisesRegex(LabError,'初始化'):
                self.service.start(original)
            start.assert_not_called()
        with patch.object(self.service,'environment_matches',return_value=True), \
             patch.object(LabController,'start',return_value={'accepted':True}) as start:
            self.assertEqual(self.service.start(original),{'accepted':True})
            request = start.call_args.args[0]
            self.assertTrue(request['setup_confirmed'])
            self.assertEqual(request['setup_confirmation_source'],'global_initialization')
        self.assertEqual(original,saved)
        with self.assertRaisesRegex(LabError,'接线'):
            validate_request(original)

    def test_context_binds_both_conditions_without_claiming_manual_checkbox(self):
        key = 'runs/combined'
        self.fixture.sweep(key)
        cfg = validate_request({**self.config(), 'setup_confirmation_source':'global_initialization'})
        self.service._save_context({'id':'fake','output':'Data/'+key,'config':cfg,
                                    'status':'complete','rf_stopped':True})
        context = json.loads((self.root/'Data'/key/'desktop_context.json').read_text())
        self.assertEqual(context['experiment'],'temperature_sweep')
        self.assertFalse(context['initial_setup_user_confirmed'])
        self.assertEqual(context['setup_confirmation_source'],'global_initialization')
        self.assertEqual(context['parameters']['temperature_sweep']['reference_temperature_c'],35.)
        self.assertEqual(context['parameters']['liquid_temperature']['temperature_c'],34.)
        self.assertEqual(self.service.liquid_load(key)['metadata']['liquid'],'water')
        self.assertEqual(self.service.liquid_history()[0]['key'],key)
        self.assertTrue(self.service.history()[0]['has_liquid_conditions'])
        report = build_comparison(self.root,[key])
        self.assertEqual(report['tables']['temperature_conditions'][0]['reference_temperature_c'],35.)
        self.assertEqual(report['tables']['liquid_temperature_conditions'][0]['temperature_c'],34.)

    def test_continuous_liquid_still_requires_serial_before_rf_launch(self):
        cfg = self.config('continuous')
        for ready in (set(), {'usrp'}, {'serial'}):
            with self.subTest(ready=ready), \
                 patch.object(self.service,'environment_matches',side_effect=lambda target: target['kind'] in ready), \
                 patch.object(LabController,'start') as launch:
                with self.assertRaises(LabError):
                    self.service.start(cfg)
                launch.assert_not_called()

    def test_historical_categories_are_not_rewritten_by_merged_navigation(self):
        for category in ('material_sweep','liquid_temperature_sweep','temperature_sweep'):
            with self.subTest(category=category):
                self.assertEqual(history_category({'key':'runs/old'}, {'experiment':category}),category)
        self.assertEqual(history_category({'key':'phase_runs/old'},{'experiment':'liquid_temperature_phase'}),
                         'liquid_temperature_phase')

    def test_missing_liquid_metadata_is_not_invented_from_temperature_label(self):
        key = 'runs/temperature_only'
        self.fixture.sweep(key)
        self.fixture.save(key,'desktop_context.json',{'experiment':'temperature_sweep','parameters':{
            'temperature_sweep':{'mode':'manual','condition_label':'water','reference_temperature_c':35.}}})
        self.assertEqual(self.service.liquid_history(),[])
        self.assertFalse(self.service.history()[0]['has_liquid_conditions'])
        with self.assertRaises((LabError,ValueError)):
            self.service.liquid_load(key)

    def test_legacy_liquid_category_is_indexed_without_inventing_metadata(self):
        key = 'runs/legacy_liquid'
        self.fixture.sweep(key)
        self.fixture.save(key,'desktop_context.json',{'experiment':'liquid_temperature_sweep'})
        row = self.service.history()[0]
        self.assertEqual(row['category'],'liquid_temperature_sweep')
        self.assertTrue(row['has_liquid_conditions'])
        with self.assertRaises((LabError,ValueError)):
            self.service.liquid_load(key)

    def test_combined_liquid_analysis_keeps_distinct_temperature_condition_tables(self):
        keys = ['runs/liquid_a','runs/liquid_b']
        before = {}
        for index,key in enumerate(keys):
            self.fixture.sweep(key)
            cfg = validate_request(self.config())
            cfg['temperature_sweep']['reference_temperature_c'] = 35.+index
            cfg['liquid_temperature']['temperature_c'] = 34.+index
            self.service._save_context({'id':key,'output':'Data/'+key,'config':cfg,
                                        'status':'complete','rf_stopped':True})
            for f in (self.root/'Data'/key).iterdir():
                if f.is_file():
                    before[f] = f.read_bytes()
        with patch.object(self.service,'_liquid_save_report',side_effect=lambda report: report):
            report = self.service.liquid_compare_sweeps(keys)
        self.assertEqual(report['tables']['temperature_conditions'][0]['reference_temperature_c'],35.)
        self.assertEqual(report['tables']['liquid_temperature_conditions'][0]['temperature_c'],34.)
        self.assertFalse(report['temperature_quantitative_allowed'])
        self.assertEqual(before,{f:f.read_bytes() for f in before})

    def test_combined_continuous_run_keeps_serial_shutdown_evidence_required(self):
        key='runs/continuous_liquid'
        manifest=self.fixture.sweep(key)
        manifest['temperature_sweep']={'status':'complete','serial_closed':False,
                                        'serial_workers_stopped':True,'data_files_closed':True}
        self.fixture.save(key,'manifest.json',manifest)
        cfg=validate_request(self.config('continuous'))
        self.service._save_context({'id':key,'output':'Data/'+key,'config':cfg,
                                    'status':'complete','rf_stopped':True})
        with self.assertRaisesRegex(ValueError,'温度采集完整保存'):
            build_comparison(self.root,[key])


if __name__ == '__main__':
    unittest.main()
