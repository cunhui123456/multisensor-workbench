"""Fresh RF publication reports bind exactly the saved diagnostic dependencies."""
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_lab.controller import LabError
from sspp_lab.diagnostics import build_diagnostics
import test_comparison_domain as domain
from test_serial_comparison_backend import make_serial


class PublicationAnalysisTests(unittest.TestCase):
    save = domain.ComparisonTests.save
    manifest = domain.ComparisonTests.manifest
    sweep = domain.ComparisonTests.sweep

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.service = DesktopService(Settings(root=self.root))

    def iq(self, run, name, amplitude=.1):
        iq = (amplitude*np.exp(2j*np.pi*10000*np.arange(1024)/250000)).astype(np.complex64)
        path = run/name
        if name.endswith('.npz'):
            np.savez(path, iq=iq, rx_rate=250000., point_info=json.dumps({'tone_hz':10000., 'requested_carrier_hz':2.4e9}))
        else:
            iq.tofile(path)
        return path

    def phase(self, key='phase_runs/one'):
        self.save(key, 'manifest.json', self.manifest(key))
        self.save(key, 'desktop_context.json', {'experiment':'finger_displacement'})
        self.save(key, 'baseline.json', {'start_s':1., 'end_s':2., 'reference_amplitude':.1,
            'reference_phase_unwrapped_deg':0., 'amplitude_sd_dB':0.})
        run = self.root/'Data'/key
        rows = [{'t_s':index*.1, 'amplitude_dBFS_est':-20., 'phase_unwrapped_deg':index*.2,
                 'i':.1, 'q':0., 'label':'initial' if index<25 else 'sample',
                 'coherent_fraction':.99, 'phase_step_deg':.2} for index in range(50)]
        (run/'amplitude_phase.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
        (run/'rx_frames.jsonl').write_text(json.dumps({'start_sample':0, 'count':1024,
                                                       'device_tick_at_rx_rate':0})+'\n')
        self.iq(run, 'rx_iq.cf32')
        return run

    def test_fresh_sweep_ignores_diagnostic_cache_and_binds_latest_raw_point(self):
        key = 'runs/one'; self.sweep(key); run = self.root/'Data'/key
        self.iq(run, 'iq_point_001.npz', .05)
        latest = self.iq(run, 'iq_point_002.npz', .1)
        before = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in run.iterdir()}
        with patch.object(self.service, 'diagnostic_view', side_effect=AssertionError('cache forbidden')):
            first = self.service.publication_analysis(key)
            self.iq(run, latest.name, .2)
            second = self.service.publication_analysis(key)
        self.assertEqual(first['analysis_mode'], 'single')
        self.assertEqual(first['kind'], 'material_sweep')
        self.assertNotEqual(first['comparison_id'], second['comparison_id'])
        first_wave = next(chart for chart in first['charts'] if chart['id']=='rx_wave')
        second_wave = next(chart for chart in second['charts'] if chart['id']=='rx_wave')
        self.assertNotEqual(first_wave['series'][0]['data'], second_wave['series'][0]['data'])
        names = [entry['relative_path'] for entry in first['sources'][0]['files']]
        self.assertIn('iq_point_002.npz', names); self.assertNotIn('iq_point_001.npz', names)
        self.assertTrue(all(series['key']==key for chart in first['charts'] for series in chart.get('series', [])))
        self.assertTrue(all(chart['source_key']==key for chart in first['charts']))
        for path in run.iterdir():
            if path.name != latest.name:
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before[path.name])

    def test_phase_iq_is_hashed_as_stream_and_all_dependencies_are_preserved(self):
        key = 'phase_runs/one'; run = self.phase(key)
        original_read = Path.read_bytes
        def no_large_read(path):
            if path.suffix in ('.cf32', '.npz'):
                raise AssertionError('IQ must not be copied into memory for hashing')
            return original_read(path)
        with patch.object(Path, 'read_bytes', no_large_read):
            report = self.service.publication_analysis(key)
        self.assertEqual(report['kind'], 'finger_displacement')
        entries = {entry['relative_path']:entry for entry in report['sources'][0]['files']}
        self.assertTrue({'manifest.json','desktop_context.json','baseline.json','amplitude_phase.jsonl',
                         'rx_frames.jsonl','rx_iq.cf32'}.issubset(entries))
        with (run/'rx_iq.cf32').open('rb') as content:
            self.assertEqual(entries['rx_iq.cf32']['sha256'], hashlib.file_digest(content,'sha256').hexdigest())
        self.assertIn('stages', [chart['id'] for chart in report['charts']])

    def test_new_latest_iq_or_new_optional_baseline_invalidates_export_binding(self):
        key='runs/one'; self.sweep(key); run=self.root/'Data'/key
        self.iq(run,'iq_point_001.npz')
        report=self.service.publication_analysis(key)
        self.iq(run,'iq_point_002.npz')
        with self.assertRaisesRegex(LabError,'依赖文件在分析后已改变'):
            self.service._comparison_source_files(report)
        (run/'iq_point_002.npz').unlink()
        self.save(key,'baseline.json',{})
        with self.assertRaisesRegex(LabError,'依赖文件在分析后已改变'):
            self.service._comparison_source_files(report)

    def test_changed_input_during_fresh_build_is_rejected(self):
        key='runs/one'; self.sweep(key); run=self.root/'Data'/key
        def changed(path,mode):
            result=build_diagnostics(path,mode)
            with (run/'measurements.csv').open('a') as content:content.write('changed')
            return result
        with patch('sspp_desktop.publication_analysis.build_diagnostics',side_effect=changed):
            with self.assertRaisesRegex(LabError,'分析中发生变化'):
                self.service.publication_analysis(key)
        self.assertFalse(getattr(self.service,'_comparison_digests',{}))
        self.assertFalse(self.service.exporting)

    def test_sealed_decision_cannot_use_metadata_changed_after_initial_hash(self):
        from sspp_desktop.publication_analysis import _json
        key='runs/one';self.sweep(key)
        changed=False
        def mutate_after_read(run,name,required=False):
            nonlocal changed
            result=_json(run,name,required)
            if name=='manifest.json' and not changed:
                updated=dict(result,status='running')
                self.save(key,'manifest.json',updated);changed=True
            return result
        with patch('sspp_desktop.publication_analysis._json',side_effect=mutate_after_read):
            with self.assertRaisesRegex(LabError,'分析中发生变化'):
                self.service.publication_analysis(key)

    def test_all_invalid_frequency_keeps_gap_and_sd_alignment_through_real_figure_archive(self):
        key='runs/gapped'
        self.sweep(key, values=((-40.,-30.,-20.),(-42.,-32.,-22.)),
                   frequencies=(2.4e9,2.5e9,2.6e9),tones=(2000.,3000.,4000.))
        path=self.root/'Data'/key/'measurements.csv'
        with path.open() as content:
            reader=csv.DictReader(content);columns=reader.fieldnames+['measurement_status'];rows=list(reader)
        for row in rows:row['measurement_status']='invalid' if row['point']=='2' else 'valid'
        with path.open('w') as content:
            writer=csv.DictWriter(content,fieldnames=columns);writer.writeheader();writer.writerows(rows)
        report=self.service.publication_analysis(key)
        chart=next(chart for chart in report['charts'] if chart['id']=='sweep_mean')
        self.assertEqual(chart['series'][0]['data'],[[2.4,-41.],[2.5,None],[2.6,-21.]])
        self.assertEqual(len(chart['series'][0]['errors']),3)
        self.assertIsNone(chart['series'][0]['errors'][1])
        result=self.service.export_publication_figure(report,'sweep_mean')
        directory=Path(result['directory'])
        self.assertEqual(set(result['files']),{'pdf','svg','png'})
        self.assertTrue(all(Path(path).stat().st_size>1000 for path in result['files'].values()))
        with (directory/'data.csv').open(encoding='utf-8-sig') as content:
            exported=list(csv.DictReader(content))
        self.assertEqual(len(exported),3)
        self.assertEqual(exported[1]['x'],'2.5')
        self.assertEqual(exported[1]['y'],'')
        self.assertEqual(exported[1]['error'],'')

    def test_unsealed_rf_and_continuous_temperature_without_serial_stop_are_rejected(self):
        key='runs/one'; original=self.sweep(key)
        for field in ('tx_end_of_burst_sent','rx_stop_command_sent','workers_stopped'):
            manifest=json.loads(json.dumps(original));manifest['telemetry'][field]=False
            self.save(key,'manifest.json',manifest)
            with self.subTest(field=field),self.assertRaisesRegex(LabError,'清洁停机'):
                self.service.publication_analysis(key)
        self.save(key,'manifest.json',original)
        self.save(key,'desktop_context.json',{'experiment':'temperature_sweep','parameters':{'temperature_sweep':{'mode':'continuous'}}})
        with self.assertRaisesRegex(LabError,'串口完整封存'):
            self.service.publication_analysis(key)
        original['temperature_sweep']={'status':'complete','serial_workers_stopped':True,'serial_closed':True,'data_files_closed':True}
        self.save(key,'manifest.json',original)
        report=self.service.publication_analysis(key)
        self.assertEqual(report['kind'],'temperature_sweep')
        self.assertIn('sweep_mean',[chart['id'] for chart in report['charts']])

    def test_serial_routes_to_existing_domain_and_teacher_or_empty_run_reports_clear_error(self):
        make_serial(self.root,'serial',[30,35])
        report=self.service.publication_analysis('temperature_pressure_runs/serial')
        self.assertEqual(report['kind'],'temperature_calibration')
        self.save('teacher_runs/one','feature_report.json',{})
        with self.assertRaisesRegex(LabError,'老师历史'):
            self.service.publication_analysis('teacher_runs/one')
        self.save('runs/empty','manifest.json',self.manifest('runs/empty'))
        with self.assertRaisesRegex(LabError,'没有可重建'):
            self.service.publication_analysis('runs/empty')

    def test_linked_dependency_and_active_capture_are_rejected(self):
        key='runs/one';self.sweep(key);run=self.root/'Data'/key
        outside=self.root/'outside.npz';outside.write_bytes(b'not IQ')
        (run/'iq_point_001.npz').symlink_to(outside)
        with self.assertRaisesRegex(LabError,'符号链接'):
            self.service.publication_analysis(key)
        (run/'iq_point_001.npz').unlink()
        self.service.job={'status':'running'}
        with self.assertRaises(LabError):self.service.publication_analysis(key)


if __name__=='__main__':unittest.main()
