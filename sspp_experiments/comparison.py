"""只读、多实验记录对比入口；不导入采集器、UHD 或 Qt。"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

SWEEP_QUALITY_VERSION = "sspp.sweep.full_window_quality.v2"
ADAPTIVE_SWEEP_VERSION = "sspp.sweep.adaptive_coherent_quality.v3"
TARGET_CONFIRMED_SWEEP_VERSION = "sspp.sweep.target_confirmed.v5"
WINDOW_EFFICIENT_SWEEP_VERSION = "sspp.sweep.target_confirmed.v6"
ROUND_AWARE_SWEEP_VERSION = "sspp.sweep.target_confirmed.v7"
WEAK_POINT_BUDGET_SWEEP_VERSION = "sspp.sweep.target_confirmed.v8"
TARGET_CONFIRMED_SWEEP_VERSIONS = (TARGET_CONFIRMED_SWEEP_VERSION, WINDOW_EFFICIENT_SWEEP_VERSION,
                                 ROUND_AWARE_SWEEP_VERSION, WEAK_POINT_BUDGET_SWEEP_VERSION)
PREVIOUS_ROUND_WINDOW_POLICIES = {
    True: "previous_round_same_frequency_successful_adaptive_level_only",
    False: "disabled",
}
WEAK_POINT_PREVIOUS_ROUND_WINDOW_POLICIES = {
    True: "previous_round_success_or_qualified_weak_confirmation_level_only",
    False: "disabled",
}
ESCALATION_BUDGET_POLICY = "require_remaining_time_for_independent_verification_and_measurement_with_rejected_prefix"
WEAK_POINT_BUDGET_POLICY = "one_extension_after_qualified_long_verify_before_base_deadline"
FAILED_POINT_WINDOW_POLICY = "next_level_after_long_verified_window_on_timeout"
WEAK_POINT_EVIDENCE_MIN_SECONDS = 1.0
TARGET_QUALITY_RULES = {
    "target_candidate_search": "expected_tone_plus_minus_verify_tolerance_hz",
    "target_presence_method": "exact_tone_with_independent_halves_on_background_dominance",
    "amplitude_estimator": "exact_expected_tone_hann_coefficient",
    "rejected_window_policy": "reuse_last_failed_window_only_on_escalation",
    "previous_tone_detection": "exact_tone_with_half_window_support_or_split_candidate",
    "target_numerical_floor": "float32_epsilon_times_input_rms",
}
LEGACY_SWEEP_VERSION = "legacy.truncated_window_fft.v1"
SWEEP_QUALITY_FIELDS = ("verify_min_seconds", "measure_min_seconds", "verify_consecutive_windows",
                        "min_tone_snr_db", "previous_tone_rejection_db")
ADAPTIVE_QUALITY_FIELDS = ("previous_tone_presence_db", "adaptive_rejections_per_level",
                           "adaptive_carryover_hold_seconds")

COLORS = ["#087f85", "#d28b32", "#7474bd", "#be6687", "#4c9271", "#527fa2", "#936d47", "#935d78"]


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def history_category(row, context=None):
    """旧 phase 与明确登记的手指研究保持不同类别；旧 CLI sweep 仍属材料扫频。"""
    context = context or {}
    key, mode = row.get("key", ""), row.get("mode", "")
    experiment = context.get("experiment", row.get("experiment"))
    if key.startswith("temperature_pressure_runs/") or mode == "serial":
        category = experiment if experiment in ("temperature_calibration", "temperature_pressure", "pressure_calibration") else row.get("category")
        return category if category in ("temperature_calibration", "temperature_pressure", "pressure_calibration") else "temperature_pressure"
    if experiment in ("liquid_temperature_sweep", "liquid_temperature_phase", "temperature_sweep"):
        return experiment
    if key.startswith("teacher_runs/") or mode == "teacher":
        return "teacher_sweep"
    if key.startswith("runs/") or mode == "sweep":
        return "material_sweep"
    if key.startswith("phase_runs/") or mode == "phase":
        return "finger_displacement" if context.get("experiment", row.get("experiment")) == "finger_displacement" else "phase_history"
    return "unknown"


def read_source(record, relative_path, required=True):
    """解析与来源 SHA 使用同份 bytes，读中变化立即拒绝；仅限 run 内普通文件。"""
    if relative_path in record["_contents"]:
        return record["_contents"][relative_path]
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("对比来源路径无效")
    path = record["_path"] / relative
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p.is_relative_to(record["_path"])):
        raise ValueError("对比来源不允许符号链接")
    if not path.is_file():
        if required:
            raise ValueError(f"记录 {record['key']} 缺少 {relative_path}")
        return None
    before = path.stat()
    content = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or len(content) != before.st_size:
        raise ValueError("来源文件正在改变，请等待采集与保存完成")
    record["files"].append({"relative_path": relative.as_posix(), "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})
    record["_contents"][relative_path] = content
    return content


def read_json_source(record, name, required=True):
    content = read_source(record, name, required)
    if content is None:
        return {}
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"来源 {name} 不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"来源 {name} 结构无效")
    return value


def chart(ident, title, xlabel, ylabel, series, note):
    return {"id": ident, "title": title, "kind": "line", "xlabel": xlabel,
            "ylabel": ylabel, "series": series, "note": note}


def series(record, data, errors=None):
    result = {"label": record["label"] + " · " + record["key"].split("/")[-1],
              "color": record["color"], "data": data}
    if errors is not None:
        result["errors"] = errors
    return result


def table(columns, rows):
    return {"columns": columns, "rows": rows}


def _number(value):
    return float(value) if finite(value) else None


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def _window_levels(value, *, maximum_seconds=1.0):
    """保留完整、有序窗口策略；缺失或无效策略不得补成当前默认值。"""
    if (not isinstance(value, (list, tuple)) or not value
            or not all(finite(v) for v in value)
            or value[0] < .020 or value[-1] > maximum_seconds
            or any(a >= b for a, b in zip(value, value[1:]))):
        return None
    return [float(v) for v in value]



def _target_quality_parameters(params, sweep, declared, time_policy=None):
    """Target-confirmed snapshots must agree; never fill from today's defaults."""
    validators = {
        "verify_min_seconds": lambda v: v >= .020,
        "measure_min_seconds": lambda v: v >= .040,
        "verify_consecutive_windows": lambda v: v >= 2 and v.is_integer(),
        "min_tone_snr_db": lambda v: v >= 6,
        "previous_tone_rejection_db": lambda v: v >= 6,
        "previous_tone_presence_db": lambda v: v >= 6,
        "adaptive_rejections_per_level": lambda v: v == 2,
        "adaptive_carryover_hold_seconds": lambda v: 0 <= v <= .5,
        "verify_tolerance_hz": lambda v: v > 0,
        "nfft": lambda v: v >= 2 and v.is_integer(),
    }
    for key, valid in validators.items():
        value = _number(sweep.get(key))
        mirrored = _number(declared.get("nfft_min" if key == "nfft" else key))
        params[key] = value if value is not None and valid(value) and mirrored == value else None
    levels = _window_levels(sweep.get("adaptive_window_seconds"), maximum_seconds=2.0)
    params["adaptive_window_seconds"] = levels if levels is not None and levels == _window_levels(
        declared.get("adaptive_window_seconds"), maximum_seconds=2.0) else None
    skip = sweep.get("skip_invalid_points")
    params["skip_invalid_points"] = skip if isinstance(skip, bool) and declared.get("skip_invalid_points") is skip else None
    for key, expected in TARGET_QUALITY_RULES.items():
        params[key] = expected if declared.get(key) == expected else None
    confirmation = _number(declared.get("target_confirmation_snr_db"))
    params["target_confirmation_snr_db"] = confirmation if confirmation is not None and confirmation >= 6 and (
        confirmation == params["min_tone_snr_db"]) else None
    version = declared.get("version")
    if version in (ROUND_AWARE_SWEEP_VERSION, WEAK_POINT_BUDGET_SWEEP_VERSION):
        reuse = sweep.get("reuse_previous_round_window")
        mirrored = declared.get("reuse_previous_round_window")
        reuse = reuse if type(reuse) is bool and type(mirrored) is bool and mirrored is reuse else None
        params["reuse_previous_round_window"] = reuse
        policies = (WEAK_POINT_PREVIOUS_ROUND_WINDOW_POLICIES if version == WEAK_POINT_BUDGET_SWEEP_VERSION
                    else PREVIOUS_ROUND_WINDOW_POLICIES)
        expected = policies[reuse] if reuse is not None else None
        params["previous_round_window_policy"] = expected if declared.get("previous_round_window_policy") == expected else None
        params["escalation_budget_policy"] = (ESCALATION_BUDGET_POLICY
            if declared.get("escalation_budget_policy") == ESCALATION_BUDGET_POLICY else None)
    if version == WEAK_POINT_BUDGET_SWEEP_VERSION:
        time_policy = time_policy if isinstance(time_policy, dict) else {}
        extra = _number(sweep.get("weak_point_extra_seconds"))
        mirrored = _number(declared.get("weak_point_extra_seconds"))
        params["weak_point_extra_seconds"] = (extra
            if extra is not None and 0 <= extra <= 30 and mirrored == extra
            and _number(time_policy.get("weak_point_extra_seconds")) == extra else None)
        base = _number(time_policy.get("point_timeout_seconds"))
        params["point_timeout_seconds"] = base if base is not None and 1 <= base <= 30 else None
        maximum = _number(time_policy.get("point_max_timeout_seconds"))
        params["point_max_timeout_seconds"] = (maximum
            if maximum is not None and 1 <= maximum <= 60
            and params["point_timeout_seconds"] is not None and params["weak_point_extra_seconds"] is not None
            and maximum == base + extra else None)
        params["weak_point_extension_policy"] = (WEAK_POINT_BUDGET_POLICY
            if time_policy.get("weak_point_extension_policy") == WEAK_POINT_BUDGET_POLICY else None)
        # None is the compatibility table's unknown sentinel; distinguish an explicit unlimited policy.
        params["total_time_limit_policy"] = ("unlimited"
            if "total_time_limit_seconds" in time_policy and time_policy["total_time_limit_seconds"] is None else None)
        evidence = _number(declared.get("weak_point_evidence_min_seconds"))
        params["weak_point_evidence_min_seconds"] = (evidence
            if evidence == WEAK_POINT_EVIDENCE_MIN_SECONDS else None)
        for key, expected in (("weak_point_budget_policy", WEAK_POINT_BUDGET_POLICY),
                              ("failed_point_window_policy", FAILED_POINT_WINDOW_POLICY)):
            params[key] = expected if declared.get(key) == expected else None


