import traceback
import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import core.tasks as tasks


class RuntimeReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.browser = MagicMock()
        self.context = self.browser.new_context.return_value
        self.page = self.context.new_page.return_value
        self.editor = self.page.locator.return_value
        self.patches = (
            patch.object(tasks.time, "sleep"),
            patch.object(tasks, "build_message", return_value="hello"),
            patch.object(tasks, "logger"),
        )
        for replacement in self.patches:
            replacement.start()
            self.addCleanup(replacement.stop)

    def run_user(self, selected, targets):
        with patch.object(tasks, "scroll_and_select_user", return_value=iter(selected)):
            tasks.do_user_task(self.browser, "Alice", [], targets)

    def test_no_matching_targets_fails_without_sending(self):
        with self.assertRaisesRegex(RuntimeError, "Bob"):
            self.run_user([], ["Bob"])
        self.editor.press.assert_not_called()
        self.context.close.assert_called_once()

    def test_partial_match_fails_after_one_submission_without_retry(self):
        with self.assertRaisesRegex(RuntimeError, "Carol"):
            self.run_user(["Bob"], ["Bob", "Carol"])
        self.editor.press.assert_called_once_with("Enter")
        self.context.close.assert_called_once()

    def test_submission_is_logged_only_after_enter_and_duplicates_are_not_resent(self):
        events = []
        self.editor.press.side_effect = lambda key: events.append(key)
        tasks.logger.info.side_effect = lambda message: events.append(message)
        self.run_user(["Bob", "Bob"], ["Bob"])
        self.assertEqual(events[0], "Enter")
        self.assertIn("是否送达", events[1])
        self.editor.press.assert_called_once_with("Enter")
        self.context.close.assert_called_once()

    def test_enter_failure_is_not_retried_or_logged_as_submitted(self):
        self.editor.press.side_effect = RuntimeError("submission uncertain")
        with self.assertRaisesRegex(RuntimeError, "submission uncertain"):
            self.run_user(["Bob"], ["Bob"])
        self.editor.press.assert_called_once_with("Enter")
        tasks.logger.info.assert_not_called()
        self.context.close.assert_called_once()

    def test_list_and_editor_timeouts_explain_login_and_page_changes(self):
        for blocked_selector in (tasks.CONVERSATION_LIST_SELECTOR, tasks.CHAT_EDITOR_SELECTOR):
            with self.subTest(selector=blocked_selector):
                def wait(selector, **kwargs):
                    if selector == blocked_selector:
                        raise PlaywrightTimeoutError("selector timed out")

                self.page.wait_for_selector.side_effect = wait
                with self.assertRaisesRegex(RuntimeError, "Cookie.*页面结构"):
                    self.run_user(["Bob"], ["Bob"])
                self.editor.press.assert_not_called()

    def test_browser_cookie_error_does_not_expose_cookie_in_traceback(self):
        secret = "PRIVATE_COOKIE_VALUE"
        self.context.add_cookies.side_effect = ValueError(secret)
        try:
            self.run_user(["Bob"], ["Bob"])
        except ValueError:
            error = traceback.format_exc()
        else:
            self.fail("Invalid cookies should fail")
        self.assertNotIn(secret, error)
        self.assertIn("Cookie JSON", error)
        self.page.goto.assert_not_called()
        self.context.close.assert_called_once()

    def test_invalid_configuration_stops_before_browser_launch(self):
        with patch.object(tasks, "get_userData", side_effect=ValueError("TASKS invalid")):
            with patch.object(tasks, "get_browser") as launch:
                with self.assertRaisesRegex(ValueError, "TASKS invalid"):
                    tasks.runTasks()
                launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
