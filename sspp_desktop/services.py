"""桌面服务边界：复用采集控制，将实时事件保存在有界内存中。"""

from collections import Counter, deque
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import csv
import math
import os
from pathlib import Path

from sspp_lab.controller import LabController, LabError, read_json, write_json
from .config import Settings
from .device_environment import DeviceEnvironmentMixin
from .research import FingerResearchMixin
from .bundle import BundleExportMixin
from .comparison_service import ComparisonMixin
from .liquid_temperature_service import LiquidTemperatureMixin
from .serial_archive import SerialArchiveMixin
from .publication_analysis import PublicationAnalysisMixin


class DesktopService(PublicationAnalysisMixin, SerialArchiveMixin, DeviceEnvironmentMixin, LiquidTemperatureMixin, FingerResearchMixin, BundleExportMixin, ComparisonMixin, LabController):
    def __init__(self, settings=None):
        self.settings = settings or Settings.from_env()
        super().__init__(self.settings.root)
        self._initialize_environment_state()
        self._reset_realtime()

    def _reset_realtime(self):
        self._research_reset()
        self.live_points = deque(maxlen=self.settings.max_display_points)
        self.temperature_rounds=[]
        self.frequency_ranking=[]
        self.temperature_points = deque(maxlen=self.settings.max_display_points)
        self.preview = None
        self.revision = 0
        self.trace_seq = 0
        self.display_gaps = 0

    def child_env(self):
        result = dict(os.environ)
        result.update({
            "MPLBACKEND": "Agg",
            "SSPP_TRACE_INTERVAL_MS": str(self.settings.trace_interval_ms),
            "SSPP_PREVIEW_INTERVAL_MS": str(self.settings.preview_interval_ms),
        })
        return result

    def refresh_profiles(self):
        candidates = [
            ("own", "自有 B210", self.settings.own_profile),
            ("teacher", "老师 B210", self.settings.teacher_profile),
        ]
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return [{"id": key, "name": item[0]} for key, item in self.profiles.items()]
            self.profiles = {}
            self.redactions = []
            result = []
            for ident, name, location in candidates:
                path = Path(location)
                if not path.is_absolute():
                    path = self.root / path
                value = read_json(path)
                if not isinstance(value, dict) or not all(
                    key in value for key in ("serial", "fpga", "fpga_sha256", "channel")
                ):
                    continue
                self.profiles[ident] = (name, path)
                self.redactions.append(str(value["serial"]))
                result.append({"id": ident, "name": name, "channel": value["channel"]})
            return result

    @staticmethod
    def _temperature_target(request):
        value = request.get("temperature_sweep") if isinstance(request, dict) else None
        if value is None:
            return None
        from sspp_experiments.temperature_sweep.metadata import validate_config
        try:
            value = validate_config(value)
        except (TypeError, ValueError) as exc:
            raise LabError(str(exc)) from exc
        if value["mode"]=="manual":return None
        return {"kind": "serial", "port": value["port"], "protocol": value["protocol"]}

    def _require_temperature_environment(self, request):
        target = self._temperature_target(request)
        if target is not None and not self.environment_matches(target):
            raise LabError("请先初始化当前温度串口；SSPP 温度联测要求 USRP 和温度串口同时就绪", 409)

    def start(self, request):
        self._require_temperature_environment(request)
        return super().start(request)

    def command(self, cfg, profile, mode="acquire"):
        if mode == "acquire":
            self._require_temperature_environment(cfg)
        return super().command(cfg, profile, mode)

    def temperature_history(self, key):
        """离线读取温度实收序列；主线程只接收抽样后的有限显示数据。"""
        run = self.run_path(key)
        context = read_json(run / "desktop_context.json", {}) or {}
        if context.get("experiment") != "temperature_sweep":
            raise LabError("请选择 SSPP 温度联测记录")
        analysis=read_json(run/'temperature_intervals.json',{}) or {}
        interval_data={'temperature_rounds':analysis.get('rounds',[]),'frequency_ranking':analysis.get('ranking',[])}
        path = run / "temperature" / "samples.csv"
        if path.is_symlink() or path.parent.is_symlink():
            raise LabError("温度记录不能是符号链接")
        if not path.is_file():
            return {**interval_data, "temperature_points": [], "temperature_latest": None, "historical": True, "stored_rows": 0, "message": "此记录未保存有效温度样本"}
        with path.open(encoding="utf-8-sig", newline="") as source:
            counts = Counter(row.get("channel") for row in csv.DictReader(source))
        count = sum(counts.values())
        per_channel = max(1, self.settings.max_display_points // max(1, len(counts)))
        steps = {channel: max(1, math.ceil(size / per_channel)) for channel, size in counts.items()}
        step = max(steps.values(), default=1)
        config = context.get("parameters", {}).get("temperature_sweep") or {}
        from sspp_experiments.temperature_sweep.metadata import validate_config, calibrated_temperature
        try:
            config = validate_config(config)
        except (TypeError, ValueError) as exc:
            raise LabError("历史温度配置无效，不能重现换算：" + str(exc)) from exc
        rows, last, indices = [], {}, Counter()
        with path.open(encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                for field in ("host_elapsed_s", "voltage_v", "temperature_c", "adc", "mcu_time_ms", "time_segment"):
                    value = row.get(field)
                    row[field] = float(value) if value not in (None, "") else None
                row["temperature_c"], row["calibration_status"] = calibrated_temperature(
                    row["voltage_v"], config["calibration"] if row.get("channel") == config["channel"] else None)
                channel = row.get("channel")
                last[channel] = row
                if indices[channel] % steps[channel] == 0:
                    rows.append(row)
                indices[channel] += 1
        for row in last.values():
            if row not in rows:
                rows.append(row)
        rows.sort(key=lambda row: (row["host_elapsed_s"], row.get("channel", "")))
        return {**interval_data, "temperature_points": rows, "temperature_latest": rows[-1] if rows else None,
                "historical": True, "stored_rows": count, "display_decimation": step,
                "temperature_sweep": context.get("parameters", {}).get("temperature_sweep"),
                "message": "历史实收电压；主机接收时间对齐，不代表硬件同步。"}

    def _handle_event(self, event, job):
        """由收集线程调用；不接触 Qt，不读取正在增长的数据文件。"""
        kind = event.get("event")
        liquid = bool(job.get("config", {}).get("liquid_temperature"))
        if kind == 'TEMP_ROUND':
            self.temperature_rounds.append(dict(event));self.revision+=1;return
        if kind == 'TEMP_ANALYSIS':
            self.frequency_ranking=list(event.get('ranking',[]));self.revision+=1;return
        if kind == "TEMP_SAMPLE":
            if not job.get("config", {}).get("temperature_sweep"):
                return
            if all(isinstance(event.get(field), (int, float)) and not isinstance(event[field], bool)
                   and math.isfinite(event[field]) for field in ("host_elapsed_s", "voltage_v")):
                self.temperature_points.append(dict(event))
                self.revision += 1
            return
        if kind != "TRACE" and not liquid:
            self._research_event(event, job)
        if kind == "TRACE":
            seq = int(event.get("seq", self.trace_seq + 1))
            if seq <= self.trace_seq:
                return
            if not liquid:
                self._research_event(event, job)
            points = event.get("points", [])
            if points and seq != self.trace_seq + 1:
                self.display_gaps += seq - self.trace_seq - 1
                self.live_points.append({
                    "x": points[0]["x"], "amplitude": None, "phase": None,
                    "label": "显示数据间断", "gap": True,
                })
            for point in points:
                x = point.get("x")
                if not isinstance(x, (int, float)) or not math.isfinite(x):
                    continue
                if any(value is not None and not math.isfinite(value)
                       for value in (point.get("amplitude"), point.get("phase"))):
                    continue
                self.live_points.append(dict(point))
            self.trace_seq = seq
            if points:
                last = points[-1]
                job["live"] = {
                    "t_s": last["x"], "amplitude_dB": last.get("amplitude"),
                    "phase_deg": last.get("phase"), "label": last.get("label", ""),
                }
            self.revision += 1
        elif kind == "PREVIEW":
            self.preview = dict(event)
            self.revision += 1
        elif kind == "MEASUREMENT":
            self.revision += 1

    def live_snapshot(self):
        with self.lock:
            mode = self.job["config"]["mode"] if self.job else None
            points = list(self.points) if mode == "sweep" else list(self.live_points)
            return {
                "mode": mode, "points": points, "preview": self.preview,
                "revision": self.revision, "job": deepcopy(self.job),
                "display_gaps": self.display_gaps,
                "temperature_rounds":deepcopy(self.temperature_rounds), "frequency_ranking":deepcopy(self.frequency_ranking),
                "temperature_points": list(self.temperature_points),
                "temperature_latest": dict(self.temperature_points[-1]) if self.temperature_points else None,
            }

    def _save_context(self, job):
        if not job.get("output"):
            return
        output = self.root / job["output"]
        if not output.is_dir():
            return
        liquid = job["config"].get("liquid_temperature")
        temperature = job["config"].get("temperature_sweep")
        if not liquid:
            self._save_finger_context(job, output)
        names = [
            "sspp_lab/controller.py", "sspp_lab/realtime.py",
            "sspp_desktop/services.py", "sspp_desktop/window.py", "sspp_desktop/plots.py",
            "sspp_desktop/research.py",
            "sspp_experiments/temperature_sweep/acquisition.py",
            "sspp_experiments/temperature_sweep/metadata.py",
        ]
        write_json(output / "desktop_context.json", {
            "schema_version": 2, "source": "local_desktop_manual_experiment",
            "experiment": "temperature_sweep" if temperature else ("liquid_temperature_" + job["config"]["mode"]) if liquid else ("material_sweep" if job["config"]["mode"] == "sweep" else "finger_displacement"),
            "job_id": job["id"], "condition": job["config"]["condition"],
            "sensor": job["config"]["sensor"], "parameters": job["config"],
            "initial_setup_user_confirmed": job["config"]["setup_confirmed"],
            "manual_events": self.events, "desktop_status": job["status"],
            "stage_timestamps_are_not_measured_motion": True,
            "rf_stopped_confirmed": job.get("rf_stopped", False),
            "conda_environment": self.settings.conda_env,
            "display_gaps": self.display_gaps,
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "source_hashes": {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                              for name in names if (self.root / name).is_file()},
        })
