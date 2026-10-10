import base64
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from portal.auth import AdminAuth, Throttled, totp
from portal.server import PortalServer
from portal.store import AccessError, ConflictError, Store

NOW = 1_800_000_000
PASSWORD = 'only a test password 123'
BOOTSTRAP = 'test-bootstrap-' * 4


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.auth = AdminAuth(self.store)
        self.clock = patch('portal.auth.time.time', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def enroll(self):
        with patch('portal.auth.time.time', return_value=NOW - 60):
            setup = self.auth.begin(BOOTSTRAP, BOOTSTRAP, 'Test')
            codes = self.auth.finish(setup['setup_token'], PASSWORD, totp(setup['secret'], (NOW - 60) // 30))
        return setup['secret'], codes

    def test_rfc6238_sha1_vectors(self):
        secret = base64.b32encode(b'12345678901234567890').decode()
        for seconds, expected in [(59, '94287082'), (1111111109, '07081804'), (1111111111, '14050471'),
                                  (1234567890, '89005924'), (2000000000, '69279037'), (20000000000, '65353130')]:
            self.assertEqual(totp(secret, seconds // 30, 8), expected)

    def test_setup_requires_bootstrap_and_confirmed_totp(self):
        with self.assertRaises(AccessError):
            self.auth.begin('incorrect', BOOTSTRAP, 'Test')
        setup = self.auth.begin(BOOTSTRAP, BOOTSTRAP, 'Test')
        self.assertTrue(setup['qr'].startswith('data:image/svg+xml;base64,'))
        self.assertFalse(self.auth.configured())
        for token, code in [(setup['setup_token'], 'bad'), ('wrong', totp(setup['secret'], NOW // 30))]:
            with self.assertRaises(AccessError):
                self.auth.finish(token, PASSWORD, code)
        self.assertFalse(self.auth.configured())
        self.auth.finish(setup['setup_token'], PASSWORD, totp(setup['secret'], NOW // 30))
        self.assertTrue(self.auth.configured())
        with self.assertRaises(ConflictError):
            self.auth.begin(BOOTSTRAP, BOOTSTRAP, 'Test')
        with self.assertRaises(AccessError):
            self.auth.verify(PASSWORD, totp(setup['secret'], NOW // 30))

    def test_password_and_factor_required_replay_and_clock_window(self):
        secret, _ = self.enroll()
        code = totp(secret, NOW // 30)
        with self.assertRaises(AccessError):
            self.auth.verify('incorrect password', code)
        with self.assertRaises(AccessError):
            self.auth.verify(PASSWORD, totp(secret, NOW // 30 - 3))
        self.auth.verify(PASSWORD, code)
        with self.assertRaises(AccessError):
            self.auth.verify(PASSWORD, code)
        self.auth.verify(PASSWORD, totp(secret, NOW // 30 + 1))

    def test_recovery_requires_password_and_is_one_time(self):
        _, codes = self.enroll()
        self.assertEqual(10, len(set(codes)))
        with self.assertRaises(AccessError):
            self.auth.verify('wrong password', codes[0], recovery=True)
        self.auth.verify(PASSWORD, codes[0].lower(), recovery=True)
        with self.assertRaises(AccessError):
            self.auth.verify(PASSWORD, codes[0], recovery=True)
        database = (Path(self.tmp.name) / 'portal.sqlite3').read_bytes()
        self.assertNotIn(PASSWORD.encode(), database)
        for code in codes:
            self.assertNotIn(code.encode(), database)
            self.assertNotIn(code.replace('-', '').encode(), database)

    def test_rate_limit_survives_restart(self):
        secret, _ = self.enroll()
        for _ in range(5):
            with self.assertRaises(AccessError):
                self.auth.verify('wrong', '000000')
        with self.assertRaises(Throttled):
            AdminAuth(self.store).verify(PASSWORD, totp(secret, NOW // 30))
        with patch('portal.auth.time.time', return_value=NOW + 301):
            self.auth.verify(PASSWORD, totp(secret, (NOW + 301) // 30))

    def test_expired_and_replaced_enrollments_do_not_activate(self):
        first = self.auth.begin(BOOTSTRAP, BOOTSTRAP, 'Test')
        second = self.auth.begin(BOOTSTRAP, BOOTSTRAP, 'Test')
        with self.assertRaises(AccessError):
            self.auth.finish(first['setup_token'], PASSWORD, totp(first['secret'], NOW // 30))
        with patch('portal.auth.time.time', return_value=NOW + 601):
            with self.assertRaises(AccessError):
                self.auth.finish(second['setup_token'], PASSWORD, totp(second['secret'], (NOW + 601) // 30))
        self.assertFalse(self.auth.configured())

    def test_migration_revokes_old_admin_sessions_but_preserves_owner(self):
        with self.store.connect() as db:
            db.execute('DROP TABLE admin_auth')
        old_admin, _ = self.store.new_session('admin')
        invite = self.store.create_invite('Test')
        owner, _ = self.store.new_session('owner', invite['token'])
        AdminAuth(self.store)
        self.assertIsNone(self.store.session(old_admin))
        self.assertEqual(self.store.session(owner)['role'], 'owner')

    def test_http_bootstrap_cannot_login_or_access_admin(self):
        server = PortalServer(('127.0.0.1', 0), self.store, BOOTSTRAP)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, data=None, cookie=None):
            client = http.client.HTTPConnection('127.0.0.1', server.server_port)
            headers = {'Origin': server.public_origin, 'Content-Type': 'application/json'}
            if cookie:
                headers['Cookie'] = cookie
            client.request('POST' if data is not None else 'GET', path,
                           json.dumps(data) if data is not None else None, headers)
            response = client.getresponse()
            result = (response.status, json.loads(response.read()), response.getheader('Set-Cookie'))
            client.close()
            return result
        try:
            self.assertEqual(403, request('/api/login', {'token': BOOTSTRAP})[0])
            self.assertEqual(403, request('/api/admin/accounts')[0])
            status, setup, cookie = request('/api/auth/setup', {'token': BOOTSTRAP})
            self.assertEqual(200, status)
            self.assertIsNone(cookie)
            status, finished, cookie = request('/api/auth/confirm', {'setup_token': setup['setup_token'],
                'password': PASSWORD, 'code': totp(setup['secret'], NOW // 30)})
            self.assertEqual(200, status)
            self.assertIn('recovery_codes', finished)
            self.assertEqual(200, request('/api/admin/accounts', cookie=cookie.split(';')[0])[0])
            self.assertEqual(403, request('/api/login', {'token': BOOTSTRAP})[0])
            self.assertNotIn('secret', request('/api/auth/status')[1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
