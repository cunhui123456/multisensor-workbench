"""Prevent macOS idle system sleep only while this acquisition process lives.

The guard deliberately has no duration and does not request display or forced
system sleep assertions. Closing it releases the assertion; ``caffeinate -w``
also releases it if the acquisition process exits without running cleanup.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time


class IdleSleepGuard:
    """Own one process-scoped ``caffeinate -i`` assertion, with bounded cleanup."""

    STARTUP_CHECK_SECONDS = 0.1
    STARTUP_DEADLINE_SECONDS = 2.0
    ASSERTION_POLL_SECONDS = 0.05
    ASSERTION_QUERY_SECONDS = 0.25
    SHUTDOWN_WAIT_SECONDS = 1.0

    def __init__(self):
        self.platform = sys.platform
        self._process = None
        self._requested = False
        self._watched_pid = None
        self._guard_pid = None
        self._returncode = None
        self._state = "not_requested"
        self._assertion_verified = False
        self._assertion_verified_monotonic = None

    def start(self):
        """Acquire protection before opening hardware; repeat calls are harmless.

        A short startup observation checks for immediate command failure. It is
        independent of, and does not impose a limit on, the acquisition duration.
        """
        self._requested = True
        if self.platform != "darwin":
            self._state = "unsupported_platform"
            return
        if self._process is not None:
            code = self._process.poll()
            if code is None:
                if self._assertion_verified:
                    return
                raise RuntimeError("采集防空闲睡眠进程仍在运行，但尚未核验系统保护断言")
            self._returncode = code
            self._process = None
            self._state = "failed"
            raise RuntimeError(f"采集防空闲睡眠保护已意外退出（caffeinate 返回码 {code}）")

        deadline = time.monotonic() + self.STARTUP_DEADLINE_SECONDS
        self._watched_pid = os.getpid()
        self._returncode = None
        self._assertion_verified = False
        self._assertion_verified_monotonic = None
        self._state = "starting"
        try:
            self._process = subprocess.Popen(
                ["/usr/bin/caffeinate", "-i", "-w", str(self._watched_pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
            )
            self._guard_pid = self._process.pid
        except OSError as exc:
            self._state = "failed"
            raise RuntimeError(f"无法启动采集防空闲睡眠保护（caffeinate）：{exc}") from exc

        try:
            code = self._process.wait(timeout=self.STARTUP_CHECK_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                self._verify_assertion(deadline)
            except RuntimeError as exc:
                cleanup_error = None
                try:
                    self.close()
                except RuntimeError as cleanup_exc:
                    cleanup_error = str(cleanup_exc)
                self._state = "failed"
                detail = f"；{cleanup_error}" if cleanup_error else ""
                raise RuntimeError(f"采集防空闲睡眠保护未建立：{exc}{detail}") from exc
            self._state = "active"
            return
        except OSError as exc:
            cleanup_error = None
            try:
                self.close()
            except RuntimeError as cleanup_exc:
                cleanup_error = str(cleanup_exc)
            self._state = "failed"
            detail = f"；{cleanup_error}" if cleanup_error else ""
            raise RuntimeError(f"无法确认采集防空闲睡眠保护已启动：{exc}{detail}") from exc
        self._returncode = code
        self._process = None  # wait() has reaped the immediately exited child.
        self._state = "failed"
        raise RuntimeError(f"采集防空闲睡眠保护启动后立即退出（caffeinate 返回码 {code}）")

    def _verify_assertion(self, deadline):
        """Require this child's PID-owned idle-system-sleep assertion, not a total."""
        pattern = re.compile(
            rf"^\s*pid\s+{self._guard_pid}\([^)]*\):\s+\[[^]\r\n]+\]"
            r"\s+\S+\s+PreventUserIdleSystemSleep(?:\s+named:|\s*$)",
            re.MULTILINE,
        )
        last_problem = "未找到本保护进程的 PreventUserIdleSystemSleep 断言"
        while time.monotonic() < deadline:
            code = self._process.poll()
            if code is not None:
                raise RuntimeError(f"caffeinate 在断言核验期间退出（返回码 {code}）")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                result = subprocess.run(
                    ["/usr/bin/pmset", "-g", "assertions"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=min(self.ASSERTION_QUERY_SECONDS, remaining),
                )
            except subprocess.TimeoutExpired:
                last_problem = "pmset 断言查询超时"
            except OSError as exc:
                raise RuntimeError(f"无法查询 macOS 电源断言：{exc}") from exc
            else:
                if result.returncode != 0:
                    last_problem = f"pmset 断言查询返回码 {result.returncode}"
                elif pattern.search(result.stdout):
                    if self._process.poll() is not None:
                        raise RuntimeError("caffeinate 在断言核验完成前已退出")
                    self._assertion_verified = True
                    self._assertion_verified_monotonic = time.monotonic()
                    return
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(self.ASSERTION_POLL_SECONDS, remaining))
        raise RuntimeError(last_problem)

    def close(self):
        """Release protection and reap the child; safe before start or repeatedly."""
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass  # The child may have exited between poll and terminate.
            try:
                self._returncode = process.wait(timeout=self.SHUTDOWN_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                self._returncode = process.wait(timeout=self.SHUTDOWN_WAIT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            # Keep ownership so another close() can retry; snapshot remains honest.
            self._state = "cleanup_failed"
            raise RuntimeError(f"无法结束或回收采集防空闲睡眠保护（caffeinate）：{exc}") from exc
        self._process = None
        self._state = "stopped"

    def snapshot(self):
        """Return JSON-compatible evidence without issuing any power commands."""
        code = self._process.poll() if self._process is not None else self._returncode
        running = self._process is not None and code is None
        active = running and self._assertion_verified
        state = "unexpected_exit" if self._state == "active" and not active else self._state
        return {
            "platform": self.platform,
            "supported": self.platform == "darwin",
            "requested": self._requested,
            "active": active,
            "guard_running": running,
            "assertion_verified": self._assertion_verified,
            "assertion_verified_monotonic_s": self._assertion_verified_monotonic,
            "assertion_type": "PreventUserIdleSystemSleep",
            "state": state,
            "mechanism": "caffeinate -i -w acquisition_pid" if self.platform == "darwin" else None,
            "watched_pid": self._watched_pid,
            "guard_pid": self._guard_pid,
            "returncode": code,
            "scope": "acquisition_process",
        }