def _parameters(record):
    m, context = record["_manifest"], record["_context"]
    actual, hardware = m.get("actual_sdr") or {}, m.get("actual_hardware") or {}
    requested, sweep = m.get("requested_sdr") or {}, m.get("sweep") or {}
    params = {"rx_rate_sps": _number(actual.get("rx_rate_actual")),
              "tx_rate_sps": _number(actual.get("tx_rate_actual")),
              "tx_gain_dB": _number(hardware.get("tx_gain")), "rx_gain_dB": _number(hardware.get("rx_gain")),
              "tx_amplitude": _number(requested.get("amp", actual.get("amp"))),
              "channel": _number(actual.get("channel")),
              "tx_antenna": _text(hardware.get("tx_antenna")), "rx_antenna": _text(hardware.get("rx_antenna")),
              "master_clock_rate_hz": _number(hardware.get("master_clock_rate")),
              "sensor": _text(context.get("sensor"))}
    if record["kind"] in ("material_sweep", "liquid_temperature_sweep", "temperature_sweep"):
        declared = m.get("sweep_algorithm")
        # 旧记录未登记版本，仍按旧源码哈希互相比对；不能把缺失字段补成新版默认值。
        version = LEGACY_SWEEP_VERSION if "sweep_algorithm" not in m else (
            _text(declared.get("version")) if isinstance(declared, dict) else None)
        if version not in (LEGACY_SWEEP_VERSION, SWEEP_QUALITY_VERSION, ADAPTIVE_SWEEP_VERSION, *TARGET_CONFIRMED_SWEEP_VERSIONS):
            version = None
        params["sweep_algorithm_version"] = version
        if version in (SWEEP_QUALITY_VERSION, ADAPTIVE_SWEEP_VERSION, *TARGET_CONFIRMED_SWEEP_VERSIONS):
            for key in SWEEP_QUALITY_FIELDS:
                params[key] = _number(sweep.get(key))
        if version in (ADAPTIVE_SWEEP_VERSION, *TARGET_CONFIRMED_SWEEP_VERSIONS):
            for key in ADAPTIVE_QUALITY_FIELDS:
                params[key] = _number(sweep.get(key))
            params["adaptive_window_seconds"] = _window_levels(sweep.get("adaptive_window_seconds"))
        for key in ("nfft", "verify_cycles", "measure_cycles", "verify_tolerance_hz", "switch_settle_time"):
            params[key] = _number(sweep.get(key))
        if version in TARGET_CONFIRMED_SWEEP_VERSIONS:
            _target_quality_parameters(params, sweep, declared, m.get("time_policy"))
        names = {"logic.py", "sweep_logic.py", "do_sweep.py"}
        hashes = m.get("current_source_hashes") or m.get("source_hashes") or {}
    else:
        params.update(carrier_hz=_number(actual.get("freq")), tone_hz=_number(actual.get("tone")))
        names = {"dsp.py", "phase_dsp.py"}
        hashes = m.get("phase_source_hashes") or {}
    profile = m.get("device_profile") or {}
    record["_identity"] = _text(profile.get("serial"))
    record["_fpga"] = _text(profile.get("fpga_sha256"))
    selected = sorted(value for name, value in hashes.items() if Path(name).name in names and isinstance(value, str))
    record["_algorithm"] = tuple(selected) if selected else None
    return params


