"""Weak-point settings are visible, isolated by experiment and included in requests."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import time
import unittest
from PySide6 import QtCore,QtWidgets
from sspp_desktop.window import MainWindow
from test_experiment_navigation import NavigationService
from qt_test_cleanup import dispose_closed_windows


class WeakPointBudgetUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def pump(self,condition):
        until=time.monotonic()+4
        while time.monotonic()<until:
            self.app.processEvents()
            if condition():return
            time.sleep(.005)
        self.fail('UI did not settle')
    def setUp(self):
        self.service=NavigationService();self.window=MainWindow(self.service)
        self.window.timer.stop();self.window.show()
        self.pump(lambda:self.window.profile.count()==1 and not self.window.runner.pending)
    def tearDown(self):
        self.service.preflight_release.set();self.service.stop()
        self.window._start_pending=False;self.window._close_requested=False
        self.window._apply_status(self.service.snapshot())
        self.pump(lambda:not self.window.runner.pending and not self.window.liquid_workspace.is_busy)
        self.window.close();self.pump(lambda:self.service.closed and not self.window.isVisible())
        dispose_closed_windows()
    def test_default_extra_is_visible_and_total_updates_without_creating_total_limit(self):
        w=self.window;extra=w.fields['weak_point_extra_seconds']
        self.assertTrue(extra.isVisible());self.assertEqual(extra.value(),8)
        self.assertIn('每点最多 16 秒',w.sweep_weak_budget_hint.text())
        w.confirm.setChecked(True);extra.setValue(12)
        self.assertFalse(w.confirm.isChecked())
        self.assertIn('每点最多 20 秒',w.sweep_weak_budget_hint.text())
        self.assertEqual(w.config()['weak_point_extra_seconds'],12)
        self.assertIsNone(w.config()['max_seconds'])
        w.fields['point_timeout'].setValue(10)
        self.assertIn('每点最多 22 秒',w.sweep_weak_budget_hint.text())
    def test_zero_and_reset_are_explicit(self):
        w=self.window;w.fields['weak_point_extra_seconds'].setValue(0)
        self.assertIn('已关闭',w.sweep_weak_budget_hint.text())
        self.assertIn('每点最多 8 秒',w.sweep_weak_budget_hint.text())
        w.reset_parameters()
        self.assertEqual(w.fields['weak_point_extra_seconds'].value(),8)
        self.assertIn('每点最多 16 秒',w.sweep_weak_budget_hint.text())
    def test_existing_draft_without_extra_uses_default_not_previous_page_value(self):
        w=self.window;old=w._capture_draft();old['values'].pop('weak_point_extra_seconds')
        w.fields['weak_point_extra_seconds'].setValue(0)
        w._restore_draft(old);w._mode_changed()
        self.assertEqual(w.fields['weak_point_extra_seconds'].value(),8)
        self.assertIn('每点最多 16 秒',w.sweep_weak_budget_hint.text())
    def test_explicit_zero_survives_draft_restore_and_confirmation_is_cleared(self):
        w=self.window;w.fields['weak_point_extra_seconds'].setValue(0);draft=w._capture_draft()
        w.fields['weak_point_extra_seconds'].setValue(8);w.confirm.setChecked(True)
        w._restore_draft(draft);w._mode_changed()
        self.assertEqual(w.config()['weak_point_extra_seconds'],0)
        self.assertFalse(w.confirm.isChecked());self.assertIn('已关闭',w.sweep_weak_budget_hint.text())

if __name__=='__main__':unittest.main()
