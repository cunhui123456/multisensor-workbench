"""标定点可撤销删除/恢复界面；只发请求，不访问文件或硬件。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from copy import deepcopy
import unittest
from unittest.mock import patch

from PySide6 import QtCore, QtWidgets
from sspp_desktop.serial_archive_widget import SerialArchiveWidget
from qt_test_cleanup import dispose_closed_windows


def details():
    points=[]
    for ident,deleted,status in ((41,False,'saved'),(7,False,'saved'),(9,True,'saved'),(10,False,'cancelled')):
        points.append({'id':ident,'deleted':deleted,'status':status,'duration_s':30,
                       'reference_mean_c':40,'requires_manual_review':True,'quality_flags':[],
                       'channels':{'S6':{'mean_v':3.58,'sample_sd_v':.14}},
                       'deletion':{'reason':'旧标注错误','deleted_utc':'2026-11-02T08:00:00Z'} if deleted else None})
    return {'key':'temperature_pressure_runs/example','context':{'experiment':'temperature_calibration'},
            'metadata':{'status':'complete','serial_closed':True,'workers_stopped':True,'data_files_closed':True},
            'curve':{'points':[],'stored_rows':0},'calibration_points':points}


class CalibrationPointActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.widget=SerialArchiveWidget()
        self.source=details()
        self.widget.display(self.source)
        self.deleted=[];self.restored=[]
        self.widget.calibration_delete_requested.connect(self.deleted.append)
        self.widget.calibration_restore_requested.connect(self.restored.append)

    def tearDown(self):
        self.widget.close()
        dispose_closed_windows()

    def select_rows(self,*rows):
        table=self.widget.table
        table.clearSelection()
        for row in rows:
            table.selectionModel().select(table.model().index(row,0),
                QtCore.QItemSelectionModel.SelectionFlag.Select | QtCore.QItemSelectionModel.SelectionFlag.Rows)

    def test_multiselect_emits_original_ids_and_reason_without_mutating_details(self):
        original=deepcopy(self.source)
        self.select_rows(0,1)
        self.assertTrue(self.widget.calibration_delete_button.isEnabled())
        with patch.object(QtWidgets.QInputDialog,'getText',return_value=('  标注错误  ',True)) as dialog:
            self.widget.calibration_delete_button.click()
        self.assertEqual(dialog.call_args.args[-1],'标注错误')
        self.assertEqual(self.deleted,[{'key':self.source['key'],'point_ids':[41,7],'reason':'标注错误'}])
        for column in range(self.widget.table.columnCount()):
            self.assertEqual(self.widget.table.item(0,column).data(QtCore.Qt.ItemDataRole.UserRole),41)
        self.assertFalse(self.widget.calibration_delete_button.isEnabled())
        self.assertFalse(self.widget.table.isEnabled())
        self.assertEqual(self.source,original)

    def test_deleted_rows_keep_values_and_restore_without_a_second_dialog(self):
        table=self.widget.table
        self.assertEqual(table.rowCount(),4)
        self.assertIn('已删除（可恢复）',table.item(2,5).text())
        self.assertIn('旧标注错误',table.item(2,5).text())
        self.assertIn('2026-11-02T08:00:00Z',table.item(2,5).toolTip())
        self.assertEqual(table.item(2,3).text(),'3.58000 ± 0.14000')
        self.select_rows(2)
        self.assertFalse(self.widget.calibration_delete_button.isEnabled())
        self.assertTrue(self.widget.calibration_restore_button.isEnabled())
        with patch.object(QtWidgets.QInputDialog,'getText') as dialog:
            self.widget.calibration_restore_button.click()
            dialog.assert_not_called()
        self.assertEqual(self.restored,[{'key':self.source['key'],'point_ids':[9],'reason':'恢复误删标定点'}])

    def test_mixed_or_unsaved_selection_is_not_silently_filtered(self):
        for rows in ((0,2),(3,),(0,3),()):
            with self.subTest(rows=rows):
                self.select_rows(*rows)
                self.assertFalse(self.widget.calibration_delete_button.isEnabled())
                self.assertFalse(self.widget.calibration_restore_button.isEnabled())
                self.widget._request_calibration_delete()
                self.widget._request_calibration_restore()
        self.assertFalse(self.deleted or self.restored)

    def test_only_complete_sealed_calibration_records_enable_actions(self):
        cases=[('status','running'),('serial_closed',False),('workers_stopped',False),('data_files_closed',False)]
        for field,value in cases:
            with self.subTest(field=field):
                source=details();source['metadata'][field]=value
                self.widget.display(source);self.select_rows(0)
                self.assertFalse(self.widget.calibration_delete_button.isEnabled())
        source=details();source['context']['experiment']='temperature_pressure'
        self.widget.display(source);self.select_rows(0)
        self.assertFalse(self.widget.calibration_delete_button.isEnabled())

    def test_cancel_or_empty_reason_emits_nothing(self):
        self.select_rows(0)
        for result in (('标注错误',False),('   ',True)):
            with self.subTest(result=result),patch.object(QtWidgets.QInputDialog,'getText',return_value=result):
                self.widget.calibration_delete_button.click()
                self.assertEqual(self.deleted,[])
                self.assertTrue(self.widget.calibration_delete_button.isEnabled())

    def test_background_record_change_during_reason_dialog_cannot_delete_wrong_run(self):
        self.select_rows(0)
        def changed(*args):
            source=details();source['key']='temperature_pressure_runs/other'
            self.widget.display(source)
            return '标注错误',True
        with patch.object(QtWidgets.QInputDialog,'getText',side_effect=changed):
            self.widget.calibration_delete_button.click()
        self.assertEqual(self.deleted,[])
        self.assertIn('档案已更新',self.widget.calibration_edit_status.text())

    def test_busy_blocks_requests_and_reloaded_details_restore_controls(self):
        self.select_rows(0)
        self.widget.set_edit_busy(True)
        self.widget._request_calibration_restore()
        self.widget._request_calibration_delete()
        self.assertFalse(self.deleted or self.restored)
        self.assertFalse(self.widget.table.isEnabled())
        self.widget.set_edit_busy(False)
        self.assertTrue(self.widget.calibration_delete_button.isEnabled())
        self.widget.set_edit_busy(True)
        self.widget.display(details())
        self.assertTrue(self.widget.table.isEnabled())
        self.assertFalse(self.widget.calibration_delete_button.isEnabled())
        self.select_rows(0)
        self.assertTrue(self.widget.calibration_delete_button.isEnabled())


if __name__=='__main__':
    unittest.main()
