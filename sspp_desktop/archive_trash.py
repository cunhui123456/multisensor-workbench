"""Recoverable archive removal: move one run directory, never its external sidecars."""
from contextlib import contextmanager, ExitStack
import ctypes
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import re
import sys
import threading
import uuid

from sspp_lab.controller import LabError
from .serial_archive import serial_category


_KEY = re.compile(r"(runs|phase_runs|teacher_runs|temperature_pressure_runs)/[A-Za-z0-9_-]+")
_ID = re.compile(r"[0-9a-f]{32}")
_ACTIVE = {"initializing", "starting", "running", "stopping", "saving"}
_FINISHED = {"complete", "complete_with_gaps", "stopped", "failed", "stop_unconfirmed", "initialization_failed"}
_SCHEMA = "sspp.archive_trash.v1"


def _present(path):
    return path.exists() or path.is_symlink()


def _directory(path):
    if path.is_symlink() or not path.is_dir():
        raise LabError("档案路径不是普通目录：" + path.name)
    return path


def _rename_directory(source, target):
    """Same-volume atomic rename with an OS-level no-replace guarantee."""
    if _present(target):
        raise FileExistsError(errno.EEXIST, "目标已存在，不能覆盖", str(target))
    if source.stat().st_dev != target.parent.stat().st_dev:
        raise OSError(errno.EXDEV, "回收站与记录必须位于同一文件系统")
    if os.name == "nt":
        os.rename(source, target)  # Windows rename refuses an existing target.
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        args = (os.fsencode(source), os.fsencode(target), 0x00000004)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        args = (-100, os.fsencode(source), -100, os.fsencode(target), 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "当前系统不支持不覆盖目标的目录移动")
    rename.restype = ctypes.c_int
    if rename(*args):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def _metadata(path):
    if path.is_symlink():
        raise LabError("实验元数据不能是符号链接：" + path.name)
    if not path.exists():
        return {}
    if not path.is_file():
        raise LabError("实验元数据不是普通文件：" + path.name)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        # Failed/incomplete archives remain recoverably removable.
        return {}
    return value if isinstance(value, dict) else {}


