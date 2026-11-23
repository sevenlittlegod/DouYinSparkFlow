import concurrent.futures
from datetime import datetime, timezone
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from portal.store import AccessError, ConflictError, Store, manual_day
import test_portal as fixtures


class ManualQueueTests(unittest.TestCase):
    def setUp(self):
        clock = patch("portal.store.time.time", return_value=1767348000)
        clock.start()
        self.addCleanup(clock.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.invite = self.store.create_invite("测试")
        self.account = self.invite["account_id"]
        self.sid, _ = self.store.new_session("owner", self.invite["token"])
        self.store.save(self.account, fixtures.config(), 0)
        self.store.approve(self.account, 1)

    def state(self):
        return self.store.account(self.account)["manual_run"]

    def enqueue(self, revision=1, prior=None, day=None):
        return self.store.request_manual_run(self.account, revision, day or self.state()["day"], prior, self.sid)

    def test_concurrent_posts_share_one_request_and_retry_needs_a_fresh_click(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(lambda _: self.enqueue(), range(12)))
        ids = {r["request"]["id"] for r in responses}
        self.assertEqual(1, len(ids))
        first = ids.pop()
        claimed = self.store.claim_manual_request()
        self.assertEqual((first, self.account, 1), (claimed["id"], claimed["account_id"], claimed["revision"]))
        self.store.finish_manual_request(first, "partial")
        self.assertFalse(self.enqueue(prior=first)["available"])
        with patch('portal.store.time.time', return_value=time.time() + 61):
            self.assertTrue(self.state()["available"])
            replay = self.enqueue(prior=None)
            self.assertEqual(first, replay["request"]["id"])
            self.assertNotEqual(first, self.enqueue(prior=first)["request"]["id"])

    def test_approval_disable_and_edit_invalidate_queued_work(self):
        self.enqueue()
        self.store.save(self.account, fixtures.config(cookies="changed_cookie_01234"), 1)
        with self.assertRaises(ConflictError):
            self.enqueue()
        self.assertIsNone(self.store.claim_manual_request())
        self.assertEqual("configuration_changed", self.state()["request"]["status"])
        self.store.approve(self.account, 2)
        with self.assertRaises(ConflictError):
            self.enqueue(revision=1)
        self.enqueue(revision=2, prior=self.state()["request"]["id"])
        self.store.disable(self.account)
        self.assertIsNone(self.store.claim_manual_request())
        self.store.enable(self.account, 2)
        self.assertEqual("unavailable", self.state()["request"]["status"])

    def test_crash_does_not_requeue_and_late_completion_cannot_overwrite_outcome(self):
        ident = self.enqueue()["request"]["id"]
        self.assertIsNotNone(self.store.claim_manual_request())
        self.assertIsNone(self.store.claim_manual_request())
        self.assertEqual("interrupted", self.state()["request"]["status"])
        self.store.finish_manual_request(ident, "completed")
        self.assertEqual("interrupted", self.state()["request"]["status"])

    def test_beijing_day_change_expires_request_and_rejects_old_click(self):
        self.assertEqual("2026-01-02", manual_day(datetime(2026, 1, 1, 16, tzinfo=timezone.utc).timestamp()))
        old_day = self.state()["day"]
        self.enqueue()
        with patch("portal.store.manual_day", return_value="2099-01-01"):
            with self.assertRaises(ConflictError):
                self.enqueue(day=old_day)
            self.assertIsNone(self.store.claim_manual_request())
            self.assertIsNone(self.state()["request"])
            self.assertEqual("queued", self.enqueue()["request"]["status"])

    def test_today_submitted_is_blocked_but_yesterdays_summary_allows_a_new_day(self):
        self.store.update_run_status(self.account, {"status": "submitted_unverified",
                                                   "started_at": datetime.fromtimestamp(time.time(), timezone.utc).isoformat()})
        self.assertFalse(self.state()["available"])
        self.assertIsNone(self.enqueue()["request"])
        self.store.update_run_status(self.account, {"status": "submitted_unverified",
                                                   "started_at": "2000-01-01T12:00:00+00:00"})
        self.assertEqual("queued", self.enqueue()["request"]["status"])

    def test_migrate_existing_approved_identity_and_keep_old_revision_after_reapproval(self):
        with self.store.connect() as db:
            db.execute("DROP TABLE approved_identities")
        self.store = Store(self.temp.name)
        self.assertEqual("owner_1", self.store.approved_identity(self.account, 1))
        self.store.save(self.account, fixtures.config(unique_id="owner_new"), 1)
        self.store.approve(self.account, 2)
        self.assertEqual("owner_1", self.store.approved_identity(self.account, 1))
        self.assertEqual("owner_new", self.store.approved_identity(self.account, 2))
        self.assertIsNone(self.store.approved_identity(self.account, 99))

    def test_revoked_invitation_cannot_enqueue_and_no_private_data_in_state(self):
        self.store.revoke_invite(self.invite["id"])
        with self.assertRaises(AccessError):
            self.enqueue()
        public = json.dumps(self.state())
        for value in (self.invite["token"], self.sid, fixtures.COOKIE, fixtures.PUSH):
            self.assertNotIn(value, public)


class ManualHTTPTests(unittest.TestCase):
    setUp = fixtures.HTTPTests.setUp
    tearDown = fixtures.HTTPTests.tearDown
    request = fixtures.HTTPTests.request
    login = fixtures.HTTPTests.login

    def test_only_owner_or_admin_can_request_current_approved_account_with_csrf(self):
        invite = self.store.create_invite("账号")
        other = self.store.create_invite("其他账号")
        aid = invite["account_id"]
        self.store.save(aid, fixtures.config(), 0)
        self.store.approve(aid, 1)
        owner, csrf = self.login("owner", invite["token"])
        admin, acsrf = self.login()
        admin_path = "/api/admin/accounts/" + aid + "/manual-run"
        owner_path = "/api/manual-run"
        data = {"revision": 1, "day": self.store.account(aid)["manual_run"]["day"], "previous_request_id": None,
                "account_id": other["account_id"]}
        for path, cookie, token, origin in [
            (admin_path, owner, csrf, None), (owner_path, None, None, None),
            (owner_path, owner, None, None), (owner_path, owner, csrf, "https://wrong.example"),
            (owner_path, admin, acsrf, None),
        ]:
            self.assertEqual(403, self.request(path, data, cookie, token, origin)[0])
        status, result, _ = self.request(owner_path, data, owner, csrf)
        self.assertEqual(200, status)
        self.assertEqual("queued", result["manual_run"]["request"]["status"])
        self.assertIsNone(self.store.account(other["account_id"])["manual_run"]["request"])
        status, again, _ = self.request(admin_path, data, admin, acsrf)
        self.assertEqual(200, status)
        self.assertEqual(result, again)
