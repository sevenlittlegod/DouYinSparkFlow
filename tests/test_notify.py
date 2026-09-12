import contextlib
import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import requests

from utils.notify import build_notification, notify, PUSHPLUS_ENDPOINT


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.report = {
            "status": "failed", "updated_at": "2026-09-12T10:20:30+00:00",
            "accounts": [{"username": "测试账号", "unique_id": "mine", "targets": [
                {"target": "friend1", "label": "阿甲", "status": "submitted_unverified"},
                {"target": "friend2", "label": "阿乙", "status": "missing", "reason": "target_missing"},
                {"target": "friend3", "label": "阿丙", "status": "failed", "reason": "send_failed"},
                {"target": "friend4", "status": "not_attempted"},
                {"target": "friend5", "status": "unknown"},
            ]}],
            "cookies": "COOKIE_SECRET", "message": "PRIVATE_MESSAGE", "logs": "FULL_LOG",
        }
        self.env = patch.dict(os.environ, {"NOTIFY_PROVIDER": "pushplus", "PUSHPLUS_TOKEN": "TOKEN_SECRET"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_partial_failure_lists_each_target_and_does_not_claim_receipt(self):
        title, body = build_notification(self.report)
        self.assertLessEqual(len(title), 32)
        self.assertNotIn("\n", title)
        self.assertIn("有未完成项", title)
        for phrase in ("已提交 1 人", "提交失败 1 人", "未找到 1 人", "未尝试 1 人", "状态未知 1 人",
                       "未找到：阿乙（friend2）", "已提交：阿甲（friend1）", "实际送达未验证", "18:20:30（北京时间）"):
            self.assertIn(phrase, body)
        for secret in ("COOKIE_SECRET", "PRIVATE_MESSAGE", "FULL_LOG"):
            self.assertNotIn(secret, body)

    @patch("utils.notify.requests.post")
    def test_success_makes_one_private_request_and_reports_only_accepted(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"code": 200, "msg": "TOKEN_SECRET"}
        result = notify(self.report)
        self.assertEqual(result, {"status": "accepted", "provider": "pushplus"})
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args, (PUSHPLUS_ENDPOINT,))
        self.assertEqual(kwargs["timeout"], 12)
        self.assertFalse(kwargs["allow_redirects"])
        payload = kwargs["json"]
        self.assertEqual(set(payload), {"token", "title", "content", "template", "channel"})
        self.assertEqual(payload["channel"], "wechat")
        self.assertEqual(payload["template"], "markdown")
        self.assertNotIn("TOKEN_SECRET", payload["content"])

    @patch("utils.notify.requests.post")
    def test_http_success_with_business_failure_is_safe_failure(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"code": 401, "msg": "TOKEN_SECRET"}
        self.assertEqual(notify(self.report), {"status": "failed", "provider": "pushplus", "reason": "provider_rejected"})
        post.assert_called_once()

    @patch("utils.notify.requests.post")
    def test_network_exception_does_not_escape_log_secret_or_retry(self, post):
        post.side_effect = requests.Timeout("TOKEN_SECRET private request details")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = notify(self.report)
        self.assertEqual(result["reason"], "request_failed")
        self.assertNotIn("TOKEN_SECRET", json.dumps(result) + output.getvalue())
        post.assert_called_once()

    @patch("utils.notify.requests.post")
    def test_disabled_and_missing_token_do_not_request(self, post):
        os.environ.pop("NOTIFY_PROVIDER")
        self.assertEqual(notify(self.report), {"status": "disabled", "provider": "off"})
        os.environ["NOTIFY_PROVIDER"] = "pushplus"
        os.environ.pop("PUSHPLUS_TOKEN")
        self.assertEqual(notify(self.report)["reason"], "missing_token")
        post.assert_not_called()

    @patch("utils.notify.requests.post")
    def test_redirect_and_invalid_json_are_not_accepted_or_exposed(self, post):
        post.return_value.status_code = 302
        self.assertEqual(notify(self.report)["reason"], "http_error")
        post.return_value.status_code = 200
        post.return_value.json.side_effect = ValueError("TOKEN_SECRET")
        result = notify(self.report)
        self.assertEqual(result["reason"], "invalid_response")
        self.assertNotIn("TOKEN_SECRET", json.dumps(result))

    def test_success_title_and_untrusted_error_field(self):
        self.report["status"] = "submitted_unverified"
        self.report["accounts"][0]["targets"] = self.report["accounts"][0]["targets"][:1]
        self.report["error"] = "TOKEN_SECRET"
        title, body = build_notification(self.report)
        self.assertIn("本次已提交", title)
        self.assertIn("已提交：阿甲（friend1）", body)
        self.assertNotIn("TOKEN_SECRET", body)

    def test_runtime_reason_codes_are_chinese_and_unknown_exceptions_are_ignored(self):
        reasons = {
            "editor_unavailable": "输入框不可用",
            "message_build_failed": "消息生成失败",
            "message_input_failed": "消息输入失败",
            "submission_in_progress": "提交结果不确定，请勿盲目重发",
            "submission_uncertain": "提交结果不确定，请勿盲目重发",
            "target_not_found": "网页会话未找到目标",
            "login_or_page_unavailable": "登录失效或页面未加载，请检查",
            "cookie_load_failed": "登录状态无法载入",
            "not_started": "尚未开始",
            "run_stopped": "前面步骤出错后停止，未尝试",
            "task_failed": "任务执行失败",
        }
        for reason, chinese in reasons.items():
            with self.subTest(reason=reason):
                self.report["accounts"][0]["targets"] = [
                    {"target": "friend1", "status": "failed", "reason": reason}
                ]
                self.report["error"] = reason
                _, body = build_notification(self.report)
                self.assertIn(f"提交失败：friend1；{chinese}", body)
                self.assertIn(f"任务情况：{chinese}。", body)
                self.assertNotIn(reason, body)
        self.report["accounts"][0]["targets"][0]["reason"] = "Raw exception TOKEN_SECRET"
        self.report["error"] = "Raw exception TOKEN_SECRET"
        _, body = build_notification(self.report)
        self.assertNotIn("Raw exception", body)
        self.assertNotIn("TOKEN_SECRET", body)

    @patch("utils.notify.requests.post")
    def test_connectivity_notification_does_not_claim_a_douyin_task_ran(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"code": 200}
        title, body = build_notification({"test": True})
        self.assertEqual(title, "抖音续火花：微信推送测试")
        self.assertIn("这只是推送测试，没有运行抖音发送任务", body)
        self.assertNotIn("已提交", body)
        self.assertEqual(notify({"test": True})["status"], "accepted")
        self.assertEqual(post.call_args.kwargs["json"]["content"], body)


if __name__ == "__main__":
    unittest.main()
