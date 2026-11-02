"""扫频伴随串口采集。共享主机单调时钟的软件关联，不宣称硬件同步。"""

import csv
import hashlib
import json
from pathlib import Path
import threading
import time
from collections import deque
from .intervals import summarize_round, save_analysis

from sspp_experiments.temperature_pressure.acquisition import CaptureConfig, SerialCaptureSession
from sspp_experiments.temperature_pressure.serial_io import PosixSerialReader
from .metadata import SCHEMA, calibrated_temperature, validate_config

POINT_FIELDS = ["repeat", "point", "carrier_hz", "tone_hz", "amplitude_dB",
                "rf_point_started_monotonic_s", "rf_measurement_monotonic_s",
                "rf_algorithm_version", "rf_window_start", "rf_window_count",
                "rf_window_duration_s", "rf_raw_iq_file",
                "channel", "sample_index", "mcu_time_ms", "time_segment",
                "host_time_utc", "temperature_sample_monotonic_s", "temperature_sample_age_s",
                "adc", "voltage_v", "temperature_c", "calibration_status",
                "calibration_id", "condition_label", "reference_temperature_c", "measurement_status", "invalid_reason"]
BAD_EVENTS = ("malformed_line", "invalid_encoding", "line_too_long", "firmware_error", "interrupted_line", "mcu_reset", "duplicate_mcu_time")


class NestedSerialCapture(SerialCaptureSession):
    """复用已验证的串口保存与关闭逻辑，只把输出放进本次扫频目录。"""

    def start(self, config):
        config.validate()
        with self.lock:
            if self.thread is not None:
                raise RuntimeError("本次联合采集已启动")
            self.run_path = self.root / "temperature"
            self.run_key = "temperature"
            self.status = "initializing"
            self.started_monotonic = time.monotonic()
            self.thread = threading.Thread(target=self._run, args=(config,),
                                           name="temperature-sweep-serial", daemon=True)
            self.thread.start()
        return self.run_key


