import json
import os
import unittest
from unittest.mock import patch

import utils.config as config


class ConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.previous_users = config.userData
        config.userData = None
        self.task = {"unique_id": "alice", "username": "Alice", "targets": ["Bob"]}
        self.cookie = {
            "name": "sessionid",
            "value": 'secret-雪-"quoted"-\\literal',
            "domain": ".douyin.com",
            "path": "/",
            "sameSite": "unspecified",
        }
        os.environ["TASKS"] = json.dumps([self.task])
        os.environ["COOKIES_ALICE"] = json.dumps([self.cookie], ensure_ascii=False)

    def tearDown(self):
        config.userData = self.previous_users
        self.environment.stop()

    def test_missing_empty_and_invalid_tasks_fail(self):
        for value in (None, "", "[]", "{}", "null", "[42]", "bad JSON"):
            with self.subTest(value=value):
                if value is None:
                    os.environ.pop("TASKS", None)
                else:
                    os.environ["TASKS"] = value
                with self.assertRaisesRegex(ValueError, "TASKS"):
                    config.get_userData()

    def test_missing_id_and_invalid_targets_fail(self):
        for changes in (
            {"unique_id": ""},
            {"targets": []},
            {"targets": "Bob"},
            {"targets": [42]},
            {"targets": ["  "]},
        ):
            with self.subTest(changes=changes):
                os.environ["TASKS"] = json.dumps([{**self.task, **changes}])
                with self.assertRaises(ValueError):
                    config.get_userData()

    def test_bad_cookies_fail_without_exposing_values(self):
        secret = "PRIVATE_COOKIE_VALUE"
        for value in (
            "",
            secret,
            "[]",
            json.dumps({"value": secret}),
            json.dumps([secret]),
            json.dumps([{**self.cookie, "value": {"secret": secret}}]),
            json.dumps([{"name": "sessionid", "value": secret}]),
        ):
            with self.subTest(value=value):
                os.environ["COOKIES_ALICE"] = value
                with self.assertRaises(ValueError) as caught:
                    config.get_userData()
                self.assertIn("COOKIES_ALICE", str(caught.exception))
                self.assertNotIn(secret, str(caught.exception))

    def test_json_escaping_and_unicode_cookie_values_are_preserved(self):
        os.environ["TASKS"] = json.dumps([{**self.task, "targets": ["  Bob\u3000"]}])
        users = config.get_userData()
        self.assertEqual(users[0]["cookies"][0]["value"], self.cookie["value"])
        self.assertNotIn("sameSite", users[0]["cookies"][0])
        self.assertEqual(users[0]["targets"], ["Bob"])

    def test_later_invalid_account_does_not_cache_partial_configuration(self):
        os.environ["TASKS"] = json.dumps([self.task, {**self.task, "unique_id": "second"}])
        with self.assertRaisesRegex(ValueError, "COOKIES_SECOND"):
            config.get_userData()
        self.assertIsNone(config.userData)
        with self.assertRaisesRegex(ValueError, "COOKIES_SECOND"):
            config.get_userData()


if __name__ == "__main__":
    unittest.main()
