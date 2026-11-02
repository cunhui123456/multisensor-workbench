"""Offline publication export and dialog contracts; no data mutation or devices."""
from copy import deepcopy
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from sspp_desktop.publication import (PUBLICATION_FILES, CALIBRATION_COLOR, build_temperature_figure,
                                      export_temperature_publication, publication_caption)
from sspp_desktop.comparison_export import export_figures


def report_fixture():
    keys = ["temperature_pressure_runs/first", "temperature_pressure_runs/second"]
    charts = []
    for channel, offset in (("S6", 0), ("S7", -.7)):
        charts.append({"id": f"temperature_calibration_{channel}", "kind": "errorbar",
                       "title": f"{channel} 温标响应", "xlabel": "参考温度 (°C)", "ylabel": "电压 (V)",
                       "publication": {"channel": channel, "kind": "calibration_response", "combined_sources": True},
                       "series": [{"key": None, "run_index": 1, "label": "Combined measurements",
             "data": [[30, 3.77 + offset], [35, 3.68 + offset], [40, 3.56 + offset],
                      [40, 3.58 + offset], [70, 3.32 + offset], [90, 3.31 + offset]],
             "errors": [.006, .005, .008, .141, .009, .009],
             "point_sources": [{"key": key, "point_id": ident} for key, ident in
                               [(keys[1], 3), (keys[1], 2), (keys[0], 1), (keys[1], 1), (keys[0], 2), (keys[0], 3)]]}]})
    return {"kind": "temperature_calibration", "reference_key": None,
            "analysis_mode": "combined",
            "presentation_mode": "combined_temperature_response",
            "sources": [{"key": key, "label": key.split('/')[-1], "parameters": {
                "reference_temperature_sequence_c": order, "reference_direction": direction}}
                for key, order, direction in [(keys[0], [40, 70, 90], 'ascending'),
                                              (keys[1], [40, 35, 30], 'descending')]],
            "charts": charts, "warnings": ["Manual reference readings require review."],
            "metrics": [], "compatibility": [], "tables": {}}


def single_report_fixture():
    report = report_fixture()
    report['analysis_mode'] = 'single'
    report['sources'] = report['sources'][:1]
    key = report['sources'][0]['key']
    for chart in report['charts']:
        chart['publication']['combined_sources'] = False
        series = chart['series'][0]
        rows = [(point, error, source) for point, error, source in
                zip(series['data'], series['errors'], series['point_sources']) if source['key'] == key]
        series['data'], series['errors'], series['point_sources'] = map(list, zip(*rows))
        series['label'] = 'Saved measurements'
    return report


