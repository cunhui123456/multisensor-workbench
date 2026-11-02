"""液体温度实验的只读分析、版本化人工记录与导出。不会调用硬件。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import zipfile

from sspp_lab.controller import LabError
from sspp_experiments.comparison import COLORS, build_comparison, read_json_source, read_source, _parameters, compare_parameters
from sspp_experiments.liquid_temperature.metadata import number, validate_metadata
from .comparison_service import csv_bytes, json_bytes


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_" + uuid.uuid4().hex[:6]


class LiquidTemperatureMixin:
    def _liquid_record(self, key, completed=False):
        path = self.run_path(key)
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p.is_relative_to(self.root)):
            raise LabError("液体实验来源不能是符号链接")
        record = {"key": key, "_path": path, "_contents": {}, "files": []}
        context = read_json_source(record, "desktop_context.json")
        mode = "sweep" if key.startswith("runs/") else "phase"
        if context.get("experiment") != "liquid_temperature_" + mode:
            raise LabError("请选择明确记录液体温度条件的实验；旧实验不能补猜温度")
        metadata = validate_metadata((context.get("parameters") or {}).get("liquid_temperature"))
        manifest = read_json_source(record, "manifest.json")
        if completed:
            telemetry = manifest.get("telemetry") or {}
            if (manifest.get("status") not in ("complete", "stopped") or manifest.get("errors") != []
                    or context.get("desktop_status") in ("failed", "stop_unconfirmed", "initialization_failed")
                    or telemetry.get("tx_end_of_burst_sent") is not True
                    or telemetry.get("workers_stopped") is not True
                    or not (telemetry.get("rx_stop_sent") is True or telemetry.get("rx_stop_command_sent") is True)):
                raise LabError("实验未完成、存在错误或缺少收发停止证据，不能分析")
        record.update(metadata=metadata, mode=mode, _manifest=manifest, _context=context)
        return record

    def liquid_history(self):
        result = []
        for row in self.history():
            if row.get("category") not in ("liquid_temperature_sweep", "liquid_temperature_phase"):
                continue
            try:
                record = self._liquid_record(row["key"])
            except (ValueError, OSError, LabError):
                continue
            result.append({**row, "temperature_c": record["metadata"]["temperature_c"]})
        return result

    def _liquid_annotation(self, record):
        folder = record["_path"] / "liquid_temperature_annotations"
        if folder.is_symlink():
            raise LabError("人工记录目录不能是符号链接")
        candidates = sorted(folder.glob("*.json")) if folder.exists() else []
        if not candidates:
            return {"annotations": [], "temperature_end_c": None}
        saved = read_json_source(record, candidates[-1].relative_to(record["_path"]).as_posix())
        for entry in saved.get("source_files", []):
            content = read_source(record, entry["relative_path"])
            if len(content) != entry["bytes"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise LabError("人工区间对应的原始来源已改变，不能复用旧标注")
        return saved

    def liquid_load(self, key):
        record = self._liquid_record(key)
        saved = self._liquid_annotation(record)
        return {"key": key, "mode": record["mode"], "metadata": record["metadata"],
                "annotations": saved.get("annotations", []), "temperature_end_c": saved.get("temperature_end_c"),
                "analysis": None}

    def liquid_save_annotations(self, key, annotations, temperature_end_c=None):
        with self._comparison_operation():
            record = self._liquid_record(key, completed=True)
            if not isinstance(annotations, list) or len(annotations) > 500:
                raise LabError("稳定区间必须为最多 500 条记录的列表")
            if temperature_end_c is not None:
                temperature_end_c = number(temperature_end_c, "结束参考温度")
            read_source(record, "amplitude_phase.jsonl" if record["mode"] == "phase" else "measurements.csv")
            if record["mode"] == "phase":
                read_source(record, "baseline.json")
            # 精确绑定原始来源。修改区间生成新版本，不改原始文件或已有标注。
            folder = record["_path"] / "liquid_temperature_annotations"
            if folder.is_symlink():
                raise LabError("人工记录目录不能是符号链接")
            folder.mkdir(exist_ok=True)
            value = {"schema_version": 1, "run_key": key, "annotations": deepcopy(annotations),
                     "temperature_end_c": temperature_end_c, "created_utc": datetime.now(timezone.utc).isoformat(),
                     "reference_source": "operator_reference_thermometer_and_ruler",
                     "source_files": deepcopy(record["files"])}
            content = json_bytes(value)
            with (folder / (_stamp() + ".json")).open("xb") as out:
                out.write(content)
            return {"key": key, "mode": record["mode"], "metadata": record["metadata"], **value, "analysis": None}

    @staticmethod
    def _liquid_source(record):
        return {"key": record["key"], "files": deepcopy(record["files"]), "metadata": record["metadata"],
                "parameters": deepcopy(record.get("parameters", {}))}

    def _liquid_verify_sources(self, sources):
        for source in sources:
            record = self._liquid_record(source["key"])
            for entry in source["files"]:
                content = read_source(record, entry["relative_path"])
                if len(content) != entry["bytes"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                    raise LabError("实验来源在分析后已改变，请重新分析")

    def _liquid_save_report(self, report):
        report["created_utc"] = datetime.now(timezone.utc).isoformat()
        code_names = ("sspp_desktop/liquid_temperature_service.py", "sspp_desktop/comparison_export.py",
                      "sspp_experiments/liquid_temperature/analysis.py", "sspp_experiments/liquid_temperature/metadata.py",
                      "sspp_experiments/finger_displacement/analysis.py", "sspp_experiments/comparison.py",
                      "sspp_experiments/material_sweep/comparison.py")
        code_root = Path(__file__).resolve().parents[1]
        report["analysis_code"] = [{"path": name, "sha256": hashlib.sha256((code_root/name).read_bytes()).hexdigest()}
                                   for name in code_names if (code_root/name).is_file()]
        base = self.root / "Data" / "liquid_temperature_reports"
        if base.is_symlink() or (self.root / "Data").is_symlink():
            raise LabError("报告目录不能是符号链接")
        output = base / _stamp()
        output.mkdir(parents=True, exist_ok=False)
        report["export_path"] = str(output)
        (output / "analysis.json").write_bytes(json_bytes(report))
        for name, rows in report.get("tables", {}).items():
            (output / (name + ".csv")).write_bytes(csv_bytes(rows["rows"] if isinstance(rows, dict) else rows))
        (output / "README.txt").write_text(
            "液体温度影响实验\n数字幅度并非校准 S21。相位只在同一连续 run 内比较。"
            "参考温度和位置为人工仪表记录；首尾温度不能证明全程恒温。\n"
            "status=partial/rejected 表示数据或质量不足；软件验证不等于真实温度效应。\n\n"
            + "\n".join(report.get("errors", []) + report.get("warnings", [])), encoding="utf-8")
        if report.get("charts"):
            proc = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("comparison_export.py")),
                                   str(output / "analysis.json"), str(output)], capture_output=True, text=True, timeout=90)
            if proc.returncode:
                raise LabError("分析已保存，但图表生成失败：" + self.public_log(proc.stderr[-600:]))
        self._liquid_verify_sources(report["sources"])
        files = [{"path": p.name, "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                 for p in sorted(output.iterdir()) if p.is_file()]
        (output / "report_manifest.json").write_bytes(json_bytes({"files": files, "raw_iq_included": False}))
        return self.register_analysis_report(report, code_files=code_names)

    def liquid_analyze_phase(self, key, annotations, temperature_tolerance_c=.5):
        from sspp_experiments.liquid_temperature.analysis import analyze_phase
        with self._comparison_operation():
            record = self._liquid_record(key, completed=True)
            if record["mode"] != "phase":
                raise LabError("请选择固定载频相位记录")
            baseline = read_json_source(record, "baseline.json")
            content = read_source(record, "amplitude_phase.jsonl")
            rows = [json.loads(line) for line in content.splitlines() if line.strip()]
            record["kind"] = "liquid_temperature_phase"
            record["parameters"] = _parameters(record)
            report = analyze_phase(rows, deepcopy(annotations), baseline, temperature_tolerance_c=temperature_tolerance_c)
            report.update(kind="liquid_temperature_phase", run_key=key, annotations=deepcopy(annotations),
                          metadata=record["metadata"], sources=[self._liquid_source(record)],
                          temperature_tolerance_c=temperature_tolerance_c)
            curves = []
            for i, block in enumerate(report["blocks"]):
                points = block.get("points", [])
                if points and block["status"] == "ready":
                    curves.append({"label": f"{block['block']} · {block['temperature_mean_c']:g} ℃", "color": COLORS[i % len(COLORS)],
                                   "data": [[p["displacement_mm"], p["delta_phase_deg"]] for p in points]})
            report["charts"] = [{"id": "phase_displacement", "title": "各温度平台的相对相位—位移响应",
                                 "xlabel": "相对各平台参考位置的位移（mm，远离为正）", "ylabel": "相对相位（°）",
                                 "series": curves, "note": "同一次连续采集；斜率是局部描述，不是毫米精度或跨次标定。"}] if curves else []
            if report["thermal_drift"]:
                report["charts"].append({"id": "thermal_drift", "title": "同run固定距离相位差（温度与时间可能混淆）",
                    "xlabel": "相对首个同距离reference的温差（℃）", "ylabel": "相对相位（°）",
                    "series": [{"label": f"{v['reference_block']} → {v['target_block']} · {v['distance_mm']:g} mm",
                                "color": COLORS[i % len(COLORS)], "data": [[0., 0.], [v["temperature_difference_c"], v["phase_difference_deg"]]]}
                               for i, v in enumerate(report["thermal_drift"])],
                    "note": "只在同一连续run的共同参考距离比较，不扣除时间漂移，不独立证明温度因果。"})
            report["tables"] = {"windows": report["windows"], "blocks": report["blocks"], "thermal_drift": report["thermal_drift"]}
            return self._liquid_save_report(report)

    def liquid_compare_sweeps(self, keys, reference_key=None, temperature_tolerance_c=.5):
        tolerance = number(temperature_tolerance_c, "温度容差", 0, 100)
        with self._comparison_operation():
            report = build_comparison(self.root, keys, reference_key)
            if report["kind"] != "liquid_temperature_sweep":
                raise LabError("请选择 2–8 条液体温度扫频记录")
            records = [self._liquid_record(key, completed=True) for key in keys]
            conditions, reasons = [], []
            for record in records:
                saved = self._liquid_annotation(record)
                start = record["metadata"]["temperature_c"]
                end = saved.get("temperature_end_c")
                if end is not None:
                    end = number(end, "结束参考温度")
                stable = end is not None and abs(end - start) <= tolerance
                if not stable:
                    reasons.append(f"{record['key']} 缺少结束温度或首尾温差超过 {tolerance:g} ℃")
                conditions.append({"key": record["key"], **record["metadata"], "temperature_end_c": end,
                                   "temperature_mean_c": (start + end)/2 if end is not None else None,
                                   "endpoint_temperature_check_passed": stable})
            for field in ("liquid", "sample_id", "container", "volume_ml", "geometry", "distance_mm", "thermometer"):
                if any(r["metadata"][field] != records[0]["metadata"][field] for r in records[1:]):
                    reasons.append(f"条件 {field} 不一致，不能单独解释为温度效应")
            report["temperature_tolerance_c"] = tolerance
            report["temperature_quantitative_allowed"] = bool(report["quantitative_difference_allowed"] and not reasons)
            report["errors"] = []
            report["warnings"].extend(reasons + ["温度为开始/结束仪表示值，不能证明全程恒温；升温顺序与时间漂移可能混淆，需降温回测和独立重复。"])
            report["status"] = "ready" if report["temperature_quantitative_allowed"] else "partial"
            report["tables"]["temperature_conditions"] = conditions
            report["conditions"] = conditions
            for chart in report["charts"]:
                for curve in chart.get("series", []):
                    for source, condition in zip(report["sources"], conditions):
                        label = source["label"] + " · " + source["key"].split("/")[-1]
                        if curve["label"] == label:
                            temperature = condition["temperature_mean_c"]
                            curve["label"] += f" · {temperature:g} ℃" if temperature is not None else " · 结束温度未记录"
                            break
            for source, record in zip(report["sources"], records):
                files = {entry["relative_path"]: entry for entry in source["files"]}
                for entry in record["files"]:
                    previous = files.get(entry["relative_path"])
                    if previous is not None and previous != entry:
                        raise LabError("分析读取过程中来源已改变，请重新分析")
                    files[entry["relative_path"]] = entry
                source.update(files=list(files.values()), metadata=record["metadata"])
            # 保留既有参数严格检查；温度条件不合格时仅显示叠加，不留下差值表误导使用。
            if not report["temperature_quantitative_allowed"]:
                report["quantitative_difference_allowed"] = False
                report["charts"] = [c for c in report["charts"] if c["id"] != "sweep_difference"]
                for name in list(report["tables"]):
                    if "difference" in name:
                        del report["tables"][name]
                for metric in report["metrics"]:
                    metric["mean_difference_dB"] = metric["max_abs_difference_dB"] = None
            ref = next(c for c in conditions if c["key"] == report["reference_key"])
            coeff = []
            if report["temperature_quantitative_allowed"]:
                table = report["tables"].get("sweep_difference", {})
                differences = table.get("rows", []) if isinstance(table, dict) else table
                for item in differences:
                    condition = next(c for c in conditions if c["key"] == item["key"])
                    dt = condition["temperature_mean_c"] - ref["temperature_mean_c"]
                    if abs(dt) > tolerance:
                        coeff.append({**item, "delta_temperature_c": dt, "amplitude_change_dB_per_c": item["difference_dB"]/dt})
            report["tables"]["temperature_coefficients"] = coeff
            if not coeff:
                report["warnings"].append("没有可用的不同温度配对，未计算 dB/℃ 温度系数。")
            return self._liquid_save_report(report)

    def liquid_compare_phase_runs(self, keys, temperature_tolerance_c=.5):
        """只比较各run各自reference下的响应和斜率，不跨run相减相位截距。"""
        from sspp_experiments.liquid_temperature.analysis import analyze_phase
        if not isinstance(keys, (list, tuple)) or not 2 <= len(keys) <= 8 or len(set(keys)) != len(keys):
            raise LabError("请选择 2–8 条不同的液体温度相位记录")
        with self._comparison_operation():
            records, analyses = [], []
            for key in keys:
                record = self._liquid_record(key, completed=True)
                if record["mode"] != "phase":
                    raise LabError("相位比较不能混入扫频记录")
                saved = self._liquid_annotation(record)
                rows = [json.loads(line) for line in read_source(record, "amplitude_phase.jsonl").splitlines() if line.strip()]
                baseline = read_json_source(record, "baseline.json")
                analysis = analyze_phase(rows, saved.get("annotations", []), baseline,
                                         temperature_tolerance_c=temperature_tolerance_c)
                analysis["run_key"] = key
                record.update(kind="liquid_temperature_phase", label=key)
                record["parameters"] = _parameters(record)
                records.append(record)
                analyses.append(analysis)
            report = {"schema_version": 1, "kind": "liquid_temperature_phase", "status": "ready", "keys": list(keys),
                      "scope": "compare_within_run_relative_responses_only", "errors": [], "warnings": [],
                      "compatibility": [], "analyses": analyses, "charts": [], "blocks": [], "thermal_drift": [],
                      "sources": [self._liquid_source(r) for r in records], "temperature_tolerance_c": temperature_tolerance_c}
            compatible = compare_parameters(records, 0, report)
            reasons = []
            for field in ("liquid", "sample_id", "container", "volume_ml", "geometry", "distance_mm", "thermometer"):
                if any(r["metadata"][field] != records[0]["metadata"][field] for r in records[1:]):
                    reasons.append(f"条件 {field} 不一致，斜率变化不能单独解释为温度效应")
            curves, ranges = [], []
            for i, analysis in enumerate(analyses):
                report["errors"].extend(f"{keys[i]}: {e}" for e in analysis["errors"])
                report["warnings"].extend(w for w in analysis["warnings"] if not w.startswith("目前仅有一个温度平台"))
                for block in analysis["blocks"]:
                    item = {**block, "run_key": keys[i], "block": keys[i] + " / " + block["block"]}
                    report["blocks"].append(item)
                    if block["status"] != "ready":
                        continue
                    points = block["points"]
                    ranges.append((block["reference_distance_mm"], tuple(sorted(set(p["displacement_mm"] for p in points)))))
                    curves.append({"label": f"{item['block']} · {block['temperature_mean_c']:g} ℃", "color": COLORS[i],
                                   "data": [[p["displacement_mm"], p["delta_phase_deg"]] for p in points]})
            if ranges and any(value != ranges[0] for value in ranges[1:]):
                reasons.append("参考位置或采样位置集合不一致；局部斜率不能直接作温度定量比较")
            report["temperature_quantitative_allowed"] = bool(compatible and not reasons and not report["errors"] and len(curves) >= 2)
            report["status"] = "ready" if report["temperature_quantitative_allowed"] else ("partial" if curves else "rejected")
            report["warnings"] = list(dict.fromkeys(report["warnings"] + reasons + [
                "各run仅使用自己的reference相位零点；未计算跨run绝对相位差或温度相位截距。",
                "斜率需在相同采集设置与位置范围下比较；拟合误差不等于位移精度，独立取放重复另行评价。",
            ]))
            if curves:
                report["charts"] = [{"id": "phase_displacement", "title": "不同温度记录的相对相位—位移响应",
                                     "xlabel": "相对各自reference的位移（mm）", "ylabel": "相对相位（°）", "series": curves,
                                     "note": "每条曲线以各自run的参考位置归零；不同run绝对相位不可比较。"}]
            report["tables"] = {"blocks": report["blocks"], "windows": [dict(w, run_key=a["run_key"]) for a in analyses for w in a["windows"]]}
            return self._liquid_save_report(report)

    def liquid_export(self, report_path, destination):
        output = Path(report_path)
        if output.name == "analysis.json":
            output = output.parent
        base = self.root / "Data" / "liquid_temperature_reports"
        if (not output.is_dir() or output.parent.resolve() != base.resolve() or output.is_symlink()
                or base.is_symlink() or (self.root / "Data").is_symlink()):
            raise LabError("请选择本项目生成的液体温度报告目录")
        target = Path(destination).expanduser()
        if (not target.is_absolute() or target.suffix.lower() != ".zip" or not target.parent.is_dir()
                or target.is_symlink() or target.resolve().is_relative_to((self.root / "Data").resolve())):
            raise LabError("请选择项目 Data 之外的 ZIP 保存位置")
        with self._comparison_operation():
            report = json.loads((output / "analysis.json").read_bytes())
            self._liquid_verify_sources(report["sources"])
            index = json.loads((output / "report_manifest.json").read_bytes())
            temporary = target.parent / ("." + target.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                    for entry in index["files"]:
                        name = entry["path"]
                        if Path(name).name != name:
                            raise LabError("报告文件路径无效")
                        path = output / name
                        if path.is_symlink():
                            raise LabError("报告文件不能是符号链接")
                        content = path.read_bytes()
                        if len(content) != entry["bytes"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                            raise LabError("报告文件已改变，请重新分析")
                        archive.writestr(name, content)
                    archive.writestr("report_manifest.json", json_bytes(index))
                self._liquid_verify_sources(report["sources"])
                with zipfile.ZipFile(temporary) as archive:
                    if archive.testzip() is not None:
                        raise LabError("ZIP 校验失败")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            return {"path": str(target), "bytes": target.stat().st_size, "raw_iq_included": False}
