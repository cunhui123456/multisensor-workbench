"""Deletion/restore preserve capture bytes and propagate to reports and exports."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_experiments.temperature_pressure.corrections import correction_path
from sspp_lab.controller import LabError
from test_serial_archive import make_record


class CalibrationCorrectionsTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name)
        self.run=make_record(self.root,'cooling')
        file=self.run/'calibration_points.json'
        self.points={'points':[]}
        for index,temp in enumerate((40,35,30),1):
            self.points['points'].append({'id':index,'status':'saved','reference_mean_c':temp,
                'channels':{'S6':{'mean_v':3+index/10,'sample_sd_v':.01,'count':2}},'quality_flags':[]})
        file.write_text(json.dumps(self.points))
        self.before={p.name:p.read_bytes() for p in self.run.iterdir()}
        self.key='temperature_pressure_runs/cooling'
        self.service=DesktopService(Settings(root=self.root))

    def test_delete_restore_retain_original_bytes_and_export_audit(self):
        self.service.correct_calibration_points(self.key,[1],'40 ℃标错')
        details=self.service.details(self.key)
        self.assertEqual([p['id'] for p in details['calibration_points'] if p['deleted']],[1])
        self.assertEqual(details['calibration_points'][0]['deletion']['reason'],'40 ℃标错')
        target=self.root/'corrected.zip'
        self.service.export_bundle(self.key,target)
        with zipfile.ZipFile(target) as z:
            self.assertEqual(z.read('results/calibration_points.json'),self.before['calibration_points.json'])
            self.assertIn('1',json.loads(z.read('corrections.json'))['deleted_points'])
            for entry in json.loads(z.read('bundle_index.json'))['files']:
                self.assertEqual(hashlib.sha256(z.read(entry['path'])).hexdigest(),entry['sha256'])
        self.service.correct_calibration_points(self.key,[1],'恢复误删',restore=True)
        self.assertFalse(any(p['deleted'] for p in self.service.details(self.key)['calibration_points']))
        ledger=json.loads(correction_path(self.root,self.key).read_text())
        self.assertEqual([event['action'] for event in ledger['history']],['delete','restore'])
        self.assertEqual({p.name:p.read_bytes() for p in self.run.iterdir()},self.before)

    def test_invalid_ids_active_capture_and_changed_source_are_rejected(self):
        for ids in ([99],[True],[]):
            with self.subTest(ids=ids),self.assertRaises(LabError):
                self.service.correct_calibration_points(self.key,ids,'错误')
        self.service.job={'status':'running'}
        with self.assertRaises(LabError):self.service.correct_calibration_points(self.key,[1],'错误')
        self.service.job=None
        self.service.correct_calibration_points(self.key,[1],'错误')
        with self.assertRaises(LabError):self.service.correct_calibration_points(self.key,[1],'重复删除')
        with self.assertRaises(LabError):self.service.correct_calibration_points(self.key,[2],'未删除',restore=True)
        (self.run/'calibration_points.json').write_text(json.dumps(self.points)+' ')
        with self.assertRaises(LabError):self.service.correct_calibration_points(self.key,[1],'恢复',restore=True)

    def test_running_record_and_linked_correction_directory_are_rejected(self):
        make_record(self.root,'live',sealed=False)
        with self.assertRaises(LabError):self.service.correct_calibration_points('temperature_pressure_runs/live',[1],'错误')
        outside=self.root/'outside';outside.mkdir()
        (self.root/'Data'/'corrections').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(LabError):self.service.correct_calibration_points(self.key,[1],'错误')


if __name__=='__main__':unittest.main()
