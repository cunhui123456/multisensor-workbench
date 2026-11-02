"""Replay the actual SweepRunner state machine using immutable saved RF frames.

Only hardware/queue/time boundaries are replaced. Every point begins at IQ
offset zero, no synthetic padding or skipped settling prefix. A single-point
quality-call adapter supplies the preceding point's saved tone, because run()
normally obtains that value by visiting earlier points in the same acquisition.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import redirect_stdout
from dataclasses import asdict
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
from queue import Queue
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np


RUNS = ["20261031T092500_814628Z", "20261031T092548_577113Z", "20261031T092647_920529Z", "20261031T092247_194698Z"]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self, frame_ends, rate, deadline):
        self.now, self.index = 0., 0
        self.arrivals = frame_ends / rate
        self.deadline = deadline

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        # Called by the real state machine only while waiting for more IQ.
        next_arrival = self.arrivals[self.index] if self.index < len(self.arrivals) else self.deadline + .001
        self.now = min(max(self.now + seconds, next_arrival), self.deadline + .001)


def replay(module, iq, frame_starts, frame_lengths, rate, tone, previous_tone, frequency, *, deadline=8.):
    if len(iq) != sum(frame_lengths):
        raise ValueError("Saved frame lengths do not cover IQ exactly")
    if not np.all(frame_starts[1:] == frame_starts[:-1] + frame_lengths[:-1]):
        raise ValueError("Saved software frame indices are discontinuous; cannot replay as contiguous IQ")
    frame_ends = np.cumsum(frame_lengths)
    clock = Clock(frame_ends, rate, deadline)
    records, received = [], []
    quality_times = []
    original_time, original_quality = module.time, module.quality_measurement

    def quality_adapter(data, actual_rate, actual_tone, _local_previous_tone, config):
        began = time.perf_counter()
        result = original_quality(data, actual_rate, actual_tone, previous_tone, config)
        quality_times.append(time.perf_counter() - began)
        return result

    class ReplayRunner(module.SweepRunner):
        def _set_freq_and_tone(self, frequency, new_tone):
            self.args.tone = new_tone

        def _clear_queue(self):
            pass

        def _fetch_available_frames(self):
            if clock.monotonic() > deadline:
                raise TimeoutError("8 second sample-time deadline")
            frames = []
            while clock.index < len(frame_ends) and clock.arrivals[clock.index] <= clock.monotonic() + 1e-12:
                i = clock.index
                left = int(frame_ends[i - 1]) if i else 0
                right = int(frame_ends[i])
                frames.append(iq[left:right])
                received.append({"frame_index": i, "software_sample_start": int(frame_starts[i]),
                                 "count": int(frame_lengths[i]), "point_iq_start": left})
                clock.index += 1
            return frames, frames[-1] if frames else None

        def on_quality_attempt(self, record):
            records.append(dict(record))

        def point_deadline_expired(self):
            return clock.monotonic() >= deadline

    config = module.SweepConfig(num_points=1, sweep_repeat=1, tone_start=tone, tone_step=0,
                                freq_start=frequency, freq_stop=frequency, switch_settle_time=0.)
    args = SimpleNamespace(rate=rate, rx_rate_actual=rate, tone=tone)
    runner = ReplayRunner(args, config, None, Queue(), threading.Event())
    outcome = "accepted"
    began = time.perf_counter()
    try:
        module.time, module.quality_measurement = clock, quality_adapter
        with redirect_stdout(io.StringIO()):
            try:
                result = runner.run(enable_plot=False)
            except TimeoutError:
                outcome = "timeout" if len(iq) / rate >= deadline else "insufficient_recorded_iq"
                result = runner.partial_result
    finally:
        module.time, module.quality_measurement = original_time, original_quality
    runtime = time.perf_counter() - began
    accepted = [r for r in records if r["accepted"]]
    completion_bound = None
    if outcome == "insufficient_recorded_iq" and records:
        # Establish a lower bound without inventing the unrecorded tail. Even
        # perfect future windows cannot finish if the mandatory window lengths
        # exceed all samples available before the unchanged point deadline.
        last = records[-1]
        verified = (last["verified_windows_before"] + 1
                    if last["state"] == "VERIFY" and last["passed"] else 0)
        verify, measure = runner._calc_samples(tone)
        levels = getattr(config, "adaptive_window_seconds", (0.,))
        level = levels[last.get("next_adaptive_level_index", 0)]
        verify, measure = max(verify, math.ceil(level * rate)), max(measure, math.ceil(level * rate))
        minimum_needed = max(0, config.verify_consecutive_windows - verified) * verify + measure
        consumed = last["window_start"] + last["window_count"]
        maximum_remaining = max(0, math.floor(deadline * rate) - consumed)
        completion_bound = {"last_consumed_sample": consumed,
                            "verified_windows_after_last_attempt": verified,
                            "minimum_future_samples_for_acceptance": minimum_needed,
                            "maximum_remaining_samples_before_deadline": maximum_remaining,
                            "unrecorded_duration_before_deadline_s": max(0., deadline-len(iq)/rate),
                            "completion_impossible_before_deadline": minimum_needed > maximum_remaining}
        if completion_bound["completion_impossible_before_deadline"]:
            outcome = "timeout_proven_by_sample_bound"
    if outcome == "accepted" and len(accepted) != 1:
        raise AssertionError("Replay returned without exactly one accepted measurement")
    if any(a["window_start"] + a["window_count"] != b["window_start"] for a, b in zip(records, records[1:])):
        raise AssertionError("Processed windows skipped or overlap recorded samples")
    if records and records[0]["window_start"] != 0:
        raise AssertionError("Replay skipped the recorded initial IQ")
    if accepted:
        final = accepted[0]
        before = records[-3:-1]
        if len(before) != 2 or any(r["state"] != "VERIFY" or not r["passed"] for r in before):
            raise AssertionError("Acceptance lacks two consecutive verified windows")
        if before[0]["window_count"] != before[1]["window_count"]:
            raise AssertionError("Two verification windows use different levels")
        if abs(final["measured_tone_hz"] - tone) >= 200 or clock.now > deadline:
            raise AssertionError("Frequency or deadline gate was bypassed")
    return {"algorithm_version": module.SWEEP_ALGORITHM_VERSION, "outcome": outcome,
            "recorded_samples": len(iq), "recorded_duration_s": len(iq) / rate,
            "sample_time_s": clock.now, "actual_replay_cpu_wall_seconds": runtime,
            "completion_bound": completion_bound,
            "quality_call_median_ms": float(np.median(quality_times) * 1000) if quality_times else None,
            "quality_call_max_ms": max(quality_times, default=0.) * 1000,
            "previous_tone_hz": previous_tone, "frames_received": len(received),
            "raw_software_index_origin": int(frame_starts[0]),
            "window_count": len(records), "rejections": dict(Counter(reason for r in records for reason in r["rejection_reasons"])),
            "accepted_record": accepted[0] if accepted else None,
            "last_three_windows": records[-3:], "attempts": records,
            "config": asdict(config)}


def inspect_file(module, source, previous_tone):
    with np.load(source, allow_pickle=False) as data:
        info = json.loads(str(data["point_info"]))
        result = replay(module, data["iq"], data["frame_start_indices"], data["frame_lengths"],
                        float(data["rx_rate"]), info["tone_hz"], previous_tone, info["requested_carrier_hz"])
    return {"run_id": source.parent.name, "file": source.name, "source_sha256": sha(source),
            "point_number": info["point_number"], **result}


def compare_original_quality(result, source):
    logs = [json.loads(row) for row in (source.parent / "sweep_quality.jsonl").read_text().splitlines()]
    logs = [row for row in logs if row.get("raw_iq_file") == source.name]
    attempts = result["attempts"]
    discrete = ("state", "passed", "accepted", "window_start", "window_count",
                "quality_attempt_index", "verified_windows_before", "rejection_reasons")
    numerical = ("measured_tone_hz", "target_amplitude_dB", "local_floor_dB",
                 "target_snr_db", "previous_tone_margin_db")
    differences = {key: max((abs(a[key]-b[key]) for a,b in zip(attempts, logs)
                            if a.get(key) is not None and b.get(key) is not None), default=0.)
                   for key in numerical}
    result["original_quality_log_comparison"] = {
        "log_sha256": sha(source.parent / "sweep_quality.jsonl"),
        "log_attempts": len(logs), "replay_attempts": len(attempts),
        "discrete_fields_identical": len(logs) == len(attempts) and
            all(a[key] == b[key] for a,b in zip(attempts,logs) for key in discrete),
        "maximum_absolute_numerical_differences": differences}
    if not result["original_quality_log_comparison"]["discrete_fields_identical"]:
        raise AssertionError("Legacy replay did not reproduce original window decisions")


def synthetic_controls(module):
    rate, count = 250000., 2015232
    t = np.arange(count) / rate
    random = np.random.default_rng(20261031)
    noise = (random.normal(size=count) + 1j * random.normal(size=count)).astype(np.complex64)
    tone, previous = 21000., 20000.
    target = np.exp(2j * np.pi * tone * t).astype(np.complex64)
    prior = np.exp(2j * np.pi * previous * t).astype(np.complex64)
    wrong = np.exp(2j * np.pi * 19000. * t).astype(np.complex64)
    cases = {"strong_target": (.1 * target + .001 * noise, True),
             "noise_only": (.001 * noise, False), "wrong_tone": (.1 * wrong + .001 * noise, False),
             "strong_previous": (.01 * target + .1 * prior + .001 * noise, False)}
    frames = np.full(count // 8192, 8192, dtype=np.int64)
    starts = np.arange(len(frames)) * 8192
    results = {}
    for name, (data, should_accept) in cases.items():
        result = replay(module, data, starts, frames, rate, tone, previous, 5.3e9)
        result["expected_acceptance"] = should_accept
        result["matches_expectation"] = (result["outcome"] == "accepted") == should_accept
        results[name] = result
    return results


def render_markdown(report):
    results = report["v3_results"]
    lines = ["# SSPP 自适应质量门槛：原始 IQ 实际状态机回放", "",
             "本报告为离线候选验证；没有连接设备、补造 IQ 或修改原始运行状态。",
             "", f"- v3 源码 SHA-256：`{report['v3_source_sha256']}`",
             f"- 回放工具 SHA-256：`{report['replay_tool_sha256']}`"]
    if report["v2_source_sha256"]:
        lines.append(f"- 冻结 v2 源码 SHA-256：`{report['v2_source_sha256']}`")
    lines += ["", "## 方法与边界", "",
              "执行候选的真实 `SweepRunner.run`，只替换硬件切频、队列帧交付和时钟边界。"
              "单点重放向质量函数传入前一保存点的实际单音；没有重新实现状态转换。",
              "", "每点从保存 IQ 下标 0 开始，按原 NPZ 的 `frame_lengths` 逐帧交付，"
              "检查 `frame_start_indices` 连续。帧可用时间为累计样点数 / 250000 Hz；"
              "8 秒截止时间不随拒绝或升级重置。所有实际消费窗连续且互不重叠。"
              "接受前必须有同档连续两窗 VERIFY，再使用另一段独立 MEASURE；频差仍严格小于 200 Hz。",
              "", "这是理想采样时间回放，时钟不计 RF 调谐、USB、写盘、温压采集或 CPU耗时。"
              "CPU 墙钟单独计量；不能据此保证真机全程用时或 GUI 延迟。旧记录长度不足时明确标为不足，"
              "不能推断为候选成功或候选超时。原增益、发射幅度和历史人工温度标注均未改写。",
              "", "## 三个失败点", "",
              "| 运行 / 保存点 | v3 采样时间 | VERIFY 1 / VERIFY 2 / MEASURE 的 IQ 半开区间 | MEASURE 频率 / 突出度 |",
              "|---|---:|---|---:|"]
    for result in results:
        if not result.get("critical_failed_point"):
            continue
        accepted = result["accepted_record"]
        windows = result.get("verification_windows", []) + ([accepted] if accepted else [])
        coordinates = " / ".join(f"[{r['window_start']}, {r['window_start']+r['window_count']})" for r in windows)
        value = f"{accepted['measured_tone_hz']:.0f} Hz / {accepted['target_snr_db']:.3f} dB" if accepted else result['outcome']
        lines.append(f"| {result['run_id']} / {result['point_number']} | {result['sample_time_s']:.6f} s | {coordinates} | {value} |")
    lines += ["", "突出度是期望单音的 Hann 相干系数相对局部 FFT 中位幅度的工程指标，"
              "不等同于经过标定的物理信噪比。表中区间不是人工挑选的候选窗；JSON保留这三点从起点开始的全部尝试。"]
    if report["v2_failed_point_comparison"]:
        lines += ["", "冻结 v2 回放与原 `sweep_quality.jsonl` 的状态、窗口、判决、拒绝原因完全一致。"
                  "三个原始文件均有 1,998,848 样点（7.995392 秒），缺少截止前最后 4.608 ms。"
                  "最后已处理到 1,995,000 样点，8 秒前最多还可提供 5,000 样点；"
                  "按真实状态至少还需如下样点，因此即使未记录的尾部全部完美也无法接受：", "",
                  "| 运行 | v2 尝试数 | 最少还需样点 | 最多可用样点 | 原日志数值最大差 |", "|---|---:|---:|---:|---:|"]
        for result in report["v2_failed_point_comparison"]:
            bound = result["completion_bound"]
            difference = max(result["original_quality_log_comparison"]["maximum_absolute_numerical_differences"].values())
            lines.append(f"| {result['run_id']} | {result['window_count']} | {bound['minimum_future_samples_for_acceptance']} | {bound['maximum_remaining_samples_before_deadline']} | {difference:.3g} |")
    lines += ["", "## 全部保存点覆盖", "", "| 运行 | 接受 | 记录不足 | 已接受点采样时长合计 / 最大值 |", "|---|---:|---:|---:|"]
    for run_id in RUNS:
        group = [r for r in results if r["run_id"] == run_id]
        accepted = [r for r in group if r["outcome"] == "accepted"]
        if group:
            lines.append(f"| {run_id} | {len(accepted)} | {sum(r['outcome']=='insufficient_recorded_iq' for r in group)} | "
                         f"{sum(r['sample_time_s'] for r in accepted):.6f} / {max((r['sample_time_s'] for r in accepted),default=0):.6f} s |")
    lines += ["", "记录不足的具体点：", "", "| 运行 / 保存点 | 保存时长 | 已消费样点 | 最少还需样点 |", "|---|---:|---:|---:|"]
    for result in results:
        if result["outcome"] == "insufficient_recorded_iq":
            bound = result["completion_bound"]
            lines.append(f"| {result['run_id']} / {result['point_number']} | {result['recorded_duration_s']:.6f} s | "
                         f"{bound['last_consumed_sample']} | {bound['minimum_future_samples_for_acceptance']} |")
    lines += ["", "完整旧运行只证明保存下来的各点覆盖情况。不能把部分点采样时长之和写成完整60点的真机时长，"
              "也不能用重复、补零或未保存的未来样点填满缺口。", "", "## 对照和耗时", "",
              "| 合成对照（固定随机种子 20261031） | 预期 | 实际 |", "|---|---|---|"]
    for name, result in report["synthetic_controls"].items():
        lines.append(f"| {name} | {'接受' if result['expected_acceptance'] else '拒绝'} | {result['outcome']} |")
    all_controls = list(report["synthetic_controls"].values())
    lines += ["", f"本次全部真实点回放 CPU 墙钟合计 {sum(r['actual_replay_cpu_wall_seconds'] for r in results):.3f} s；"
              f"单次真实 IQ 质量计算最大 {max(r['quality_call_max_ms'] for r in results):.3f} ms。"
              f"含最长 1 秒窗的合成控制单次质量计算最大 {max(r['quality_call_max_ms'] for r in all_controls):.3f} ms。"
              "这些数值只描述本次执行环境的离线运算，不能替代真机并发与主线程响应测试。", "",
              "## 复现", "", "在仓库根目录使用既有 `rf-gnuradio` 环境：", "", "```sh",
              "python -B tools/replay_adaptive_sweep.py --all-points --output docs/sweep_adaptive_replay_20261031.json",
              "```", "", "可选 `--legacy-source /path/to/frozen/v2/logic.py` 重新生成 v2 对照；"
              "未提供时仅重放当前 v3。`--include-attempts` 可输出全部尝试，否则仅关键三个失败点保留完整候选尝试。"
              "数据按源文件和源码 SHA-256 溯源；脚本拒绝把输出写入 `Data`。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--legacy-source", type=Path,
                        help="Optional frozen v2 logic.py, used only for the three failed-point comparison")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--all-points", action="store_true")
    parser.add_argument("--include-attempts", action="store_true",
                        help="Include full per-window logs for every replay instead of the compact curated report")
    opts = parser.parse_args()
    root = opts.root.resolve()
    v3source = root / "sspp_experiments/material_sweep/logic.py"
    v3_digest = sha(v3source)
    v2_digest = sha(opts.legacy_source) if opts.legacy_source else None
    v2 = load_module(opts.legacy_source, "replay_sweep_v2_frozen") if opts.legacy_source else None
    v3 = load_module(v3source, "replay_sweep_v3_current")
    if v3.SWEEP_ALGORITHM_VERSION != "sspp.sweep.adaptive_coherent_quality.v3":
        raise SystemExit("Candidate v3 not ready")
    results, comparisons = [], []
    for run_id in RUNS:
        sources = sorted((root / "Data/runs" / run_id).glob("iq_point_*.npz"))
        previous = None
        for source in sources:
            with np.load(source, allow_pickle=False) as data:
                tone = json.loads(str(data["point_info"]))["tone_hz"]
            critical = run_id != RUNS[-1] and source == sources[-1]
            if opts.all_points or critical:
                result = inspect_file(v3, source, previous)
                result["critical_failed_point"] = critical
                results.append(result)
            if critical and v2:
                comparison = inspect_file(v2, source, previous)
                compare_original_quality(comparison, source)
                comparisons.append(comparison)
            previous = tone
    controls = synthetic_controls(v3)
    for result in results + comparisons + list(controls.values()):
        if result["accepted_record"]:
            result["verification_windows"] = result.pop("last_three_windows")[:2]
        if not opts.include_attempts and not result.get("critical_failed_point"):
            result.pop("attempts")
        if result in comparisons and not opts.include_attempts:
            result.pop("last_three_windows", None)
            result.pop("config", None)
    if sha(v3source) != v3_digest or (opts.legacy_source and sha(opts.legacy_source) != v2_digest):
        raise SystemExit("Algorithm source changed during replay; rerun after the candidate is frozen")
    report = {"schema": "sspp.actual_state_replay.v1", "v3_source_sha256": v3_digest,
              "v2_source_sha256": v2_digest,
              "replay_tool_sha256": sha(__file__),
              "method": ["Actual SweepRunner.run with overridden hardware and frame-delivery hooks only.",
                         "Known preceding tone supplied at quality function boundary for single-point replay.",
                         "All saved frames read from IQ offset zero; software frame indices verified continuous.",
                         "Frame arrival time is cumulative samples/rate; deterministic eight-second point deadline.",
                         "EOF is insufficient recorded IQ unless mandatory remaining window lengths prove timeout even with a perfect unrecorded tail; no invented samples.",
                         "CPU duration benchmark is separate from ideal sample-time clock; no actual USB/hardware validation.",
                         "No changes to gain, amplitude, frequency tolerance, raw data or historical status."],
              "v3_results": results, "v2_failed_point_comparison": comparisons, "synthetic_controls": controls,
              "summary": {"v3_outcomes": dict(Counter(r["outcome"] for r in results)),
                          "v2_outcomes": dict(Counter(r["outcome"] for r in comparisons)),
                          "synthetic_controls_passed": all(r["matches_expectation"] for r in controls.values())}}
    output = opts.output.resolve()
    if root / "Data" == output or root / "Data" in output.parents:
        raise SystemExit("Refusing to write inside original Data")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    output.with_suffix(".md").write_text(render_markdown(report))
    print(json.dumps(report["summary"], ensure_ascii=False))
    for r in results:
        if r["run_id"] != RUNS[-1] and r["file"] == sorted((root/'Data/runs'/r['run_id']).glob('iq_point_*.npz'))[-1].name:
            a=r["accepted_record"] or {}
            print(r["run_id"], r["file"], r["outcome"], r["sample_time_s"], a.get("window_start"), a.get("window_count"))
    if not report["summary"]["synthetic_controls_passed"]:
        raise SystemExit("Synthetic controls failed")


if __name__ == "__main__":
    main()
