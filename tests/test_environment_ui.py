"""显式环境初始化的离线 Qt 回归；不打开 USRP、串口或真实采集进程。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6 import QtCore, QtWidgets

from qt_test_cleanup import dispose_closed_windows
from sspp_desktop.window import MainWindow
import test_liquid_temperature_mainwindow as liquid_fixtures
from test_temperature_ui import FakePreparation
from test_temperature_capture import FakeReader
from sspp_experiments.temperature_pressure import SerialCaptureSession


class EnvironmentService(liquid_fixtures.LiquidFakeService):
    """可阻塞、失败和取消的初始化假服务，保留真实异步调用边界。"""

    def __init__(self):
        super().__init__()
        self.initialize_release = threading.Event()
        self.initialize_release.set()
        self.dispatch_release = threading.Event()
        self.dispatch_release.set()
        self.initialize_dispatched = threading.Event()
        self.initialize_entered = threading.Event()
        self.initialize_thread_ids = []
        self.hardware_open_calls = []
        self.fail_initialization = False
        self.fail_kinds = set()
        self.kind_releases = {}
        self.cancel_calls = 0
        self.shutdown_calls = 0

    def refresh_profiles(self):
        return [{"id": "own", "name": "测试设备 A", "channel": 0},
                {"id": "teacher", "name": "测试设备 B", "channel": 0}]

    def initialize_environment(self, target, *, cancel_event=None):
        kind = target["kind"]
        self.initialize_calls.append(dict(target))
        self.initialize_thread_ids.append(threading.get_ident())
        self.initialize_dispatched.set()
        if not self.dispatch_release.wait(4):
            raise RuntimeError("假初始化调度未被测试释放")
        if self.closed or (cancel_event is not None and cancel_event.is_set()):
            self._batch_cancelled = True
            if self.environments[kind]["state"] != "ready":
                self.environments[kind] = {"state": "cancelled", "target": None, "message": "已取消"}
            return self.environment_snapshot(kind)
        self._active_kind = kind
        self.hardware_open_calls.append(dict(target))
        self.environments[kind] = {"state": "initializing", "target": dict(target), "message": "检查中"}
        self.initialize_entered.set()
        if not self.kind_releases.get(kind, self.initialize_release).wait(4):
            raise RuntimeError("假初始化未被测试释放")
        if self.closed or (cancel_event is not None and cancel_event.is_set()):
            self.environments[kind] = {"state": "cancelled", "target": None, "message": "已取消"}
        if self.environments[kind]["state"] == "cancelled":
            return self.environment_snapshot(kind)
        if self.fail_initialization or kind in self.fail_kinds:
            self.environments[kind] = {"state": "failed", "target": dict(target), "message": "测试设备不可用"}
            return self.environment_snapshot(kind)
        self.environments[kind] = {"state": "ready", "target": dict(target),
                            "message": "初始化检查通过，设备已释放；未开始采集"}
        return self.environment_snapshot(kind)

    def cancel_environment_initialization(self):
        self.cancel_calls += 1
        super().cancel_environment_initialization()
        self.initialize_release.set()
        for release in self.kind_releases.values():
            release.set()
        return self.environment_snapshot()

    def shutdown(self):
        self.shutdown_calls += 1
        if self.environment["state"] == "initializing":
            self.cancel_environment_initialization()
        self.dispatch_release.set()
        self.initialize_release.set()
        super().shutdown()


class EnvironmentUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.port_patch = patch("sspp_desktop.temperature_widget.list_serial_ports",
                                return_value=["/dev/cu.usbserial-TEST_A", "/dev/cu.usbserial-TEST_B"])
        self.port_patch.start()
        self.addCleanup(self.port_patch.stop)
        self.dialog_ports = patch("sspp_desktop.environment_dialog.list_serial_ports",
                                  return_value=["/dev/cu.usbserial-TEST_A", "/dev/cu.usbserial-TEST_B"])
        self.dialog_ports.start()
        self.addCleanup(self.dialog_ports.stop)
        # 数据源子进程与本测试的设备门禁无关，隔离本机 rf-analysis 配置。
        self.analysis_patch = patch("sspp_desktop.analysis_workbench_widget.AnalysisWorkbenchWidget.refresh_sources")
        self.analysis_patch.start()
        self.addCleanup(self.analysis_patch.stop)
        self.service = EnvironmentService()
        self.window = MainWindow(self.service)
        self.preparation = FakePreparation()
        self.window.temperature_workspace.preparation = self.preparation
        self.window.timer.stop()
        self.window.show()
        self.pump(lambda: self.window.profile.count() == 2 and not self.window.runner.pending)
        # 环境门禁测试提供有效材料名称，避免先被样品表单校验截住。
        self.window.material_workspace.material_name.setText("offline_sample")

    def tearDown(self):
        if self.window.environment_dialog is not None:
            self.window.environment_dialog.reject()
        self.service.dispatch_release.set()
        self.service.initialize_release.set()
        self.service.preflight_release.set()
        self.pump(lambda: not self.window.runner.pending and not self.window.liquid_workspace.is_busy)
        if self.window.isVisible():
            self.service.stop()
            self.window._start_pending = False
            self.window._apply_status(self.service.snapshot())
            with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
                self.window.close()
            self.pump(lambda: self.service.closed and not self.window.isVisible())
        dispose_closed_windows()

    def pump(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Qt environment condition timed out")

    def open_dialog(self):
        self.window.initialize_button.click()
        self.pump(lambda: self.window.environment_dialog is not None and self.window.environment_dialog.isVisible())
        return self.window.environment_dialog

    def request_initialize(self, profile=None, both=False):
        dialog = self.open_dialog()
        if not both:
            dialog.usrp_check.setChecked(self.window.experiment_id != "temperature")
            dialog.serial_check.setChecked(self.window.experiment_id == "temperature")
        if profile is not None:
            dialog.profile_combo.setCurrentIndex(dialog.profile_combo.findData(profile))
        dialog.apply_button.click()
        return dialog

    def initialize(self, profile=None):
        self.request_initialize(profile)
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)

    def begin_blocked_initialization(self):
        self.service.initialize_release.clear()
        self.request_initialize()
        self.pump(lambda: self.service.initialize_entered.is_set()
                  and "initialize_environment" in self.window.runner.pending)

    def select(self, key):
        self.assertTrue(self.window.select_experiment(key), self.window.notice.text())
        self.pump(lambda: not self.window.runner.pending and not self.window.liquid_workspace.is_busy)

    def prepare_serial(self):
        serial = self.window.temperature_workspace
        serial.start_preparation(upload=False)
        self.assertTrue(serial.is_running)
        self.preparation.finish()
        serial._poll()
        self.assertTrue(serial.preparation_ready)
        self.assertFalse(serial.is_running)
        return serial

    def test_startup_stays_uninitialized_and_initialization_is_leftmost_navigation_control(self):
        window = self.window
        navigation = window.centralWidget().layout().itemAt(0).layout()
        self.assertIs(navigation.itemAt(0).widget(), window.initialize_button)
        self.assertLess(window.initialize_button.mapTo(window, QtCore.QPoint()).x(),
                        window.experiment_nav.mapTo(window, QtCore.QPoint()).x())
        self.assertEqual(window.initialize_button.text(), "初始化实验台")
        self.assertTrue(window.initialize_button.isEnabled())
        self.assertEqual(self.service.initialize_calls, [])
        self.assertIsNone(self.service.started)
        self.assertEqual(self.service.environment_snapshot()["state"], "idle")
        self.assertFalse(window._environment_ready())
        self.assertFalse(window.start_button.isEnabled())
        self.assertFalse(window.temperature_workspace.start_button.isEnabled())
        self.assertIn("未初始化", window.environment_label.text())

    def test_default_is_first_material_navigation_and_device_selection_is_global(self):
        self.assertEqual(self.window.experiment_id, "material")
        self.assertEqual(self.window.experiment_nav.currentIndex(), 0)
        self.assertEqual(self.window.config()["mode"], "sweep")
        self.assertEqual(self.window.config()["profile"], "own")
        self.assertNotIn("profile", self.window.fields)
        self.assertFalse(self.window.sidebar.isAncestorOf(self.window.profile))
        self.assertTrue(self.window.profile.isHidden())
        self.assertEqual(self.service.initialize_calls, [])

    def test_opening_and_cancelling_dialog_changes_neither_global_device_nor_readiness(self):
        for already_ready in (False, True):
            with self.subTest(already_ready=already_ready):
                if already_ready:
                    self.initialize()
                before = self.service.environment_snapshot()
                previous_calls = len(self.service.initialize_calls)
                dialog = self.open_dialog()
                self.window.open_environment_dialog()
                self.assertIs(self.window.environment_dialog, dialog)
                dialog.profile_combo.setCurrentIndex(dialog.profile_combo.findData("teacher"))
                self.assertEqual(self.window.profile.currentData(), "own")
                self.assertEqual(self.service.environment_snapshot(), before)
                self.assertEqual(len(self.service.initialize_calls), previous_calls)
                dialog.cancel_button.click()
                self.pump(lambda: self.window.environment_dialog is None)
                self.assertEqual(self.window.profile.currentData(), "own")
                self.assertEqual(self.service.environment_snapshot(), before)
                self.assertEqual(self.window._environment_ready(), already_ready)
                self.assertFalse(hasattr(self.window, "confirm"))
                self.assertEqual(len(self.service.initialize_calls), previous_calls)
                self.assertEqual(self.service.cancel_calls, 0)

    def test_confirming_teacher_keeps_one_global_device_across_drafts_and_resets(self):
        expected_gains = {"material": 31., "finger": 42.}
        # 先建立 own 的不同实验草稿，随后改变全局选择；恢复旧草稿不得带回 own。
        for experiment, gain in expected_gains.items():
            self.select(experiment)
            self.window.fields["rx_gain"].setValue(gain)
        self.select("material")
        self.initialize("teacher")
        self.assertEqual(self.service.initialize_calls, [{"kind": "usrp", "profile": "teacher"}])
        self.assertIn("测试设备 B", self.window.environment_label.text())
        for experiment, gain in expected_gains.items():
            with self.subTest(experiment=experiment):
                self.select(experiment)
                self.assertEqual(self.window.config()["profile"], "teacher")
                self.assertEqual(self.window.fields["rx_gain"].value(), gain)
                self.assertTrue(self.window._environment_ready())
                self.window.reset_parameters()
                self.assertEqual(self.window.config()["profile"], "teacher")
                self.assertTrue(self.window._environment_ready())
        self.select("temperature")
        self.select("material")
        self.assertEqual(self.window.config()["profile"], "teacher")
        self.assertEqual(len(self.service.initialize_calls), 1)
        self.assertIsNone(self.service.started)

    def test_confirming_changed_device_invalidates_old_ready_before_background_finishes(self):
        self.initialize()
        self.service.initialize_release.clear()
        dialog = self.request_initialize("teacher")
        # QDialog 的确认信号重复送达也不能启动第二项后台初始化。
        dialog.apply_button.click()
        self.pump(lambda: len(self.service.initialize_calls) == 2 and self.service.environment["state"] == "initializing")
        self.assertEqual(self.window.profile.currentData(), "teacher")
        self.assertFalse(self.window._environment_ready())
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertFalse(hasattr(self.window, "confirm"))
        self.assertNotEqual(self.service.initialize_thread_ids[-1], threading.get_ident())
        self.service.initialize_release.set()
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        self.assertTrue(self.window._environment_ready())
        self.assertEqual(self.service.initialize_calls, [{"kind": "usrp", "profile": "own"},
                                                       {"kind": "usrp", "profile": "teacher"}])
        self.assertIsNone(self.service.started)

    def test_dialog_has_both_global_devices_on_every_experiment_and_cancel_keeps_them(self):
        self.initialize("teacher")
        self.select("temperature")
        dialog = self.open_dialog()
        self.assertTrue(dialog.profile_combo.isEnabled())
        self.assertTrue(dialog.serial_check.isChecked())
        self.assertEqual(dialog.selected_profile(), "teacher")
        self.assertEqual(dialog.port_combo.currentData(), "/dev/cu.usbserial-TEST_A")
        dialog.profile_combo.setCurrentIndex(dialog.profile_combo.findData("own"))
        dialog.port_combo.setCurrentIndex(dialog.port_combo.findData("/dev/cu.usbserial-TEST_B"))
        self.assertEqual(len(self.service.initialize_calls), 1)
        dialog.cancel_button.click()
        self.pump(lambda: self.window.environment_dialog is None)
        self.assertEqual(self.window.profile.currentData(), "teacher")
        self.assertEqual(self.window.temperature_workspace.port.currentData(), "/dev/cu.usbserial-TEST_A")
        self.assertEqual(len(self.service.initialize_calls), 1)
        self.initialize()
        self.assertEqual(self.service.initialize_calls[-1]["kind"], "serial")
        self.assertEqual(self.window.profile.currentData(), "teacher")

    def test_confirmed_direct_start_cannot_bypass_uninitialized_environment(self):
        self.window.start_experiment()
        self.assertIsNone(self.service.started)
        self.assertEqual(self.service.initialize_calls, [])
        self.assertFalse(self.window._start_pending)
        self.assertIn("初始化实验台", self.window.notice.text())

    def test_explicit_button_initializes_in_background_and_then_allows_confirmed_start(self):
        self.initialize()
        self.assertEqual(self.service.initialize_calls, [{"kind": "usrp", "profile": "own"}])
        self.assertNotEqual(self.service.initialize_thread_ids[0], threading.get_ident())
        self.assertTrue(self.window._environment_ready())
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertFalse(hasattr(self.window, "confirm"))
        self.assertIsNone(self.service.started)
        self.assertIn("就绪", self.window.environment_label.text())
        self.assertIn("设备已释放", self.window.environment_label.toolTip())
        self.window.start_button.click()
        self.pump(lambda: self.service.started is not None and "start" not in self.window.runner.pending)
        self.assertTrue(self.service.started["setup_confirmed"])
        self.assertEqual(self.service.started["profile"], "own")

    def test_failed_initialization_keeps_start_disabled_and_can_retry(self):
        self.service.fail_initialization = True
        self.initialize()
        self.assertFalse(self.window._environment_ready())
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(self.window.initialize_button.isEnabled())
        self.assertIn("可重试", self.window.environment_label.text())
        self.assertIn("测试设备不可用", self.window.notice.text())
        self.service.fail_initialization = False
        self.initialize()
        self.assertEqual(len(self.service.initialize_calls), 2)
        self.assertTrue(self.window._environment_ready())
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertIsNone(self.service.started)

    def test_pending_initialization_locks_parameters_navigation_and_duplicate_handlers(self):
        self.select("finger")
        window = self.window
        window.fields["rate"].setValue(500000)
        self.begin_blocked_initialization()
        for widget in (window.config_container, window.finger_settings, window.temperature_workspace.config_group,
                       window.initialize_button, window.start_button, window.check_button, window.experiment_nav):
            self.assertFalse(widget.isEnabled())
        self.assertTrue(window.stop_button.isEnabled())
        self.assertEqual(window.stop_button.text(), "取消初始化")
        self.assertFalse(window.select_experiment("material"))
        window.experiment_nav.setCurrentIndex(0)
        self.assertEqual(window.experiment_id, "finger")
        self.assertEqual(window.experiment_nav.currentIndex(), 1)
        window.reset_parameters()
        self.assertEqual(window.fields["rate"].value(), 500000)
        window.preflight()
        self.assertNotIn("preflight", window.runner.pending)
        window.open_environment_dialog()
        self.assertIsNone(window.environment_dialog)
        window.initialize_environment()
        window.start_experiment()
        self.assertEqual(len(self.service.initialize_calls), 1)
        self.assertIsNone(self.service.started)
        self.service.initialize_release.set()
        self.pump(lambda: not window.runner.pending)
        self.assertTrue(window.config_container.isEnabled())
        self.assertTrue(window.experiment_nav.isEnabled())

    def test_global_stop_cancels_initialization_without_stopping_a_capture(self):
        self.begin_blocked_initialization()
        self.window.stop_button.click()
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        self.assertEqual(self.service.cancel_calls, 1)
        self.assertFalse(self.service.stopped)
        self.assertEqual(self.service.environment_snapshot()["state"], "cancelled")
        self.assertFalse(self.window._environment_ready())
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(self.window.initialize_button.isEnabled())
        self.assertIsNone(self.service.started)

    def test_cancel_before_worker_opens_hardware_is_not_lost_when_worker_later_runs(self):
        self.service.dispatch_release.clear()
        self.request_initialize()
        self.pump(lambda: self.service.initialize_dispatched.is_set())
        self.assertTrue(self.window._environment_pending)
        self.assertEqual(self.service.hardware_open_calls, [])
        self.window.stop_button.click()
        self.pump(lambda: self.service.cancel_calls == 1)
        self.assertTrue(self.window._environment_cancel_event.is_set())
        self.service.dispatch_release.set()
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        self.assertEqual(self.service.hardware_open_calls, [])
        self.assertEqual(self.service.environment_snapshot()["state"], "cancelled")
        self.assertFalse(self.window._environment_ready())
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertIsNone(self.service.started)

    def test_cancel_after_worker_returns_ready_before_gui_delivery_preserves_completed_ready(self):
        self.request_initialize()
        # 等后台方法真正返回，但不派送 Qt finished 信号，确定复现交界窗口。
        self.assertTrue(self.window.runner.control.waitForDone(1000))
        self.assertEqual(self.service.environment_snapshot("usrp")["state"], "ready")
        self.assertIn("initialize_environment", self.window.runner.pending)
        self.assertTrue(self.window._environment_pending)
        self.assertFalse(self.window.start_button.isEnabled())
        # 已完成并释放的设备不因取消剩余检查而失效。
        with patch.object(self.service, "cancel_environment_initialization",
                          return_value=self.service.environment_snapshot()) as cancel:
            self.window.stop_button.click()
            self.assertTrue(self.window._environment_cancel_event.is_set())
            self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
            cancel.assert_called_once_with()
        self.assertEqual(self.service.environment_snapshot("usrp")["state"], "ready")
        self.assertTrue(self.window._environment_ready())
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertIn("取消", self.window.notice.text())
        self.assertIsNone(self.service.started)

    def test_close_pending_initialization_calls_shutdown_and_waits_for_cancellation(self):
        self.begin_blocked_initialization()
        with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.window.close()
        self.pump(lambda: self.service.closed and not self.window.isVisible() and not self.window.runner.pending)
        self.assertEqual(self.service.shutdown_calls, 1)
        self.assertEqual(self.service.cancel_calls, 1)
        self.assertTrue(self.service.initialize_release.is_set())
        self.assertFalse(self.window._environment_pending)
        self.assertFalse(self.window._environment_ready())
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertIsNone(self.service.started)

    def test_changing_profile_invalidates_ready_and_confirmation_without_automatic_retry(self):
        self.initialize()
        self.window.profile.setCurrentIndex(self.window.profile.findData("teacher"))
        self.assertFalse(self.window._environment_ready())
        self.assertFalse(hasattr(self.window, "confirm"))
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertEqual(len(self.service.initialize_calls), 1)
        self.window.profile.setCurrentIndex(self.window.profile.findData("own"))
        self.assertFalse(self.window._environment_ready())
        self.initialize()
        self.assertTrue(self.window._environment_ready())

    def test_same_profile_rf_experiment_switch_retains_check_but_clears_confirmation(self):
        self.initialize()
        for experiment in ("finger", "liquid", "material"):
            with self.subTest(experiment=experiment):
                self.select(experiment)
                self.assertEqual(self.window.profile.currentData(), "own")
                self.assertTrue(self.window._environment_ready())
                self.assertFalse(hasattr(self.window, "confirm"))
                self.assertEqual(len(self.service.initialize_calls), 1)
                self.assertIsNone(self.service.started)

    def test_port_change_requires_reinitialization_but_mode_change_only_requires_firmware_preparation(self):
        self.select("temperature")
        serial = self.window.temperature_workspace
        for field in (serial.port, serial.protocol):
            with self.subTest(field="port" if field is serial.port else "protocol"):
                self.initialize()
                self.assertTrue(self.window._environment_ready())
                self.assertTrue(serial.verify_button.isEnabled())
                self.assertFalse(serial.start_button.isEnabled())
                self.assertEqual(self.service.initialize_calls[-1],
                                 {"kind": "serial", "port": serial.port.currentData(),
                                  "protocol": serial.protocol.currentData()})
                self.prepare_serial()
                field.setCurrentIndex(1 - field.currentIndex())
                self.assertEqual(self.window._environment_ready(), field is serial.protocol)
                self.assertEqual(serial._environment_ready, field is serial.protocol)
                self.assertFalse(serial.preparation_ready)
                self.assertFalse(hasattr(serial, "confirm"))
                self.assertFalse(serial.start_button.isEnabled())
        self.assertEqual(len(self.service.initialize_calls), 2)

    def test_serial_direct_start_is_gated_even_with_valid_fields_and_confirmation(self):
        self.select("temperature")
        serial = self.window.temperature_workspace
        serial.sample_name.setText("test_sample")
        with patch.object(serial.session, "start") as start:
            serial.start_capture()
            start.assert_not_called()
        self.assertIn("初始化实验台", serial.status.text())
        self.assertFalse(serial.start_button.isEnabled())
        self.initialize()
        self.assertTrue(serial.verify_button.isEnabled())
        self.assertFalse(serial.start_button.isEnabled())
        self.assertFalse(hasattr(serial, "confirm"))
        self.assertEqual(self.service.started, None)

    def test_switching_hardware_kind_keeps_each_completed_check(self):
        self.initialize()
        self.select("temperature")
        self.assertFalse(self.window._environment_ready())
        self.assertFalse(self.window.temperature_workspace.start_button.isEnabled())
        self.initialize()
        self.assertTrue(self.window._environment_ready())
        self.select("finger")
        self.assertTrue(self.window._environment_ready())
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertTrue(self.window._environment_ready("serial"))
        self.select("temperature")
        self.assertTrue(self.window.temperature_workspace.verify_button.isEnabled())
        self.assertFalse(self.window.temperature_workspace.start_button.isEnabled())
        self.assertEqual(len(self.service.initialize_calls), 2)

    def test_batch_dialog_commits_both_devices_and_runs_serial_before_rf(self):
        dialog = self.open_dialog()
        self.assertTrue(dialog.usrp_check.isChecked())
        self.assertTrue(dialog.serial_check.isChecked())
        dialog.profile_combo.setCurrentIndex(dialog.profile_combo.findData("teacher"))
        dialog.port_combo.setCurrentIndex(dialog.port_combo.findData("/dev/cu.usbserial-TEST_B"))
        expected_serial = dialog.selected_serial()
        dialog.apply_button.click()
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        self.assertEqual(self.service.initialize_calls,
                         [expected_serial, {"kind": "usrp", "profile": "teacher"}])
        self.assertEqual(self.window._environment_target("serial"), expected_serial)
        self.assertEqual(self.window.profile.currentData(), "teacher")
        self.assertEqual(self.service.environment_snapshot()["state"], "ready")
        self.assertEqual(self.window.windowTitle(), "多传感器实验台")
        self.assertIn("Arduino", self.window.environment_label.text())
        for key in ("finger", "liquid", "temperature", "material"):
            self.select(key)
            self.assertTrue(self.window._environment_ready())

    def test_initializing_unchanged_protocol_preserves_user_wiring_and_sample_fields(self):
        serial = self.window.temperature_workspace
        serial.protocol.setCurrentIndex(serial.protocol.findData("temperature_v1"))
        serial.channel.setText("A2")  # 真实接线记录即使与另一协议默认值相同，也不能被初始化覆盖。
        serial.sample_name.setText("existing_sample")
        serial.notes.setText("保留现有样品和接线")
        self.request_initialize(both=True)
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        self.assertEqual(serial.protocol.currentData(), "temperature_v1")
        self.assertEqual(serial.channel.text(), "A2")
        self.assertEqual(serial.sample_name.text(), "existing_sample")
        self.assertEqual(serial.notes.text(), "保留现有样品和接线")
        self.assertTrue(self.window._environment_ready("serial"))

    def test_missing_rf_configuration_does_not_block_serial_capture(self):
        self.window.profile.clear()
        dialog = self.open_dialog()
        self.assertFalse(dialog.usrp_check.isChecked())
        self.assertFalse(dialog.usrp_check.isEnabled())
        self.assertTrue(dialog.serial_check.isChecked())
        dialog.apply_button.click()
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        self.assertIsNone(self.service.workbench_calls[-1]["usrp"])
        self.assertFalse(self.window.start_button.isEnabled())
        self.select("temperature")
        serial = self.window.temperature_workspace
        self.assertTrue(serial.verify_button.isEnabled())
        self.assertFalse(serial.start_button.isEnabled())
        self.prepare_serial()
        self.assertTrue(serial.start_button.isEnabled())
        self.window.material_workspace.material_name.setText("offline_material")
        self.window.start_experiment()
        self.assertIsNone(self.service.started)
        serial.sample_name.setText("offline_sample")
        with tempfile.TemporaryDirectory() as temporary:
            reader = FakeReader([b"100,2.70\n150,2.80\n"])
            serial.session = SerialCaptureSession(temporary, lambda port, baud: reader)
            serial.start_capture()
            self.pump(lambda: serial.session.snapshot()["rows"] == 2)
            serial._poll()
            self.assertIn("A2", serial.lines)
            self.assertEqual(list(serial.lines["A2"][0].yData), [2.7, 2.8])
            serial.stop_capture()
            self.pump(lambda: not serial.is_running)
            self.assertTrue(reader.closed)
        self.assertIn("已就绪设备可开始", self.service.environment_snapshot()["message"])

    def test_missing_serial_port_does_not_block_rf_capture(self):
        with patch("sspp_desktop.environment_dialog.list_serial_ports", return_value=[]):
            dialog = self.open_dialog()
        self.assertFalse(dialog.serial_check.isChecked())
        self.assertFalse(dialog.serial_check.isEnabled())
        self.assertIn("跳过", dialog.port_note.text())
        dialog.apply_button.click()
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        self.assertIsNone(self.service.workbench_calls[-1]["serial"])
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertFalse(self.window.temperature_workspace.start_button.isEnabled())
        self.window.material_workspace.material_name.setText("offline_material")
        self.window.start_experiment()
        self.pump(lambda: self.service.started is not None and not self.window._start_pending)
        self.assertEqual(self.service.started["profile"], "own")

    def test_one_failed_device_does_not_block_the_other_ready_device(self):
        for failed, usable in (("usrp", "serial"), ("serial", "usrp")):
            with self.subTest(failed=failed):
                self.service.invalidate_environment()
                self.service.fail_kinds = {failed}
                self.select("material")
                self.request_initialize(both=True)
                self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
                self.assertEqual(self.service.environment_snapshot()["state"], "partial")
                self.assertFalse(self.window._environment_ready(failed))
                self.assertTrue(self.window._environment_ready(usable))
                self.assertIn("已就绪设备可开始实验", self.window.notice.text())
                self.select("temperature" if usable == "serial" else "material")
                button = self.window.temperature_workspace.verify_button if usable == "serial" else self.window.start_button
                self.assertTrue(button.isEnabled())

    def test_configuration_changes_invalidate_only_the_changed_device(self):
        self.request_initialize(both=True)
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        serial = self.window.temperature_workspace
        self.window.profile.setCurrentIndex(self.window.profile.findData("teacher"))
        self.assertFalse(self.window._environment_ready("usrp"))
        self.assertTrue(self.window._environment_ready("serial"))
        self.assertFalse(hasattr(serial, "confirm"))
        self.initialize("teacher")
        serial.port.setCurrentIndex(1 - serial.port.currentIndex())
        self.assertTrue(self.window._environment_ready("usrp"))
        self.assertFalse(self.window._environment_ready("serial"))
        self.assertFalse(hasattr(self.window, "confirm"))
        self.assertFalse(hasattr(serial, "confirm"))

    def test_cancel_batch_preserves_serial_completed_before_rf_and_releases_global_lock(self):
        self.service.kind_releases["usrp"] = threading.Event()
        self.request_initialize(both=True)
        self.pump(lambda: self.service.environment_snapshot("usrp")["state"] == "initializing")
        self.assertEqual(self.service.environment_snapshot("serial")["state"], "ready")
        self.assertFalse(self.window.temperature_workspace.start_button.isEnabled())
        self.assertFalse(self.window.start_button.isEnabled())
        self.window.stop_button.click()
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        self.assertEqual(self.service.environment_snapshot("serial")["state"], "ready")
        self.assertEqual(self.service.environment_snapshot("usrp")["state"], "cancelled")
        self.assertTrue(self.window._environment_ready("serial"))
        self.assertFalse(self.window._environment_ready("usrp"))
        self.assertIn("就绪状态保留", self.window.notice.text())
        self.select("temperature")
        self.assertTrue(self.window.temperature_workspace.verify_button.isEnabled())
        self.assertFalse(self.window.temperature_workspace.start_button.isEnabled())
        self.assertIsNone(self.service.started)

    def test_refreshing_same_serial_port_keeps_ready_and_changed_port_invalidates_only_serial(self):
        self.request_initialize(both=True)
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        serial = self.window.temperature_workspace
        serial.refresh_ports()
        self.assertTrue(self.window._environment_ready("serial"))
        self.assertFalse(hasattr(serial, "confirm"))
        with patch("sspp_desktop.temperature_widget.list_serial_ports", return_value=["/dev/cu.usbserial-TEST_B"]):
            serial.refresh_ports()
        self.assertFalse(self.window._environment_ready("serial"))
        self.assertFalse(hasattr(serial, "confirm"))
        self.assertTrue(self.window._environment_ready("usrp"))

    def test_all_missing_devices_leave_history_available_without_claiming_ready(self):
        self.window.profile.clear()
        with patch("sspp_desktop.environment_dialog.list_serial_ports", return_value=[]):
            dialog = self.open_dialog()
        self.assertFalse(dialog.usrp_check.isChecked())
        self.assertFalse(dialog.serial_check.isChecked())
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.cancel_button.click()
        self.pump(lambda: self.window.environment_dialog is None)
        self.assertFalse(self.window._environment_ready("usrp"))
        self.assertFalse(self.window._environment_ready("serial"))
        self.assertTrue(self.window.archive_button.isEnabled())
        self.window.archive_button.click()
        self.assertEqual(self.window.tabs.currentIndex(), 1)
        self.assertEqual(self.service.initialize_calls, [])

    def test_system_virtual_ports_do_not_enable_or_select_arduino(self):
        system_ports = ["/dev/cu.Bluetooth-Incoming-Port", "/dev/cu.debug-console", "/dev/cu.wlan-debug"]
        with patch("sspp_desktop.temperature_widget.list_serial_ports", return_value=system_ports), \
             patch("sspp_desktop.environment_dialog.list_serial_ports", return_value=system_ports):
            self.window.temperature_workspace.refresh_ports()
            dialog = self.open_dialog()
        self.assertEqual(self.window.temperature_workspace.port.count(), 0)
        self.assertEqual(dialog.port_combo.count(), 0)
        self.assertFalse(dialog.serial_check.isChecked())
        self.assertFalse(dialog.serial_check.isEnabled())
        self.assertIsNone(dialog.selected_targets()["serial"])
        self.assertIn("系统虚拟端口", dialog.port_note.text())
        self.assertIn("不识别固件、不烧录", dialog.explanation.text())
        dialog.apply_button.click()
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        self.assertEqual(self.service.initialize_calls, [{"kind": "usrp", "profile": "own"}])
        self.assertTrue(self.window._environment_ready("usrp"))
        self.assertFalse(self.window._environment_ready("serial"))

    def test_port_name_filter_only_offers_usb_candidates_and_excludes_bluetooth_named_devices(self):
        ports = ["/dev/cu.Bluetooth-Incoming-Port", "/dev/cu.usbmodemFAKE", "/dev/cu.custom-controller", "/dev/cu.ULTFIELD1", "/dev/cu.usbserial-TEST_A"]
        with patch("sspp_desktop.temperature_widget.list_serial_ports", return_value=ports), \
             patch("sspp_desktop.environment_dialog.list_serial_ports", return_value=ports):
            self.window.temperature_workspace.refresh_ports()
            dialog = self.open_dialog()
        expected = [ports[1], ports[4]]
        self.assertEqual([dialog.port_combo.itemData(i) for i in range(dialog.port_combo.count())], expected)
        self.assertEqual([self.window.temperature_workspace.port.itemData(i) for i in range(2)], expected)
        self.assertTrue(dialog.serial_check.isChecked())
        dialog.cancel_button.click()
        self.pump(lambda: self.window.environment_dialog is None)

    def test_firmware_preparation_locks_global_navigation_initialization_and_rf_controls(self):
        self.select("temperature")
        self.initialize()
        serial = self.window.temperature_workspace
        self.window.fields["rate"].setValue(500000)
        self.window._message("上一次样品名称错误")
        serial.start_preparation(upload=False)
        self.assertTrue(serial.is_running)
        self.assertFalse(self.window.notice.isVisible())
        self.assertIn("固件准备中", self.window.environment_label.text())
        self.assertIn("固件准备", self.window.status_label.text())
        for control in (self.window.initialize_button, self.window.experiment_nav,
                        self.window.start_button, self.window.check_button, serial.config_group):
            self.assertFalse(control.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())
        self.assertEqual(self.window.stop_button.text(), "取消固件准备")
        self.assertFalse(self.window.select_experiment("material"))
        self.window.experiment_nav.setCurrentIndex(0)
        self.assertEqual(self.window.experiment_id, "temperature")
        self.window.preflight()
        self.assertNotIn("preflight", self.window.runner.pending)
        self.window.reset_parameters()
        self.assertEqual(self.window.fields["rate"].value(), 500000)
        before = len(self.service.initialize_calls)
        self.window.open_environment_dialog()
        self.window.initialize_environment()
        self.assertIsNone(self.window.environment_dialog)
        self.assertEqual(len(self.service.initialize_calls), before)
        self.window.start_experiment()
        self.assertIsNone(self.service.started)
        self.preparation.write_active = True
        self.window.stop_button.click()
        self.assertTrue(self.preparation.stop_requested)
        self.assertTrue(serial.is_running)
        self.assertIn("请勿拔线", serial.status.text())
        self.preparation.write_active = False
        self.preparation.finish("cancelled")
        serial._poll()
        self.assertTrue(self.window.experiment_nav.isEnabled())
        self.assertTrue(self.window.initialize_button.isEnabled())
        self.assertFalse(serial.start_button.isEnabled())
        self.assertFalse(self.service.stopped)

    def test_successful_preparation_and_recording_clear_stale_notice_and_draw_received_data(self):
        self.select("temperature")
        self.initialize()
        self.window._message("之前的错误提示")
        serial = self.prepare_serial()
        self.assertFalse(self.window.notice.isVisible())
        self.assertIn("数据已验证", self.window.environment_label.text())
        serial.sample_name.clear()
        serial.start_capture()
        self.assertIn("样品名称", self.window.notice.text())
        self.assertTrue(self.window.notice.isVisible())
        serial.sample_name.setText("test sample")
        with tempfile.TemporaryDirectory() as temporary:
            reader = FakeReader([b"100,2.71\n200,2.82\n300,2.72\n"])
            serial.session = SerialCaptureSession(temporary, lambda port, baud: reader)
            serial.start_capture()
            self.assertFalse(self.window.notice.isVisible())
            self.pump(lambda: serial.session.snapshot()["rows"] == 3)
            serial._poll()
            self.assertEqual(list(serial.lines["A2"][0].yData), [2.71, 2.82, 2.72])
            self.window.stop_button.click()
            self.pump(lambda: not serial.is_running)
            self.assertEqual(serial.session.snapshot()["status"], "complete")
        self.assertTrue(serial.preparation_ready)
        self.assertTrue(self.window.experiment_nav.isEnabled())

    def test_reinitializing_same_serial_target_requires_fresh_data_verification(self):
        self.request_initialize(both=True)
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        self.select("temperature")
        serial = self.prepare_serial()
        self.assertTrue(self.window._environment_ready("usrp"))
        self.window.initialize_environment({"serial": self.window._environment_target("serial"), "usrp": None})
        self.assertFalse(serial.preparation_ready)
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        self.assertTrue(self.window._environment_ready("serial"))
        self.assertTrue(self.window._environment_ready("usrp"))
        self.assertTrue(serial.verify_button.isEnabled())
        self.assertFalse(serial.start_button.isEnabled())
        self.assertIn("端口已检查", self.window.environment_label.text())
        self.assertNotIn("数据已验证", self.window.environment_label.text())

    def test_usrp_only_initialization_temporarily_locks_but_preserves_serial_verification(self):
        self.select("temperature")
        self.initialize()
        serial = self.prepare_serial()
        original = dict(serial.preparation.snapshot()["result"])
        self.service.initialize_release.clear()
        self.window.initialize_environment({"usrp": self.window._environment_target("usrp"), "serial": None})
        self.pump(lambda: self.service.environment_snapshot("usrp")["state"] == "initializing")
        self.assertTrue(serial.preparation_ready)
        self.assertEqual(serial.preparation.snapshot()["result"], original)
        self.assertFalse(serial.upload_button.isEnabled())
        self.assertFalse(serial.start_button.isEnabled())
        self.service.initialize_release.set()
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        self.assertTrue(serial.preparation_ready)
        self.assertEqual(serial.preparation.snapshot()["result"], original)
        self.assertTrue(serial.start_button.isEnabled())

    def test_close_waits_for_firmware_write_to_finish(self):
        self.select("temperature")
        self.initialize()
        serial = self.window.temperature_workspace
        serial.start_preparation(upload=False)
        self.preparation.write_active = True
        with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.window.close()
        self.app.processEvents()
        self.assertTrue(self.window.isVisible())
        self.assertTrue(serial.is_running)
        self.assertTrue(self.preparation.stop_requested)
        self.preparation.write_active = False
        self.preparation.finish("cancelled")
        self.pump(lambda: self.service.closed and not self.window.isVisible())

    def test_capture_failure_invalidates_only_its_device(self):
        self.request_initialize(both=True)
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        self.window.temperature_workspace.environment_invalidated.emit()
        self.assertTrue(self.window._environment_ready("usrp"))
        self.assertFalse(self.window._environment_ready("serial"))
        self.request_initialize(both=True)
        self.pump(lambda: not self.window.runner.pending and not self.window._environment_pending)
        self.window.material_workspace.material_name.setText("offline_material")
        with patch.object(self.service, "start", side_effect=RuntimeError("fake RF capture failure")):
            self.window.start_experiment()
            self.pump(lambda: "start" not in self.window.runner.pending)
        self.assertFalse(self.window._environment_ready("usrp"))
        self.assertTrue(self.window._environment_ready("serial"))


if __name__ == "__main__":
    unittest.main()
