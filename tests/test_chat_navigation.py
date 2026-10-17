import os
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import core.tasks as tasks


@unittest.skipUnless(os.getenv('RUN_BROWSER_TESTS') == '1', 'Requires the container browser runtime')
class ChatNavigationTests(unittest.TestCase):
    def test_visible_chat_does_not_wait_for_deferred_resource(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_default_navigation_timeout(1500)
                pending = []
                page.route('https://www.douyin.com/slow-fixture.js', lambda route: pending.append(route))
                page.route('https://www.douyin.com/chat', lambda route: route.fulfill(
                    content_type='text/html', body='''<!doctype html>
                    <script>window.domReady = false;
                    document.addEventListener('DOMContentLoaded', () => window.domReady = true);</script>
                    <script defer src="/slow-fixture.js"></script>
                    <div class="conversationConversationListwrapper">Conversation fixture</div>'''))
                with patch.dict(tasks.config, {'taskRetryTimes': 1, 'browserTimeout': 1500}):
                    tasks.open_chat_page(page, 'Fixture')
                self.assertTrue(page.locator(tasks.CONVERSATION_LIST_SELECTOR).is_visible())
                self.assertFalse(page.evaluate('window.domReady'))
                self.assertEqual(len(pending), 1)
                pending[0].fulfill(content_type='application/javascript', body='')
                page.wait_for_load_state('domcontentloaded', timeout=3000)
            finally:
                browser.close()
