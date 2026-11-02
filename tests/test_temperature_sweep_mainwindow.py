"""Joint navigation, dual-device gating and visible live voltage, without hardware."""
from copy import deepcopy
import unittest
from PySide6 import QtCore
import test_experiment_navigation as fixtures


class JointMainWindowTests(unittest.TestCase):
    setUpClass = fixtures.ExperimentNavigationTests.__dict__['setUpClass']
    setUp = fixtures.ExperimentNavigationTests.setUp
    tearDown = fixtures.ExperimentNavigationTests.tearDown
    pump = fixtures.ExperimentNavigationTests.pump
    select = fixtures.ExperimentNavigationTests.select

    def configure(self):
        self.select('temperature_sweep')
        self.window.temperature_sweep_workspace.experiment_mode.setCurrentIndex(1)
        self.window.temperature_sweep_workspace.set_port('/dev/cu.usbserial_TEST')
        return self.window.config()

    def ready(self):
        for kind in ('usrp','serial'):
            self.service.initialize_environment(self.window._environment_target(kind))
        self.window._set_active(False)

    def test_defaults_drafts_reset_and_primary_dual_view(self):
        cfg = self.configure()
        self.assertEqual((cfg['freq_start_ghz'],cfg['freq_stop_ghz'],cfg['points']), (5.,5.3,20))
        self.assertEqual(cfg['temperature_sweep']['channel'],'S6')
        self.assertIsNone(cfg['temperature_sweep']['calibration'])
        self.assertIs(self.window.tabs.currentWidget(),self.window.temperature_sweep_workspace)
        self.assertTrue(self.window.sidebar.isAncestorOf(self.window.temperature_sweep_settings))
        self.window.fields['points'].setValue(25)
        self.select('material')
        self.assertEqual(self.window.config()['freq_start_ghz'],2.4)
        self.assertIsNone(self.window.config()['temperature_sweep'])
        self.select('temperature_sweep')
        self.assertEqual(self.window.config()['points'],25)
        self.window.reset_parameters()
        self.assertEqual(self.window.config()['points'],20)
        self.assertEqual(self.window.config()['freq_start_ghz'],5.)
        self.assertFalse(self.window.confirm.isChecked())
        self.assertFalse(self.service.initialize_calls)

    def test_requires_both_devices_and_locks_changes_during_joint_run(self):
        self.configure()
        self.service.initialize_environment(self.window._environment_target('usrp'))
        self.window._set_active(False)
        self.assertFalse(self.window.start_button.isEnabled())
        self.window.confirm.setChecked(True)
        self.window.start_experiment()
        self.assertIsNone(self.service.started)
        self.assertIn('串口',self.window.notice.text())
        self.ready()
        self.window.confirm.setChecked(True)
        self.window.start_experiment()
        self.pump(lambda:self.service.started is not None and not self.window.runner.pending)
        self.assertEqual(self.service.started['temperature_sweep']['channel'],'S6')
        self.assertTrue(self.window.stop_button.isEnabled())
        self.assertFalse(self.window.temperature_sweep_workspace.config_group.isEnabled())
        self.assertFalse(self.window.select_experiment('material'))
        self.assertIs(self.window.tabs.currentWidget(),self.window.temperature_sweep_workspace)

    def test_live_voltage_updates_while_rf_running_and_configuration_invalidates_confirmation(self):
        cfg=self.configure()
        self.ready()
        self.window.confirm.setChecked(True)
        self.window.temperature_sweep_workspace.condition_label.setText('升温平台2')
        self.assertFalse(self.window.confirm.isChecked())
        self.service.job={'status':'running','config':cfg}
        live={'mode':'sweep','revision':1,'job':self.service.job,
              'points':[{'repeat':1,'carrier_hz':5e9,'amplitude_dB':-35.}],
              'temperature_points':[{'channel':'S6','host_elapsed_s':1.,'voltage_v':4.15},
                                    {'channel':'S6','host_elapsed_s':2.,'voltage_v':4.16},
                                    {'channel':'S7','host_elapsed_s':2.,'voltage_v':2.72}]}
        self.window._poll_done((self.service.snapshot(),live,{}))
        panel=self.window.temperature_sweep_workspace
        self.assertIn('4.16000',panel.readout.text())
        self.assertEqual(list(panel.voltage_lines['S6'].getData()[1]),[4.15,4.16])
        live=deepcopy(live);live['revision']=2
        live['temperature_points'].append({'channel':'S6','host_elapsed_s':3.,'voltage_v':4.18})
        self.window._poll_done((self.service.snapshot(),live,{}))
        self.assertIn('4.18000',panel.readout.text())
        self.assertEqual(len(panel.voltage_lines['S6'].getData()[0]),3)
        self.assertFalse(panel.temperature_plot.isVisible())

    def test_temp_history_category_is_scoped(self):
        self.configure()
        self.window._history_all_rows=[{'key':'runs/joint','category':'temperature_sweep','mode':'sweep',
                                      'label':'室温','status':'complete'}]+self.service.rows
        self.window._render_history_rows()
        self.assertEqual([r['key'] for r in self.window.history_rows],['runs/joint'])

    def test_manual_platform_can_start_without_arduino(self):
        self.select('temperature_sweep')
        panel=self.window.temperature_sweep_workspace
        panel.experiment_mode.setCurrentIndex(0)
        panel.reference_temperature.setText('49.7')
        panel.set_port('')
        self.service.initialize_environment(self.window._environment_target('usrp'))
        self.window._set_active(False)
        self.assertTrue(self.window.start_button.isEnabled())
        self.window.confirm.setChecked(True)
        self.window.start_experiment()
        self.pump(lambda:self.service.started is not None and not self.window.runner.pending)
        self.assertEqual(self.service.started['temperature_sweep']['mode'],'manual')
        self.assertEqual(self.service.started['temperature_sweep']['reference_temperature_c'],49.7)
