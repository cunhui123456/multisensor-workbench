"""只读历史回放：连续模式使用原始样点时间，扫频明确使用逐点播放时序。"""

from bisect import bisect_right
import csv
import json
import math
from pathlib import Path

import numpy as np

from sspp_lab.controller import LabError, read_json
from sspp_lab.realtime import preview_event


class ReplaySession:
    def __init__(self, run_path, mode):
        self.run_path = Path(run_path)
        self.mode = mode
        self.points = []
        self.times = []
        self.rows = []
        self.raw = None
        self.previews = []
        self._preview = None
        self._preview_bucket = -1
        self.manifest = read_json(self.run_path / "manifest.json", {})
        self.params = self.manifest.get("actual_sdr") or self.manifest.get("requested_sdr") or {}
        self.rate = self.params.get("rx_rate_actual") or self.params.get("rate")
        self.timing_note = "历史回放：按原始 RX 样点时间播放；不连接 USRP"
        if mode == "phase":
            self._load_phase()
        elif mode == "sweep":
            self._load_sweep()
        else:
            raise LabError("此历史记录没有可回放的采集序列；请打开原有文件")
        if not self.points:
            raise LabError("记录没有可回放的数据")
        self.duration_s = self.times[-1]

    def _load_phase(self):
        series = self.run_path / "amplitude_phase.jsonl"
        if series.is_symlink() or not series.is_file():
            raise LabError("记录缺少幅相数据")
        with series.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    t = float(row["t_s"])
                    if not math.isfinite(t) or (self.times and t < self.times[-1]):
                        continue
                    self.rows.append(row)
                    self.times.append(t)
                    self.points.append({
                        "x": t, "amplitude": row.get("relative_amplitude_dB"),
                        "phase": row.get("relative_phase_deg"), "label": row.get("label", ""),
                        "absolute_amplitude_dBFS": row.get("amplitude_dBFS_est"),
                    })
                except (ValueError, KeyError, TypeError):
                    continue
        raw = self.run_path / "rx_iq.cf32"
        if raw.is_file() and not raw.is_symlink() and raw.stat().st_size >= 64 and self.rate:
            self.raw = np.memmap(raw, dtype=np.complex64, mode="r", shape=(raw.stat().st_size // 8,))

    def _load_sweep(self):
        self.timing_note = "历史扫频回放：每点 1 秒演示，原 CSV 未保存逐点时间；不代表实际采集速度"
        table = self.run_path / "measurements.csv"
        if table.is_symlink() or not table.is_file():
            raise LabError("记录缺少扫频数据")
        with table.open() as stream:
            for row in csv.DictReader(stream):
                point = {
                    "carrier_hz": float(row["requested_carrier_GHz"]) * 1e9,
                    "amplitude_dB": None if row.get("measurement_status")=="invalid" else float(row["peak_amplitude_dBFS_est"]),
                    "repeat": int(row["repeat"]), "point": int(row["point"]),
                }
                if not all(math.isfinite(v) for v in point.values() if v is not None):
                    continue
                self.points.append(point)
                self.times.append(float(len(self.points)))
        files = sorted(self.run_path.glob("iq_point_[0-9]*.npz"),
                       key=lambda p: int(p.stem.rsplit("_", 1)[-1]))
        # 在后台加载时准备有界预览；播放期间无需反复解压 NPZ。
        by_number = {}
        for path in files:
            if path.is_symlink():
                continue
            with np.load(path, allow_pickle=False) as data:
                info = json.loads(str(data["point_info"]))
                number = int(info["point_number"])
                iq = data["iq"]
                if len(iq) < 8:
                    continue
                origin = max(0, len(iq) - 2048)
                event = preview_event(
                    iq[origin:], rate=float(data["rx_rate"]),
                    carrier_hz=float(info["requested_carrier_hz"]),
                    tone_hz=float(info["tone_hz"]), t_s=float(number),
                    sample_start=origin, mode="sweep",
                )
                event["source"] = "historical_saved_sweep_point"
                by_number[number] = event
        self.previews = [by_number.get(i + 1) for i in range(len(self.points))]

    def snapshot(self, elapsed_s):
        elapsed_s = max(0.0, min(float(elapsed_s), self.duration_s))
        count = bisect_right(self.times, elapsed_s)
        points = self.points[max(0, count - 20000):count]
        if self.mode == "phase" and count and self.raw is not None:
            bucket = int(elapsed_s * 4)
            if bucket != self._preview_bucket:
                row = self.rows[count - 1]
                end = min(len(self.raw), int(row["start_sample"]) + int(row["count"]))
                start = max(int(row["start_sample"]), end - 2048)
                if end - start >= 8:
                    self._preview = preview_event(
                        self.raw[start:end], rate=self.rate,
                        carrier_hz=self.params.get("freq", 0), tone_hz=self.params.get("tone", 0),
                        t_s=row["t_s"], sample_start=start, mode="phase",
                    )
                    self._preview["source"] = "historical_rx_samples"
                self._preview_bucket = bucket
        elif self.mode == "sweep":
            self._preview = self.previews[count - 1] if count else None
        if not count:
            self._preview = None
        return {
            "mode": self.mode, "points": points, "preview": self._preview,
            "revision": count, "job": None, "replay": True,
            "elapsed_s": elapsed_s, "duration_s": self.duration_s, "timing_note": self.timing_note,
        }
