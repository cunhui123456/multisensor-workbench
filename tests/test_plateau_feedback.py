"""Persistent interval validation feedback and explicit voltage-rate units."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from PySide6 import QtWidgets
from sspp_desktop.plateau_widget import PlateauWidget


class PlateauFeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.widget=PlateauWidget();self.widget.show()
        self.widget.source.setText('BY1010')
        self.widget.update_state({},True,False)
        self.calls=[]
        self.widget.requested.connect(lambda op,values:self.calls.append((op,values)))

    def tearDown(self):
        self.widget.close();self.widget.deleteLater();self.app.processEvents()

    def test_25_v_per_min_rejection_remains_next_to_input_across_polling(self):
        widget=self.widget;widget.voltage_limit.setText('25')
        widget.begin_button.click()
        self.assertFalse(self.calls)
        self.assertIn('10',widget.feedback.text())
        self.assertFalse(widget.feedback.isHidden())
        for _ in range(100):widget.update_state({'reference':{'status':'insufficient_reference'}},True,False)
        self.assertIn('10',widget.feedback.text())
        self.assertFalse(widget.feedback.isHidden())
        self.assertTrue(widget.begin_button.isEnabled())

    def test_explicit_millivolt_selection_converts_25_to_point025_and_clears_error(self):
        widget=self.widget;widget.voltage_limit.setText('25');widget.begin_button.click()
        widget.voltage_unit.setCurrentIndex(widget.voltage_unit.findData(.001))
        self.assertIn('0.025 V/min',widget.voltage_hint.text())
        widget.begin_button.click()
        self.assertEqual(len(self.calls),1)
        op,values=self.calls[0]
        self.assertEqual(op,'begin')
        self.assertEqual(values['settings']['voltage_rate_limit_v_per_min'],.025)
        self.assertEqual(values['reference_source'],'BY1010')
        self.assertTrue(widget.feedback.isHidden())
        widget.update_state({},True,True)
        self.assertFalse(widget.voltage_unit.isEnabled())

    def test_blank_is_optional_and_invalid_number_or_source_never_starts_interval(self):
        widget=self.widget
        for text in ('nan','inf','-1','not a number'):
            with self.subTest(text=text):
                widget.voltage_limit.setText(text);widget.begin_button.click()
                self.assertFalse(self.calls)
                self.assertFalse(widget.feedback.isHidden())
        widget.voltage_limit.clear();widget.source.clear();widget.begin_button.click()
        self.assertFalse(self.calls)
        self.assertIn('参考仪器',widget.feedback.text())
        widget.source.setText('BY1010');widget.begin_button.click()
        self.assertIsNone(self.calls[0][1]['settings']['voltage_rate_limit_v_per_min'])
        self.assertTrue(widget.feedback.isHidden())

    def test_queue_and_backend_errors_survive_poll_and_clear_after_valid_action(self):
        widget=self.widget
        widget.show_error('标定操作队列已满')
        widget.update_state({},True,False)
        self.assertIn('队列已满',widget.feedback.text())
        widget.clear_error()
        widget.update_state({'last_error':'请先结束当前标定区间'},True,False)
        self.assertIn('请先结束',widget.feedback.text())
        widget.update_state({},True,False)
        self.assertTrue(widget.feedback.isHidden())
        widget.reference.setText('bad');widget.add_reference()
        widget.update_state({},True,False)
        self.assertIn('实测参考温度',widget.feedback.text())
        widget.reference.setText('49.7');widget.add_reference()
        self.assertTrue(widget.feedback.isHidden())
        self.assertEqual(self.calls[-1],('reference',{'temperature_c':49.7}))


if __name__=='__main__':unittest.main()
