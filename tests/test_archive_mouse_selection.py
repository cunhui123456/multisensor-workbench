"""Real mouse regressions: archive checkboxes are independent of detail navigation."""
import tempfile
import threading
from pathlib import Path
import unittest

from PySide6 import QtCore, QtTest, QtWidgets

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
import test_experiment_navigation as navigation
from test_serial_comparison_backend import make_serial


class ArchiveMouseSelectionTests(unittest.TestCase):
    setUpClass = navigation.ExperimentNavigationTests.__dict__['setUpClass']
    pump = navigation.ExperimentNavigationTests.pump
    select = navigation.ExperimentNavigationTests.select

    def setUp(self):
        navigation.ExperimentNavigationTests.setUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.details_release = threading.Event()
        self.details_release.set()
        root = Path(self.temp.name)
        for name, references in (('cal_a', [30, 35]), ('cal_b', [40, 45]),
                                 ('serial_a', None), ('serial_b', None)):
            make_serial(root, name, references)
        self.archive = DesktopService(Settings(root=root))
        self.service.rows.extend(self.archive.history())
        self.details_started = threading.Event()

        def details(key):
            self.details_started.set()
            if not self.details_release.wait(4):
                raise RuntimeError('offline detail callback not released')
            return self.archive.details(key)

        self.service.details = details
        self.service.compare_runs = self.archive.compare_runs
        self.open_archive('temperature_calibration')

    def tearDown(self):
        self.details_release.set()
        navigation.ExperimentNavigationTests.tearDown(self)
        self.temp.cleanup()

    def open_archive(self, experiment):
        self.select(experiment)
        self.window.open_archive()
        self.pump(lambda: not self.window.runner.pending)
        self.assertEqual(self.window.history_table.rowCount(), 2)

    def click_cell(self, row, column=2, checkbox=False):
        table = self.window.history_table
        item = table.item(row, column)
        table.scrollToItem(item)
        self.app.processEvents()
        rect = table.visualItemRect(item)
        if checkbox:
            option = QtWidgets.QStyleOptionViewItem()
            option.initFrom(table)
            option.rect = rect
            option.features |= QtWidgets.QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
            option.checkState = item.checkState()
            rect = table.style().subElementRect(QtWidgets.QStyle.SubElement.SE_ItemViewItemCheckIndicator,
                                               option, table)
        self.assertTrue(rect.isValid())
        QtTest.QTest.mouseClick(table.viewport(), QtCore.Qt.MouseButton.LeftButton,
                               pos=rect.center())
        self.app.processEvents()

    def assert_archive_visible(self):
        self.assertIs(self.window.tabs.currentWidget(), self.window.history_table.parentWidget())

    def test_actual_checkbox_clicks_keep_archive_open_and_combine_both_serial_types(self):
        for experiment in ('temperature_calibration', 'temperature'):
            with self.subTest(experiment=experiment):
                self.open_archive(experiment)
                keys = {row['key'] for row in self.window.history_rows}
                for row in range(2):
                    self.click_cell(row, 0, checkbox=True)
                    self.pump(lambda: not self.window.runner.pending)
                    self.assertEqual(self.window.history_table.item(row, 0).checkState(),
                                     QtCore.Qt.CheckState.Checked)
                    self.assert_archive_visible()
                self.assertEqual(self.window.comparison_keys, keys)
                # Native checkbox presses may leave no row highlighted; they must
                # not turn checkbox membership into multiple highlighted rows.
                self.assertLessEqual(len(self.window.history_table.selectionModel().selectedRows()), 1)
                self.assertEqual(self.window.history_table.selectionMode(),
                                 QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
                self.assertTrue(self.window.compare_button.isEnabled())
                QtTest.QTest.mouseClick(self.window.compare_button, QtCore.Qt.MouseButton.LeftButton)
                self.pump(lambda: self.window.comparison_dialog is not None and not self.window.runner.pending)
                self.assertEqual(set(self.window.comparison_dialog.report['keys']), keys)
                self.assert_archive_visible()
                self.window.comparison_dialog.close()
                self.pump(lambda: self.window.comparison_dialog is None)

    def test_actual_single_checkbox_and_analyze_click_use_real_service_without_auto_navigation(self):
        for experiment in ('temperature_calibration','temperature'):
            with self.subTest(experiment=experiment):
                self.open_archive(experiment)
                key=self.window.history_rows[0]['key']
                self.assertFalse(self.window.compare_button.isEnabled())
                self.click_cell(0,0,checkbox=True)
                self.pump(lambda: not self.window.runner.pending)
                self.assertEqual(self.window.comparison_keys,{key})
                self.assertTrue(self.window.compare_button.isEnabled())
                self.assertEqual(self.window.compare_button.text(),'分析所选记录')
                self.assert_archive_visible()
                # Highlighting another row must not redirect the single-record analysis.
                self.click_cell(1)
                self.pump(lambda: not self.window.runner.pending)
                self.assertNotEqual(self.window.selected_details['key'],key)
                self.assertEqual(self.window.comparison_keys,{key})
                self.assert_archive_visible()
                QtTest.QTest.mouseClick(self.window.compare_button,QtCore.Qt.MouseButton.LeftButton)
                self.pump(lambda: self.window.comparison_dialog is not None and not self.window.runner.pending)
                report=self.window.comparison_dialog.report
                self.assertEqual(report['keys'],[key])
                self.assertEqual(report['analysis_mode'],'single')
                self.assertEqual(len(report['sources']),1)
                self.assertEqual(report['kind'],'temperature_calibration' if experiment=='temperature_calibration' else 'temperature_pressure')
                self.assert_archive_visible()
                self.window.comparison_dialog.close()
                self.pump(lambda: self.window.comparison_dialog is None)

    def test_actual_row_click_keeps_checks_and_requires_explicit_view_button(self):
        self.click_cell(0, 0, checkbox=True)
        self.pump(lambda: not self.window.runner.pending)
        checked = set(self.window.comparison_keys)
        self.click_cell(1)
        self.pump(lambda: not self.window.runner.pending)
        self.assert_archive_visible()
        self.assertEqual(self.window.comparison_keys, checked)
        self.assertEqual(self.window.selected_details['key'], self.window.history_rows[1]['key'])
        self.assertEqual(len(self.window.history_table.selectionModel().selectedRows()), 1)
        self.assertTrue(self.window.view_history_button.isEnabled())
        QtTest.QTest.mouseClick(self.window.view_history_button, QtCore.Qt.MouseButton.LeftButton)
        self.assertEqual(self.window.tabs.currentIndex(), self.window.serial_archive_tab)
        self.assertEqual(self.window.comparison_keys, checked)

    def test_late_detail_callback_does_not_leave_user_selected_page(self):
        self.details_release.clear()
        self.click_cell(0)
        self.pump(self.details_started.is_set)
        self.assert_archive_visible()
        self.assertFalse(self.window.view_history_button.isEnabled())
        # Move to another visible page while the detail read is still pending.
        tab_bar = self.window.tabs.tabBar()
        target = self.window.temperature_tab
        self.assertTrue(self.window.tabs.isTabVisible(target))
        QtTest.QTest.mouseClick(tab_bar, QtCore.Qt.MouseButton.LeftButton,
                               pos=tab_bar.tabRect(target).center())
        self.assertEqual(self.window.tabs.currentIndex(), target)
        self.details_release.set()
        self.pump(lambda: not self.window.runner.pending and self.window.selected_details is not None)
        self.assertEqual(self.window.tabs.currentIndex(), target)
        self.assertFalse(self.window.comparison_keys)


if __name__ == '__main__':
    unittest.main()