def _relation(values, reference_index):
    ref = values[reference_index]
    status = "unknown" if any(v is None for v in values) else ("same" if all(v == ref for v in values) else "different")
    safe = ["unknown" if v is None or ref is None else ("reference" if i == reference_index else ("same" if v == ref else "different")) for i, v in enumerate(values)]
    return status, safe


def compare_parameters(records, reference_index, report):
    labels = {"rx_rate_sps": "实际 RX 采样率 S/s", "tx_rate_sps": "实际 TX 采样率 S/s",
              "tx_gain_dB": "实际 TX 增益 dB", "rx_gain_dB": "实际 RX 增益 dB", "tx_amplitude": "数字 TX 幅值",
              "channel": "通道", "tx_antenna": "实际 TX 天线选项", "rx_antenna": "实际 RX 天线选项",
              "master_clock_rate_hz": "主时钟 Hz", "sensor": "人工传感器说明",
              "nfft": "FFT 配置点数（全窗版本为最低长度）", "verify_cycles": "验证单音周期数", "measure_cycles": "测量单音周期数",
              "verify_tolerance_hz": "峰频验证容差 Hz", "switch_settle_time": "切频等待 s",
              "carrier_hz": "载波设定 Hz", "tone_hz": "基带单音 Hz",
              "sweep_algorithm_version": "扫频估计器版本（旧截窗 / 固定全窗 / 自适应相干门槛 / 目标确认）",
              "verify_min_seconds": "验证最小窗口 s", "measure_min_seconds": "测量最小窗口 s",
              "verify_consecutive_windows": "连续有效验证窗口数", "min_tone_snr_db": "目标单音最低局部突出度 dB",
              "previous_tone_rejection_db": "前一单音最低抑制 dB",
              "previous_tone_presence_db": "前一单音存在判据 dB",
              "adaptive_window_seconds": "自适应窗口档位 s",
              "adaptive_rejections_per_level": "每档升级所需拒绝次数",
              "adaptive_carryover_hold_seconds": "已确认前音调残留的短窗保留时长 s",
              "skip_invalid_points": "单点质量超时保留缺测后继续",
              "target_candidate_search": "目标候选搜索规则",
              "target_presence_method": "目标单音与背景峰占优时的独立半窗确认规则",
              "amplitude_estimator": "幅值估计器",
              "rejected_window_policy": "升级窗长时的失败窗口复用规则",
              "previous_tone_detection": "前一单音的半窗支持或独立候选确认规则",
              "target_numerical_floor": "目标确认的数值噪声底规则",
              "target_confirmation_snr_db": "目标完整窗与半窗最低突出度 dB",
              "reuse_previous_round_window": "使用上一轮同频点窗长提示",
              "previous_round_window_policy": "跨轮窗长选择规则（不复用样本）",
              "escalation_budget_policy": "窗口升级前的独立验证与测量预算规则",
              "weak_point_extra_seconds": "合格弱频点单次额外时间上限 s",
              "weak_point_evidence_min_seconds": "触发弱频点策略的最短有效验证窗 s",
              "weak_point_budget_policy": "合格弱频点额外时间触发规则",
              "failed_point_window_policy": "超时弱频点的下一轮窗长提示规则",
              "point_timeout_seconds": "基础单点质量期限 s",
              "point_max_timeout_seconds": "含合格弱点扩展的单点时间上限 s",
              "weak_point_extension_policy": "采集器弱频点期限扩展规则",
              "total_time_limit_policy": "整场时间限制策略（unlimited 表示无总时限）"}
    rows = []
    for parameter in dict.fromkeys(key for r in records for key in r["parameters"]):
        values = [r["parameters"].get(parameter) for r in records]
        status = "unknown" if any(v is None for v in values) else ("same" if all(v == values[0] for v in values) else "different")
        blocking = parameter != "sensor"
        rows.append({"parameter": parameter, "label": labels[parameter], "values": values,
                     "status": status, "blocking": blocking, "note": "缺失值不是默认值；不自动校正仪器差异。" if blocking else "人工说明不能证明机械位置/样品条件相同。"})
    for private, parameter, label in (("_identity", "device_identity", "设备身份一致性"),
                                      ("_fpga", "fpga_identity", "FPGA 镜像一致性"),
                                      ("_algorithm", "estimator_identity", "估计器源码一致性")):
        status, safe = _relation([r[private] for r in records], reference_index)
        rows.append({"parameter": parameter, "label": label, "values": safe, "status": status,
                     "blocking": True, "note": "仅显示一致性结论，不导出设备身份值。"})
        for record, value in zip(records, safe):
            record["parameters"][parameter] = value
    report["compatibility"] = rows
    blocking = [r for r in rows if r["blocking"] and r["status"] != "same"]
    if blocking:
        report["warnings"].append("测量参数不同或未知：" + "、".join(r["label"] for r in blocking) + "。仅叠加查看，禁止定量参考差值。")
    versions = [r["parameters"].get("sweep_algorithm_version") for r in records]
    if LEGACY_SWEEP_VERSION in versions and any(v not in (None, LEGACY_SWEEP_VERSION) for v in versions):
        report["warnings"].append("新旧扫频估计器混合：旧记录截取固定 FFT 窗口，新记录使用完整测量窗口与质量门槛；仅叠加查看，不计算定量幅度差。")
    elif SWEEP_QUALITY_VERSION in versions and ADAPTIVE_SWEEP_VERSION in versions:
        report["warnings"].append("扫频质量策略版本混合：v2 使用固定完整窗口与前音调频带最大峰，v3 使用自适应窗口与相干存在判据；仅叠加查看，不计算定量幅度差。")
    if WEAK_POINT_BUDGET_SWEEP_VERSION in versions and any(v != WEAK_POINT_BUDGET_SWEEP_VERSION for v in versions):
        report["warnings"].append("扫频窗口策略版本混合或未知：v8 增加合格弱频点的一次额外时间与超时后窗长提示；虽然幅值计算与质量门槛不变，采样策略与 v5/v6/v7 不同，仅叠加查看，不计算跨版本定量幅度差。")
    elif ROUND_AWARE_SWEEP_VERSION in versions and any(v != ROUND_AWARE_SWEEP_VERSION for v in versions):
        report["warnings"].append("扫频窗口策略版本混合或未知：v7 增加上一轮同频点窗长选择和升级预算约束；虽然幅值计算与质量门槛不变，采样策略与 v5/v6 不同，仅叠加查看，不计算跨版本定量幅度差。")
    elif WINDOW_EFFICIENT_SWEEP_VERSION in versions and any(v != WINDOW_EFFICIENT_SWEEP_VERSION for v in versions):
        report["warnings"].append("扫频窗口策略版本混合或未知：v6 默认使用20／100／1000／2000ms四档窗口，v5使用七档；虽然幅值计算与质量门槛相同，实采窗口选择不同，仅叠加查看，不计算跨版本定量幅度差。")
    elif TARGET_CONFIRMED_SWEEP_VERSION in versions and any(v != TARGET_CONFIRMED_SWEEP_VERSION for v in versions):
        report["warnings"].append("扫频质量策略版本混合或未知：v5 使用目标范围内候选、精确单音及独立半窗确认与精确单音幅值；不能与旧版本计算定量幅度差。")
    if any(v in (ADAPTIVE_SWEEP_VERSION, *TARGET_CONFIRMED_SWEEP_VERSIONS) for v in versions):
        report["warnings"].append("自适应策略一致不代表逐点实采窗口时长一致；弱信号可能使用更长窗口，频率分辨率、噪声水平及通过门槛后的筛选效应可能不同。逐点表保留实采窗长与档位；幅度差仍是未校准的描述性比较。")
    if any(r["parameter"] == "sensor" and r["status"] != "same" for r in rows):
        report["warnings"].append("传感器/机械位置的一致性未得到完整记录；叠加及数字幅度差不能证明材料造成了变化。")
    return not blocking


