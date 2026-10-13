import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from portal.binding import Bindings
from portal.binding_worker import IdentityReader
from portal.store import Store, AccessError, ConflictError, ValidationError
import test_portal as portal_tests
from test_portal import config, COOKIE


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.jobs = Bindings(self.store)
        self.invite = self.store.create_invite('测试扫码')
        self.aid = self.invite['account_id']
        self.sid, _ = self.store.new_session('owner', self.invite['token'])

    def ready(self):
        job = self.jobs.start(self.sid, 'qr', 0)
        self.jobs.claim()
        self.jobs.update(job['id'], 'ready', identity={'unique_id':'owner_1','nickname':'测试本人'},
                         friends=[{'douyin_id':'friend.1','nickname':'好友甲','ambiguous':False}],
                         cookies=COOKIE, partial=False)
        return job['id']

    def test_start_claim_and_ready_never_change_account_or_expose_cookies(self):
        jid = self.ready()
        public = self.jobs.get(self.sid, jid)
        self.assertEqual('ready', public['state'])
        self.assertEqual('owner_1', public['identity']['unique_id'])
        self.assertNotIn(COOKIE, json.dumps(public))
        self.assertEqual([], self.store.active_accounts())
        self.assertEqual(0, self.store.account(self.aid)['revision'])

    def test_other_owner_admin_and_relogged_session_cannot_access_result_or_qr(self):
        jid = self.ready()
        other = self.store.create_invite('另一人')
        sid2, _ = self.store.new_session('owner', other['token'])
        admin, _ = self.store.new_session('admin')
        relog, _ = self.store.new_session('owner', self.invite['token'])
        for sid in (sid2, admin, relog, ''):
            with self.subTest(sid=sid[:3]):
                for image in (True, False):
                    with self.assertRaises(AccessError): self.jobs.get(sid, jid, image)
                with self.assertRaises(AccessError): self.jobs.cancel(sid, jid)

    def test_atomic_submit_only_creates_pending_and_clears_ephemeral_secret(self):
        jid = self.ready()
        saved = self.store.save(self.aid, config(cookies=''), 0, jid, self.sid)
        self.assertTrue(saved['pending']['cookies_configured'])
        self.assertEqual([], self.store.active_accounts())
        self.assertEqual('consumed', self.jobs.get(self.sid, jid)['state'])
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT cookies FROM binding_jobs WHERE id=?', (jid,)).fetchone()[0])
        self.store.approve(self.aid, saved['revision'])
        self.assertEqual(COOKIE, self.store.active_accounts()[0]['config']['cookies'][0]['value'])

    def test_invalid_form_rolls_back_consumption(self):
        jid = self.ready()
        with self.assertRaises(ValidationError):
            self.store.save(self.aid, config(cookies='', friends=[]), 0, jid, self.sid)
        self.assertEqual('ready', self.jobs.get(self.sid, jid)['state'])
        self.assertEqual(0, self.store.account(self.aid)['revision'])

    def test_identity_change_or_manual_cookie_override_rejected(self):
        jid = self.ready()
        for payload in (config(cookies='', unique_id='someone_else'), config(cookies='another_secret')):
            with self.assertRaises(ValidationError): self.store.save(self.aid, payload, 0, jid, self.sid)
        self.assertEqual('ready', self.jobs.get(self.sid, jid)['state'])

    def test_config_revision_change_blocks_stale_binding(self):
        jid = self.ready()
        self.store.save(self.aid, config(), 0)
        with self.assertRaises(ConflictError): self.store.save(self.aid, config(cookies=''), 1, jid, self.sid)

    def test_expired_result_unusable_and_sweep_removes_secret(self):
        jid = self.ready()
        with patch('portal.binding.time.time', return_value=time.time()+901):
            self.assertEqual('expired', self.jobs.get(self.sid, jid)['state'])
            with self.assertRaises(ConflictError): self.store.save(self.aid, config(cookies=''), 0, jid, self.sid)
            self.jobs.sweep()
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT cookies FROM binding_jobs WHERE id=?',(jid,)).fetchone()[0])

    def test_cancel_and_revocation_win_late_worker_callback(self):
        for revoke in (False, True):
            job = self.jobs.start(self.sid, 'qr', 0)
            self.jobs.claim()
            if revoke: self.store.revoke_invite(self.invite['id'])
            else: self.jobs.cancel(self.sid, job['id'])
            self.jobs.update(job['id'], 'ready', identity={'unique_id':'owner_1'}, friends=[], cookies=COOKIE)
            self.assertFalse(self.jobs.alive(job['id']))
            with self.store.connect() as db:
                row = db.execute('SELECT state,cookies FROM binding_jobs WHERE id=?',(job['id'],)).fetchone()
                self.assertNotEqual('ready', row['state'])
                self.assertIsNone(row['cookies'])

    def test_one_active_per_account_and_rate_limit(self):
        job = self.jobs.start(self.sid, 'qr', 0)
        with self.assertRaises(ConflictError): self.jobs.start(self.sid, 'qr', 0)
        self.jobs.cancel(self.sid, job['id'])
        for _ in range(4):
            job = self.jobs.start(self.sid, 'qr', 0); self.jobs.cancel(self.sid, job['id'])
        with self.assertRaises(ValidationError): self.jobs.start(self.sid, 'qr', 0)

    def test_worker_restart_clears_in_progress_and_keeps_ready_result(self):
        jid = self.ready()
        self.jobs.sweep(restart=True)
        self.assertEqual('ready', self.jobs.get(self.sid, jid)['state'])
        new = self.jobs.start(self.sid, 'qr', 0)
        self.jobs.claim();self.jobs.sweep(restart=True)
        self.assertEqual('failed', self.jobs.get(self.sid, new['id'])['state'])
        self.assertEqual('cancelled', self.jobs.get(self.sid, jid)['state'])

    def test_saved_mode_uses_only_this_accounts_saved_cookie(self):
        with self.assertRaises(ValidationError): self.jobs.start(self.sid, 'saved', 0)
        self.store.save(self.aid, config(), 0)
        public = self.jobs.start(self.sid, 'saved', 1)
        job = self.jobs.claim()
        self.assertEqual('owner_1', job['expected_id'])
        self.assertIn(COOKIE, job['cookies'])
        self.assertNotIn(COOKIE, json.dumps(public))

    def test_qr_is_private_and_disappears_after_login(self):
        job = self.jobs.start(self.sid, 'qr', 0);self.jobs.claim()
        png=b'\x89PNG\r\n\x1a\nfixture'
        self.jobs.update(job['id'], 'waiting_scan', qr=png)
        self.assertEqual(png, self.jobs.get(self.sid, job['id'], image=True))
        self.jobs.update(job['id'], 'reading')
        with self.assertRaises(ValidationError): self.jobs.get(self.sid, job['id'], image=True)

    def test_admin_can_edit_explicit_account_but_jobs_stay_session_scoped(self):
        admin,_=self.store.new_session('admin')
        other=self.store.create_invite('另一账号')['account_id']
        with self.assertRaises(AccessError): self.jobs.start(admin,'qr',0)
        with self.assertRaises(AccessError): self.jobs.start(self.sid,'qr',0,other)
        job=self.jobs.start(admin,'qr',0,self.aid); self.jobs.claim()
        self.jobs.update(job['id'],'ready',identity={'unique_id':'owner_1'},friends=[],cookies=COOKIE)
        self.assertEqual('ready',self.jobs.get(admin,job['id'],account_id=self.aid)['state'])
        with self.assertRaises(AccessError): self.jobs.get(admin,job['id'],account_id=other)
        with self.assertRaises(AccessError): self.jobs.get(self.sid,job['id'],account_id=self.aid)
        saved=self.store.save(self.aid,config(cookies=''),0,job['id'],admin)
        self.assertFalse(saved['enabled']);self.assertTrue(saved['pending']['cookies_configured'])


