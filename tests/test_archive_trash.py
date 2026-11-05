"""Recoverable whole-record deletion, source preservation, and operation guards."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sspp_desktop import archive_trash
from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_lab.controller import LabError


class ArchiveTrashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = DesktopService(Settings(root=self.root))

    def record(self, key="runs/a", *, category=None, status="failed", context_status=None):
        path = self.root / "Data" / key
        path.mkdir(parents=True)
        manifest = {"label": "样品 " + key, "sample_name": "样品 " + key,
                    "status": status, "started_utc": "2026-11-05T01:02:03+00:00",
                    "errors": ["original acquisition failure"], "telemetry": {"workers_stopped": False}}
        if category in ("temperature_calibration", "pressure_calibration"):
            manifest[category] = {"enabled": True}
            (path / "calibration_points.json").write_text(json.dumps({"points": [{"id": 1, "status": "saved"}]}))
        name = "feature_report.json" if key.startswith("teacher_runs/") else "manifest.json"
        (path / name).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        context = {}
        if category:
            context["experiment"] = category
        if context_status:
            context["desktop_status"] = context_status
        if context:
            (path / "desktop_context.json").write_text(json.dumps(context), encoding="utf-8")
        (path / "raw").mkdir()
        (path / "raw" / "sample.bin").write_bytes(bytes(range(256)) + b"\x00raw\xff")
        (path / "events.jsonl").write_bytes(b'{"saved":true}\n')
        return path

    @staticmethod
    def contents(path):
        return {item.relative_to(path).as_posix(): ("link", str(item.readlink())) if item.is_symlink()
                else ("file", item.read_bytes()) for item in path.rglob("*") if item.is_symlink() or item.is_file()}

    def test_all_history_categories_round_trip_without_changing_any_record_bytes(self):
        records = [("runs/material", "material_sweep"), ("runs/temperature", "temperature_sweep"),
                   ("runs/liquid", "liquid_temperature_sweep"), ("phase_runs/history", "phase_history"),
                   ("phase_runs/finger", "finger_displacement"), ("phase_runs/liquid", "liquid_temperature_phase"),
                   ("teacher_runs/teacher", "teacher_sweep"),
                   ("temperature_pressure_runs/serial", "temperature_pressure"),
                   ("temperature_pressure_runs/temperature", "temperature_calibration"),
                   ("temperature_pressure_runs/pressure", "pressure_calibration")]
        expected = {}
        for key, category in records:
            expected[key] = self.contents(self.record(key, category=category))
        before = self.service.history()  # Exercise the complete MRO and populate summary cache.
        self.assertEqual({row["key"]: row["category"] for row in before}, dict(records))
        result = self.service.trash_records([key for key, _ in records])
        self.assertEqual(result["deleted_keys"], [key for key, _ in records])
        self.assertEqual(self.service.history(), [])
        trashed = self.service.trashed_records()
        self.assertEqual({row["key"]: row["category"] for row in trashed}, dict(records))
        for row in trashed:
            with self.subTest(key=row["key"]):
                container = self.root / "Data/.archive_trash" / row["trash_id"]
                self.assertEqual(self.contents(container / "record"), expected[row["key"]])
                self.assertEqual(json.loads((container / "entry.json").read_text())["key"], row["key"])
        restored = self.service.restore_records([row["trash_id"] for row in trashed])
        self.assertEqual(set(restored["restored_keys"]), set(expected))
        self.assertEqual(self.service.history(), before)
        self.assertEqual(self.service.trashed_records(), [])
        for key, original in expected.items():
            self.assertEqual(self.contents(self.root / "Data" / key), original)

    def test_external_sidecars_and_calibration_corrections_stay_in_place(self):
        key = "temperature_pressure_runs/calibration"
        run = self.record(key, category="temperature_calibration")
        calibration = (run / "calibration_points.json").read_bytes()
        ledger = {"version": 1, "run_key": key, "source_calibration_sha256": hashlib.sha256(calibration).hexdigest(),
                  "deleted_points": {"1": {"reason": "误标", "deleted_utc": "2026-11-05T00:00:00+00:00"}}, "history": []}
        paths = {"Data/corrections/temperature_pressure_runs/calibration.json": json.dumps(ledger).encode(),
                 "Data/train/shared.xlsx": b"shared teacher export",
                 "Data/liquid_temperature_reports/report.json": b"existing report",
                 "Figures/report.pdf": b"existing figure"}
        for name, content in paths.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.service.trash_records([key])
        for name, content in paths.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        self.service.restore_records([self.service.trashed_records()[0]["trash_id"]])
        self.assertTrue(self.service.details(key, include_curve=False)["calibration_points"][0]["deleted"])
        for name, content in paths.items():
            self.assertEqual((self.root / name).read_bytes(), content)

    def test_failed_incomplete_and_collector_terminal_records_remain_removable(self):
        for index, status in enumerate(("failed", "initialization_failed", "stop_unconfirmed", "unknown")):
            with self.subTest(status=status):
                key = f"runs/failure_{index}"
                path = self.record(key, status=("initializing" if status == "initialization_failed" else "running") if status != "unknown" else "unknown",
                                   context_status=status if status != "unknown" else None)
                original = self.contents(path)
                self.service.trash_records([key])
                row = next(row for row in self.service.trashed_records() if row["key"] == key)
                self.service.restore_records([row["trash_id"]])
                self.assertEqual(self.contents(path), original)
        for index, contents in enumerate((b"{broken json", None)):
            with self.subTest(manifest=contents):
                key = f"runs/incomplete_{index}"
                path = self.record(key)
                if contents is None:
                    (path / "manifest.json").unlink()
                else:
                    (path / "manifest.json").write_bytes(contents)
                original = self.contents(path)
                self.service.trash_records([key])
                row = next(row for row in self.service.trashed_records() if row["key"] == key)
                self.service.restore_records([row["trash_id"]])
                self.assertEqual(self.contents(path), original)

    def test_active_disk_statuses_are_rejected_without_an_in_memory_job(self):
        for index, status in enumerate(("initializing", "starting", "running", "stopping", "saving")):
            for context in (False, True):
                with self.subTest(status=status, context=context):
                    key = f"runs/active_{index}_{int(context)}"
                    path = self.record(key, status="failed" if context else status,
                                       context_status=status if context else None)
                    with self.assertRaisesRegex(LabError, "采集或保存"):
                        self.service.trash_records([key])
                    self.assertTrue(path.is_dir())

    def test_live_sessions_initialization_analysis_and_collector_block_mutation(self):
        path = self.record()
        states = [{"closed": True}, {"preparing": True}, {"exporting": True},
                  {"_environment_active": True}, {"_environment_starting": True},
                  {"proc": SimpleNamespace(poll=lambda: None)},
                  {"job": {"status": "saving"}}, {"job": {"status": "initializing"}},
                  {"proc": SimpleNamespace(poll=lambda: 0), "collector_thread": SimpleNamespace(is_alive=lambda: True)}]
        for state in states:
            with self.subTest(state=list(state)), patch.multiple(self.service, **state):
                with self.assertRaises(LabError):
                    self.service.trash_records(["runs/a"])
                self.assertTrue(path.is_dir())
        for lock in (self.service.finger_analysis_lock, self.service.diagnostic_lock):
            with self.subTest(lock=lock), lock:
                with self.assertRaises(LabError):
                    self.service.trash_records(["runs/a"])
        session = SimpleNamespace(is_running=True)
        for sources in ((session,), (lambda: session,), (None, lambda: session)):
            with self.subTest(sources=sources):
                self.service.bind_archive_activity(*sources)
                with self.assertRaisesRegex(LabError, "串口采集"):
                    self.service.trash_records(["runs/a"])
        session.is_running = False
        self.service.trash_records(["runs/a"])
        session.is_running = True
        with self.assertRaises(LabError):
            self.service.restore_records([self.service.trashed_records()[0]["trash_id"]])

    def test_reservation_prevents_new_initialization_analysis_or_export(self):
        self.record()
        with self.service._archive_operation():
            self.assertTrue(self.service.exporting)
            with self.assertRaises(LabError):
                self.service._reserve_environment()
            with self.assertRaises(LabError), self.service._comparison_operation():
                self.fail("comparison entered during archive mutation")
            with self.assertRaises(LabError):
                self.service.export_bundle("runs/a", self.root / "export.zip")
        self.assertFalse(self.service.exporting)
        self.assertFalse(self.service.diagnostic_lock.locked())

    def test_invalid_batches_are_rejected_before_the_first_move(self):
        path = self.record()
        for keys in ([], ["runs/a", "runs/a"], ["runs/a", "runs/missing"], ["runs/a", "../outside"],
                     ["/tmp/a"], ["runs/a/extra"], ["Data/runs/a"], ["train/a"], [True], "runs/a"):
            with self.subTest(keys=keys), self.assertRaises(LabError):
                self.service.trash_records(keys)
            self.assertTrue(path.is_dir())
        self.assertFalse((self.root / "Data/.archive_trash").exists())
        self.service.trash_records(["runs/a"])
        trash_id = self.service.trashed_records()[0]["trash_id"]
        for ids in ([], [trash_id, trash_id], [trash_id, "0" * 32], ["../outside"], [True], trash_id):
            with self.subTest(ids=ids), self.assertRaises(LabError):
                self.service.restore_records(ids)
            self.assertFalse(path.exists())
            self.assertEqual(len(self.service.trashed_records()), 1)

    def test_directory_and_metadata_links_are_rejected_without_following_them(self):
        path = self.record()
        for name, target in (("runs/link", path), ("runs/dangling", self.root / "missing")):
            link = self.root / "Data" / name
            link.symlink_to(target, target_is_directory=True)
            with self.subTest(name=name), self.assertRaises(LabError):
                self.service.trash_records([name])
            self.assertTrue(link.is_symlink())
        for relative in ("Data", "Data/runs"):
            original = self.root / relative
            moved = original.with_name(original.name + "_actual")
            original.rename(moved)
            original.symlink_to(moved, target_is_directory=True)
            try:
                with self.subTest(root=relative), self.assertRaises(LabError):
                    self.service.trash_records(["runs/a"])
            finally:
                original.unlink()
                moved.rename(original)
        external = self.root / "external"
        external.mkdir()
        trash_root = self.root / "Data/.archive_trash"
        trash_root.symlink_to(external, target_is_directory=True)
        with self.assertRaises(LabError):
            self.service.trash_records(["runs/a"])
        self.assertEqual(list(external.iterdir()), [])
        trash_root.unlink()
        manifest = path / "manifest.json"
        metadata = manifest.read_bytes()
        manifest.unlink()
        outside = external / "manifest.json"
        outside.write_bytes(metadata)
        manifest.symlink_to(outside)
        with self.assertRaises(LabError):
            self.service.trash_records(["runs/a"])
        self.assertEqual(outside.read_bytes(), metadata)

    def test_internal_data_links_move_verbatim_and_never_move_their_target(self):
        path = self.record()
        external = self.root / "external.bin"
        external.write_bytes(b"external bytes remain")
        (path / "raw" / "link.bin").symlink_to(external)
        before = self.contents(path)
        self.service.trash_records(["runs/a"])
        self.assertEqual(external.read_bytes(), b"external bytes remain")
        self.service.restore_records([self.service.trashed_records()[0]["trash_id"]])
        self.assertEqual(self.contents(path), before)

    def test_restore_conflicts_including_dangling_links_do_not_overwrite_or_partially_restore(self):
        first, second = self.record("runs/a"), self.record("runs/b")
        self.service.trash_records(["runs/a", "runs/b"])
        ids = {row["key"]: row["trash_id"] for row in self.service.trashed_records()}
        for conflict in ("directory", "file", "dangling"):
            with self.subTest(conflict=conflict):
                if conflict == "directory":
                    second.mkdir()
                elif conflict == "file":
                    second.write_bytes(b"existing unrelated file")
                else:
                    second.symlink_to(self.root / "missing", target_is_directory=True)
                with self.assertRaisesRegex(LabError, "不能覆盖"):
                    self.service.restore_records([ids["runs/a"], ids["runs/b"]])
                self.assertFalse(first.exists())
                self.assertEqual(len(self.service.trashed_records()), 2)
                if conflict == "directory":
                    second.rmdir()
                else:
                    second.unlink()

    def test_mid_batch_move_failures_roll_back_both_delete_and_restore(self):
        first, second = self.record("runs/a"), self.record("runs/b")
        before = {path: self.contents(path) for path in (first, second)}
        rename = archive_trash._rename_directory
        for restore in (False, True):
            with self.subTest(restore=restore):
                if restore:
                    self.service.trash_records(["runs/a", "runs/b"])
                    ids = [row["trash_id"] for row in self.service.trashed_records()]
                calls = 0

                def fail_second(source, target):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise OSError("injected second rename failure")
                    return rename(source, target)

                with patch.object(archive_trash, "_rename_directory", side_effect=fail_second), self.assertRaises(LabError):
                    if restore:
                        self.service.restore_records(ids)
                    else:
                        self.service.trash_records(["runs/a", "runs/b"])
                self.assertFalse(self.service.exporting)
                if restore:
                    self.assertFalse(first.exists())
                    self.assertFalse(second.exists())
                    self.assertEqual(len(self.service.trashed_records()), 2)
                    self.service.restore_records(ids)
                else:
                    self.assertEqual(self.service.trashed_records(), [])
                for path, expected in before.items():
                    self.assertEqual(self.contents(path), expected)

    def test_metadata_write_failure_never_loses_original_or_trashed_records(self):
        path = self.record()
        original = self.contents(path)
        with patch.object(archive_trash, "_write_entry", side_effect=OSError("entry disk failure")), self.assertRaises(LabError):
            self.service.trash_records(["runs/a"])
        self.assertEqual(self.contents(path), original)
        self.service.trash_records(["runs/a"])
        trash_id = self.service.trashed_records()[0]["trash_id"]
        with patch.object(archive_trash, "_write_entry", side_effect=OSError("entry disk failure")), self.assertRaises(LabError):
            self.service.restore_records([trash_id])
        self.assertFalse(path.exists())
        self.assertEqual(len(self.service.trashed_records()), 1)
        self.service.restore_records([trash_id])
        self.assertEqual(self.contents(path), original)

    def test_failed_rollback_reports_partial_keys_and_preserves_recoverable_entry(self):
        first, second = self.record("runs/a"), self.record("runs/b")
        original = self.contents(first)
        rename = archive_trash._rename_directory
        calls = 0

        def fail_forward_and_rollback(source, target):
            nonlocal calls
            calls += 1
            if calls in (2, 3):
                raise OSError("injected move/rollback failure")
            return rename(source, target)

        with patch.object(archive_trash, "_rename_directory", side_effect=fail_forward_and_rollback):
            with self.assertRaises(LabError) as raised:
                self.service.trash_records(["runs/a", "runs/b"])
        self.assertEqual(raised.exception.deleted_keys, ["runs/a"])
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual([row["key"] for row in self.service.trashed_records()], ["runs/a"])
        self.service.restore_records([self.service.trashed_records()[0]["trash_id"]])
        self.assertEqual(self.contents(first), original)

    def test_corrupt_and_restored_trash_entries_do_not_hide_valid_records(self):
        self.record()
        self.service.trash_records(["runs/a"])
        valid = self.service.trashed_records()[0]
        trash = self.root / "Data/.archive_trash"
        corrupt = trash / ("e" * 32)
        corrupt.mkdir()
        (corrupt / "entry.json").write_text("{bad")
        (corrupt / "record").mkdir()
        (trash / ("f" * 32)).symlink_to(trash / valid["trash_id"], target_is_directory=True)
        self.assertEqual(self.service.trashed_records(), [valid])
        with self.assertRaises(LabError):
            self.service.restore_records(["e" * 32])
        with self.assertRaises(LabError):
            self.service.restore_records(["f" * 32])
        self.service.restore_records([valid["trash_id"]])
        self.assertEqual(self.service.trashed_records(), [])

    def test_deleted_current_result_and_caches_stay_detached_after_restore(self):
        self.record()
        self.service.history()
        self.service.diagnostic_cache["runs/a"] = "stale plot"
        self.service._comparison_digests = {"report": "stale report"}
        self.service.job = {"status": "stop_unconfirmed", "output": "Data/runs/a", "config": {"mode": "sweep"}}
        self.service.points.append({"amplitude": 1})
        self.service.live_points.append({"x": 1})
        self.service.temperature_points.append({"voltage_v": 2.5})
        self.service.preview = {"old": True}
        self.service.trash_records(["runs/a"])
        self.assertIsNone(self.service.job["output"])
        self.assertEqual(self.service.job["archive_trashed_output"], "Data/runs/a")
        snapshot = self.service.live_snapshot()
        self.assertEqual(snapshot["points"], [])
        self.assertEqual(snapshot["temperature_points"], [])
        self.assertIsNone(snapshot["preview"])
        self.assertNotIn("runs/a", self.service._archive_rows)
        self.assertEqual(self.service.diagnostic_cache, {})
        self.assertEqual(self.service._comparison_digests, {})
        self.service.restore_records([self.service.trashed_records()[0]["trash_id"]])
        self.assertIsNone(self.service.job["output"])
        self.assertEqual([row["key"] for row in self.service.history()], ["runs/a"])


if __name__ == "__main__":
    unittest.main()