class TemperatureSweepCapture:
    """先取得递增温度样本才允许 RF 开始；失联/停更联动同一个 RF stop。"""

    def __init__(self, output, config, stop, failures, *, transport_factory=PosixSerialReader,
                 emit=None, readiness_timeout_s=10.0, startup_settle_s=3.0):
        self.output = Path(output)
        self.config = validate_config(config)
        self.stop = stop
        self.failures = failures
        self.emit = emit or (lambda value: print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True))
        self.session = NestedSerialCapture(output, transport_factory,
                                           first_sample_timeout_s=readiness_timeout_s)
        self.readiness_timeout_s = readiness_timeout_s
        self.startup_settle_s = startup_settle_s
        self.baseline_event_counts = {}
        self.monitor_stop = threading.Event()
        self.monitor = None
        self.ready = False
        self.point_file = None
        self.writer = None
        self.point_count = 0
        self.failure = None
        self.last_emitted = 0
        self.interval_lock = threading.RLock()
        self.interval_samples = deque(maxlen=200000)
        self.last_collected = 0
        self.interval_lost = False
        self.current_round = None
        self.rounds = []

    def _collect_interval_samples(self):
        with self.interval_lock:
            snapshot = self.session.snapshot()
            if self.last_collected and snapshot['points'] and snapshot['points'][0]['sample_index']>self.last_collected+2:
                self.interval_lost=True
            for row in snapshot['points']:
                if row['sample_index']<=self.last_collected:continue
                if row['channel']==self.config['channel']:
                    if len(self.interval_samples)==self.interval_samples.maxlen:self.interval_lost=True
                    self.interval_samples.append((self.session.started_monotonic+row['host_elapsed_s'],row['voltage_v'],row['time_segment']))
                self.last_collected=row['sample_index']

    def begin_round(self, repeat):
        if self.config['mode']!='continuous':return
        with self.interval_lock:
            self._collect_interval_samples()
            self.interval_samples.clear();self.interval_lost=False
            self.current_round=(repeat,time.monotonic())

    def finish_round(self, repeat, complete, invalid_point_count=0):
        if self.config['mode']!='continuous':return
        with self.interval_lock:
            if self.current_round is None:return
            end=time.monotonic();self._collect_interval_samples()
            index,start=self.current_round
            summary=summarize_round(index,start,end,list(self.interval_samples),complete,self.config['calibration'],self.interval_lost)
            summary['invalid_point_count']=invalid_point_count
            if invalid_point_count:summary['quality_flags'].append('invalid_frequency_points')
            self.rounds.append(summary);self.current_round=None
            self.emit({'event':'TEMP_ROUND',**summary})

    def _fail(self, message):
        if self.failure is None:
            self.failure = "温度联合采集: " + message
            self.failures.append(self.failure)
        self.stop.set()

    def _selected(self, snapshot):
        return [row for row in snapshot["points"] if row["channel"] == self.config["channel"]]

    def _check(self, snapshot, *, require_fresh):
        if snapshot["errors"]:
            raise RuntimeError("; ".join(snapshot["errors"]))
        keys = BAD_EVENTS if self.ready else tuple(k for k in BAD_EVENTS if k not in ("mcu_reset", "duplicate_mcu_time"))
        if any(snapshot["event_counts"].get(key, 0) > self.baseline_event_counts.get(key, 0) for key in keys):
            raise RuntimeError("温度串口出现错误行或 MCU 重启，原始数据已保留")
        if not snapshot["running"]:
            raise RuntimeError("温度串口采集已退出")
        rows = self._selected(snapshot)
        if require_fresh:
            if not rows:
                raise RuntimeError("尚无所选温度通道的数据")
            age = time.monotonic() - self.session.started_monotonic - rows[-1]["host_elapsed_s"]
            if age > self.config["freshness_s"]:
                raise TimeoutError(f"温度通道停更 {age:.2f} 秒，停止联合扫频")
        return rows

    def start(self):
        config = self.config
        self.point_file = (self.output / "temperature_measurements.csv").open("w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.point_file, POINT_FIELDS)
        self.writer.writeheader()
        self.point_file.flush()
        self.session.start(CaptureConfig(config["port"], config["protocol"], config["condition_label"],
                                        channel=config["channel"], baudrate=config["baudrate"],
                                        preparation=config["preparation"]))
        deadline = time.monotonic() + self.readiness_timeout_s
        self.emit({"event": "TEMPERATURE_WAITING", "message": "等待温度通道递增样本，再启动 SSPP 扫频"})
        while not self.stop.is_set():
            snapshot = self.session.snapshot()
            rows = self._check(snapshot, require_fresh=False)
            # 重复的同一 MCU 时间不能作为就绪证据；两次真实递增读数才放行。
            if (time.monotonic() - self.session.started_monotonic >= self.startup_settle_s
                    and len(rows) >= 2 and rows[-1]["time_segment"] == rows[-2]["time_segment"]
                    and rows[-1]["mcu_elapsed_s"] > rows[-2]["mcu_elapsed_s"]):
                self._check(snapshot, require_fresh=True)
                self.baseline_event_counts = dict(snapshot["event_counts"])
                self.ready = True
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("未在时限内取得温度通道的递增电压样本；未启动射频")
            self.stop.wait(.02)
        if not self.ready:
            raise RuntimeError("温度通道就绪前已请求停止；未启动射频")
        self.emit({"event": "TEMPERATURE_READY", "channel": config["channel"]})
        self.monitor = threading.Thread(target=self._monitor, name="temperature-sweep-monitor", daemon=True)
        self.monitor.start()

    def _monitor(self):
        try:
            while not self.monitor_stop.is_set():
                snapshot = self.session.snapshot()
                self._check(snapshot, require_fresh=True)
                if self.config["mode"]=="continuous":self._collect_interval_samples()
                for row in snapshot["points"]:
                    if row["sample_index"] <= self.last_emitted:
                        continue
                    temperature, status = calibrated_temperature(row["voltage_v"], self.config["calibration"]
                                                                if row["channel"] == self.config["channel"] else None)
                    self.emit({**row, "sample_event": row["event"], "event": "TEMP_SAMPLE",
                               "temperature_c": temperature, "calibration_status": status})
                    self.last_emitted = row["sample_index"]
                self.monitor_stop.wait(.1)
        except Exception as exc:
            self._fail(f"{type(exc).__name__}: {exc}")

    def ensure_fresh(self):
        """设备配置可能耗时；发起 RF 流前再次检查温度采集仍可用。"""
        if not self.ready:
            raise RuntimeError("温度通道尚未就绪")
        self._check(self.session.snapshot(), require_fresh=True)

    def record_point(self, *, repeat, point, carrier_hz, tone_hz, amplitude_dB,
                     point_started_monotonic_s=None, measurement_quality=None, measurement_status="valid", invalid_reason=""):
        # 与 RF 估计回调关联最近已收到的实测电压，不向未来找样本、不插值。
        snapshot = self.session.snapshot()
        rows = self._check(snapshot, require_fresh=True)
        row = rows[-1]
        measured_at = time.monotonic()
        received_at = self.session.started_monotonic + row["host_elapsed_s"]
        if measured_at - received_at > self.config["freshness_s"]:
            raise TimeoutError("温度样本已过期，未关联到射频频点")
        temperature, status = calibrated_temperature(row["voltage_v"], self.config["calibration"])
        calibration = self.config["calibration"]
        quality = measurement_quality or {}
        record = {"repeat": repeat, "point": point, "carrier_hz": float(carrier_hz),
                  "tone_hz": float(tone_hz), "amplitude_dB": float(amplitude_dB) if amplitude_dB is not None else None,
                  "measurement_status":measurement_status,"invalid_reason":invalid_reason,
                  "rf_point_started_monotonic_s": point_started_monotonic_s,
                  "rf_measurement_monotonic_s": measured_at,
                  "rf_algorithm_version": quality.get("algorithm_version"),
                  "rf_window_start": quality.get("window_start"),
                  "rf_window_count": quality.get("window_count"),
                  "rf_window_duration_s": quality.get("window_duration_s"),
                  "rf_raw_iq_file": quality.get("raw_iq_file"),
                  **{key: row[key] for key in ("channel", "sample_index", "mcu_time_ms", "time_segment",
                                             "host_time_utc", "adc", "voltage_v")},
                  "temperature_sample_monotonic_s": received_at,
                  "temperature_sample_age_s": measured_at - received_at,
                  "temperature_c": temperature, "calibration_status": status,
                  "calibration_id": calibration["id"] if calibration else "",
                  "condition_label": self.config["condition_label"],
                  "reference_temperature_c": self.config["reference_temperature_c"]}
        self.writer.writerow(record)
        self.point_file.flush()
        self.point_count += 1
        return record

    def close(self):
        if self.current_round is not None:self.finish_round(self.current_round[0],False)
        self.monitor_stop.set()
        if self.monitor:
            self.monitor.join(2)
            if self.monitor.is_alive():
                self._fail("温度监测线程未按时结束")
        if self.session.thread is not None and not self.session.stop(3):
            self._fail("温度采集线程未按时结束")
        if self.point_file:
            try:
                self.point_file.flush()
                self.point_file.close()
            except OSError as exc:
                self._fail("逐点温度表保存失败: " + str(exc))
        if self.config['mode']=='continuous' and self.point_file:
            self.analysis=save_analysis(self.output,self.rounds,self.config['calibration'])
            self.emit({'event':'TEMP_ANALYSIS','ranking':self.analysis['ranking']})
        snapshot = self.session.snapshot()
        if snapshot["errors"]:
            self._fail("; ".join(snapshot["errors"]))
        manifest_path = self.output / "temperature" / "manifest.json"
        serial_manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        stopped = not self.session.is_running and (not self.monitor or not self.monitor.is_alive())
        valid = self.ready and stopped and serial_manifest.get("status") == "complete" and not self.failure
        if not valid and not self.failure:
            self._fail("温度数据未完整封存或就绪未完成")
        files = []
        for name in ("temperature_measurements.csv", "temperature/manifest.json",
                     "temperature/raw_serial.bin", "temperature/samples.csv", "temperature/serial_events.jsonl",
                     "temperature_intervals.json", "temperature_rounds.csv", "frequency_sensitivity.csv"):
            path = self.output / name
            if path.is_file():
                files.append({"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                              "bytes": path.stat().st_size})
        return {"schema": SCHEMA, "config": self.config, "ready": self.ready,
                "status": "complete" if valid else "failed_or_incomplete",
                "serial_workers_stopped": stopped,
                "serial_closed": bool(serial_manifest.get("serial_closed")),
                "data_files_closed": bool(serial_manifest.get("data_files_closed")) and bool(self.point_file and self.point_file.closed),
                "round_count":len(self.rounds), "point_count": self.point_count, "serial_rows": snapshot["rows"],
                "serial_started_monotonic_s": self.session.started_monotonic,
                "startup_event_counts": self.baseline_event_counts,
                "files": files, "errors": [self.failure] if self.failure else [],
                "alignment": "latest received selected-channel sample at RF estimator callback; host monotonic clock; no interpolation",
                "limitations": ["Software time association only, no hardware trigger or shared MCU/USRP clock",
                                "RF callback time includes queue and DSP delay; voltage time is byte receipt, not sensor sample time",
                                "Paired voltage is one latest received sample, not an average over the recorded adaptive RF window",
                                "S6/S7 firmware readings are sequential; both raw channels retained",
                                "Nominal firmware voltage is not calibrated temperature; reference temperature is an operator label",
                                "Calibration applies only to the selected channel within its recorded voltage range"]}
