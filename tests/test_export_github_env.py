import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from dotenv import dotenv_values
from utils.export_github_env import main


class ExportTests(unittest.TestCase):
    def test_only_task_settings_round_trip_without_exposing_credentials(self):
        cookies = json.dumps([{"name": "sessionid", "value": "x'\\y${HOME}", "domain": ".douyin.com", "path": "/"}])
        message = "First line\\nDon't change ${HOME}"
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                with patch.dict(os.environ, {"VARS_JSON": json.dumps({"MESSAGE_TEMPLATE": message}),
                                             "SECRETS_JSON": json.dumps({"COOKIES_TEST": cookies, "GITHUB_TOKEN": "private-token", "UNRELATED": "other-secret"})}):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        main()
                actual = dotenv_values(".env", interpolate=False)
                self.assertEqual(actual, {"MESSAGE_TEMPLATE": message, "COOKIES_TEST": cookies})
                self.assertNotIn(cookies, output.getvalue())
                self.assertNotIn("private-token", Path(".env").read_text(encoding="utf-8"))
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