def _write_entry(path, value):
    temporary = path.with_name(".entry-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise LabError("回收站元数据不能是符号链接")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


class ArchiveTrashMixin:
    def __init__(self, *args, **kwargs):
        self._archive_activity_sources = ()
        self._archive_trash_lock = threading.RLock()
        super().__init__(*args, **kwargs)

    def bind_archive_activity(self, *sources):
        """Bind backend sessions (or resolvers); never inspect Qt widgets in a worker."""
        with self.lock:
            self._archive_activity_sources = sources

    @contextmanager
    def _archive_operation(self):
        with self._archive_trash_lock, ExitStack() as stack:
            with self.lock:
                if (getattr(self, "_environment_active", False)
                        or getattr(self, "_environment_starting", False)):
                    raise LabError("初始化或启动检查尚未完成，请稍后删除或恢复档案", 409)
                if (self.job or {}).get("status") in _ACTIVE:
                    raise LabError("实验仍在初始化、采集或保存，请稍后删除或恢复档案", 409)
                collector = getattr(self, "collector_thread", None)
                if collector is not None and collector.is_alive():
                    raise LabError("实验仍在收尾保存，请稍后删除或恢复档案", 409)
                for source in self._archive_activity_sources:
                    source = source() if callable(source) else source
                    if source is not None and source.is_running:
                        raise LabError("串口采集、固件准备或外部分析仍在运行，请完成后操作档案", 409)
                stack.enter_context(self._comparison_operation())
                if not self.diagnostic_lock.acquire(blocking=False):
                    raise LabError("档案诊断正在读取，请等待完成", 409)
                stack.callback(self.diagnostic_lock.release)
            yield

    def _archive_data(self):
        _directory(self.root)
        return _directory(self.root / "Data")

    def _archive_source(self, key, *, required=True):
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise LabError("实验编号无效")
        data = self._archive_data()
        kind, name = key.split("/")
        parent = data / kind
        _directory(parent)
        path = parent / name
        if required:
            _directory(path)
        elif _present(path):
            raise LabError("原实验路径已存在，不能覆盖：" + key, 409)
        return path

    def _archive_trash_root(self, *, create=False):
        path = self._archive_data() / ".archive_trash"
        if _present(path):
            return _directory(path)
        if create:
            path.mkdir()
            return _directory(path)
        return path

    @staticmethod
    def _archive_keys(values, pattern, noun):
        if (not isinstance(values, (list, tuple)) or not values
                or any(not isinstance(value, str) or not pattern.fullmatch(value) for value in values)
                or len(set(values)) != len(values)):
            raise LabError("请选择不同的有效" + noun)
        return list(values)

    def _archive_record_metadata(self, key, path):
        kind = key.split("/")[0]
        manifest = _metadata(path / ("feature_report.json" if kind == "teacher_runs" else "manifest.json"))
        desktop, web = _metadata(path / "desktop_context.json"), _metadata(path / "web_context.json")
        context = desktop or web
        context_status = context.get("desktop_status", context.get("web_status"))
        # Match the history row: a collector's saved failure/stop-unconfirmed
        # result can supersede the crashed child's stale "running" manifest.
        status = context_status if isinstance(context_status, str) and context_status in _FINISHED else manifest.get("status")
        if ((isinstance(context_status, str) and context_status in _ACTIVE)
                or (isinstance(status, str) and status in _ACTIVE)):
            raise LabError("记录仍标记为采集或保存中，不能移动：" + key, 409)
        if kind == "temperature_pressure_runs":
            calibration = _metadata(path / "calibration_points.json")
            return {"label": str(manifest.get("sample_name") or manifest.get("label") or path.name),
                    "category": serial_category(path, manifest, calibration)}
        row = self._history_row(key, kind, manifest, context)
        return {"label": str(row["label"]), "category": row["category"]}

    def _read_trash_entry(self, trash_id):
        if not isinstance(trash_id, str) or not _ID.fullmatch(trash_id):
            raise LabError("回收站编号无效")
        container = _directory(self._archive_trash_root() / trash_id)
        path = container / "entry.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
            raise LabError("回收站条目缺少普通元数据文件")
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(entry, dict) or entry.get("schema") != _SCHEMA
                    or entry.get("trash_id") != trash_id or not isinstance(entry.get("key"), str)
                    or not _KEY.fullmatch(entry["key"])
                    or any(not isinstance(entry.get(field), str) for field in ("label", "category", "deleted_utc"))
                    or datetime.fromisoformat(entry["deleted_utc"].replace("Z", "+00:00")).tzinfo is None):
                raise ValueError("条目字段无效")
        except (ValueError, UnicodeError, TypeError, KeyError) as error:
            raise LabError("回收站条目元数据无效：" + trash_id) from error
        _directory(container / "record")
        return entry, container

    def trashed_records(self):
        with self._archive_trash_lock:
            if not (self.root / "Data").exists() and not (self.root / "Data").is_symlink():
                return []
            root = self._archive_trash_root()
            if not root.exists():
                return []
            rows = []
            for path in root.iterdir():
                try:
                    entry, _ = self._read_trash_entry(path.name)
                except (LabError, OSError):
                    # One interrupted/restored/corrupt item must not hide other recoverable runs.
                    continue
                rows.append({field: entry[field] for field in ("trash_id", "key", "label", "category", "deleted_utc")})
            return sorted(rows, key=lambda row: (row["deleted_utc"], row["trash_id"]), reverse=True)

    def _invalidate_archive_state(self, keys):
        with self._archive_lock:
            for key in keys:
                self._archive_rows.pop(key, None)
        # _archive_operation owns diagnostic_lock and reserves every analysis/export.
        self.diagnostic_cache.clear()
        with self.lock:
            getattr(self, "_comparison_digests", {}).clear()

    def _detach_trashed_current_result(self, entries):
        with self.lock:
            output = (self.job or {}).get("output")
            if not output:
                return
            for entry in entries:
                if Path(output) not in (Path("Data") / entry["key"], self.root / "Data" / entry["key"]):
                    continue
                self.job.update(output=None, archive_trashed_output=output, archive_trash_id=entry["trash_id"])
                self.points.clear()
                self.live_points.clear()
                self.temperature_points.clear()
                self.temperature_rounds.clear()
                self.frequency_ranking.clear()
                self.preview = None
                self._research_reset()
                self.revision += 1

    @staticmethod
    def _rollback(moves):
        failures = []
        for source, target, entry in reversed(moves):
            try:
                _directory(target)
                _directory(source.parent)
                _rename_directory(target, source)
            except (OSError, LabError) as error:
                failures.append((entry, str(error)))
        return failures

    def trash_records(self, keys):
        keys = self._archive_keys(keys, _KEY, "实验记录")
        with self._archive_operation():
            planned, moved = [], []
            try:
                # Validate the complete batch before creating any trash entries or moving a run.
                for key in keys:
                    source = self._archive_source(key)
                    metadata = self._archive_record_metadata(key, source)
                    planned.append((source, {"schema": _SCHEMA, "trash_id": uuid.uuid4().hex,
                                             "key": key, **metadata,
                                             "deleted_utc": datetime.now(timezone.utc).isoformat(),
                                             "state": "trashed", "external_sidecars": "retained_at_original_paths"}))
                trash = self._archive_trash_root(create=True)
                for source, entry in planned:
                    container = trash / entry["trash_id"]
                    container.mkdir()
                    _write_entry(container / "entry.json", entry)
                for source, entry in planned:
                    self._archive_source(entry["key"])
                    self._archive_record_metadata(entry["key"], source)
                    container = _directory(self._archive_trash_root() / entry["trash_id"])
                    target = container / "record"
                    _rename_directory(source, target)
                    moved.append((source, target, entry))
            except Exception as error:
                failures = self._rollback(moved)
                remaining = [entry for _, target, entry in moved if target.is_dir() and not target.is_symlink()]
                self._detach_trashed_current_result(remaining)
                if failures:
                    failure = LabError("删除未全部完成，部分记录仍在回收站，可恢复：" +
                                       "、".join(entry["key"] for entry in remaining), 500)
                    failure.deleted_keys = [entry["key"] for entry in remaining]
                    failure.rollback_errors = failures
                    raise failure from error
                if isinstance(error, LabError):
                    raise
                raise LabError("删除失败，已移动的记录已回滚：" + self.public_log(str(error)), 500) from error
            finally:
                self._invalidate_archive_state(keys)
            self._detach_trashed_current_result([entry for _, entry in planned])
            return {"deleted_keys": keys}

    def restore_records(self, trash_ids):
        trash_ids = self._archive_keys(trash_ids, _ID, "回收站条目")
        with self._archive_operation():
            planned, moved = [], []
            try:
                for trash_id in trash_ids:
                    entry, container = self._read_trash_entry(trash_id)
                    target = self._archive_source(entry["key"], required=False)
                    if any(previous[2]["key"] == entry["key"] for previous in planned):
                        raise LabError("所选回收站条目对应同一原路径，不能同时恢复")
                    planned.append((container / "record", target, entry))
                for source, target, entry in planned:
                    self._read_trash_entry(entry["trash_id"])
                    self._archive_source(entry["key"], required=False)
                    _rename_directory(source, target)
                    moved.append((source, target, entry))
                for source, _, entry in planned:
                    _write_entry(source.parent / "entry.json", {**entry, "state": "restored",
                                 "restored_utc": datetime.now(timezone.utc).isoformat()})
            except Exception as error:
                failures = self._rollback(moved)
                if failures:
                    restored = [entry["key"] for _, target, entry in moved if target.is_dir() and not target.is_symlink()]
                    failure = LabError("恢复未全部完成，已恢复记录：" + ("、".join(restored) or "无") +
                                       "；其余记录仍保存在回收站，请刷新后重试", 500)
                    failure.restored_keys = restored
                    failure.rollback_errors = failures
                    raise failure from error
                if isinstance(error, LabError):
                    raise
                raise LabError("恢复失败，记录仍保存在回收站：" + self.public_log(str(error)), 500) from error
            finally:
                self._invalidate_archive_state([entry["key"] for _, _, entry in planned])
            # Restoring files never reactivates an old job or opens an old live view.
            return {"restored_keys": [entry["key"] for _, _, entry in planned]}
