"""档案刷新合并与保存后定位；真实 Qt TaskRunner，fake 服务，无设备操作。"""
from copy import deepcopy
import threading
import unittest
from unittest.mock import patch

import test_experiment_navigation as navigation


class HistoryRefreshQueueTests(unittest.TestCase):
    setUpClass = navigation.ExperimentNavigationTests.__dict__["setUpClass"]
    pump = navigation.ExperimentNavigationTests.pump

    def setUp(self):
        navigation.ExperimentNavigationTests.setUp(self)
        self.releases = []
        self.sources = patch.object(self.window.analysis_workspace, "refresh_sources")
        self.sources.start()
        self.service.details = lambda key, include_curve=True: {
            "key": key, "curve_loaded": include_curve, "metadata": {}, "files": [], "context": {}, "curve": {}}

    def tearDown(self):
        for release in self.releases:
            release.set()
        self.sources.stop()
        navigation.ExperimentNavigationTests.tearDown(self)

    def add_record(self, name):
        row = deepcopy(self.service.rows[0])
        row.update(key="runs/" + name, label=name)
        self.service.rows.append(row)
        return row["key"]

    def delayed_history(self, fail_first=False):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []

        def history():
            rows = deepcopy(self.service.rows)
            calls.append(rows)
            if len(calls) == 1:
                entered.set()
                if not release.wait(4):
                    raise RuntimeError("test release timed out")
                if fail_first:
                    raise RuntimeError("older history read failed")
            return rows

        self.service.history = history
        return entered, release, calls

    def test_saved_key_survives_pending_read_and_ordinary_refresh(self):
        entered, release, calls = self.delayed_history()
        with patch.object(self.window, "_render_history_rows", wraps=self.window._render_history_rows) as rendered:
            self.window.refresh_history()
            self.pump(entered.is_set)
            key = self.add_record("saved_after_read_started")
            self.window.refresh_history(select_key=key)
            self.window.refresh_history(False)
            release.set()
            self.pump(lambda: not self.window.runner.pending)
            self.assertEqual(len(calls), 2)
            self.assertEqual(rendered.call_count, 1, "obsolete snapshot must not render")
            self.assertEqual(self.window._selected_history_key, key)
            self.assertEqual(self.window.selected_details["key"], key)

    def test_many_requests_coalesce_and_latest_explicit_key_wins(self):
        entered, release, calls = self.delayed_history()
        first = self.add_record("first")
        last = self.add_record("last")
        self.window.refresh_history(first)
        self.pump(entered.is_set)
        for _ in range(10):
            self.window.refresh_history(first)
        self.window.refresh_history(last)
        self.window.refresh_history()
        release.set()
        self.pump(lambda: not self.window.runner.pending)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.window._selected_history_key, last)

    def test_failed_older_read_still_flushes_new_request(self):
        entered, release, calls = self.delayed_history(fail_first=True)
        with patch.object(self.window, "_error") as errors:
            self.window.refresh_history()
            self.pump(entered.is_set)
            key = self.add_record("saved_after_failed_read")
            self.window.refresh_history(key)
            release.set()
            self.pump(lambda: not self.window.runner.pending)
            self.assertEqual(len(calls), 2)
            self.assertEqual(self.window._selected_history_key, key)
            errors.assert_not_called()

    def test_latest_failure_is_reported_once_without_automatic_retry(self):
        with patch.object(self.service, "history", side_effect=RuntimeError("read failed")) as history:
            with patch.object(self.window, "_error") as errors:
                self.window.refresh_history()
                self.pump(lambda: not self.window.runner.pending)
                self.app.processEvents()
                history.assert_called_once()
                errors.assert_called_once()
                self.assertFalse(self.window._history_refresh_requested)

    def test_full_queue_retries_on_idle_and_keeps_selection(self):
        release = threading.Event()
        self.releases.append(release)
        for index in range(4):
            self.assertTrue(self.window.runner.submit("occupy:" + str(index), lambda: release.wait(4)))
        key = self.add_record("saved_while_queue_full")
        with patch.object(self.service, "history", wraps=self.service.history) as history:
            self.window.refresh_history(key)
            self.assertNotIn("history", self.window.runner.pending)
            self.assertTrue(self.window._history_refresh_requested)
            release.set()
            self.pump(lambda: self.window._selected_history_key == key and not self.window.runner.pending)
            history.assert_called_once()

    def test_close_discards_queued_refresh_and_late_snapshot(self):
        entered, release, calls = self.delayed_history()
        self.window.runner.idle.disconnect(self.window._finish_close)
        try:
            with patch.object(self.window, "_render_history_rows") as rendered:
                self.window.refresh_history()
                self.pump(entered.is_set)
                self.window.refresh_history(self.add_record("queued_before_close"))
                self.window._close_requested = True
                self.window.runner.closing = True
                release.set()
                self.pump(lambda: not self.window.runner.pending)
                self.assertEqual(len(calls), 1)
                rendered.assert_not_called()
                self.assertFalse(self.window._history_refresh_requested)
        finally:
            self.window._close_requested = False
            self.window.runner.closing = False
            self.window.runner.idle.connect(self.window._finish_close)


if __name__ == "__main__":
    unittest.main()
