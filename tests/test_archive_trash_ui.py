"""Recoverable archive deletion, independent selection, asynchronous locks and stale views."""
from copy import deepcopy
import threading
import unittest
from unittest.mock import PropertyMock, patch
from PySide6 import QtCore, QtWidgets

from sspp_desktop.archive_trash_dialog import ArchiveDeleteDialog
from sspp_desktop.analysis_workbench_widget import AnalysisWorkbenchWidget
from sspp_desktop.temperature_widget import TemperaturePressureWidget
import test_experiment_navigation as fixtures


class ArchiveTrashUiTests(unittest.TestCase):
    setUpClass = fixtures.ExperimentNavigationTests.__dict__['setUpClass']
    pump = fixtures.ExperimentNavigationTests.pump
    select = fixtures.ExperimentNavigationTests.select

    def setUp(self):
        fixtures.ExperimentNavigationTests.setUp(self)
        self.main_thread = threading.get_ident()
        self.calls = []
        self.trash = {}
        self.release = threading.Event()
        self.release.set()
        self.entered = threading.Event()
        self.partial_failure = False
        self.service.rows = [
            {'key':f'runs/test_{i}', 'label':f'实验 {i}', 'category':'temperature_sweep' if i % 2 else 'material_sweep',
             'mode':'sweep', 'status':'failed' if i == 0 else 'complete', 'sensor':'SSPP'} for i in range(12)]
        def remove(keys):
            self.calls.append(('delete', list(keys), threading.get_ident()))
            self.entered.set()
            self.release.wait(4)
            actual = list(keys[:1] if self.partial_failure else keys)
            for key in actual:
                row = next(row for row in self.service.rows if row['key'] == key)
                self.trash['trash-' + key] = {**row, 'trash_id':'trash-' + key, 'deleted_utc':'2026-11-05T07:00:00Z'}
            self.service.rows = [row for row in self.service.rows if row['key'] not in actual]
            if self.partial_failure:
                error = RuntimeError('部分移动完成，回滚未完全成功')
                error.deleted_keys = actual
                raise error
            return {'deleted_keys':actual}
        def trashed():
            self.calls.append(('list', [], threading.get_ident()))
            return deepcopy(list(self.trash.values()))
        def restore(ids):
            self.calls.append(('restore', list(ids), threading.get_ident()))
            restored = [self.trash.pop(value) for value in ids]
            self.service.rows.extend(restored)
            return {'restored_keys':[row['key'] for row in restored]}
        self.service.trash_records = remove
        self.service.trashed_records = trashed
        self.service.restore_records = restore
        self.service.details = lambda key, include_curve=True: {
            'key':key, 'metadata':{}, 'files':[], 'curve_loaded':include_curve,
            'curve':{'mode':'sweep','points':[]}, 'context':{'experiment':'material_sweep'}}
        self.window._history_all_rows = deepcopy(self.service.rows)
        self.window._render_history_rows()
        self.window.tabs.setCurrentIndex(1)
        self.pump(lambda:not self.window._archive_operation_blocked())

    def tearDown(self):
        self.release.set()
        fixtures.ExperimentNavigationTests.tearDown(self)

    def check(self, *indices):
        for index in indices:
            self.window.history_table.item(index, 0).setCheckState(QtCore.Qt.CheckState.Checked)

    def delete(self, accepted=True):
        result = QtWidgets.QDialog.DialogCode.Accepted if accepted else QtWidgets.QDialog.DialogCode.Rejected
        with patch.object(ArchiveDeleteDialog, 'exec', return_value=result):
            self.window.delete_selected_records()

    def idle(self):
        self.pump(lambda:not self.window._archive_busy and not self.window.runner.pending
                  and not self.window.liquid_workspace.is_busy and not self.window.analysis_workspace.is_running)

    def test_confirmation_lists_count_names_ids_and_cancel_does_nothing(self):
        rows = self.service.rows[:2]
        rows[0]['condition_summary'] = '手填参考 30 °C'
        dialog = ArchiveDeleteDialog(rows, self.window)
        self.assertEqual(dialog.records.count(), 2)
        for index, row in enumerate(rows):
            self.assertIn(row['label'], dialog.records.item(index).text())
            self.assertIn(row['key'], dialog.records.item(index).text())
        self.assertIn('手填参考 30 °C', dialog.records.item(0).text())
        labels = ' '.join(item.text() for item in dialog.findChildren(QtWidgets.QLabel))
        self.assertIn('2 条', labels)
        self.assertIn('恢复', labels)
        dialog.close()
        self.check(0, 1)
        self.delete(accepted=False)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.service.rows), 12)

    def test_batch_delete_accepts_failed_cross_category_and_over_eight_records(self):
        w = self.window
        self.check(*range(12))
        self.assertEqual(len(w.comparison_keys), 12)
        self.assertTrue(w.delete_records_button.isEnabled())
        self.assertFalse(w.compare_button.isEnabled())
        with patch.object(w, '_request_comparison') as analyze:
            w.compare_selected()
            analyze.assert_not_called()
        self.delete()
        self.idle()
        self.assertEqual(len(self.trash), 12)
        self.assertEqual(w.history_rows, [])
        self.assertEqual(w.comparison_keys, set())
        self.assertNotEqual(self.calls[0][2], self.main_thread)

    def test_deletion_locks_hardware_actions_and_keeps_event_loop_responsive(self):
        w = self.window
        self.check(0)
        self.release.clear()
        self.delete()
        self.pump(self.entered.is_set)
        self.assertFalse(w.start_button.isEnabled())
        self.assertFalse(w.check_button.isEnabled())
        self.assertFalse(w.initialize_button.isEnabled())
        self.assertTrue(w.temperature_workspace._external_busy)
        self.assertTrue(w.analysis_workspace._acquisition_busy)
        w.start_experiment()
        w.preflight()
        w.temperature_workspace.start_capture()
        w.temperature_workspace.start_preparation(upload=False)
        with patch.object(w, '_submit') as submit:
            w.selected_details = {'key':self.service.rows[0]['key']}
            w.export_diagnostics()
            w.export_bundle()
            w.open_run()
            w.open_file('manifest.json')
            w.save_file()
            submit.assert_not_called()
        self.assertIsNone(self.service.started)
        self.assertNotIn('preflight', w.runner.pending)
        self.assertFalse(w.temperature_workspace.is_running)
        fired = []
        QtCore.QTimer.singleShot(0, lambda:fired.append(True))
        self.pump(lambda:bool(fired))
        self.delete()
        self.assertEqual(len([call for call in self.calls if call[0] == 'delete']), 1)
        self.release.set()
        self.idle()
        self.assertFalse(w.temperature_workspace._external_busy)

    def test_handlers_reject_capture_preflight_analysis_and_initialization(self):
        w = self.window
        self.check(0)
        for owner, attribute in ((TemperaturePressureWidget, 'is_running'), (AnalysisWorkbenchWidget, 'is_running')):
            with self.subTest(owner=owner), patch.object(owner, attribute, new_callable=PropertyMock, return_value=True), \
                    patch.object(ArchiveDeleteDialog, 'exec') as confirm:
                w.delete_selected_records()
                w.restore_deleted_records(['missing'])
                confirm.assert_not_called()
        for pending in ('preflight', 'export', 'comparison_export', 'figure_export', 'diagnostics', 'save_file', 'open_file', 'open_run'):
            with self.subTest(pending=pending):
                w.runner.pending[pending] = (None, None, 'analysis')
                try:
                    self.assertTrue(w._archive_operation_blocked())
                    self.delete()
                finally:
                    w.runner.pending.pop(pending)
        w._environment_pending = True
        self.delete()
        w._environment_pending = False
        self.assertEqual(self.calls, [])

    def test_restore_dialog_batches_records_and_rejects_duplicate_refresh(self):
        w = self.window
        self.check(0, 1)
        self.delete()
        self.idle()
        w.open_deleted_records()
        w._refresh_deleted_records()
        self.idle()
        dialog = w.archive_trash_dialog
        self.assertEqual(len([call for call in self.calls if call[0] == 'list']), 1)
        self.assertEqual(dialog.table.rowCount(), 2)
        self.assertIn(dialog.table.item(0, 3).text(), ('材料逐点扫频', 'SSPP 温度同步扫频'))
        for row in range(2):
            dialog.table.item(row, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        dialog.restore_button.click()
        self.idle()
        self.assertEqual(len(self.service.rows), 12)
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertFalse(self.trash)
        self.assertTrue(all(call[2] != self.main_thread for call in self.calls))

    def test_partial_failure_refreshes_actual_state_and_clears_deleted_views(self):
        w = self.window
        key = self.service.rows[0]['key']
        self.check(0, 1)
        w._selected_history_key = w.viewing_key = key
        w.selected_details = {'key':key}
        w.diagnostic_model = {'key':key, 'groups':{}}
        self.partial_failure = True
        self.delete()
        self.idle()
        self.assertNotIn(key, {row['key'] for row in w.history_rows})
        self.assertIsNone(w.viewing_key)
        self.assertIsNone(w.selected_details)
        self.assertEqual(w.diagnostic_model, {})
        self.assertIn('回滚', w.notice.text())

    def test_old_poll_cannot_reintroduce_a_deleted_record(self):
        w = self.window
        key = self.service.rows[0]['key']
        old = {'job':{'status':'complete', 'config':{'mode':'sweep'}, 'output':'Data/' + key}}
        callbacks = {}
        original_submit = w.runner.submit
        def capture(name, operation, success=None, failure=None, lane='analysis'):
            if name == 'poll':
                callbacks['poll'] = success
                return True
            return original_submit(name, operation, success, failure, lane)
        with patch.object(w.runner, 'submit', side_effect=capture):
            w._tick()
        self.check(0)
        self.delete()
        self.idle()
        callbacks['poll']((old, {'mode':'sweep','points':[{'repeat':1,'carrier_hz':5e9,'amplitude_dB':-10}]}, {}))
        self.assertNotEqual((w.snapshot.get('job') or {}).get('output'), 'Data/' + key)
        self.assertFalse(w.current_result_button.isEnabled())

    def test_mutation_refreshes_sources_after_releasing_workspace_locks(self):
        w = self.window
        self.check(0)
        states = []
        with patch.object(w.analysis_workspace, 'refresh_sources', side_effect=lambda:states.append(('analysis', w.analysis_workspace._acquisition_busy))), \
                patch.object(w.liquid_workspace, 'refresh_history', side_effect=lambda:states.append(('liquid', w.liquid_workspace._external_busy))):
            self.delete()
            self.pump(lambda:not w._archive_busy and not w.runner.pending)
        self.assertIn(('analysis', False), states)
        self.assertIn(('liquid', False), states)
        self.assertNotIn(('analysis', True), states)
        self.assertNotIn(('liquid', True), states)


if __name__ == '__main__':
    unittest.main()
