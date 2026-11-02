"""Liquid publication UI binding and shared source validation; no hardware."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from PySide6 import QtCore, QtGui, QtWidgets
from sspp_desktop.liquid_temperature_widget import LiquidTemperatureWidget
from sspp_lab.controller import LabError
import test_liquid_temperature_ui as ui
import test_liquid_temperature_service as fixtures


def report(identity='registered-one'):
    return {'comparison_id':identity, 'status':'ready', 'export_path':'/private/tmp/liquid-report',
            'charts':[{'id':name, 'title':name, 'series':[{'label':'observed', 'data':[[0, 0], [1, 2]]}]}
                      for name in ('first', 'second')], 'warnings':[]}


class FigureService(ui.FakeService):
    def __init__(self):
        super().__init__()
        self.release=threading.Event(); self.release.set()
        self.fail=False

    def export_publication_figure(self, value, chart_id):
        self._call('archive', value, chart_id)
        self.release.wait(3)
        if self.fail:
            raise ValueError('图形导出测试失败')
        return {'directory':'/private/tmp/liquid-publication', 'path':'/private/tmp/liquid-publication'}

    def export_analysis_figure(self, value, chart_id, destination):
        self._call('figure', value, chart_id, destination)
        self.release.wait(3)
        return {'path':destination}


class LiquidFigureUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.service=FigureService()
        self.widget=LiquidTemperatureWidget(self.service)
        self.widget._show_report(report())
        self.widget.chart_choice.setCurrentIndex(1)

    def pump(self, predicate):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            self.app.processEvents()
            if predicate(): return
            time.sleep(.005)
        self.fail('liquid figure task did not finish')

    def tearDown(self):
        self.service.release.set()
        self.pump(lambda:not self.widget.is_busy)
        self.widget.close(); self.widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_registered_report_and_current_chart_are_required(self):
        unregistered=report();unregistered.pop('comparison_id')
        self.widget._show_report(unregistered)
        self.assertFalse(self.widget.figure_export_button.isEnabled())
        self.widget.export_figure(); self.widget.save_figure_as()
        self.assertFalse(self.service.calls)
        self.widget._show_report(report())
        self.widget.chart_choice.setCurrentIndex(1)
        self.widget.chart_choice.setItemData(1,{'id':'unknown'})
        self.widget.export_figure()
        self.assertFalse(self.service.calls)

    def test_archive_is_background_without_picker_and_busy_interlocks_recover(self):
        busy=[];self.widget.busy_changed.connect(busy.append)
        self.service.release.clear()
        with patch.object(QtWidgets.QFileDialog,'getSaveFileName') as picker:
            self.widget.figure_export_button.click()
            self.pump(lambda:bool(self.service.calls))
            picker.assert_not_called()
        name,args,thread=self.service.calls[0]
        self.assertEqual((name,args[1]),('archive','second'))
        self.assertNotEqual(thread,threading.get_ident())
        self.assertIsNot(args[0],self.widget._publication_report)
        for control in (self.widget.chart_choice,self.widget.figure_export_button,
                        self.widget.figure_save_as_button,self.widget.figure_folder_button,
                        self.widget.prepare_button,self.widget.export_button):
            self.assertFalse(control.isEnabled())
        self.widget.export_figure();self.widget.save_figure_as()
        self.assertEqual(len(self.service.calls),1)
        self.service.release.set();self.pump(lambda:not self.widget.is_busy)
        self.assertEqual(busy,[True,False])
        self.assertTrue(self.widget.figure_export_button.isEnabled())
        self.assertTrue(self.widget.figure_folder_button.isEnabled())
        with patch.object(QtGui.QDesktopServices,'openUrl',return_value=True) as opened:
            self.widget.figure_folder_button.click()
            self.assertEqual(opened.call_args.args[0].toLocalFile(),'/private/tmp/liquid-publication')

    def test_save_as_cancel_formats_invalid_suffix_and_worker_thread(self):
        with patch.object(QtWidgets.QFileDialog,'getSaveFileName',return_value=('','')):
            self.widget.save_figure_as()
        self.assertFalse(self.service.calls)
        for extension,label in (('.pdf','矢量 PDF (*.pdf)'),('.svg','矢量 SVG (*.svg)'),('.png','高清 PNG (*.png)')):
            with self.subTest(extension=extension),patch.object(QtWidgets.QFileDialog,'getSaveFileName',
                    return_value=('/private/tmp/liquid-single',label)) as picker:
                self.widget.figure_save_as_button.click()
                self.pump(lambda:not self.widget.is_busy)
                self.assertTrue(picker.call_args.args[2].endswith('.pdf'))
                name,args,thread=self.service.calls[-1]
                self.assertEqual((name,args[1],args[2]),('figure','second','/private/tmp/liquid-single'+extension))
                self.assertNotEqual(thread,threading.get_ident())
        count=len(self.service.calls)
        with patch.object(QtWidgets.QFileDialog,'getSaveFileName',return_value=('/private/tmp/liquid.exe','')):
            self.widget.save_figure_as()
        self.assertEqual(len(self.service.calls),count)
        self.assertIn('PDF、SVG 或 PNG',self.widget.status.text())

    def test_report_or_chart_change_during_picker_cancels_export(self):
        for change in ('report','chart','busy','close'):
            with self.subTest(change=change):
                self.widget._show_report(report());self.widget.chart_choice.setCurrentIndex(1)
                def changed(*args):
                    if change=='report': self.widget._show_report(report('new-report'))
                    elif change=='chart': self.widget.chart_choice.setCurrentIndex(0)
                    elif change=='busy': self.widget.set_external_busy(True)
                    else: self.widget.request_close()
                    return '/private/tmp/liquid.pdf','矢量 PDF (*.pdf)'
                with patch.object(QtWidgets.QFileDialog,'getSaveFileName',side_effect=changed):
                    self.widget.save_figure_as()
                self.assertFalse(self.service.calls)
                self.widget.set_external_busy(False)

    def test_failure_and_queue_rejection_restore_controls_without_directory(self):
        self.service.fail=True
        self.widget.export_figure();self.pump(lambda:not self.widget.is_busy)
        self.assertIn('失败',self.widget.status.text())
        self.assertTrue(self.widget.figure_export_button.isEnabled())
        self.assertFalse(self.widget.figure_folder_button.isEnabled())
        previous=len(self.service.calls)
        with patch.object(self.widget.runner,'submit',return_value=False):
            self.widget.export_figure()
        self.assertEqual(len(self.service.calls),previous)
        self.assertTrue(self.widget.figure_export_button.isEnabled())

    def test_late_result_does_not_attach_old_directory_to_new_report_or_closed_widget(self):
        for action in ('replace','close'):
            with self.subTest(action=action):
                self.widget._show_report(report())
                self.service.release.clear();self.widget.export_figure()
                self.pump(lambda:self.widget.is_busy)
                if action=='replace': self.widget._show_report(report('replacement'))
                else: self.assertFalse(self.widget.request_close())
                self.service.release.set();self.pump(lambda:not self.widget.is_busy)
                self.assertIsNone(self.widget._figure_directory)
                self.assertFalse(self.widget.figure_folder_button.isEnabled())

    def test_new_record_or_saved_annotation_invalidates_old_export_binding(self):
        self.widget.export_figure();self.pump(lambda:not self.widget.is_busy)
        self.widget._show_record({'key':'runs/new','mode':'sweep'})
        self.assertIsNone(self.widget._figure_directory)
        self.assertIsNone(self.widget._publication_report)
        self.widget._show_report(report())
        self.widget.export_figure();self.pump(lambda:not self.widget.is_busy)
        self.widget.end_temperature.setText('30.1')
        self.widget.save_annotations();self.pump(lambda:not self.widget.is_busy)
        self.assertIsNone(self.widget.report_path)
        self.assertIsNone(self.widget._publication_report)
        self.assertIsNone(self.widget._figure_directory)
        self.assertFalse(self.widget.figure_export_button.isEnabled())
        self.assertFalse(self.widget.figure_folder_button.isEnabled())
        self.assertFalse(self.widget.export_button.isEnabled())
        self.assertIn('重新分析',self.widget.status.text())


class LiquidFigureServiceTests(unittest.TestCase):
    setUp=fixtures.LiquidTemperatureServiceTests.setUp
    context=fixtures.LiquidTemperatureServiceTests.context
    finish=fixtures.LiquidTemperatureServiceTests.finish
    phase_record=fixtures.LiquidTemperatureServiceTests.phase_record
    fake_figures=staticmethod(fixtures.LiquidTemperatureServiceTests.fake_figures)

    @staticmethod
    def render(report, chart_id, extension):
        return ('offline liquid '+chart_id+extension).encode()

    def test_sweep_and_phase_reports_register_for_shared_archive_and_save_as(self):
        self.finish()
        sweep=self.service.liquid_compare_sweeps(self.keys)
        annotations=self.phase_record('phase_runs/one')
        phase=self.service.liquid_analyze_phase('phase_runs/one',annotations)
        self.phase_record('phase_runs/two',temperature=30.)
        combined=self.service.liquid_compare_phase_runs(['phase_runs/one','phase_runs/two'])
        before={p:p.read_bytes() for p in (self.root/'Data').rglob('*') if p.is_file()}
        with patch.object(self.service,'_render_analysis_figure',side_effect=self.render):
            for value in (sweep,phase,combined):
                with self.subTest(kind=value['kind'],sources=len(value['sources'])):
                    self.assertTrue(value['comparison_id'])
                    chart=value['charts'][0]['id']
                    self.service._verify_analysis_report(value,chart)
                    result=self.service.export_publication_figure(value,chart)
                    folder=Path(result['directory'])
                    self.assertEqual(folder.parent.parent,self.root.resolve()/'Figures/liquid_temperature')
                    manifest=json.loads((folder/'figure_manifest.json').read_text())
                    self.assertEqual(manifest['source_keys'],value['keys'])
                    self.assertEqual(manifest['sources'],value['sources'])
                    self.assertEqual(manifest['analysis_mode'],'single' if value is phase else 'combined')
                    self.assertEqual(set(result['files']),{'pdf','svg','png'})
                    self.assertTrue((folder/'caption.txt').read_text())
                    target=self.root.resolve()/('single-'+value['comparison_id']+'.svg')
                    self.service.export_analysis_figure(value,chart,str(target))
                    self.assertTrue(target.read_bytes())
        self.assertEqual(before,{p:p.read_bytes() for p in before})

    def test_registration_and_source_hashes_reject_unknown_modified_and_stale_reports(self):
        self.finish();value=self.service.liquid_compare_sweeps(self.keys)
        chart=value['charts'][0]['id']
        for change in ('missing','unknown','mutated'):
            modified=deepcopy(value)
            if change=='missing':modified.pop('comparison_id')
            elif change=='unknown':modified['comparison_id']='unregistered'
            else:modified['charts'][0]['series'][0]['data'][0][1]+=1
            with self.subTest(change=change),self.assertRaisesRegex(LabError,'更改或已过期'):
                self.service.export_publication_figure(modified,chart)
        target=self.root.resolve()/'previous.pdf';target.write_bytes(b'previous')
        path=self.root/'Data'/self.keys[0]/'measurements.csv'
        path.write_bytes(path.read_bytes()+b'\n')
        with self.assertRaisesRegex(LabError,'比较后已改变'):
            self.service.export_analysis_figure(value,chart,str(target))
        self.assertEqual(target.read_bytes(),b'previous')
        with self.assertRaisesRegex(LabError,'比较后已改变'):
            self.service.export_publication_figure(value,chart)
        self.assertFalse(self.service.exporting)

    def test_shared_verification_checks_liquid_analysis_code_after_registration(self):
        code=self.root/'sspp_desktop/liquid_temperature_service.py'
        code.parent.mkdir(parents=True)
        code.write_bytes(Path(fixtures.__file__).parents[1].joinpath('sspp_desktop/liquid_temperature_service.py').read_bytes())
        self.finish();value=self.service.liquid_compare_sweeps(self.keys)
        self.assertIn('sspp_desktop/liquid_temperature_service.py',[entry['path'] for entry in value['analysis_code']])
        code.write_bytes(code.read_bytes()+b'\n')
        with self.assertRaisesRegex(LabError,'代码在比较后已改变'):
            self.service.export_publication_figure(value,value['charts'][0]['id'])


if __name__=='__main__':
    unittest.main()
