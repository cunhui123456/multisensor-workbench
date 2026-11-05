"""Compact parameter layout, contextual help and saved-condition presentation."""
from copy import deepcopy
import unittest

from PySide6 import QtCore, QtWidgets

from sspp_desktop.archive_conditions import archive_conditions
import test_experiment_navigation as fixtures


class CompactExperimentUiTests(unittest.TestCase):
    setUpClass = fixtures.ExperimentNavigationTests.__dict__['setUpClass']
    setUp = fixtures.ExperimentNavigationTests.setUp
    tearDown = fixtures.ExperimentNavigationTests.tearDown
    pump = fixtures.ExperimentNavigationTests.pump
    select = fixtures.ExperimentNavigationTests.select

    def test_help_is_outside_parameters_and_conditions_share_one_group(self):
        w = self.window
        self.assertFalse(w.help_dialog.isVisible())
        self.assertFalse(w.sidebar.isAncestorOf(w.help_dialog))
        w.experiment_help_button.click()
        self.assertTrue(w.help_dialog.isVisible())
        self.assertIn('初始化 B210', w.help_dialog.body.toPlainText())
        w.help_dialog.close()
        self.assertFalse(w.help_dialog.isVisible())
        for widget in (w.material_workspace.material_name, w.fields['sensor'],
                       w.sweep_options.temperature_mode, w.sweep_options.liquid_enabled):
            self.assertTrue(w.conditions_group.isAncestorOf(widget))
        titles = {group.title() for group in w.config_container.findChildren(QtWidgets.QGroupBox)
                  if group.isVisible() and group.title()}
        self.assertEqual(titles, {'样品与条件', '扫频设置', '高级参数'})
        self.assertFalse(self.service.initialize_calls)
        self.assertIsNone(self.service.started)

    def test_default_essential_controls_fit_minimum_window(self):
        w = self.window
        w.resize(1080, 740)
        self.app.processEvents()
        viewport = w.sidebar_scroll.viewport()
        for widget in (w.material_workspace.material_name, w.sweep_options.temperature_mode,
                       w.fields['freq_start_ghz'], w.fields['points'], w.fields['repeat']):
            with self.subTest(control=widget):
                bottom = widget.mapTo(viewport, widget.rect().bottomRight())
                self.assertTrue(viewport.rect().contains(bottom), (widget, bottom, viewport.rect()))
        self.assertTrue(w.start_button.isVisible())
        self.assertTrue(w.reset_button.isVisible())

    def test_archive_search_and_plain_text_details_use_saved_conditions(self):
        w = self.window
        row = {'key':'runs/record', 'category':'temperature_sweep', 'mode':'sweep',
               'status':'complete', 'label':'stored_label', 'sensor':'SSPP_A'}
        context = {'experiment':'temperature_sweep', 'parameters':{
            'label':'<b>水样_30C</b>', 'sensor':'SSPP_A', 'notes':'静置后冷却',
            'temperature_sweep':{'mode':'manual', 'reference_temperature_c':30},
            **fixtures.liquid_request(),
        }}
        context['parameters']['liquid_temperature']['thermometer'] = '参考探头_A'
        row.update(archive_conditions(row, context, {}))
        details = {'key':row['key'], 'context':context, 'metadata':{}, 'files':[],
                   'curve':{'mode':'sweep', 'points':[]}}
        self.service.details = lambda key, include_curve=True: {**deepcopy(details), "curve_loaded": include_curve}
        w._history_all_rows = [row]
        w._render_history_rows()
        w.tabs.setCurrentIndex(1)
        for term in ('glass_1', '参考探头_A', 'cycle_1', '静置后冷却', '30'):
            w.history_search.setText(term)
            self.pump(lambda: not w._history_filter_timer.isActive())
            self.assertEqual(len(w.history_rows), 1, term)
        self.assertIn('手填参考', w.history_table.item(0, 6).text())
        self.assertIn('glass_1', w.history_table.item(0, 6).toolTip())
        w.history_table.selectRow(0)
        self.pump(lambda: not w.runner.pending)
        self.assertEqual(w.tabs.currentIndex(), 1)
        self.assertIn('<b>水样_30C</b>', w.metadata.toPlainText())
        self.assertIn('参考探头_A', w.metadata.toPlainText())
        self.assertTrue(w.metadata.toPlainText().startswith('实验条件\n'))

    def test_failure_diagnosis_remains_visible_and_preserves_independent_serial(self):
        w = self.window
        for kind in ('usrp', 'serial'):
            self.service.initialize_environment(w._environment_target(kind))
        w._apply_status({'job':{'status':'running','config':{'mode':'sweep'}}})
        summary = 'B210 通信无响应；停止状态未确认。请检查连接并重新连接 B210，再初始化。'
        self.service.environments['usrp'].update(state='failed', message=summary)
        snapshot = {'job':{'status':'stop_unconfirmed', 'config':{'mode':'sweep'},
                           'failure_summary':summary, 'errors':['quality failure', 'stop unknown']}}
        w._apply_status(snapshot)
        self.assertEqual(w.status_detail.text(), summary)
        self.assertEqual(w.notice.text(), summary)
        self.assertEqual(self.service.environment_snapshot('usrp')['message'], summary)
        self.assertEqual(self.service.environment_snapshot('serial')['state'], 'ready')
        w._message('查看档案')
        w._apply_status(snapshot)
        self.assertEqual(w.notice.text(), '查看档案')


if __name__ == '__main__':
    unittest.main()
