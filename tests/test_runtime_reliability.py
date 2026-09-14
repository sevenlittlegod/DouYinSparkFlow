import traceback
import unittest
from unittest.mock import MagicMock, call, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import core.tasks as tasks


class RuntimeReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.browser = MagicMock()
        self.context = self.browser.new_context.return_value
        self.page = self.context.new_page.return_value
        self.editor = self.page.locator.return_value
        self.editor.inner_text.return_value = ''
        self.prepare = MagicMock(return_value=self.editor)
        self.patches = (
            patch.object(tasks.time, "sleep"),
            patch.object(tasks, "build_message", return_value="hello"),
            patch.object(tasks, "logger"),
            patch.object(tasks, 'open_chat_editor', self.prepare),
            patch.object(tasks, 'check_chat_title'),
            patch.dict(tasks.config, {'taskRetryTimes': 1}),
        )
        for replacement in self.patches:
            replacement.start()
            self.addCleanup(replacement.stop)

    def run_user(self, selected, targets, on_result=None):
        with patch.object(tasks, "scroll_and_select_user", return_value=iter(tasks.SelectedTarget(t, t) for t in selected)):
            tasks.do_user_task(self.browser, "Alice", [], targets, on_result=on_result)

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

    def test_list_timeout_explains_login_and_page_changes(self):
        self.page.wait_for_selector.side_effect = PlaywrightTimeoutError('list timed out')
        with self.assertRaisesRegex(RuntimeError, 'Cookie.*页面结构'):
            self.run_user(['Bob'], ['Bob'])
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

    def test_missing_targets_are_reported_without_sending(self):
        results = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "Bob"):
            self.run_user([], ["Bob", "Carol"], results)
        self.assertEqual(results.call_args_list, [
            call("Bob", "missing", "target_not_found"),
            call("Carol", "missing", "target_not_found"),
        ])
        self.editor.press.assert_not_called()

    def test_partial_match_preserves_submitted_status_and_reports_missing(self):
        results = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "Carol"):
            self.run_user(["Bob"], ["Bob", "Carol"], results)
        self.assertEqual(results.call_args_list, [
            call("Bob", "unknown", "submission_in_progress"),
            call("Bob", "submitted_unverified", None),
            call("Carol", "missing", "target_not_found"),
        ])
        self.editor.press.assert_called_once_with("Enter")

    def test_later_editor_failure_does_not_overwrite_earlier_submission(self):
        results = MagicMock()
        self.prepare.side_effect = [self.editor, PlaywrightTimeoutError('editor unavailable'), self.editor]
        with self.assertRaises(tasks.IncompleteTaskError):
            self.run_user(["Bob", "Carol", "Dave"], ["Bob", "Carol", "Dave"], results)
        self.assertEqual(results.call_args_list, [
            call("Bob", "unknown", "submission_in_progress"),
            call("Bob", "submitted_unverified", None),
            call("Carol", "failed", "conversation_unavailable"),
            call("Dave", "unknown", "submission_in_progress"),
            call("Dave", "submitted_unverified", None),
        ])
        self.assertEqual(self.editor.press.call_count, 2)
        self.context.close.assert_called_once()

    def test_uncertain_enter_is_recorded_before_press_and_never_retried(self):
        events = []

        def record(target, status, reason):
            events.append((target, status, reason))

        def uncertain_press(key):
            events.append(key)
            raise RuntimeError("submission uncertain")

        self.editor.press.side_effect = uncertain_press
        with self.assertRaisesRegex(RuntimeError, "submission uncertain"):
            self.run_user(["Bob", "Bob", "Carol"], ["Bob", "Carol"], record)
        self.assertEqual(events, [
            ("Bob", "unknown", "submission_in_progress"),
            "Enter",
            ("Bob", "unknown", "submission_uncertain"),
        ])
        self.editor.press.assert_called_once_with("Enter")
        tasks.logger.info.assert_not_called()
        self.context.close.assert_called_once()

    def test_message_build_failure_is_recorded_without_typing_or_sending(self):
        results = MagicMock()
        with patch.object(tasks, "build_message", side_effect=RuntimeError("build failed")):
            with self.assertRaisesRegex(RuntimeError, "build failed"):
                self.run_user(["Bob"], ["Bob", "Carol"], results)
        results.assert_called_once_with("Bob", "failed", "message_build_failed")
        self.editor.type.assert_not_called()
        self.editor.press.assert_not_called()

    def test_message_input_failure_is_recorded_without_sending(self):
        results = MagicMock()
        self.editor.type.side_effect = RuntimeError("input failed")
        with self.assertRaisesRegex(RuntimeError, "input failed"):
            self.run_user(["Bob"], ["Bob", "Carol"], results)
        results.assert_called_once_with("Bob", "failed", "message_input_failed")
        self.editor.press.assert_not_called()

    def test_login_failure_marks_all_targets_unattempted(self):
        results = MagicMock()
        self.page.wait_for_selector.side_effect = PlaywrightTimeoutError("list unavailable")
        with self.assertRaises(RuntimeError):
            self.run_user(["Bob"], ["Bob", "Carol"], results)
        self.assertEqual(results.call_args_list, [
            call("Bob", "not_attempted", "login_or_page_unavailable"),
            call("Carol", "not_attempted", "login_or_page_unavailable"),
        ])
        self.editor.press.assert_not_called()

    def test_cookie_failure_reports_safe_reason_without_sending(self):
        results = MagicMock()
        self.context.add_cookies.side_effect = ValueError("PRIVATE_COOKIE_VALUE")
        with self.assertRaises(ValueError):
            self.run_user(["Bob"], ["Bob"], results)
        results.assert_called_once_with("Bob", "not_attempted", "cookie_load_failed")
        self.page.goto.assert_not_called()
        self.editor.press.assert_not_called()

    def test_result_persistence_failure_stops_before_enter(self):
        results = MagicMock(side_effect=OSError("status storage unavailable"))
        with self.assertRaisesRegex(OSError, "status storage unavailable"):
            self.run_user(["Bob"], ["Bob"], results)
        self.editor.press.assert_not_called()
        self.context.close.assert_called_once()

    def test_run_tasks_forwards_each_account_identity_to_report(self):
        users = [
            {"username": "Alice", "unique_id": "alice-id", "cookies": [], "targets": ["Bob"]},
            {"username": "Eve", "unique_id": "eve-id", "cookies": [], "targets": ["Carol"]},
        ]
        report = MagicMock()
        playwright = MagicMock()

        def complete_user(browser, username, cookies, targets, on_result=None):
            on_result(targets[0], "submitted_unverified", None)

        with patch.object(tasks, "get_userData", return_value=users):
            with patch.object(tasks, "get_browser", return_value=(playwright, self.browser)):
                with patch.object(tasks, "do_user_task", side_effect=complete_user):
                    tasks.runTasks(report)
        self.assertEqual(report.update_target.call_args_list, [
            call("alice-id", "Bob", "submitted_unverified", None),
            call("eve-id", "Carol", "submitted_unverified", None),
        ])
        self.browser.close.assert_called_once()
        playwright.stop.assert_called_once()

    def test_recovery_reopens_context_and_excludes_submitted_targets(self):
        results = MagicMock()
        self.prepare.side_effect = [self.editor, PlaywrightTimeoutError('editor unavailable'), self.editor]
        selections = [iter([tasks.SelectedTarget('Bob', 'Bob'), tasks.SelectedTarget('Carol', 'Carol')]),
                      iter([tasks.SelectedTarget('Carol', 'Carol')])]
        with patch.dict(tasks.config, {'taskRetryTimes': 3}):
            with patch.object(tasks, 'scroll_and_select_user', side_effect=selections) as select:
                tasks.do_user_task(self.browser, 'Alice', [], ['Bob', 'Carol'], results)
        self.assertEqual([c.args[2] for c in select.call_args_list], [['Bob', 'Carol'], ['Carol']])
        self.assertEqual(self.browser.new_context.call_count, 2)
        self.assertEqual(self.editor.press.call_args_list, [call('Enter'), call('Enter')])
        self.assertEqual(results.call_args_list[-1], call('Carol', 'submitted_unverified', None))

    def test_existing_draft_stops_without_typing_or_sending(self):
        self.editor.inner_text.return_value = 'Unfinished personal message'
        results = MagicMock()
        with self.assertRaisesRegex(RuntimeError, '草稿'):
            self.run_user(['Bob'], ['Bob'], results)
        self.editor.type.assert_not_called()
        self.editor.press.assert_not_called()
        results.assert_called_once_with('Bob', 'failed', 'draft_present')

    def test_changed_recipient_after_typing_stops_before_enter(self):
        results = MagicMock()
        with patch.object(tasks, 'check_chat_title', side_effect=AssertionError('Wrong chat')):
            with self.assertRaisesRegex(RuntimeError, '标题'):
                self.run_user(['Bob'], ['Bob'], results)
        self.editor.press.assert_not_called()
        results.assert_called_once_with('Bob', 'failed', 'conversation_changed')


if __name__ == "__main__":
    unittest.main()
