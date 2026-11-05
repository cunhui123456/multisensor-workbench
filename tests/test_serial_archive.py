"""温度标定档案分类、只读详情、原始数据导出及界面隔离。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch,PropertyMock
from PySide6 import QtCore,QtWidgets
from sspp_desktop.services import DesktopService
from sspp_desktop.config import Settings
from sspp_desktop.temperature_widget import TemperaturePressureWidget
from sspp_lab.controller import LabError
import test_experiment_navigation as navigation


def make_record(root,name,calibration=True,sealed=True):
    path=Path(root)/'Data'/'temperature_pressure_runs'/name;path.mkdir(parents=True)
    raw=b'100,600,2.9,140,650,3.1\n'
    (path/'raw_serial.bin').write_bytes(raw)
    (path/'samples.csv').write_text('host_elapsed_s,voltage_v,mcu_time_ms,time_segment,channel,adc\n0,2.9,100,0,S6,600\n0,3.1,140,0,S7,650\n1,2.91,200,0,S6,602\n1,3.11,240,0,S7,652\n')
    m={'status':'complete' if sealed else 'running','sample_name':name,'started_utc':'2026-11-02T00:00:00Z','protocol':'temperature_v1',
       'rows':4,'serial_closed':sealed,'workers_stopped':sealed,'data_files_closed':sealed,
       'serial':{'port':'PRIVATE_DEVICE'},'preparation':{'identity':'PRIVATE'},'files':[]}
    if calibration:
        m['temperature_calibration']={'mode':'platform_intervals'}
        (path/'calibration_points.json').write_text(json.dumps({'points':[{'id':1,'duration_s':30,'reference_mean_c':50,
            'channels':{'S6':{'count':2,'mean_v':2.905,'sample_sd_v':.007}},'status':'saved','quality_flags':[]}]}))
    for name in ('raw_serial.bin','samples.csv'):
        data=(path/name).read_bytes();m['files'].append({'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    (path/'manifest.json').write_text(json.dumps(m))
    return path


class SerialArchiveTests(unittest.TestCase):
    def test_category_and_curve_are_serial_specific_without_private_device_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            make_record(root,'calibration');make_record(root,'ordinary',False)
            legacy=make_record(root,'legacy');m=json.loads((legacy/'manifest.json').read_text());m.pop('temperature_calibration');(legacy/'manifest.json').write_text(json.dumps(m))
            service=DesktopService(Settings(root=Path(root)))
            rows={r['key']:r for r in service.history()}
            self.assertEqual(rows['temperature_pressure_runs/calibration']['category'],'temperature_calibration')
            self.assertEqual(rows['temperature_pressure_runs/ordinary']['category'],'temperature_pressure')
            self.assertEqual(rows['temperature_pressure_runs/legacy']['category'],'temperature_calibration')
            details=service.details('temperature_pressure_runs/calibration')
            self.assertEqual(details['context']['experiment'],'temperature_calibration')
            self.assertEqual(details['curve']['mode'],'serial')
            self.assertEqual(details['curve']['stored_rows'],4)
            self.assertEqual(len(details['calibration_points']),1)
            self.assertNotIn('PRIVATE',json.dumps(details['metadata']))
            self.assertIn('calibration_points.json',details['files'])
            self.assertTrue(service.download(details['key'],'calibration_points.json').is_file())
            with self.assertRaises(LabError):service.run_path('temperature_pressure_runs/../other')

    def test_export_includes_real_files_and_rejects_mutated_or_unsealed_samples(self):
        with tempfile.TemporaryDirectory() as root:
            run=make_record(root,'sealed');make_record(root,'live',sealed=False)
            service=DesktopService(Settings(root=Path(root)));target=Path(root)/'archive.zip'
            result=service.export_bundle('temperature_pressure_runs/sealed',target)
            self.assertGreater(result['count'],1)
            with zipfile.ZipFile(target) as z:
                self.assertEqual(z.read('results/raw_serial.bin'),(run/'raw_serial.bin').read_bytes())
                self.assertIn('results/calibration_points.json',z.namelist())
            with self.assertRaises(LabError):service.export_bundle('temperature_pressure_runs/live',Path(root)/'live.zip')
            (run/'samples.csv').write_text('changed')
            with self.assertRaises(LabError):service.export_bundle('temperature_pressure_runs/sealed',Path(root)/'bad.zip')
            self.assertFalse((Path(root)/'bad.zip').exists())


class SerialArchiveNavigationTests(unittest.TestCase):
    setUpClass=navigation.ExperimentNavigationTests.__dict__['setUpClass']
    setUp=navigation.ExperimentNavigationTests.setUp
    tearDown=navigation.ExperimentNavigationTests.tearDown
    pump=navigation.ExperimentNavigationTests.pump
    select=navigation.ExperimentNavigationTests.select

    def test_multi_record_analysis_delete_and_restore_refresh_the_report(self):
        from test_serial_comparison_backend import make_serial
        with tempfile.TemporaryDirectory() as root:
            make_serial(root,'cooling',[40,35,30]);make_serial(root,'heating',[40,45])
            archive=DesktopService(Settings(root=Path(root)))
            self.service.rows.extend(archive.history())
            self.service.details=archive.details
            self.service.compare_runs=archive.compare_runs
            self.service.correct_calibration_points=archive.correct_calibration_points
            self.select('temperature_calibration')
            self.window.open_archive()
            self.pump(lambda:not self.window.runner.pending)
            for row in range(2):self.window.history_table.item(row,0).setCheckState(QtCore.Qt.CheckState.Checked)
            self.assertTrue(self.window.compare_button.isEnabled())
            self.window.compare_button.click()
            self.pump(lambda:self.window.comparison_dialog is not None and not self.window.runner.pending)
            self.assertEqual(len(self.window.comparison_dialog.report['tables']['calibration_points']['rows']),10)
            cooling=next(i for i,r in enumerate(self.window.history_rows) if r['key'].endswith('/cooling'))
            self.window.history_table.selectRow(cooling)
            self.pump(lambda:self.window.selected_details is not None)
            self.window.view_history_button.click()
            self.pump(lambda: not self.window.runner.pending and self.window.selected_details.get('curve_loaded'))
            widget=self.window.serial_archive_workspace
            widget.table.selectRow(0)
            with patch.object(QtWidgets.QInputDialog,'getText',return_value=('40 ℃标错',True)):
                widget.calibration_delete_button.click()
            self.pump(lambda:not self.window.runner.pending and self.window.selected_details['calibration_points'][0].get('deleted'))
            self.assertIsNone(self.window.comparison_dialog)
            self.window.compare_button.click()
            self.pump(lambda:self.window.comparison_dialog is not None and not self.window.runner.pending)
            report=self.window.comparison_dialog.report
            self.assertEqual(len(report['tables']['calibration_points']['rows']),8)
            self.assertEqual(report['excluded_calibration_points'][0]['point_id'],1)
            widget.table.selectRow(0)
            widget.calibration_restore_button.click()
            self.pump(lambda:not self.window.runner.pending and not self.window.selected_details['calibration_points'][0].get('deleted'))
            self.assertEqual(self.window.comparison_keys,{r['key'] for r in self.window.history_rows})

    def test_calibration_archive_defaults_to_own_records_and_loads_real_serial_graph(self):
        with tempfile.TemporaryDirectory() as root:
            make_record(root,'calibration');make_record(root,'ordinary',False)
            archive=DesktopService(Settings(root=Path(root)))
            self.service.rows.extend(archive.history())
            self.service.details=archive.details
            self.select('temperature_calibration')
            self.window.open_archive()
            self.pump(lambda:'history' not in self.window.runner.pending)
            self.assertFalse(hasattr(self.window, 'history_scope'))
            self.assertEqual([r['key'] for r in self.window.history_rows],['temperature_pressure_runs/calibration'])
            self.assertEqual(self.window.archive_button.text(),'实验档案')
            self.assertTrue(bool(self.window.history_table.item(0,0).flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable))
            live_cache=[{'sentinel':'live capture'}]
            self.window.temperature_workspace.timer.stop()
            self.window.temperature_workspace._plot_points_cache=live_cache
            with patch.object(TemperaturePressureWidget,'is_running',new_callable=PropertyMock,return_value=True):
                self.window._update_comparison_controls()
                self.window.history_table.selectRow(0)
                self.pump(lambda:self.window.selected_details is not None)
                self.assertIs(self.window.tabs.currentWidget(),self.window.history_table.parentWidget())
                self.assertFalse(self.window.selected_details.get('curve_loaded'))
                self.window.view_history_button.click()
                self.pump(lambda: not self.window.runner.pending and self.window.tabs.currentIndex()==self.window.serial_archive_tab)
                self.assertEqual(self.window.serial_archive_workspace.table.rowCount(),1)
                self.assertTrue(self.window.serial_archive_workspace.lines)
                self.assertIs(self.window.temperature_workspace._plot_points_cache,live_cache)
                self.assertFalse(self.window.replay_button.isEnabled())
                item=self.window.history_table.item(0,0)
                self.assertFalse(bool(item.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable))
            self.window._update_comparison_controls()
            self.assertEqual(len(self.window.history_rows),1)
            self.assertTrue(bool(self.window.history_table.item(0,0).flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable))
            self.select('temperature')
            self.window.open_archive()
            self.pump(lambda:'history' not in self.window.runner.pending)
            self.assertEqual([r['key'] for r in self.window.history_rows],['temperature_pressure_runs/ordinary'])