class IdentityReaderTests(unittest.TestCase):
    def test_self_identity_only_comes_from_official_profile_response(self):
        reader = IdentityReader()
        response = Mock(url='https://other.example/aweme/v1/web/user/profile/self/')
        response.json.return_value = {'user': {'unique_id':'attacker', 'nickname':'错误账号'}}
        reader.response(response)
        self.assertIsNone(reader.identity)
        response.url='https://www.douyin.com/aweme/v1/web/user/profile/self/'
        response.json.return_value={'user':{'short_id':'123456789','nickname':'本人'}}
        reader.response(response)
        self.assertEqual({'unique_id':'123456789','nickname':'本人'}, reader.identity)

    def test_empty_identity_responses_and_invalid_ids_are_not_contacts(self):
        reader = IdentityReader()
        response = Mock(url='https://www.douyin.com/aweme/v1/web/im/user/info/')
        response.json.return_value = {'data':None}; reader.response(response)
        response.json.return_value = {'data':[{'nickname':'无身份'}, {'unique_id':'../invalid','nickname':'坏格式'}, {'unique_id':'friend_a','nickname':'朋友'}]}
        reader.response(response)
        self.assertEqual([{'douyin_id':'friend_a','nickname':'朋友','ambiguous':False}], reader.friends({'无身份','坏格式','朋友'}))

    def test_current_conversations_only_exclude_self_and_flag_duplicates(self):
        reader = IdentityReader(); reader.identity={'unique_id':'self'}
        reader.people = {str(i):dict(unique_id=uid,short_id='',nickname=name,remark_name='')
                         for i,(uid,name) in enumerate([('self','本人'),('a','小明'),('b','小明'),('c','小红'),('d','不在会话中')])}
        friends = reader.friends({'本人','小明','小红'})
        self.assertEqual({'a','b','c'}, {p['douyin_id'] for p in friends})
        self.assertEqual({'a','b'}, {p['douyin_id'] for p in friends if p['ambiguous']})


