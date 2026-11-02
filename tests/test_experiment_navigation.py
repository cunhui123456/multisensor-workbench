"""四类实验全局导航、参数隔离与采集互锁；仅使用 fake 服务。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from copy import deepcopy
import time
import unittest
from unittest.mock import PropertyMock, patch

from PySide6 import QtCore, QtWidgets
from sspp_desktop.window import MainWindow
from sspp_desktop.temperature_widget import TemperaturePressureWidget
from qt_test_cleanup import dispose_closed_windows
import test_desktop_ui as desktop_fixtures
import test_liquid_temperature_ui as liquid_fixtures


class NavigationService(desktop_fixtures.FakeService, liquid_fixtures.FakeService):
    def __init__(self):
        desktop_fixtures.FakeService.__init__(self)
        liquid_fixtures.FakeService.__init__(self)
        self.rows = [
            {"key": "runs/material", "category": "material_sweep", "mode": "sweep"},
            {"key": "teacher_runs/teacher", "category": "teacher_sweep", "mode": "teacher"},
            {"key": "phase_runs/finger", "category": "finger_displacement", "mode": "phase"},
            {"key": "phase_runs/legacy", "category": "phase_history", "mode": "phase"},
            {"key": "runs/liquid", "category": "liquid_temperature_sweep", "mode": "sweep"},
            {"key": "phase_runs/liquid", "category": "liquid_temperature_phase", "mode": "phase"},
        ]
        for row in self.rows:
            row.update(label=row["key"], status="complete", sensor="SSPP", started_utc="2026-10-27T00:00:00Z")

    def history(self):
        return deepcopy(self.rows)


def liquid_request(mode="sweep"):
    return {"mode": mode, "liquid_temperature": {
        "liquid": "water", "sample_id": "sample_1", "container": "glass_1", "geometry": "fixed_jig",
        "thermometer": "reference_1", "repeat_id": "cycle_1", "target_temperature_c": 30.,
        "temperature_c": 29.8, "volume_ml": 20., "distance_mm": 10.}}


class ExperimentNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.service = NavigationService()
        self.window = MainWindow(self.service)
        self.window.timer.stop()
        self.window.show()
        self.pump(lambda: self.window.profile.count() == 1 and not self.window.runner.pending)

    def tearDown(self):
        self.service.preflight_release.set()
        self.service.stop()
        self.window._start_pending = False
        self.window._close_requested = False
        self.window._apply_status(self.service.snapshot())
        self.pump(lambda: not self.window.runner.pending and not self.window.liquid_workspace.is_busy)
        self.window.close()
        self.pump(lambda: self.service.closed and not self.window.isVisible())
        dispose_closed_windows()

    def pump(self, predicate, timeout=4):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Qt navigation condition timed out")

    def select(self, key):
        self.assertTrue(self.window.select_experiment(key), self.window.notice.text())
        self.pump(lambda: not self.window.liquid_workspace.is_busy and not self.window.runner.pending)
        self.app.processEvents()

    def test_four_top_navigation_items_and_extracted_settings_belong_to_sidebar(self):
        window = self.window
        self.assertEqual(window.experiment_id, "material")
        self.assertEqual(window.experiment_nav.currentIndex(), 0)
        self.assertEqual(window.config()["mode"], "sweep")
        self.assertEqual([window.experiment_nav.tabData(i) for i in range(window.experiment_nav.count())],
                         ["material", "finger", "liquid", "temperature", "temperature_calibration", "temperature_sweep"])
        self.assertEqual([window.experiment_nav.tabText(i) for i in range(window.experiment_nav.count())],
                         ["材料扫频", "手指位移", "液体温度影响", "温压采集", "温度标定", "SSPP 温度扫频"])
        self.assertLess(window.experiment_nav.mapTo(window, QtCore.QPoint()).y(),
                        window.sidebar.mapTo(window, QtCore.QPoint()).y())
        self.assertGreater(window.experiment_nav.width(), window.sidebar.width())
        for widget in (window.finger_settings, window.liquid_settings, window.temperature_settings):
            self.assertTrue(window.sidebar.isAncestorOf(widget))
        for key, visible in (("material", None), ("finger", window.finger_settings),
                             ("liquid", window.liquid_settings), ("temperature", window.temperature_settings)):
            with self.subTest(experiment=key):
                self.select(key)
                self.assertTrue(window.sidebar.isVisible())
                for widget in (window.finger_settings, window.liquid_settings, window.temperature_settings):
                    self.assertEqual(not widget.isHidden(), widget is visible)
                self.assertEqual(not window.rf_actions.isHidden(), key != "temperature")
        self.assertIsNone(self.service.started)

    def test_rf_drafts_and_liquid_metadata_are_isolated_and_confirmations_reset(self):
        self.select("finger")
        window = self.window
        window.fields["label"].setText("finger_draft")
        window.fields["rx_gain"].setValue(33.)
        window.fields["carrier_ghz"].setValue(2.7)
        window.confirm.setChecked(True)
        self.select("material")
        self.assertFalse(window.confirm.isChecked())
        self.assertEqual(window.config()["mode"], "sweep")
        window.material_workspace.material_name.setText("empty_glass")
        window.fields["rx_gain"].setValue(41.)
        window.fields["freq_start_ghz"].setValue(2.5)
        window.confirm.setChecked(True)
        self.select("liquid")
        self.assertFalse(window.confirm.isChecked())
        window._prepare_liquid(liquid_request("phase"))
        window.fields["rx_gain"].setValue(52.)
        window.fields["carrier_ghz"].setValue(2.8)
        liquid_name = window.config()["label"]
        window.confirm.setChecked(True)
        self.select("finger")
        self.assertEqual(window.config()["label"], "finger_draft")
        self.assertEqual(window.config()["rx_gain"], 33.)
        self.assertEqual(window.config()["carrier_ghz"], 2.7)
        self.assertIsNone(window.config()["liquid_temperature"])
        self.assertFalse(window.confirm.isChecked())
        self.select("material")
        self.assertEqual(window.config()["label"], "empty_glass")
        self.assertEqual(window.config()["rx_gain"], 41.)
        self.assertEqual(window.config()["freq_start_ghz"], 2.5)
        self.assertIsNone(window.config()["liquid_temperature"])
        self.select("liquid")
        self.assertEqual(window.config()["label"], liquid_name)
        self.assertEqual(window.config()["rx_gain"], 52.)
        self.assertEqual(window.config()["carrier_ghz"], 2.8)
        self.assertEqual(window.config()["liquid_temperature"]["temperature_c"], 29.8)
        self.assertFalse(window.confirm.isChecked())

    def test_serial_draft_preserved_but_confirmation_does_not_survive_navigation(self):
        self.select("temperature")
        widget = self.window.temperature_workspace
        widget.sample_name.setText("serial_sample")
        widget.confirm.setChecked(True)
        self.select("material")
        self.select("temperature")
        self.assertEqual(widget.sample_name.text(), "serial_sample")
        self.assertFalse(widget.confirm.isChecked())

    def test_archive_always_scoped_to_current_experiment(self):
        expected = {"material": {"material_sweep", "teacher_sweep"},
                    "finger": {"finger_displacement", "phase_history"},
                    "liquid": {"liquid_temperature_sweep", "liquid_temperature_phase"},
                    "temperature": set(), "temperature_calibration": set(), "temperature_sweep": set()}
        for key, categories in expected.items():
            with self.subTest(experiment=key):
                self.select(key)
                self.assertFalse(hasattr(self.window, "history_scope"))
                self.assertEqual({r["category"] for r in self.window.history_rows}, categories)
                self.window.open_archive()
                self.pump(lambda: 'history' not in self.window.runner.pending)
                self.assertEqual({r['category'] for r in self.window.history_rows}, categories)
                allowed = {self.window.history_type.itemData(i) for i in range(self.window.history_type.count())}
                from sspp_desktop.window import EXPERIMENTS
                self.assertLessEqual(allowed - {''}, EXPERIMENTS[key][2])
                self.assertEqual(self.window.archive_button.text(), '实验档案')
                self.assertEqual(self.window.tabs.tabText(1), '实验档案')
                self.assertIs(self.window.tabs.currentWidget(), self.window.history_table.parentWidget())

    def test_rf_busy_navigation_rejects_direct_handler_and_tab_change(self):
        self.select("finger")
        window = self.window
        for status in ("starting", "running", "stopping", "saving"):
            with self.subTest(status=status):
                self.service.job = {"status": status, "config": {"mode": "phase"}}
                window._apply_status(self.service.snapshot())
                original = window.config()
                self.assertFalse(window.select_experiment("material"))
                window.experiment_nav.setCurrentIndex(0)
                self.assertEqual(window.experiment_id, "finger")
                self.assertEqual(window.experiment_nav.tabData(window.experiment_nav.currentIndex()), "finger")
                self.assertEqual(window.config(), original)
                self.assertTrue(window.stop_button.isEnabled())
        self.assertIsNone(self.service.started)

    def test_pending_preflight_cannot_be_bypassed_by_navigation(self):
        self.select("finger")
        window = self.window
        window.fields["carrier_ghz"].setValue(2.75)
        window.preflight()
        self.pump(lambda: "preflight" in window.runner.pending)
        self.assertFalse(window.select_experiment("material"))
        window.experiment_nav.setCurrentIndex(2)
        self.assertEqual(window.experiment_id, "finger")
        self.assertEqual(window.fields["carrier_ghz"].value(), 2.75)
        self.service.preflight_release.set()
        self.pump(lambda: "preflight" not in window.runner.pending)
        self.select("material")
        self.assertIsNone(self.service.started)

    def test_serial_busy_navigation_and_global_stop_route_to_serial(self):
        self.select("temperature")
        window = self.window
        with patch.object(TemperaturePressureWidget, "is_running", new_callable=PropertyMock, return_value=True), \
                patch.object(window.temperature_workspace, "stop_capture") as stop:
            window._temperature_busy_changed(True)
            self.assertFalse(window.select_experiment("finger"))
            window.experiment_nav.setCurrentIndex(1)
            self.assertEqual(window.experiment_id, "temperature")
            self.assertEqual(window.experiment_nav.currentIndex(), 3)
            self.assertTrue(window.stop_button.isEnabled())
            window.tabs.setCurrentIndex(window.analysis_tab)
            self.assertTrue(window.stop_button.isVisible())
            window.stop_button.click()
            stop.assert_called_once_with()
            self.assertFalse(self.service.stopped)
        window._temperature_busy_changed(False)

    def test_global_stop_remains_visible_on_each_secondary_page(self):
        self.select("finger")
        window = self.window
        self.service.job = {"status": "running", "config": {"mode": "phase"}}
        window._apply_status(self.service.snapshot())
        for index in range(window.tabs.count()):
            if window.tabs.isTabVisible(index):
                with self.subTest(tab=window.tabs.tabText(index)):
                    window.tabs.setCurrentIndex(index)
                    self.app.processEvents()
                    self.assertTrue(window.sidebar.isVisible())
                    self.assertTrue(window.stop_button.isVisible())
                    self.assertTrue(window.stop_button.isEnabled())
        window.stop_button.click()
        self.pump(lambda: self.service.stopped and "stop" not in window.runner.pending)

    def test_liquid_without_applied_setup_cannot_start_even_if_confirmed(self):
        self.select("liquid")
        window = self.window
        window.material_workspace.material_name.setText("must_not_bypass")
        window.confirm.setChecked(True)
        window.start_experiment()
        self.assertIsNone(self.service.started)
        self.assertIn("应用", window.notice.text())
        self.assertFalse(window._start_pending)

    def test_reset_affects_only_current_rf_experiment_draft(self):
        self.select("finger")
        window = self.window
        window.fields["rx_gain"].setValue(34.)
        window.fields["carrier_ghz"].setValue(2.75)
        self.select("material")
        window.material_workspace.material_name.setText("sample")
        window.fields["rx_gain"].setValue(45.)
        window.fields["freq_start_ghz"].setValue(2.5)
        self.select("finger")
        window.reset_parameters()
        self.assertEqual(window.fields["carrier_ghz"].value(), 2.4)
        self.select("material")
        self.assertEqual(window.fields["rx_gain"].value(), 45.)
        self.assertEqual(window.fields["freq_start_ghz"].value(), 2.5)
        self.assertEqual(window.config()["label"], "sample")

    def test_completed_liquid_poll_cannot_pollute_finger_state_or_plots(self):
        self.select("finger")
        window = self.window
        liquid_key = "phase_runs/completed_liquid"
        snapshot = {"job": {"status": "complete", "config": liquid_request("phase"),
                            "output": "Data/" + liquid_key, "rf_stopped": True}, "logs": [], "events": []}
        live = {"mode": "phase", "points": [{"x": 10., "phase": 99., "amplitude": 1.}],
                "preview": None, "revision": 17}
        research = {"run_key": liquid_key, "annotations": [], "pending": None}

        def assert_finger_empty():
            self.assertIsNone(window.finger_workspace.run_key)
            self.assertIsNone(window.finger_workspace.source_key)
            self.assertEqual(window.finger_workspace.records, [])
            for curve in (window.finger_workspace.interval_line, window.live_plots.phase_line):
                values = curve.yData
                self.assertTrue(values is None or len(values) == 0)
            self.assertEqual(window.status_label.text(), "待开始")

        window._poll_done((snapshot, live, research))
        assert_finger_empty()
        self.select("liquid")
        window._poll_done((snapshot, live, research))
        self.assertEqual(list(window.live_plots.phase_line.yData), [99.])
        self.assertIsNone(window.finger_workspace.run_key)
        self.select("finger")
        window._poll_done((snapshot, live, research))
        assert_finger_empty()

    def test_finger_sampling_controls_remain_available_while_rf_config_locked(self):
        self.select("finger")
        window = self.window
        widget = window.finger_workspace
        self.assertFalse(window.config_container.isAncestorOf(window.finger_settings))
        self.service.job = {"status": "running", "config": {"mode": "phase"}, "baseline": {"end_s": 7}}
        window._apply_status(self.service.snapshot())
        widget.apply_snapshot({"run_key": "phase_runs/finger", "annotations": [], "pending": None})
        self.assertFalse(window.config_container.isEnabled())
        self.assertFalse(window.fields["carrier_ghz"].isEnabled())
        self.assertTrue(window.finger_settings.isEnabled())
        for control in (widget.initial_distance, widget.settle, widget.hold, widget.method):
            self.assertTrue(control.isEnabled())
        widget.apply_snapshot({"run_key": "phase_runs/finger", "annotations": [],
                               "pending": {"id": "window_1", "state": "capturing", "distance_mm": 10., "role": "calibration"}})
        self.assertFalse(widget.method.isEnabled())
        self.assertFalse(window.config_container.isEnabled())
        self.assertTrue(window.stop_button.isEnabled())
        for control in (widget.initial_distance, widget.settle, widget.hold):
            self.assertTrue(control.isEnabled())
        widget.apply_snapshot({"run_key": "phase_runs/finger", "annotations": [], "pending": None})
        self.assertTrue(widget.method.isEnabled())

    def test_editing_liquid_temperature_invalidates_applied_setup_and_confirmation(self):
        self.select("liquid")
        window = self.window
        window._prepare_liquid(liquid_request())
        self.assertIsNotNone(window._liquid_setup)
        window.confirm.setChecked(True)
        window.liquid_workspace.fields["temperature_c"].setText("30.1")
        self.assertIsNone(window._liquid_setup)
        self.assertIsNone(window.config()["liquid_temperature"])
        self.assertFalse(window.confirm.isChecked())
        self.assertEqual(window.experiment_id, "liquid")
        window.confirm.setChecked(True)
        window.start_experiment()
        self.assertIsNone(self.service.started)
        self.assertIn("应用", window.notice.text())

    def test_temperature_calibration_is_serial_only_and_reuses_verified_temperature_workflow(self):
        self.select("temperature_calibration")
        workspace = self.window.temperature_workspace
        self.assertTrue(workspace.calibration_mode)
        self.assertEqual(workspace.protocol.currentData(), "temperature_v1")
        self.assertFalse(workspace.protocol.isEnabled())
        self.assertFalse(self.window.rf_actions.isVisible())
        self.assertTrue(self.window.temperature_settings.isVisible())
        self.assertTrue(workspace.plateau_controls.isVisible())
        self.assertFalse(hasattr(workspace,"ramp_group"))
        self.assertFalse(hasattr(workspace,"endpoint_button"))
        self.assertEqual(self.window._environment_target()["kind"], "serial")
        self.assertEqual(self.window.tabs.currentIndex(), self.window.temperature_tab)
        self.select("temperature")
        self.assertFalse(workspace.calibration_mode)
        self.assertFalse(workspace.plateau_controls.isVisible())
        self.assertTrue(workspace.protocol.isEnabled())


if __name__ == "__main__":
    unittest.main()
