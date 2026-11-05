"""Single sweep form actions use current values and only explicitly ready devices."""
import unittest
from PySide6 import QtCore

import test_experiment_navigation as fixtures
from test_temperature_ui import FakePreparation


class UnifiedSweepInteractionTests(unittest.TestCase):
    setUpClass = fixtures.ExperimentNavigationTests.__dict__['setUpClass']
    setUp = fixtures.ExperimentNavigationTests.setUp
    tearDown = fixtures.ExperimentNavigationTests.tearDown
    pump = fixtures.ExperimentNavigationTests.pump
    select = fixtures.ExperimentNavigationTests.select

    def fill_liquid(self):
        w = self.window
        for key, value in fixtures.liquid_request()['liquid_temperature'].items():
            if key in w.liquid_workspace.fields:
                w.liquid_workspace.fields[key].setText(str(value))
        w.sweep_options.liquid_enabled.setChecked(True)

    def test_liquid_and_manual_temperature_share_current_sweep_parameters_and_start(self):
        w = self.window
        w.material_workspace.material_name.setText('liquid_temperature_sample')
        w.fields['points'].setValue(25)
        w.fields['freq_start_ghz'].setValue(4.9)
        w.sweep_options.temperature_mode.setCurrentIndex(1)
        w.temperature_sweep_workspace.reference_temperature.setText('29.8')
        self.fill_liquid()
        self.service.initialize_environment(w._environment_target('usrp'))
        w._set_active(False)
        # Edits are the source of the next request; there is no apply/confirm step.
        w.liquid_workspace.fields['temperature_c'].setText('30.1')
        w.temperature_sweep_workspace.reference_temperature.setText('30.1')
        self.assertTrue(w.start_button.isEnabled())
        w.start_experiment()
        self.pump(lambda:self.service.started is not None and not w.runner.pending)
        request = self.service.started
        self.assertEqual(request['points'],25)
        self.assertEqual(request['freq_start_ghz'],4.9)
        self.assertEqual(request['temperature_sweep']['reference_temperature_c'],30.1)
        self.assertEqual(request['liquid_temperature']['temperature_c'],30.1)
        self.assertEqual(request['liquid_temperature']['sample_id'],'liquid_temperature_sample')
        self.assertEqual([x['kind'] for x in self.service.initialize_calls],['usrp'])
        self.assertTrue(request['setup_confirmed'])

    def test_switching_optional_temperature_modes_does_not_reset_shared_frequency_plan(self):
        w = self.window
        w.material_workspace.material_name.setText('sample')
        w.fields['freq_start_ghz'].setValue(4.8)
        w.fields['freq_stop_ghz'].setValue(5.2)
        w.fields['points'].setValue(31)
        w.fields['weak_point_extra_seconds'].setValue(0)
        for mode in ('manual','continuous','none'):
            w.sweep_options.temperature_mode.setCurrentIndex(w.sweep_options.temperature_mode.findData(mode))
            self.assertEqual(w.fields['freq_start_ghz'].value(),4.8)
            self.assertEqual(w.fields['freq_stop_ghz'].value(),5.2)
            self.assertEqual(w.fields['points'].value(),31)
            self.assertEqual(w.fields['weak_point_extra_seconds'].value(),0)
        self.assertEqual(self.service.initialize_calls,[])
        self.assertIsNone(self.service.started)

    def test_pressure_protocol_readiness_cannot_authorize_continuous_temperature(self):
        w = self.window
        w.material_workspace.material_name.setText('sample')
        w.sweep_options.temperature_mode.setCurrentIndex(2)
        port = '/dev/cu.usbserial-TEST'
        w.temperature_sweep_workspace.set_port(port)
        for kind in ('usrp','serial'):
            self.service.initialize_environment(w._environment_target(kind))
        prepared = FakePreparation()
        prepared.start(port,'pressure_v1',upload=False)
        prepared.finish()
        w.temperature_workspace.preparation = prepared
        w._set_active(False)
        self.assertFalse(w.start_button.isEnabled())
        w.start_experiment()
        self.assertIsNone(self.service.started)
        self.assertIn('固件',w.notice.text())
        self.assertEqual(prepared.calls,[(port,'pressure_v1','nano_old',False)])

    def test_temperature_and_liquid_record_uses_liquid_comparison_and_excludes_finger_analysis(self):
        w = self.window
        row = {'key':'runs/combined', 'category':'temperature_sweep', 'mode':'sweep',
               'status':'complete', 'label':'water_30C', 'has_liquid_conditions':True}
        w._history_all_rows = [row]
        w._render_history_rows()
        self.assertEqual([record['key'] for record in w.history_rows], ['runs/combined'])
        item = w.history_table.item(0, 0)
        item.setCheckState(QtCore.Qt.CheckState.Checked)
        self.assertEqual(item.checkState(), QtCore.Qt.CheckState.Checked)
        self.assertEqual(w.comparison_keys, {'runs/combined'})
        self.assertFalse(w.compare_button.isEnabled())
        w.compare_selected()
        self.assertIn('温度记录与比较', w.notice.text())
        # Details carry the original parameter groups even without the index flag.
        w.selected_details = {'key':'runs/combined', 'context':{
            'experiment':'temperature_sweep', 'parameters':fixtures.liquid_request()}}
        w._history_buttons(True)
        self.assertFalse(w.finger_history_button.isEnabled())
        w._finger_load_history()
        self.assertIn('温度记录与比较', w.notice.text())
        self.assertIsNone(w.finger_workspace.source_key)
        self.assertFalse(w.runner.pending)


if __name__ == '__main__':
    unittest.main()
