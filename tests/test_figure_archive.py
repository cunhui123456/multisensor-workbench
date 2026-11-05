"""Publication archives are versioned, traceable, atomic, and never alter Data."""
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sspp_desktop.config import Settings
from sspp_desktop.figure_archive import EXPERIMENT_FOLDERS
from sspp_desktop.services import DesktopService
from sspp_lab.controller import LabError
from test_serial_comparison_backend import make_serial


class FigureArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.key = 'temperature_pressure_runs/one'
        self.run = make_serial(self.root, 'one', [25, 30, 35])
        self.service = DesktopService(Settings(root=self.root))
        self.report = self.service.compare_runs([self.key])
        self.chart_id = 'temperature_calibration_S6'
        self.caption = patch('sspp_desktop.publication.analysis_figure_caption', create=True,
                             return_value='Interval voltage means; error bars are sample SD, not confidence intervals.\n')
        self.data = patch('sspp_desktop.publication.analysis_figure_data', create=True,
                          return_value=[{'series_index':0, 'label':'Measured intervals', 'point_index':0,
                                         'x':25., 'y':3.75, 'error':.01, 'source_key':self.key, 'source_point_id':1}])
        self.caption.start(); self.data.start()
        self.addCleanup(self.caption.stop); self.addCleanup(self.data.stop)

    @staticmethod
    def render(report, chart_id, extension):
        return ('offline publication ' + chart_id + extension).encode()

    def export(self):
        with patch.object(self.service, '_render_analysis_figure', side_effect=self.render):
            return self.service.export_publication_figure(self.report, self.chart_id)

    def test_default_archive_contains_three_formats_data_caption_and_source_manifest(self):
        before = {p:p.read_bytes() for p in self.run.iterdir()}
        result = self.export()
        directory = Path(result['directory'])
        self.assertEqual(directory.parent, self.root / 'Figures/temperature_calibration/one')
        self.assertEqual(set(result['files']), {'pdf', 'svg', 'png'})
        self.assertEqual(len(list(directory.iterdir())), 6)
        manifest = json.loads((directory / 'figure_manifest.json').read_text())
        self.assertEqual(manifest['schema'], 'sspp.figure_archive.v1')
        self.assertEqual(manifest['source_keys'], [self.key])
        self.assertEqual(manifest['chart_id'], self.chart_id)
        self.assertEqual(manifest['png_dpi'], 600)
        self.assertEqual(manifest['sources'], self.report['sources'])
        self.assertEqual(manifest['analysis_code'], self.report['analysis_code'])
        self.assertEqual(manifest['point_sources'][0]['points'][0]['point_id'], 1)
        self.assertIn('SD', manifest['statistical_semantics'])
        for item in manifest['files']:
            content = (directory/item['path']).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), item['sha256'])
            self.assertEqual(len(content), item['bytes'])
        with (directory/'data.csv').open(encoding='utf-8-sig') as content:
            row = list(csv.DictReader(content))[0]
        self.assertEqual(row['source_key'], self.key)
        self.assertEqual(row['source_point_id'], '1')
        self.assertEqual(before, {p:p.read_bytes() for p in before})

    def test_repeated_export_creates_new_versions_without_overwriting_old_files(self):
        first = Path(self.export()['directory'])
        before = {p.name:p.read_bytes() for p in first.iterdir()}
        second = Path(self.export()['directory'])
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, second.parent)
        self.assertEqual(before, {p.name:p.read_bytes() for p in first.iterdir()})
        self.assertTrue(all(not path.name.startswith('.') for path in first.parent.iterdir()))

    def test_source_changed_during_render_cancels_entire_new_version(self):
        old = Path(self.export()['directory'])
        old_files = {p.name:p.read_bytes() for p in old.iterdir()}
        def render_and_mutate(report, chart_id, extension):
            if extension == '.png':
                with (self.run/'samples.csv').open('a') as content: content.write('changed')
            return self.render(report, chart_id, extension)
        with patch.object(self.service, '_render_analysis_figure', side_effect=render_and_mutate):
            with self.assertRaisesRegex(LabError, '比较后已改变'):
                self.service.export_publication_figure(self.report, self.chart_id)
        self.assertEqual(list(old.parent.iterdir()), [old])
        self.assertEqual(old_files, {p.name:p.read_bytes() for p in old.iterdir()})
        self.assertFalse(self.service.exporting)

    def test_renderer_failure_leaves_no_partial_archive(self):
        with patch.object(self.service, '_render_analysis_figure', side_effect=LabError('render failed')):
            with self.assertRaisesRegex(LabError, 'render failed'):
                self.service.export_publication_figure(self.report, self.chart_id)
        parent = self.root / 'Figures/temperature_calibration/one'
        self.assertEqual(list(parent.iterdir()), [])
        self.assertFalse(self.service.exporting)

    def test_six_experiment_folders_and_combined_source_identity(self):
        self.assertEqual(set(EXPERIMENT_FOLDERS.values()), {'material_sweep', 'finger_displacement',
            'liquid_temperature', 'temperature_pressure', 'temperature_calibration', 'pressure_calibration', 'temperature_sweep'})
        for kind, folder in EXPERIMENT_FOLDERS.items():
            report = dict(self.report, kind=kind)
            parent, category = self.service._publication_parent(report)
            self.assertEqual(parent.parent.name, folder)
            self.assertEqual(category, folder)
        make_serial(self.root, 'two', [40, 45])
        second_key = 'temperature_pressure_runs/two'
        combined = self.service.compare_runs([self.key, second_key])
        reverse = self.service.compare_runs([second_key, self.key])
        first_parent, _ = self.service._publication_parent(combined)
        other_parent, _ = self.service._publication_parent(reverse)
        self.assertTrue(first_parent.name.startswith('combined_'))
        self.assertEqual(first_parent, other_parent)

    def test_linked_figures_directory_and_unknown_kind_are_rejected(self):
        (self.root/'Figures').symlink_to(self.root/'Data', target_is_directory=True)
        with self.assertRaisesRegex(LabError, '归档路径'):
            self.export()
        self.assertFalse((self.root/'Data/temperature_calibration').exists())
        (self.root/'Figures').unlink()
        report = self.service.register_analysis_report(dict(self.report, kind='unknown'))
        with self.assertRaisesRegex(LabError, '尚未登记'):
            self.service.export_publication_figure(report, self.chart_id)

    def test_registration_hashes_adapter_code_and_rejects_bad_source_binding(self):
        code = self.root/'sspp_desktop/other_adapter.py'
        code.parent.mkdir(); code.write_text('# adapter v1\n')
        original = deepcopy(self.report)
        registered = self.service.register_analysis_report(original, ['sspp_desktop/other_adapter.py'])
        self.assertEqual(original, self.report)
        self.assertNotEqual(registered['comparison_id'], original['comparison_id'])
        self.assertIn('sspp_desktop/other_adapter.py', [entry['path'] for entry in registered['analysis_code']])
        code.write_text('# adapter v2\n')
        with self.assertRaisesRegex(LabError, '代码在比较后已改变'):
            self.service.export_publication_figure(registered, self.chart_id)
        bad = deepcopy(self.report); bad['sources'][0]['files'][0]['sha256'] = '0'*64
        with self.assertRaisesRegex(LabError, '比较后已改变'):
            self.service.register_analysis_report(bad)
        empty = deepcopy(self.report); empty['sources'][0]['files'] = []
        with self.assertRaisesRegex(LabError, '文件哈希'):
            self.service.register_analysis_report(empty)


if __name__ == '__main__':
    unittest.main()