def _started(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return math.inf


def _temperature_comparison_label(label, metadata):
    """Use the saved reference value in all report labels, without inferring °C."""
    parts = [label.strip()]
    condition = _text(metadata.get("condition_label"))
    if condition and condition.strip() != parts[0]:
        parts.append(condition.strip())
    mode = metadata.get("mode", "platform")
    if mode == "continuous":
        # A changing-temperature run has no single representative temperature.
        parts.append("连续温度记录")
    else:
        reference = metadata.get("reference_temperature_c")
        if finite(reference):
            source = "手填参考" if mode == "manual" else "参考"
            parts.append(f"{source} {reference:.12g} °C")
        else:
            parts.append("参考温度未记录")
    return " · ".join(parts)


def build_comparison(root, keys, reference_key=None):
    """1–8 条同类型可信记录；单条只分析自身数据，不建立跨记录参考。"""
    root = Path(root).resolve()
    if not isinstance(keys, (list, tuple)) or not 1 <= len(keys) <= 8 or any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys):
        raise ValueError("请选择 1–8 条不同实验记录")
    if reference_key is not None and reference_key not in keys:
        raise ValueError("参考记录必须属于本次选择")
    records = []
    for index, key in enumerate(keys):
        relative = Path(key)
        if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] not in ("runs", "phase_runs", "teacher_runs", "temperature_pressure_runs") or ".." in relative.parts:
            raise ValueError("实验记录路径无效")
        run = root / "Data" / relative
        if not run.is_dir() or run.is_symlink() or any(p.is_symlink() for p in run.parents if p.is_relative_to(root)):
            raise ValueError("实验记录不存在或包含符号链接")
        record = {"key": key, "files": [], "_path": run, "_data_root": root / "Data", "_contents": {}, "color": COLORS[index]}
        manifest = read_json_source(record, "feature_report.json" if key.startswith("teacher_runs/") else "manifest.json")
        context = read_json_source(record, "desktop_context.json", False) or read_json_source(record, "web_context.json", False)
        kind = history_category({"key": key}, context)
        record.update(_manifest=manifest, _context=context, kind=kind,
                      label=str(manifest.get("label") or manifest.get("initial_label") or manifest.get("material_input") or relative.name),
                      started_utc=str(manifest.get("started_utc", "")))
        if kind == "temperature_sweep":
            metadata = (context.get("parameters") or {}).get("temperature_sweep") or {}
            record["label"] = _temperature_comparison_label(record["label"], metadata)
        if kind == "teacher_sweep":
            raise ValueError("老师历史扫频缺少统一的 measurements.csv 对比契约；请逐条查看或导出，不能混入新扫频定量对比")
        if key.startswith("temperature_pressure_runs/"):
            from .temperature_pressure.comparison import prepare_serial_record
            prepare_serial_record(record)
            if context.get("desktop_status", context.get("web_status")) in ("failed", "stop_unconfirmed", "initialization_failed"):
                raise ValueError(f"记录 {key} 的桌面状态未确认完成")
            records.append(record)
            continue
        telemetry = manifest.get("telemetry") or {}
        if (manifest.get("status") not in ("complete", "stopped") or manifest.get("errors") != [] or
                telemetry.get("tx_end_of_burst_sent") is not True or telemetry.get("workers_stopped") is not True or
                not (telemetry.get("rx_stop_sent") is True or telemetry.get("rx_stop_command_sent") is True) or
                context.get("desktop_status", context.get("web_status")) in ("failed", "stop_unconfirmed", "initialization_failed")):
            raise ValueError(f"记录 {key} 未完成、存在错误或缺少完整收发停止证据，不能参与对比")
        if kind == "temperature_sweep" and (context.get("parameters",{}).get("temperature_sweep") or {}).get("mode")!="manual":
            thermal = manifest.get("temperature_sweep") or {}
            if thermal.get("status") != "complete" or not all(thermal.get(name) is True for name in
                    ("serial_workers_stopped", "serial_closed", "data_files_closed")):
                raise ValueError(f"记录 {key} 缺少温度采集完整保存与关闭证据，不能参与联测对比")
        record["parameters"] = _parameters(record)
        records.append(record)
    if len({r["kind"] for r in records}) != 1:
        raise ValueError("请选择同一种实验类型；历史幅相与手指位移研究需分别对比")
    if records[0]['kind']=='temperature_sweep':
        modes={(r['_context'].get('parameters',{}).get('temperature_sweep') or {}).get('mode','platform') for r in records}
        if 'continuous' in modes:
            raise ValueError('连续变温请查看各轮电压／温度与频点敏感性报告；不将不同状态的轮次平均后跨记录相减')
        if len(modes)>1:raise ValueError('手填平台与历史联测模式请分别对比')
    if reference_key is None:
        reference_key = min(records, key=lambda r: _started(r["started_utc"]))["key"]
    reference_index = list(keys).index(reference_key)
    single = len(records) == 1
    report = {"schema_version": 1, "status": "ready", "kind": records[0]["kind"], "keys": list(keys),
              "analysis_mode": "single" if single else "combined",
              "reference_key": None if single else reference_key, "sources": [], "compatibility": [], "warnings": [],
              "quantitative_difference_allowed": False, "charts": [], "metrics": [], "tables": {}}
    serial_kind = records[0]["kind"] in ("temperature_pressure", "temperature_calibration", "pressure_calibration")
    compatible = False if serial_kind or single else compare_parameters(records, reference_index, report)
    if serial_kind:
        from .temperature_pressure.comparison import compare_serial
        compare_serial(records, report)
    elif records[0]["kind"] in ("material_sweep", "liquid_temperature_sweep", "temperature_sweep"):
        from .material_sweep.comparison import compare_sweeps
        compare_sweeps(records, reference_index, report, compatible)
    elif records[0]["kind"] == "liquid_temperature_phase":
        raise ValueError("液体温度相位实验请在液体温度影响页按同一次记录的稳定区间分析，不能跨记录相减相位")
    else:
        from .finger_displacement.comparison import compare_phase
        compare_phase(records, reference_index, report)
    if records[0]["kind"] == "temperature_sweep":
        report["warnings"].append("温度联测按主机接收时间关联；未标定电压不能称为温度，数字幅度差不等于温度灵敏度或独立温度因果。")
        conditions = []
        for record in records:
            metadata = record["_context"].get("parameters", {}).get("temperature_sweep", {})
            conditions.append({"key": record["key"], "condition_label": metadata.get("condition_label"),
                               "channel": metadata.get("channel"),
                               "reference_temperature_c": metadata.get("reference_temperature_c"),
                               "calibration": metadata.get("calibration")})
            # 连同温度实收序列与逐点关联结果绑定来源，比较导出保留两路证据。
            for name in (() if metadata.get("mode")=="manual" else ("temperature_measurements.csv", "temperature/samples.csv", "temperature/manifest.json")):
                read_source(record, name, required=True)
        report["tables"]["temperature_conditions"] = conditions
    # A unified sweep can combine a liquid setup with either temperature source.
    # Preserve both independent condition records in reports; the initial liquid
    # thermometer value is not a substitute for the continuous sensor series.
    liquid_conditions = [
        {"key": record["key"], **record["_context"]["parameters"]["liquid_temperature"]}
        for record in records
        if isinstance((record["_context"].get("parameters") or {}).get("liquid_temperature"), dict)
    ]
    if liquid_conditions:
        report["tables"]["liquid_temperature_conditions"] = liquid_conditions
    if single:
        report["compatibility"] = []
        report["quantitative_difference_allowed"] = False
        report["warnings"].append("单记录分析展示本次保存曲线与统计；不设置跨记录参考，不计算自身差值。")
    report["sources"] = [{k: r[k] for k in ("key", "label", "started_utc", "parameters", "files", "correction") if k in r} for r in records]
    json.dumps(report, allow_nan=False)
    return report
