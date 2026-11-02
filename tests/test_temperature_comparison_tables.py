"""Real comparison reports must render both table forms without altering data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from copy import deepcopy
import unittest
from PySide6 import QtCore, QtWidgets

from sspp_experiments.comparison import build_comparison
from sspp_desktop.comparison_widget import ComparisonDialog
import test_comparison_domain as fixtures


class TemperatureComparisonTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.fixture = fixtures.ComparisonTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.dialog = ComparisonDialog()

    def tearDown(self):
        self.dialog.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def report(self, mode='manual', count=3):
        keys = [f'runs/platform_{i}' for i in range(count)]
        for i, key in enumerate(keys):
            manifest = self.fixture.sweep(key)
            self.fixture.save(key, 'desktop_context.json', {'experiment': 'temperature_sweep', 'parameters': {
                'temperature_sweep': {'mode': mode, 'condition_label': f'平台 {i}',
                                      'reference_temperature_c': 35 + i * 5, 'channel': 'S6', 'calibration': None}}})
            if mode != 'manual':
                thermal = {'status': 'complete', 'serial_workers_stopped': True,
                           'serial_closed': True, 'data_files_closed': True}
                manifest['temperature_sweep'] = thermal
                self.fixture.save(key, 'manifest.json', manifest)
                self.fixture.save(key, 'temperature/manifest.json', thermal)
                run = self.fixture.root/'Data'/key
                (run/'temperature/samples.csv').write_text('host_elapsed_s,voltage_v\n0,0.5\n')
                (run/'temperature_measurements.csv').write_text('repeat,point,voltage_v\n1,1,0.5\n')
        return build_comparison(self.fixture.root, keys)

    def conditions_table(self):
        i = next(i for i in range(self.dialog.data_tabs.count())
                 if self.dialog.data_tabs.tabText(i) == '温度条件与来源')
        return self.dialog.data_tabs.widget(i).findChild(QtWidgets.QTableWidget)

    def test_manual_three_platforms_real_report_opens_with_reference_temperatures(self):
        report = self.report()
        original = deepcopy(report)
        self.dialog.apply_report(report)
        self.dialog.set_busy(False)
        self.dialog.show()
        self.app.processEvents()
        self.assertTrue(self.dialog.isVisible())
        self.assertTrue(self.dialog.figure_export_button.isEnabled())
        table = self.conditions_table()
        self.assertEqual(table.rowCount(), 3)
        self.assertEqual(table.horizontalHeaderItem(3).text(), '人工参考温度 (°C)')
        self.assertEqual([table.item(i, 3).text() for i in range(3)], ['35', '40', '45'])
        self.assertEqual(report, original)
        self.assertIs(self.dialog.report, report)

    def test_manual_single_record_keeps_single_analysis_and_condition(self):
        report = self.report(count=1)
        self.dialog.apply_report(report)
        self.assertEqual(self.conditions_table().rowCount(), 1)
        self.assertIn('1 条', self.dialog.summary.text())
        self.assertIsNone(report['reference_key'])

    def test_serial_platform_report_keeps_both_source_evidence_and_conditions(self):
        report = self.report(mode='platform', count=2)
        original = deepcopy(report)
        self.dialog.apply_report(report)
        self.assertEqual(self.conditions_table().rowCount(), 2)
        self.assertIn('temperature/samples.csv', [s['relative_path'] for s in report['sources'][0]['files']])
        self.assertEqual(report, original)

    def test_list_columns_union_and_preview_limit_preserve_export_rows(self):
        report = self.report(count=1)
        rows = [{'key': str(i), **({'later_field': 0} if i else {})} for i in range(600)]
        report['tables'] = {'temperature_conditions': rows}
        original = deepcopy(report)
        self.dialog.apply_report(report)
        table = self.conditions_table()
        self.assertEqual(table.columnCount(), 2)
        self.assertEqual(table.rowCount(), 500)
        self.assertEqual(table.item(0, 1).text(), '—')
        self.assertEqual(table.item(1, 1).text(), '0')
        self.assertEqual(report, original)

    def test_empty_list_and_explicit_columns_positional_rows_are_supported(self):
        report = self.report(count=1)
        report['tables'] = {'temperature_conditions': [],
                            'sweep_summary': {'columns': ['key', 'count'], 'rows': [['runs/a', 3]]}}
        self.dialog.apply_report(report)
        self.assertEqual(self.conditions_table().rowCount(), 0)
        table = self.dialog.data_tabs.widget(1).findChild(QtWidgets.QTableWidget)
        self.assertEqual(table.item(0, 1).text(), '3')

    def test_malformed_table_does_not_replace_previous_report_or_display(self):
        report = self.report(count=1)
        self.dialog.apply_report(report)
        for dataset in ['bad', [3], {'columns': ['key'], 'rows': [[1, 2]]}]:
            with self.subTest(dataset=dataset):
                bad = deepcopy(report)
                bad['tables']['temperature_conditions'] = dataset
                with self.assertRaisesRegex(ValueError, '数据表'):
                    self.dialog.apply_report(bad)
                self.assertIs(self.dialog.report, report)
                self.assertEqual(self.conditions_table().item(0, 3).text(), '35')


if __name__ == '__main__':
    unittest.main()