class BindingHTTPTests(unittest.TestCase):
    setUp = portal_tests.HTTPTests.setUp
    tearDown = portal_tests.HTTPTests.tearDown
    request = portal_tests.HTTPTests.request
    login = portal_tests.HTTPTests.login
    def test_admin_editor_has_mfa_session_and_csrf_gates(self):
        invite=self.store.create_invite('管理员配置'); aid=invite['account_id'];route='/spark/api/admin/accounts/'+aid
        owner,owner_csrf=self.login('owner',invite['token'])
        self.assertEqual(403,self.request(route,cookie=owner)[0])
        self.assertEqual(403,self.request(route+'/binding/start',{'mode':'qr','revision':0},owner,owner_csrf)[0])
        admin,csrf=self.login()
        self.assertEqual(200,self.request(route,cookie=admin)[0])
        self.assertEqual(403,self.request(route+'/binding/start',{'mode':'qr','revision':0},admin)[0])
        status,result,_=self.request(route+'/binding/start',{'mode':'qr','revision':0},admin,csrf)
        self.assertEqual(200,status);jid=result['binding']['id']
        self.server.bindings.claim()
        self.server.bindings.update(jid,'ready',identity={'unique_id':'owner_1'},friends=[],cookies=COOKIE)
        self.assertEqual(200,self.request(route+'/binding/'+jid,cookie=admin)[0])
        status,result,_=self.request(route,{'config':config(cookies=''),'revision':0,'binding_id':jid},admin,csrf)
        self.assertEqual(200,status);self.assertFalse(result['account']['enabled'])
        self.assertNotIn(COOKIE,json.dumps(result))

    def test_binding_routes_enforce_owner_csrf_and_no_secrets(self):
        invite=self.store.create_invite('扫码用户'); cookie,csrf=self.login('owner',invite['token'])
        route='/spark/api/binding/start';payload={'mode':'qr','revision':0}
        self.assertEqual(403,self.request(route,payload,cookie,None)[0])
        status,data,_=self.request(route,payload,cookie,csrf)
        self.assertEqual(200,status);jid=data['binding']['id']
        self.assertEqual(403,self.request('/spark/api/binding/'+jid)[0])
        self.server.bindings.claim()
        self.server.bindings.update(jid,'ready',identity={'unique_id':'owner_1'},friends=[],cookies=COOKIE)
        status,data,_=self.request('/spark/api/binding/'+jid,cookie=cookie)
        self.assertNotIn(COOKIE,json.dumps(data));self.assertEqual(200,status)
        status,data,_=self.request('/spark/api/account',{'config':config(cookies=''),'revision':0,'binding_id':jid},cookie,csrf)
        self.assertEqual(200,status);self.assertFalse(data['account']['enabled'])


if __name__ == '__main__':
    unittest.main()
