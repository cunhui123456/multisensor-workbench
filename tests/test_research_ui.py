"""离线验收两条研究流程：人工确认、来源隔离、标注与用户选择的 ZIP 导出。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import copy
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from PySide6 import QtWidgets
from sspp_desktop.experiments.finger_displacement import FingerDisplacementWidget
from sspp_desktop.window import MainWindow
from test_desktop_ui import FakeService
from qt_test_cleanup import dispose_closed_windows


class ResearchService(FakeService):
    def __init__(self):
        super().__init__()
        self.research = {"run_key": "phase_runs/offline_test", "annotations": [], "actions": [], "pending": None}
        self.calls = []

    def finger_snapshot(self):
        return copy.deepcopy(self.research)

    def finger_begin_window(self, **request):
        self.calls.append(("window", request, threading.get_ident()))
        self.research["pending"] = dict(request, id="window_1", state="awaiting_ack")
        return self.finger_snapshot()

    def finger_cancel_window(self):
        self.research["pending"] = None
        return self.finger_snapshot()

    def finger_mark_action(self, **request):
        self.calls.append(("action", request, threading.get_ident()))
        self.research["actions"].append(dict(request, t_s=12))
        return self.finger_snapshot()

    def finger_analyze(self, **request):
        self.calls.append(("analysis", request, threading.get_ident()))
        return {"status": "invalid", "errors": ["测试窗口不足"], "export_path": "/tmp/offline_analysis"}

    def export_bundle(self, key, destination, include_raw=False):
        self.calls.append(("export", (key, destination, include_raw), threading.get_ident()))
        return {"path": destination, "count": 3}


class ResearchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self):
        dispose_closed_windows()

    def wait(self, predicate, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.003)
        self.fail("Qt condition timed out")

    def close_window(self, window, service):
        service.stop()
        window._apply_status(service.snapshot())
        window.close()
        self.wait(lambda: not window.runner.pending and not window.isVisible())

    def widget(self):
        widget = FingerDisplacementWidget()
        widget.apply_snapshot({"run_key": "phase_runs/first", "annotations": []})
        widget.set_acquisition(True, True)
        return widget

    def test_material_name_required_and_workspace_config_is_unambiguous(self):
        service = ResearchService()
        window = MainWindow(service)
        try:
            self.wait(lambda: window.profile.count() == 1)
            window.mode.setCurrentIndex(window.mode.findData("sweep"))
            self.assertEqual(window.live_plots.mode, "sweep")
            self.assertTrue(window.live_plots.phase.isHidden())
            self.assertTrue(window.mark_group.isHidden())
            window._render_live({"mode": None, "points": []})
            self.assertEqual(window.live_plots.mode, "sweep")
            self.assertFalse(hasattr(window, 'confirm'))
            service.initialize_environment({"kind": "usrp", "profile": window.profile.currentData()})
            window.start_experiment()
            self.assertIsNone(service.started)
            self.assertIn("材料", window.notice.text())
            window.material_workspace.material_name.setText("水瓶_2of3")
            self.assertEqual(window.config()["condition"], "material")
            self.assertEqual(window.config()["label"], "水瓶_2of3")
            self.assertTrue(window.select_experiment("finger"))
            self.wait(lambda: not window.runner.pending)
            self.assertEqual(window.config()["condition"], "finger")
            self.assertEqual(window.config()["label"], "finger_session")
            self.assertEqual(window.fields["duration"].maximum(), 180)
        finally:
            self.close_window(window, service)

    def test_guided_sequence_never_moves_or_starts_next_window_automatically(self):
        widget = self.widget()
        requests = []
        widget.begin_requested.connect(requests.append)
        try:
            widget.use_plan()
            widget.capture_button.click()
            self.assertEqual(requests, [{"distance_mm": 10., "role": "calibration", "action": "hold", "settle_s": 2., "hold_s": 2., "research_settings": {"initial_distance_mm": 10., "tolerance_mm": 2., "workflow": "guided"}}])
            self.assertFalse(widget.capture_button.isEnabled())
            pending = dict(requests[0], id="first", state="capturing", start_s=10, end_s=12)
            widget.capture_result({"run_key": "phase_runs/first", "annotations": [], "pending": pending})
            widget.apply_snapshot({"run_key": "phase_runs/first", "annotations": [dict(pending, state="complete")], "pending": None})
            self.assertEqual(widget._step, 1)
            self.assertIn("20 mm", widget.step_label.text())
            self.assertEqual(len(requests), 1)
            self.assertTrue(widget.capture_button.isEnabled())
            widget.set_acquisition(True, False)
            self.assertFalse(widget.capture_button.isEnabled())
        finally:
            widget.close()

    def test_guided_plan_rejects_out_of_range_and_duplicate_validation(self):
        widget = self.widget()
        widget.validation_plan.setText("15, 15")
        widget.use_plan()
        self.assertFalse(widget._plan)
        self.assertIn("不同验证", widget.progress.text())
        widget.validation_plan.setText("15, 40")
        widget.use_plan()
        self.assertIn("范围", widget.progress.text())
        self.assertEqual(widget.hold.minimum(), .5)
        widget.close()

    def test_free_action_records_truth_separately_from_windows(self):
        widget = self.widget()
        requests = []
        widget.action_requested.connect(requests.append)
        widget.method.setCurrentIndex(1)
        widget.distance.setValue(17.5)
        widget.action.setCurrentIndex(widget.action.findData("near"))
        widget.action_note.setText("从 20 mm 靠近")
        widget.action_button.click()
        self.assertEqual(requests[0]["distance_mm"], 17.5)
        self.assertEqual(requests[0]["action"], "near")
        self.assertEqual(widget.records, [])
        widget.close()

    def test_history_source_isolated_and_manual_interval_analysis_bound_to_key(self):
        widget = self.widget()
        widget.load_history({"run_key": "phase_runs/history", "annotations": [], "duration_s": 30},
                            {"mode": "phase", "points": [{"x": 8, "phase": 1}, {"x": 12, "phase": 2}]})
        widget.apply_snapshot({"run_key": "phase_runs/other", "annotations": [{"id": "other"}]})
        self.assertEqual(widget.run_key, "phase_runs/history")
        widget.region.setRegion((9, 11))
        widget._region_changed()
        widget.interval_distance.setValue(15)
        widget.interval_role.setCurrentIndex(1)
        widget.add_interval()
        self.assertEqual(widget.records[0]["start_s"], 9)
        self.assertEqual(widget.records[0]["end_s"], 11)
        self.assertEqual(widget.records[0]["role"], "validation")
        requests = []
        widget.analyze_requested.connect(requests.append)
        widget.request_analysis()
        self.assertEqual(requests[0]["key"], "phase_runs/history")
        self.assertEqual(requests[0]["annotations"][0]["distance_mm"], 15)
        self.assertFalse(widget.capture_button.isEnabled())
        widget.close()

    def test_failed_estimate_and_new_run_never_display_old_millimeters(self):
        widget = self.widget()
        model = {"model": {"active": True}, "initial_distance_mm": 10,
                 "calibration": {"nodes": [{"distance_mm": 10, "phase_deg": 0}, {"distance_mm": 20, "phase_deg": 10}], "distance_range_mm": [10, 20]},
                 "validation": {"windows": [{"distance_mm": 15, "estimated_distance_mm": 15.1}], "max_error_mm": .1, "rmse_mm": .1}}
        widget.apply_snapshot({"run_key": "phase_runs/first", "analysis": model,
                               "estimate": {"ok": True, "distance_mm": 15.1, "displacement_mm": 5.1}})
        self.assertIn("15.10", widget.estimate_label.text())
        self.assertEqual(len(widget.calibration_line.xData), 2)
        widget.apply_snapshot({"run_key": "phase_runs/first", "analysis": model,
                               "estimate": {"ok": False, "error": "超出标定范围"}})
        self.assertNotIn("15.10", widget.estimate_label.text())
        self.assertIn("超出", widget.estimate_reason.text())
        widget.apply_snapshot({"run_key": "phase_runs/second", "annotations": []})
        self.assertIsNone(widget.analysis)
        self.assertTrue(widget.calibration_line.xData is None or len(widget.calibration_line.xData) == 0)
        self.assertNotIn("15.10", widget.estimate_label.text())
        self.assertEqual(widget.records, [])
        widget.close()

    def test_history_restores_reference_and_poll_does_not_override_pending_edits(self):
        widget = self.widget()
        analysis = {"model": {"active": False}, "initial_distance_mm": 20, "tolerance_mm": 1.5}
        widget.load_history({"run_key": "phase_runs/history", "annotations": [], "analysis": analysis})
        self.assertEqual(widget.initial_distance.value(), 20)
        self.assertEqual(widget.tolerance.value(), 1.5)
        widget.use_current()
        widget.apply_snapshot({"run_key": "phase_runs/first", "analysis": analysis})
        widget.initial_distance.setValue(25)
        widget.apply_snapshot({"run_key": "phase_runs/first", "analysis": copy.deepcopy(analysis)})
        self.assertEqual(widget.initial_distance.value(), 25)
        widget.close()

    def test_begin_and_analysis_use_background_threads_and_stop_stays_available(self):
        service = ResearchService()
        window = MainWindow(service)
        window.timer.stop()
        try:
            self.wait(lambda: window.profile.count() == 1 and not window.runner.pending)
            self.assertTrue(window.select_experiment("finger"))
            self.wait(lambda: not window.runner.pending)
            # 明确选择手指实验，并排空旧空闲快照后再注入正在采集状态。
            service.job = {"status": "running", "config": {"mode": "phase"}, "baseline": {"end_s": 7}}
            window._apply_status(service.snapshot())
            window.finger_workspace.apply_snapshot(service.finger_snapshot())
            window.finger_workspace.use_plan()
            window.finger_workspace.capture_button.click()
            self.wait(lambda: bool(service.calls))
            self.assertNotEqual(service.calls[0][2], threading.get_ident())
            self.assertTrue(window.stop_button.isEnabled())
            window.stop_experiment()
            self.wait(lambda: service.stopped)
        finally:
            self.close_window(window, service)

    def test_bundle_dialog_cancel_has_no_effect_and_selection_runs_in_background(self):
        service = ResearchService()
        window = MainWindow(service)
        window.selected_details = {"key": "phase_runs/test"}
        try:
            with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=("", "")):
                window.export_bundle()
            self.assertFalse(any(call[0] == "export" for call in service.calls))
            self.assertFalse(window.include_raw.isChecked())
            window.snapshot = {"job": {"status": "running"}}
            with patch.object(QtWidgets.QFileDialog, "getSaveFileName") as dialog:
                window.export_bundle()
                dialog.assert_not_called()
            window.snapshot = {}
            window.include_raw.setChecked(True)
            with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=("/tmp/my_results.zip", "ZIP")):
                window.export_bundle()
            self.wait(lambda: any(call[0] == "export" for call in service.calls))
            exported = next(call for call in service.calls if call[0] == "export")
            self.assertEqual(exported[1], ("phase_runs/test", "/tmp/my_results.zip", True))
            self.assertNotEqual(exported[2], threading.get_ident())
        finally:
            self.close_window(window, service)


if __name__ == "__main__":
    unittest.main()
