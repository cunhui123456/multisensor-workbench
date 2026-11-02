"""可携带实验包：参数、结果、图表与逐文件完整性索引。"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import zipfile

from sspp_lab.controller import LabError, read_json

RESULT_EXTENSIONS = {".json", ".jsonl", ".csv", ".xlsx", ".png", ".pdf", ".txt", ".log"}
RAW_EXTENSIONS = {".cf32", ".npz"}


def _json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _teacher_exports(root, manifest):
    """把旧绝对路径按 Data/train 后缀映射到当前副本，核对原记录哈希。

    只读取当前项目中的普通 XLSX；不读取旧路径所指向的另一项目，也不跟随
    符号链接。老师导出位于 run 目录之外，必须显式加入可携带实验包。
    """
    exports = manifest.get("exports", [])
    if not isinstance(exports, list):
        raise LabError("老师实验的 Excel 导出索引结构无效")
    candidates, seen = [], set()
    for entry in exports:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            raise LabError("老师实验缺少有效 Excel 来源路径")
        original = Path(entry["file"])
        parts = original.parts
        anchors = [i for i in range(len(parts)-1) if parts[i:i+2] == ("Data", "train")]
        if ".." in parts or len(anchors) != 1 or original.suffix.lower() != ".xlsx":
            raise LabError("老师 Excel 路径必须唯一映射到 Data/train，且不含上级目录跳转")
        tail = parts[anchors[0]+2:]
        if not tail:
            raise LabError("老师 Excel 路径无效")
        expected = entry.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise LabError("老师 Excel 缺少有效的原始 SHA-256 记录，不能核验后打包")
        base = root / "Data" / "train"
        source = base.joinpath(*tail)
        if (source.is_symlink() or any(p.is_symlink() for p in source.parents if p.is_relative_to(root)) or
                not source.resolve().is_relative_to(base.resolve())):
            raise LabError("老师 Excel 的当前副本不能是符号链接或越出 Data/train")
        if not source.is_file():
            raise LabError("老师实验缺少核心 Excel 当前副本：Data/train/" + "/".join(tail))
        with source.open("rb") as content:
            actual = hashlib.file_digest(content, "sha256").hexdigest()
        if actual != expected.lower():
            raise LabError("老师 Excel 与历史 SHA-256 不一致，已取消打包并保留原目标文件")
        name = "results/exports/" + "/".join(tail)
        if name not in seen:
            candidates.append((source, name, expected.lower()))
            seen.add(name)
    if not candidates and manifest.get("status") == "complete":
        raise LabError("完整老师实验缺少核心 Excel 导出索引，不能生成不完整实验包")
    return candidates


class BundleExportMixin:
    def export_bundle(self, key, destination, include_raw=False):
        """由用户选定保存位置；耗时任务在分析工作线程执行，最终原子提交 ZIP。"""
        if not isinstance(include_raw, bool):
            raise LabError("原始 IQ 导出选项无效")
        target = Path(destination).expanduser()
        if not target.is_absolute() or target.suffix.lower() != ".zip":
            raise LabError("请选择绝对路径，文件扩展名应为 .zip")
        if not target.parent.is_dir() or target.is_symlink() or (target.exists() and not target.is_file()):
            raise LabError("导出位置不是可用的普通文件路径")
        with self.lock:
            if self.preparing or (self.proc and self.proc.poll() is None) or (self.job and self.job.get("status") in {"starting", "running", "stopping", "saving"}):
                raise LabError("请先停止采集并等待保存完成，再打包导出", 409)
            if getattr(self, "finger_analysis_lock", None) and self.finger_analysis_lock.locked():
                raise LabError("研究分析正在保存，请等待完成后导出", 409)
            if self.exporting:
                raise LabError("已有导出任务正在运行", 409)
            if key is None:
                output = (self.job or {}).get("output")
                if not output:
                    raise LabError("请先选择实验记录")
                key = str(Path(output).relative_to("Data"))
            run = self.run_path(key)
            # 归档不可嵌套在源实验目录，以免下一次打包递归包含归档本身。
            if target.resolve().is_relative_to(run.resolve()):
                raise LabError("请选择本次实验原始目录以外的位置保存实验包")
            self.exporting = True
        temporary = target.parent / ("." + target.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            # 独立进程绘图；不持控制锁、不使用硬件、不通过 shell。
            warnings = []
            manifest_name = "feature_report.json" if key.startswith("teacher_runs/") else "manifest.json"
            manifest = read_json(run / manifest_name, {}) or {}
            if not key.startswith("teacher_runs/"):
                figure_error = None
                try:
                    result = subprocess.run([sys.executable, "-B", "-m", "sspp_lab.export_figures",
                                             "--root", str(self.root), "--key", key],
                                            cwd=self.root, capture_output=True, text=True, timeout=90, env=self.child_env())
                    if result.returncode:
                        figure_error = self.public_log(result.stderr[-1000:])
                except subprocess.TimeoutExpired:
                    if manifest.get("status") == "complete":
                        raise
                    figure_error = "图表生成超过 90 秒"
                if figure_error is not None:
                    if manifest.get("status") == "complete":
                        raise LabError("重要图表生成失败，未导出不完整实验包：" + figure_error, 500)
                    warnings.append("源实验未完成（状态：" + str(manifest.get("status", "unknown")) +
                                    "），图表生成失败；仅打包实际存在的参数、日志、结果和图，不补造缺失数据：" + figure_error)
            else:
                warnings.append("老师历史实验仅打包原有图表；缺少连续 IQ，不能补造诊断图。")
            context = read_json(run / "desktop_context.json", {}) or read_json(run / "web_context.json", {}) or {}
            parameters = {"schema_version": 1, "run_key": key,
                          "experiment": context.get("experiment", "finger_displacement" if key.startswith("phase_runs/") else "material_sweep"),
                          "label": manifest.get("label", manifest.get("initial_label", manifest.get("material_input"))),
                          "sensor": context.get("sensor"), "desktop_parameters": context.get("parameters", {}),
                          "finger_research_settings": (read_json(run / "finger_annotations.json", {}) or {}).get("settings", {}),
                          "requested_sdr": manifest.get("requested_sdr", manifest.get("sdr", {})), "actual_sdr": manifest.get("actual_sdr", {}),
                          "actual_hardware": manifest.get("actual_hardware", {}),
                          "sweep_config": manifest.get("sweep", manifest.get("sweep_config", manifest.get("requested_sweep", {}))),
                          "duration_limit_s": manifest.get("duration_limit_s"),
                          "temperature_sweep": manifest.get("temperature_sweep"),
                          "sweep_algorithm": manifest.get("sweep_algorithm"),
                          "source_manifest": "results/" + manifest_name,
                          "missing_fields_are_unknown_not_defaults": True}
            candidates = []
            expected_hashes = {}
            excluded_raw = []
            for source in sorted(run.rglob("*")):
                if not source.is_file() or source.is_symlink() or any(p.is_symlink() for p in source.parents if p.is_relative_to(run)):
                    continue
                relative = source.relative_to(run)
                if any(part.startswith(".") for part in relative.parts):
                    continue
                if source.suffix in RAW_EXTENSIONS and not include_raw:
                    excluded_raw.append({"path": str(relative), "bytes": source.stat().st_size})
                    continue
                serial_raw = relative.as_posix() == "temperature/raw_serial.bin"
                if not serial_raw and source.suffix not in RESULT_EXTENSIONS | (RAW_EXTENSIONS if include_raw else set()):
                    continue
                candidates.append((source, "results/" + str(relative)))
            if key.startswith("teacher_runs/"):
                teacher_exports = _teacher_exports(self.root, manifest)
                for source, name, expected in teacher_exports:
                    candidates.append((source, name))
                    expected_hashes[name] = expected
                if not teacher_exports:
                    warnings.append("该未完成的老师实验没有记录 Excel 导出；包内只保留实际存在的结果。")
            if not candidates:
                raise LabError("该实验没有可打包的结果文件")
            index = {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
                     "run_key": key, "include_raw_iq": include_raw, "warnings": warnings,
                     "source_status": manifest.get("status"),
                     "raw_iq_excluded": excluded_raw, "files": []}
            readme = ("SSPP 实验导出包\n\nparameters.json：参数设置（缺失字段表示历史记录未保存，不填默认值）。\n"
                      "results/：原有结果数据、重要图表、标注、标定和验证报告。\n"
                      "bundle_manifest.json：逐文件 SHA-256、大小、导出选项与来源实验。\n\n"
                      "标定及毫米估算仅绑定源连续采集；不能套用到新基线。失败实验仍保留原失败状态。\n"
                      "温度联测包含 temperature/ 原始串口字节、电压与逐点关联结果；未标定时不输出传感器温度。\n"
                      "新版扫频的 sweep_quality.jsonl 保存逐次验证、被拒窗口与重测证据；算法版本记录于 parameters.json。\n"
                      + ("包含原始 IQ（cf32 / npz）。\n" if include_raw else "未包含原始 IQ（cf32 / npz）；幅相 JSONL 与结果表仍包含。\n"))
            if warnings:
                readme += "\n导出说明与缺失项：\n" + "\n".join("- " + warning for warning in warnings) + "\n"
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=5, allowZip64=True) as archive:
                for name, data in [("parameters.json", _json(parameters)), ("README.txt", readme.encode("utf-8"))]:
                    archive.writestr(name, data)
                    index["files"].append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
                for source, name in candidates:
                    before = source.stat()
                    digest, size = hashlib.sha256(), 0
                    with source.open("rb") as content, archive.open(name, "w", force_zip64=True) as entry:
                        for block in iter(lambda: content.read(1024 * 1024), b""):
                            entry.write(block)
                            digest.update(block)
                            size += len(block)
                    after = source.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != before.st_size:
                        raise LabError("打包时源数据发生变化，已取消，请等待数据保存结束后重试")
                    if name in expected_hashes and digest.hexdigest() != expected_hashes[name]:
                        raise LabError("老师 Excel 在打包前后与历史 SHA-256 不一致，已取消导出")
                    index["files"].append({"path": name, "bytes": size, "sha256": digest.hexdigest()})
                archive.writestr("bundle_manifest.json", _json(index))
            with zipfile.ZipFile(temporary) as archive:
                if archive.testzip() is not None:
                    raise LabError("压缩包校验失败，未覆盖目标文件")
            digest = hashlib.sha256()
            with temporary.open("rb") as archive:
                for block in iter(lambda: archive.read(1024 * 1024), b""):
                    digest.update(block)
            os.replace(temporary, target)
            return {"path": str(target), "files": index["files"], "count": len(index["files"]) + 1,
                    "bytes": target.stat().st_size, "sha256": digest.hexdigest(), "warnings": warnings,
                    "include_raw_iq": include_raw}
        except subprocess.TimeoutExpired as exc:
            raise LabError("图表生成超过 90 秒，打包未完成，可稍后重试", 503) from exc
        finally:
            if temporary.exists():
                temporary.unlink()
            with self.lock:
                self.exporting = False
