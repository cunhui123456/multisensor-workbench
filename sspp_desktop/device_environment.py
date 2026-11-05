"""设备环境状态与可取消子进程；构造和状态快照均不访问硬件。"""

from copy import deepcopy
from datetime import datetime, timezone
import codecs
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import threading
import time
import uuid

from sspp_lab.controller import LabError
from .initialize_device import EVENT, PROTOCOL_VERSION

INITIALIZATION_TIMEOUT_S = 30.0
USRP_INITIALIZATION_TIMEOUT_S = 120.0
INITIALIZATION_PROGRESS_INTERVAL_S = 10.0
_ACTIVE_JOB_STATES = {"starting", "running", "stopping", "saving"}


class _InitializationCancelled(Exception):
    pass


def normalize_target(target):
    if not isinstance(target, dict):
        raise LabError("请先选择需要初始化的实验设备")
    if target.get("kind") == "usrp":
        if (set(target) != {"kind", "profile"} or not isinstance(target.get("profile"), str)
                or target["profile"] not in {"own", "teacher"}):
            raise LabError("请选择有效的 B210 设备配置")
    elif target.get("kind") == "serial":
        from sspp_experiments.temperature_pressure.acquisition import CaptureConfig
        if set(target) != {"kind", "port", "protocol"}:
            raise LabError("串口初始化参数无效")
        try:
            CaptureConfig(target["port"], target["protocol"], "初始化检查").validate()
        except (TypeError, ValueError) as exc:
            raise LabError(str(exc)) from exc
    else:
        raise LabError("不支持的实验设备类型")
    return dict(target)


