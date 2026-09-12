import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("docker_runtime", Path(__file__).resolve().parents[1] / "docker" / "runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class ScheduleTests(unittest.TestCase):
    def test_default_schedule_is_beijing_nine(self):
        self.assertEqual(runtime.schedule_settings({}), (9, 0, 0, "Asia/Shanghai"))

    def test_invalid_schedule_and_shell_text_fail(self):
        for values in ({"CRON_HOUR": "24"}, {"CRON_MINUTE": "-1"}, {"CRON_SECOND": "60"},
                       {"CRON_HOUR": "9; echo x"}, {"CRON_HOUR": "*"}, {"TZ": "../../etc/passwd"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                runtime.schedule_settings(values)

    def test_valid_schedule_normalizes_digits(self):
        self.assertEqual(runtime.schedule_settings({"CRON_HOUR": "09", "CRON_MINUTE": "05", "CRON_SECOND": "3", "TZ": "UTC"}), (9, 5, 3, "UTC"))


if __name__ == "__main__":
    unittest.main()