class TemperaturePublicationTests(unittest.TestCase):
    def test_single_saved_record_exports_with_accurate_caption_and_metadata(self):
        report = single_report_fixture()
        with tempfile.TemporaryDirectory() as folder:
            export_figures(report, folder)
            root = Path(folder)
            self.assertTrue(all((root/name).stat().st_size > 100 for name in PUBLICATION_FILES))
            self.assertTrue((root/'comparison_report.pdf').is_file())
            caption = (root/'caption.txt').read_text()
            self.assertIn('from one saved experimental record', caption)
            self.assertIn('本图分析一条已保存记录', caption)
            self.assertNotIn('not a single acquisition', caption)
            self.assertNotIn('Combined temperature', caption)
            self.assertNotIn('多条独立记录', caption)
            self.assertIn(report['sources'][0]['key'], caption)
            self.assertIn('40 °C: source 1, interval 1', caption)
            with Image.open(root/'publication_figure.png') as image:
                self.assertAlmostEqual(image.info['dpi'][0], 600, delta=.1)
                self.assertIn('one saved experimental record', image.info['Description'])
        fig = build_temperature_figure(report)
        self.assertTrue(all(len(ax.containers)==1 for ax in fig.axes))
        np.testing.assert_array_equal(fig.axes[0].containers[0].lines[0].get_xdata(), [40, 70, 90])
        fig.clear()

    def test_complete_single_channel_publication_and_real_zip_do_not_invent_s7(self):
        from sspp_desktop.publication import publication_files
        from sspp_desktop.config import Settings
        from sspp_desktop.services import DesktopService
        from test_serial_comparison_backend import make_serial, seal
        import zipfile
        import json
        report = single_report_fixture()
        report['charts'] = report['charts'][:1]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            export_figures(report, root)
            expected = publication_files(report)
            self.assertEqual(len(expected), 7)
            self.assertTrue(all((root/name).is_file() for name in expected))
            self.assertFalse(list(root.glob('*S7*')))
            self.assertIn('The figure shows S6.', (root/'caption.txt').read_text())
            with Image.open(root/'publication_figure.png') as image:
                self.assertAlmostEqual(image.width, 88/25.4*600, delta=1)
            run = make_serial(root, 'single', [25, 30, 35])
            points = json.loads((run/'calibration_points.json').read_text())
            for point in points['points']:
                point['channels'].pop('S7')
            (run/'calibration_points.json').write_text(json.dumps(points))
            samples = (run/'samples.csv').read_text().splitlines()
            (run/'samples.csv').write_text('\n'.join(line for line in samples if ',S7,' not in line)+'\n')
            (run/'raw_serial.bin').write_bytes(b'100,600,3.0\n200,620,3.1\n')
            manifest = json.loads((run/'manifest.json').read_text())
            manifest.update(channels=['S6'], rows=2)
            seal(run, manifest)
            service = DesktopService(Settings(root=root))
            try:
                actual = service.compare_runs(['temperature_pressure_runs/single'])
                service.export_comparison(actual, root/'single.zip')
                with zipfile.ZipFile(root/'single.zip') as archive:
                    names = archive.namelist()
                    self.assertIn('figures/publication_figure.pdf', names)
                    self.assertIn('figures/publication_S6.svg', names)
                    self.assertFalse(any('publication_S7' in name for name in names))
                    self.assertIsNone(archive.testzip())
            finally:
                service.shutdown()

    def test_single_curve_retains_duplicate_temperatures_and_each_sd_without_fit(self):
        report = report_fixture()
        before = deepcopy(report)
        fig = build_temperature_figure(report)
        self.assertAlmostEqual(fig.get_size_inches()[0] * 25.4, 180)
        self.assertEqual(len(fig.axes), 2)
        for channel, ax in zip(("S6", "S7"), fig.axes):
            self.assertEqual(len(ax.containers), 1)
            line = ax.containers[0].lines[0]
            np.testing.assert_array_equal(line.get_xdata(), [30, 35, 40, 40, 70, 90])
            self.assertEqual(line.get_color(), CALIBRATION_COLOR)
            self.assertEqual(line.get_marker(), 'o')
            self.assertIsNone(ax.get_legend())
            segments = ax.containers[0].lines[2][0].get_segments()
            self.assertAlmostEqual(segments[3][1, 1] - segments[3][0, 1], 2 * .141)
            self.assertEqual(ax.get_xlabel(), "Reference temperature (°C)")
            self.assertEqual(ax.get_ylabel(), "Voltage (V)")
            self.assertIn(channel, ax.get_title(loc="left"))
            self.assertFalse(any(line.get_visible() for line in ax.get_xgridlines()))
        self.assertEqual(report, before)
        fig.clear()

    def test_vector_and_600dpi_exports_include_source_caption(self):
        report = report_fixture()
        with tempfile.TemporaryDirectory() as folder:
            files = export_temperature_publication(report, folder)
            self.assertEqual({path.name for path in files}, set(PUBLICATION_FILES))
            self.assertTrue(all(path.stat().st_size > 100 for path in files))
            root = Path(folder)
            self.assertTrue((root/'publication_figure.pdf').read_bytes().startswith(b'%PDF'))
            svg = (root/'publication_figure.svg').read_text()
            self.assertIn('<text', svg)
            self.assertNotIn('<image', svg)
            with Image.open(root/'publication_figure.png') as png:
                self.assertAlmostEqual(png.info['dpi'][0], 600, delta=.1)
                self.assertAlmostEqual(png.width, 180 / 25.4 * 600, delta=1)
            caption = (root/'caption.txt').read_text()
            self.assertIn('not confidence intervals', caption)
            self.assertIn('No automatic calibration', caption)
            self.assertIn('manual readings', caption)
            self.assertIn('40 → 35 → 30 (decreasing)', caption)
            self.assertIn('not a single acquisition', caption)
            self.assertIn('no averaging of duplicate points', caption)
            self.assertIn('40 °C: source 1, interval 1', caption)
            self.assertIn('40 °C: source 2, interval 1', caption)
            for source in report['sources']:
                self.assertIn(source['key'], caption)

    def test_excluded_point_is_not_reintroduced_by_plot_or_caption(self):
        report = report_fixture()
        for chart in report['charts']:
            series = chart['series'][0]
            rows = [(point, sd, origin) for point, sd, origin in
                    zip(series['data'], series['errors'], series['point_sources'])
                    if origin != {'key': report['sources'][1]['key'], 'point_id': 1}]
            series['data'], series['errors'], series['point_sources'] = map(list, zip(*rows))
        report['sources'][1]['parameters']['reference_temperature_sequence_c'] = [35, 30]
        report['excluded_calibration_points'] = [{
            'run_key': report['sources'][1]['key'], 'point_id': 1, 'reference_mean_c': 40,
            'channels': ['S6', 'S7'], 'reason': 'Incorrect manual reference label',
            'deleted_utc': '2026-11-02T08:00:00+00:00'}]
        fig = build_temperature_figure(report)
        for ax in fig.axes:
            np.testing.assert_array_equal(ax.containers[0].lines[0].get_xdata(), [30, 35, 40, 70, 90])
        caption = publication_caption(report)
        self.assertIn('35 → 30 (decreasing)', caption)
        self.assertNotIn('40 → 35 → 30', caption)
        self.assertIn('Excluded reference-temperature intervals (raw samples retained)', caption)
        self.assertIn('point 1, 40 °C, channels S6, S7', caption)
        self.assertIn('Incorrect manual reference label', caption)
        fig.clear()

    def test_malformed_sd_fails_instead_of_hiding_error_bars(self):
        report = report_fixture()
        for bad in ([.001], [.001, -.01, .002, .001, .001, .001]):
            report['charts'][0]['series'][0]['errors'] = bad
            with self.subTest(errors=bad), self.assertRaises(ValueError):
                build_temperature_figure(report)

    def test_existing_rf_export_retains_report_and_comparison_png(self):
        report = {"kind": "material_sweep", "reference_key": "runs/first",
                  "sources": [{"key": "runs/first", "label": "First"}], "charts": [
            {"id": "sweep", "title": "Sweep response", "xlabel": "Frequency (GHz)", "ylabel": "Amplitude (dB)",
             "series": [{"label": "First", "data": [[5, -90], [5.3, -88]], "errors": [.1, .2]}]}]}
        with tempfile.TemporaryDirectory() as folder:
            export_figures(report, folder)
            self.assertEqual({path.name for path in Path(folder).iterdir()},
                             {"comparison_01_sweep.png", "comparison_report.pdf"})


