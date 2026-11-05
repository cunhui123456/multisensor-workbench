"""Unified sweep temperature options, protocol readiness and live views; no hardware."""
from copy import deepcopy
import unittest
from unittest.mock import patch
import test_experiment_navigation as fixtures
from test_temperature_ui import FakePreparation


class JointMainWindowTests(unittest.TestCase):
    setUpClass = fixtures.ExperimentNavigationTests.__dict__['setUpClass']
    setUp = fixtures.ExperimentNavigationTests.setUp
    tearDown = fixtures.ExperimentNavigationTests.tearDown
    pump = fixtures.ExperimentNavigationTests.pump
    select = fixtures.ExperimentNavigationTests.select

    def configure(self):
        self.select('material')
        self.window.material_workspace.material_name.setText('temperature_sample')
        self.window.sweep_options.temperature_mode.setCurrentIndex(2)
        self.window.temperature_sweep_workspace.set_port('/dev/cu.usbserial-TEST')
        self.window._joint_port_changed('/dev/cu.usbserial-TEST')
        return self.window.config()

    def ready(self, protocol_ready=True):
        for kind in ('usrp','serial'):
            self.service.initialize_environment(self.window._environment_target(kind))
        serial=self.window.temperature_workspace
        serial.protocol.setCurrentIndex(serial.protocol.findData('temperature_v1'))
        serial.port.setCurrentIndex(serial.port.findData('/dev/cu.usbserial-TEST'))
        preparation=FakePreparation()
        serial.preparation=preparation
        self.window._set_active(False)
        if protocol_ready:
            preparation.start('/dev/cu.usbserial-TEST','temperature_v1',upload=False)
            preparation.finish()
        self.window._set_active(False)

    def test_defaults_drafts_reset_and_primary_dual_view(self):
        cfg = self.configure()
        self.assertEqual((cfg['freq_start_ghz'],cfg['freq_stop_ghz'],cfg['points']), (5.,5.3,30))
        self.assertEqual((cfg['rx_gain'],cfg['tone_start']), (45.,20000.))
        self.assertEqual(cfg['temperature_sweep']['channel'],'S6')
        self.assertIsNone(cfg['temperature_sweep']['calibration'])
        self.assertTrue(self.window.sidebar.isAncestorOf(self.window.sweep_options))
        self.window.fields['points'].setValue(25)
        self.window.fields['rx_gain'].setValue(40)
        self.window.fields['tone_start'].setValue(15000)
        self.select('finger')
        self.assertIsNone(self.window.config()['temperature_sweep'])
        self.select('material')
        self.assertEqual(self.window.config()['points'],25)
        self.assertEqual((self.window.config()['rx_gain'],self.window.config()['tone_start']), (40.,15000.))
        self.assertEqual(self.window.sweep_options.temperature_mode.currentData(),'continuous')
        self.window.reset_parameters()
        self.assertEqual(self.window.config()['points'],30)
        self.assertEqual((self.window.config()['rx_gain'],self.window.config()['tone_start']), (45.,20000.))
        self.assertEqual(self.window.config()['freq_start_ghz'],5.)
        self.assertFalse(hasattr(self.window,'confirm'))
        self.assertFalse(self.service.initialize_calls)

    def test_requires_both_devices_and_protocol_and_locks_changes_during_joint_run(self):
        self.configure()
        self.service.initialize_environment(self.window._environment_target('usrp'))
        self.window._set_active(False)
        self.assertFalse(self.window.start_button.isEnabled())
        self.window.start_experiment()
        self.assertIsNone(self.service.started)
        self.ready(protocol_ready=False)
        self.assertFalse(self.window.start_button.isEnabled())
        self.window.start_experiment()
        self.assertIsNone(self.service.started)
        self.assertIn('固件',self.window.notice.text())
        self.ready()
        self.assertTrue(self.window.start_button.isEnabled())
        self.window.start_experiment()
        self.pump(lambda:self.service.started is not None and not self.window.runner.pending)
        self.assertEqual(self.service.started['temperature_sweep']['channel'],'S6')
        self.assertTrue(self.window.stop_button.isEnabled())
        self.assertFalse(self.window.sweep_options.isEnabled())
        self.assertFalse(self.window.select_experiment('finger'))
        self.assertIs(self.window.tabs.currentWidget(),self.window.temperature_sweep_workspace)

    def test_live_voltage_updates_and_condition_edits_preserve_initialized_device(self):
        cfg=self.configure()
        self.ready()
        self.window.material_workspace.material_name.setText('warming_platform_2')
        self.assertTrue(self.window._environment_ready('usrp'))
        self.assertTrue(self.window._environment_ready('serial'))
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

    def test_temperature_history_is_in_same_sweep_archive_with_material_and_liquid(self):
        self.configure()
        self.window._history_all_rows=[{'key':'runs/joint','category':'temperature_sweep','mode':'sweep',
                                      'label':'室温','status':'complete'}]+self.service.rows
        self.window._render_history_rows()
        self.assertEqual({r['key'] for r in self.window.history_rows},
                         {'runs/joint','runs/material','runs/liquid','teacher_runs/teacher'})

    def test_manual_platform_can_start_without_arduino(self):
        self.select('material')
        self.window.material_workspace.material_name.setText('manual_temperature_sample')
        self.window.sweep_options.temperature_mode.setCurrentIndex(1)
        panel=self.window.temperature_sweep_workspace
        panel.reference_temperature.setText('49.7')
        panel.set_port('')
        self.service.initialize_environment(self.window._environment_target('usrp'))
        self.window._set_active(False)
        self.assertTrue(self.window.start_button.isEnabled())
        self.window.start_experiment()
        self.pump(lambda:self.service.started is not None and not self.window.runner.pending)
        self.assertEqual(self.service.started['temperature_sweep']['mode'],'manual')
        self.assertEqual(self.service.started['temperature_sweep']['reference_temperature_c'],49.7)
        self.assertEqual((self.service.started['points'],self.service.started['rx_gain'],
                          self.service.started['tone_start']), (30,45.,20000.))
        self.assertIsNone(self.service.started['temperature_sweep'].get('serial'))
