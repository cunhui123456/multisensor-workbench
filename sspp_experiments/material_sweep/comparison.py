"""重复扫频均值/样本 SD 与同设置、共同实际频点的描述性差值。"""
import csv
import io
import math
from collections import defaultdict
from statistics import mean, stdev

from ..comparison import (TARGET_CONFIRMED_SWEEP_VERSIONS, ROUND_AWARE_SWEEP_VERSION,
                          WEAK_POINT_BUDGET_SWEEP_VERSION,
                          chart, finite, read_source, series, table)


def _optional_window_value(row, name, *, integer=False, minimum=0.):
    value = row.get(name)
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("扫频 CSV 的实采窗口元数据无效") from exc
    if not math.isfinite(number) or number < minimum or (integer and not number.is_integer()):
        raise ValueError("扫频 CSV 的实采窗口元数据无效")
    return int(number) if integer else number


def _load(record):
    manifest = record["_manifest"]
    quality, cfg = manifest.get("quality") or {}, manifest.get("sweep") or {}
    if quality.get("complete") is not True or quality.get("measurement_tone_failures", 0):
        raise ValueError(f"扫频 {record['key']} 缺少完整点数或峰频质量验收")
    try:
        rows = list(csv.DictReader(io.StringIO(read_source(record, "measurements.csv").decode("utf-8-sig"))))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError("扫频 CSV 无法解析") from exc
    if not rows:
        raise ValueError("扫频 CSV 没有测量数据")
    parsed, seen = [], set()
    for row in rows:
        declared = manifest.get("sweep_algorithm")
        if (isinstance(declared, dict) and declared.get("version") in TARGET_CONFIRMED_SWEEP_VERSIONS
                and row.get("algorithm_version") != declared["version"]):
            raise ValueError(f"{declared['version']} 扫频 CSV 的 algorithm_version 与 manifest 不一致或缺失")
        try:
            repeat, point = int(row["repeat"]), int(row["point"])
            carrier, tone = float(row["requested_carrier_GHz"])*1e9, float(row["expected_tone_Hz"])
            measured, amplitude = float(row["measured_tone_Hz"]), float(row["peak_amplitude_dBFS_est"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("扫频 CSV 字段缺失或数值无效") from exc
        if repeat < 1 or point < 1 or not all(math.isfinite(v) for v in (carrier, tone, measured, amplitude)) or (repeat, point) in seen:
            raise ValueError("扫频 CSV 存在非有限数值、重复点或无效轮次")
        tolerance = cfg.get("verify_tolerance_hz")
        if finite(tolerance) and abs(measured-tone) >= tolerance:
            raise ValueError("扫频正式测量峰频不满足原记录的验证容差")
        seen.add((repeat, point))
        parsed.append({"repeat": repeat, "point": point, "requested_carrier_hz": carrier,
                       "expected_tone_hz": tone, "measured_tone_hz": measured, "amplitude_dB": amplitude,
                       "accepted_window_count": _optional_window_value(row, "accepted_window_count", integer=True, minimum=3),
                       "accepted_window_duration_s": _optional_window_value(row, "accepted_window_duration_s", minimum=1e-12),
                       "adaptive_level_index": _optional_window_value(row, "adaptive_level_index", integer=True)})
    repeats = sorted({r["repeat"] for r in parsed})
    points = sorted({r["point"] for r in parsed})
    if repeats != list(range(1, len(repeats)+1)) or points != list(range(1, len(points)+1)) or len(parsed) != len(repeats)*len(points):
        raise ValueError("扫频 CSV 的轮次/频点网格不完整")
    if cfg.get("num_points", len(points)) != len(points) or cfg.get("sweep_repeat", len(repeats)) != len(repeats):
        raise ValueError("扫频 CSV 点数与保存参数不符")
    if quality.get("counts", [len(points)]*len(repeats)) != [len(points)]*len(repeats):
        raise ValueError("扫频 CSV 与原质量计数不符")
    actual_points = {}
    for item in manifest.get("raw_files", []):
        if not isinstance(item, dict):
            continue
        number = item.get("point_number")
        if isinstance(number, int):
            if number in actual_points:
                raise ValueError("实际载波回读索引重复")
            actual_points[number] = item
    groups, seen_actual = defaultdict(list), set()
    for row in parsed:
        raw = actual_points.get((row["repeat"]-1)*len(points)+row["point"], {})
        tx, rx = raw.get("tx_actual_carrier_hz"), raw.get("rx_actual_carrier_hz")
        actual = finite(tx) and finite(rx)
        if actual:
            if (not finite(raw.get("requested_carrier_hz")) or
                    abs(raw["requested_carrier_hz"]-row["requested_carrier_hz"]) > .011 or
                    raw.get("tone_hz") != row["expected_tone_hz"]):
                raise ValueError("扫频 CSV 与实际频点回读元数据不一致")
            identity = (float(tx), float(rx), row["expected_tone_hz"])
        else:
            identity = (row["requested_carrier_hz"], None, row["expected_tone_hz"])
        if (row["repeat"], identity) in seen_actual:
            raise ValueError("同轮存在重复的实际频点，不能把它当作独立重复轮次")
        seen_actual.add((row["repeat"], identity))
        row.update(actual_tx_carrier_hz=float(tx) if actual else None,
                   actual_rx_carrier_hz=float(rx) if actual else None,
                   target_rf_hz=(float(tx) if actual else row["requested_carrier_hz"])+row["expected_tone_hz"],
                   frequency_basis="actual_carrier_plus_expected_tone" if actual else "requested_carrier_plus_expected_tone")
        groups[identity].append(row)
    aggregated = {}
    for identity, group in groups.items():
        values = [r["amplitude_dB"] for r in group]
        first = group[0]
        aggregated[identity] = {"key": record["key"], "label": record["label"],
            **{field: first[field] for field in ("requested_carrier_hz", "actual_tx_carrier_hz", "actual_rx_carrier_hz", "expected_tone_hz", "target_rf_hz", "frequency_basis")},
            "count": len(values), "mean_amplitude_dB": float(mean(values)),
            "repeat_sd_dB": float(stdev(values)) if len(values) > 1 else None,
            "max_tone_error_hz": max(abs(r["measured_tone_hz"]-r["expected_tone_hz"]) for r in group)}
    return aggregated, parsed


def compare_sweeps(records, reference_index, report, compatible):
    single = len(records) == 1
    data, raw = [], []
    for record in records:
        values, measurements = _load(record)
        data.append(values)
        raw.extend({"key": record["key"], "label": record["label"], **row} for row in measurements)
    actual_known = [all(v["actual_tx_carrier_hz"] is not None for v in values.values()) for values in data]
    report["compatibility"].append({"parameter": "actual_frequency_evidence", "label": "逐点实际 TX/RX 载波回读",
        "values": ["known" if known else "unknown" for known in actual_known],
        "status": "same" if all(actual_known) else "unknown", "blocking": True,
        "note": "共同频点使用实际 TX/RX 载波与设定 tone 的完整组合，绝不按点号或插值配对。"})
    if not all(actual_known):
        report["warnings"].append("本记录缺少逐点实际载波回读，横轴使用请求载波+tone。" if single else
                                  "部分记录缺少逐点实际载波回读，叠加使用请求载波+tone；禁止定量参考差值。")
    common = set(data[reference_index])
    for values in data:
        common.intersection_update(values)
    same_grid = all(set(values) == set(data[0]) for values in data)
    report["compatibility"].append({"parameter": "frequency_grid", "label": "载波与单音组合网格",
        "values": [len(v) for v in data], "status": "same" if same_grid else "different", "blocking": False,
        "note": f"所有记录共有 {len(common)} 个实际组合频点；不同网格只比较交集，不插值。"})
    if not same_grid:
        report["warnings"].append(f"频点网格不完全相同；差值只使用全部所选记录共有的 {len(common)} 点，其余仅叠加。")
    if not common:
        report["warnings"].append("没有共同载波+tone 频点，不能生成参考差值。")
    report["quantitative_difference_allowed"] = bool(not single and compatible and all(actual_known) and common)
    mean_series, sd_series, summary_rows = [], [], []
    for record, values in zip(records, data):
        ordered = sorted(values.values(), key=lambda r: r["target_rf_hz"])
        summary_rows.extend(ordered)
        mean_series.append(series(record, [[r["target_rf_hz"]/1e9, r["mean_amplitude_dB"]] for r in ordered], [r["repeat_sd_dB"] for r in ordered]))
        sd_series.append(series(record, [[r["target_rf_hz"]/1e9, r["repeat_sd_dB"]] for r in ordered]))
    note = "纵轴为数字单音幅度（估计方法由各记录算法版本定义）；dB 域均值与 ddof=1 样本 SD。误差棒不是置信区间，连续扫频轮次不是独立取放。横轴为载波回读+设定tone（回读缺失时明确降为请求值）。"
    if any(record["parameters"].get("sweep_algorithm_version") == ROUND_AWARE_SWEEP_VERSION for record in records):
        note += "v7 的跨轮策略仅选择窗长，已通过的验证和正式测量仍使用独立新样本；策略是否启用及预算规则见参数核对。"
    if any(record["parameters"].get("sweep_algorithm_version") == WEAK_POINT_BUDGET_SWEEP_VERSION for record in records):
        note += "v8 可在基础期限内通过合格长窗验证后给予一次额外时间，并为超时弱点保留下一轮窗长提示；已通过的验证和正式测量仍使用独立新样本。额外时间上限与提示规则见参数核对，不同触发次数不等于相同实采时长。"
    report["charts"] = [chart("sweep_mean", "各实验扫频均值 ± 1 样本 SD", "载波 + tone（GHz）", "单音幅度（dB 数字估计）", mean_series, note),
                         chart("sweep_repeat_sd", "各实验的轮间重复 SD", "载波 + tone（GHz）", "样本 SD（dB）", sd_series, "单轮频点 SD 不可估计，保持空缺；不可解释为跨次取放重复性。")]
    differences, difference_series = [], []
    reference = data[reference_index]
    for index, (record, values) in enumerate(zip(records, data)):
        local = []
        if report["quantitative_difference_allowed"] and index != reference_index:
            for identity in sorted(common):
                target, ref = values[identity], reference[identity]
                combined = math.hypot(target["repeat_sd_dB"], ref["repeat_sd_dB"]) if target["repeat_sd_dB"] is not None and ref["repeat_sd_dB"] is not None else None
                local.append({"key": record["key"], "label": record["label"], "reference_key": records[reference_index]["key"],
                              "target_rf_hz": target["target_rf_hz"], "actual_tx_carrier_hz": identity[0],
                              "actual_rx_carrier_hz": identity[1], "expected_tone_hz": identity[2],
                              "target_count": target["count"], "reference_count": ref["count"],
                              "target_mean_dB": target["mean_amplitude_dB"], "reference_mean_dB": ref["mean_amplitude_dB"],
                              "difference_dB": target["mean_amplitude_dB"]-ref["mean_amplitude_dB"],
                              "combined_repeat_sd_dB": combined})
            differences.extend(local)
            difference_series.append(series(record, [[r["target_rf_hz"]/1e9, r["difference_dB"]] for r in local], [r["combined_repeat_sd_dB"] for r in local]))
        sds = [r["repeat_sd_dB"] for r in values.values() if r["repeat_sd_dB"] is not None]
        report["metrics"].append({"key": record["key"], "label": record["label"], "frequency_points": len(values),
            "repeat_count_min": min(r["count"] for r in values.values()), "repeat_count_max": max(r["count"] for r in values.values()),
            "max_repeat_sd_dB": max(sds) if sds else None, "common_points_vs_reference": len(common),
            "mean_difference_dB": float(mean(r["difference_dB"] for r in local)) if local else None,
            "max_abs_difference_dB": max(abs(r["difference_dB"]) for r in local) if local else None})
    if difference_series:
        plot = chart("sweep_difference", "相对参考记录的均值差：目标 − 参考", "共同实际载波 + tone（GHz）", "数字幅度差（dB）", difference_series,
                     "仅在相同已知测量设置的共同实际频点相减，无插值/增益补偿。误差棒为两组重复SD平方和开根，仅描述波动，不是均值差置信区间；不能归因为材料。")
        plot["horizontal"] = [{"y": 0., "label": "与参考相同"}]
        report["charts"].append(plot)
    report["warnings"].append("幅度与差值不是校准 S21/dBm；不同会话可能存在姿态、温漂和环境变化。")
    report["tables"] = {
        "sweep_summary": table(["key", "label", "requested_carrier_hz", "actual_tx_carrier_hz", "actual_rx_carrier_hz", "expected_tone_hz", "target_rf_hz", "frequency_basis", "count", "mean_amplitude_dB", "repeat_sd_dB", "max_tone_error_hz"], summary_rows),
        "sweep_measurements": table(["key", "label", "repeat", "point", "requested_carrier_hz", "actual_tx_carrier_hz", "actual_rx_carrier_hz", "expected_tone_hz", "measured_tone_hz", "target_rf_hz", "frequency_basis", "amplitude_dB", "accepted_window_count", "accepted_window_duration_s", "adaptive_level_index"], raw),
        "sweep_difference": table(["key", "label", "reference_key", "target_rf_hz", "actual_tx_carrier_hz", "actual_rx_carrier_hz", "expected_tone_hz", "target_count", "reference_count", "target_mean_dB", "reference_mean_dB", "difference_dB", "combined_repeat_sd_dB"], differences)}
    if single:
        report['charts'][0]['title'] = '本记录扫频均值 ± 1 样本 SD'
        report['charts'][1]['title'] = '本记录轮间重复 SD'
        report['tables'].pop('sweep_difference')
        for metric in report['metrics']:
            for name in ('common_points_vs_reference', 'mean_difference_dB', 'max_abs_difference_dB'):
                metric.pop(name, None)
