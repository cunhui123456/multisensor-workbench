"""Power assertion lifecycle checks; all process creation is mocked, no hardware."""
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from sspp_lab.power import IdleSleepGuard


class IdleSleepGuardTests(unittest.TestCase):
    ASSERTIONS = '   pid 4321(caffeinate): [0x000123000001abc] 00:00:00 PreventUserIdleSystemSleep named: "caffeinate command-line tool"\n'

    def setUp(self):
        self.elapsed = 0.
        def sleep(seconds):
            self.elapsed += seconds
        clock = patch("sspp_lab.power.time.monotonic", side_effect=lambda: self.elapsed)
        sleeper = patch("sspp_lab.power.time.sleep", side_effect=sleep)
        query = patch("sspp_lab.power.subprocess.run", return_value=subprocess.CompletedProcess(
            ["/usr/bin/pmset", "-g", "assertions"], 0, stdout=self.ASSERTIONS, stderr=""))
        clock.start()
        sleeper.start()
        self.query = query.start()
        self.addCleanup(clock.stop)
        self.addCleanup(sleeper.stop)
        self.addCleanup(query.stop)

    def make_guard(self, platform="darwin"):
        with patch("sspp_lab.power.sys.platform", platform):
            return IdleSleepGuard()

    def process(self, pid=4321):
        process = Mock()
        process.pid = pid
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("caffeinate", .1)
        return process

    def start(self, guard, process):
        with patch("sspp_lab.power.subprocess.Popen", return_value=process) as popen, \
             patch("sspp_lab.power.os.getpid", return_value=1234):
            guard.start()
        return popen

    def test_darwin_uses_only_idle_assertion_watching_acquisition_pid(self):
        guard = self.make_guard()
        process = self.process()
        popen = self.start(guard, process)
        popen.assert_called_once_with(
            ["/usr/bin/caffeinate", "-i", "-w", "1234"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=False, start_new_session=True,
        )
        process.wait.assert_called_once_with(timeout=guard.STARTUP_CHECK_SECONDS)
        snapshot = guard.snapshot()
        self.assertTrue(snapshot["active"])
        self.assertTrue(snapshot["requested"])
        self.assertTrue(snapshot["supported"])
        self.assertEqual(snapshot["watched_pid"], 1234)
        self.assertEqual(snapshot["guard_pid"], 4321)
        self.assertEqual(snapshot["state"], "active")
        self.assertTrue(snapshot["assertion_verified"])
        self.query.assert_called_once_with(["/usr/bin/pmset", "-g", "assertions"], capture_output=True, text=True, check=False, timeout=guard.ASSERTION_QUERY_SECONDS)
        json.dumps(snapshot, allow_nan=False)

    def test_initial_snapshot_and_close_do_not_create_processes(self):
        guard = self.make_guard()
        with patch("sspp_lab.power.subprocess.Popen") as popen:
            self.assertFalse(guard.snapshot()["requested"])
            guard.close()
            guard.close()
        popen.assert_not_called()
        self.assertFalse(guard.snapshot()["active"])

    def test_non_darwin_start_and_close_are_noops(self):
        for platform in ("linux", "win32", "freebsd14"):
            with self.subTest(platform=platform):
                guard = self.make_guard(platform)
                with patch("sspp_lab.power.subprocess.Popen") as popen:
                    guard.start()
                    guard.start()
                    guard.close()
                popen.assert_not_called()
                self.query.assert_not_called()
                snapshot = guard.snapshot()
                self.assertTrue(snapshot["requested"])
                self.assertFalse(snapshot["supported"])
                self.assertFalse(snapshot["active"])
                self.assertIsNone(snapshot["watched_pid"])
                self.assertIsNone(snapshot["guard_pid"])
                self.assertEqual(snapshot["state"], "unsupported_platform")

    def test_repeated_start_does_not_leak_a_second_guard(self):
        guard = self.make_guard()
        process = self.process()
        with patch("sspp_lab.power.subprocess.Popen", return_value=process) as popen:
            guard.start()
            guard.start()
        popen.assert_called_once()
        process.wait.assert_called_once()

    def test_launch_failure_is_explicit_and_no_active_guard_is_claimed(self):
        for error in (FileNotFoundError("missing caffeinate"), PermissionError("denied")):
            with self.subTest(error=type(error).__name__):
                guard = self.make_guard()
                with patch("sspp_lab.power.subprocess.Popen", side_effect=error), \
                     self.assertRaisesRegex(RuntimeError, "无法启动.*防空闲睡眠"):
                    guard.start()
                self.assertEqual(guard.snapshot()["state"], "failed")
                self.assertFalse(guard.snapshot()["active"])
                guard.close()

    def test_even_zero_exit_during_startup_is_a_failure(self):
        for code in (0, 1, -15):
            with self.subTest(code=code):
                guard = self.make_guard()
                process = self.process()
                process.wait.side_effect = None
                process.wait.return_value = code
                with patch("sspp_lab.power.subprocess.Popen", return_value=process), \
                     self.assertRaisesRegex(RuntimeError, "启动后立即退出"):
                    guard.start()
                self.assertFalse(guard.snapshot()["active"])
                self.assertEqual(guard.snapshot()["returncode"], code)
                guard.close()
                process.terminate.assert_not_called()
                process.kill.assert_not_called()
                process.wait.assert_called_once()

    def test_startup_observation_failure_attempts_cleanup(self):
        guard = self.make_guard()
        process = self.process()
        process.wait.side_effect = [OSError("wait failed"), -15]
        with patch("sspp_lab.power.subprocess.Popen", return_value=process), \
             self.assertRaisesRegex(RuntimeError, "无法确认.*已启动"):
            guard.start()
        process.terminate.assert_called_once()
        self.assertFalse(guard.snapshot()["active"])
        self.assertEqual(guard.snapshot()["state"], "failed")

    def test_close_terminates_waits_and_preserves_auditable_pids(self):
        guard = self.make_guard()
        process = self.process()
        self.start(guard, process)
        process.wait.side_effect = None
        process.wait.return_value = -15
        guard.close()
        guard.close()
        process.terminate.assert_called_once()
        process.kill.assert_not_called()
        self.assertEqual(process.wait.call_count, 2)
        process.wait.assert_called_with(timeout=guard.SHUTDOWN_WAIT_SECONDS)
        snapshot = guard.snapshot()
        self.assertFalse(snapshot["active"])
        self.assertEqual(snapshot["state"], "stopped")
        self.assertTrue(snapshot["assertion_verified"])
        self.assertEqual(snapshot["watched_pid"], 1234)
        self.assertEqual(snapshot["guard_pid"], 4321)
        self.assertEqual(snapshot["returncode"], -15)

    def test_close_kills_and_reaps_when_terminate_does_not_exit(self):
        guard = self.make_guard()
        process = self.process()
        self.start(guard, process)
        process.wait.side_effect = [subprocess.TimeoutExpired("caffeinate", 1), -9]
        guard.close()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 3)
        self.assertFalse(guard.snapshot()["active"])
        self.assertEqual(guard.snapshot()["returncode"], -9)

    def test_already_exited_guard_is_reaped_without_sending_a_signal(self):
        guard = self.make_guard()
        process = self.process()
        self.start(guard, process)
        process.poll.return_value = 1
        process.wait.side_effect = None
        process.wait.return_value = 1
        self.assertEqual(guard.snapshot()["state"], "unexpected_exit")
        guard.close()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertFalse(guard.snapshot()["active"])

    def test_exit_race_between_poll_and_terminate_still_reaps(self):
        guard = self.make_guard()
        process = self.process()
        self.start(guard, process)
        process.terminate.side_effect = ProcessLookupError("already gone")
        process.wait.side_effect = None
        process.wait.return_value = 0
        guard.close()
        self.assertFalse(guard.snapshot()["active"])
        self.assertEqual(guard.snapshot()["state"], "stopped")

    def test_failed_cleanup_is_bounded_and_can_be_retried(self):
        guard = self.make_guard()
        process = self.process()
        self.start(guard, process)
        process.wait.side_effect = subprocess.TimeoutExpired("caffeinate", 1)
        with self.assertRaisesRegex(RuntimeError, "无法结束或回收"):
            guard.close()
        self.assertEqual(process.wait.call_count, 3)
        process.kill.assert_called_once()
        self.assertEqual(guard.snapshot()["state"], "cleanup_failed")
        self.assertTrue(guard.snapshot()["active"])
        process.wait.side_effect = None
        process.wait.return_value = -9
        guard.close()
        self.assertFalse(guard.snapshot()["active"])
        self.assertEqual(guard.snapshot()["state"], "stopped")

    def test_assertion_must_belong_to_exact_guard_pid_and_type(self):
        for output in (
            "PreventUserIdleSystemSleep 1\n",
            self.ASSERTIONS.replace("4321", "14321"),
            self.ASSERTIONS.replace("PreventUserIdleSystemSleep", "PreventUserIdleDisplaySleep"),
            self.ASSERTIONS.replace('00:00:00 PreventUserIdleSystemSleep', '00:00:00 UserIsActive').replace('command-line tool', 'PreventUserIdleSystemSleep'),
        ):
            with self.subTest(output=output):
                guard = self.make_guard()
                process = self.process()
                process.wait.side_effect = [subprocess.TimeoutExpired("caffeinate", .1), -15]
                self.query.return_value = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
                started = self.elapsed
                with self.assertRaisesRegex(RuntimeError, "未建立"):
                    self.start(guard, process)
                self.assertLessEqual(self.elapsed-started, guard.STARTUP_DEADLINE_SECONDS + 1e-8)
                process.terminate.assert_called_once()
                self.assertFalse(guard.snapshot()["assertion_verified"])
                self.assertFalse(guard.snapshot()["active"])

    def test_delayed_assertion_is_accepted_within_startup_window(self):
        guard = self.make_guard()
        process = self.process()
        self.query.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="PreventUserIdleSystemSleep 1", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=self.ASSERTIONS, stderr=""),
        ]
        self.start(guard, process)
        self.assertTrue(guard.snapshot()["assertion_verified"])
        self.assertGreater(self.elapsed, 0)
        self.assertEqual(self.query.call_count, 2)

    def test_assertion_query_timeout_is_bounded_and_cleans_up(self):
        guard = self.make_guard()
        process = self.process()
        process.wait.side_effect = [subprocess.TimeoutExpired("caffeinate", .1), -15]
        self.query.side_effect = subprocess.TimeoutExpired("pmset", .25)
        with self.assertRaisesRegex(RuntimeError, "查询超时"):
            self.start(guard, process)
        self.assertLessEqual(self.elapsed, guard.STARTUP_DEADLINE_SECONDS + 1e-8)
        process.terminate.assert_called_once()
        self.assertFalse(guard.snapshot()["assertion_verified"])

    def test_unavailable_pmset_fails_before_claiming_protection(self):
        guard = self.make_guard()
        process = self.process()
        process.wait.side_effect = [subprocess.TimeoutExpired("caffeinate", .1), -15]
        self.query.side_effect = FileNotFoundError("pmset unavailable")
        with self.assertRaisesRegex(RuntimeError, "无法查询 macOS"):
            self.start(guard, process)
        process.terminate.assert_called_once()
        self.assertFalse(guard.snapshot()["active"])

    def test_assertion_is_rejected_if_guard_exits_during_query(self):
        guard = self.make_guard()
        process = self.process()
        process.wait.side_effect = [subprocess.TimeoutExpired("caffeinate", .1), 1]
        process.poll.side_effect = [None, 1, 1]
        with self.assertRaisesRegex(RuntimeError, "核验完成前已退出"):
            self.start(guard, process)
        self.assertFalse(guard.snapshot()["active"])
        self.assertFalse(guard.snapshot()["assertion_verified"])

    def test_repeated_start_reports_unexpected_previous_exit(self):
        guard = self.make_guard()
        process = self.process()
        self.start(guard, process)
        process.poll.return_value = 1
        with patch("sspp_lab.power.subprocess.Popen") as popen, \
             self.assertRaisesRegex(RuntimeError, "已意外退出"):
            guard.start()
        popen.assert_not_called()
        self.assertFalse(guard.snapshot()["active"])
        self.assertEqual(guard.snapshot()["state"], "failed")


if __name__ == "__main__":
    unittest.main()
