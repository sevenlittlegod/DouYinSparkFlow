"""Password + RFC 6238 TOTP. Setup is never an administrator login bypass."""
import base64
import hashlib
import hmac
import io
import json
import re
import secrets
import struct
import time
from urllib.parse import quote, urlencode

from portal.store import AccessError, ConflictError, ValidationError, digest

ITERATIONS = 600_000


class Throttled(AccessError):
    pass


def totp(secret, counter, digits=6):
    key = base64.b32decode(secret + '=' * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = mac[-1] & 15
    value = struct.unpack('>I', mac[offset:offset + 4])[0] & 0x7fffffff
    return str(value % (10 ** digits)).zfill(digits)


def matched_step(secret, code, previous=-1):
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
        return None
    step = int(time.time()) // 30
    for counter in (step, step - 1, step + 1):
        if counter > previous and counter >= 0 and hmac.compare_digest(totp(secret, counter), code):
            return counter
    return None


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), ITERATIONS).hex()


def password_valid(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ValidationError('密码请使用 12–128 个字符；可使用较长的词组。')


def recovery_hash(code):
    if not isinstance(code, str):
        return ''
    normalized = code.strip().replace('-', '').replace(' ', '').upper()
    if not re.fullmatch(r'[0-9A-F]{20}', normalized):
        return ''
    return digest(normalized)


def enrollment_view(secret, token, issuer):
    uri = 'otpauth://totp/' + quote(issuer + ':admin', safe='') + '?' + urlencode({
        'secret': secret, 'issuer': issuer, 'algorithm': 'SHA1', 'digits': 6, 'period': 30})
    # Render locally. A remote QR service must never receive the shared secret.
    import qrcode
    import qrcode.image.svg
    image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage,
                        box_size=8, border=4)
    output = io.BytesIO()
    image.save(output)
    return {'setup_token': token, 'secret': secret, 'expires_in': 600,
            'qr': 'data:image/svg+xml;base64,' + base64.b64encode(output.getvalue()).decode('ascii')}


