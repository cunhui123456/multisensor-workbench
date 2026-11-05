"""设备环境的离线契约；全部设备与采集子进程均为替身。"""

import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from sspp_desktop.config import Settings
import sspp_desktop.device_environment as environment_module
from sspp_desktop.device_environment import parse_result
from sspp_desktop.initialize_device import EVENT, check_serial, check_usrp
from sspp_desktop.services import DesktopService
from sspp_lab.controller import DEFAULTS, LabError

RF = {"kind": "usrp", "profile": "own"}
SERIAL = {"kind": "serial", "port": "/dev/cu.OFFLINE", "protocol": "temperature_v1"}


def result(request_id="request", kind="usrp", status="ready", message="检查完成"):
    return {"event": EVENT, "version": 1, "request_id": request_id, "kind": kind,
            "status": status, "message": message, "released": True}


class FakeProcess:
    def __init__(self, args, *, blocked=False, resist_terminate=False, response=None, before_result=None):
        self.args = args
        self.blocked = blocked
        self.resist_terminate = resist_terminate
        self.response = response
        self.before_result = before_result
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.returncode = None
        self.terminated = self.killed = self.waited = False
        self.entered = threading.Event()

    def poll(self):
        return self.returncode

    def communicate(self, timeout):
        self.entered.set()
        if self.blocked:
            time.sleep(min(timeout, 0.005))
            raise subprocess.TimeoutExpired(self.args, timeout)
        if self.before_result:
            self.before_result()
        event = result(self.args[self.args.index("--request-id") + 1], self.args[self.args.index("--kind") + 1])
        if self.response:
            event = self.response(event)
        self.returncode = 0 if isinstance(event, dict) and event.get("status") == "ready" else 1
        return json.dumps(event) if isinstance(event, dict) else event, "private driver diagnostics PRIVATE_SERIAL"

    def terminate(self):
        self.terminated = True
        if not self.resist_terminate:
            self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "image.bin"
        self.image.write_bytes(b"offline image fixture")
        self.profile = self.root / "device.json"
        self.profile.write_text(json.dumps({"serial": "PRIVATE_SERIAL", "channel": 0,
            "fpga": str(self.image), "fpga_sha256": hashlib.sha256(self.image.read_bytes()).hexdigest()}))
        self.service = DesktopService(Settings(root=self.root, own_profile="device.json", teacher_profile="missing.json"))
        self.processes = []
        process_check = patch("sspp_desktop.device_environment.subprocess.run",
                              return_value=Mock(returncode=0, stdout=""))
        self.process_check = process_check.start()
        self.addCleanup(process_check.stop)
        # 本文件验证状态机、绑定和收尾；真实双管道流读取由独立 subprocess 测试覆盖。
        runner = patch.object(self.service, "_run_environment_process", side_effect=self.run_fake_process)
        self.process_runner = runner.start()
        self.addCleanup(runner.stop)

    def run_fake_process(self, proc, *, cancelled, started, timeout_s, emit):
        """保留旧替身的调度/超时行为，生产 finally 仍负责真正的回收断言。"""
        while True:
            if cancelled():
                return {"stdout": "", "returncode": proc.returncode,
                        "reason": "cancelled", "stderr_tail": ""}
            remaining = timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                return {"stdout": "", "returncode": proc.returncode,
                        "reason": "timeout", "stderr_tail": ""}
            try:
                stdout, stderr = proc.communicate(timeout=min(.1, remaining))
            except subprocess.TimeoutExpired:
                continue
            safe_lines = [emit(line, source="设备") for line in stderr.splitlines()]
            return {"stdout": stdout, "returncode": proc.returncode,
                    "reason": None, "stderr_tail": "\n".join(safe_lines)}

    def factory(self, **options):
        def launch(args, **kwargs):
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(kwargs["start_new_session"])
            self.assertNotIn("shell", kwargs)
            process = FakeProcess(args, **options)
            self.processes.append(process)
            return process
        return launch

    def initialize(self, target=RF, **options):
        with patch("sspp_desktop.device_environment.subprocess.Popen", side_effect=self.factory(**options)):
            return self.service.initialize_environment(target)

    def test_constructor_and_snapshot_have_no_io_and_no_device_access(self):
        with patch("pathlib.Path.open", side_effect=AssertionError("snapshot IO")), \
             patch("sspp_desktop.device_environment.subprocess.Popen", side_effect=AssertionError("opened hardware")):
            service = DesktopService(Settings(root=self.root))
            snap = service.environment_snapshot()
            self.assertEqual(snap["state"], "idle")
            self.assertEqual(set(snap["devices"]), {"usrp", "serial"})
            self.assertTrue(all(device["state"] == "idle" for device in snap["devices"].values()))
            snap["devices"]["usrp"]["state"] = "ready"
            self.assertEqual(service.environment_snapshot("usrp")["state"], "idle")

    def test_explicit_rf_success_is_reaped_and_bound_to_profile(self):
        snap = self.initialize()
        self.assertEqual(snap["state"], "ready")
        self.process_runner.assert_called_once()
        log = Path(snap["log_path"])
        self.assertTrue(log.is_absolute())
        self.assertTrue(log.is_file())
        self.assertEqual(log.parent, self.root / ".cache" / "initialization_logs")
        diagnostic = log.read_text(encoding="utf-8")
        self.assertIn("private driver diagnostics [设备标识]", diagnostic)
        self.assertNotIn("PRIVATE_SERIAL", diagnostic)
        process = self.processes[-1]
        self.assertTrue(process.waited)
        self.assertTrue(process.stdout.closed and process.stderr.closed)
        self.assertIsNone(self.service._environment_process)
        self.assertTrue(self.service.environment_matches(RF))
        self.assertFalse(self.service.environment_matches({"kind": "usrp", "profile": "teacher"}))
        self.assertFalse((self.root / "Data").exists())
        with patch("pathlib.Path.open", side_effect=AssertionError("snapshot IO")):
            self.assertEqual(self.service.environment_snapshot("usrp")["state"], "ready")

    def test_serial_binding_includes_port_and_protocol(self):
        self.assertEqual(self.initialize(SERIAL)["state"], "ready")
        self.assertTrue(self.service.environment_matches(SERIAL))
        self.assertFalse(self.service.environment_matches({**SERIAL, "protocol": "pressure_v1"}))
        self.assertFalse(self.service.environment_matches({**SERIAL, "port": "/dev/cu.OTHER"}))
        self.assertFalse((self.root / "Data").exists())
        self.service.invalidate_environment()
        self.assertEqual(self.service.environment_snapshot("usrp")["state"], "idle")

    def test_config_change_invalidates_match_and_blocks_acquisition(self):
        self.initialize()
        self.profile.write_text(self.profile.read_text() + "\n")
        self.assertFalse(self.service.environment_matches(RF))
        with patch("sspp_lab.controller.LabController.start") as start:
            with self.assertRaises(LabError):
                self.service.start({**DEFAULTS, "profile": "own", "setup_confirmed": True})
            start.assert_not_called()

    def test_config_change_during_initialization_cannot_be_ready(self):
        snap = self.initialize(before_result=lambda: self.profile.write_text(self.profile.read_text() + "\n"))
        self.assertEqual(snap["state"], "failed")
        self.assertFalse(self.service.environment_matches(RF))

    def test_start_requires_ready_and_preserves_real_start_failure(self):
        request = {**DEFAULTS, "profile": "own", "setup_confirmed": True}
        with patch("sspp_lab.controller.LabController.start") as start:
            with self.assertRaises(LabError):
                self.service.start(request)
            start.assert_not_called()
        self.initialize()
        with patch("sspp_lab.controller.LabController.start", return_value={"accepted": True}) as start:
            self.assertEqual(self.service.start(request), {"accepted": True})
            start.assert_called_once_with({**request, "setup_confirmed": True, "setup_confirmation_source": "global_initialization"})
        with patch("sspp_lab.controller.LabController.start", side_effect=LabError("PRIVATE_SERIAL failed")):
            with self.assertRaises(LabError):
                self.service.start(request)
        self.assertEqual(self.service.environment_snapshot("usrp")["state"], "failed")
        self.assertNotIn("PRIVATE_SERIAL", self.service.environment_snapshot("usrp")["message"])

    def test_acquire_command_rechecks_profile_after_preflight(self):
        self.initialize()
        request = {**DEFAULTS, "profile": "own"}
        self.assertIn("acquire", self.service.command(request, self.profile))
        self.profile.write_text(self.profile.read_text() + "\n")
        with self.assertRaises(LabError):
            self.service.command(request, self.profile)
        self.assertIn("plan", self.service.command(request, self.profile, "plan"))

    def test_changed_config_at_launch_does_not_leave_job_stuck_starting(self):
        self.initialize()
        def prepare(request):
            self.profile.write_text(self.profile.read_text() + "\n")
        with patch.object(self.service, "preflight", side_effect=prepare), \
             patch("sspp_lab.controller.subprocess.run", return_value=Mock(stdout="")), \
             patch("sspp_lab.controller.subprocess.Popen") as launch:
            with self.assertRaises(LabError):
                self.service.start({**DEFAULTS, "profile": "own", "setup_confirmed": True})
            launch.assert_not_called()
        self.assertEqual(self.service.job["status"], "failed")
        self.assertFalse(self.service.preparing)
        self.assertEqual(self.initialize()["state"], "ready")

    def test_failures_are_redacted_and_malformed_success_is_rejected(self):
        snap = self.initialize(response=lambda event: {**event, "status": "failed", "message": "error PRIVATE_SERIAL"})
        self.assertEqual(snap["state"], "failed")
        self.assertTrue(snap["message"].startswith("error [设备标识]"))
        self.assertNotIn("PRIVATE_SERIAL", snap["message"])
        self.assertIn("private driver diagnostics [设备标识]", snap["message"])
        first_log = snap["log_path"]
        snap = self.initialize(response=lambda event: json.dumps(event) + "\n" + json.dumps(event))
        self.assertEqual(snap["state"], "failed")
        self.assertIn("未返回有效结果", snap["message"])
        self.assertIn("private driver diagnostics [设备标识]", snap["message"])
        self.assertNotIn("PRIVATE_SERIAL", snap["message"])
        self.assertNotEqual(snap["log_path"], first_log)
        for path in (first_log, snap["log_path"]):
            diagnostic = Path(path).read_text(encoding="utf-8")
            self.assertIn("private driver diagnostics [设备标识]", diagnostic)
            self.assertNotIn("PRIVATE_SERIAL", diagnostic)

    def begin_blocked(self, **options):
        values = []
        worker = threading.Thread(target=lambda: values.append(self.service.initialize_environment(RF)))
        launch = patch("sspp_desktop.device_environment.subprocess.Popen", side_effect=self.factory(blocked=True, **options))
        launch.start()
        self.addCleanup(launch.stop)
        worker.start()
        self.addCleanup(lambda: (self.service.cancel_environment_initialization(), worker.join(2)))
        deadline = time.monotonic() + 2
        while not self.processes and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertTrue(self.processes)
        self.assertTrue(self.processes[-1].entered.wait(2))
        return worker, values

    def test_cancel_is_nonblocking_reaps_and_excludes_duplicate_work(self):
        worker, values = self.begin_blocked(resist_terminate=True)
        self.assertEqual(self.service.environment_snapshot("usrp")["state"], "initializing")
        for operation in (lambda: self.service.initialize_environment(RF), lambda: self.service.preflight(DEFAULTS),
                          lambda: self.service.start({**DEFAULTS, "profile": "own", "setup_confirmed": True})):
            with self.assertRaises(LabError):
                operation()
        started = time.monotonic()
        self.service.cancel_environment_initialization()
        self.assertLess(time.monotonic() - started, .1)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(values[0]["state"], "cancelled")
        self.assertTrue(self.processes[-1].killed and self.processes[-1].waited)
        self.assertFalse(self.service.preparing)

    def test_timeout_kills_blocked_native_initialization(self):
        with patch("sspp_desktop.device_environment.USRP_INITIALIZATION_TIMEOUT_S", .02):
            snap = self.initialize(blocked=True, resist_terminate=True)
        self.assertEqual(snap["state"], "failed")
        self.assertIn("0.02 秒", snap["message"])
        self.assertTrue(self.processes[-1].killed)
        self.assertFalse(self.service.environment_matches(RF))

    def test_rf_and_serial_receive_separate_initialization_budgets(self):
        self.assertEqual(self.initialize(RF)["state"], "ready")
        self.assertEqual(self.process_runner.call_args.kwargs["timeout_s"], 120.0)
        self.assertEqual(self.initialize(SERIAL)["state"], "ready")
        self.assertEqual(self.process_runner.call_args.kwargs["timeout_s"], 30.0)

    def test_shutdown_cancels_and_waits_for_initialization_cleanup(self):
        worker, values = self.begin_blocked(resist_terminate=True)
        self.service.shutdown()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(values[0]["state"], "cancelled")
        self.assertTrue(self.processes[-1].killed and self.processes[-1].stdout.closed)

    def test_busy_and_closed_reject_initialization_without_launch(self):
        for attribute, value in (("preparing", True), ("exporting", True), ("closed", True),
                                 ("job", {"status": "saving"})):
            with self.subTest(attribute=attribute), patch.object(self.service, attribute, value), \
                 patch("sspp_desktop.device_environment.subprocess.Popen") as launch:
                with self.assertRaises(LabError):
                    self.service.initialize_environment(RF)
                launch.assert_not_called()

    def test_standalone_preflight_reserves_environment_until_finished(self):
        entered, release = threading.Event(), threading.Event()
        def check(request):
            entered.set()
            release.wait(2)
            return {"valid": True}
        with patch("sspp_lab.controller.LabController.preflight", side_effect=check):
            worker = threading.Thread(target=lambda: self.service.preflight(DEFAULTS))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.service.preparing)
                with self.assertRaises(LabError):
                    self.service.initialize_environment(RF)
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(self.service.preparing)

    def test_invalidate_before_process_launch_prevents_device_open(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service._profile_binding
        def binding(profile):
            entered.set()
            release.wait(2)
            return original(profile)
        values = []
        with patch.object(self.service, "_profile_binding", side_effect=binding), \
             patch("sspp_desktop.device_environment.subprocess.Popen") as launch:
            worker = threading.Thread(target=lambda: values.append(self.service.initialize_environment(RF)))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.service.invalidate_environment()
            finally:
                release.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            launch.assert_not_called()
        self.assertEqual(values[0]["state"], "cancelled")

    def test_process_launch_error_clears_preparing_and_redacts_identity(self):
        with patch("sspp_desktop.device_environment.subprocess.Popen", side_effect=OSError("PRIVATE_SERIAL launch error")):
            snap = self.service.initialize_environment(RF)
        self.assertEqual(snap["state"], "failed")
        self.assertNotIn("PRIVATE_SERIAL", snap["message"])
        self.assertFalse(self.service.preparing)
        self.assertTrue(self.service._environment_done.is_set())

    def test_capture_failure_invalidates_previous_ready(self):
        self.initialize()
        job = {"status": "initialization_failed"}
        with patch("sspp_lab.controller.LabController._collect"):
            self.service._collect(None, job)
        self.assertEqual(self.service.environment_snapshot("usrp")["state"], "failed")
        self.assertFalse(self.service.environment_matches(RF))

    def test_child_rf_reads_identity_and_channels_without_setting_or_streaming(self):
        calls = []
        class Device:
            def __init__(self, address):
                calls.append(("open", address))
            def get_usrp_rx_info(self, channel):
                calls.append(("rx_identity", channel))
                return {"mboard_serial": "PRIVATE_SERIAL"}
            def get_usrp_tx_info(self, channel):
                calls.append(("tx_identity", channel))
                return {"mboard_serial": "PRIVATE_SERIAL"}
            def get_tx_num_channels(self):
                calls.append(("tx_channels",))
                return 2
            def get_rx_num_channels(self):
                calls.append(("rx_channels",))
                return 2
            def __del__(self):
                calls.append(("released",))
        digest = hashlib.sha256(self.profile.read_bytes()).hexdigest()
        check_usrp(self.profile, digest, Device)
        self.assertEqual([entry[0] for entry in calls], ["open", "tx_channels", "rx_channels", "rx_identity", "tx_identity", "released"])
        self.assertIn("serial=PRIVATE_SERIAL", calls[0][1])
        self.image.write_bytes(b"tampered image")
        factory = Mock()
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            check_usrp(self.profile, digest, factory)
        factory.assert_not_called()

    def test_child_rejects_invalid_channel_and_mismatched_identity(self):
        for channel in (True, 2, "0"):
            profile = json.loads(self.profile.read_text())
            profile["channel"] = channel
            self.profile.write_text(json.dumps(profile))
            factory = Mock()
            with self.assertRaisesRegex(ValueError, "通道"):
                check_usrp(self.profile, hashlib.sha256(self.profile.read_bytes()).hexdigest(), factory)
            factory.assert_not_called()
        profile["channel"] = 0
        self.profile.write_text(json.dumps(profile))
        device = Mock()
        device.get_rx_num_channels.return_value = device.get_tx_num_channels.return_value = 2
        device.get_usrp_rx_info.return_value = {"mboard_serial": "DIFFERENT"}
        device.get_usrp_tx_info.return_value = {"mboard_serial": "PRIVATE_SERIAL"}
        with self.assertRaisesRegex(ValueError, "序列号"):
            check_usrp(self.profile, hashlib.sha256(self.profile.read_bytes()).hexdigest(), lambda _: device)
        device.get_usrp_rx_info.assert_called_once_with(0)
        device.get_usrp_tx_info.assert_called_once_with(0)
        device.get_usrp_rx_info.return_value = {"mboard_serial": "PRIVATE_SERIAL"}
        device.get_usrp_tx_info.return_value = {"mboard_serial": "DIFFERENT"}
        with self.assertRaisesRegex(ValueError, "序列号"):
            check_usrp(self.profile, hashlib.sha256(self.profile.read_bytes()).hexdigest(), lambda _: device)
        device.reset_mock()
        device.get_rx_num_channels.return_value = 0
        with self.assertRaisesRegex(ValueError, "通道"):
            check_usrp(self.profile, hashlib.sha256(self.profile.read_bytes()).hexdigest(), lambda _: device)
        device.get_usrp_rx_info.assert_not_called()
        device.get_usrp_tx_info.assert_not_called()

    def test_installed_uhd_exposes_read_only_initialization_methods(self):
        # 只导入 Python 类型，不实例化 MultiUSRP；防止替身掩盖 binding API 拼写错误。
        import uhd
        for name in ("get_usrp_rx_info", "get_usrp_tx_info", "get_rx_num_channels", "get_tx_num_channels"):
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(uhd.usrp.MultiUSRP, name, None)))

    def test_known_external_acquisition_blocks_rf_before_open(self):
        for entry in ("run_local.py acquire", "run_phase.py acquire", "run_teacher.py", "sspp_lab.acquire_phase"):
            with self.subTest(entry=entry):
                self.process_check.return_value = Mock(returncode=0, stdout="123 python /repo/sspp-usrp/" + entry)
                with patch("sspp_desktop.device_environment.subprocess.Popen") as launch:
                    snap = self.service.initialize_environment(RF)
                    launch.assert_not_called()
                self.assertEqual(snap["state"], "failed")
                self.assertIn("另一个 SSPP", snap["message"])
        self.process_check.assert_called_with(["ps", "-axo", "pid=,command="], capture_output=True,
                                              text=True, timeout=5)

    def test_process_listing_failure_does_not_open_device(self):
        for failure in (subprocess.TimeoutExpired("ps", 5), OSError("ps unavailable")):
            with self.subTest(failure=failure), \
                 patch("sspp_desktop.device_environment.subprocess.run", side_effect=failure), \
                 patch("sspp_desktop.device_environment.subprocess.Popen") as launch:
                self.assertEqual(self.service.initialize_environment(RF)["state"], "failed")
                launch.assert_not_called()
        self.process_check.return_value = Mock(returncode=1, stdout="")
        with patch("sspp_desktop.device_environment.subprocess.Popen") as launch:
            self.assertEqual(self.service.initialize_environment(RF)["state"], "failed")
            launch.assert_not_called()

    def test_external_cancel_queued_before_initialization_prevents_open(self):
        cancel = threading.Event()
        cancel.set()
        with patch("sspp_desktop.device_environment.subprocess.Popen") as launch:
            snap = self.service.initialize_environment(RF, cancel_event=cancel)
            launch.assert_not_called()
        self.process_check.assert_not_called()
        self.assertEqual(snap["state"], "cancelled")
        self.assertTrue(cancel.is_set())
        self.assertFalse(self.service.preparing)

    def test_external_cancel_during_process_check_prevents_open(self):
        cancel = threading.Event()
        def listing(*args, **kwargs):
            cancel.set()
            return Mock(returncode=0, stdout="")
        self.process_check.side_effect = listing
        with patch("sspp_desktop.device_environment.subprocess.Popen") as launch:
            snap = self.service.initialize_environment(RF, cancel_event=cancel)
            launch.assert_not_called()
        self.assertEqual(snap["state"], "cancelled")

    def test_external_cancel_on_completion_rejects_late_success(self):
        cancel = threading.Event()
        with patch("sspp_desktop.device_environment.subprocess.Popen",
                   side_effect=self.factory(before_result=cancel.set)):
            snap = self.service.initialize_environment(RF, cancel_event=cancel)
        self.assertEqual(snap["state"], "cancelled")
        self.assertFalse(self.service.environment_matches(RF))
        self.assertTrue(self.processes[-1].waited)

    def test_external_cancel_while_native_initialization_is_blocked(self):
        cancel, values = threading.Event(), []
        with patch("sspp_desktop.device_environment.subprocess.Popen",
                   side_effect=self.factory(blocked=True, resist_terminate=True)):
            worker = threading.Thread(target=lambda: values.append(
                self.service.initialize_environment(RF, cancel_event=cancel)))
            worker.start()
            try:
                deadline = time.monotonic() + 2
                while not self.processes and time.monotonic() < deadline:
                    time.sleep(.001)
                self.assertTrue(self.processes)
                self.assertTrue(self.processes[-1].entered.wait(1))
            finally:
                cancel.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(values[0]["state"], "cancelled")
        self.assertTrue(self.processes[-1].killed)

    def test_failure_after_spawn_reaps_without_claiming_rf_stopped(self):
        self.initialize()
        process = Mock()
        process.poll.return_value = None
        process.stdout = io.StringIO("")
        process.wait.return_value = -9
        process.returncode = -9
        def end(proc, job):
            self.assertIs(proc, process)
            proc.poll.return_value = -9
        with patch.object(self.service, "preflight"), \
             patch("sspp_lab.controller.subprocess.Popen", return_value=process), \
             patch("sspp_lab.controller.threading.Thread.start", side_effect=RuntimeError("cannot start thread")), \
             patch.object(self.service, "_end_process", side_effect=end) as cleanup:
            with self.assertRaisesRegex(RuntimeError, "cannot start thread"):
                self.service.start({**DEFAULTS, "profile": "own", "setup_confirmed": True})
            cleanup.assert_called_once()
        self.assertEqual(self.service.job["status"], "stop_unconfirmed")
        self.assertFalse(self.service.job["rf_stopped"])
        self.assertEqual(self.service.environment_snapshot("usrp")["state"], "failed")
        self.assertFalse(self.service.preparing or self.service._environment_starting)

    def test_child_serial_only_opens_and_closes_without_read_or_write(self):
        reader = Mock(spec=["close"])
        factory = Mock(return_value=reader)
        check_serial(SERIAL["port"], SERIAL["protocol"], factory)
        factory.assert_called_once_with(SERIAL["port"], 9600)
        reader.close.assert_called_once_with()
        with self.assertRaises(ValueError):
            check_serial("/tmp/not-a-port", SERIAL["protocol"], factory)
        self.assertEqual(factory.call_count, 1)

    def test_result_parser_requires_exact_success_contract(self):
        self.assertEqual(parse_result(json.dumps(result()), 0, "request", "usrp")["status"], "ready")
        malformed = ["", "[]", json.dumps(result()) + json.dumps(result()),
                     json.dumps({**result(), "released": False}), json.dumps({**result(), "unexpected": True}),
                     json.dumps({**result(), "request_id": "old"}), json.dumps({**result(), "version": True}),
                     json.dumps({**result(), "message": 7}), json.dumps({**result(), "status": "initializing"}),
                     json.dumps(result()).replace('"version": 1', '"version": NaN'),
                     json.dumps(result()).replace('"version": 1', '"version": 1, "version": 1')]
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(LabError):
                parse_result(payload, 0, "request", "usrp")
        with self.assertRaises(LabError):
            parse_result(json.dumps(result()), 1, "request", "usrp")


if __name__ == "__main__":
    unittest.main()
