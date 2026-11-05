"""Pressure records retain force units, grouping, immutable provenance and export."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_desktop.publication import build_analysis_figure, analysis_figure_data
from sspp_experiments.comparison import build_comparison
from sspp_desktop.serial_archive import serial_category
from sspp_lab.controller import LabError


def make_pressure(root, name='pressure', area=None, direction='loading'):
    path=Path(root)/'Data'/'temperature_pressure_runs'/name
    path.mkdir(parents=True)
    (path/'samples.csv').write_text('host_elapsed_s,voltage_v,mcu_time_ms,time_segment,channel,adc\n0,1,0,0,S6,200\n0,2,0,0,S7,400\n1,1.1,1000,0,S6,220\n1,2.1,1000,0,S7,420\n')
    (path/'raw_serial.bin').write_bytes(b'raw pressure fixture\n')
    points=[]
    for ident,force in enumerate((0.,2.,4.),1):
        points.append({'id':ident,'status':'saved','start_s':(ident-1)*30.,'end_s':ident*30.,'duration_s':30.,
            'reference_mean_n':force,'reference_mean_kpa':force*1000/area if area else None,
            'target_force_n':force+1,'contact_area_mm2':area,'pressure_channel':'S6','direction':direction,'cycle':1,
            'reference_source':'Independent force gauge','reference_stability':{'status':'within_limits'},
            'requires_manual_review':True,'quality_flags':[],
            'channels':{'S6':{'count':31,'mean_v':1+force*.2,'sample_sd_v':.001},
                        'S7':{'count':31,'mean_v':2.5,'sample_sd_v':.002}}})
    (path/'calibration_points.json').write_text(json.dumps({'schema':'sspp.pressure_plateaus.v1','points':points}))
    manifest={'status':'complete','sample_name':name,'started_utc':'2026-11-04T00:00:00Z','errors':[],
              'experiment':'temperature_pressure','pressure_calibration':{'mode':'force_intervals'},
              'protocol':'pressure_v1','serial':{'baudrate':9600},'rows':4,'channels':['S6','S7'],
              'serial_closed':True,'workers_stopped':True,'data_files_closed':True}
    seal(path,manifest)
    return path


def seal(path, manifest=None):
    manifest=manifest or json.loads((path/'manifest.json').read_text())
    manifest['files']=[]
    for name in ('calibration_points.json','samples.csv','raw_serial.bin'):
        content=(path/name).read_bytes()
        manifest['files'].append({'path':name,'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()})
    (path/'manifest.json').write_text(json.dumps(manifest))


class PressureArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.path=make_pressure(self.root);self.key='temperature_pressure_runs/pressure'
        self.service=DesktopService(Settings(root=self.root))

    def tearDown(self):self.temp.cleanup()

    def test_separate_category_details_and_force_units_without_area(self):
        self.assertEqual(self.service.history()[0]['category'],'pressure_calibration')
        details=self.service.details(self.key)
        self.assertEqual(details['context']['experiment'],'pressure_calibration')
        self.assertEqual(details['calibration_points'][2]['reference_mean_n'],4)
        self.assertEqual(details['curve']['stored_rows'],4)
        report=build_comparison(self.root,[self.key])
        self.assertEqual(report['kind'],'pressure_calibration')
        self.assertFalse(report['automatic_calibration_activated'])
        self.assertEqual([chart['id'] for chart in report['charts']],['pressure_force_S6','serial_voltage_S6','serial_voltage_S7'])
        self.assertEqual(report['charts'][0]['series'][0]['data'],[[0.,1.],[2.,1.4],[4.,1.8]])
        self.assertEqual(len(report['tables']['pressure_calibration_points']['rows']),3)
        fits=report['tables']['pressure_candidate_fits']['rows']
        self.assertEqual(len(fits),1)
        self.assertEqual(fits[0]['status'],'candidate')
        self.assertAlmostEqual(fits[0]['slope_n_per_v'],5)
        self.assertAlmostEqual(fits[0]['intercept_n'],-5)
        self.assertEqual(report['tables']['serial_samples']['rows'][1]['channel'],'S7')
        json.dumps(report,allow_nan=False)

    def test_v2_archive_keeps_manual_delta_and_breaks_baseline_changes(self):
        from PySide6 import QtWidgets
        from sspp_desktop.serial_archive_widget import SerialArchiveWidget
        from sspp_desktop.qt_cleanup import close_plot_widgets
        data=json.loads((self.path/'calibration_points.json').read_text())
        for point in data['points']:
            point['pressure_channel']='S3'
            point['channels']={'S3':point['channels']['S6']}
        (self.path/'calibration_points.json').write_text(json.dumps(data))
        (self.path/'samples.csv').write_text(
            'host_elapsed_s,voltage_v,mcu_time_ms,time_segment,channel,adc,baseline_v,delta_voltage_v,zero_id,zero_count,selection_token\n'
            '0,2.7,0,0,S3,552,,,,0,77\n'
            '1,2.8,1000,0,S3,573,2.7,0.1,1,40,77\n'
            '2,2.9,2000,0,S3,593,2.9,0,2,40,77\n')
        # The first unset baseline still carries explicit zero_id 0.
        raw=(self.path/'samples.csv').read_text().replace('552,,,,0,77','552,,,0,0,77')
        (self.path/'samples.csv').write_text(raw)
        manifest=json.loads((self.path/'manifest.json').read_text())
        manifest.update(protocol='pressure_v2',rows=3,channels=['S3'],pressure_channel='S3')
        seal(self.path,manifest)
        details=self.service.details(self.key)
        self.assertIsNone(details['curve']['points'][0]['delta_voltage_v'])
        self.assertEqual(details['curve']['points'][1]['delta_voltage_v'],.1)
        self.assertEqual(details['metadata']['pressure_channel'],'S3')
        report=build_comparison(self.root,[self.key])
        self.assertEqual(report['tables']['serial_samples']['rows'][2]['baseline_v'],2.9)
        self.assertEqual(report['tables']['serial_samples']['rows'][2]['zero_id'],2)
        app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        widget=SerialArchiveWidget()
        try:
            widget.display(details)
            self.assertIn('ΔV',widget.adc.plotItem.titleLabel.text)
            values=widget.adc.listDataItems()[0].yData
            self.assertTrue(any(__import__('math').isnan(v) for v in values))
            self.assertEqual(values[-1],0)
        finally:
            widget.close();close_plot_widgets(widget);widget.deleteLater();app.processEvents()

    def test_area_conversion_and_separate_run_direction_fits(self):
        second=make_pressure(self.root,'unloading',area=20,direction='unloading')
        report=build_comparison(self.root,[self.key,'temperature_pressure_runs/unloading'])
        force=next(c for c in report['charts'] if c['id']=='pressure_force_S6')
        pressure=next(c for c in report['charts'] if c['id']=='pressure_kpa_S6')
        self.assertEqual(len(force['series']),2)
        self.assertEqual(len(pressure['series']),1)
        self.assertEqual(pressure['series'][0]['data'],[[0.,1.],[100.,1.4],[200.,1.8]])
        fits=report['tables']['pressure_candidate_fits']['rows']
        self.assertEqual({fit['key'] for fit in fits},{self.key,'temperature_pressure_runs/unloading'})
        self.assertTrue(all(fit['status']=='candidate' for fit in fits))
        figure=build_analysis_figure(report,'pressure_kpa_S6')
        self.assertEqual(figure.axes[0].get_xlabel(),'Mean contact pressure (kPa)')
        figure.clear()
        self.assertEqual(analysis_figure_data(report,'pressure_kpa_S6')[2]['source_point_id'],3)

    def test_deleted_point_removed_from_response_and_fit_without_source_mutation(self):
        before={p.name:p.read_bytes() for p in self.path.iterdir()}
        self.service.correct_calibration_points(self.key,[2],'wrong force reading')
        report=build_comparison(self.root,[self.key])
        self.assertEqual(len(report['charts'][0]['series'][0]['data']),2)
        self.assertNotEqual(report['tables']['pressure_candidate_fits']['rows'][0]['status'],'candidate')
        self.assertEqual(report['excluded_calibration_points'][0]['reference_mean_n'],2)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.path.iterdir()})
        self.service.correct_calibration_points(self.key,[2],'restore',restore=True)
        self.assertEqual(build_comparison(self.root,[self.key])['tables']['pressure_candidate_fits']['rows'][0]['status'],'candidate')

    def test_missing_reference_target_never_substituted_and_quality_points_not_fitted(self):
        content=json.loads((self.path/'calibration_points.json').read_text())
        content['points'][0]['reference_mean_n']=None
        content['points'][1]['quality_flags']=['voltage_data_gap']
        (self.path/'calibration_points.json').write_text(json.dumps(content));seal(self.path)
        report=build_comparison(self.root,[self.key])
        self.assertEqual(report['charts'][0]['series'][0]['data'],[[2.,1.4],[4.,1.8]])
        self.assertNotEqual(report['tables']['pressure_candidate_fits']['rows'][0]['status'],'candidate')

    def test_pressure_files_sealed_and_bundle_has_original_points(self):
        target=self.root/'pressure.zip'
        self.service.export_bundle(self.key,target)
        with zipfile.ZipFile(target) as bundle:
            self.assertEqual(bundle.read('results/calibration_points.json'),(self.path/'calibration_points.json').read_bytes())
        (self.path/'calibration_points.json').write_bytes((self.path/'calibration_points.json').read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'校验失败'):
            build_comparison(self.root,[self.key])

    def test_actual_publication_export_and_source_hash_guard(self):
        from PIL import Image
        report=self.service.compare_runs([self.key])
        before={p.name:p.read_bytes() for p in self.path.iterdir()}
        result=self.service.export_publication_figure(report,'pressure_force_S6')
        directory=Path(result['directory'])
        self.assertIn('Figures/pressure_calibration/',directory.as_posix())
        self.assertTrue(Path(result['files']['pdf']).read_bytes().startswith(b'%PDF'))
        svg=Path(result['files']['svg']).read_text()
        self.assertIn('<svg',svg)
        self.assertIn('Normal contact force (N)',svg)
        with Image.open(result['files']['png']) as image:
            self.assertGreater(image.width,4000)
            self.assertAlmostEqual(image.info['dpi'][0],600,delta=1)
        manifest=json.loads((directory/'figure_manifest.json').read_text())
        self.assertEqual(manifest['source_keys'],[self.key])
        self.assertEqual(manifest['experiment'],'pressure_calibration')
        for artifact in manifest['files']:
            self.assertEqual(hashlib.sha256((directory/artifact['path']).read_bytes()).hexdigest(),artifact['sha256'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.path.iterdir()})
        self.service.correct_calibration_points(self.key,[1],'changed after analysis')
        with self.assertRaisesRegex(LabError,'比较后已改变'):
            self.service.export_analysis_figure(report,'pressure_force_S6',self.root.resolve()/'outdated.svg')
        self.assertFalse((self.root.resolve()/'outdated.svg').exists())

    def test_native_pressure_channel_a2_in_history_and_analysis(self):
        from PySide6 import QtWidgets
        from sspp_desktop.serial_archive_widget import SerialArchiveWidget
        data=json.loads((self.path/'calibration_points.json').read_text())
        for point in data['points']:
            point['pressure_channel']='A2'
            point['channels']={'A2':point['channels']['S6']}
        (self.path/'calibration_points.json').write_text(json.dumps(data))
        lines=(self.path/'samples.csv').read_text().splitlines()
        (self.path/'samples.csv').write_text('\n'.join(line.replace('S6','A2') for line in lines if 'S7' not in line)+'\n')
        manifest=json.loads((self.path/'manifest.json').read_text());manifest.update(rows=2,channels=['A2'])
        seal(self.path,manifest)
        details=self.service.details(self.key)
        app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        widget=SerialArchiveWidget()
        try:
            widget.display(details)
            self.assertEqual(widget.table.horizontalHeaderItem(6).text(),'A2均值±SD V')
            self.assertIn('1.00000',widget.table.item(0,6).text())
            self.assertEqual(widget.table.columnCount(),8)
            widget.table.selectRow(0)
            self.assertTrue(widget.calibration_delete_button.isEnabled())
        finally:
            widget.close();widget.deleteLater();app.processEvents()
        report=build_comparison(self.root,[self.key])
        self.assertEqual(report['charts'][0]['id'],'pressure_force_A2')
        self.assertEqual(report['tables']['pressure_candidate_fits']['rows'][0]['channel'],'A2')

    def test_pressure_schema_without_metadata_still_uses_pressure_category(self):
        manifest=json.loads((self.path/'manifest.json').read_text())
        manifest.pop('pressure_calibration')
        (self.path/'manifest.json').write_text(json.dumps(manifest))
        self.assertEqual(self.service.history()[0]['category'],'pressure_calibration')
        self.assertEqual(build_comparison(self.root,[self.key])['kind'],'pressure_calibration')

    def test_inconsistent_area_or_duplicate_ids_are_rejected(self):
        original=json.loads((self.path/'calibration_points.json').read_text())
        for change in ('pressure','duplicate'):
            doc=deepcopy(original)
            if change=='pressure':doc['points'][0]['reference_mean_kpa']=1
            else:doc['points'][1]['id']=1
            (self.path/'calibration_points.json').write_text(json.dumps(doc));seal(self.path)
            with self.assertRaises(ValueError):build_comparison(self.root,[self.key])


if __name__=='__main__':unittest.main()
