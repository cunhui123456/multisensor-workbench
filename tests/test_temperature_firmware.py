"""Preparation flow uses injected transports and commands, never attached hardware."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from sspp_experiments.temperature_pressure.firmware import (
    APPLICATION_BYTES, FLASH_BYTES, SIGNATURE, PreparationCancelled, PreparationSession,
    firmware_bundle, is_usb_serial_port, _hex_memory, validate_avrdude_toolchain,
    avrdude_candidates, _macho_cpu_types,
)

REPO = Path(__file__).resolve().parents[1]


class Reader:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def read(self, timeout=.1):
        if self.chunks:
            item = self.chunks.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        time.sleep(.005)
        return None

    def close(self):
        self.closed = True


def pressure():
    return [b"100,2.70\n150,2.71\n200,2.72\n"]


def temperature():
    return [b"100,512,2.502,139,600,2.933\n280,513,2.507,319,601,2.937\n460,514,2.512,499,602,2.942\n"]


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(REPO / "firmware", self.root / "firmware")
        self.reader = Reader(pressure())
        self.factory = Mock(return_value=self.reader)
        self.commands = []
        for target, value in (("platform.system", "Darwin"), ("platform.machine", "arm64")):
            mocked = patch("sspp_experiments.temperature_pressure.firmware." + target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.uploader = self.root / "avrdude"
        self.uploader.write_bytes(b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x0100000C) + b"\0" * 24)
        self.uploader.chmod(0o755)
        self.uploader_config = self.root / "avrdude.conf"
        self.uploader_config.write_text("# fake native uploader configuration\n")
        self.session = PreparationSession(self.root, transport_factory=self.factory,
                                          toolchain={"executable": str(self.uploader), "config": str(self.uploader_config)},
                                          verify_timeout_s=.12)
        self.session._run_command = self.command
        self.addCleanup(lambda: self.session.stop())

    def command(self, args, **options):
        self.commands.append((args, options))
        for arg in args:
            if arg.startswith("signature:r:"):
                Path(arg[len("signature:r:"):-2]).write_bytes(SIGNATURE)
            if arg.startswith("flash:r:"):
                Path(arg[len("flash:r:"):-2]).write_bytes(b"\xff" * FLASH_BYTES)
        return "fake avrdude success"

    def finish(self):
        end = time.monotonic() + 3
        while self.session.is_running and time.monotonic() < end:
            time.sleep(.005)
        self.assertFalse(self.session.is_running)
        return self.session.snapshot()

    def test_no_side_effect_on_construct_or_invalid_port(self):
        self.session.snapshot()
        self.assertFalse((self.root / "Data").exists())
        self.factory.assert_not_called()
        for name in ("/dev/cu.ULTFIELD1", "/dev/cu.Bluetooth-Incoming-Port", "/tmp/usbserial", "/dev/cu.usbserial/escape"):
            with self.assertRaises(ValueError):
                self.session.start(name, "pressure_v1")
        self.assertEqual(self.commands, [])
        self.assertFalse((self.root / "Data").exists())

    def test_known_usb_names_are_candidates_not_identity(self):
        for name in ("usbserial-140", "usbmodem101", "wchusbserial001", "SLAB_USBtoUART"):
            self.assertTrue(is_usb_serial_port("/dev/cu." + name))

    def test_upload_requires_complete_backup_and_signature_before_flash(self):
        key = self.session.start("/dev/cu.usbserialFAKE", "pressure_v1")
        snapshot = self.finish()
        self.assertEqual(snapshot["status"], "ready", snapshot)
        self.assertEqual(len(self.commands), 3)
        backup_cmd, write_cmd = self.commands[1][0], self.commands[2][0]
        self.assertIn("-p", write_cmd)
        self.assertIn("m328p", write_cmd)
        self.assertIn("-D", write_cmd)
        self.assertIn("-A", write_cmd)
        self.assertNotIn("-F", write_cmd)
        self.assertFalse(any("fuse" in arg or "eeprom" in arg for arg in write_cmd))
        self.assertTrue(any(arg.startswith("signature:r:") for arg in backup_cmd))
        self.assertTrue(any(arg.startswith("flash:w:") for arg in write_cmd))
        self.assertTrue(any(arg.startswith("flash:v:") for arg in write_cmd))
        self.assertFalse(self.commands[2][1]["cancellable"])
        folder = self.root / "Data" / key
        self.assertEqual((folder / "flash_before.bin").stat().st_size, FLASH_BYTES)
        result = snapshot["result"]
        self.assertEqual(result["firmware_identity"], "uploaded_and_avrdude_verified")
        self.assertTrue(result["firmware_sha256"])
        self.assertTrue(result["write_verified"])
        self.assertEqual(result["verified_frames"], 3)
        self.assertTrue(self.session.matches("/dev/cu.usbserialFAKE", "pressure_v1"))
        self.assertFalse(self.session.matches("/dev/cu.usbserialFAKE", "temperature_v1"))
        self.assertTrue(self.reader.closed)
        self.assertEqual(json.loads((folder / "manifest.json").read_text())["status"], "ready")
        self.assertFalse((self.root / "Data" / "temperature_pressure_runs").exists())

    def test_short_backup_or_wrong_chip_never_writes_or_opens_data_reader(self):
        for corrupt in ("signature", "size"):
            with self.subTest(corrupt=corrupt):
                self.commands.clear()
                def bad_command(args, **options):
                    result = self.command(args, **options)
                    for arg in args:
                        if corrupt == "signature" and arg.startswith("signature:r:"):
                            Path(arg[len("signature:r:"):-2]).write_bytes(bytes.fromhex("1e9516"))
                        if corrupt == "size" and arg.startswith("flash:r:"):
                            Path(arg[len("flash:r:"):-2]).write_bytes(b"short")
                    return result
                self.session._run_command = bad_command
                self.session.start("/dev/cu.usbserialFAKE", "pressure_v1")
                snapshot = self.finish()
                self.assertEqual(snapshot["status"], "failed")
                self.assertFalse(any(any(arg.startswith("flash:w:") for arg in cmd) for cmd, _ in self.commands))
                self.factory.assert_not_called()
                self.assertIsNone(snapshot["result"])

    def test_verify_existing_firmware_does_not_claim_image_identity(self):
        self.session.start("/dev/cu.usbserialFAKE", "pressure_v1", upload=False)
        snapshot = self.finish()
        self.assertEqual(snapshot["status"], "ready", snapshot)
        self.assertEqual(self.commands, [])
        result = snapshot["result"]
        self.assertEqual(result["firmware_identity"], "protocol_only")
        self.assertIsNone(result["firmware_sha256"])
        self.assertNotIn("requested_firmware", result)
        self.assertNotIn("write_started", result)
        self.assertTrue(self.reader.closed)
        self.session.invalidate()
        self.assertFalse(self.session.matches("/dev/cu.usbserialFAKE", "pressure_v1"))
        self.assertIsNone(self.session.snapshot()["result"])

    def test_temperature_requires_three_full_dual_channel_frames(self):
        self.reader.chunks = temperature()
        self.session.start("/dev/cu.usbserialFAKE", "temperature_v1", upload=False)
        snapshot = self.finish()
        self.assertEqual(snapshot["status"], "ready", snapshot)
        self.assertEqual(len(snapshot["result"]["preview"]), 6)
        self.assertEqual({row["channel"] for row in snapshot["result"]["preview"]}, {"S6", "S7"})

    def test_wrong_protocol_no_bytes_firmware_error_and_stuck_clock_fail(self):
        cases = [(temperature(), "另一种实验协议"), ([], "任何串口字节"),
                 ([b"# ERROR: TCA9555 initialization failed\n"], "硬件错误"),
                 ([b"100,2.7\n100,2.7\n100,2.7\n"], "时间递增"),
                 ([b"100,2.7\nnoise\n150,2.7\n200,2.7\n"], "时间递增")]
        for chunks, message in cases:
            with self.subTest(message=message):
                self.reader = Reader(chunks)
                self.factory.return_value = self.reader
                self.session.start("/dev/cu.usbserialFAKE", "pressure_v1", upload=False)
                snapshot = self.finish()
                self.assertEqual(snapshot["status"], "failed", snapshot)
                self.assertIn(message, snapshot["message"])
                self.assertIsNone(snapshot["result"])
                self.assertTrue(self.reader.closed)

    def test_cancel_during_write_finishes_verify_but_never_enters_acquisition(self):
        def command(args, **options):
            result = self.command(args, **options)
            if any(arg.startswith("flash:w:") for arg in args):
                self.assertFalse(options["cancellable"])
                self.assertTrue(self.session.snapshot()["write_active"])
                self.session.request_stop()
                self.assertIn("请勿拔线", self.session.snapshot()["message"])
            return result
        self.session._run_command = command
        self.session.start("/dev/cu.usbserialFAKE", "pressure_v1")
        snapshot = self.finish()
        self.assertEqual(snapshot["status"], "cancelled", snapshot)
        self.assertIsNone(snapshot["result"])
        evidence = json.loads((self.session.path / "manifest.json").read_text())
        self.assertTrue(evidence["write_verified"])
        self.factory.assert_not_called()

    def test_cancel_verification_closes_reader_and_preserves_evidence(self):
        self.reader.chunks = []
        self.session.verify_timeout_s = 3
        self.session.start("/dev/cu.usbserialFAKE", "pressure_v1", upload=False)
        end = time.monotonic() + 1
        while not self.factory.called and time.monotonic() < end:
            time.sleep(.005)
        self.session.request_stop()
        snapshot = self.finish()
        self.assertEqual(snapshot["status"], "cancelled")
        self.assertTrue(self.reader.closed)
        self.assertTrue((self.session.path / "verification_serial.bin").exists())
        self.assertIsNone(snapshot["result"])

    def test_broken_toolchain_stops_before_backup_or_serial(self):
        self.session._run_command = PreparationSession._run_command.__get__(self.session)
        self.session.process_factory = Mock(side_effect=OSError(86, "Bad CPU type in executable"))
        self.session.start("/dev/cu.usbserialFAKE", "pressure_v1")
        snapshot = self.finish()
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("工具架构", snapshot["message"])
        self.factory.assert_not_called()
        self.assertFalse((self.session.path / "flash_before.bin").exists())

    def test_real_subprocess_streams_and_reaps_on_failure_or_timeout(self):
        self.session._run_command = PreparationSession._run_command.__get__(self.session)
        with self.assertRaisesRegex(RuntimeError, "退出码 7"):
            self.session._run_command([sys.executable, "-u", "-c", "print('failure detail'); raise SystemExit(7)"])
        self.assertTrue(any("failure detail" in line for line in self.session.snapshot()["log"]))
        process_list = []
        def create(*args, **kwargs):
            process = subprocess.Popen(*args, **kwargs)
            process_list.append(process)
            return process
        self.session.process_factory = create
        with self.assertRaises(TimeoutError):
            self.session._run_command([sys.executable, "-u", "-c", "import time; time.sleep(10)"], timeout_s=.1)
        self.assertIsNotNone(process_list[-1].poll())

    def test_log_disk_failure_does_not_kill_flash_child_but_blocks_readiness(self):
        class FailingSink:
            writes = 0
            closed = False
            def write(self, value):
                self.writes += 1
                if self.writes >= 2:
                    raise OSError(28, "No space left on device")
                return len(value)
            def flush(self):
                pass
            def close(self):
                self.closed = True
        marker = self.root / "flash-verified-marker"
        process_list = []
        def create(*args, **kwargs):
            process = subprocess.Popen(*args, **kwargs)
            process_list.append(process)
            return process
        self.session.process_factory = create
        real_command = PreparationSession._run_command.__get__(self.session)
        sink = FailingSink()
        def execute(args, **options):
            if not any(arg.startswith("flash:w:") for arg in args):
                return self.command(args, **options)
            self.session._log_handle.close()
            self.session._log_handle = sink
            program = ("import time; from pathlib import Path; "
                       "print('writing flash', flush=True); time.sleep(.1); "
                       f"Path({str(marker)!r}).write_text('verified'); "
                       "print('verification completed', flush=True)")
            return real_command([sys.executable, "-u", "-c", program], **options)
        self.session._run_command = execute
        self.session.start("/dev/cu.usbserialFAKE", "pressure_v1")
        snapshot = self.finish()
        self.assertTrue(marker.is_file(), snapshot)
        self.assertEqual(process_list[-1].returncode, 0)
        self.assertTrue(sink.closed)
        self.assertEqual(snapshot["status"], "failed")
        self.assertIsNone(snapshot["result"])
        self.factory.assert_not_called()
        evidence = json.loads((self.session.path / "manifest.json").read_text())
        self.assertTrue(evidence["write_verified"])
        self.assertEqual(evidence["status"], "failed")
        self.assertIn("No space", evidence["log_errors"][0])
        self.assertTrue(any("verification completed" in line for line in snapshot["log"]))

    def test_native_tool_failure_stops_without_trying_alternative_or_device(self):
        candidates = [dict(self.session.toolchain),
                      {"executable": "/intel/avrdude", "config": "/intel/avrdude.conf"}]
        attempted = []
        def execute(args, **options):
            attempted.append(args)
            raise RuntimeError("native installation unavailable")
        self.session.toolchain = None
        self.session._run_command = execute
        with patch("sspp_experiments.temperature_pressure.firmware.avrdude_candidates", return_value=candidates):
            self.session.start("/dev/cu.usbserialFAKE", "pressure_v1")
            snapshot = self.finish()
        self.assertEqual(snapshot["status"], "failed", snapshot)
        self.assertEqual(len(attempted), 1)
        self.assertEqual(attempted[0][0], str(self.uploader))
        self.factory.assert_not_called()

    def test_explicit_intel_toolchain_is_rejected_without_starting_it(self):
        self.uploader.write_bytes(b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x01000007) + b"\0" * 24)
        self.session._run_command = Mock()
        self.session.start("/dev/cu.usbserialFAKE", "temperature_v1")
        snapshot = self.finish()
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("启动前拒绝", snapshot["message"])
        self.session._run_command.assert_not_called()
        self.factory.assert_not_called()
        self.assertFalse((self.session.path / "flash_before.bin").exists())

    def test_static_native_architecture_and_universal_binary_detection(self):
        self.assertEqual(validate_avrdude_toolchain(self.session.toolchain), self.session.toolchain)
        header = b"\xca\xfe\xba\xbe" + struct.pack(">I", 2)
        table = b"".join(struct.pack(">IIIII", cpu, 0, 0, 0, 0) for cpu in (0x01000007, 0x0100000C))
        self.uploader.write_bytes(header + table)
        self.assertEqual(_macho_cpu_types(self.uploader), {0x01000007, 0x0100000C})
        self.assertEqual(validate_avrdude_toolchain(self.session.toolchain), self.session.toolchain)
        self.uploader.write_text("#!/bin/sh\nexec /intel/avrdude\n")
        with self.assertRaisesRegex(RuntimeError, "启动前拒绝"):
            validate_avrdude_toolchain(self.session.toolchain)
        self.uploader.write_bytes(header + table[:10])
        self.assertEqual(_macho_cpu_types(self.uploader), set())

    def test_discovery_prefers_native_homebrew_without_executing_candidates(self):
        checked = []
        def validate(tool):
            checked.append(tool["executable"])
            if tool["executable"] != "/opt/homebrew/bin/avrdude":
                raise RuntimeError("not native")
            return tool
        with patch("sspp_experiments.temperature_pressure.firmware.validate_avrdude_toolchain", side_effect=validate), \
             patch("sspp_experiments.temperature_pressure.firmware.subprocess.Popen") as spawn:
            candidates = avrdude_candidates()
        self.assertEqual(candidates, [{"executable": "/opt/homebrew/bin/avrdude", "config": "/opt/homebrew/etc/avrdude.conf"}])
        self.assertEqual(checked[0], "/opt/homebrew/bin/avrdude")
        self.assertFalse(any("Arduino15" in path for path in checked))
        spawn.assert_not_called()

    def test_bundle_source_hash_and_bootloader_boundaries_are_checked(self):
        for protocol in ("pressure_v1", "temperature_v1"):
            info = firmware_bundle(self.root, protocol)
            self.assertLess(max(_hex_memory(info["path"])), APPLICATION_BYTES)
        image = self.root / "firmware/temperature_pressure/temperature_v1.hex"
        image.write_text(":01800000FF80\n:00000001FF\n")
        with self.assertRaisesRegex(ValueError, "来源校验"):
            firmware_bundle(self.root, "temperature_v1")
        with self.assertRaises(ValueError):
            _hex_memory(image)


if __name__ == "__main__":
    unittest.main()