class AnalysisFigureTests(unittest.TestCase):
    def test_selected_single_channel_has_publication_size_and_no_ui_text(self):
        from sspp_desktop.publication import build_analysis_figure, export_analysis_figure, analysis_figure_data
        report = single_report_fixture()
        report['charts'] = report['charts'][:1]  # Independent export does not require an S7 chart.
        before = deepcopy(report)
        ident = report['charts'][0]['id']
        fig = build_analysis_figure(report, ident)
        self.assertAlmostEqual(fig.get_size_inches()[0]*25.4, 88)
        self.assertEqual(fig.axes[0].get_title(), '')
        self.assertEqual(fig.axes[0].get_title(loc='left'), '')
        self.assertIsNone(fig.axes[0].get_legend())
        fig.clear()
        rows = analysis_figure_data(report, ident)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['source_key'], report['sources'][0]['key'])
        self.assertEqual(rows[0]['source_point_id'], 1)
        self.assertEqual(rows[0]['error'], .008)
        with tempfile.TemporaryDirectory() as folder:
            for suffix in ('pdf', 'svg', 'png'):
                target = export_analysis_figure(report, ident, Path(folder)/f'figure.{suffix}')
                self.assertGreater(target.stat().st_size, 100)
            svg = (Path(folder)/'figure.svg').read_text()
            self.assertIn('<text', svg)
            self.assertNotIn('<image', svg)
            self.assertTrue((Path(folder)/'figure.pdf').read_bytes().startswith(b'%PDF'))
            with Image.open(Path(folder)/'figure.png') as image:
                self.assertAlmostEqual(image.width, 88/25.4*600, delta=1)
                self.assertAlmostEqual(image.info['dpi'][0], 600, delta=.1)
                self.assertIn('one saved experimental record', image.info['Description'])
                self.assertIn(ident, image.info['Description'])
        self.assertEqual(report, before)

    def test_computed_tx_reference_is_never_described_as_measured_data(self):
        from sspp_desktop.publication import analysis_figure_caption
        report = {'sources': [{'key': 'one'}], 'charts': [{
            'id': 'tx_reference', 'source_type': 'computed_setting', 'kind': 'line',
            'note': 'Not sampled at the TX RF output.', 'series': [{'data': [[0, .1], [1, .2]]}]}]}
        caption = analysis_figure_caption(report, 'tx_reference')
        self.assertIn('computed from saved transmitter settings', caption)
        self.assertIn('not a measured TX waveform or RF output', caption)
        self.assertNotIn('Saved measured data', caption)

    def test_raw_series_keep_gaps_and_separate_time_origins(self):
        from sspp_desktop.publication import build_analysis_figure, analysis_figure_data
        report = {'sources': [{'key': 'one'}, {'key': 'two'}], 'charts': [{
            'id': 'raw', 'kind': 'line', 'xlabel': '各自采集起点后的主机实收时间（s）', 'ylabel': '电压 (V)',
            'series': [{'key': 'one', 'label': 'S6 · first', 'data': [[0, 1], [1, None], [2, 3]]},
                       {'key': 'two', 'label': 'S6 · second', 'data': [[0, 4], [1, 5]]}]}]}
        fig = build_analysis_figure(report, 'raw')
        ax = fig.axes[0]
        self.assertEqual(len(ax.lines), 2)
        self.assertTrue(np.isnan(ax.lines[0].get_ydata()[1]))
        self.assertEqual(ax.lines[1].get_xdata()[0], 0)
        self.assertEqual(ax.get_xlabel(), 'Elapsed acquisition time (s)')
        self.assertEqual(ax.get_ylabel(), 'Voltage (V)')
        rows = analysis_figure_data(report, 'raw')
        self.assertIsNone(rows[1]['y'])
        self.assertEqual(rows[3]['source_key'], 'two')
        fig.clear()

    def test_heatmap_keeps_missing_cells_and_vector_grid(self):
        from sspp_desktop.publication import build_analysis_figure, analysis_figure_data, export_analysis_figure
        report = {'sources': [{'key': 'one'}], 'charts': [{
            'id': 'waterfall', 'kind': 'heatmap', 'x': [0, 2], 'y': [10, 20],
            'z': [[1, None], [3, 4]], 'colorbar': 'Amplitude (dB)',
            'xlabel': 'Time (s)', 'ylabel': 'Frequency (Hz)'}]}
        before = deepcopy(report)
        fig = build_analysis_figure(report, 'waterfall')
        self.assertEqual(len(fig.axes), 2)
        mesh = fig.axes[0].collections[0]
        self.assertTrue(np.ma.getmaskarray(mesh.get_array()).reshape(2, 2)[0, 1])
        np.testing.assert_array_equal(mesh.cmap.get_bad(), [1, 1, 1, 1])
        self.assertEqual(fig.axes[1].get_ylabel(), 'Amplitude (dB)')
        rows = analysis_figure_data(report, 'waterfall')
        self.assertEqual(len(rows), 4)
        self.assertIsNone(rows[1]['z'])
        self.assertEqual(rows[1]['source_key'], 'one')
        with tempfile.TemporaryDirectory() as folder:
            path = export_analysis_figure(report, 'waterfall', Path(folder)/'figure.svg')
            self.assertNotIn('<image', path.read_text())
        self.assertEqual(report, before)
        fig.clear()

    def test_stage_bar_scatter_annotations_and_exact_kind_are_preserved(self):
        from sspp_desktop.publication import build_analysis_figure, analysis_figure_data
        report = {'sources': [], 'charts': [
            {'id': 'stages', 'kind': 'segments', 'xlabel': 'Time (s)', 'ylabel': 'Stage',
             'segments': [{'start': 0, 'end': 2, 'label': 'Approach', 'transition': True, 'contact': False},
                          {'start': 2, 'end': 5, 'label': 'Contact', 'transition': False, 'contact': True}]},
            {'id': 'counts', 'kind': 'bar', 'xlabel': 'Stage', 'ylabel': 'Windows',
             'xticks': ['Rest', 'Contact'], 'series': [{'data': [[0, 5], [1, 8]], 'errors': [1, 2]}]},
            {'id': 'iq', 'kind': 'scatter', 'xlabel': 'I', 'ylabel': 'Q', 'equal_aspect': True,
             'baseline_interval': [0, .2], 'horizontal': [{'y': 0}], 'vertical': [{'x': 0}],
             'series': [{'data': [[0, 1], [1, 0]]}]}]}
        fig = build_analysis_figure(report, 'stages')
        self.assertEqual([patch.get_width() for patch in fig.axes[0].patches], [2, 3])
        self.assertEqual(fig.axes[0].get_yticklabels()[1].get_text(), 'Contact · contact')
        self.assertEqual(analysis_figure_data(report, 'stages')[1]['end'], 5)
        fig.clear()
        fig = build_analysis_figure(report, 'counts')
        self.assertEqual([patch.get_height() for patch in fig.axes[0].patches], [5, 8])
        self.assertEqual([label.get_text() for label in fig.axes[0].get_xticklabels()], ['Rest', 'Contact'])
        fig.clear()
        fig = build_analysis_figure(report, 'iq')
        self.assertEqual(fig.axes[0].get_aspect(), 1)
        self.assertEqual(fig.axes[0].lines[0].get_linestyle(), 'None')
        self.assertEqual(len(fig.axes[0].lines), 3)
        self.assertEqual(len(fig.axes[0].patches), 1)
        fig.clear()
        for chart in ({'id': 'bad', 'kind': 'unknown'}, {'id': 'bad', 'series': []},
                      {'id': 'bad', 'kind': 'heatmap', 'x': [0, 1], 'y': [0], 'z': [[None, None]]}):
            with self.subTest(chart=chart), self.assertRaises(ValueError):
                build_analysis_figure({'charts': [chart]}, 'bad')
        with self.assertRaises(ValueError):
            build_analysis_figure(report, 'not-existing')

    def test_full_report_renders_heatmap_and_stages_instead_of_blank_lines(self):
        report = {'kind': 'phase_history', 'analysis_mode': 'single', 'sources': [{'key': 'one'}],
                  'charts': [{'id': 'heat', 'title': 'Measured grid', 'kind': 'heatmap',
                              'x': [0, 1], 'y': [0, 1], 'z': [[1, 2], [3, 4]],
                              'xlabel': 'Time (s)', 'ylabel': 'Frequency (Hz)'}]}
        with tempfile.TemporaryDirectory() as folder:
            export_figures(report, folder)
            self.assertTrue((Path(folder)/'comparison_report.pdf').is_file())
            self.assertTrue((Path(folder)/'comparison_01_heat.png').is_file())


class TemperatureCombinationDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6 import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_combination_hides_reference_and_keeps_report_and_export_signal(self):
        from sspp_desktop.comparison_widget import ComparisonDialog
        from qt_test_cleanup import dispose_closed_windows
        report = report_fixture()
        report['charts'].append({'id': 'raw_S6', 'title': 'S6 原始电压叠加', 'xlabel': '时间 (s)',
                                 'ylabel': '电压 (V)', 'series': [
                                     {'run_index': 1, 'label': 'Run 1', 'data': [[0, 3.5], [1, 3.4]]},
                                     {'run_index': 2, 'label': 'Run 2', 'data': [[0, 3.6], [1, 3.7]]}]})
        before = deepcopy(report)
        dialog = ComparisonDialog()
        dialog.apply_report(report)
        dialog.set_busy(False)
        dialog.show()
        self.app.processEvents()
        try:
            self.assertIn('组合分析', dialog.windowTitle())
            self.assertTrue(dialog.reference.isHidden())
            self.assertTrue(dialog.reference_label.isHidden())
            self.assertNotIn('差值', dialog.summary.text())
            self.assertTrue(dialog.warning.isHidden())
            self.assertTrue(dialog.warning_toggle.isVisible())
            dialog.warning_toggle.click()
            self.assertTrue(dialog.warning.isVisible())
            dialog.warning_toggle.click()
            self.assertTrue(dialog.warning.isHidden())
            self.assertEqual(dialog.export_button.text(), '导出论文图与数据…')
            self.assertEqual(dialog.chart_choice.count(), 3)
            self.assertEqual(dialog.plot.model['series'][0]['symbol'], 'o')
            self.assertEqual(len(dialog.plot.model['series']), 1)
            self.assertTrue(dialog.legend.isHidden())
            emitted = []
            dialog.export_requested.connect(lambda: emitted.append(True))
            dialog.export_button.click()
            self.assertEqual(emitted, [True])
            self.assertEqual(report, before)
            dialog.chart_choice.setCurrentIndex(2)
            self.assertEqual(dialog.plot.model['id'], 'raw_S6')
            self.assertEqual(len(dialog.plot.model['series']), 2)
            self.assertTrue(dialog.legend.isVisible())
            self.assertEqual(dialog.plot.model['series'][1]['data'][0][0], 0)
            dialog.set_busy(True)
            self.assertFalse(dialog.export_button.isEnabled())
        finally:
            dialog.close()
            self.app.processEvents()
            dispose_closed_windows()

    def test_current_chart_export_controls_busy_blocked_and_directory_lifecycle(self):
        from sspp_desktop.comparison_widget import ComparisonDialog
        from qt_test_cleanup import dispose_closed_windows
        dialog = ComparisonDialog()
        report = single_report_fixture()
        dialog.apply_report(report)
        dialog.set_busy(False)
        exported, saved, opened = [], [], []
        dialog.figure_export_requested.connect(exported.append)
        dialog.figure_save_as_requested.connect(saved.append)
        dialog.open_figure_directory_requested.connect(lambda: opened.append(True))
        try:
            self.assertEqual(dialog.figure_export_button.text(), '导出分析图')
            self.assertFalse(dialog.open_figure_directory_button.isEnabled())
            dialog.chart_choice.setCurrentIndex(1)
            dialog.figure_export_button.click()
            dialog.figure_save_as_button.click()
            self.assertEqual(exported, ['temperature_calibration_S7'])
            self.assertEqual(saved, exported)
            dialog.set_publication_directory('/tmp/figures')
            dialog.open_figure_directory_button.click()
            self.assertEqual(opened, [True])
            for busy, blocked in ((True, False), (False, True)):
                dialog.set_busy(busy, blocked=blocked)
                self.assertFalse(dialog.figure_export_button.isEnabled())
                self.assertFalse(dialog.figure_save_as_button.isEnabled())
                self.assertFalse(dialog.open_figure_directory_button.isEnabled())
            dialog.set_busy(False)
            self.assertTrue(dialog.open_figure_directory_button.isEnabled())
            dialog.apply_report(report)
            self.assertIsNone(dialog.publication_directory)
            self.assertFalse(dialog.open_figure_directory_button.isEnabled())
        finally:
            dialog.close()
            self.app.processEvents()
            dispose_closed_windows()

    def test_single_record_dialog_is_analysis_with_export_available(self):
        from sspp_desktop.comparison_widget import ComparisonDialog
        from qt_test_cleanup import dispose_closed_windows
        for kind in ('temperature_calibration', 'temperature_pressure', 'material_sweep'):
            with self.subTest(kind=kind):
                report = single_report_fixture()
                report['kind'] = kind
                dialog = ComparisonDialog()
                dialog.apply_report(report)
                dialog.set_busy(False)
                dialog.show()
                self.app.processEvents()
                try:
                    expected = '温标记录分析' if kind=='temperature_calibration' else '实验记录分析'
                    self.assertIn(expected, dialog.windowTitle())
                    self.assertIn('1 条', dialog.summary.text())
                    self.assertNotIn('组合', dialog.summary.text())
                    self.assertNotIn('多记录', dialog.summary.text())
                    self.assertNotIn('不是一次', dialog.summary.text())
                    self.assertEqual(dialog.tabs.tabText(0), '分析图')
                    self.assertTrue(dialog.reference.isHidden())
                    self.assertFalse(dialog.reference.isEnabled())
                    self.assertTrue(dialog.export_button.isEnabled())
                    exported = []
                    dialog.export_requested.connect(lambda: exported.append(True))
                    dialog.export_button.click()
                    self.assertEqual(exported, [True])
                finally:
                    dialog.close()
                    self.app.processEvents()
                    dispose_closed_windows()


if __name__ == '__main__':
    unittest.main()
