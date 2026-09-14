import os
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import core.tasks as tasks


@unittest.skipUnless(os.getenv('RUN_BROWSER_TESTS') == '1', 'Requires the container browser runtime')
class ChatPreparationTests(unittest.TestCase):
    def setUp(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.playwright.stop)
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()
        self.config = patch.dict(tasks.config, {'browserTimeout': 1000})
        self.config.start()
        self.addCleanup(self.config.stop)

    def fixture(self, change_header=True):
        action = "document.querySelector('.RightPanelHeadertitle').textContent='Bob'" if change_header else ''
        self.page.set_content('''
          <div class="conversationConversationItemwrapper"><div class="conversationConversationItemtitle">Other</div></div>
          <div class="conversationConversationItemwrapper" onclick="''' + action + '''"><div class="conversationConversationItemtitle">Bob</div></div>
          <div class="RightPanelHeadertitle">Other</div>
          <div data-slate-editor="true" data-placeholder="发送消息" contenteditable="true" oninput="this.removeAttribute('data-placeholder')"></div>
        ''')

    def test_exact_title_survives_row_reorder_and_uses_real_editable(self):
        self.fixture()
        self.page.evaluate("document.body.prepend(document.querySelectorAll('.conversationConversationItemwrapper')[1])")
        editor = tasks.open_chat_editor(self.page, tasks.SelectedTarget('bob-id', 'Bob'))
        editor.fill('draft only')
        self.assertEqual(editor.inner_text(), 'draft only')
        self.assertIsNone(editor.get_attribute('data-placeholder'))
        editor.press('Shift+Enter')
        editor.type('second line')
        self.assertIn('second line', editor.inner_text())
        self.assertEqual(self.page.locator(tasks.CHAT_TITLE_SELECTOR).inner_text(), 'Bob')

    def test_previous_chat_editor_is_not_accepted_before_header_changes(self):
        self.fixture(change_header=False)
        with self.assertRaises(AssertionError):
            tasks.open_chat_editor(self.page, tasks.SelectedTarget('bob-id', 'Bob'))
        self.assertEqual(self.page.locator(tasks.CHAT_INPUT_SELECTOR).inner_text(), '')

    def test_duplicate_exact_titles_are_rejected_before_click(self):
        self.fixture()
        self.page.evaluate("document.body.append(document.querySelectorAll('.conversationConversationItemwrapper')[1].cloneNode(true))")
        with self.assertRaises(tasks.IncompleteTaskError):
            tasks.open_chat_editor(self.page, tasks.SelectedTarget('bob-id', 'Bob'))
        self.assertEqual(self.page.locator(tasks.CHAT_TITLE_SELECTOR).inner_text(), 'Other')
