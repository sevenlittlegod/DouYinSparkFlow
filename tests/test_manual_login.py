import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

from dotenv import dotenv_values


spec = importlib.util.spec_from_file_location(
    "manual_login", Path(__file__).resolve().parents[1] / "docker" / "login.py"
)
login = importlib.util.module_from_spec(spec)
spec.loader.exec_module(login)


class ManualLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / ".env"
        self.marker = Path(self.temp.name) / "login-ready.json"
        self.tasks = [{"unique_id": "alice_id", "targets": ["Bob"]}]
        self.cookie = {"name": "sessionid", "value": "SECRET'雪${KEEP}\\literal\\\"quoted", "domain": ".douyin.com", "path": "/"}

    def write_config(self, tasks):
        self.config.write_text("MESSAGE_TEMPLATE=hello\nTASKS=" + json.dumps(tasks) + "\n", encoding="utf-8")

    def test_save_preserves_task_and_cookie_value_and_marker_contains_no_secret(self):
        self.write_config(self.tasks)
        unrelated = {**self.cookie, "domain": "douyin.com.example.com"}
        login.save_login(self.config, [self.cookie, unrelated], self.marker)
        values = dotenv_values(self.config, interpolate=False)
        self.assertEqual(json.loads(values["TASKS"]), self.tasks)
        self.assertEqual(values["MESSAGE_TEMPLATE"], "hello")
        self.assertEqual(json.loads(values["COOKIES_ALICE_ID"]), [self.cookie])
        marker = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(set(marker), {"status", "saved_at"})
        self.assertEqual(marker["status"], "ready")
        self.assertNotIn("SECRET", self.marker.read_text(encoding="utf-8"))
        if os.name != "nt":
            self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_no_config_no_task_or_no_id_cannot_create_cookie_key(self):
        for content in (None, "", "TASKS=not-json\n", "TASKS=[]\n", "TASKS=[{}]\n"):
            with self.subTest(content=content):
                self.config.unlink(missing_ok=True)
                if content is not None:
                    self.config.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    login.save_login(self.config, [self.cookie], self.marker)
                self.assertFalse(self.marker.exists())
                if self.config.exists():
                    self.assertNotIn("COOKIES_", self.config.read_text(encoding="utf-8"))

    def test_multiple_accounts_or_invalid_id_cannot_be_overwritten(self):
        for tasks in ([self.tasks[0], {"unique_id": "second"}], [{"unique_id": "alice\nNEW_KEY=value"}]):
            with self.subTest(tasks=tasks):
                self.write_config(tasks)
                before = self.config.read_bytes()
                with self.assertRaises(ValueError):
                    login.save_login(self.config, [self.cookie], self.marker)
                self.assertEqual(self.config.read_bytes(), before)

    def test_no_douyin_session_cookie_cannot_be_saved(self):
        self.write_config(self.tasks)
        before = self.config.read_bytes()
        for cookies in ([], [{**self.cookie, "value": ""}], [{**self.cookie, "domain": "evil.example"}]):
            with self.subTest(cookies=cookies):
                with self.assertRaises(ValueError):
                    login.save_login(self.config, cookies, self.marker)
                self.assertEqual(self.config.read_bytes(), before)
                self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
