"""Archive display caching and lazy curves stay separate from source validation."""
from collections import Counter
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_lab.controller import LabError
from test_serial_comparison_backend import make_serial


class ArchivePerformanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = DesktopService(Settings(root=self.root))

    def rf_record(self, key='runs/sample'):
        path = self.root / 'Data' / key
        path.mkdir(parents=True)
        (path / 'manifest.json').write_text(json.dumps({'status':'complete','label':'sample'}))
        (path / 'desktop_context.json').write_text(json.dumps({'experiment':'material_sweep',
            'parameters':{'label':'sample','sensor':'SSPP_A'}}))
        (path / 'measurements.csv').write_text('requested_carrier_GHz,peak_amplitude_dBFS_est,repeat,point\n5.0,-40.0,1,1\n')
        return path

    def test_index_reads_each_metadata_once_then_uses_isolated_summaries(self):
        path = self.rf_record()
        calls = Counter()
        original = Path.open

        def counted(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            calls[path.name] += 1
            return result

        with patch.object(Path, 'open', counted):
            first = self.service.history()
        self.assertEqual(calls, {'manifest.json':1,'desktop_context.json':1})
        with patch.object(Path, 'open', side_effect=AssertionError('Warm index must not reread sources')):
            second = self.service.history()
        self.assertEqual(first, second)
        first[0]['condition_summary'] = 'changed by caller'
        self.assertNotEqual(self.service.history()[0]['condition_summary'], first[0]['condition_summary'])

    def test_same_size_metadata_edit_with_restored_mtime_invalidates_index(self):
        path = self.rf_record() / 'desktop_context.json'
        self.assertIn('SSPP_A', self.service.history()[0]['condition_search'])
        before = path.stat()
        path.write_text(path.read_text().replace('SSPP_A','SSPP_B'))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_size,before.st_size)
        self.assertIn('SSPP_B', self.service.history()[0]['condition_search'])

    def test_new_records_and_removed_records_do_not_hide_behind_cache(self):
        first = self.rf_record()
        self.assertEqual(len(self.service.history()),1)
        self.rf_record('runs/new')
        self.assertEqual(len(self.service.history()),2)
        first.rename(first.with_name('renamed'))
        self.assertEqual({r['key'] for r in self.service.history()},{'runs/renamed','runs/new'})

    def test_changed_during_read_and_failed_builds_are_not_cached(self):
        path = self.rf_record() / 'manifest.json'
        calls = []

        def changing():
            calls.append(True)
            path.write_text(path.read_text()+' ')
            return {'value':len(calls)}

        for _ in range(2):
            self.service._cached_archive_row('changing',[path],changing)
        self.assertEqual(len(calls),2)
        with patch.object(self.service,'_history_row',side_effect=ValueError('bad metadata')) as build:
            for _ in range(2):
                with self.assertRaises(ValueError):self.service.history()
            self.assertEqual(build.call_count,2)

    def test_lightweight_rf_details_skip_curve_and_full_view_reads_new_data(self):
        path = self.rf_record()
        with patch.object(self.service,'curve',side_effect=AssertionError('Do not load curve on selection')):
            summary = self.service.details('runs/sample',include_curve=False)
        self.assertFalse(summary['curve_loaded'])
        self.assertEqual(summary['curve']['points'],[])
        self.assertIn('SSPP_A',summary['condition_search'])
        (path/'measurements.csv').write_text((path/'measurements.csv').read_text().replace('-40.0','-42.0'))
        full = self.service.details('runs/sample')
        self.assertTrue(full['curve_loaded'])
        self.assertEqual(full['curve']['points'][0]['amplitude_dB'],-42.0)

    def test_lightweight_serial_preserves_fresh_corrections_without_opening_samples(self):
        path = make_serial(self.root,'serial',[30])
        key = 'temperature_pressure_runs/serial'
        original = Path.open

        def no_samples(path,*args,**kwargs):
            if path.name in ('samples.csv','raw_serial.bin'):
                raise AssertionError('Selection must not read samples')
            return original(path,*args,**kwargs)

        self.service.history()
        with patch.object(Path,'open',no_samples):
            summary = self.service.details(key,include_curve=False)
        self.assertFalse(summary['curve_loaded'])
        self.assertFalse(summary['calibration_points'][0]['deleted'])
        self.service.correct_calibration_points(key,[1],'incorrect reference')
        with patch.object(Path,'open',no_samples):
            changed = self.service.details(key,include_curve=False)
        self.assertTrue(changed['calibration_points'][0]['deleted'])
        self.service.correct_calibration_points(key,[1],'restored',restore=True)
        self.assertFalse(self.service.details(key,include_curve=False)['calibration_points'][0]['deleted'])
        ledger = self.root/'Data/corrections/temperature_pressure_runs/serial.json'
        data = json.loads(ledger.read_text());data['source_calibration_sha256']='0'*64
        ledger.write_text(json.dumps(data))
        with self.assertRaises(LabError):self.service.details(key,include_curve=False)

    def test_analysis_still_rejects_changed_sources_after_index_and_summary(self):
        path = make_serial(self.root,'serial',[30])
        key = 'temperature_pressure_runs/serial'
        self.service.history()
        self.service.details(key,include_curve=False)
        (path/'samples.csv').write_text('changed')
        with self.assertRaises(LabError):self.service.compare_runs([key])


if __name__ == '__main__':
    unittest.main()
