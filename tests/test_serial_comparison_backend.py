"""Sealed serial combination, distinct manual references, integrity, and export."""
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from sspp_experiments.comparison import build_comparison, history_category
from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_lab.controller import LabError


def make_serial(root, name, references=None):
    path=Path(root)/'Data/temperature_pressure_runs'/name
    path.mkdir(parents=True)
    (path/'samples.csv').write_text('host_elapsed_s,voltage_v,mcu_time_ms,time_segment,channel,adc\n0.2,3.0,100,0,S6,600\n0.2,2.7,140,0,S7,540\n1.2,3.1,200,0,S6,620\n1.2,2.8,240,0,S7,560\n')
    (path/'raw_serial.bin').write_bytes(b'100,600,3.0,140,540,2.7\n200,620,3.1,240,560,2.8\n')
    manifest={'status':'complete','errors':[], 'started_utc':'2026-11-02T00:00:00Z','sample_name':name,
              'protocol':'temperature_v1','serial':{'baudrate':9600,'port':'PRIVATE_PORT'},
              'channels':['S6','S7'],'rows':4,'serial_closed':True,'workers_stopped':True,'data_files_closed':True,'files':[]}
    if references is not None:
        manifest['temperature_calibration']={'mode':'platform_intervals'}
        points=[]
        for ident,temperature in enumerate(references,1):
            points.append({'id':ident,'status':'saved','start_s':ident*30,'end_s':(ident+1)*30,'duration_s':30,
                           'target_temperature_c':temperature+1,'reference_mean_c':temperature,
                           'reference_source':'reference thermometer','reference_stability':{'status':'insufficient_reference'},
                           'requires_manual_review':True,'quality_flags':[],
                           'channels':{channel:{'count':2,'mean_v':(4.-temperature*.01 if channel=='S6' else 2.7),
                                                'sample_sd_v':.01 if channel=='S6' else .02} for channel in ('S6','S7')}})
        (path/'calibration_points.json').write_text(json.dumps({'points':points}))
    seal(path,manifest)
    return path