def parse_result(stdout, returncode, request_id, kind):
    """拒绝多条事件、未知字段、重复键、非有限 JSON 和不匹配的请求。"""
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("重复字段")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("非有限 JSON")

    try:
        if not isinstance(stdout, str) or len(stdout) > 16384:
            raise ValueError("结果大小无效")
        event = json.loads(stdout, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
        if not isinstance(event, dict) or set(event) != {
            "event", "version", "request_id", "kind", "status", "message", "released"
        }:
            raise ValueError("事件字段无效")
        if (event["event"] != EVENT or type(event["version"]) is not int
                or event["version"] != PROTOCOL_VERSION or event["request_id"] != request_id
                or event["kind"] != kind or event["released"] is not True
                or event["status"] not in {"ready", "failed"}
                or not isinstance(event["message"], str) or not 1 <= len(event["message"]) <= 2000
                or (event["status"] == "ready") != (returncode == 0)):
            raise ValueError("事件内容无效")
        return event
    except (TypeError, ValueError) as exc:
        raise LabError("设备初始化子进程未返回有效结果，请重新初始化", 503) from exc


class _InitializationLog:
    """每次初始化独立日志；终端和文件只接收脱敏后的文本，无日志队列。"""

    def __init__(self, root, public_log):
        self.public_log = public_log
        self.lock = threading.Lock()
        self.secrets = set()
        self.path = None
        self.handle = None
        self.warning = None
        self.driver_tail = ""
        self.terminal_available = True
        try:
            directory = Path(root) / ".cache" / "initialization_logs"
            directory.mkdir(parents=True, exist_ok=True)
            name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_" + uuid.uuid4().hex[:8] + ".log"
            path = directory / name
            self.handle = path.open("x", encoding="utf-8")
            self.path = str(path.absolute())
        except OSError as exc:
            self.warning = "初始化日志无法保存：" + self.public_log(str(exc))

    def add_redactions(self, secrets):
        with self.lock:
            self.secrets.update(str(secret) for secret in secrets if secret)

    def redact(self, message):
        safe = self.public_log(str(message))
        for secret in sorted(self.secrets, key=len, reverse=True):
            safe = safe.replace(secret, "[设备标识]")
        return safe

    def write(self, message, *, source="主进程"):
        with self.lock:
            safe = self.redact(message).rstrip("\r\n")
            if source == "设备":
                self.driver_tail = (self.driver_tail + safe + "\n")[-4000:]
            stamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            line = f"[{stamp} UTC][初始化/{source}] {safe}\n"
            if self.terminal_available:
                try:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                except (OSError, ValueError, AttributeError):
                    self.terminal_available = False
            if self.handle is not None:
                try:
                    self.handle.write(line)
                    self.handle.flush()
                except (OSError, ValueError) as exc:
                    self.warning = "初始化日志写入失败：" + self.redact(exc)
                    try:
                        self.handle.close()
                    except (OSError, ValueError):
                        pass
                    self.handle = None
                    if self.terminal_available:
                        try:
                            sys.stdout.write(self.warning + "\n")
                            sys.stdout.flush()
                        except (OSError, ValueError, AttributeError):
                            self.terminal_available = False
            return safe

    def close(self):
        with self.lock:
            if self.handle is not None:
                try:
                    self.handle.close()
                except (OSError, ValueError) as exc:
                    self.warning = "初始化日志关闭失败：" + self.redact(exc)
                finally:
                    self.handle = None


def _terminate_initialization_process(proc):
    """不关闭管道，让两个 reader 在终止后继续排空尾部输出。"""
    if proc.poll() is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    else:
        proc.wait(timeout=2)


def _read_initialization_process(proc, *, cancelled, deadline, timeout_s, emit):
    """双管道并行排空；不让 stderr 填满阻塞结果事件，也不拼接截断的成功 JSON。"""
    stdout_parts, stdout_size = [], 0
    stdout_overflow = False
    stderr_tail = ""
    read_errors = []
    stop_readers = threading.Event()
    eof = {"stdout": False, "stderr": False}
    threads = []
    reason = None
    started = deadline - timeout_s
    next_progress = started + INITIALIZATION_PROGRESS_INTERVAL_S

    def driver_line(line):
        nonlocal stderr_tail
        safe = emit(line, source="设备")
        stderr_tail = (stderr_tail + safe + "\n")[-4000:]

    def read_stream(stream, name):
        nonlocal stdout_size, stdout_overflow
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        discarding = False

        def stderr_chunk(text, final=False):
            nonlocal pending, discarding
            pending += text
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                if not discarding:
                    driver_line(line.rstrip("\r") if len(line) <= 8192 else "[过长驱动日志行已省略]")
                discarding = False
            if len(pending) > 8192:
                if not discarding:
                    driver_line("[过长驱动日志行已省略]")
                pending = ""
                discarding = True
            if final and pending and not discarding:
                driver_line(pending.rstrip("\r"))
                pending = ""

        try:
            with selectors.DefaultSelector() as selector:
                selector.register(stream.fileno(), selectors.EVENT_READ)
                while not stop_readers.is_set():
                    if not selector.select(timeout=0.05):
                        continue
                    chunk = os.read(stream.fileno(), 4096)
                    if not chunk:
                        eof[name] = True
                        break
                    if name == "stdout":
                        stdout_size += len(chunk)
                        if stdout_size > 65536:
                            stdout_overflow = True
                            stdout_parts.clear()
                        elif not stdout_overflow:
                            stdout_parts.append(chunk)
                    else:
                        stderr_chunk(decoder.decode(chunk))
                if name == "stderr":
                    stderr_chunk(decoder.decode(b"", final=True), final=True)
        except Exception as exc:
            read_errors.append(f"{name} 读取失败：{exc}")
        finally:
            stream.close()

    try:
        for name in ("stdout", "stderr"):
            stream = getattr(proc, name)
            if stream is None:
                raise LabError("初始化进程缺少输出管道", 503)
            thread = threading.Thread(target=read_stream, args=(stream, name),
                                      name="sspp-initialize-" + name, daemon=True)
            threads.append(thread)
            thread.start()
        while proc.poll() is None:
            if cancelled():
                reason = "cancelled"
                emit("收到取消请求，正在终止初始化检查并回收设备")
                break
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                reason = "timeout"
                emit(f"设备初始化超过 {timeout_s:g} 秒，正在终止子进程")
                break
            if now >= next_progress:
                emit(f"仍在等待设备驱动（已等待 {now - started:.0f} 秒，最多 {timeout_s:g} 秒）；可取消初始化")
                next_progress = now + INITIALIZATION_PROGRESS_INTERVAL_S
            try:
                proc.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                pass
    finally:
        try:
            _terminate_initialization_process(proc)
        finally:
            # 退出后给尾部错误/析构日志时间排空；异常继承管道也不能留下读取线程。
            drain_deadline = time.monotonic() + 2
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=max(0, drain_deadline - time.monotonic()))
            stop_readers.set()
            for thread in threads:
                if thread.ident is not None and thread.is_alive():
                    thread.join(timeout=1)
            for name in ("stdout", "stderr"):
                stream = getattr(proc, name)
                if stream is not None and not stream.closed:
                    stream.close()
    if read_errors:
        raise LabError("；".join(read_errors), 503)
    if not all(eof.values()):
        raise LabError("初始化输出管道未正常关闭，结果无法确认", 503)
    if stdout_overflow:
        raise LabError("初始化结果超过允许大小，拒绝不完整结果", 503)
    try:
        stdout = b"".join(stdout_parts).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise LabError("初始化结果不是有效 UTF-8，结果无法确认", 503) from exc
    return {"stdout": stdout, "returncode": proc.returncode,
            "reason": reason, "stderr_tail": stderr_tail.rstrip()}


