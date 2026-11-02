"""Verified single-chart exports preserve sources and existing destinations on failure."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_lab.controller import LabError
from test_serial_comparison_backend import make_serial


class AnalysisFigureServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.key = 'temperature_pressure_runs/one'
        self.run = make_serial(self.root, 'one', [25, 30, 35])
        self.code = self.root / 'sspp_desktop/publication.py'
        self.code.parent.mkdir(); self.code.write_text('# renderer version 1\n')
        self.service = DesktopService(Settings(root=self.root))
        self.report = self.service.compare_runs([self.key])
        self.chart_id = 'temperature_calibration_S6'
        self.target = self.root / 'S6.pdf'

    def render(self, args, **kwargs):
        self.assertEqual(args[args.index('--chart-id') + 1], self.chart_id)
        target = Path(args[args.index('--output') + 1])
        self.assertNotEqual(target, self.target)
        target.write_bytes(b'offline figure bytes')
        return SimpleNamespace(returncode=0, stderr='')

    def test_single_current_chart_exports_all_formats_with_digest(self):
        for extension in ('.pdf', '.svg', '.png'):
            self.target = self.root / ('S6' + extension)
            self.target.write_bytes(b'previous figure')
            with patch('sspp_desktop.comparison_service.subprocess.run', side_effect=self.render):
                result = self.service.export_analysis_figure(self.report, self.chart_id, self.target)
            self.assertEqual(result['path'], str(self.target))
            self.assertEqual(result['bytes'], self.target.stat().st_size)
            self.assertEqual(result['sha256'], hashlib.sha256(self.target.read_bytes()).hexdigest())
            self.assertFalse(self.service.exporting)

    def test_unknown_chart_invalid_extension_data_target_and_symlink_are_rejected(self):
        with patch('sspp_desktop.comparison_service.subprocess.run') as renderer:
            for ident, target in (('unknown', self.target), (self.chart_id, self.root / 'plot.txt'),
                                  (self.chart_id, self.run / 'plot.pdf')):
                with self.subTest(ident=ident, target=target), self.assertRaises(LabError):
                    self.service.export_analysis_figure(self.report, ident, target)
            linked = self.root / 'linked.pdf'; linked.symlink_to(self.run / 'samples.csv')
            with self.assertRaisesRegex(LabError, '符号链接'):
                self.service.export_analysis_figure(self.report, self.chart_id, linked)
            renderer.assert_not_called()

    def assert_failed_preserves_target(self, renderer, message):
        self.target.write_bytes(b'previous figure')
        with patch('sspp_desktop.comparison_service.subprocess.run', side_effect=renderer):
            with self.assertRaisesRegex(LabError, message):
                self.service.export_analysis_figure(self.report, self.chart_id, self.target)
        self.assertEqual(self.target.read_bytes(), b'previous figure')
        self.assertEqual(list(self.root.glob('.*.tmp')), [])
        self.assertFalse(self.service.exporting)

    def test_renderer_failure_or_missing_output_preserves_target(self):
        self.assert_failed_preserves_target(lambda *a, **k: SimpleNamespace(returncode=1, stderr='failed'), '生成失败')
        self.assert_failed_preserves_target(lambda *a, **k: SimpleNamespace(returncode=0, stderr=''), '生成不完整')

    def test_source_changed_before_or_during_render_is_rejected(self):
        source = self.run / 'samples.csv'; original = source.read_bytes()
        source.write_bytes(original + b' ')
        with patch('sspp_desktop.comparison_service.subprocess.run') as renderer:
            self.assert_failed_preserves_target(renderer, '比较后已改变')
            renderer.assert_not_called()
        source.write_bytes(original)
        def change_source(args, **kwargs):
            result = self.render(args, **kwargs)
            source.write_bytes(original + b' ')
            return result
        self.assert_failed_preserves_target(change_source, '比较后已改变')

    def test_new_correction_ledger_or_changed_code_during_render_is_rejected(self):
        correction = self.root / 'Data/corrections' / (self.key + '.json')
        def new_correction(args, **kwargs):
            result = self.render(args, **kwargs)
            correction.parent.mkdir(parents=True); correction.write_text('{}')
            return result
        self.assert_failed_preserves_target(new_correction, '比较后已改变')
        correction.unlink()
        def changed_code(args, **kwargs):
            result = self.render(args, **kwargs)
            self.code.write_text('# renderer version 2\n')
            return result
        self.assert_failed_preserves_target(changed_code, '代码在比较后已改变')

    def test_changed_existing_correction_ledger_is_rejected(self):
        correction = self.root / 'Data/corrections' / (self.key + '.json')
        correction.parent.mkdir(parents=True)
        ledger = {'version':1, 'run_key':self.key,
                  'source_calibration_sha256':hashlib.sha256((self.run/'calibration_points.json').read_bytes()).hexdigest(),
                  'deleted_points':{}, 'history':[]}
        correction.write_text(json.dumps(ledger))
        self.report = self.service.compare_runs([self.key])
        def change_correction(args, **kwargs):
            result = self.render(args, **kwargs)
            ledger['deleted_points']['1'] = {'reason':'wrong reference', 'deleted_utc':'2026-11-02T08:00:00Z'}
            correction.write_text(json.dumps(ledger))
            return result
        self.assert_failed_preserves_target(change_correction, '比较后已改变')

    def test_running_and_modified_report_cannot_export_a_figure(self):
        self.service.job = {'status':'running'}
        with self.assertRaises(LabError):
            self.service.export_analysis_figure(self.report, self.chart_id, self.target)
        self.service.job = None
        self.report['charts'][0]['series'][0]['data'][0][1] = 0
        with self.assertRaisesRegex(LabError, '报告已更改'):
            self.service.export_analysis_figure(self.report, self.chart_id, self.target)
        self.assertFalse(self.target.exists())


if __name__ == '__main__':
    unittest.main()
