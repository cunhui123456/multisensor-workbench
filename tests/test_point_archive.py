"""Lossless point NPZ compatibility and fail-closed registration; no hardware."""
import hashlib
import json
from pathlib import Path
from queue import Queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import numpy as np

from sspp_experiments.material_sweep import acquisition, logic


class PointArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.frames = [np.array([1+.5j, -0.-.25j, 0.+0.j], dtype=np.complex64),
                       np.array([-.75+.125j, .25-.5j], dtype=np.complex64)]
        self.info = {"point_number":1, "requested_carrier_hz":5e9, "tone_hz":2000.,
                     "measurement_status":"invalid", "reason_code":"point_quality_timeout", "note":"原始样点"}
        self.arrays = {"iq":np.concatenate(self.frames), "frame_start_indices":np.array([2**40,2**40+3], dtype=np.int64),
                       "frame_lengths":np.array([3,2]), "rx_rate":250000., "point_info":json.dumps(self.info)}
        self.runner = acquisition.make_runner(SimpleNamespace(rx_rate_actual=250000.), logic.SweepConfig(),
            None, Queue(), threading.Event(), None, self.output, SimpleNamespace(point_timeout=8.), [])
        self.runner.point_number = 1
        self.runner.point_info = dict(self.info)
        self.runner.frames = list(self.frames)
        self.runner.indices = [2**40,2**40+3]

    def test_numpy_load_and_all_npy_entry_bytes_match_legacy_archive(self):
        old, new = self.output/'old.npz', self.output/'new.npz'
        np.savez_compressed(old, **self.arrays)
        acquisition.save_point_archive(new, **self.arrays)
        with zipfile.ZipFile(old) as prior, zipfile.ZipFile(new) as current:
            self.assertEqual(current.namelist(), prior.namelist())
            self.assertIsNone(current.testzip())
            for name in prior.namelist():
                self.assertEqual(current.read(name), prior.read(name))
                self.assertEqual(current.getinfo(name).compress_type, zipfile.ZIP_DEFLATED)
        with np.load(new, allow_pickle=False) as current:
            self.assertEqual(current.files, list(self.arrays))
            for name, value in self.arrays.items():
                expected = np.asanyarray(value)
                self.assertEqual(current[name].dtype, expected.dtype)
                self.assertEqual(current[name].shape, expected.shape)
                self.assertEqual(current[name].tobytes(), expected.tobytes())
            self.assertEqual(json.loads(current['point_info'].item()), self.info)

    def test_flush_closes_zip_before_hash_and_registers_actual_file(self):
        opened = []
        original_zip = zipfile.ZipFile
        original_sha = acquisition.sha
        class TrackedZip(original_zip):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                opened.append(self)
        def checked_sha(path):
            self.assertTrue(opened)
            self.assertTrue(all(archive.fp is None for archive in opened))
            with original_zip(path) as archive:
                self.assertIsNone(archive.testzip())
            return original_sha(path)
        with patch.object(acquisition.zipfile, 'ZipFile', TrackedZip), patch.object(acquisition, 'sha', side_effect=checked_sha):
            self.runner.flush_point()
        path = self.output/'iq_point_001.npz'
        self.assertEqual(self.runner.raw_files, [{'file':path.name, 'sha256':hashlib.sha256(path.read_bytes()).hexdigest(), **self.info}])
        self.assertEqual(self.runner.frames, [])
        self.assertEqual(self.runner.indices, [])
        with np.load(path, allow_pickle=False) as saved:
            for name, value in self.arrays.items():
                self.assertEqual(saved[name].tobytes(), np.asanyarray(value).tobytes())

    def test_write_failure_closes_zip_and_never_registers_or_clears_unsaved_frames(self):
        opened = []
        original_zip = zipfile.ZipFile
        class TrackedZip(original_zip):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                opened.append(self)
        with patch.object(acquisition.zipfile, 'ZipFile', TrackedZip), \
             patch.object(np.lib.format, 'write_array', side_effect=OSError('offline disk failure')), \
             patch.object(acquisition, 'sha') as hash_file:
            with self.assertRaisesRegex(OSError, 'offline disk failure'):
                self.runner.flush_point()
            hash_file.assert_not_called()
        self.assertTrue(opened)
        self.assertTrue(all(archive.fp is None for archive in opened))
        self.assertEqual(self.runner.raw_files, [])
        self.assertEqual(len(self.runner.frames), 2)
        self.assertEqual(self.runner.indices, [2**40,2**40+3])


if __name__ == '__main__':
    unittest.main()
