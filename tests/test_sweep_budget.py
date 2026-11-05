"""无总时限的扫频表单、旧草稿兼容与人工停止；仅使用 fake 服务。"""
from copy import deepcopy
import unittest

from PySide6 import QtWidgets
import test_experiment_navigation as fixtures
from test_temperature_ui import FakePreparation


class UnlimitedSweepWindowTests(unittest.TestCase):
    setUpClass = fixtures.ExperimentNavigationTests.__dict__['setUpClass']
    setUp = fixtures.ExperimentNavigationTests.setUp
    tearDown = fixtures.ExperimentNavigationTests.tearDown
    pump = fixtures.ExperimentNavigationTests.pump
    select = fixtures.ExperimentNavigationTests.select

    def assert_unlimited(self):
        window = self.window
        self.assertNotIn('max_seconds', window.fields)
        self.assertFalse(hasattr(window, 'auto_sweep_budget'))
        self.assertFalse(hasattr(window, 'sweep_budget_hint'))
        self.assertIsNone(window.config()['max_seconds'])
        visible_text = '\n'.join(widget.text() for widget in
                                 window.sweep_group.findChildren(QtWidgets.QLabel))
        # The concise hint may state that no total limit exists; no total-limit control/config is allowed.
        self.assertNotIn('自动预留', visible_text)
        self.assertNotIn('总时限', window.help_dialog.body.toPlainText())

    def ready(self, serial=False):
        for kind in ('usrp', 'serial') if serial else ('usrp',):
            self.service.initialize_environment(self.window._environment_target(kind))
        if serial:
            panel = self.window.temperature_workspace
            panel.protocol.setCurrentIndex(panel.protocol.findData('temperature_v1'))
            prepared = FakePreparation()
            panel.preparation = prepared
            self.window._set_active(False)
            prepared.start(self.window.temperature_sweep_workspace.port.currentData(), 'temperature_v1', upload=False)
            prepared.finish()
        self.window._set_active(False)

    def start_and_stop(self):
        window = self.window
        self.assertFalse(hasattr(window, 'confirm'))
        window.start_button.click()
        self.pump(lambda: self.service.started is not None and not window.runner.pending)
        self.assertIsNone(self.service.started['max_seconds'])
        self.assertTrue(window.stop_button.isEnabled())
        self.assertFalse(window.config_container.isEnabled())
        window.stop_button.click()
        self.pump(lambda: self.service.stopped and not window.runner.pending)
        self.assertEqual(self.service.job['status'], 'stopped')
        self.assertTrue(self.service.job['rf_stopped'])

    def test_finite_sweep_keeps_round_count_and_point_timeout(self):
        window = self.window
        window.material_workspace.material_name.setText('测试材料')
        window.fields['points'].setValue(40)
        window.fields['repeat'].setValue(2)
        window.fields['point_timeout'].setValue(12)
        self.assert_unlimited()
        self.assertEqual((window.config()['points'], window.config()['repeat'],
                          window.config()['point_timeout']), (40, 2, 12))
        self.assertTrue(window.fields['repeat'].isEnabled())
        self.assertIn('指定轮数', window.sweep_completion_hint.text())
        self.ready()
        self.start_and_stop()

    def test_changed_sweep_parameters_preserve_device_readiness_without_confirmation(self):
        self.ready()
        for key, value in [('points', 40), ('repeat', 2), ('point_timeout', 12)]:
            with self.subTest(parameter=key):
                self.window.fields[key].setValue(value)
                self.assertFalse(hasattr(self.window, 'confirm'))
                self.assertTrue(self.window._environment_ready('usrp'))
                self.assertTrue(self.window.config()['setup_confirmed'])
                self.assert_unlimited()

    def test_old_draft_limits_are_ignored_across_navigation_and_reset(self):
        window = self.window
        window.material_workspace.material_name.setText('legacy_draft_sample')
        draft = deepcopy(window._capture_draft())
        draft['values'].update(max_seconds=90, auto_sweep_budget=False, points=40, repeat=2)
        draft['auto_sweep_budget'] = False
        window._restore_draft(draft)
        self.assertEqual(window.config()['points'], 40)
        self.assert_unlimited()
        self.assertNotIn('max_seconds', window._capture_draft()['values'])
        self.assertNotIn('auto_sweep_budget', window._capture_draft())
        self.select('temperature_sweep')
        window.temperature_sweep_workspace.reference_temperature.setText('50')
        window.fields['points'].setValue(30)
        self.assert_unlimited()
        self.select('finger')
        self.select('material')
        self.assertEqual((window.config()['points'], window.config()['repeat']), (30, 2))
        self.assertEqual(window.config()['temperature_sweep']['reference_temperature_c'], 50)
        window.reset_parameters()
        self.assertEqual(window.config()['points'], 30)
        self.assertEqual(window.config()['point_timeout'], 8)
        self.assert_unlimited()
        self.select('temperature_sweep')
        self.assertEqual(window.config()['points'], 30)
        self.assert_unlimited()

    def test_continuous_sweep_ignores_round_count_and_keeps_manual_stop(self):
        self.select('temperature_sweep')
        window = self.window
        window.material_workspace.material_name.setText('continuous_sample')
        panel = window.temperature_sweep_workspace
        window.sweep_options.temperature_mode.setCurrentIndex(2)
        window._joint_port_changed('/dev/cu.usbserial_TEST')
        window.fields['repeat'].setValue(1)
        self.assertEqual(window.config()['temperature_sweep']['mode'], 'continuous')
        self.assertFalse(window.fields['repeat'].isEnabled())
        self.assertIn('停止并保存', window.sweep_completion_hint.text())
        self.assert_unlimited()
        self.ready(serial=True)
        self.start_and_stop()

    def test_manual_temperature_sweep_retains_finite_rounds(self):
        self.select('temperature_sweep')
        window = self.window
        window.material_workspace.material_name.setText('manual_sample')
        panel = window.temperature_sweep_workspace
        window.sweep_options.temperature_mode.setCurrentIndex(2)
        window.sweep_options.temperature_mode.setCurrentIndex(1)
        panel.reference_temperature.setText('50')
        window.fields['repeat'].setValue(5)
        self.assertEqual(window.config()['temperature_sweep']['mode'], 'manual')
        self.assertEqual(window.config()['repeat'], 5)
        self.assertTrue(window.fields['repeat'].isEnabled())
        self.assertIn('指定轮数', window.sweep_completion_hint.text())
        self.assert_unlimited()
        self.ready()
        self.start_and_stop()

    def test_phase_recording_duration_is_preserved(self):
        self.select('finger')
        window = self.window
        self.assertTrue(window.phase_group.isVisible())
        window.fields['duration'].setValue(45)
        self.assertEqual(window.config()['duration'], 45)
        window.reset_parameters()
        self.assertEqual(window.config()['duration'], 120)
        self.assert_unlimited()
