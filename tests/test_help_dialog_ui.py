"""Help opens without resizing the workbench or blocking acquisition controls."""
import unittest
from PySide6 import QtCore, QtTest
import test_experiment_navigation as fixtures


class HelpDialogUiTests(unittest.TestCase):
    setUpClass = fixtures.ExperimentNavigationTests.__dict__['setUpClass']
    setUp = fixtures.ExperimentNavigationTests.setUp
    tearDown = fixtures.ExperimentNavigationTests.tearDown
    pump = fixtures.ExperimentNavigationTests.pump
    select = fixtures.ExperimentNavigationTests.select

    def test_help_is_non_modal_reusable_and_does_not_change_layout(self):
        w = self.window
        widgets = (w, w.centralWidget(), w.sidebar, w.tabs)
        before = [widget.geometry() for widget in widgets]
        self.assertFalse(w.experiment_help_button.isCheckable())
        self.assertFalse(hasattr(w, 'help_panel'))
        w.experiment_help_button.click()
        self.app.processEvents()
        dialog = w.help_dialog
        self.assertTrue(dialog.isVisible())
        self.assertFalse(dialog.isModal())
        self.assertEqual(dialog.windowModality(), QtCore.Qt.WindowModality.NonModal)
        self.assertEqual([widget.geometry() for widget in widgets], before)
        w.experiment_help_button.click()
        self.assertIs(w.help_dialog, dialog)
        QtTest.QTest.keyClick(dialog, QtCore.Qt.Key.Key_Escape)
        self.assertFalse(dialog.isVisible())
        w.experiment_help_button.click()
        self.assertTrue(dialog.isVisible())
        self.assertEqual([widget.geometry() for widget in widgets], before)
        self.assertFalse(self.service.initialize_calls)
        self.assertIsNone(self.service.started)

    def test_open_help_follows_current_experiment(self):
        w = self.window
        w.experiment_help_button.click()
        self.assertIn('SSPP 扫频', w.help_dialog.windowTitle())
        self.select('pressure_calibration')
        self.assertIn('压力传感器标定', w.help_dialog.windowTitle())
        self.assertIn('参考力', w.help_dialog.body.toPlainText())
        self.assertTrue(w.help_dialog.isVisible())
        self.select('finger')
        self.assertIn('同一次连续记录', w.help_dialog.body.toPlainText())


if __name__ == '__main__':
    unittest.main()
