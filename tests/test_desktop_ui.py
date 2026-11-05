"""离线 Qt 验收：线程响应、参数锁、阶段标记、完整诊断与回放展示。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from PySide6 import QtCore, QtWidgets
from sspp_desktop.plots import DiagnosticPlot, LivePlots
from sspp_desktop.window import MainWindow
from sspp_desktop.workers import TaskRunner
from qt_test_cleanup import dispose_closed_windows


class FakeService:
    def __init__(self):
        self.settings = SimpleNamespace(root=Path(__file__).resolve().parents[1], ui_interval_ms=30,
                                        preview_interval_ms=100, default_profile="own")
        self.job = None
        self.marked = None
        self.started = None
        self.stopped = False
        self.closed = False
        self.preflight_release = threading.Event()
        self.environments = {kind: {"state": "idle", "target": None, "message": "设备未初始化", "log_path": None}
                             for kind in ("usrp", "serial")}
        self._batch_initializing = False
        self._batch_cancelled = False
        self._active_kind = None
        self.initialize_calls = []
        self.workbench_calls = []

    @property
    def environment(self):
        return self.environment_snapshot()

    def environment_snapshot(self, kind=None):
        if kind is not None:
            return dict(self.environments[kind])
        devices = {key: dict(value) for key, value in self.environments.items()}
        states = [item["state"] for item in devices.values()]
        state = ("initializing" if self._batch_initializing or "initializing" in states else
                 "ready" if all(item == "ready" for item in states) else
                 "partial" if "ready" in states else
                 "cancelled" if self._batch_cancelled or "cancelled" in states else
                 "unavailable" if any(item in {"failed", "skipped"} for item in states) else "idle")
        message = "已就绪设备可开始实验" if state in {"partial", "ready"} else "；".join(item["message"] for item in devices.values())
        return {"state": state, "devices": devices, "message": message, "log_path": None}

    def initialize_environment(self, target, *, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            self.cancel_environment_initialization()
            return self.environment_snapshot(target["kind"])
        self.initialize_calls.append(dict(target))
        self.environments[target["kind"]] = {"state": "ready", "target": dict(target), "message": "初始化检查通过，未开始采集", "log_path": None}
        return self.environment_snapshot(target["kind"])

    def initialize_workbench(self, targets, *, cancel_event=None):
        self.workbench_calls.append({kind: dict(value) if value else None for kind, value in targets.items()})
        self._batch_initializing, self._batch_cancelled = True, False
        try:
            for kind in ("serial", "usrp"):
                target = targets.get(kind)
                if target is None:
                    if self.environments[kind]["state"] != "ready":
                        self.environments[kind] = {"state": "skipped", "target": None, "message": "本次跳过", "log_path": None}
                    continue
                if cancel_event is not None and cancel_event.is_set():
                    self._batch_cancelled = True
                    break
                self.initialize_environment(target, cancel_event=cancel_event)
        finally:
            self._active_kind = None
            self._batch_initializing = False
        return self.environment_snapshot()

    def invalidate_environment(self, kind=None):
        for key in (kind,) if kind else ("usrp", "serial"):
            self.environments[key] = {"state": "idle", "target": None, "message": "设备未初始化", "log_path": None}

    def environment_matches(self, target):
        state = self.environment_snapshot(target["kind"])
        return state["state"] == "ready" and state["target"] == target

    def cancel_environment_initialization(self):
        self._batch_cancelled = True
        if self._active_kind is not None:
            self.environments[self._active_kind] = {"state": "cancelled", "target": None, "message": "已取消", "log_path": None}

    def refresh_profiles(self):
        return [{"id": "own", "name": "测试配置", "channel": 0}]

    def history(self):
        return []

    def snapshot(self):
        return {"job": self.job, "logs": [], "events": []}

    def live_snapshot(self):
        return {"mode": None, "points": [], "preview": None, "revision": 0}

    def preflight(self, config):
        self.preflight_release.wait(2)
        return {"valid": True, "summary": "离线参数检查"}

    def start(self, config):
        self.started = config
        self.job = {"status": "running", "config": config, "baseline": {"end_s": 7}}
        return self.snapshot()

    def stop(self):
        self.stopped = True
        if self.job:
            self.job["status"] = "stopped"
            self.job["rf_stopped"] = True
        return self.snapshot()

    def mark(self, label, settle, contact):
        self.marked = (label, settle, contact)
        return {"accepted": True}

    def shutdown(self):
        self.closed = True
        self.preflight_release.set()


class DesktopUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self):
        dispose_closed_windows()

    def pump_until(self, predicate, timeout=3):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Qt condition timed out")

    def test_worker_is_bounded_and_callbacks_arrive_on_gui_thread(self):
        runner = TaskRunner()
        release = threading.Event()
        values = []
        gui_thread = threading.get_ident()
        self.assertTrue(runner.submit("one", lambda: (release.wait(2), threading.get_ident()),
                                      lambda result: values.append((result, threading.get_ident()))))
        self.assertFalse(runner.submit("one", lambda: None))
        release.set()
        self.pump_until(lambda: bool(values))
        self.assertNotEqual(values[0][0][1], gui_thread)
        self.assertEqual(values[0][1], gui_thread)
        self.assertFalse(runner.pending)

    def test_urgent_stop_remains_available_when_control_pool_is_full(self):
        runner = TaskRunner()
        release = threading.Event()
        stopped = []
        try:
            for key in ("preflight", "start"):
                self.assertTrue(runner.submit(key, lambda: release.wait(2), lane="control"))
            self.assertFalse(runner.submit("mark", lambda: None, lane="control"))
            self.assertTrue(runner.submit("stop", lambda: stopped.append(True), lane="urgent"))
            self.pump_until(lambda: bool(stopped), .5)
        finally:
            release.set()
            self.pump_until(lambda: not runner.pending)

    def test_parameter_lock_mark_stop_and_nonblocking_preflight(self):
        service = FakeService()
        window = MainWindow(service)
        window.show()
        try:
            self.pump_until(lambda: window.profile.count() == 1 and not window.runner.pending)
            self.assertTrue(window.select_experiment("finger"))
            self.pump_until(lambda: not window.runner.pending)
            self.assertEqual(window.config()["mode"], "phase")
            self.assertEqual(set(window.config()), set(__import__("sspp_lab.controller", fromlist=["DEFAULTS"]).DEFAULTS) | {"profile"})
            window.preflight()
            ticked = []
            QtCore.QTimer.singleShot(10, lambda: ticked.append(True))
            self.pump_until(lambda: bool(ticked), .5)
            self.assertIn("preflight", window.runner.pending)
            service.preflight_release.set()
            self.pump_until(lambda: "preflight" not in window.runner.pending)
            window.initialize_environment()
            self.pump_until(lambda: "initialize_environment" not in window.runner.pending)
            window.start_experiment()
            self.pump_until(lambda: service.started is not None and "start" not in window.runner.pending)
            self.assertFalse(window.config_container.isEnabled())
            self.assertTrue(window.stop_button.isEnabled())
            self.assertTrue(window.mark_group.isEnabled())
            window.settle.setValue(2.5)
            window.contact.setChecked(True)
            window.mark("finger_near")
            self.pump_until(lambda: service.marked is not None)
            self.assertEqual(service.marked, ("finger_near", 2.5, True))
            window.stop_experiment()
            self.pump_until(lambda: service.stopped and "stop" not in window.runner.pending)
            self.assertTrue(window.config_container.isEnabled())
        finally:
            service.preflight_release.set()
            service.stop()
            window._apply_status(service.snapshot())
            window.close()
            self.pump_until(lambda: service.closed and not window.isVisible())

    def test_parameter_reset_restores_both_modes_and_preserves_research_context(self):
        from sspp_lab.controller import DEFAULTS, validate_request
        service = FakeService()
        window = MainWindow(service)
        window.show()
        window.timer.stop()
        try:
            self.pump_until(lambda: window.profile.count() == 1 and not window.runner.pending)
            self.assertTrue(window.select_experiment("finger"))
            self.pump_until(lambda: not window.runner.pending)
            window.profile.addItem("另一测试设备", "teacher")
            window.profile.setCurrentIndex(1)
            window.fields["label"].setText("session_keep")
            window.fields["sensor"].setText("sensor_keep")
            window.material_workspace.material_name.setText("空瓶")
            window.finger_workspace.initial_distance.setValue(25)
            window.finger_workspace.tolerance.setValue(1.5)
            window.finger_workspace.records = [{"id": "keep_annotation"}]
            for mode in ("phase", "sweep"):
                with self.subTest(mode=mode):
                    window.mode.setCurrentIndex(window.mode.findData(mode))
                    for widget in window.fields.values():
                        if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                            widget.setValue(widget.maximum())
                    # 单点时限复位；扫频没有总时限控件或隐藏预算。
                    self.assertNotIn("max_seconds", window.fields)
                    window.fields["point_timeout"].setValue(30)
                    window.reset_button.click()
                    cfg = validate_request(window.config(), require_setup=False)
                    self.assertIsNone(cfg["max_seconds"])
                    for key, value in DEFAULTS.items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            self.assertEqual(cfg[key], value)
                    self.assertEqual(cfg["mode"], mode)
                    self.assertEqual(cfg["profile"], "teacher")
                    self.assertEqual(cfg["label"], "空瓶" if mode == "sweep" else "session_keep")
                    self.assertEqual(cfg["sensor"], "sensor_keep")
                    self.assertFalse(cfg["setup_confirmed"])
                    self.assertEqual(window.finger_workspace.initial_distance.value(), 25)
                    self.assertEqual(window.finger_workspace.tolerance.value(), 1.5)
                    self.assertEqual(window.finger_workspace.records, [{"id": "keep_annotation"}])
                    window.start_experiment()
                    self.assertIsNone(service.started)  # 参数复位不自动初始化设备。
        finally:
            window.close()
            self.pump_until(lambda: service.closed and not window.isVisible())

    def test_parameter_reset_is_blocked_during_acquisition_and_pending_preflight(self):
        service = FakeService()
        window = MainWindow(service)
        window.show()
        window.timer.stop()
        try:
            self.pump_until(lambda: window.profile.count() == 1 and not window.runner.pending)
            self.assertTrue(window.select_experiment("finger"))
            self.pump_until(lambda: not window.runner.pending)
            window.fields["carrier_ghz"].setValue(3.5)
            for snapshot in ([{"job": {"status": status}} for status in
                              ("starting", "running", "stopping", "saving")]
                             + [{"preparing": True}]):
                with self.subTest(snapshot=snapshot):
                    window._apply_status(snapshot)
                    self.assertFalse(window.reset_button.isEnabled())
                    window.reset_parameters()  # 直接调用也不能绕过禁用按钮。
                    self.assertEqual(window.fields["carrier_ghz"].value(), 3.5)
                    self.assertFalse(hasattr(window, "confirm"))
            window._apply_status({})
            for flag in ("_start_pending", "_close_requested"):
                setattr(window, flag, True)
                window.reset_parameters()
                self.assertEqual(window.fields["carrier_ghz"].value(), 3.5)
                setattr(window, flag, False)
            window.preflight()
            self.assertFalse(window.reset_button.isEnabled())
            window.reset_parameters()
            self.assertEqual(window.fields["carrier_ghz"].value(), 3.5)
            service.preflight_release.set()
            self.pump_until(lambda: "preflight" not in window.runner.pending)
            self.assertTrue(window.reset_button.isEnabled())
            window.reset_button.click()
            self.assertEqual(window.fields["carrier_ghz"].value(), 2.4)
            self.assertFalse(hasattr(window, "confirm"))
            self.assertIsNone(service.started)
            self.assertFalse(service.stopped)
        finally:
            service.preflight_release.set()
            window._close_requested = False
            window._start_pending = False
            window._apply_status({})
            window.close()
            self.pump_until(lambda: service.closed and not window.isVisible())

    def test_phase_gaps_and_rewind_do_not_bridge_or_leave_future_rounds(self):
        plots = LivePlots()
        plots.update_snapshot({"mode": "phase", "points": [
            {"x": 1, "amplitude": 1, "phase": 10}, {"x": 2, "amplitude": None, "phase": None},
            {"x": 3, "amplitude": 2, "phase": 20}], "display_gaps": 1})
        self.assertTrue(np.isnan(plots.amplitude_line.yData[1]))
        self.assertEqual(plots.amplitude_line.opts["connect"], "finite")
        points = [{"carrier_hz": 2.4e9, "amplitude_dB": -40, "repeat": 1, "point": 1},
                  {"carrier_hz": 2.4e9, "amplitude_dB": -39, "repeat": 2, "point": 1}]
        plots.update_snapshot({"mode": "sweep", "points": points})
        self.assertEqual(len(plots.sweep_items), 2)
        plots.update_snapshot({"mode": "sweep", "points": points[:1]})
        self.assertEqual(set(plots.sweep_items), {1})
        plots.close()

    def test_every_diagnostic_model_type_renders_and_switches(self):
        plot = DiagnosticPlot()
        common = {"title": "诊断验证", "xlabel": "时间", "ylabel": "幅度", "note": "真实数据格式",
                  "series": [{"label": "测试", "data": [[0, 1], [1, 2]], "errors": [.1, .2]}]}
        models = [dict(common, kind=kind) for kind in ("line", "bar", "scatter")]
        models += [dict(common, kind="heatmap", x=[0, 1], y=[1, 2], z=[[1, None], [2, 3]], colorbar="PSD"),
                   dict(common, kind="segments", segments=[{"start": 0, "end": 2, "label": "参考", "contact": False}]),
                   dict(common, kind="line", vertical=[{"x": .5, "label": "单音"}], horizontal=[{"y": 1, "label": "门槛"}])]
        for model in models:
            plot.display(model)
            self.app.processEvents()
            self.assertEqual(plot.model["kind"], model["kind"])
        plot.close()

    def test_close_requires_active_confirmation_and_waits_without_freezing(self):
        service = FakeService()
        window = MainWindow(service)
        window.show()
        self.pump_until(lambda: window.profile.count() == 1)
        service.job = {"status": "running", "config": {"mode": "phase"}}
        window._apply_status(service.snapshot())
        with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.No):
            window.close()
        self.assertTrue(window.isVisible())
        self.assertFalse(service.closed)
        release = threading.Event()
        window.runner.submit("analysis_in_progress", lambda: release.wait(2))
        with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            window.close()
        self.pump_until(lambda: service.closed)
        self.assertTrue(window.isVisible())
        ticked = []
        QtCore.QTimer.singleShot(10, lambda: ticked.append(True))
        self.pump_until(lambda: bool(ticked), .5)
        release.set()
        self.pump_until(lambda: not window.isVisible())

    def test_real_replay_seek_force_refreshes_preview_and_shows_timing_note(self):
        from sspp_desktop.config import Settings
        from sspp_desktop.services import DesktopService
        service = DesktopService(Settings.from_env(Path(__file__).resolve().parents[1]))
        window = MainWindow(service)
        window.timer.stop()
        window.show()
        try:
            for mode in ("phase", "sweep"):
                self.pump_until(lambda: not window.runner.pending and not window._navigation_blocked())
                self.assertTrue(window.select_experiment("finger" if mode == "phase" else "material"), window.notice.text())
                self.pump_until(lambda: not window.runner.pending)
                window.open_archive()
                self.pump_until(lambda: bool(window.history_rows) and not window.runner.pending)
                index = next(i for i, row in enumerate(window.history_rows) if row["mode"] == mode and row["status"] == "complete")
                window.history_table.selectRow(index)
                self.pump_until(lambda: window.selected_details is not None)
                window.play_replay()
                self.pump_until(lambda: window.replay is not None)
                window.pause_replay()
                window.seek_replay(700)
                self.assertGreater(len(window.preview_plots.i_line.xData), 0)
                self.assertFalse(window.replay_running)
                self.assertEqual(window.replay_note.text(), window.replay.timing_note)
                self.assertTrue(window.replay_note.isVisible())
                self.assertIn("历史回放", window.view_banner.text())
                self.assertIsNone(service.proc)
            window.return_live()
            self.assertIsNone(window.replay)
        finally:
            window.close()
            self.pump_until(lambda: not window.isVisible())

    def test_real_history_diagnostic_coverage(self):
        from sspp_desktop.config import Settings
        from sspp_desktop.services import DesktopService
        service = DesktopService(Settings.from_env(Path(__file__).resolve().parents[1]))
        plot = DiagnosticPlot()
        covered = set()
        for mode in ("phase", "sweep"):
            candidates = [row for row in service.history() if row["mode"] == mode and row["status"] == "complete"]
            if not candidates:
                continue
            model = service.diagnostic_view(candidates[0]["key"])
            for charts in model["groups"].values():
                for chart in charts:
                    plot.display(chart)
                    self.app.processEvents()
                    covered.add(chart["kind"])
        self.assertTrue({"line", "scatter", "heatmap", "bar", "segments"}.issubset(covered), covered)
        plot.close()
        service.shutdown()


if __name__ == "__main__":
    unittest.main()