class AdminAuth:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS admin_auth (
                  id INTEGER PRIMARY KEY CHECK(id=1), password_hash TEXT,
                  salt TEXT, secret TEXT, last_step INTEGER NOT NULL DEFAULT -1,
                  recovery_hashes TEXT, setup_hash TEXT, setup_secret TEXT, setup_expires INTEGER);
                CREATE TABLE IF NOT EXISTS auth_limits (name TEXT PRIMARY KEY, started INTEGER, attempts INTEGER);
            ''')
            if db.execute('INSERT OR IGNORE INTO admin_auth(id) VALUES(1)').rowcount:
                # One-time migration: token-only admin sessions cannot survive MFA.
                db.execute("DELETE FROM sessions WHERE role='admin'")

    def configured(self):
        with self.store.connect() as db:
            return bool(db.execute('SELECT password_hash FROM admin_auth WHERE id=1').fetchone()[0])

    def attempt(self, name):
        now = int(time.time())
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM auth_limits WHERE name=?', (name,)).fetchone()
            if row and now - row['started'] < 300:
                if row['attempts'] >= 5:
                    raise Throttled('尝试次数过多，请 5 分钟后再试。')
                db.execute('UPDATE auth_limits SET attempts=attempts+1 WHERE name=?', (name,))
            else:
                db.execute('INSERT OR REPLACE INTO auth_limits VALUES(?,?,1)', (name, now))

    def begin(self, proof, bootstrap, issuer):
        self.attempt('setup')
        if not isinstance(proof, str) or len(proof) > 200 or not hmac.compare_digest(digest(proof), digest(bootstrap)):
            raise AccessError('初始化凭据不正确。')
        token = secrets.token_urlsafe(32)
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
        view = enrollment_view(secret, token, issuer)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT password_hash FROM admin_auth WHERE id=1').fetchone()[0]:
                raise ConflictError('管理员已绑定验证器，请使用密码和动态码登录。')
            db.execute('UPDATE admin_auth SET setup_hash=?,setup_secret=?,setup_expires=? WHERE id=1',
                       (digest(token), secret, int(time.time()) + 600))
        return view

    def finish(self, token, password, code):
        self.attempt('finish')
        password_valid(password)
        if not isinstance(token, str) or len(token) > 200:
            raise AccessError('绑定会话已失效，请重新初始化。')
        salt = secrets.token_hex(16)
        hashed = password_hash(password, salt)
        codes = [secrets.token_hex(10).upper() for _ in range(10)]
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM admin_auth WHERE id=1').fetchone()
            if row['password_hash']:
                raise ConflictError('管理员已完成绑定。')
            if not row['setup_hash'] or row['setup_expires'] <= int(time.time()) or not hmac.compare_digest(digest(token), row['setup_hash']):
                raise AccessError('绑定会话已失效，请重新初始化。')
            step = matched_step(row['setup_secret'], code)
            if step is None:
                raise AccessError('动态码不正确或已过期；请检查手机自动时间后重试。')
            db.execute('''UPDATE admin_auth SET password_hash=?,salt=?,secret=setup_secret,
                          last_step=?,recovery_hashes=?,setup_hash=NULL,setup_secret=NULL,setup_expires=NULL WHERE id=1''',
                       (hashed, salt, step, json.dumps([recovery_hash(c) for c in codes])))
            db.execute("DELETE FROM sessions WHERE role='admin'")
            db.execute('DELETE FROM auth_limits')
        return ['-'.join(c[i:i + 5] for i in range(0, 20, 5)) for c in codes]

    def verify(self, password, code, recovery=False):
        self.attempt('login')
        error = AccessError('密码或验证信息不正确，或动态码已经使用。')
        if not isinstance(password, str) or len(password) > 128:
            raise error
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM admin_auth WHERE id=1').fetchone()
            if not row['password_hash']:
                raise AccessError('请先完成管理员密码和验证器绑定。')
            correct_password = hmac.compare_digest(password_hash(password, row['salt']), row['password_hash'])
            if recovery:
                code_hash = recovery_hash(code)
                hashes = json.loads(row['recovery_hashes'])
                if not correct_password or not code_hash or code_hash not in hashes:
                    raise error
                hashes.remove(code_hash)
                db.execute('UPDATE admin_auth SET recovery_hashes=? WHERE id=1', (json.dumps(hashes),))
            else:
                step = matched_step(row['secret'], code, row['last_step'])
                if not correct_password or step is None:
                    raise error
                db.execute('UPDATE admin_auth SET last_step=? WHERE id=1', (step,))
            db.execute("DELETE FROM auth_limits WHERE name='login'")


def main():
    """SSH-only recovery. No web endpoint can disable MFA or reset credentials."""
    import argparse
    from pathlib import Path
    from portal.store import Store
    parser = argparse.ArgumentParser(description='Reset administrator authentication using server filesystem authority')
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--reset-admin', action='store_true', required=True)
    args = parser.parse_args()
    store = Store(args.data_dir)
    AdminAuth(store)
    # Rotate bootstrap before resetting so the old initialization key stays invalid.
    path = Path(args.data_dir) / 'admin-token'
    temporary = path.with_suffix('.new')
    with temporary.open('w', encoding='ascii') as out:
        temporary.chmod(0o600)
        out.write(secrets.token_urlsafe(36) + '\n')
    temporary.replace(path)
    with store.connect() as db:
        db.execute('DELETE FROM admin_auth')
        db.execute('INSERT INTO admin_auth(id) VALUES(1)')
        db.execute("DELETE FROM sessions WHERE role='admin'")
        db.execute('DELETE FROM auth_limits')
    print('管理员认证已重置，账号配置和任务保持不变。重启网页服务后，使用新 admin-token 重新绑定。')


if __name__ == '__main__':
    main()
