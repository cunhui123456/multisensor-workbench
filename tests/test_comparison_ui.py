"""离线核查比较勾选、筛选、异步来源绑定、导出选择及 Qt 清理。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import copy
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import PropertyMock, patch

from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid
from sspp_desktop.window import MainWindow
from sspp_desktop.temperature_widget import TemperaturePressureWidget
from test_desktop_ui import FakeService
from qt_test_cleanup import dispose_closed_windows


def comparison_report(keys, reference=None, kind="material_sweep"):
    return {"keys": keys, "reference_key": reference or keys[-1], "kind": kind,
            "analysis_mode": "single" if len(keys) == 1 else "combined",
            "sources": [{"key": key, "label": key.split("/")[-1], "parameters": {}} for key in keys],
            "quantitative_difference_allowed": kind == "material_sweep" and len(keys) > 1,
            "charts": [{"id": "sweep_mean", "kind": "line", "title": "平均幅度", "xlabel": "GHz", "ylabel": "dB",
                        "series": [{"label": key, "data": [[2.4, -40], [2.5, -38]]} for key in keys]}],
            "metrics": [{"key": key, "repeat_count": 2} for key in keys],
            "compatibility": [{"parameter": "rate", "label": "采样率", "values": [250000] * len(keys), "status": "same", "note": ""}],
            "warnings": ["离线测试来源"], "tables": {"均值": {"columns": ["key", "mean"], "rows": [{"key": key, "mean": -39} for key in keys]}}}


class ComparisonService(FakeService):
    def __init__(self):
        super().__init__()
        self.rows = [{"key": f"local_runs/material_{n}", "label": f"样品 {n}", "mode": "sweep", "category": "material_sweep",
                      "started_utc": f"2026-10-10T00:00:{n:02d}+00:00", "status": "complete", "sensor": "SSPP A"} for n in range(9)]
        self.rows += [{"key": f"phase_runs/{kind}", "label": kind, "mode": "phase", "category": kind,
                       "started_utc": "2026-10-09T00:00:00Z", "status": "stopped", "sensor": "SSPP B"}
                      for kind in ("finger_displacement", "phase_history")]
        self.rows += [{"key": "teacher_runs/one", "label": "老师记录", "mode": "teacher", "category": "teacher_sweep", "status": "failed", "sensor": ""}]
        self.comparisons = []
        self.exports = []
        self.compare_release = threading.Event()
        self.compare_release.set()
        self.details_release = threading.Event()
        self.details_release.set()
        self.fail_reference = False
        self.export_release = threading.Event()
        self.export_release.set()
        self.figure_exports = []
        self.publication_exports = []
        self.publication_analyses = []
        self.figure_release = threading.Event()
        self.figure_release.set()
        self.fail_figure = False

    def history(self):
        return copy.deepcopy(self.rows)

    def details(self, key, *, include_curve=True):
        self.details_release.wait(2)
        return {"key": key, "metadata": {"key": key}, "curve_loaded": include_curve, "curve": {"mode": "sweep", "points": []}, "files": []}

    def compare_runs(self, keys, reference_key=None):
        self.comparisons.append((list(keys), reference_key, threading.get_ident()))
        self.compare_release.wait(3)
        if self.fail_reference and reference_key:
            raise ValueError("测试参考重算失败")
        kind = next(row["category"] for row in self.rows if row["key"] == keys[0])
        return comparison_report(list(keys), reference_key, kind)

    def export_comparison(self, report, destination):
        self.exports.append((report, destination, threading.get_ident()))
        self.export_release.wait(3)
        return {"path": destination, "count": 5}

    def export_analysis_figure(self, report, chart_id, destination):
        self.figure_exports.append((report,chart_id,destination,threading.get_ident()))
        self.figure_release.wait(3)
        if self.fail_figure:
            raise ValueError('测试图导出失败')
        return {'path':destination,'warnings':[]}

    def export_publication_figure(self, report, chart_id):
        self.publication_exports.append((report,chart_id,threading.get_ident()))
        self.figure_release.wait(3)
        if self.fail_figure:
            raise ValueError('测试论文图导出失败')
        return {'directory':'/private/tmp/sspp_publication_ui_result','warnings':[]}

    def publication_analysis(self, key):
        self.publication_analyses.append((key,threading.get_ident()))
        report=comparison_report([key])
        report['charts'][0]['id']='diagnostic_second'
        return report


class ComparisonUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.service = ComparisonService()
        self.window = MainWindow(self.service)
        self.window.open_archive()
        self.window.show()
        self.wait(lambda: bool(self.window.history_rows) and not self.window.runner.pending)

    def tearDown(self):
        self.service.compare_release.set()
        self.service.details_release.set()
        self.service.export_release.set()
        self.service.figure_release.set()
        self.service.stop()
        self.window._apply_status(self.service.snapshot())
        self.wait(lambda: not self.window.runner.pending)
        self.window.close()
        self.wait(lambda: not self.window.isVisible())
        dispose_closed_windows()

    def wait(self, predicate, timeout=4):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Qt condition timed out")

    def item(self, key):
        return next(self.window.history_table.item(n, 0) for n in range(self.window.history_table.rowCount())
                    if self.window.history_table.item(n, 0).data(QtCore.Qt.ItemDataRole.UserRole) == key)

    def check(self, key, checked=True):
        self.item(key).setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)

    def prepare_comparison(self):
        keys = [row["key"] for row in self.service.rows[:2]]
        for key in keys:
            self.check(key)
        self.window.compare_selected()
        self.wait(lambda: self.window.comparison_dialog is not None and not self.window.runner.pending)
        return keys, self.window.comparison_dialog

    def test_zero_one_two_and_back_to_one_update_action_and_count(self):
        first,second=[row['key'] for row in self.service.rows[:2]]
        self.assertFalse(self.window.compare_button.isEnabled())
        self.assertEqual(self.window.compare_button.text(),'分析所选记录')
        self.window.compare_selected()
        self.assertFalse(self.service.comparisons)
        self.assertIn('1–8',self.window.notice.text())
        self.check(first)
        self.assertTrue(self.window.compare_button.isEnabled())
        self.assertEqual(self.window.compare_button.text(),'分析所选记录')
        self.assertIn('已勾选 1',self.window.comparison_count.text())
        self.assertNotIn('至少 2',self.window.comparison_count.text())
        self.assertIn('1–8',self.item(first).toolTip())
        self.check(second)
        self.assertTrue(self.window.compare_button.isEnabled())
        self.assertEqual(self.window.compare_button.text(),'组合分析所选记录')
        self.check(second,False)
        self.assertTrue(self.window.compare_button.isEnabled())
        self.assertEqual(self.window.compare_button.text(),'分析所选记录')
        self.check(first,False)
        self.assertFalse(self.window.compare_button.isEnabled())

    def test_save_as_exports_only_current_chart_in_selected_format_on_worker_thread(self):
        keys,dialog=self.prepare_comparison()
        extra=copy.deepcopy(dialog.report['charts'][0]);extra.update(id='second_chart',title='第二张图')
        dialog.report['charts'].append(extra)
        dialog.apply_report(dialog.report)
        dialog.chart_choice.setCurrentIndex(1)
        with patch.object(QtWidgets.QFileDialog,'getSaveFileName',return_value=('', '')):
            dialog.figure_save_as_requested.emit('second_chart')
        self.assertFalse(self.service.figure_exports)
        self.assertFalse(self.service.publication_exports)
        cases=[('PDF 矢量图 (*.pdf)','.pdf'),('SVG 矢量图 (*.svg)','.svg'),('PNG 位图（600 dpi） (*.png)','.png')]
        for selected_filter,suffix in cases:
            with self.subTest(format=suffix),patch.object(QtWidgets.QFileDialog,'getSaveFileName',
                    return_value=('/private/tmp/sspp_figure_ui_result',selected_filter)) as picker:
                dialog.figure_save_as_requested.emit('second_chart')
                self.wait(lambda: not self.window.runner.pending)
                self.assertTrue(picker.call_args.args[2].endswith('.pdf'))
                self.assertEqual(picker.call_args.args[4],'PDF 矢量图 (*.pdf)')
                call=self.service.figure_exports[-1]
                self.assertIs(call[0],dialog.report)
                self.assertEqual(call[1],'second_chart')
                self.assertEqual(call[2],'/private/tmp/sspp_figure_ui_result'+suffix)
                self.assertNotEqual(call[3],threading.get_ident())
        with patch.object(QtWidgets.QFileDialog,'getSaveFileName',return_value=('/private/tmp/explicit.SVG',cases[0][0])):
            self.window._export_analysis_figure(dialog,'second_chart')
            self.wait(lambda: not self.window.runner.pending)
        self.assertEqual(self.service.figure_exports[-1][2],'/private/tmp/explicit.SVG')
        previous=len(self.service.figure_exports)
        with patch.object(QtWidgets.QFileDialog,'getSaveFileName',return_value=('/private/tmp/invalid.exe',cases[0][0])):
            self.window._export_analysis_figure(dialog,'second_chart')
        self.assertEqual(len(self.service.figure_exports),previous)
        self.assertIn('PDF、SVG 或 PNG',self.window.notice.text())
        self.assertFalse(self.service.exports, 'single figure export must not invoke ZIP export')

    def test_default_publication_export_needs_no_picker_and_opens_its_directory(self):
        keys,dialog=self.prepare_comparison()
        self.assertIsNone(dialog.publication_directory)
        with patch.object(QtWidgets.QFileDialog,'getSaveFileName') as picker:
            dialog.figure_export_button.click()
            self.wait(lambda: not self.window.runner.pending)
            picker.assert_not_called()
        self.assertEqual(len(self.service.publication_exports),1)
        report,chart_id,thread=self.service.publication_exports[0]
        self.assertIs(report,dialog.report)
        self.assertEqual(chart_id,'sweep_mean')
        self.assertNotEqual(thread,threading.get_ident())
        self.assertEqual(dialog.publication_directory,'/private/tmp/sspp_publication_ui_result')
        with patch.object(QtGui.QDesktopServices,'openUrl',return_value=True) as opened:
            dialog.open_figure_directory_requested.emit()
            self.assertEqual(opened.call_args.args[0].toLocalFile(),dialog.publication_directory)
        self.assertFalse(self.service.figure_exports or self.service.exports)

    def test_figure_export_interlocks_navigation_capture_and_selection_then_recovers(self):
        keys,dialog=self.prepare_comparison()
        self.window.initialize_environment()
        self.wait(lambda: not self.window.runner.pending)
        self.assertTrue(self.window.start_button.isEnabled())
        self.service.figure_release.clear()
        dialog.figure_export_button.click()
        self.wait(lambda: bool(self.service.publication_exports))
        self.assertIn('figure_export',self.window.runner.pending)
        self.assertFalse(dialog.figure_export_button.isEnabled())
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertFalse(dialog.reference.isEnabled())
        self.assertFalse(self.window.compare_button.isEnabled())
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertFalse(self.window.experiment_nav.isEnabled())
        self.assertTrue(self.window.temperature_workspace._external_busy)
        self.assertFalse(bool(self.item(keys[0]).flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable))
        self.check(keys[0],False)
        self.assertEqual(self.window.comparison_keys,set(keys))
        with patch.object(QtWidgets.QFileDialog,'getSaveFileName') as picker:
            self.window._export_comparison(dialog)
            self.window._export_analysis_figure(dialog,'sweep_mean')
            picker.assert_not_called()
        self.service.figure_release.set()
        self.wait(lambda: not self.window.runner.pending)
        self.assertTrue(dialog.figure_export_button.isEnabled())
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertTrue(self.window.experiment_nav.isEnabled())
        self.assertFalse(self.window.temperature_workspace._external_busy)

    def test_figure_export_failure_queue_rejection_and_closed_callback_are_safe(self):
        keys,dialog=self.prepare_comparison()
        self.service.fail_figure=True
        dialog.figure_export_button.click()
        self.wait(lambda: not self.window.runner.pending)
        self.assertIn('失败',dialog.status.text())
        self.assertTrue(dialog.figure_export_button.isEnabled())
        self.assertTrue(self.window.compare_button.isEnabled())
        self.assertIsNone(dialog.publication_directory)
        previous=len(self.service.publication_exports)
        with patch.object(self.window,'_submit',return_value=False):
            self.window._export_publication_figure(dialog,'sweep_mean')
        self.assertEqual(len(self.service.publication_exports),previous)
        self.assertTrue(dialog.figure_export_button.isEnabled())
        self.service.fail_figure=False
        self.service.figure_release.clear()
        dialog.figure_export_button.click()
        self.wait(lambda: len(self.service.publication_exports)>previous)
        dialog.close()
        self.service.figure_release.set()
        self.wait(lambda: not self.window.runner.pending)
        QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)
        self.assertIsNone(self.window.comparison_dialog)
        self.assertFalse(isValid(dialog))

    def test_liquid_background_work_blocks_serial_capture_and_recovers(self):
        release=threading.Event()
        started=threading.Event()
        key=self.service.rows[0]['key']
        self.check(key)
        chart=copy.deepcopy(comparison_report([key])['charts'][0])
        self.window.diagnostic_model={'key':key,'groups':{self.window.plot_group.currentData():[chart]}}
        self.window._select_plot_group()
        def operation():
            started.set()
            release.wait(3)
        try:
            self.assertFalse(self.window.temperature_workspace._external_busy)
            self.assertTrue(self.window.liquid_workspace._submit('figure_export',operation,lambda result:None))
            self.wait(started.is_set)
            self.assertTrue(self.window.temperature_workspace._external_busy)
            self.assertFalse(self.window.temperature_workspace.start_button.isEnabled())
            self.assertFalse(self.window.experiment_nav.isEnabled())
            self.assertFalse(self.window.compare_button.isEnabled())
            self.assertFalse(self.window.diagnostic_figure_export_button.isEnabled())
            self.window._export_diagnostic_figure()
            self.assertFalse(self.service.publication_analyses)
        finally:
            release.set()
            self.wait(lambda:not self.window.liquid_workspace.is_busy)
        self.assertFalse(self.window.temperature_workspace._external_busy)
        self.assertTrue(self.window.experiment_nav.isEnabled())
        self.assertTrue(self.window.compare_button.isEnabled())
        self.assertTrue(self.window.diagnostic_figure_export_button.isEnabled())

    def test_initialization_blocks_analysis_and_figure_exports(self):
        keys,dialog=self.prepare_comparison()
        self.window._environment_pending=True
        try:
            self.window._set_active(False)
            self.assertFalse(self.window.compare_button.isEnabled())
            self.assertFalse(dialog.figure_export_button.isEnabled())
            self.window._export_publication_figure(dialog,'sweep_mean')
            with patch.object(QtWidgets.QFileDialog,'getSaveFileName') as picker:
                self.window._export_analysis_figure(dialog,'sweep_mean')
                picker.assert_not_called()
            self.assertFalse(self.service.publication_exports or self.service.figure_exports)
        finally:
            self.window._environment_pending=False
            self.window._set_active(False)
        self.assertTrue(dialog.figure_export_button.isEnabled())

    def test_save_as_cancels_when_report_changes_during_picker(self):
        keys,dialog=self.prepare_comparison()
        def changed(*args):
            dialog.apply_report(comparison_report(keys,kind='phase_history'))
            return '/private/tmp/changed.pdf','PDF 矢量图 (*.pdf)'
        with patch.object(QtWidgets.QFileDialog,'getSaveFileName',side_effect=changed):
            self.window._export_analysis_figure(dialog,'sweep_mean')
        self.assertFalse(self.service.figure_exports)
        self.assertNotIn('figure_export',self.window.runner.pending)

    def test_diagnostic_export_uses_displayed_source_and_chart_not_highlighted_record(self):
        first,second=[row['key'] for row in self.service.rows[:2]]
        self.window.history_table.selectRow(self.item(second).row())
        self.wait(lambda: self.window.selected_details is not None)
        self.assertEqual(self.window.selected_details['key'],second)
        chart=copy.deepcopy(comparison_report([first])['charts'][0])
        other=copy.deepcopy(chart);other.update(id='diagnostic_second',title='诊断第二图')
        group=self.window.plot_group.currentData()
        self.window.diagnostic_model={'key':first,'groups':{group:[chart,other]}}
        self.window._select_plot_group()
        self.window.plot_choice.setCurrentIndex(1)
        self.assertTrue(self.window.diagnostic_figure_export_button.isEnabled())
        self.assertFalse(self.window.diagnostic_figure_directory_button.isEnabled())
        current_page=self.window.tabs.currentIndex()
        self.service.figure_release.clear()
        with patch.object(QtWidgets.QFileDialog,'getSaveFileName') as picker:
            self.window.diagnostic_figure_export_button.click()
            self.wait(lambda: bool(self.service.publication_exports))
            picker.assert_not_called()
        self.assertFalse(self.window.diagnostic_figure_export_button.isEnabled())
        self.assertEqual(self.service.publication_analyses[0][0],first)
        report,chart_id,thread=self.service.publication_exports[0]
        self.assertEqual(report['keys'],[first])
        self.assertEqual(chart_id,'diagnostic_second')
        self.assertNotEqual(thread,threading.get_ident())
        self.assertEqual(thread,self.service.publication_analyses[0][1])
        self.service.figure_release.set()
        self.wait(lambda: not self.window.runner.pending)
        self.assertEqual(self.window.tabs.currentIndex(),current_page)
        self.assertTrue(self.window.diagnostic_figure_directory_button.isEnabled())
        with patch.object(QtGui.QDesktopServices,'openUrl',return_value=True) as opened:
            self.window.diagnostic_figure_directory_button.click()
            self.assertEqual(opened.call_args.args[0].toLocalFile(),'/private/tmp/sspp_publication_ui_result')
        self.window.diagnostic_model={'key':second,'groups':{group:[other]}}
        self.window._select_plot_group()
        self.assertFalse(self.window.diagnostic_figure_directory_button.isEnabled())

    def test_diagnostic_export_empty_chart_and_failure_do_not_enable_a_folder(self):
        self.assertFalse(self.window.diagnostic_figure_export_button.isEnabled())
        self.window._export_diagnostic_figure()
        self.assertFalse(self.service.publication_exports)
        key=self.service.rows[0]['key']
        chart=copy.deepcopy(comparison_report([key])['charts'][0]);chart['id']='diagnostic_second'
        group=self.window.plot_group.currentData()
        self.window.diagnostic_model={'key':key,'groups':{group:[chart]}}
        self.window._select_plot_group()
        self.service.fail_figure=True
        self.window.diagnostic_figure_export_button.click()
        self.wait(lambda: not self.window.runner.pending)
        self.assertTrue(self.window.diagnostic_figure_export_button.isEnabled())
        self.assertFalse(self.window.diagnostic_figure_directory_button.isEnabled())
        self.assertIn('失败',self.window.notice.text())

    def test_single_selection_remains_blocked_by_acquisition_preparation_and_analysis(self):
        key=self.service.rows[0]['key'];self.check(key)
        self.window.snapshot={'job':{'status':'saving'}}
        self.window._update_comparison_controls()
        self.assertFalse(self.window.compare_button.isEnabled())
        self.window.compare_selected()
        self.assertFalse(self.service.comparisons)
        self.window.snapshot={}
        self.window._start_pending=True
        self.window._update_comparison_controls()
        self.assertFalse(self.window.compare_button.isEnabled())
        self.window._start_pending=False
        with patch.object(TemperaturePressureWidget,'is_running',new_callable=PropertyMock,return_value=True):
            self.window._update_comparison_controls()
            self.assertFalse(self.window.compare_button.isEnabled())
            self.window.compare_selected()
            self.assertFalse(self.service.comparisons)
        self.window._comparison_busy=True
        self.window._update_comparison_controls()
        self.assertFalse(self.window.compare_button.isEnabled())
        self.window._comparison_busy=False
        self.window._update_comparison_controls()
        self.assertTrue(self.window.compare_button.isEnabled())
        self.assertEqual(self.window.compare_button.text(),'分析所选记录')

    def test_single_late_analysis_is_discarded_after_filter_changes_selection(self):
        first,second=[row['key'] for row in self.service.rows[:2]]
        self.check(first)
        self.service.compare_release.clear()
        self.window.compare_selected()
        self.wait(lambda: bool(self.service.comparisons))
        self.assertEqual(self.service.comparisons[0][0],[first])
        self.assertFalse(self.window.compare_button.isEnabled())
        self.window.history_search.setText('样品 1')
        self.wait(lambda: not self.window._history_filter_timer.isActive())
        self.assertFalse(self.window.comparison_keys)
        self.service.compare_release.set()
        self.wait(lambda: not self.window.runner.pending)
        self.assertIsNone(self.window.comparison_dialog)
        self.assertFalse(self.window._comparison_busy)
        self.assertFalse(self.window.compare_button.isEnabled())
        self.check(second)
        self.assertTrue(self.window.compare_button.isEnabled())
        self.assertEqual(self.window.compare_button.text(),'分析所选记录')

    def test_filters_clear_hidden_checks_and_preserve_only_visible_keys(self):
        first, second = [row["key"] for row in self.service.rows[:2]]
        self.check(first)
        self.check(second)
        self.window.history_search.setText("样品 1")
        self.wait(lambda: not self.window._history_filter_timer.isActive())
        self.assertEqual([row["key"] for row in self.window.history_rows], [second])
        self.assertEqual(self.window.comparison_keys, {second})
        self.assertIn("已清除 1 条", self.window.notice.text())
        self.assertTrue(self.window.compare_button.isEnabled())
        self.assertEqual(self.window.compare_button.text(),'分析所选记录')
        self.window.history_search.clear()
        self.assertEqual(self.window.history_type.findData("finger_displacement"), -1)
        self.window.history_type.setCurrentIndex(self.window.history_type.findData("teacher_sweep"))
        self.assertEqual(len(self.window.history_rows), 1)
        self.assertFalse(self.window.comparison_keys)
        self.window.history_status.setCurrentIndex(self.window.history_status.findData("complete"))
        self.assertFalse(self.window.history_rows)
        self.window.history_type.setCurrentIndex(0)
        self.assertEqual(len(self.window.history_rows), 9)
        self.window.history_search.setText("sspp a")
        self.assertEqual(len(self.window.history_rows), 9)
        self.window.history_search.setText(first)
        self.wait(lambda: not self.window._history_filter_timer.isActive())
        self.assertEqual(self.window.history_rows[0]["key"], first)
        self.assertIn("原始 UTC", self.window.history_table.item(0, 1).toolTip())

    def test_refresh_uses_keys_for_checks_and_highlight_after_reorder(self):
        key = self.service.rows[2]["key"]
        self.check(key)
        self.window.history_table.selectRow(self.item(key).row())
        self.wait(lambda: self.window.selected_details is not None)
        self.service.rows.reverse()
        self.window.refresh_history()
        self.wait(lambda: not self.window.runner.pending and self.window.selected_details is not None)
        self.assertEqual(self.window.comparison_keys, {key})
        self.assertEqual(self.item(key).checkState(), QtCore.Qt.CheckState.Checked)
        self.assertEqual(self.window.history_table.currentRow(), self.item(key).row())
        self.assertEqual(self.window.selected_details["key"], key)

    def test_batch_selection_preserves_analysis_limits_and_teacher_rejection(self):
        for row in self.service.rows[:9]:
            self.check(row["key"])
        self.assertEqual(len(self.window.comparison_keys), 9)
        self.assertFalse(self.window.compare_button.isEnabled())
        self.window.compare_selected()
        self.assertFalse(self.service.comparisons)
        self.assertTrue(self.window.select_experiment("finger"))
        self.wait(lambda: not self.window.runner.pending)
        self.assertFalse(self.window.comparison_keys)
        self.check("phase_runs/finger_displacement")
        self.check("phase_runs/phase_history")
        self.assertEqual(self.window.comparison_keys, {"phase_runs/finger_displacement", "phase_runs/phase_history"})
        self.assertFalse(self.window.compare_button.isEnabled())
        self.window.compare_selected()
        self.assertFalse(self.service.comparisons)
        self.assertTrue(self.window.select_experiment("material"))
        self.wait(lambda: not self.window.runner.pending)
        self.check("teacher_runs/one")
        self.assertEqual(self.window.comparison_keys, {"teacher_runs/one"})
        self.assertFalse(self.window.compare_button.isEnabled())
        self.window.compare_selected()
        self.assertFalse(self.service.comparisons)

    def test_late_details_and_comparison_do_not_override_changed_filter(self):
        key = self.service.rows[0]["key"]
        self.service.details_release.clear()
        self.window.history_table.selectRow(0)
        self.window.history_search.setText("finger_displacement")
        self.service.details_release.set()
        self.wait(lambda: not self.window.runner.pending)
        self.assertIsNone(self.window.selected_details)
        self.assertIsNone(self.window.viewing_key)
        self.window.history_search.clear()
        self.wait(lambda: not self.window._history_filter_timer.isActive())
        self.check(key)
        self.check(self.service.rows[1]["key"])
        self.service.compare_release.clear()
        self.window.compare_selected()
        self.wait(lambda: bool(self.service.comparisons))
        self.window.history_search.setText("finger_displacement")
        self.service.compare_release.set()
        self.wait(lambda: not self.window.runner.pending)
        self.assertIsNone(self.window.comparison_dialog)
        self.assertFalse(self.window._comparison_busy)

    def test_reference_recompute_and_export_cancel_and_destination(self):
        keys, dialog = self.prepare_comparison()
        self.assertEqual(dialog.reference.currentData(), keys[-1])
        self.assertTrue(dialog.export_button.isEnabled())
        self.service.compare_release.clear()
        dialog.reference.setCurrentIndex(dialog.reference.findData(keys[0]))
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertFalse(dialog.reference.isEnabled())
        self.service.compare_release.set()
        self.wait(lambda: not self.window.runner.pending)
        self.assertEqual(dialog.report["reference_key"], keys[0])
        with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=("", "")):
            self.window._export_comparison(dialog)
        self.assertFalse(self.service.exports)
        with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=("/private/tmp/comparison_ui_result", "")):
            self.window._export_comparison(dialog)
        self.wait(lambda: not self.window.runner.pending)
        self.assertEqual(self.service.exports[0][1], "/private/tmp/comparison_ui_result.zip")
        self.assertIs(self.service.exports[0][0], dialog.report)
        self.assertNotEqual(self.service.exports[0][2], threading.get_ident())
        self.assertTrue(all(thread != threading.get_ident() for _, _, thread in self.service.comparisons))

    def test_failed_reference_restores_last_report_and_closed_dialog_is_not_reopened(self):
        keys, dialog = self.prepare_comparison()
        self.service.fail_reference = True
        dialog.reference.setCurrentIndex(dialog.reference.findData(keys[0]))
        self.wait(lambda: not self.window.runner.pending)
        self.assertEqual(dialog.reference.currentData(), dialog.report["reference_key"])
        self.assertIn("失败", dialog.status.text())
        self.service.fail_reference = False
        self.service.compare_release.clear()
        dialog.reference.setCurrentIndex(dialog.reference.findData(keys[0]))
        dialog.close()
        self.service.compare_release.set()
        self.wait(lambda: not self.window.runner.pending)
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.assertIsNone(self.window.comparison_dialog)
        self.assertFalse(isValid(dialog))

    def test_active_acquisition_and_export_block_new_compare(self):
        keys, dialog = self.prepare_comparison()
        self.service.job = {"status": "running", "config": {"mode": "phase"}}
        self.window._apply_status(self.service.snapshot())
        self.assertFalse(self.window.compare_button.isEnabled())
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertFalse(dialog.reference.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())
        old = set(self.window.comparison_keys)
        self.check(keys[0], False)
        self.assertEqual(self.window.comparison_keys, old)
        self.window.compare_selected()
        self.assertEqual(len(self.service.comparisons), 1)

    def test_export_blocks_reference_and_selection_until_background_finishes(self):
        keys, dialog = self.prepare_comparison()
        self.window.initialize_environment()
        self.wait(lambda: not self.window.runner.pending)
        self.assertTrue(self.window.start_button.isEnabled())
        self.service.export_release.clear()
        with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=("/private/tmp/comparison_ui_result.zip", "")):
            self.window._export_comparison(dialog)
        self.wait(lambda: bool(self.service.exports))
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertFalse(dialog.reference.isEnabled())
        self.assertFalse(self.window.compare_button.isEnabled())
        self.assertFalse(self.window.start_button.isEnabled())
        self.check(keys[0], False)
        self.assertEqual(self.window.comparison_keys, set(keys))
        self.service.export_release.set()
        self.wait(lambda: not self.window.runner.pending)
        self.assertTrue(dialog.reference.isEnabled())
        self.assertTrue(self.window.compare_button.isEnabled())
        self.assertTrue(self.window.start_button.isEnabled())

    def test_phase_report_disables_cross_run_reference_switch(self):
        from sspp_desktop.comparison_widget import ComparisonDialog
        dialog = ComparisonDialog(self.window)
        dialog.apply_report(comparison_report(["phase_runs/a", "phase_runs/b"], kind="phase_history"))
        dialog.set_busy(False)
        dialog.show()
        self.assertFalse(dialog.reference.isEnabled())
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertIn("无跨实验相减", dialog.summary.text())
        dialog.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(dialog))

    def test_large_table_preview_is_bounded_without_mutating_export_report(self):
        from sspp_desktop.comparison_widget import ComparisonDialog
        report = comparison_report(["phase_runs/a", "phase_runs/b"], kind="phase_history")
        report["tables"] = {"phase_samples": {"columns": ["key", "relative_phase_deg"],
                            "rows": [{"key": "phase_runs/a", "relative_phase_deg": n / 10} for n in range(5000)]}}
        original = copy.deepcopy(report)
        dialog = ComparisonDialog(self.window)
        dialog.apply_report(report)
        page = dialog.data_tabs.widget(0)
        self.assertEqual(page.findChild(QtWidgets.QTableWidget).rowCount(), 500)
        self.assertIn("500 / 总 5000", page.findChild(QtWidgets.QLabel).text())
        self.assertIn("导出包含完整数据", page.findChild(QtWidgets.QLabel).text())
        self.assertEqual(dialog.data_tabs.tabText(0), "幅相样点")
        self.assertEqual(page.findChild(QtWidgets.QTableWidget).horizontalHeaderItem(1).text(), "相对相位 (°)")
        self.assertIs(dialog.report, report)
        self.assertEqual(report, original)
        dialog.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
