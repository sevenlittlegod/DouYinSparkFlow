import os
import unittest

from playwright.sync_api import sync_playwright
from core.submission import snapshot, confirm_submission, SubmissionUnconfirmed, SubmissionRejected


@unittest.skipUnless(os.getenv('RUN_BROWSER_TESTS') == '1', 'Requires the container browser runtime')
class SubmissionConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=True)
        self.addCleanup(self.pw.stop)
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()
        self.page.set_content('''<div id="messages"></div>
          <div id="editor" contenteditable="true" data-slate-editor="true">hello</div>''')

    def bubble(self, text='hello', outgoing=True, failed=False):
        self.page.evaluate('''({text,outgoing,failed}) => {
            const row=document.createElement('div'); row.className='messageMessageBoxmessageBox';
            const box=document.createElement('div');
            box.className='messageMessageBoxcontentBox'+(outgoing?' messageMessageBoxisFromMe':'');
            const body=document.createElement('div'); body.className='TextMessageTexttextInnerContent';
            body.textContent=text; box.append(body); row.append(box);
            if(failed) {const icon=document.createElement('span');icon.className='sendErrorIcon';row.append(icon);}
            document.querySelector('#messages').append(row);
        }''', dict(text=text, outgoing=outgoing, failed=failed))

    def clear(self):
        self.page.locator('#editor').fill('')

    def test_enter_that_only_adds_newline_is_unconfirmed(self):
        before = snapshot(self.page, 'hello')
        self.page.locator('#editor').press('Enter')
        with self.assertRaises(SubmissionUnconfirmed):
            confirm_submission(self.page, 'hello', before, timeout_ms=300)

    def test_old_identical_message_is_not_new_submission(self):
        self.bubble()
        before = snapshot(self.page, 'hello')
        self.clear()
        with self.assertRaises(SubmissionUnconfirmed):
            confirm_submission(self.page, 'hello', before, timeout_ms=300)

    def test_incoming_message_cannot_confirm_outgoing_send(self):
        before = snapshot(self.page, 'hello')
        self.bubble(outgoing=False)
        self.clear()
        with self.assertRaises(SubmissionUnconfirmed):
            confirm_submission(self.page, 'hello', before, timeout_ms=300)

    def test_new_message_requires_editor_clear_and_settles(self):
        before = snapshot(self.page, 'hello')
        self.bubble()
        with self.assertRaises(SubmissionUnconfirmed):
            confirm_submission(self.page, 'hello', before, timeout_ms=300)
        self.clear()
        confirm_submission(self.page, 'hello', before, timeout_ms=2500)

    def test_error_icon_and_delayed_toast_reject_submission(self):
        before = snapshot(self.page, 'hello')
        self.bubble(failed=True)
        self.clear()
        with self.assertRaises(SubmissionRejected):
            confirm_submission(self.page, 'hello', before, timeout_ms=300)
        self.page.locator('.sendErrorIcon').evaluate('(el)=>el.remove()')
        self.page.evaluate('''() => setTimeout(() => {
            const alert=document.createElement('div'); alert.setAttribute('role','alert');
            alert.textContent='发送失败'; document.body.append(alert);
        }, 300)''')
        with self.assertRaises(SubmissionRejected):
            confirm_submission(self.page, 'hello', before, timeout_ms=2500)

    def test_emoji_alt_and_line_breaks_are_compared(self):
        self.bubble('hello')
        self.page.locator('.TextMessageTexttextInnerContent').evaluate('''el => {
            el.textContent='第一行'; el.append(document.createElement('br'));
            const img=document.createElement('img');img.alt='[赞]';el.append(img);
        }''')
        self.assertEqual(snapshot(self.page, '第一行\\n[赞]')['matches'], 1)
        self.assertEqual(snapshot(self.page, '其他内容')['matches'], 0)

    def test_real_chat_emoji_title_without_alt_confirms(self):
        before = snapshot(self.page, '今日续火花[赞]\\n第二行')
        self.bubble()
        self.page.locator('.TextMessageTexttextInnerContent').evaluate('''el => {
            el.innerHTML='<span>今日续火花</span><span class="TextMessageTextemoji"><img title="[赞]"></span><span>\\n第二行</span>';
        }''')
        self.clear()
        confirm_submission(self.page, '今日续火花[赞]\\n第二行', before, timeout_ms=2500)

    def test_newest_first_failure_or_pending_cannot_look_successful(self):
        self.bubble()
        before = snapshot(self.page, 'hello')
        self.bubble(failed=True)
        self.page.locator('#messages').evaluate('(el) => el.prepend(el.lastElementChild)')
        self.clear()
        with self.assertRaises(SubmissionRejected):
            confirm_submission(self.page, 'hello', before, timeout_ms=300)
        self.page.locator('.sendErrorIcon').evaluate('(el) => el.className="sendingSpinner"')
        with self.assertRaises(SubmissionUnconfirmed):
            confirm_submission(self.page, 'hello', before, timeout_ms=300)
