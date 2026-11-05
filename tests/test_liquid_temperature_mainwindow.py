"""主窗液体温度集成：仅 fake 服务；准备条件不打开硬件。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from copy import deepcopy
import threading
import time
import unittest

from PySide6 import QtCore, QtWidgets
from test_desktop_ui import FakeService
from qt_test_cleanup import dispose_closed_windows
from sspp_desktop.window import MainWindow
from sspp_lab.controller import DEFAULTS
from sspp_experiments.comparison import history_category


METADATA = {"liquid": "水", "sample_id": "sample-A", "container": "glass-A", "volume_ml": 10.,
            "geometry": "瓶底平行，线缆固定", "thermometer": "参考温度计A", "target_temperature_c": 30.,
            "temperature_c": 29.8, "distance_mm": 10., "repeat_id": "r1"}


class LiquidFakeService(FakeService):
    def __init__(self):
        super().__init__()
        self.liquid_load_calls = []
        self.finger_load_calls = []

    def liquid_history(self):
        return []

    def liquid_load(self, key):
        self.liquid_load_calls.append(key)
        return {"key": key, "mode": "phase" if key.startswith("phase_runs/") else "sweep",
                "metadata": deepcopy(METADATA), "annotations": [], "analysis": None}

    def finger_load(self, key):
        self.finger_load_calls.append(key)
        raise ValueError("液体温度记录不应到达手指加载器")


class LiquidTemperatureMainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.service = LiquidFakeService()
        self.window = MainWindow(self.service)
        self.window.show()
        self.window.timer.stop()
        self.release = threading.Event()
        self.pump(lambda: self.window.profile.count() == 1 and not self.window.runner.pending)

    def tearDown(self):
        self.release.set()
        self.service.preflight_release.set()
        if (self.window.snapshot.get("job") or {}).get("status") in ("starting", "running", "stopping", "saving"):
            self.window._apply_status({})
        self.window.close()
        self.pump(lambda: self.service.closed and not self.window.isVisible())
        self.pump(lambda: not self.window.runner.pending and not self.window.liquid_workspace.is_busy)
        dispose_closed_windows()

    def pump(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("主窗口液体温度集成等待超时")

    def prepare(self, mode="phase"):
        self.assertTrue(self.window.select_experiment('finger' if mode=='phase' else 'material'))
        self.pump(lambda:not self.window.runner.pending and not self.window.liquid_workspace.is_busy)
        if mode=='phase':self.window.fields['label'].setText('liquid_sample')
        else:self.window.material_workspace.material_name.setText('liquid_sample')
        for key,value in METADATA.items():
            if key in self.window.liquid_workspace.fields:
                self.window.liquid_workspace.fields[key].setText(str(value))
        self.window.sweep_options.liquid_enabled.setChecked(True)

    def test_direct_fields_set_metadata_without_apply_or_hardware_action(self):
        self.prepare()
        config = self.window.config()
        self.assertEqual(config['mode'],'phase')
        self.assertEqual(config['condition'],'material')
        self.assertEqual(config['liquid_temperature']['temperature_c'],29.8)
        self.assertEqual(config['liquid_temperature']['temperature_source'],'operator_reference_thermometer')
        self.assertFalse(config['setup_confirmed'])
        self.assertIsNone(self.service.started)
        self.assertFalse(self.service.stopped)
        self.assertFalse(self.window.tabs.isTabEnabled(self.window.finger_tab))
        self.assertIn('液体',self.window.start_button.text())
        config['liquid_temperature']['temperature_c']=99.
        self.assertEqual(self.window.config()['liquid_temperature']['temperature_c'],29.8)
        self.window.start_experiment()
        self.assertIsNone(self.service.started)
        self.assertIn('初始化',self.window.notice.text())

    def test_optional_liquid_group_can_be_disabled_without_leaking_metadata(self):
        self.assertEqual(set(self.window.config()),set(DEFAULTS)|{'profile'})
        self.assertIsNone(self.window.config()['liquid_temperature'])
        self.prepare('sweep')
        self.assertTrue(self.window.config()['liquid_temperature'])
        self.window.sweep_options.liquid_enabled.setChecked(False)
        self.assertIsNone(self.window.config()['liquid_temperature'])
        self.assertEqual(self.window.experiment_id,'material')
        self.assertNotIn('应用',self.window.start_button.text())
        self.assertFalse(hasattr(self.window,'confirm'))

    def test_phase_and_sweep_optional_condition_drafts_remain_independent(self):
        self.prepare('phase')
        self.window.liquid_workspace.fields['temperature_c'].setText('31.2')
        self.assertTrue(self.window.select_experiment('material'))
        self.pump(lambda:not self.window.runner.pending and not self.window.liquid_workspace.is_busy)
        self.assertIsNone(self.window.config()['liquid_temperature'])
        self.assertTrue(self.window.select_experiment('finger'))
        self.pump(lambda:not self.window.runner.pending and not self.window.liquid_workspace.is_busy)
        self.assertEqual(self.window.config()['liquid_temperature']['temperature_c'],31.2)
        self.assertIsNone(self.window.config()['temperature_sweep'])
        self.assertFalse(self.window.tabs.isTabEnabled(self.window.finger_tab))
        self.assertEqual(self.window.experiment_id,'finger')
        self.assertNotIn('应用',self.window.start_button.text())

    def test_completed_liquid_run_preserves_form_and_loads_saved_record(self):
        self.prepare()
        config=self.window.config()
        running={'job':{'status':'running','config':config,'baseline':{'end_s':7.},'output':'Data/phase_runs/temperature-test'}}
        self.window._apply_status(deepcopy(running))
        self.assertFalse(self.window.config_container.isEnabled())
        done=deepcopy(running);done['job'].update(status='complete',rf_stopped=True)
        self.window._apply_status(done)
        self.pump(lambda:not self.window.runner.pending and not self.window.liquid_workspace.is_busy)
        self.assertEqual(self.window.config()['liquid_temperature']['temperature_c'],29.8)
        self.assertEqual(self.service.liquid_load_calls,['phase_runs/temperature-test'])
        self.assertEqual(self.window.liquid_workspace.current_key,'phase_runs/temperature-test')
        self.assertIsNone(self.service.started)

    def test_temperature_history_category_and_finger_action_are_separate(self):
        rows = [
            {"key": "phase_runs/liquid", "mode": "phase", "experiment": "liquid_temperature_phase", "label": "液体相位"},
            {"key": "runs/liquid", "mode": "sweep", "experiment": "liquid_temperature_sweep", "label": "液体扫频"},
            {"key": "phase_runs/finger", "mode": "phase", "experiment": "finger_displacement", "label": "手指"},
        ]
        for row in rows:
            row.update(category=history_category(row), status="complete", started_utc="2026-10-27T00:00:00Z")
        self.assertEqual([row["category"] for row in rows],
                         ["liquid_temperature_phase", "liquid_temperature_sweep", "finger_displacement"])
        self.assertTrue(self.window.select_experiment("finger"))
        self.window.open_archive()
        self.pump(lambda: not self.window.runner.pending and not self.window.liquid_workspace.is_busy)
        self.window._history_all_rows = rows
        self.window.history_type.setCurrentIndex(self.window.history_type.findData("liquid_temperature_phase"))
        self.window._render_history_rows()
        self.assertEqual([r["key"] for r in self.window.history_rows], ["phase_runs/liquid"])
        self.assertIn("液体温度", self.window.history_table.item(0, 3).text())
        checkbox = self.window.history_table.item(0, 0)
        checkbox.setCheckState(QtCore.Qt.CheckState.Checked)
        self.assertEqual(self.window.comparison_keys, {'phase_runs/liquid'})
        self.assertFalse(self.window.compare_button.isEnabled())
        self.window.compare_selected()
        self.assertIn("液体", self.window.notice.text())
        self.window.selected_details = {"key": "phase_runs/liquid", "context": {"experiment": "liquid_temperature_phase"}}
        self.window._history_buttons(True)
        self.assertFalse(self.window.finger_history_button.isEnabled())
        self.window._finger_load_history()
        self.pump(lambda: not self.window.runner.pending)
        self.assertEqual(self.service.finger_load_calls, [])
        self.window.selected_details = {"key": "phase_runs/finger", "context": {"experiment": "finger_displacement"}}
        self.window._history_buttons(True)
        self.assertTrue(self.window.finger_history_button.isEnabled())

    def test_close_waits_for_liquid_background_task_and_releases_scenes(self):
        from shiboken6 import isValid
        import pyqtgraph as pg
        self.window.liquid_workspace._submit("delayed", lambda: self.release.wait(3), lambda value: None)
        self.assertTrue(self.window.liquid_workspace.is_busy)
        plots = self.window.findChildren(pg.PlotWidget)
        self.window.close()
        self.pump(lambda: self.service.closed)
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window._auxiliary_shutdown_done)
        self.assertTrue(self.window.liquid_workspace.runner.closing)
        self.release.set()
        self.pump(lambda: not self.window.isVisible())
        self.assertFalse(self.window.liquid_workspace.is_busy)
        self.assertTrue(all(not isValid(plot) or plot.plotItem is None for plot in plots))


if __name__ == "__main__":
    unittest.main()
