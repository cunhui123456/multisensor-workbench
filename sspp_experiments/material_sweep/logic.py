# 当前 SSPP 实验软件模块；源自已验证实现，老师原件保留在 historical/teacher_20260908/。
"""扫频逻辑模块（简化版）。"""

import os
import math
import time
from itertools import count
from queue import Empty
from dataclasses import dataclass

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

SWEEP_ALGORITHM_VERSION = "sspp.sweep.target_confirmed.v5"


class PointQualityTimeout(TimeoutError):
    """实收IQ持续但没有在点时限内得到可信测量；不代表设备故障。"""
    pass

"""
Python 里的装饰器。
装饰器的作用是：在不改变函数或类主体写法的情况下，给它额外增加一种行为
@dataclass 的作用是：让这个类自动变成一个参数容器
如果不用 @dataclass，你可能要这样写：
class SweepConfig:
    def __init__(
        self,
        num_points=12,
        freq_start=2.4e9,
        freq_stop=3.0e9,
        sweep_repeat=3,
        tone_start=2e3,
        tone_step=1e3
    ):
        self.num_points = num_points
        self.freq_start = freq_start
        self.freq_stop = freq_stop
        self.sweep_repeat = sweep_repeat
        self.tone_start = tone_start
        self.tone_step = tone_step
"""
# 集中保存扫描范围、轮数、单音周期窗、FFT 点数和导出位置。
@dataclass 
class SweepConfig:
    """扫频实验参数集中管理。"""
    num_points: int = 12
    freq_start: float = 2.4e9
    freq_stop: float = 3.0e9
    sweep_repeat: int = 3#改
    continuous: bool = False
    skip_invalid_points: bool = False
    tone_start: float = 2e3
    tone_step: float = 1e3
    # VERIFY 阶段：采集 20 个 tone 周期，用于判断当前频点是否稳定；
    verify_cycles: int = 20
    # MEASURE 阶段：采集 80 个 tone 周期，用于正式记录幅值
    measure_cycles: int = 80
    # 这是验证容差。比如当前期望 tone 是 13000 Hz，实测 FFT 峰值只要在：
    # 12800 Hz 到 13200 Hz 之间，就认为验证通过
    # 如果采样率是 250 kS/s，nfft=2048 时频率间隔大约是：250000 / 2048 ≈ 122 Hz
    # 设置的验证容差是 100 Hz，比一个 FFT bin 还小。
    # 会导致有时明明主峰已经接近期望 tone，也可能因为落在相邻格点而失败。
    # 上方历史说明中的“100 Hz”是旧文字；当前实际配置为 200 Hz，以字段值为准。
    verify_tolerance_hz: float = 200.0
    nfft: int = 2048
    verify_min_seconds: float = 0.020
    measure_min_seconds: float = 0.040
    verify_consecutive_windows: int = 2
    min_tone_snr_db: float = 6.0
    previous_tone_rejection_db: float = 6.0
    previous_tone_presence_db: float = 6.0
    adaptive_window_seconds: tuple = (.020, .050, .100, .250, .500, 1.000, 2.000)
    adaptive_rejections_per_level: int = 2
    adaptive_carryover_hold_seconds: float = .250
    switch_settle_time: float = 0.1
    save_dir: str = "./Data"
    save_filename: str = "Sweep_data.xlsx"

    # @property 的作用是：把一个函数伪装成一个属性来用
    # 正常函数调用应该是：sweep_cfg.freq_step()
    # 但是加了 @property 后，就可以写成：sweep_cfg.freq_step
    # 包含首尾两端点，因此分母是 num_points - 1；单点返回零避免除零。
    @property
    def freq_step(self):
        if self.num_points <= 1:
            return 0.0
        return (self.freq_stop - self.freq_start) / (self.num_points - 1)

    # 以接收采样率检查最高基带单音，防止落到 Nyquist 范围之外。
    def validate(self, sdr_rate):
        values = (sdr_rate, self.verify_min_seconds, self.measure_min_seconds,
                  self.min_tone_snr_db, self.previous_tone_rejection_db,
                  self.previous_tone_presence_db, self.adaptive_carryover_hold_seconds,
                  self.verify_tolerance_hz, self.tone_start, self.tone_step)
        if not all(math.isfinite(v) for v in values) or sdr_rate <= 0:
            raise ValueError("扫频质量参数必须为有限数，采样率必须为正")
        if (self.verify_min_seconds < .020 or self.measure_min_seconds < .040
                or self.verify_consecutive_windows < 2 or int(self.verify_consecutive_windows) != self.verify_consecutive_windows
                or self.min_tone_snr_db < 6 or self.previous_tone_rejection_db < 6 or self.previous_tone_presence_db < 6
                or not 0 <= self.adaptive_carryover_hold_seconds <= .5
                or self.nfft < 2 or self.verify_cycles <= 0 or self.measure_cycles <= 0
                or self.verify_tolerance_hz <= 0 or self.tone_start <= 0 or self.tone_step < 0):
            raise ValueError("扫频质量门限或窗口参数无效")
        levels = self.adaptive_window_seconds
        if (not isinstance(levels, (tuple,list)) or not levels
                or not all(isinstance(v, (int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in levels)
                or levels[0] < .020 or levels[-1] > 2.0
                or any(a >= b for a,b in zip(levels,levels[1:]))
                or self.adaptive_rejections_per_level != 2):
            raise ValueError("自适应窗口必须递增且位于 20 ms–2 s；每档累计两次拒绝后升级")
        base_max_samples = max(math.ceil(self.verify_cycles*sdr_rate/self.tone_start),
                               math.ceil(self.measure_cycles*sdr_rate/self.tone_start),
                               math.ceil(self.verify_min_seconds*sdr_rate),
                               math.ceil(self.measure_min_seconds*sdr_rate))
        if base_max_samples/sdr_rate > 1.0:
            raise ValueError("最低周期数或最短时长要求超过 1 秒窗口上限；请调整单音/采样参数")
        max_tone = self.tone_start + (self.num_points - 1) * self.tone_step
        if max_tone >= sdr_rate / 2:
            raise ValueError("最大 tone 不能超过 Nyquist 频率")

# 输入复数 IQ 和采样率，返回峰频 Hz/幅度 dB；短窗补零不会提高真实频率分辨能力。
def fft_peak(data_seg, rate, nfft=2048, expected_freq=None, search_bw=1000.0):
    """
    返回数据段 FFT 峰值频率(Hz)和幅度(dB)。

    expected_freq:
        如果为 None，则全频谱找最大峰；
        如果给定，例如当前 tone=2000 Hz，则只在 tone 附近找峰。

    search_bw:
        在 expected_freq ± search_bw 范围内搜索峰值。
    """
    if data_seg is None or len(data_seg) < 2 or nfft < 2:
        return 0.0, -300.0

    x = np.asarray(data_seg, dtype=np.complex64)

    # nfft 是最低 FFT 长度；完整测量窗全部参与，禁止截掉早期实测样本。
    nfft = max(int(nfft), len(x))

    # Hann 窗抑制频谱泄漏；用窗和归一化可估计落在 FFT 栅格上的单音幅值。
    window = np.hanning(len(x))
    spec = np.fft.fftshift(np.fft.fft(x * window, nfft))
    freq = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / rate))
    # 幅度用 20 log10，不是功率的 10 log10；频率不落在栅格上时仍有栅栏误差。
    spec_db = 20.0 * np.log10(np.abs(spec) / np.sum(window) + 1e-12)

    # 局部找峰只回答目标单音附近的问题；需要全频谱检查才能排除别处更强峰。
    if expected_freq is not None:
        mask = (
            (freq >= expected_freq - search_bw)
            &
            (freq <= expected_freq + search_bw)
        )

        if np.any(mask):
            local_freq = freq[mask]
            local_spec_db = spec_db[mask]
            idx = np.argmax(local_spec_db)
            return float(local_freq[idx]), float(local_spec_db[idx])

    idx = np.argmax(spec_db)
    return float(freq[idx]), float(spec_db[idx])