def seal(path,manifest=None):
    manifest=manifest or json.loads((path/'manifest.json').read_text())
    manifest['files']=[]
    for name in ('samples.csv','raw_serial.bin','calibration_points.json'):
        source=path/name
        if source.is_file():
            data=source.read_bytes();manifest['files'].append({'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    (path/'manifest.json').write_text(json.dumps(manifest))


class SerialComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.keys=['temperature_pressure_runs/low','temperature_pressure_runs/high']
        self.low=make_serial(self.root,'low',[40,35,30]);self.high=make_serial(self.root,'high',list(range(40,91,5)))

    def tearDown(self):self.temp.cleanup()

    def write_correction(self, deleted=None, **changes):
        path=self.root/'Data/corrections/temperature_pressure_runs/low.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        ledger={'version':1,'run_key':self.keys[0],
                'source_calibration_sha256':hashlib.sha256((self.low/'calibration_points.json').read_bytes()).hexdigest(),
                'deleted_points':{'1':{'reason':'wrong temperature label','deleted_utc':'2026-11-02T08:00:00Z'}} if deleted is None else deleted,
                'history':[{'action':'delete','point_ids':[1],'reason':'wrong temperature label','utc':'2026-11-02T08:00:00Z'}]}
        ledger.update(changes);path.write_text(json.dumps(ledger));return path

    def test_correction_excludes_whole_point_preserves_raw_and_can_restore(self):
        before={p:p.read_bytes() for p in self.low.iterdir() if p.is_file()}
        ledger=self.write_correction()
        report=build_comparison(self.root,self.keys)
        for chart in report['charts'][:2]:
            self.assertEqual(len(chart['series']),1)
            series=chart['series'][0]
            self.assertEqual(series['x'],[30,35,*range(40,91,5)])
            self.assertEqual(len(series['data']),13)
            self.assertEqual(series['publication_label'],'Combined measurements')
            self.assertNotIn({'key':self.keys[0],'point_id':1},series['point_sources'])
            self.assertIn({'key':self.keys[1],'point_id':1},series['point_sources'])
        self.assertEqual(report['sources'][0]['parameters']['reference_temperature_sequence_c'],[35,30])
        self.assertEqual(report['sources'][0]['parameters']['reference_direction'],'descending')
        self.assertEqual(len(report['tables']['calibration_points']['rows']),26)
        self.assertEqual(len(report['tables']['serial_samples']['rows']),8)
        self.assertEqual(len(report['excluded_calibration_points']),1)
        self.assertEqual(report['excluded_calibration_points'][0]['channels'],['S6','S7'])
        self.assertEqual(report['sources'][0]['correction']['sha256'],hashlib.sha256(ledger.read_bytes()).hexdigest())
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        self.write_correction(deleted={})
        restored=build_comparison(self.root,self.keys)
        self.assertEqual(restored['charts'][0]['series'][0]['x'],[30,35,40,*range(40,91,5)])
        self.assertEqual(len(restored['charts'][0]['series'][0]['data']),14)
        self.assertEqual(restored['excluded_calibration_points'],[])

    def test_correction_source_and_contents_must_be_valid(self):
        cases=[{'version':2},{'version':True},{'run_key':self.keys[1]},
               {'source_calibration_sha256':'0'*64},{'deleted_points':{'99':{'reason':'wrong','deleted_utc':'2026-11-02T00:00:00Z'}}},
               {'deleted_points':{'1':{'reason':'','deleted_utc':'2026-11-02T00:00:00Z'}}},
               {'deleted_points':{'1':{'reason':'wrong','deleted_utc':'yesterday'}}}]
        for changes in cases:
            self.write_correction(**changes)
            with self.subTest(changes=changes),self.assertRaisesRegex(ValueError,'更正账本'):
                build_comparison(self.root,self.keys)

    def test_correction_new_changed_removed_or_symlinked_invalidates_export(self):
        service=DesktopService(Settings(root=self.root))
        initial=service.compare_runs(self.keys)
        ledger=self.write_correction()
        with self.assertRaisesRegex(LabError,'比较后已改变'):service._comparison_source_files(initial)
        report=service.compare_runs(self.keys)
        files=service._comparison_source_files(report)
        self.assertIn('corrections/'+self.keys[0]+'.json',[name for path,name,entry in files])
        self.write_correction(deleted={})
        with self.assertRaisesRegex(LabError,'比较后已改变'):service._comparison_source_files(report)
        ledger.unlink()
        with self.assertRaisesRegex(LabError,'比较后已改变'):service._comparison_source_files(report)
        ledger.symlink_to(self.low/'calibration_points.json')
        with self.assertRaisesRegex(ValueError,'符号链接'):build_comparison(self.root,self.keys)
        with self.assertRaisesRegex(LabError,'路径已变化'):service._comparison_source_files(report)

    def test_real_reference_points_overlap_and_review_are_preserved(self):
        before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}
        report=build_comparison(self.root,self.keys)
        self.assertEqual(report['kind'],'temperature_calibration')
        self.assertEqual(report['presentation_mode'],'combined_temperature_response')
        self.assertFalse(report['automatic_calibration_activated'])
        self.assertFalse(report['quantitative_difference_allowed'])
        self.assertEqual([c['publication']['channel'] for c in report['charts'][:2]],['S6','S7'])
        self.assertEqual([c['id'] for c in report['charts'][2:]],['serial_voltage_S6','serial_voltage_S7'])
        self.assertTrue(all('publication' not in c for c in report['charts'][2:]))
        self.assertEqual(len(report['tables']['serial_samples']['rows']),8)
        for c in report['charts'][:2]:
            self.assertEqual(c['publication']['xlabel'],'Reference temperature (°C)')
            self.assertEqual(c['publication']['ylabel'],'Voltage (V)')
            self.assertEqual([len(s['data']) for s in c['series']],[14])
            self.assertEqual(sum(x==40 for s in c['series'] for x,y in s['data']),2)
            self.assertEqual(c['series'][0]['x'],[30,35,40,*range(40,91,5)])
            self.assertEqual(c['series'][0]['run_index'],1)
            self.assertEqual(c['series'][0]['color'],'#24445C')
            self.assertIsNone(c['series'][0]['key'])
            self.assertEqual(c['series'][0]['publication_label'],'Combined measurements')
            self.assertEqual(c['publication']['source_keys'],self.keys)
            self.assertTrue(c['publication']['combined_sources'])
            self.assertEqual(c['series'][0]['errors'],c['series'][0]['y_error'])
            self.assertEqual(len(c['series'][0]['point_sources']),14)
        parameters=[source['parameters'] for source in report['sources']]
        self.assertEqual(parameters[0]['reference_temperature_sequence_c'],[40,35,30])
        self.assertEqual(parameters[0]['reference_direction'],'descending')
        self.assertEqual(parameters[1]['reference_temperature_sequence_c'],list(range(40,91,5)))
        self.assertEqual(parameters[1]['reference_direction'],'ascending')
        rows=report['tables']['calibration_points']['rows']
        self.assertEqual(len(rows),28)
        self.assertTrue(all(r['requires_manual_review'] for r in rows))
        self.assertTrue(any('参考稳定性不足' in w for w in report['warnings']))
        self.assertNotIn('PRIVATE_PORT',json.dumps(report))
        self.assertEqual(before,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in before})
        json.dumps(report,allow_nan=False)

    def test_missing_reference_never_uses_target_temperature(self):
        p=self.low/'calibration_points.json';d=json.loads(p.read_text());d['points'][0]['reference_mean_c']=None
        p.write_text(json.dumps(d));seal(self.low)
        report=build_comparison(self.root,self.keys)
        self.assertEqual(report['charts'][0]['series'][0]['x'],[30,35,*range(40,91,5)])
        self.assertNotIn({'key':self.keys[0],'point_id':1},report['charts'][0]['series'][0]['point_sources'])
        self.assertTrue(any('没有用目标温度补值' in w for w in report['warnings']))

    def test_unified_response_preserves_duplicate_observations_sd_and_point_provenance(self):
        path=self.high/'calibration_points.json'
        document=json.loads(path.read_text())
        document['points'][0]['channels']['S6'].update(mean_v=3.45,sample_sd_v=.034)
        path.write_text(json.dumps(document));seal(self.high)
        report=build_comparison(self.root,self.keys)
        plotted=report['charts'][0]['series'][0]
        at40=[i for i,x in enumerate(plotted['x']) if x==40]
        self.assertEqual([plotted['y'][i] for i in at40],[3.6,3.45])
        self.assertEqual([plotted['y_error'][i] for i in at40],[.01,.034])
        self.assertEqual([plotted['point_sources'][i] for i in at40],
                         [{'key':self.keys[0],'point_id':1},{'key':self.keys[1],'point_id':1}])
        table_rows=report['tables']['calibration_points']['rows']
        for chart in report['charts'][:2]:
            channel=chart['publication']['channel'];series=chart['series'][0]
            by_id={(row['key'],row['point_id']):row for row in table_rows if row['channel']==channel}
            for i,source in enumerate(series['point_sources']):
                row=by_id[(source['key'],source['point_id'])]
                self.assertEqual(series['data'][i],[row['reference_mean_c'],row['mean_v']])
                self.assertEqual(series['errors'][i],row['sample_sd_v'])
        self.assertTrue(any('并非一次连续采集' in warning for warning in report['warnings']))
        self.assertTrue(any('仅作视觉引导' in warning for warning in report['warnings']))
        # Combining reference-temperature observations never combines raw clocks.
        for chart in report['charts'][2:]:
            self.assertEqual([series['key'] for series in chart['series']],self.keys)
            self.assertTrue(all(series['x']==[.2,1.2] for series in chart['series']))
            self.assertTrue(all('point_sources' not in series for series in chart['series']))

    def test_within_limits_reference_is_not_mislabeled_as_insufficient(self):
        for path in (self.low,self.high):
            document=json.loads((path/'calibration_points.json').read_text())
            for point in document['points']:
                point['requires_manual_review']=False
                point['reference_stability']['status']='within_limits'
            (path/'calibration_points.json').write_text(json.dumps(document));seal(path)
        report=build_comparison(self.root,self.keys)
        self.assertFalse(any('个标定区间需要人工复核' in w for w in report['warnings']))

    def test_different_categories_cannot_be_combined(self):
        make_serial(self.root,'plain')
        with self.assertRaisesRegex(ValueError,'同一种实验类型'):
            build_comparison(self.root,[self.keys[0],'temperature_pressure_runs/plain'])

    def test_serial_raw_time_overlay_keeps_each_origin_and_breaks_reset(self):
        a=make_serial(self.root,'plain_a');b=make_serial(self.root,'plain_b')
        text=(b/'samples.csv').read_text().replace('1.2,3.1,200,0,S6','7.2,3.1,200,1,S6').replace('1.2,2.8,240,0,S7','7.2,2.8,240,1,S7')
        (b/'samples.csv').write_text(text);seal(b)
        report=build_comparison(self.root,['temperature_pressure_runs/plain_a','temperature_pressure_runs/plain_b'])
        self.assertEqual(report['kind'],'temperature_pressure')
        self.assertNotIn('presentation_mode',report)
        self.assertEqual(report['charts'][0]['series'][0]['data'],[[.2,3.],[1.2,3.1]])
        self.assertEqual(report['charts'][0]['series'][1]['data'],[[.2,3.],[7.2,None],[7.2,3.1]])
        self.assertEqual(len(report['tables']['serial_samples']['rows']),8)

    def test_running_unsealed_and_failed_records_are_rejected(self):
        original=json.loads((self.low/'manifest.json').read_text())
        variants=[dict(original,status='running'),dict(original,status='stopped'),dict(original,errors=['bad line'])]
        variants += [dict(original,**{key:False}) for key in ('serial_closed','workers_stopped','data_files_closed')]
        for manifest in variants:
            with self.subTest(manifest=manifest):
                (self.low/'manifest.json').write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError,'未封存|停止证据'):
                    build_comparison(self.root,self.keys)

    def test_modified_sealed_samples_and_calibration_are_rejected(self):
        for name in ('samples.csv','calibration_points.json'):
            p=self.low/name;before=p.read_bytes();p.write_bytes(before+b' ')
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'校验失败'):
                build_comparison(self.root,self.keys)
            p.write_bytes(before)

    def test_bad_numeric_summary_and_duplicate_point_ids_are_rejected(self):
        p=self.low/'calibration_points.json';original=json.loads(p.read_text())
        cases=[]
        for value in (float('nan'),-1):
            d=deepcopy(original);d['points'][0]['channels']['S6']['sample_sd_v']=value;cases.append(d)
        d=deepcopy(original);d['points'][1]['id']=1;cases.append(d)
        for d in cases:
            p.write_text(json.dumps(d));seal(self.low)
            with self.assertRaises(ValueError):build_comparison(self.root,self.keys)

    def test_path_traversal_symlink_and_selection_limits_are_rejected(self):
        for keys in ([], self.keys*2,[self.keys[0],'temperature_pressure_runs/../high']):
            with self.assertRaises(ValueError):build_comparison(self.root,keys)
        p=self.low/'samples.csv';data=p.read_bytes();p.unlink();outside=self.root/'outside.csv';outside.write_bytes(data);p.symlink_to(outside)
        with self.assertRaisesRegex(ValueError,'符号链接'):build_comparison(self.root,self.keys)

    def test_category_recognizes_serial_names_without_assuming_rf(self):
        self.assertEqual(history_category({'key':self.keys[0],'category':'temperature_calibration'}),'temperature_calibration')
        self.assertEqual(history_category({'key':'temperature_pressure_runs/a'}),'temperature_pressure')

    def test_service_hashes_new_modules_and_exports_publication_artifacts(self):
        for name in ('sspp_experiments/temperature_pressure/comparison.py','sspp_desktop/publication.py'):
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('# code snapshot\n')
        self.write_correction()
        service=DesktopService(Settings(root=self.root));report=service.compare_runs(self.keys)
        self.assertTrue({'sspp_experiments/temperature_pressure/comparison.py','sspp_desktop/publication.py'}.issubset({r['path'] for r in report['analysis_code']}))
        def figures(args,**kwargs):
            out=Path(args[-1]);(out/'comparison_report.pdf').write_bytes(b'PDF');(out/'comparison_01_temperature.png').write_bytes(b'PNG')
            for stem in ('publication_figure','publication_S6','publication_S7'):
                for suffix in ('.pdf','.svg','.png'):(out/(stem+suffix)).write_bytes(b'figure')
            (out/'caption.txt').write_text('Measured interval means; error bars are sample SD.')
            return SimpleNamespace(returncode=0,stderr='')
        destination=self.root/'combo.zip'
        with patch('sspp_desktop.comparison_service.subprocess.run',side_effect=figures):
            service.export_comparison(report,destination)
        with zipfile.ZipFile(destination) as z:
            self.assertIn('figures/publication_figure.svg',z.namelist())
            self.assertIn('figures/publication_S7.pdf',z.namelist())
            self.assertIn('figures/caption.txt',z.namelist())
            self.assertIn('sources/'+self.keys[0]+'/calibration_points.json',z.namelist())
            self.assertIn('tables/calibration_points.csv',z.namelist())
            self.assertIn('corrections/'+self.keys[0]+'.json',z.namelist())
            self.assertEqual(json.loads(z.read('comparison.json'))['excluded_calibration_points'][0]['point_id'],1)
        before=destination.read_bytes()
        def changed_while_rendering(args,**kwargs):
            result=figures(args,**kwargs);self.write_correction(deleted={});return result
        with patch('sspp_desktop.comparison_service.subprocess.run',side_effect=changed_while_rendering):
            with self.assertRaisesRegex(LabError,'变化|改变'):service.export_comparison(report,destination)
        self.assertEqual(destination.read_bytes(),before)
        (self.low/'samples.csv').write_text('changed')
        with self.assertRaisesRegex(LabError,'比较后已改变'):service._comparison_source_files(report)

    def test_actual_records_preserve_14_raw_intervals_and_respect_derived_corrections(self):
        root=Path('/Users/cunhui/Git-Projects/Academic_Research/multisensor-workbench')
        keys=['temperature_pressure_runs/20261102T064612_169547Z','temperature_pressure_runs/20261102T041220_624444Z']
        if not all((root/'Data'/k/'manifest.json').is_file() for k in keys):self.skipTest('actual sealed records not installed')
        original=[json.loads((root/'Data'/key/'calibration_points.json').read_text())['points'] for key in keys]
        self.assertEqual([len(points) for points in original],[3,11])
        expected=[]
        for key,points in zip(keys,original):
            correction=root/'Data/corrections'/(key+'.json')
            deleted=json.loads(correction.read_text())['deleted_points'] if correction.is_file() else {}
            expected.append(sum(str(p['id']) not in deleted for p in points))
        report=build_comparison(root,keys)
        self.assertEqual([m['saved_calibration_points'] for m in report['metrics']],expected)
        self.assertEqual(len(report['tables']['calibration_points']['rows']),sum(expected)*2)
        self.assertEqual(sum(len(s['data']) for s in report['charts'][0]['series']),sum(expected))
        for chart in report['charts'][:2]:
            self.assertEqual(len(chart['series']),1)
            series=chart['series'][0]
            self.assertEqual(series['x'],sorted(series['x']))
            self.assertEqual(len(series['point_sources']),sum(expected))
            for exclusion in report['excluded_calibration_points']:
                self.assertNotIn({'key':exclusion['run_key'],'point_id':exclusion['point_id']},series['point_sources'])
        self.assertTrue(all(r['requires_manual_review'] for r in report['tables']['calibration_points']['rows']))


if __name__=='__main__':unittest.main()