class DeviceEnvironmentMixin:
    def _initialize_environment_state(self):
        self._device_environments = {
            kind: {"state": "idle", "target": None, "message": "尚未初始化设备", "log_path": None}
            for kind in ("usrp", "serial")
        }
        self._environment_binding = None  # 仅 USRP 需要配置文件指纹。
        self._environment_process = None
        self._environment_active = False
        self._environment_current_kind = None
        self._environment_last_log_path = None
        self._environment_cancel = threading.Event()
        self._environment_invalidated = set()
        self._environment_done = threading.Event()
        self._environment_done.set()
        self._environment_starting = False
        self._environment_start_thread = None

    def environment_snapshot(self, kind=None):
        """纯内存快照；单设备状态与实验台汇总状态分别表达。"""
        with self.lock:
            if kind is not None:
                if kind not in self._device_environments:
                    raise LabError("未知设备类型")
                return deepcopy(self._device_environments[kind])
            devices = deepcopy(self._device_environments)
            states = [device["state"] for device in devices.values()]
            ready_count = states.count("ready")
            if self._environment_active:
                state = "initializing"
            elif ready_count == 2:
                state = "ready"
            elif ready_count:
                state = "partial"
            elif "cancelled" in states:
                state = "cancelled"
            elif any(value in {"failed", "skipped"} for value in states):
                state = "unavailable"
            else:
                state = "idle"
            names = {"ready": "可用", "idle": "未初始化", "initializing": "检查中",
                     "failed": "不可用", "cancelled": "已取消", "skipped": "本次未检查"}
            message = "；".join(label + "：" + names.get(devices[key]["state"], "未知")
                                for key, label in (("usrp", "USRP"), ("serial", "Arduino 温压")))
            if self._environment_active and self._environment_cancel.is_set():
                message += "；正在取消剩余检查"
            return {"state": state, "message": message, "devices": devices,
                    "log_path": self._environment_last_log_path}

    def _reserve_environment(self):
        with self.lock:
            if self.closed:
                raise LabError("程序正在关闭", 409)
            if (self.preparing or self.exporting or self._environment_starting or self._environment_active
                    or (self.proc is not None and self.proc.poll() is None)
                    or (self.job and self.job.get("status") in _ACTIVE_JOB_STATES)):
                raise LabError("正在初始化、采集或保存，请完成后再初始化实验台", 409)
            self.preparing = True
            self.prepare_cancelled = False
            self._environment_active = True
            self._environment_cancel.clear()
            self._environment_invalidated.clear()
            self._environment_done.clear()

    def _release_environment(self):
        with self.lock:
            self._environment_active = False
            self._environment_current_kind = None
            self.preparing = False
            self._environment_done.set()

    def _initialization_cancelled(self, cancel_event=None, kind=None):
        with self.lock:
            return (self._environment_cancel.is_set() or self.closed
                    or (cancel_event is not None and cancel_event.is_set())
                    or (kind is not None and kind in self._environment_invalidated))

    def _skip_environment(self, kind, state="skipped", message="本次未配置或未选择检查此设备", target=None):
        with self.lock:
            previous = self._device_environments[kind]
            if previous["state"] == "ready":
                self._device_environments[kind] = {**previous, "message": message + "；保留此前通过的设备检查"}
            else:
                self._device_environments[kind] = {"state": state, "target": target,
                                                   "message": message, "log_path": previous.get("log_path")}
                if kind == "usrp":
                    self._environment_binding = None

    def initialize_workbench(self, targets, *, cancel_event=None):
        """串口与 USRP 独立检查；整个批次持有启动互锁，单方失败不阻止另一方。"""
        if not isinstance(targets, dict) or set(targets) - {"usrp", "serial"}:
            raise LabError("实验台设备配置必须只包含 usrp 和 serial")
        targets = deepcopy(targets)
        self._reserve_environment()
        try:
            for kind in ("serial", "usrp"):
                target = targets.get(kind)
                if self._initialization_cancelled(cancel_event, kind):
                    self._skip_environment(kind, "cancelled", "本次检查已取消，未打开此设备", target)
                    continue
                if target is None:
                    self._skip_environment(kind)
                    continue
                try:
                    target = normalize_target(target)
                    if target["kind"] != kind:
                        raise LabError("设备配置类型与所选设备不一致")
                except (LabError, TypeError) as exc:
                    log = _InitializationLog(self.root, self.public_log)
                    log.add_redactions(self.redactions)
                    message = log.redact(str(exc))
                    log.write(kind + " 配置不可用：" + message)
                    log.close()
                    with self.lock:
                        self._device_environments[kind] = {"state": "failed", "target": None,
                                                           "message": message, "log_path": log.path}
                        self._environment_last_log_path = log.path
                        if kind == "usrp":
                            self._environment_binding = None
                    continue
                self._initialize_device(target, cancel_event=cancel_event)
        finally:
            self._release_environment()
        return self.environment_snapshot()

    def initialize_environment(self, target, *, cancel_event=None):
        """兼容单设备显式检查；只改变所选设备状态，返回单设备快照。"""
        target = normalize_target(target)
        self._reserve_environment()
        try:
            return self._initialize_device(target, cancel_event=cancel_event)
        finally:
            self._release_environment()

    def _profile_binding(self, profile):
        self.refresh_profiles()
        with self.lock:
            entry = self.profiles.get(profile)
        if entry is None:
            raise LabError("设备配置不存在，请检查本机配置文件")
        path = Path(entry[1]).absolute()
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise LabError("设备配置文件无法读取") from exc
        return str(path), digest

    def environment_matches(self, target):
        try:
            target = normalize_target(target)
        except (LabError, TypeError):
            return False
        kind = target["kind"]
        with self.lock:
            device = self._device_environments[kind]
            if device["state"] != "ready" or device["target"] != target:
                return False
            binding = self._environment_binding if kind == "usrp" else None
        if kind == "usrp":
            try:
                if binding is None or hashlib.sha256(Path(binding[0]).read_bytes()).hexdigest() != binding[1]:
                    return False
            except OSError:
                return False
        with self.lock:
            device = self._device_environments[kind]
            return (device["state"] == "ready" and device["target"] == target
                    and (kind != "usrp" or self._environment_binding == binding))

    @staticmethod
    def _reap_initialization(proc):
        _terminate_initialization_process(proc)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def _run_environment_process(self, proc, *, cancelled, started, timeout_s, emit):
        """独立 IO 边界，离线契约测试可注入 runner，无设备替身分支。"""
        return _read_initialization_process(proc, cancelled=cancelled,
                                            deadline=started + timeout_s, timeout_s=timeout_s, emit=emit)

    @staticmethod
    def _assert_no_external_acquisition():
        """只读进程表；与采集入口相同地检查新旧仓库的已知 SSPP 运行器。"""
        try:
            result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                                    text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LabError("无法检查已有采集进程；未初始化设备，请稍后重试", 503) from exc
        if result.returncode != 0:
            raise LabError("采集进程检查失败；未初始化设备", 503)
        for line in result.stdout.splitlines():
            if any(name in line for name in ("sspp-usrp", "multisensor-workbench")) and any(entry in line for entry in (
                    "run_local.py acquire", "run_phase.py acquire", "run_teacher.py", "sspp_lab.acquire_")):
                raise LabError("发现另一个 SSPP／多传感器采集进程，请先结束它再初始化实验台", 409)

    def _initialize_device(self, target, *, cancel_event=None):
        """已取得批次互锁后检查一种设备；不能在这里清除批次取消请求。"""
        kind = target["kind"]
        timeout_s = USRP_INITIALIZATION_TIMEOUT_S if kind == "usrp" else INITIALIZATION_TIMEOUT_S
        with self.lock:
            previous = deepcopy(self._device_environments[kind])
            previous_binding = self._environment_binding if kind == "usrp" else None
            if kind == "usrp":
                self._environment_binding = None
            self._environment_current_kind = kind
            self._device_environments[kind] = {"state": "initializing", "target": target,
                                               "message": f"正在初始化检查；最长 {timeout_s:g} 秒", "log_path": None}

        def cancelled():
            return self._initialization_cancelled(cancel_event, kind)

        proc, binding, diagnostic = None, None, None
        state, message = "failed", "设备初始化检查失败"
        log = _InitializationLog(self.root, self.public_log)
        log.add_redactions(self.redactions)
        if kind == "serial":
            log.add_redactions([target["port"]])
        with self.lock:
            self._device_environments[kind]["log_path"] = log.path
            self._environment_last_log_path = log.path
        log.write("开始初始化设备：" + ("USRP" if kind == "usrp" else "Arduino 温压"))
        if log.warning:
            log.write(log.warning)
        try:
            if cancelled():
                raise _InitializationCancelled()
            request_id = uuid.uuid4().hex
            args = [sys.executable, "-B", "-u", "-m", "sspp_desktop.initialize_device",
                    "--kind", target["kind"], "--request-id", request_id]
            if target["kind"] == "usrp":
                log.write("所选设备：" + {"own": "自有 B210", "teacher": "老师 B210"}[target["profile"]])
                log.write(f"B210 冷启动可能加载固件并重新枚举 USB；检查时限 {timeout_s:g} 秒，可随时取消")
                log.write("正在读取并核对本机设备配置")
                binding = self._profile_binding(target["profile"])
                log.add_redactions(self.redactions)
                log.write("设备配置已读取；FPGA 文件与设备通道将在子进程中验证")
                args += ["--profile-path", binding[0], "--profile-sha256", binding[1]]
                if cancelled():
                    raise _InitializationCancelled()
                log.write("正在检查是否已有 SSPP 采集进程")
                self._assert_no_external_acquisition()
                log.write("采集进程检查通过")
            else:
                log.write("所选设备：温压串口；协议 " + target["protocol"])
                args += ["--port", target["port"], "--protocol", target["protocol"]]
            if cancelled():
                raise _InitializationCancelled()
            log.write("正在启动设备初始化子进程；驱动信息将实时显示")
            started = time.monotonic()
            proc = subprocess.Popen(args, cwd=self.root, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True, env=self.child_env())
            with self.lock:
                self._environment_process = proc
            diagnostic = self._run_environment_process(proc, cancelled=cancelled, started=started,
                                                       timeout_s=timeout_s, emit=log.write)
            if diagnostic["reason"] == "cancelled":
                state, message = "cancelled", "初始化已取消，未开始采集"
            elif diagnostic["reason"] == "timeout":
                state, message = "failed", f"设备初始化超过 {timeout_s:g} 秒，已结束检查；请检查设备连接、供电和全局设备选择后重试"
            else:
                event = parse_result(diagnostic["stdout"], diagnostic["returncode"], request_id, target["kind"])
                state, message = event["status"], event["message"]
                if state == "ready" and target["kind"] == "usrp" and self._profile_binding(target["profile"]) != binding:
                    state, message = "failed", "设备配置在初始化期间改变，请重新初始化"
        except _InitializationCancelled:
            state, message = "cancelled", "初始化已取消，未开始采集"
            log.write("收到取消请求；未启动或已结束设备检查")
        except Exception as exc:
            state, message = "failed", log.redact(str(exc)) or "设备初始化检查失败"
            log.write("初始化异常：" + message)
        finally:
            try:
                if proc is not None:
                    self._reap_initialization(proc)
            except Exception as exc:
                state, message = "failed", "初始化子进程清理失败：" + log.redact(exc)
                log.write(message)
            if cancelled():
                if state != "cancelled":
                    log.write("已收到取消请求，忽略晚到的初始化结果")
                state, message = "cancelled", "初始化已取消，未开始采集"
            driver_tail = (diagnostic or {}).get("stderr_tail") or log.driver_tail
            if state == "failed" and driver_tail:
                tail = log.redact(driver_tail).strip()
                log.write("失败前驱动输出摘要：" + tail[-1200:].replace("\n", " | "))
                # 短界面提示优先显示真正停住的最后一行；完整过程仍在终端和文件。
                message += "；驱动最后输出：" + tail.splitlines()[-1][:400]
            code = proc.returncode if proc is not None else "未启动"
            log.write(f"检查完成：{state}；子进程退出码={code}；{message}")
            log.close()
            if log.warning:
                message += "；诊断日志未完整保存，请查看终端"
            with self.lock:
                if (state == "cancelled" and proc is None and previous["state"] == "ready"
                        and previous["target"] == target and kind not in self._environment_invalidated):
                    self._device_environments[kind] = {**previous, "message": "本次检查已取消，设备未打开；保留此前检查结果"}
                    if kind == "usrp":
                        self._environment_binding = previous_binding
                else:
                    self._device_environments[kind] = {"state": state, "target": target,
                                                       "message": log.redact(message), "log_path": log.path}
                    if kind == "usrp":
                        self._environment_binding = binding if state == "ready" else None
                self._environment_process = None
                self._environment_current_kind = None
        return self.environment_snapshot(kind)

    def cancel_environment_initialization(self):
        with self.lock:
            if self._environment_active:
                self._environment_cancel.set()
                if self._environment_current_kind is not None:
                    self._device_environments[self._environment_current_kind]["message"] = "正在取消初始化并释放设备"
        return self.environment_snapshot()

    def invalidate_environment(self, kind=None):
        kinds = ("usrp", "serial") if kind is None else (kind,)
        if any(device not in self._device_environments for device in kinds):
            raise LabError("未知设备类型")
        with self.lock:
            for device in kinds:
                previous = self._device_environments[device]
                if self._environment_active:
                    self._environment_invalidated.add(device)
                if device == self._environment_current_kind:
                    previous["message"] = "配置已改变，正在取消此设备检查"
                else:
                    self._device_environments[device] = {"state": "idle", "target": None,
                                                        "message": "请重新初始化此设备", "log_path": previous.get("log_path")}
                if device == "usrp":
                    self._environment_binding = None
            if kind is None and self._environment_active:
                self._environment_cancel.set()
        return self.environment_snapshot(kind)

    def preflight(self, request):
        with self.lock:
            if self._environment_active:
                raise LabError("初始化检查尚未结束", 409)
            standalone = self._environment_start_thread != threading.get_ident()
            if standalone:
                if self.closed or self.preparing or self._environment_starting:
                    raise LabError("程序正在关闭或已有检查正在进行", 409)
                self.preparing = True
        try:
            return super().preflight(request)
        finally:
            if standalone:
                with self.lock:
                    self.preparing = False

    def start(self, request):
        target = {"kind": "usrp", "profile": request.get("profile")} if isinstance(request, dict) else None
        with self.lock:
            if self._environment_starting or self.preparing:
                raise LabError("已有初始化或启动检查正在进行", 409)
            if not self.environment_matches(target):
                raise LabError("请先初始化当前设备环境；配置改变后需要重新初始化", 409)
            if (self.closed or self.exporting or (self.proc is not None and self.proc.poll() is None)
                    or (self.job and self.job.get("status") in _ACTIVE_JOB_STATES)):
                raise LabError("程序正在关闭或已有实验正在运行或保存", 409)
            self._environment_starting = True
            self._environment_start_thread = threading.get_ident()
            previous_proc, previous_job = self.proc, self.job
            # Global initialization is the desktop readiness gate. The explicit
            # Start click uses that verified state; it does not invent a second
            # manual wiring confirmation or mutate the caller's draft.
            request = {**request, "setup_confirmed": True,
                       "setup_confirmation_source": "global_initialization"}
        try:
            return super().start(request)
        except Exception as exc:
            with self.lock:
                message = self.public_log(str(exc))
                launched = self.proc if self.proc is not previous_proc else None
                job = self.job if self.job is not previous_job else None
                if job is not None:
                    job["errors"] = [self.public_log(str(error)) for error in job.get("errors", [])]
                    if message not in job["errors"]:
                        job["errors"].append(message)
                    if launched is not None and launched.poll() is None:
                        job["status"] = "stopping"
                        job["stop_cleanup_started"] = True
                    elif job.get("status") == "starting":
                        job["status"] = "failed"
                self._environment_binding = None
                self._device_environments["usrp"] = {"state": "failed", "target": target,
                                                     "message": "采集启动失败，请重新初始化 USRP：" + message,
                                                     "log_path": self._device_environments["usrp"].get("log_path")}
            # Popen 成功而收集/看门狗线程启动失败，也必须收回本次新进程。
            # 继续使用原停止流程和 manifest 验收，进程退出不代表 RF 已停。
            if launched is not None and job is not None:
                try:
                    if launched.poll() is None:
                        self._end_process(launched, job)
                    collector = self.collector_thread
                    if collector is not None and collector.ident is not None:
                        if collector.is_alive():
                            collector.join(timeout=5)
                    elif launched.poll() is not None:
                        self._collect(launched, job)
                except Exception as cleanup_error:
                    with self.lock:
                        job["status"] = "stop_unconfirmed"
                        job["errors"].append("启动失败后的清理未确认：" + self.public_log(str(cleanup_error)))
            raise
        finally:
            with self.lock:
                self._environment_starting = False
                self._environment_start_thread = None

    def command(self, cfg, profile, mode="acquire"):
        if mode == "acquire":
            target = {"kind": "usrp", "profile": cfg.get("profile")}
            if (not self.environment_matches(target) or self._environment_binding is None
                    or str(Path(profile).absolute()) != self._environment_binding[0]):
                raise LabError("设备配置已改变或初始化已失效，请重新初始化", 409)
        return super().command(cfg, profile, mode)

    def _collect(self, proc, job):
        try:
            return super()._collect(proc, job)
        finally:
            with self.lock:
                if job.get("status") in {"failed", "initialization_failed", "stop_unconfirmed"}:
                    self._environment_binding = None
                    previous = self._device_environments["usrp"]
                    self._device_environments["usrp"] = {"state": "failed", "target": previous["target"],
                                                         "message": job.get("failure_summary") or "射频采集失败，请检查并重新初始化 USRP",
                                                         "log_path": previous.get("log_path")}

    def stop(self):
        self.cancel_environment_initialization()
        return super().stop()

    def shutdown(self):
        with self.lock:
            self.closed = True
        self.cancel_environment_initialization()
        self._environment_done.wait(timeout=7)
        super().shutdown()
