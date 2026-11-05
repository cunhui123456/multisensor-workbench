"""Archive selection stays lightweight and stale explicit reads cannot replace it."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from PySide6 import QtCore, QtTest
import test_experiment_navigation as fixtures


class ArchiveLazyUiTests(unittest.TestCase):
    setUpClass = fixtures.ExperimentNavigationTests.__dict__['setUpClass']
    tearDown = fixtures.ExperimentNavigationTests.tearDown
    pump = fixtures.ExperimentNavigationTests.pump
    select = fixtures.ExperimentNavigationTests.select

    def setUp(self):
        fixtures.ExperimentNavigationTests.setUp(self)
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.entered = threading.Event()

        def details(key, include_curve=True):
            self.calls.append((key, include_curve))
            if include_curve:
                self.entered.set()
                self.release.wait(3)
            return {'key':key, 'context':{'experiment':'material_sweep'}, 'metadata':{},
                    'files':['manifest.json'], 'condition_details':'条件：' + key,
                    'curve_loaded':include_curve,
                    'curve':{'mode':'sweep', 'loaded':include_curve,
                             'points':[{'repeat':1,'carrier_hz':5e9,'amplitude_dB':-35}] if include_curve else []}}

        self.service.details = details
        self.rows = [{'key':f'runs/sample_{i}', 'category':'material_sweep', 'mode':'sweep',
                      'status':'complete', 'label':f'sample_{i}', 'condition_details':f'缓存条件 {i}'} for i in range(150)]
        self.window._history_all_rows = self.rows
        self.window._render_history_rows()
        self.window.tabs.setCurrentIndex(1)
        self.addCleanup(self.release.set)

    def pick(self, index):
        self.window.history_table.selectRow(index)
        self.pump(lambda: self.window.selected_details is not None and not self.window.runner.pending)

    def test_selecting_and_checking_rows_never_read_or_render_curves(self):
        w = self.window
        with patch.object(w.live_plots, 'update_snapshot') as draw:
            for index in range(4):
                self.pick(index)
                w.history_table.item(index, 0).setCheckState(QtCore.Qt.CheckState.Checked)
            self.assertEqual(self.calls, [(row['key'], False) for row in self.rows[:4]])
            draw.assert_not_called()
            self.assertIsNone(w.viewing_key)
            self.assertEqual(w.tabs.currentIndex(), 1)
            self.assertEqual(len(w.comparison_keys), 4)
            w._view_saved_curve()
            self.pump(lambda: not w.runner.pending)
            self.assertEqual(self.calls[-1], (self.rows[3]['key'], True))
            draw.assert_called_once()
            self.assertEqual(w.tabs.currentIndex(), 0)

    def test_unchanged_comparison_state_does_not_emit_full_table_changes(self):
        w = self.window
        spy = QtTest.QSignalSpy(w.history_table.model().dataChanged)
        for _ in range(5):
            w._update_comparison_controls()
        self.assertEqual(spy.count(), 0)
        w.history_table.item(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        self.assertEqual(spy.count(), 1)
        w.snapshot = {'job':{'status':'running','config':{'mode':'sweep'}}}
        w._update_comparison_controls()
        changed = spy.count()
        self.assertEqual(changed, 151)
        for _ in range(5):
            w._update_comparison_controls()
        self.assertEqual(spy.count(), changed)
        self.assertFalse(w.history_table.item(1, 0).flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable)

    def test_slow_explicit_read_keeps_ui_responsive_and_cannot_replace_new_selection(self):
        w = self.window
        self.pick(0)
        self.release.clear()
        with patch.object(w.live_plots, 'update_snapshot') as draw:
            w._view_saved_curve()
            self.pump(self.entered.is_set)
            fired = []
            QtCore.QTimer.singleShot(0, lambda: fired.append(True))
            w.history_table.selectRow(1)
            self.pump(lambda: bool(fired))
            self.assertIn('缓存条件 1', w.metadata.toPlainText())
            self.assertEqual(w.tabs.currentIndex(), 1)
            self.release.set()
            self.pump(lambda: not w.runner.pending and w.selected_details is not None)
            self.assertEqual(w.selected_details['key'], self.rows[1]['key'])
            self.assertEqual(self.calls[-1], (self.rows[1]['key'], False))
            draw.assert_not_called()
            self.assertIsNone(w.viewing_key)

    def test_search_keystrokes_coalesce_and_enter_flushes(self):
        w = self.window
        with patch.object(w, '_render_history_rows', wraps=w._render_history_rows) as render:
            w.history_search.setText('s')
            w.history_search.setText('sample_')
            w.history_search.setText('sample_149')
            self.assertEqual(render.call_count, 0)
            self.pump(lambda: render.call_count == 1)
            self.assertEqual(len(w.history_rows), 1)
            w.history_search.setText('sample_148')
            w.history_search.returnPressed.emit()
            self.assertEqual(render.call_count, 2)
            self.assertFalse(w._history_filter_timer.isActive())

    def test_search_immediately_invalidates_a_comparison_before_debounce(self):
        w = self.window
        callbacks = {}
        def capture(key, operation, success=None, lane='analysis', failure=None):
            callbacks[key] = success
            return True
        with patch.object(w, '_submit', side_effect=capture), patch('sspp_desktop.window.ComparisonDialog') as dialog:
            w._request_comparison([self.rows[0]['key']])
            self.assertTrue(w._comparison_busy)
            w.history_search.setText('sample_149')
            self.assertTrue(w._history_filter_pending)
            self.assertFalse(w.compare_button.isEnabled())
            callbacks['comparison']({})
            dialog.assert_not_called()
            self.assertIsNone(w.comparison_dialog)
            self.assertFalse(w._comparison_busy)
            self.assertTrue(w._history_filter_timer.isActive())

    def test_analyze_during_debounce_cannot_use_old_filter_checks(self):
        w = self.window
        w.history_table.item(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        self.assertIn(self.rows[0]['key'], w.comparison_keys)
        w.history_search.setText('sample_149')
        self.assertFalse(w.compare_button.isEnabled())
        with patch.object(w, '_request_comparison') as request:
            w.compare_selected()
            request.assert_not_called()
        self.assertEqual(w.comparison_keys, set())
        self.assertEqual([row['key'] for row in w.history_rows], [self.rows[149]['key']])

    def test_temperature_history_is_only_loaded_for_explicit_curve_action(self):
        w = self.window
        original = self.service.details
        def details(key, include_curve=True):
            result = original(key, include_curve)
            result['context']['experiment'] = 'temperature_sweep'
            return result
        self.service.details = details
        self.service.temperature_history = lambda key: {'temperature_points':[]}
        with patch.object(self.service, 'temperature_history', wraps=self.service.temperature_history) as temperature:
            self.pick(0)
            temperature.assert_not_called()
            w._view_saved_curve()
            self.pump(lambda: not w.runner.pending)
            temperature.assert_called_once_with(self.rows[0]['key'])
            self.assertEqual(w.tabs.currentIndex(), w.temperature_sweep_tab)

    def real_serial_archive(self, root, *, sealed=False, calibration=False):
        from sspp_desktop.config import Settings
        from sspp_desktop.services import DesktopService
        from test_serial_archive import make_record
        run = make_record(root, 'growing', calibration=calibration, sealed=sealed)
        archive = DesktopService(Settings(root=Path(root)))
        self.addCleanup(archive.shutdown)
        self.service.history = archive.history
        def details(key, include_curve=True):
            self.calls.append((key, include_curve))
            return archive.details(key, include_curve=include_curve)
        self.service.details = details
        self.select('temperature_calibration' if calibration else 'temperature')
        self.window.refresh_history()
        self.pump(lambda: not self.window.runner.pending)
        self.pick(0)
        return run, archive, 'temperature_pressure_runs/growing'

    @staticmethod
    def grow_serial_record(run, *, sealed=False):
        samples = run / 'samples.csv'
        samples.write_text(samples.read_text() + '2,2.95,300,0,S6,610\n')
        manifest = json.loads((run / 'manifest.json').read_text())
        manifest.update(rows=5, status='complete' if sealed else 'running', sample_name='updated_sample',
                        serial_closed=sealed, workers_stopped=sealed, data_files_closed=sealed)
        for entry in manifest['files']:
            data = (run / entry['path']).read_bytes()
            entry.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        (run / 'manifest.json').write_text(json.dumps(manifest))

    def test_serial_completion_refreshes_summary_then_explicitly_reads_final_samples(self):
        with tempfile.TemporaryDirectory() as root:
            run, archive, key = self.real_serial_archive(root)
            w = self.window
            w._view_saved_curve()
            self.pump(lambda: not w.runner.pending)
            self.assertEqual(len(w.selected_details['curve']['points']), 4)
            self.grow_serial_record(run, sealed=True)
            w._temperature_complete(key)
            self.pump(lambda: not w.runner.pending and w.selected_details is not None
                      and w.selected_details['metadata']['rows'] == 5)
            self.assertFalse(w.selected_details['curve_loaded'])
            self.assertEqual(self.calls, [(key, False), (key, True), (key, False)])
            w._view_saved_curve()
            self.pump(lambda: not w.runner.pending)
            self.assertEqual(self.calls[-1], (key, True))
            self.assertEqual(len(w.selected_details['curve']['points']), 5)

    def test_unsealed_serial_curve_is_reread_on_each_explicit_view(self):
        with tempfile.TemporaryDirectory() as root:
            run, archive, key = self.real_serial_archive(root)
            w = self.window
            w._view_saved_curve()
            self.pump(lambda: not w.runner.pending)
            self.grow_serial_record(run)
            w._view_saved_curve()
            self.pump(lambda: not w.runner.pending)
            self.assertEqual(self.calls, [(key, False), (key, True), (key, True)])
            self.assertEqual(len(w.selected_details['curve']['points']), 5)

    def test_plain_refresh_updates_current_summary_without_reading_samples(self):
        with tempfile.TemporaryDirectory() as root:
            run, archive, key = self.real_serial_archive(root, sealed=True)
            w = self.window
            w._view_saved_curve()
            self.pump(lambda: not w.runner.pending)
            self.grow_serial_record(run, sealed=True)
            w.refresh_history()
            self.pump(lambda: not w.runner.pending and w.selected_details is not None
                      and w.selected_details['metadata']['sample_name'] == 'updated_sample')
            self.assertFalse(w.selected_details['curve_loaded'])
            self.assertEqual(self.calls, [(key, False), (key, True), (key, False)])
            self.assertIn('updated_sample', w.metadata.toPlainText())
            w._view_saved_curve()
            self.pump(lambda: not w.runner.pending)
            self.assertEqual(len(w.selected_details['curve']['points']), 5)

    def test_sealed_calibration_correction_reuses_only_the_displayed_raw_curve(self):
        with tempfile.TemporaryDirectory() as root:
            run, archive, key = self.real_serial_archive(root, sealed=True, calibration=True)
            self.service.correct_calibration_points = archive.correct_calibration_points
            w = self.window
            w._view_saved_curve()
            self.pump(lambda: not w.runner.pending)
            w._delete_calibration_points({'key':key, 'point_ids':[1], 'reason':'误标'})
            self.pump(lambda: not w.runner.pending and w.selected_details is not None
                      and w.selected_details['calibration_points'][0].get('deleted') is True)
            self.assertEqual(self.calls, [(key, False), (key, True), (key, False)])
            self.assertTrue(w.selected_details['curve_loaded'])
            self.assertEqual(len(w.selected_details['curve']['points']), 4)
            w._view_saved_curve()
            self.assertEqual(len(self.calls), 3)


if __name__ == '__main__':
    unittest.main()
