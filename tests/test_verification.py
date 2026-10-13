import json
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from portal.store import Store, AccessError, ValidationError
from portal.binding import Bindings
from portal.verification import VerificationBridge, exposed, qr_exposed, verification_page


class VerificationCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.jobs=Bindings(self.store)
        invite=self.store.create_invite('本人');self.sid,_=self.store.new_session('owner',invite['token'])
        self.job=self.jobs.start(self.sid,'qr',0)['id'];self.jobs.claim()
        self.view={'actions':[{'id':'confirm','label':'确认'}],'fields':[{'id':'code','label':'短信验证码'}]}
        self.jobs.update(self.job,'verification',verification=self.view)

    def test_numeric_code_is_session_scoped_never_public_and_consumed_once(self):
        other=self.store.create_invite('别人');sid,_=self.store.new_session('owner',other['token'])
        with self.assertRaises(AccessError): self.jobs.command(sid,self.job,'confirm',{'code':'123456'})
        self.jobs.command(self.sid,self.job,'confirm',{'code':'123456'})
        self.assertNotIn('123456',json.dumps(self.jobs.get(self.sid,self.job)))
        self.assertEqual('123456',self.jobs.take_command(self.job)['values']['code'])
        self.assertIsNone(self.jobs.take_command(self.job))

    def test_arbitrary_actions_fields_and_nonnumeric_codes_rejected(self):
        for action,values in [('navigate',{}),('confirm',{'password':'anything'}),('confirm',{'code':'script()'})]:
            with self.assertRaises(ValidationError): self.jobs.command(self.sid,self.job,action,values)

    def test_expiry_cancel_and_ready_clear_pending_code(self):
        self.jobs.command(self.sid,self.job,'confirm',{'code':'123456'})
        with patch('portal.binding.time.time',return_value=time.time()+46):
            self.assertIsNone(self.jobs.take_command(self.job))
        self.jobs.command(self.sid,self.job,'confirm',{'code':'123456'})
        self.jobs.update(self.job,'ready',identity={'unique_id':'owner'},friends=[],cookies='synthetic_session_123456')
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT command FROM binding_jobs WHERE id=?',(self.job,)).fetchone()[0])
        self.assertNotIn('123456',json.dumps(self.jobs.get(self.sid,self.job)))

    def test_browser_deadline_matches_countdown_and_rejects_late_click(self):
        with self.store.connect() as db:
            db.execute('UPDATE binding_jobs SET browser_deadline=? WHERE id=?',(int(time.time())+5,self.job))
        self.assertLessEqual(self.jobs.get(self.sid,self.job)['expires_at'],time.time()+5)
        with patch('portal.binding.time.time',return_value=time.time()+6):
            self.assertEqual('expired',self.jobs.get(self.sid,self.job)['state'])
            self.assertFalse(self.jobs.alive(self.job))
            from portal.store import ConflictError
            with self.assertRaises(ConflictError): self.jobs.command(self.sid,self.job,'confirm',{'code':'123456'})
            self.jobs.sweep()
            with self.store.connect() as db:
                self.assertEqual('expired',db.execute('SELECT state FROM binding_jobs WHERE id=?',(self.job,)).fetchone()[0])

    def test_action_feedback_does_not_disclose_code(self):
        self.jobs.command(self.sid,self.job,'confirm',{'code':'123456'})
        self.assertEqual('queued',self.jobs.get(self.sid,self.job)['action_result'])
        self.jobs.take_command(self.job);self.jobs.command_result(self.job,'clicked')
        result=self.jobs.get(self.sid,self.job)
        self.assertEqual('clicked',result['action_result']);self.assertNotIn('123456',json.dumps(result))

    def test_only_trusted_open_authentication_popup_can_be_shown(self):
        def page(url, closed=False):
            result=Mock();result.main_frame.url=url;result.is_closed.return_value=closed;return result
        primary=page('https://www.douyin.com/chat')
        popup=page('https://verify.snssdk.com/identity')
        untrusted=page('https://other.example/identity')
        closed=page('https://verify.snssdk.com/identity',True)
        context=Mock(pages=[primary,popup,untrusted,closed])
        self.assertIs(popup,verification_page(context,primary))


@unittest.skipUnless(os.getenv('RUN_BROWSER_TESTS')=='1','Requires container browser')
class VerificationBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.pw=sync_playwright().start();self.browser=self.pw.chromium.launch()
        self.addCleanup(self.pw.stop);self.addCleanup(self.browser.close)
        self.page=self.browser.new_page();self.bridge=VerificationBridge()
        p=patch('portal.verification.trusted_frame',return_value=True);p.start();self.addCleanup(p.stop)

    def test_human_sms_selection_and_code_submission_use_current_visible_controls(self):
        self.page.set_content('<h1>身份验证</h1><button id="sms">接收短信验证</button><button>手机刷脸验证</button>')
        self.page.locator('#sms').evaluate('''el => el.onclick=()=>{
          document.body.innerHTML='<h1>安全验证</h1><input placeholder="请输入短信验证码"><button id="submit">确认</button>';
          document.querySelector('#submit').onclick=()=>{window.submitted=document.querySelector('input').value};
        }''')
        self.assertEqual({'sms','face'},{a['id'] for a in self.bridge.inspect(self.page)['actions']})
        self.assertTrue(self.bridge.perform(self.page,{'action':'sms','values':{}}))
        view=self.bridge.inspect(self.page);self.assertEqual('code',view['fields'][0]['id'])
        self.assertTrue(self.bridge.perform(self.page,{'action':'confirm','values':{'code':'123456'}}))
        self.assertEqual('123456',self.page.evaluate('window.submitted'))
        self.assertNotIn('123456',json.dumps(self.bridge.inspect(self.page)))
        self.assertTrue(self.bridge.screenshot(self.page).startswith(b'\x89PNG'))

    def test_covered_login_fields_and_disappeared_challenge_cannot_be_operated(self):
        self.page.set_content('<input placeholder="手机号"><div style="position:fixed;inset:0;background:white"><h1>身份验证</h1><button>接收短信验证</button></div>')
        self.assertEqual([],self.bridge.inspect(self.page)['fields'])
        self.page.set_content('<h1>聊天页面</h1><input placeholder="搜索">')
        self.assertFalse(self.bridge.perform(self.page,{'action':'sms','values':{}}))

    def test_official_center_logo_is_not_a_verification_overlay(self):
        self.page.set_content('''<div style="position:relative;width:178px;height:178px">
          <img aria-label="二维码" style="width:178px;height:178px" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E">
          <svg style="position:absolute;left:69px;top:69px;width:40px;height:40px"><rect width="40" height="40"/></svg>
          </div>''')
        qr=self.page.locator('img')
        self.assertFalse(exposed(qr)); self.assertTrue(qr_exposed(qr))
        self.page.evaluate("() => {const p=document.createElement('div');p.style='position:fixed;inset:0;background:white';document.body.append(p)}")
        self.assertFalse(qr_exposed(qr))
