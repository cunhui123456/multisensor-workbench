"""已保存实验的只读比较与可追溯导出；不接触采集硬件。"""
from contextlib import contextmanager
from copy import deepcopy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
import zipfile

from sspp_lab.controller import LabError, read_json
from sspp_experiments.comparison import history_category as result_category
from .figure_archive import FigureArchiveMixin, json_bytes, report_digest
from .archive_conditions import archive_conditions



def csv_bytes(rows, columns=None):
    if columns is None:
        columns = list(dict.fromkeys(key for row in rows for key in row))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="raise")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                         for key, value in row.items()})
    return stream.getvalue().encode("utf-8-sig")


class ComparisonMixin(FigureArchiveMixin):
    def _history_row(self, key, kind, manifest, context):
        row = super()._history_row(key, kind, manifest, context)
        row["category"] = result_category(row, context)
        parameters = context.get("parameters")
        row["has_liquid_conditions"] = (
            row["category"].startswith("liquid_temperature_")
            or isinstance(parameters, dict) and isinstance(parameters.get("liquid_temperature"), dict)
        )
        row.update(archive_conditions(row, context, manifest))
        return row

    def _archive_details(self, details, manifest):
        details = super()._archive_details(details, manifest)
        details.update(archive_conditions(details, manifest=manifest))
        return details

    @contextmanager
    def _comparison_operation(self):
        # 与 start / 研究分析 / 其他导出共享短时控制锁，耗时工作在锁外。
        with self.lock:
            if self.closed:
                raise LabError("服务正在关闭", 409)
            if self.preparing or (self.proc and self.proc.poll() is None) or (self.job and self.job.get("status") in {"starting", "running", "stopping", "saving"}):
                raise LabError("请先停止实验并等待保存，再进行结果比较", 409)
            if self.exporting or (getattr(self, "finger_analysis_lock", None) and self.finger_analysis_lock.locked()):
                raise LabError("已有分析或导出任务，请等待完成", 409)
            self.exporting = True
        try:
            yield
        finally:
            with self.lock:
                self.exporting = False

    def compare_runs(self, keys, reference_key=None):
        from sspp_experiments.comparison import build_comparison
        with self._comparison_operation():
            try:
                report = build_comparison(self.root, keys, reference_key)
            except (ValueError, OSError, KeyError) as error:
                raise LabError(self.public_log(str(error))) from error
            code_files = ["sspp_experiments/comparison.py", "sspp_experiments/material_sweep/comparison.py",
                          "sspp_experiments/finger_displacement/comparison.py", "sspp_experiments/temperature_pressure/comparison.py",
                          "sspp_experiments/temperature_pressure/corrections.py",
                          "sspp_experiments/temperature_pressure/pressure.py",
                          "sspp_desktop/comparison_service.py", "sspp_desktop/publication.py",
                          "sspp_desktop/comparison_export.py", "sspp_lab/dsp.py"]
            return self.register_analysis_report(report, code_files)

    def _comparison_source_files(self, report):
        sources = report.get("sources", [])
        if not 1 <= len(sources) <= 8:
            raise LabError("对比报告来源数量无效，请重新比较")
        result, seen = [], set()
        for source in sources:
            run = self.run_path(source["key"])
            if run.is_symlink():
                raise LabError("对比来源不能是符号链接")
            for entry in source["files"]:
                relative = Path(entry["relative_path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise LabError("对比来源路径无效")
                path = run / relative
                if (not path.is_file() or path.is_symlink() or
                        any(p.is_symlink() for p in path.parents if p.is_relative_to(run)) or
                        not path.resolve().is_relative_to(run.resolve())):
                    raise LabError("对比来源文件缺失或路径已变化，请重新比较")
                name = "sources/" + source["key"] + "/" + relative.as_posix()
                if name in seen:
                    raise LabError("对比来源文件重复")
                seen.add(name)
                with path.open("rb") as content:
                    digest = hashlib.file_digest(content, "sha256").hexdigest()
                if digest != entry["sha256"] or path.stat().st_size != entry["bytes"]:
                    raise LabError("实验文件在比较后已改变，请重新比较再导出")
                result.append((path, name, entry))
            if 'diagnostic_dependencies' in source:
                from .publication_analysis import diagnostic_dependency_snapshot
                dependency = source['diagnostic_dependencies']
                if diagnostic_dependency_snapshot(run, dependency.get('mode')) != dependency:
                    raise LabError('诊断依赖文件在分析后已改变，请重新分析再导出')
            correction = source.get('correction')
            if correction is not None:
                expected_path = 'corrections/' + source['key'] + '.json'
                if (not source['key'].startswith('temperature_pressure_runs/')
                        or correction.get('relative_path') != expected_path
                        or type(correction.get('present')) is not bool):
                    raise LabError('温标更正账本路径或状态无效，请重新比较')
                data_root = self.root / 'Data'
                path = data_root / expected_path
                if (path.is_symlink() or any(p.is_symlink() for p in path.parents if p.is_relative_to(data_root))
                        or not path.resolve().is_relative_to(data_root.resolve())):
                    raise LabError('温标更正账本路径已变化，请重新比较')
                if not correction['present']:
                    if path.exists():
                        raise LabError('温标更正账本在比较后已改变，请重新比较再导出')
                    continue
                if not path.is_file():
                    raise LabError('温标更正账本在比较后已改变，请重新比较再导出')
                with path.open('rb') as content:
                    digest = hashlib.file_digest(content, 'sha256').hexdigest()
                if digest != correction['sha256'] or path.stat().st_size != correction['bytes']:
                    raise LabError('温标更正账本在比较后已改变，请重新比较再导出')
                if expected_path in seen:
                    raise LabError('温标更正账本来源重复')
                seen.add(expected_path)
                result.append((path, expected_path, correction))
        return result

    def _verify_comparison_code(self, report):
        for entry in report.get("analysis_code", []):
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise LabError("比较源码路径无效")
            path = self.root / relative
            if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()) or
                    hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]):
                raise LabError("分析或绘图代码在比较后已改变，请重新比较再导出")

    def export_comparison(self, report, destination):
        report = deepcopy(report)
        target = Path(destination).expanduser()
        if not target.is_absolute() or target.suffix.lower() != ".zip":
            raise LabError("请选择绝对路径的 ZIP 保存位置")
        if not target.parent.is_dir() or target.is_symlink() or (target.exists() and not target.is_file()):
            raise LabError("保存位置不是可用的普通文件路径")
        # 任意源记录均不允许成为导出目录，保持后续单次实验包边界清晰。
        if target.resolve().is_relative_to((self.root / "Data").resolve()):
            raise LabError("请选择项目 Data 以外的位置保存对比包")
        temporary = target.parent / ("." + target.name + "." + uuid.uuid4().hex + ".tmp")
        with self._comparison_operation():
            try:
                _, files = self._verify_analysis_report(report)
                with tempfile.TemporaryDirectory(prefix="sspp-comparison-") as scratch:
                    scratch = Path(scratch)
                    report_path = scratch / "comparison.json"
                    report_path.write_bytes(json_bytes(report))
                    script = Path(__file__).with_name("comparison_export.py")
                    process = subprocess.run([sys.executable, "-B", str(script), str(report_path), str(scratch)],
                                             capture_output=True, text=True, timeout=90, env=self.child_env())
                    if process.returncode:
                        raise LabError("对比图表生成失败，原目标文件未覆盖：" + self.public_log(process.stderr[-1200:]))
                    figures = sorted(scratch.glob("comparison_*.png")) + [scratch / "comparison_report.pdf"]
                    if report.get("kind") == "temperature_calibration":
                        channels = sorted({item.get("publication", {}).get("channel") for item in report.get("charts", [])
                                           if item.get("publication", {}).get("kind") == "calibration_response"})
                        if not channels or any(not isinstance(c, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", c) for c in channels):
                            raise LabError("温标发表图缺少有效通道元数据")
                        names = ["publication_figure"] + ["publication_" + channel for channel in channels]
                        figures += [scratch / (name + extension) for name in names for extension in (".pdf", ".svg", ".png")]
                        figures.append(scratch / "caption.txt")
                    if not figures or any(not p.is_file() or p.stat().st_size == 0 for p in figures):
                        raise LabError("对比图表生成不完整，未导出")
                    index = {"schema_version": 1, "kind": "experiment_comparison",
                             "analysis_mode": report.get("analysis_mode", "single" if len(report['sources']) == 1 else "combined"),
                             "created_utc": report["created_utc"], "comparison_id": report["comparison_id"],
                             "reference_key": report["reference_key"], "source_keys": [s["key"] for s in report["sources"]],
                             "raw_iq_included": False, "files": []}
                    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=5, allowZip64=True) as archive:
                        def put(name, data):
                            archive.writestr(name, data)
                            index["files"].append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
                        put("comparison.json", json_bytes(report))
                        put("parameters.json", json_bytes({"sources": [{k: s.get(k) for k in ("key", "label", "parameters")} for s in report["sources"]],
                                                           "compatibility": report["compatibility"]}))
                        put("metrics.csv", csv_bytes(report.get("metrics", [])))
                        for name, table in report.get("tables", {}).items():
                            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                                raise LabError("报告表格名称无效")
                            rows = table["rows"] if isinstance(table, dict) else table
                            columns = table.get("columns") if isinstance(table, dict) else None
                            put("tables/" + name + ".csv", csv_bytes(rows, columns))
                        single = report.get('analysis_mode') == 'single'
                        text = (("SSPP 单记录分析包\n\ncomparison.json：本次记录的曲线、统计、参数和源文件哈希；无跨记录参考或自身差值。\n" if single else
                                 "SSPP 实验结果对比包\n\ncomparison.json：对比设置、参考、计算结果、参数一致性和源文件哈希。\n") +
                                "parameters.json：记录参数及适用的一致性检查。\nmetrics.csv / tables/：实际参与分析的数值表。\n"
                                "figures/：图表 PNG 与多页 PDF；温标分析另含独立发表级 PDF/SVG/600 dpi PNG 和英文图注。\nsources/：对应源结果、参数与基线；不含原始 IQ。\n"
                                "bundle_manifest.json：每个包内文件大小与 SHA-256。\n\n"
                                "扫频差值为目标减参考的数字幅度变化，不是标定 S21，也不能单独证明材料因果。\n"
                                "连续幅相以各自基线和时间起点展示响应，不跨实验相减绝对相位或复用毫米模型。\n"
                                "温标按真实参考温度与区间电压统计分来源展示，重叠点不合并，区间 SD 不是温度不确定度，不自动激活校准。\n\n"
                                + "\n".join("- " + str(w) for w in report.get("warnings", [])) + "\n")
                        put("README.txt", text.encode("utf-8"))
                        for path in figures:
                            put("figures/" + path.name, path.read_bytes())
                        for path, name, entry in files:
                            digest, count = hashlib.sha256(), 0
                            before = path.stat()
                            with path.open("rb") as content, archive.open(name, "w", force_zip64=True) as dest:
                                for block in iter(lambda: content.read(1024 * 1024), b""):
                                    dest.write(block)
                                    digest.update(block)
                                    count += len(block)
                            after = path.stat()
                            if ((before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or
                                    digest.hexdigest() != entry["sha256"] or count != entry["bytes"]):
                                raise LabError("打包时源结果发生变化，已取消并保留原目标文件")
                            index["files"].append({"path": name, "bytes": count, "sha256": digest.hexdigest()})
                        archive.writestr("bundle_manifest.json", json_bytes(index))
                    with zipfile.ZipFile(temporary) as archive:
                        if archive.testzip() is not None:
                            raise LabError("对比压缩包校验失败，原目标文件未覆盖")
                    self._verify_analysis_report(report)
                    with temporary.open("rb") as content:
                        digest = hashlib.file_digest(content, "sha256").hexdigest()
                    size = temporary.stat().st_size
                    os.replace(temporary, target)
                    return {"path": str(target), "count": len(index["files"]) + 1, "bytes": size,
                            "sha256": digest, "warnings": list(report.get("warnings", []))}
            finally:
                if temporary.exists():
                    temporary.unlink()
