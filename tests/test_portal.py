import copy
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from portal.server import PortalServer, load_admin_token
from portal.store import AccessError, ConflictError, Store, ValidationError, parse_cookies
from portal.auth import totp


COOKIE = "private_cookie_0123456789"
PUSH = "private_pushplus_token_0123456789"


def config(**changes):
    value = {"username": "测试账号", "unique_id": "owner_1", "cookies": COOKIE,
             "friends": "好友甲 friend.1\n好友 乙 friend_2", "message": "今日续火花\n[API]",
             "hour": 9, "minute": 0, "pushplus_token": PUSH}
    value.update(changes)
    return value


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.invite = self.store.create_invite("测试账号")
        self.account_id = self.invite["account_id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_save_never_activates_and_public_views_redact_secrets(self):
        saved = self.store.save(self.account_id, config(), 0)
        self.assertEqual([], self.store.active_accounts())
        self.assertEqual(1, saved["revision"])
        rendered = json.dumps(self.store.accounts())
        for secret in (COOKIE, PUSH, self.invite["token"]):
            self.assertNotIn(secret, rendered)
        self.assertTrue(saved["pending"]["cookies_configured"])

    def test_pending_edit_keeps_approved_snapshot_and_exact_revision(self):
        self.store.save(self.account_id, config(), 0)
        self.store.approve(self.account_id, 1)
        self.store.save(self.account_id, config(cookies="", pushplus_token="", message="待审核的新内容"), 1)
        active = self.store.active_accounts()[0]
        self.assertEqual(1, active["revision"])
        self.assertEqual("今日续火花\n[API]", active["config"]["message"])
        with self.assertRaises(ConflictError):
            self.store.approve(self.account_id, 1)
        with self.assertRaises(ConflictError):
            self.store.save(self.account_id, config(), 1)
        self.store.approve(self.account_id, 2)
        active = self.store.active_accounts()[0]
        self.assertEqual("待审核的新内容", active["config"]["message"])
        self.assertEqual(COOKIE, active["config"]["cookies"][0]["value"])
        self.assertEqual(PUSH, active["config"]["pushplus_token"])

    def test_duplicate_enabled_identity_rejected_then_disable(self):
        self.store.save(self.account_id, config(), 0)
        self.store.approve(self.account_id, 1)
        other = self.store.create_invite("另一个配置")["account_id"]
        self.store.save(other, config(unique_id="OWNER_1"), 0)
        with self.assertRaises(ConflictError):
            self.store.approve(other, 1)
        self.store.disable(self.account_id)
        self.store.approve(other, 1)
        self.assertEqual([other], [a["id"] for a in self.store.active_accounts()])

    def test_owner_session_revocation_and_expiry(self):
        sid, session = self.store.new_session("owner", self.invite["token"])
        self.assertEqual(self.account_id, session["account_id"])
        self.store.revoke_invite(self.invite["id"])
        self.assertIsNone(self.store.session(sid))
        with self.assertRaises(AccessError):
            self.store.new_session("owner", self.invite["token"])
        invite = self.store.create_invite("过期")
        with self.store.connect() as db:
            db.execute("UPDATE invites SET expires_at=? WHERE id=?", (int(time.time()) - 1, invite["id"]))
        with self.assertRaises(AccessError):
            self.store.new_session("owner", invite["token"])

    def test_reenable_approved_version_preserves_pending_and_checks_conflicts(self):
        self.store.save(self.account_id, config(), 0)
        self.store.approve(self.account_id, 1)
        self.store.disable(self.account_id)
        self.store.save(self.account_id, config(message="尚未批准"), 1)
        with self.assertRaises(ConflictError):
            self.store.enable(self.account_id, 2)
        self.store.enable(self.account_id, 1)
        self.assertEqual("今日续火花\n[API]", self.store.active_accounts()[0]["config"]["message"])
        self.assertIsNotNone(self.store.account(self.account_id)["pending"])
        self.store.disable(self.account_id)
        other = self.store.create_invite("重复账号")["account_id"]
        self.store.save(other, config(), 0)
        self.store.approve(other, 1)
        with self.assertRaises(ConflictError):
            self.store.enable(self.account_id, 1)

    def test_exported_nameless_cookie_keeps_validation(self):
        session = {"name": "sessionid", "value": COOKIE}
        nameless = {"name": "", "value": "browser-export", "domain": ".douyin.com"}
        self.assertEqual(parse_cookies([session, nameless])[1]['name'], '')
        for invalid in ([nameless], [session, {**nameless, 'domain': 'other.example'}],
                        [session, {**nameless, 'value': 'bad\nvalue'}]):
            with self.assertRaises(ValidationError):
                parse_cookies(invalid)

    def test_cookie_input_forms_and_domain_restrictions(self):
        for raw in (COOKIE, "sessionid=" + COOKIE + "; ttwid=abc", json.dumps({"cookies": [{"name": "sessionid", "value": COOKIE, "url": "https://www.douyin.com", "sameSite": "unspecified"}]})):
            with self.subTest(raw_type=type(raw)):
                parsed = parse_cookies(raw)
                self.assertEqual(COOKIE, parsed[0]["value"])
                self.assertNotIn("sameSite", parsed[0])
        for domain in ("evil.example", "douyin.com.evil.example", "evil-douyin.com", "https://douyin.com"):
            with self.assertRaises(ValidationError):
                parse_cookies([{"name": "sessionid", "value": COOKIE, "domain": domain}])
        with self.assertRaises(ValidationError):
            parse_cookies("sessionid=bad\nheader")

    def test_friend_validation_and_explicit_push_removal(self):
        for changes in ({"friends": "好友 abc\n同号 ABC"}, {"friends": "只有昵称"}, {"unique_id": "../outside"}, {"hour": True}, {"friends": "自己 owner_1"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.store.save(self.account_id, config(**changes), 0)
        self.store.save(self.account_id, config(), 0)
        self.store.save(self.account_id, config(cookies="", clear_pushplus=True), 1)
        self.store.approve(self.account_id, 2)
        self.assertEqual("", self.store.active_accounts()[0]["config"]["pushplus_token"])

    def test_run_summary_cannot_contain_arbitrary_logs(self):
        self.store.update_run_status(self.account_id, {"status": "failed", "failed": 1, "cookies": COOKIE, "error": PUSH, "logs": COOKIE})
        summary = self.store.account(self.account_id)["run_status"]
        self.assertEqual({"status": "failed", "failed": 1}, summary)

    @unittest.skipIf(os.name == "nt", "POSIX permission test")
    def test_storage_permissions(self):
        self.assertEqual(0o700, Path(self.temp.name).stat().st_mode & 0o777)
        self.assertEqual(0o600, self.store.path.stat().st_mode & 0o777)

    def test_bootstrap_token_is_stable_and_not_printed(self):
        path = Path(self.temp.name) / "admin-token"
        first = load_admin_token(path)
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual(first, load_admin_token(path))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.server = PortalServer(("127.0.0.1", 0), self.store, "admin-token-" * 5, base_path="/spark")
        self.password = 'test administrator password'
        setup_time = int(time.time()) - 60
        with patch('portal.auth.time.time', return_value=setup_time):
            enrollment = self.server.admin_auth.begin(self.server.admin_token, self.server.admin_token, 'Test')
            self.secret = enrollment['secret']
            self.server.admin_auth.finish(enrollment['setup_token'], self.password, totp(self.secret, setup_time // 30))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = self.server.public_origin

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, data=None, cookie=None, csrf=None, origin=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {}
        if host:
            headers["Host"] = host
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-CSRF-Token"] = csrf
        if data is not None:
            headers.update({"Content-Type": "application/json", "Origin": origin or self.origin})
        connection.request("POST" if data is not None else "GET", path, json.dumps(data) if data is not None else None, headers)
        response = connection.getresponse()
        raw = response.read()
        result = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw.decode()
        response_headers = dict(response.getheaders())
        status = response.status
        connection.close()
        return status, result, response_headers

    def login(self, role="admin", token=None):
        payload = {'password': self.password, 'code': totp(self.secret, int(time.time()) // 30)} if role == 'admin' else {'token': token}
        status, data, headers = self.request("/spark/api/" + ("login" if role == "admin" else "exchange"), payload)
        self.assertEqual(200, status)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("Path=/spark/", headers["Set-Cookie"])
        return headers["Set-Cookie"].split(";", 1)[0], data["session"]["csrf"]

    def test_authorization_csrf_origin_host_and_no_secrets(self):
        invite = self.store.create_invite("用户甲")
        cookie, csrf = self.login("owner", invite["token"])
        other = self.store.create_invite("用户乙")
        status, result, _ = self.request("/spark/api/account", {"account_id": other["account_id"], "config": config(), "revision": 0}, cookie, csrf)
        self.assertEqual(200, status)
        self.assertEqual(invite["account_id"], result["account"]["id"])
        self.assertEqual(0, self.store.account(other["account_id"])["revision"])
        self.assertNotIn(COOKIE, json.dumps(result))
        self.assertNotIn(PUSH, json.dumps(result))
        for path, data, sent_csrf, origin in [
            ("/spark/api/admin/accounts", None, csrf, None),
            ("/spark/api/admin/invites", {"label": "越权"}, csrf, None),
            ("/spark/api/account", {"config": config(), "revision": 1}, None, None),
            ("/spark/api/account", {"config": config(), "revision": 1}, csrf, "https://evil.example")
        ]:
            with self.subTest(path=path, origin=origin):
                self.assertEqual(403, self.request(path, data, cookie, sent_csrf, origin)[0])
        self.assertEqual(403, self.request("/spark/", host="attacker.example")[0])

    def test_full_invitation_review_flow_and_stripped_proxy_path(self):
        admin_cookie, admin_csrf = self.login()
        status, body, _ = self.request("/api/admin/invites", {"label": "小王的续火花", "days": 7}, admin_cookie, admin_csrf)
        self.assertEqual(200, status)
        url = body["invite"]["url"]
        self.assertIn("/spark/#invite=", url)
        account_id = body["invite"]["account_id"]
        owner_cookie, owner_csrf = self.login("owner", url.split("#invite=", 1)[1])
        self.assertEqual(200, self.request("/api/account", {"config": config(), "revision": 0}, owner_cookie, owner_csrf)[0])
        self.assertEqual([], self.store.active_accounts())

        approve = f"/api/admin/accounts/{account_id}/approve"
        self.assertEqual(409, self.request(approve, {"revision": 0}, admin_cookie, admin_csrf)[0])
        self.assertEqual(200, self.request(approve, {"revision": 1}, admin_cookie, admin_csrf)[0])
        self.assertEqual(1, len(self.store.active_accounts()))
        self.assertEqual(200, self.request(f"/api/admin/accounts/{account_id}/disable", {}, admin_cookie, admin_csrf)[0])
        self.assertEqual([], self.store.active_accounts())
        enable = f"/api/admin/accounts/{account_id}/enable"
        self.assertEqual(409, self.request(enable, {"active_revision": 2}, admin_cookie, admin_csrf)[0])
        self.assertEqual(200, self.request(enable, {"active_revision": 1}, admin_cookie, admin_csrf)[0])
        self.assertEqual(1, len(self.store.active_accounts()))

    def test_static_security_headers_base_path_and_no_filesystem_escape(self):
        status, body, headers = self.request("/spark/")
        self.assertEqual(200, status)
        self.assertIn('src="/spark/app.js"', body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual("no-store", headers["Cache-Control"])
        for path in ("/spark/../store.py", "/spark/%2e%2e/admin-token", "/portal-data/portal.sqlite3"):
            self.assertEqual(404, self.request(path)[0])


if __name__ == "__main__":
    unittest.main()
