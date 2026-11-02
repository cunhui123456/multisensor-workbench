"""Single-source saved analysis keeps statistics and integrity without self-differences."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_experiments.comparison import build_comparison
from sspp_lab.controller import LabError
import test_comparison_domain as domain
from test_serial_comparison_backend import make_serial


class SingleRecordAnalysisTests(unittest.TestCase):
    save = domain.ComparisonTests.save
    manifest = domain.ComparisonTests.manifest
    sweep = domain.ComparisonTests.sweep
    phase = domain.ComparisonTests.phase
    write_rows = domain.ComparisonTests.write_rows

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def assert_single(self, report, key):
        self.assertEqual(report['analysis_mode'], 'single')
        self.assertEqual(report['keys'], [key])
        self.assertEqual([source['key'] for source in report['sources']], [key])
        self.assertIsNone(report['reference_key'])
        self.assertFalse(report['quantitative_difference_allowed'])
        self.assertEqual(report['compatibility'], [])
        self.assertTrue(report['charts'])
        self.assertTrue(all(len(chart['series']) == 1 for chart in report['charts']))
        self.assertEqual(len(report['metrics']), 1)
        json.dumps(report, allow_nan=False)

    def calibration(self):
        key = 'temperature_pressure_runs/calibration'
        run = make_serial(self.root, 'calibration', [40, 35, 30])
        correction = self.root / 'Data/corrections' / (key + '.json')
        correction.parent.mkdir(parents=True)
        correction.write_text(json.dumps({'version': 1, 'run_key': key,
            'source_calibration_sha256': hashlib.sha256((run / 'calibration_points.json').read_bytes()).hexdigest(),
            'deleted_points': {'1': {'reason': 'incorrect reference', 'deleted_utc': '2026-11-02T08:00:00Z'}},
            'history': []}))
        return key, run, correction

    def test_single_calibration_keeps_means_sd_corrections_and_raw_time(self):
        key, run, correction = self.calibration()
        before = {p: p.read_bytes() for p in run.iterdir()}
        report = build_comparison(self.root, [key])
        self.assert_single(report, key)
        self.assertEqual(report['kind'], 'temperature_calibration')
        self.assertEqual(report['presentation_mode'], 'single_temperature_response')
        self.assertFalse(report['charts'][0]['publication']['combined_sources'])
        self.assertEqual(report['charts'][0]['series'][0]['key'], key)
        self.assertNotIn('多记录组合', '\n'.join(report['warnings'] + [c['note'] for c in report['charts']]))
        self.assertTrue(all('Combined' not in s['label'] for c in report['charts'] for s in c['series']))
        self.assertEqual(report['charts'][0]['series'][0]['x'], [30, 35])
        self.assertEqual(report['charts'][0]['series'][0]['errors'], [.01, .01])
        self.assertEqual(len(report['charts']), 4)
        self.assertEqual(len(report['tables']['calibration_points']['rows']), 4)
        self.assertEqual(len(report['tables']['serial_samples']['rows']), 4)
        self.assertEqual(report['excluded_calibration_points'][0]['point_id'], 1)
        self.assertFalse(report['automatic_calibration_activated'])
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_single_ordinary_serial_keeps_received_time(self):
        key = 'temperature_pressure_runs/plain'
        make_serial(self.root, 'plain')
        report = build_comparison(self.root, [key])
        self.assert_single(report, key)
        self.assertEqual(report['kind'], 'temperature_pressure')
        self.assertEqual(report['charts'][0]['series'][0]['data'], [[.2, 3.], [1.2, 3.1]])
        self.assertEqual(report['metrics'][0]['raw_sample_count'], 4)

    def test_single_sweep_retains_repeat_statistics_without_self_comparison(self):
        key = 'runs/one'
        self.sweep(key)
        report = build_comparison(self.root, [key], reference_key=key)
        self.assert_single(report, key)
        self.assertEqual([c['id'] for c in report['charts']], ['sweep_mean', 'sweep_repeat_sd'])
        self.assertEqual([y for x, y in report['charts'][0]['series'][0]['data']], [-41., -31.])
        self.assertAlmostEqual(report['charts'][0]['series'][0]['errors'][0], 2**.5)
        self.assertEqual(len(report['tables']['sweep_measurements']['rows']), 4)
        self.assertNotIn('sweep_difference', report['tables'])
        self.assertNotIn('common_points_vs_reference', report['metrics'][0])
        self.assertNotIn('mean_difference_dB', report['metrics'][0])
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_single_phase_and_finger_keep_their_own_saved_baseline(self):
        for index, finger in enumerate((False, True)):
            key = 'phase_runs/one' + str(index)
            self.phase(key, baseline_end=7., offset=100., finger=finger)
            report = build_comparison(self.root, [key])
            with self.subTest(finger=finger):
                self.assert_single(report, key)
                self.assertEqual(report['kind'], 'finger_displacement' if finger else 'phase_history')
                self.assertAlmostEqual(report['charts'][1]['series'][0]['data'][0][0], .1)
                self.assertEqual(report['charts'][1]['series'][0]['data'][0][1], 101.)
                self.assertEqual(len(report['tables']['phase_curves']['rows']), 29)
                self.assertNotIn('estimated_distance_mm', json.dumps(report))

    def test_single_keeps_seal_hash_and_correction_checks(self):
        key, run, correction = self.calibration()
        original = json.loads((run / 'manifest.json').read_text())
        for field in ('serial_closed', 'workers_stopped', 'data_files_closed'):
            changed = dict(original, **{field: False})
            (run / 'manifest.json').write_text(json.dumps(changed))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, '封存|停止证据'):
                build_comparison(self.root, [key])
        (run / 'manifest.json').write_text(json.dumps(original))
        ledger = json.loads(correction.read_text())
        ledger['source_calibration_sha256'] = '0' * 64
        correction.write_text(json.dumps(ledger))
        with self.assertRaisesRegex(ValueError, '哈希不匹配'):
            build_comparison(self.root, [key])
        self.sweep('runs/one')
        manifest = json.loads((self.root / 'Data/runs/one/manifest.json').read_text())
        manifest['telemetry']['workers_stopped'] = False
        self.save('runs/one', 'manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, '停止证据'):
            build_comparison(self.root, ['runs/one'])

    def test_teacher_and_liquid_phase_keep_their_separate_workflows(self):
        self.save('teacher_runs/one', 'feature_report.json', {'status': 'complete'})
        with self.assertRaisesRegex(ValueError, '老师历史'):
            build_comparison(self.root, ['teacher_runs/one'])
        self.phase('phase_runs/liquid')
        self.save('phase_runs/liquid', 'desktop_context.json', {'experiment': 'liquid_temperature_phase'})
        with self.assertRaisesRegex(ValueError, '液体温度影响页'):
            build_comparison(self.root, ['phase_runs/liquid'])

    @staticmethod
    def fake_figures(args, **kwargs):
        out = Path(args[-1])
        (out / 'comparison_01_calibration.png').write_bytes(b'plot')
        (out / 'comparison_report.pdf').write_bytes(b'report')
        for stem in ('publication_figure', 'publication_S6', 'publication_S7'):
            for suffix in ('.pdf', '.svg', '.png'):
                (out / (stem + suffix)).write_bytes(b'publication')
        (out / 'caption.txt').write_text('One saved record; interval sample SD.')
        return SimpleNamespace(returncode=0, stderr='')

    def test_single_export_hashes_one_source_and_correction_and_rejects_later_edits(self):
        key, run, correction = self.calibration()
        service = DesktopService(Settings(root=self.root))
        report = service.compare_runs([key])
        target = self.root / 'single.zip'
        with patch('sspp_desktop.comparison_service.subprocess.run', side_effect=self.fake_figures):
            result = service.export_comparison(report, target)
        with zipfile.ZipFile(target) as archive:
            index = json.loads(archive.read('bundle_manifest.json'))
            self.assertEqual(index['source_keys'], [key])
            self.assertEqual(index['analysis_mode'], 'single')
            self.assertIsNone(index['reference_key'])
            self.assertIn('corrections/' + key + '.json', archive.namelist())
            self.assertEqual(json.loads(archive.read('comparison.json')), report)
            for entry in index['files']:
                content = archive.read(entry['path'])
                self.assertEqual(hashlib.sha256(content).hexdigest(), entry['sha256'])
                self.assertEqual(len(content), entry['bytes'])
        self.assertEqual(result['sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
        old_target = target.read_bytes()
        ledger = json.loads(correction.read_text())
        ledger['deleted_points'] = {}
        correction.write_text(json.dumps(ledger))
        with self.assertRaisesRegex(LabError, '比较后已改变'):
            service.export_comparison(report, target)
        self.assertEqual(target.read_bytes(), old_target)

    def test_active_acquisition_still_blocks_single_analysis_and_export(self):
        key, run, correction = self.calibration()
        service = DesktopService(Settings(root=self.root))
        report = service.compare_runs([key])
        for status in ('starting', 'running', 'stopping', 'saving'):
            service.job = {'status': status}
            with self.subTest(status=status):
                with self.assertRaises(LabError): service.compare_runs([key])
                with self.assertRaises(LabError): service.export_comparison(report, self.root / 'single.zip')
        self.assertFalse((self.root / 'single.zip').exists())


if __name__ == '__main__':
    unittest.main()
