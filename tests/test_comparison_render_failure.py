"""Comparison display failures leave a recoverable UI, never a partial export."""

import sys
import unittest
from unittest.mock import patch

from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid

from sspp_desktop import window as window_module
from sspp_desktop.comparison_widget import ComparisonDialog
import test_comparison_ui as fixtures


class ComparisonRenderFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    wait = fixtures.ComparisonUiTests.wait
    item = fixtures.ComparisonUiTests.item
    check = fixtures.ComparisonUiTests.check
    prepare_comparison = fixtures.ComparisonUiTests.prepare_comparison

    def setUp(self):
        fixtures.ComparisonUiTests.setUp(self)
        self.unhandled = []
        hook = patch.object(sys, 'excepthook', side_effect=lambda *args: self.unhandled.append(args))
        hook.start()
        self.addCleanup(hook.stop)

    def tearDown(self):
        fixtures.ComparisonUiTests.tearDown(self)
        self.assertEqual(self.unhandled, [], 'UI completion errors must not escape into Qt excepthook')

    def select_keys(self):
        keys = [r['key'] for r in self.service.rows[:2]]
        for key in keys:
            self.check(key)
        return keys

    def finish(self):
        self.wait(lambda: not self.window.runner.pending)
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def assert_retry_ready(self, keys):
        self.assertFalse(self.window._comparison_busy)
        self.assertTrue(self.window.compare_button.isEnabled())
        self.assertEqual(self.window.comparison_keys, set(keys))
        self.assertIn('分析结果显示失败', self.window.notice.text())
        self.assertNotIn('正在后台', self.window.notice.text())

    def test_constructor_failure_cleans_parent_owned_dialog_and_retry_succeeds(self):
        keys, created = self.select_keys(), []
        def incomplete_constructor(parent):
            instance = ComparisonDialog(parent)
            created.append(instance)
            raise ValueError('injected construction failure')
        with patch.object(window_module, 'ComparisonDialog', side_effect=incomplete_constructor):
            self.window.compare_selected()
            self.finish()
        self.assert_retry_ready(keys)
        self.assertIsNone(self.window.comparison_dialog)
        self.assertEqual(len(created), 1)
        self.assertFalse(isValid(created[0]))
        self.window.compare_selected()
        self.finish()
        self.assertTrue(self.window.comparison_dialog.isVisible())
        self.assertIn('组合分析已完成', self.window.notice.text())

    def test_initial_partial_render_is_discarded_and_never_exportable(self):
        keys, created = self.select_keys(), []
        def broken_apply(view, report):
            created.append(view)
            view.report = report
            view.summary.setText('partially rendered')
            raise AttributeError('injected table rendering failure')
        with patch.object(ComparisonDialog, 'apply_report', new=broken_apply):
            self.window.compare_selected()
            self.finish()
        self.assert_retry_ready(keys)
        self.assertIsNone(self.window.comparison_dialog)
        self.assertFalse(isValid(created[0]))
        self.assertIsNone(created[0].report)
        self.assertFalse(self.service.exports or self.service.figure_exports or self.service.publication_exports)

    def test_show_and_raise_failures_are_reported_and_cleaned(self):
        keys = self.select_keys()
        for method in ('show', 'raise_'):
            with self.subTest(method=method), patch.object(ComparisonDialog, method, side_effect=RuntimeError('injected '+method)):
                self.window.compare_selected()
                self.finish()
            self.assert_retry_ready(keys)
            self.assertIsNone(self.window.comparison_dialog)
            self.assertFalse(self.window.findChildren(ComparisonDialog))
        self.window.compare_selected()
        self.finish()
        self.assertTrue(self.window.comparison_dialog.isVisible())

    def test_existing_report_and_export_directory_are_restored_after_partial_render(self):
        keys, dialog = self.prepare_comparison()
        previous = dialog.report
        dialog.set_publication_directory('/private/tmp/previous-comparison-figures')
        dialog.tabs.setCurrentIndex(2)
        original = ComparisonDialog.apply_report
        def fail_new_report(view, report):
            if report is not previous:
                view.report = report
                view.summary.setText('partially replaced')
                raise ValueError('injected new-reference render failure')
            original(view, report)
        with patch.object(ComparisonDialog, 'apply_report', new=fail_new_report):
            dialog.reference.setCurrentIndex(dialog.reference.findData(keys[0]))
            self.finish()
        self.assert_retry_ready(keys)
        self.assertIs(self.window.comparison_dialog, dialog)
        self.assertIs(dialog.report, previous)
        self.assertEqual(dialog.reference.currentData(), previous['reference_key'])
        self.assertEqual(dialog.publication_directory, '/private/tmp/previous-comparison-figures')
        self.assertEqual(dialog.tabs.currentIndex(), 2)
        self.assertEqual(dialog.plot.model, previous['charts'][0])
        self.assertIn('已恢复上一次完整结果', dialog.status.text())
        self.assertNotIn('partially', dialog.summary.text())
        self.assertTrue(dialog.export_button.isEnabled())
        dialog.figure_export_button.click()
        self.finish()
        self.assertIs(self.service.publication_exports[-1][0], previous)
        # The same visible dialog can still recompute successfully after recovery.
        dialog.reference.setCurrentIndex(dialog.reference.findData(keys[0]))
        self.finish()
        self.assertEqual(dialog.report['reference_key'], keys[0])
        self.assertIsNot(dialog.report, previous)

    def test_failed_restore_closes_dialog_and_allows_clean_retry(self):
        keys, dialog = self.prepare_comparison()
        def never_render(view, report):
            view.report = report
            raise ValueError('injected render and rollback failure')
        with patch.object(ComparisonDialog, 'apply_report', new=never_render):
            dialog.reference.setCurrentIndex(dialog.reference.findData(keys[0]))
            self.finish()
        self.assert_retry_ready(keys)
        self.assertIn('已关闭结果窗口', self.window.notice.text())
        self.assertIsNone(self.window.comparison_dialog)
        self.assertIsNone(dialog.report)
        self.assertFalse(isValid(dialog))
        self.window.compare_selected()
        self.finish()
        self.assertTrue(self.window.comparison_dialog.isVisible())

    def test_failed_new_window_keeps_previous_complete_window(self):
        keys, previous_dialog = self.prepare_comparison()
        previous_report = previous_dialog.report
        original = ComparisonDialog.apply_report
        def fail_only_new_window(view, report):
            if view is not previous_dialog:
                view.report = report
                raise RuntimeError('injected replacement failure')
            original(view, report)
        with patch.object(ComparisonDialog, 'apply_report', new=fail_only_new_window):
            self.window.compare_selected()
            self.finish()
        self.assert_retry_ready(keys)
        self.assertIs(self.window.comparison_dialog, previous_dialog)
        self.assertIs(previous_dialog.report, previous_report)
        self.assertTrue(previous_dialog.isVisible())
        self.assertTrue(previous_dialog.export_button.isEnabled())
        self.assertEqual(self.window.findChildren(ComparisonDialog), [previous_dialog])

    def test_successful_replacement_commits_new_window_before_closing_old_one(self):
        keys, previous_dialog = self.prepare_comparison()
        generation = self.window._comparison_generation
        self.window.compare_selected()
        self.finish()
        current = self.window.comparison_dialog
        self.assertIsNot(current, previous_dialog)
        self.assertFalse(isValid(previous_dialog))
        self.assertTrue(current.isVisible())
        self.assertEqual(current.report['keys'], keys)
        self.assertTrue(current.export_button.isEnabled())
        self.assertEqual(self.window._comparison_generation, generation+1)
        self.assertIn('组合分析已完成', self.window.notice.text())

    def test_cleanup_exception_does_not_hide_original_error_or_leave_busy(self):
        keys = self.select_keys()
        with patch.object(ComparisonDialog, 'apply_report', side_effect=ValueError('original rendering failure')), \
             patch.object(ComparisonDialog, 'close', side_effect=ValueError('cleanup failed')):
            self.window.compare_selected()
            self.finish()
        self.assert_retry_ready(keys)
        self.assertIn('original rendering failure', self.window.notice.text())
        self.assertIsNone(self.window.comparison_dialog)
        self.assertFalse(self.window.findChildren(ComparisonDialog))

    def test_closed_dialog_late_completion_is_never_rendered_or_reopened(self):
        keys, dialog = self.prepare_comparison()
        self.service.compare_release.clear()
        with patch.object(ComparisonDialog, 'apply_report', side_effect=AssertionError('must not render late data')) as apply:
            dialog.reference.setCurrentIndex(dialog.reference.findData(keys[0]))
            self.wait(lambda: len(self.service.comparisons) == 2)
            dialog.close()
            self.service.compare_release.set()
            self.finish()
        apply.assert_not_called()
        self.assertIsNone(self.window.comparison_dialog)
        self.assertFalse(self.window._comparison_busy)
        self.assertTrue(self.window.compare_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