def sweep_algorithm_policy(cfg):
    """用于 manifest/跨记录比较的可复现算法身份。"""
    return {"version": SWEEP_ALGORITHM_VERSION,
            "window": "Hann; use all measured samples; FFT length=max(nfft, window_count)",
            "verify_min_seconds": cfg.verify_min_seconds, "measure_min_seconds": cfg.measure_min_seconds,
            "verify_consecutive_windows": cfg.verify_consecutive_windows,
            "min_tone_snr_db": cfg.min_tone_snr_db,
            "previous_tone_rejection_db": cfg.previous_tone_rejection_db,
            "previous_tone_presence_db": cfg.previous_tone_presence_db,
            "adaptive_window_seconds": list(cfg.adaptive_window_seconds),
            "adaptive_rejections_per_level": cfg.adaptive_rejections_per_level,
            "rejected_window_policy": "reuse_last_failed_window_only_on_escalation",
            "skip_invalid_points":cfg.skip_invalid_points, "point_budget_policy":"unchanged absolute deadline; invalid points remain missing; no reset or interpolation",
            "adaptive_carryover_hold_seconds": cfg.adaptive_carryover_hold_seconds,
            "previous_tone_detection": "exact_tone_with_half_window_support_or_split_candidate",
            "verify_tolerance_hz": cfg.verify_tolerance_hz,
            "nfft_min": cfg.nfft, "search_bandwidth_hz": cfg.verify_tolerance_hz,
            "target_candidate_search": "expected_tone_plus_minus_verify_tolerance_hz",
            "target_presence_method": "exact_tone_with_independent_halves_on_background_dominance",
            "target_confirmation_snr_db": cfg.min_tone_snr_db,
            "amplitude_estimator": "exact_expected_tone_hann_coefficient",
            "target_numerical_floor": "float32_epsilon_times_input_rms",
            "noise_floor_bandwidth_hz": 1000.0,
            "snr_definition": "exact expected-tone Hann coefficient versus median local FFT-bin amplitude; engineering prominence, not calibrated physical SNR",
            "retry_policy": "on escalation retain only the last failed window as prefix; accepted VERIFY and MEASURE windows never overlap; two cumulative rejected windows at a level advance duration; evidenced previous-tone rejection during initial carryover hold delays escalation only; isolated passes do not reset rejection count; rejected MEASURE returns to two-window VERIFY; original point deadline never reset"}


