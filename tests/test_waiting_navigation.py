"""No browser required: waiting semantics, network accounting and CDP commands."""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import server as s


class WaitingTests(unittest.TestCase):
    def test_timeout_validation(self):
        for value in (-1, 300001, 1.2, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                s._deadline(value)

    def test_zero_timeout_still_checks_once(self):
        check = Mock(return_value=True)
        self.assertTrue(s._poll(check, time.monotonic()))
        check.assert_called_once()
        self.assertFalse(s._poll(lambda: False, time.monotonic()))

    def test_text_immediate_and_timeout(self):
        for value in (True, False):
            with patch.object(s, "_eval", return_value=value) as evaluate:
                self.assertEqual(s.wait_for_text('"hello"', 0, "#inner")["found"], value)
                self.assertIn("document.body?.textContent", evaluate.call_args.args[0])
                self.assertEqual(evaluate.call_args.kwargs["frame"], "#inner")

    def test_text_rechecks_after_change(self):
        with patch.object(s, "_eval", side_effect=[False, True]):
            self.assertTrue(s.wait_for_text("hello", 500)["found"])

    def test_url_regex_and_zero_timeout(self):
        with patch.object(s, "_eval", return_value="https://example.test/#ready"):
            self.assertTrue(s.wait_for_url(r"/#ready$", 0)["matched"])
            self.assertFalse(s.wait_for_url(r"missing$", 0)["matched"])
        with self.assertRaises(ValueError):
            s.wait_for_url("[")

    def test_navigation_context_retry(self):
        with patch.object(s, "_eval", side_effect=[RuntimeError("Execution context was destroyed"), True]):
            self.assertTrue(s.wait_for_text("hello", 500)["found"])

    def test_connection_error_is_not_a_timeout(self):
        with patch.object(s, "_eval", side_effect=RuntimeError("Connection refused")):
            with self.assertRaisesRegex(RuntimeError, "Connection refused"):
                s.wait_for_url(".", 100)

    def test_load_readiness(self):
        with patch.object(s, "_ensure_tab"), patch.object(s, "_eval", return_value="interactive"):
            self.assertTrue(s.wait_for_load_state("domcontentloaded", 0)["reached"])
            self.assertFalse(s.wait_for_load_state("load", 0)["reached"])
        with self.assertRaises(ValueError):
            s.wait_for_load_state("invalid", 0)

    def test_network_requests_started_before_wait_are_counted(self):
        tracker = s._NetworkTracker()
        tracker.request("slow")
        tracker.changed_at -= 1
        with patch.dict(s._state, network=tracker), patch.object(s, "_ensure_tab"), patch.object(s, "_eval", return_value="complete"):
            self.assertFalse(s.wait_for_load_state("networkidle", 0)["reached"])
            tracker.finished("slow")
            self.assertFalse(s.wait_for_load_state("networkidle", 0)["reached"])
            tracker.changed_at -= 1
            self.assertTrue(s.wait_for_load_state("networkidle", 0)["reached"])

    def test_redirect_failed_request_and_websocket(self):
        tracker = s._NetworkTracker()
        tracker.request("redirect")
        tracker.request("redirect")
        self.assertEqual(len(tracker.pending), 1)
        tracker.finished("redirect")  # loadingFailed uses this handler too
        tracker.request("socket", type="WebSocket")
        self.assertFalse(tracker.pending)


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.tab = Mock(id="selected")
        self.ensure = patch.object(s, "_ensure_tab", return_value=self.tab)
        self.ensure.start()
        self.addCleanup(self.ensure.stop)
        self.browser = Mock()
        self.browser_patch = patch.object(s, "_browser", return_value=self.browser)
        self.browser_patch.start()
        self.addCleanup(self.browser_patch.stop)

    def test_history_uses_entry_id_not_navigate(self):
        entries = [{"id": 11, "url": "https://a.test"}, {"id": 12, "url": "https://b.test"}]
        self.tab.Page.getNavigationHistory.return_value = {"currentIndex": 1, "entries": entries}
        self.assertTrue(s.go_back()["went_back"])
        self.tab.Page.navigateToHistoryEntry.assert_called_with(entryId=11, _timeout=10)
        self.tab.Page.getNavigationHistory.return_value["currentIndex"] = 0
        self.assertTrue(s.go_forward()["went_forward"])
        self.tab.Page.navigateToHistoryEntry.assert_called_with(entryId=12, _timeout=10)
        self.tab.Page.navigate.assert_not_called()

    def test_history_boundaries(self):
        self.tab.Page.getNavigationHistory.return_value = {"currentIndex": 0, "entries": [{"id": 1, "url": "about:blank"}]}
        self.assertFalse(s.go_back()["went_back"])
        self.assertFalse(s.go_forward()["went_forward"])
        self.tab.Page.navigateToHistoryEntry.assert_not_called()

    def test_reload(self):
        s.reload(True)
        self.tab.Page.reload.assert_called_once_with(ignoreCache=True, _timeout=10)
        self.tab.wait.assert_not_called()

    def test_new_tab_preserves_selection(self):
        self.browser.new_tab.return_value.id = "new"
        with patch.dict(s._state, tab=self.tab):
            self.assertEqual(s.new_tab(), {"id": "new", "url": "about:blank"})
            self.browser.new_tab.assert_called_once_with(url="about:blank", timeout=10)
            self.assertIs(s._state["tab"], self.tab)

    def test_close_selected_clears_state(self):
        with patch.dict(s._state, tab=self.tab, contexts={"f": 2}, network=Mock()), patch.object(s, "_list_pages", return_value=[{"id": "selected"}]):
            self.assertEqual(s.close_tab(), {"closed": "selected"})
            self.browser.close_tab.assert_called_once_with("selected", timeout=10)
            self.assertIsNone(s._state["tab"])
            self.assertIsNone(s._state["network"])
            self.assertEqual(s._state["contexts"], {})

    def test_close_other_preserves_selection(self):
        with patch.dict(s._state, tab=self.tab), patch.object(s, "_list_pages", return_value=[{"id": "other"}]):
            s.close_tab("other")
            self.assertIs(s._state["tab"], self.tab)
            self.tab.stop.assert_not_called()

    def test_unknown_close_is_error(self):
        with patch.object(s, "_list_pages", return_value=[]), self.assertRaises(ValueError):
            s.close_tab("missing")
        self.browser.close_tab.assert_not_called()


if __name__ == "__main__":
    unittest.main()
