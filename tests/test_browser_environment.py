import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import core.browser as browser_module
import utils.config as config

spec = importlib.util.spec_from_file_location('browser_runtime', Path(__file__).resolve().parents[1] / 'docker/runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class BrowserEnvironmentTests(unittest.TestCase):
    def load(self, environment=None, configured_path=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            content = (
                'TASKS=[{"username":"Alice","unique_id":"alice","targets":["Bob"]}]\n'
                'COOKIES_ALICE=[{"name":"sessionid","value":"test","domain":".douyin.com","path":"/"}]\n'
            )
            if configured_path is not None:
                content += 'PLAYWRIGHT_BROWSERS_PATH=' + configured_path + '\n'
            path.write_text(content, encoding='utf-8')
            settings = {'CONFIG_ENV_PATH': str(path), **(environment or {})}
            with patch.dict(os.environ, settings, clear=True), patch.object(runtime.os, 'chdir'):
                with patch.object(config, 'config', None), patch.object(config, 'userData', None):
                    runtime.load_configuration()
                    return os.environ['PLAYWRIGHT_BROWSERS_PATH'], os.environ['GITHUB_ACTIONS']

    def test_cron_environment_without_image_variables_uses_bundled_browser(self):
        for environment in ({}, {'PLAYWRIGHT_BROWSERS_PATH': ''}):
            with self.subTest(environment=environment):
                self.assertEqual(self.load(environment), ('/ms-playwright', 'true'))

    def test_explicit_browser_cache_environment_is_preserved(self):
        self.assertEqual(self.load({'PLAYWRIGHT_BROWSERS_PATH': '/custom/browsers'})[0], '/custom/browsers')

    def test_config_file_can_choose_browser_cache(self):
        self.assertEqual(self.load(configured_path='/configured/browsers')[0], '/configured/browsers')

    def test_launch_failure_preserves_cause_and_stops_playwright(self):
        cause = RuntimeError("Executable doesn't exist")
        with patch.object(browser_module, 'get_environment', return_value=browser_module.Environment.GITHUBACTION):
            with patch.object(browser_module, 'sync_playwright') as manager:
                driver = manager.return_value.start.return_value
                driver.chromium.launch.side_effect = cause
                with self.assertRaises(browser_module.BrowserStartupError) as caught:
                    browser_module.get_browser()
                self.assertIs(caught.exception.__cause__, cause)
                driver.stop.assert_called_once()

    def test_driver_start_failure_is_not_returned_as_none(self):
        cause = RuntimeError('Driver failed to start')
        with patch.object(browser_module, 'get_environment', return_value=browser_module.Environment.GITHUBACTION):
            with patch.object(browser_module, 'sync_playwright') as manager:
                manager.return_value.start.side_effect = cause
                with self.assertRaises(browser_module.BrowserStartupError) as caught:
                    browser_module.get_browser()
                self.assertIs(caught.exception.__cause__, cause)