def _split_previous_candidate(data, rate, previous_tone, target_tone, cfg):
    """前半窗选候选频率，后半窗独立确认；噪声频带最大峰本身不算旧单音。"""
    halves = (data[:len(data)//2], data[len(data)//2:])
    results = []
    candidate = None
    for samples in halves:
        count = len(samples)
        window = np.hanning(count)
        weighted = samples*window
        frequencies = np.fft.fftfreq(count, 1/rate)
        spectrum = abs(np.fft.fft(weighted))/window.sum()
        band = np.flatnonzero(abs(frequencies-previous_tone) <= cfg.verify_tolerance_hz)
        exclusion = max(cfg.verify_tolerance_hz, 3*rate/count)
        floor_mask = ((abs(frequencies-previous_tone) <= 1000)
                      & (abs(frequencies-previous_tone) > exclusion)
                      & (abs(frequencies-target_tone) > exclusion))
        if not len(band) or np.count_nonzero(floor_mask) < 5:
            return {"present": False, "candidate_hz": None, "reason": "insufficient_half_window_resolution"}
        peak_hz = float(frequencies[band[np.argmax(spectrum[band])]])
        if candidate is None:
            candidate = peak_hz
        coefficient = float(abs(np.sum(weighted*np.exp(-2j*np.pi*candidate*np.arange(count)/rate))/window.sum()))
        floor = float(np.median(spectrum[floor_mask]))
        prominence = float(20*np.log10(max(coefficient,1e-15)/max(floor,1e-15)))
        results.append({"peak_hz":peak_hz,"candidate_prominence_db":prominence,
                        "bin_width_hz":rate/count})
    agrees = abs(results[0]["peak_hz"]-results[1]["peak_hz"]) <= max(q["bin_width_hz"] for q in results)
    present = agrees and all(q["candidate_prominence_db"] >= cfg.previous_tone_presence_db for q in results)
    return {"present":bool(present),"candidate_hz":candidate,"frequency_agreement":bool(agrees),
            "first_half":results[0],"second_half":results[1]}


def _target_half_confirmation(data, rate, tone, previous_tone, cfg, threshold_db=None):
    """在两个互不重叠半窗各自确认已知单音，不以搜索得到的噪声峰代替目标。"""
    threshold_db = cfg.min_tone_snr_db if threshold_db is None else threshold_db
    results = []
    for samples in (data[:len(data)//2], data[len(data)//2:]):
        n = len(samples)
        if n < 3:
            return {"confirmed": False, "reason": "insufficient_half_window_resolution", "halves": results}
        window = np.hanning(n)
        weighted = samples * window
        frequencies = np.fft.fftfreq(n, 1.0 / rate)
        spectrum = np.abs(np.fft.fft(weighted)) / window.sum()
        exclusion = max(cfg.verify_tolerance_hz, 3.0 * rate / n)
        floor_mask = ((np.abs(frequencies-tone) <= 1000.0)
                      & (np.abs(frequencies-tone) > exclusion))
        if previous_tone is not None and abs(previous_tone-tone) > 2*cfg.verify_tolerance_hz:
            floor_mask &= np.abs(frequencies-previous_tone) > exclusion
        if np.count_nonzero(floor_mask) < 5:
            return {"confirmed": False, "reason": "insufficient_half_window_resolution", "halves": results}
        coefficient = float(abs(np.sum(weighted*np.exp(-2j*np.pi*tone*np.arange(n)/rate))/window.sum()))
        floor = float(np.median(spectrum[floor_mask]))
        prominence = float(20*np.log10(max(coefficient, 1e-15)/max(floor, 1e-15)))
        results.append({"sample_count": n, "target_snr_db": prominence,
                        "target_amplitude_dB": float(20*np.log10(max(coefficient, 1e-15))),
                        "local_floor_dB": float(20*np.log10(max(floor, 1e-15)))})
    return {"confirmed": all(row["target_snr_db"] >= threshold_db for row in results),
            "halves": results}


def quality_measurement(data, rate, tone, previous_tone, cfg):
    """目标带候选＋完整窗/两半窗已知单音确认；独立保存宽带背景诊断。"""
    x = np.asarray(data, dtype=np.complex64)
    if x.ndim != 1 or len(x) < 3 or not np.isfinite(x).all():
        raise ValueError("扫频质量判定需要有限的一维 IQ 窗")
    n = len(x)
    # 不能把单精度量化/运算残差相对近零噪声底的“突出”当成真实目标。
    input_rms = float(np.sqrt(np.mean(np.abs(x.astype(np.complex128))**2)))
    numerical_floor = max(1e-15, input_rms * np.finfo(np.float32).eps)
    nfft = max(int(cfg.nfft), n)
    window = np.hanning(n)
    weighted = x * window
    scale = window.sum()
    spectrum = np.abs(np.fft.fft(weighted, nfft)) / scale
    frequencies = np.fft.fftfreq(nfft, 1.0 / rate)
    local = np.abs(frequencies - tone) <= 1000.0
    indices = np.flatnonzero(local)
    if not len(indices):
        raise ValueError("采样率/FFT 设置无法覆盖目标单音")
    background_index = indices[np.argmax(spectrum[indices])]
    target_indices = np.flatnonzero(np.abs(frequencies-tone) <= cfg.verify_tolerance_hz)
    if not len(target_indices):
        raise ValueError("采样率/FFT 设置无法覆盖目标频率容差带")
    peak_index = target_indices[np.argmax(spectrum[target_indices])]
    measured = float(frequencies[peak_index])
    # 用已知发射单音的相干系数判定目标是否存在，避免把搜索区噪声最大峰当信号。
    times = np.arange(n, dtype=np.float64) / rate
    target = float(abs(np.sum(weighted * np.exp(-2j * np.pi * tone * times)) / scale))
    exclusion = max(cfg.verify_tolerance_hz, 3.0 * rate / n)
    floor_mask = local & (np.abs(frequencies - tone) > exclusion)
    distinct_previous = previous_tone is not None and abs(previous_tone - tone) > 2 * cfg.verify_tolerance_hz
    if distinct_previous:
        floor_mask &= np.abs(frequencies - previous_tone) > exclusion
    if np.count_nonzero(floor_mask) < 5:
        raise ValueError("窗口过短，无法估计目标单音邻域噪声底")
    floor = float(np.median(spectrum[floor_mask]))
    snr = float(20 * np.log10(max(target, 1e-15) / max(floor, 1e-15)))
    previous_amplitude, previous_margin = None, None
    previous_floor_db, previous_snr, previous_present = None, None, False
    previous_exact_present = False
    previous_exact_confirmation = None
    previous_split_evidence = None
    if distinct_previous:
        # 先证明已知的前一发射单音高于它自己的频域噪声底，再施加抑制门限。
        # 不把前一频率 ±200 Hz 内任意随机最大峰冒充残留旧单音。
        previous_coherent = float(abs(np.sum(weighted * np.exp(-2j*np.pi*previous_tone*times))/scale))
        previous_floor_mask = ((np.abs(frequencies-previous_tone) <= 1000)
                               & (np.abs(frequencies-previous_tone) > exclusion)
                               & (np.abs(frequencies-tone) > exclusion))
        if np.count_nonzero(previous_floor_mask) < 5:
            raise ValueError("窗口过短，无法估计前一单音邻域噪声底")
        previous_floor = float(np.median(spectrum[previous_floor_mask]))
        previous_amplitude = float(20 * np.log10(max(previous_coherent, 1e-15)))
        previous_floor_db = float(20 * np.log10(max(previous_floor, 1e-15)))
        previous_snr = float(20 * np.log10(max(previous_coherent, 1e-15)/max(previous_floor, 1e-15)))
        previous_halves = _target_half_confirmation(x, rate, previous_tone, tone, cfg,
                                                     cfg.previous_tone_presence_db)["halves"]
        # 全窗突出还须至少半窗支持；旧单音只在前半残留时也必须拦截。
        previous_exact_confirmation = {
            "confirmed": len(previous_halves) == 2 and any(
                h["target_snr_db"] >= cfg.previous_tone_presence_db for h in previous_halves),
            "required_halves": 1, "halves": previous_halves}
        previous_exact_present = (previous_snr >= cfg.previous_tone_presence_db
                                  and previous_coherent > numerical_floor
                                  and previous_exact_confirmation["confirmed"])
        previous_split_evidence = _split_previous_candidate(x,rate,previous_tone,tone,cfg)
        previous_present = previous_exact_present or previous_split_evidence['present']
        if previous_split_evidence['present']:
            # 只有独立半窗已证明有旧单音时才读取完整 FFT 的旧频带峰值。
            # 半窗的频率栅格较粗，不能用其频率重算整窗相干系数，否则可低估约 6 dB。
            previous_band = np.abs(frequencies-previous_tone) <= cfg.verify_tolerance_hz
            candidate_amplitude = float(np.max(spectrum[previous_band]))
            previous_coherent = max(previous_coherent,candidate_amplitude)
            previous_amplitude = float(20*np.log10(max(previous_coherent,1e-15)))
        previous_present = previous_present and previous_coherent > numerical_floor
        previous_margin = float(20 * np.log10(max(target, 1e-15)/max(previous_coherent, 1e-15)))
    background_dominates = abs(float(frequencies[background_index])-tone) >= cfg.verify_tolerance_hz
    target_confirmation = ({"required": True, **_target_half_confirmation(x, rate, tone, previous_tone, cfg)}
                           if background_dominates else {"required": False, "confirmed": None, "halves": []})
    reasons = []
    if target <= numerical_floor:
        reasons.append("target_tone_below_numerical_resolution")
    if background_dominates and not target_confirmation["confirmed"]:
        reasons.append("target_tone_not_confirmed_in_both_halves")
    if abs(measured - tone) >= cfg.verify_tolerance_hz:
        reasons.append("tone_frequency_out_of_tolerance")
    if snr < cfg.min_tone_snr_db:
        reasons.append("target_tone_below_local_floor_margin")
    if previous_present and previous_margin < cfg.previous_tone_rejection_db:
        reasons.append("previous_tone_not_sufficiently_rejected")
    return {"algorithm_version": SWEEP_ALGORITHM_VERSION, "passed": not reasons,
            "rejection_reasons": reasons, "measured_tone_hz": measured,
            "amplitude_dB": float(20*np.log10(max(target, 1e-15))), "expected_tone_hz": float(tone),
            "amplitude_estimator": "exact_expected_tone_hann_coefficient",
            "amplitude_frequency_hz": float(tone),
            "target_confirmation": target_confirmation,
            "target_band_peak_amplitude_dB": float(20*np.log10(max(float(spectrum[peak_index]), 1e-15))),
            "wide_band_peak_hz": float(frequencies[background_index]),
            "wide_band_peak_amplitude_dB": float(20*np.log10(max(float(spectrum[background_index]), 1e-15))),
            "previous_tone_hz": float(previous_tone) if previous_tone is not None else None,
            "target_amplitude_dB": float(20 * np.log10(max(target, 1e-15))),
            "local_floor_dB": float(20 * np.log10(max(floor, 1e-15))), "target_snr_db": snr,
            "numerical_amplitude_floor_dB": float(20*np.log10(numerical_floor)),
            "previous_tone_amplitude_dB": previous_amplitude, "previous_tone_margin_db": previous_margin,
            "previous_tone_present": bool(previous_present), "previous_tone_snr_db": previous_snr,
            "previous_local_floor_dB": previous_floor_db,
            "previous_tone_detection": "exact_tone_with_half_window_support_or_split_candidate",
            "previous_exact_frequency_present":bool(previous_exact_present),
            "previous_exact_confirmation": previous_exact_confirmation,
            "previous_split_evidence":previous_split_evidence,
            "previous_tone_gate_status": ("applied" if previous_present else
                                           "not_present_above_floor" if distinct_previous else "not_distinct"),
            "window_count": n, "fft_length": nfft, "sample_rate_hz": float(rate),
            "window_duration_s": n / rate, "frequency_error_hz": measured - float(tone)}

# def fft_peak(data_seg, rate, nfft=2048):
#     """
#     返回数据段 FFT 峰值频率(Hz)和幅度(dB)。
#     高 tone 时 VERIFY 数据段较短，实际 FFT 点数被压得很小。
#     例如 13 kHz 时，验证阶段只取 20 个周期，数据点大约几百个。    
#     """
#     if data_seg is None or len(data_seg) < 2 or nfft < 2:
#         return 0.0, -300.0
#     # 把输入数据转成复数数组。USRP 接收的是 IQ 数据，所以数据类型应该是复数。
#     x = np.asarray(data_seg, dtype=np.complex64)

#     if len(x) > nfft:
#         x = x[-nfft:]

#     window = np.hanning(len(x))
#     spec = np.fft.fftshift(np.fft.fft(x * window, nfft))
#     freq = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / rate))
#     spec_db = 20.0 * np.log10(np.abs(spec) / np.sum(window) + 1e-12)
#     idx = np.argmax(spec_db)

#     return float(freq[idx]), float(spec_db[idx])

# 处理扫频状态与缓存；硬件设置委托 manager，图形更新委托 drawer。
class SweepRunner:
    """扫频流程控制器（不涉及USRP底层）。"""
    VERIFY, MEASURE = 0, 1
    def on_measurement(self, sweep_idx, point_idx, freq, tone, measured, amplitude):
        """测量事件扩展点；默认无操作，网页采集子类仅输出结构化事件。"""
        pass
    # 把外部对象保存到类内部
    # 共享 args.tone 供 TX 读取；raw_queue、stop_event 连接接收线程和控制器。
    def __init__(self, args, sweep_cfg, usrp_mgr, raw_queue, stop_event, drawer=None):
        self.args = args              # args 是 SDRConfig，比如采样率、当前 tone 等
        self.cfg = sweep_cfg          # sweep_cfg 是 SweepConfig，也就是扫频参数
        self.usrp_mgr = usrp_mgr      # usrp_mgr 是 USRPManager，负责设置 USRP 的 TX/RX 频率
        self.raw_queue = raw_queue
        self.stop_event = stop_event
        self.drawer = drawer

    # 报告请求扫描参数；实际硬件载波回读由本机入口另行保存。
    def _print_info(self, freq_list):
        num = len(freq_list)
        print("连续循环扫频至手动停止" if self.cfg.continuous else f"重复扫频次数: {self.cfg.sweep_repeat}")
        print(f"扫频范围: {self.cfg.freq_start/1e9:.3f} ~ {self.cfg.freq_stop/1e9:.3f} GHz, 共 {num} 个点")
        print(f"频率步进: {self.cfg.freq_step/1e6:.3f} MHz")
        print(f"基带频率: {self.cfg.tone_start/1e3:.1f} ~ {(self.cfg.tone_start+(num-1)*self.cfg.tone_step)/1e3:.1f} kHz, 步进 {self.cfg.tone_step/1e3:.1f} kHz")
        print(f"FFT 点数: {self.cfg.nfft}")

    # 同时切换基带单音和双端载波；等待期用于减少切频过渡，不保证在途帧全部排空。
    def _set_freq_and_tone(self, freq, tone):
        self.args.tone = tone
        self.usrp_mgr.set_center_freq(freq)
        time.sleep(self.cfg.switch_settle_time)

    # 清除切频前已入队的数据；后续 VERIFY 仍需识别残留旧单音。
    def _clear_queue(self):
        """
        切换频点后，队列里可能还有上一个频点残留的数据。
        为了避免上一个频点的数据参与当前频点的 VERIFY/MEASURE，切频后要清空队列。
        """
        while True:
            try:
                self.raw_queue.get_nowait()
            except Empty:
                break

    # 取出所有当前帧供测量，最后一帧供显示；原版丢弃 start_index 不作连续性判定。
    def _fetch_available_frames(self):
        """
        从 raw_queue 中取出当前已经到达的所有 RX 数据帧。
        返回:
            frames:
                当前队列中所有新数据帧，按接收顺序排列。
                用于加入 rx_buffer，保证扫频测量尽量使用连续数据。
            latest:
                最新一帧数据。
                用于实时绘图。
        raw_queue 中的数据格式可以是：
            1. (start_index, samples)
            2. samples
        如果是第一种格式，只取 samples。

        raw_queue 中可能有多帧 RX 数据
                    ↓
        fetch_available_frames() 全部取出
                    ↓
        所有 frames 加入 rx_buffer 参与 VERIFY / MEASURE
                    ↓
        最后一帧 latest_samples 用于实时绘图
        """
        frames = []
        latest = None

        while True:
            try:
                # 不断从 raw_queue 里取数据，直到队列为空
                # get_nowait() 是非阻塞读取。如果队列有数据，就取出；
                # 如果没有数据，就抛出 Empty，结束while循环
                item = self.raw_queue.get_nowait()
                # 这里兼容两种队列格式
                if isinstance(item, tuple) and len(item) == 2:
                    _, samples = item
                else:
                    samples = item
                # 把数据统一转成 complex64 格式
                samples = np.asarray(samples, dtype=np.complex64)
                # 把当前帧加入 frames。同时把 latest 更新为当前帧。
                # 因为循环会一直取到队列为空，所以最后一次赋值的 latest 就是最新一帧。
                frames.append(samples)
                latest = samples
            except Empty:
                break

        return frames, latest       
    
    def _calc_samples(self, tone):
        """周期数与最短真实采样时长双重下限；高单音不再退化成数百点短窗。"""
        rate = getattr(self.args, "rx_rate_actual", None) or self.args.rate
        verify = max(math.ceil(self.cfg.verify_cycles * rate / tone),
                     math.ceil(self.cfg.verify_min_seconds * rate))
        measure = max(math.ceil(self.cfg.measure_cycles * rate / tone),
                      math.ceil(self.cfg.measure_min_seconds * rate))
        return verify, measure

    def _verify_pass(self, peak_freq, tone):
        return abs(peak_freq - tone) < self.cfg.verify_tolerance_hz

    def on_quality_attempt(self, record):
        """记录每次实际处理的窗；子类负责持久化，默认不操作磁盘。"""
        pass

    def point_deadline_expired(self):
        """硬件记录子类提供绝对截止时间；离线领域执行可注入独立时间策略。"""
        return False

    def on_invalid_measurement(self, repeat, point, frequency, tone, quality):
        pass

    def invalid_quality_details(self):
        return {}

    def on_round_started(self, repeat):
        pass

    def on_round_finished(self, repeat, complete):
        pass

    def run(self, enable_plot=True):
        rate = getattr(self.args, "rx_rate_actual", None) or self.args.rate
        self.cfg.validate(rate)
        frequencies = np.linspace(self.cfg.freq_start, self.cfg.freq_stop, self.cfg.num_points)
        self._print_info(frequencies)
        if enable_plot and self.drawer:
            self.drawer.setup_plot()
        # 从创建起即保存引用；停止/异常时调用者可以封存此前已接受的数据。
        self.partial_result = {
            "freq_list": frequencies, "all_freq_records": [], "all_amp_records": [],
            "all_expected_tone_records": [], "all_measured_tone_records": [],
            "all_quality_records": [], "algorithm_version": SWEEP_ALGORITHM_VERSION,
            "sweep_algorithm": sweep_algorithm_policy(self.cfg),
        }
        self.last_measurement_quality = None
        last_plot_time = 0.0
        previous_tone = None
        for repeat in (count() if self.cfg.continuous else range(self.cfg.sweep_repeat)):
            if self.stop_event.is_set():
                break
            print(f"开始第 {repeat+1} 轮连续扫频" if self.cfg.continuous else f"开始第 {repeat+1} / {self.cfg.sweep_repeat} 次扫频")
            self.on_round_started(repeat+1)
            round_frequencies, amplitudes, expected, measured, accepted_quality = [], [], [], [], []
            for key, rows in (("all_freq_records", round_frequencies), ("all_amp_records", amplitudes),
                              ("all_expected_tone_records", expected), ("all_measured_tone_records", measured),
                              ("all_quality_records", accepted_quality)):
                self.partial_result[key].append(rows)
            for point, frequency in enumerate(frequencies):
                if self.stop_event.is_set() or (enable_plot and self.drawer and not self.drawer.is_open()):
                    break
                tone = self.cfg.tone_start + point * self.cfg.tone_step
                self._set_freq_and_tone(frequency, tone)
                self._clear_queue()
                verify_samples, measure_samples = self._calc_samples(tone)
                state, consecutive, attempt = self.VERIFY, 0, 0
                adaptive_level, level_rejections = 0, 0
                rx_buffer = np.empty(0, dtype=np.complex64)
                window_start = 0
                quality_timeout = None
                while not self.stop_event.is_set():
                    if enable_plot and self.drawer and not self.drawer.is_open():
                        self.stop_event.set()
                        break
                    # 每次检查同一频点的绝对截止时间；拒绝/重验不会重置它。
                    try:
                        frames, latest = self._fetch_available_frames()
                    except PointQualityTimeout as exc:
                        if not self.cfg.skip_invalid_points:raise
                        quality_timeout=str(exc)
                        break
                    if frames:
                        rx_buffer = np.concatenate((rx_buffer, *frames))
                    if enable_plot and self.drawer and latest is not None:
                        now = time.monotonic()
                        if now - last_plot_time >= self.drawer.plot_interval:
                            self.drawer.update_plots(latest)
                            self.drawer.pause(.001)
                            last_plot_time = now
                    level_duration = self.cfg.adaptive_window_seconds[adaptive_level]
                    required = max(verify_samples if state == self.VERIFY else measure_samples,
                                   math.ceil(level_duration*rate))
                    if len(rx_buffer) < required:
                        time.sleep(.001)
                        continue
                    # 通过窗顺序消费、不重叠；升级时仅失败窗可扩展。坐标对应保存 NPZ 的 iq。
                    data, rx_buffer = rx_buffer[:required], rx_buffer[required:]
                    quality = quality_measurement(data, rate, tone, previous_tone, self.cfg)
                    attempt += 1
                    rejections_before = level_rejections
                    if not quality['passed']:
                        level_rejections += 1
                    next_level = adaptive_level
                    carryover_hold = (not quality['passed']
                                      and 'previous_tone_not_sufficiently_rejected' in quality['rejection_reasons']
                                      and (window_start+required)/rate <= self.cfg.adaptive_carryover_hold_seconds)
                    if (not quality['passed'] and level_rejections >= self.cfg.adaptive_rejections_per_level
                            and not carryover_hold
                            and adaptive_level+1 < len(self.cfg.adaptive_window_seconds)):
                        next_level += 1
                    deadline_expired = self.point_deadline_expired()
                    cancelled = self.stop_event.is_set()
                    record = {**quality, "repeat": repeat+1, "point": point+1,
                              "requested_carrier_hz": float(frequency),
                              "state": "VERIFY" if state == self.VERIFY else "MEASURE",
                              "quality_attempt_index": attempt, "window_start": window_start,
                              "accepted": state == self.MEASURE and quality["passed"] and not deadline_expired and not cancelled,
                              "verified_windows_before": consecutive,
                              "adaptive_level_index":adaptive_level,"adaptive_window_seconds":level_duration,
                              "level_rejections_before":rejections_before,"level_rejections_after":level_rejections,
                              "next_adaptive_level_index":next_level,
                              "retained_prefix_count": (required if next_level != adaptive_level
                                                        and not deadline_expired and not cancelled else 0),
                              "carryover_hold":bool(carryover_hold),"point_deadline_expired":bool(deadline_expired),
                              "stop_requested":bool(cancelled),
                              "host_processed_monotonic_s": time.monotonic()}
                    window_start += required
                    self.on_quality_attempt(record)
                    if deadline_expired:
                        if self.cfg.skip_invalid_points:
                            quality_timeout="Point quality deadline expired during window processing; measurement not accepted"
                            break
                        raise TimeoutError("Point quality deadline expired during window processing; measurement not accepted")
                    if cancelled:
                        break
                    if not quality["passed"]:
                        # 孤立通过不清空本档拒绝累计；失败测量也计入升级，始终重验两窗。
                        state, consecutive = self.VERIFY, 0
                        if next_level != adaptive_level:
                            # 失败窗不作测量，但其真实 IQ 可扩展为下一档更长候选；
                            # 两个已通过的 VERIFY 与最终 MEASURE 仍各用独立新样本。
                            rx_buffer = np.concatenate((data, rx_buffer))
                            window_start -= required
                            adaptive_level, level_rejections = next_level, 0
                        print(f"[第 {repeat+1} 次/点 {point+1}] 质量未通过，继续同频点 "
                              f"{self.cfg.adaptive_window_seconds[adaptive_level]*1000:g} ms 验证: "
                              + ", ".join(quality["rejection_reasons"]))
                        continue
                    if state == self.VERIFY:
                        consecutive += 1
                        if consecutive >= self.cfg.verify_consecutive_windows:
                            state = self.MEASURE
                        continue
                    peak_frequency, amplitude = quality["measured_tone_hz"], quality["amplitude_dB"]
                    round_frequencies.append(float(frequency/1e9))
                    amplitudes.append(amplitude)
                    expected.append(float(tone))
                    measured.append(peak_frequency)
                    accepted_quality.append(record)
                    self.last_measurement_quality = record
                    print(f"[第 {repeat+1} 次/点 {point+1}] 测量通过: {frequency/1e9:.6f} GHz, "
                          f"{peak_frequency:.1f} Hz, {amplitude:.2f} dB, 完整 {required} 样点")
                    self.on_measurement(repeat, point, frequency, tone, peak_frequency, amplitude)
                    break
                if quality_timeout is not None and not self.stop_event.is_set():
                    invalid={"measurement_status":"invalid", "accepted":False, "reason_code":"point_quality_timeout",
                             "reason":quality_timeout, **self.invalid_quality_details()}
                    round_frequencies.append(float(frequency/1e9));amplitudes.append(None)
                    expected.append(float(tone));measured.append(None);accepted_quality.append(invalid)
                    self.on_invalid_measurement(repeat,point,frequency,tone,invalid)
                previous_tone = tone
                if self.stop_event.is_set():
                    break
            self.on_round_finished(repeat+1, len(amplitudes)==self.cfg.num_points)
        return self.partial_result


# 保留老师的频率两位小数与统计舍入格式；精确频率表由 run_local.py 导出。
def save_sweep_summary_excel(result, save_dir, filename):
    """保存多次扫频数据到 Excel（自动调整列宽）。"""
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, filename)
    freq_ghz = [round(f / 1e9, 2) for f in result["freq_list"]]
    amp_records = result["all_amp_records"]

    data = {"Carrier_Frequency_GHz": freq_ghz}
    for i, rec in enumerate(amp_records):
        col = f"Amp_Run_{i+1}_dB"
        data[col] = rec + [np.nan] * (len(freq_ghz) - len(rec))
    df = pd.DataFrame(data)

    if len(amp_records) > 1:
        amp_cols = [f"Amp_Run_{i+1}_dB" for i in range(len(amp_records))]
        df["Amp_Mean_dB"] = df[amp_cols].mean(axis=1).round(2)
        # Pandas std 默认 ddof=1，为各轮 dB 读数的样本标准差，非独立取放重复性。
        df["Amp_Std_dB"] = df[amp_cols].std(axis=1).round(2)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Sweep_Data")
        ws = writer.sheets["Sweep_Data"]
        for col in ws.columns:
            max_len = max(len(str(cell.value)) for cell in col if cell.value is not None)
            ws.column_dimensions[col[0].column_letter].width = max_len + 2

    print(f"多次扫频数据已保存至：{path}")
    return path


# 在 GUI 中阻塞展示已记录曲线；外部包装负责保证查看图时 RF 已停止。
def plot_sweep_summary(result):
    """绘制多次扫频曲线。"""
    freq_list, amp_records = result["freq_list"], result["all_amp_records"]
    if not amp_records:
        print("未记录到数据。")
        return

    fig, ax = plt.subplots(figsize=(10, 6))
    for i, rec in enumerate(amp_records):
        if rec:
            ax.plot(freq_list[:len(rec)] / 1e9, rec, marker="o", label=f"Run {i+1}")
    ax.set(xlabel="Carrier Frequency (GHz)", ylabel="Magnitude (dB)",
           title="Frequency Response: Multi-run Sweep")
    ax.grid(True)
    ax.legend()
    plt.show(block=True)


# 逐载波打印各轮读数；不足一轮时显示 NaN，不伪造缺失测量。
def print_sweep_summary(result):
    """终端打印多次扫频幅值汇总。"""
    freq_ghz = result["freq_list"] / 1e9
    amp_records = result["all_amp_records"]
    header = "载波(GHz)" + "".join(f"    Run{i+1}_Amp(dB)" for i in range(len(amp_records)))
    print("\n多次扫频幅值汇总:")
    print(header)
    for i, f in enumerate(freq_ghz):
        row = f"{f:8.3f}"
        for rec in amp_records:
            row += f"        {rec[i]:8.2f}" if i < len(rec) else "        NaN"
        print(row)
