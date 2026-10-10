"""Private, short-lived login jobs shared with an isolated browser worker."""
import json
import secrets
import time

from portal.store import AccessError, ConflictError, ValidationError, digest, parse_cookies

LIVE = ('queued', 'starting', 'waiting_scan', 'reading')
ERRORS = {
    'expired': '本次操作已过期，请重新开始。',
    'cancelled': '已取消，尚未修改账号配置。',
    'interrupted': '扫码服务重启，本次操作已停止，请重新开始。',
    'login_required': '保存的登录已失效，请改用扫码绑定。',
    'verification_required': '抖音要求额外验证，请在本人抖音 App 完成；仍无法登录时联系管理员。',
    'identity_mismatch': '扫码账号与本配置的抖音号不一致，未保存，请使用正确账号。',
    'identity_unavailable': '未取得本人抖音号，不能确认绑定身份，请稍后重试。',
    'page_unavailable': '抖音页面未能加载，请稍后重试。',
    'browser_unavailable': '浏览器暂时不可用，请联系管理员。',
}


class Bindings:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS binding_jobs (
                  id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
                  session_hash TEXT NOT NULL, revision INTEGER NOT NULL, mode TEXT NOT NULL,
                  state TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL,
                  updated INTEGER NOT NULL, expected_id TEXT, qr BLOB, identity TEXT,
                  friends TEXT, cookies TEXT, partial INTEGER NOT NULL DEFAULT 1, error TEXT);
                CREATE INDEX IF NOT EXISTS binding_queue ON binding_jobs(state,created);
            ''')

    @staticmethod
    def _owner(db, sid):
        session = db.execute('SELECT * FROM sessions WHERE token_hash=? AND role=? AND expires_at>?',
                             (digest(sid), 'owner', int(time.time()))).fetchone()
        if not session:
            raise AccessError('请通过本人的邀请链接登录。')
        return session

    @staticmethod
    def _owned(db, sid, job_id):
        session = Bindings._owner(db, sid)
        row = db.execute('SELECT * FROM binding_jobs WHERE id=? AND account_id=? AND session_hash=?',
                         (job_id, session['account_id'], digest(sid))).fetchone()
        if not row:
            raise AccessError('本次扫码不存在或不属于当前登录。')
        return row

    @staticmethod
    def _public(row):
        expired = row['expires'] <= int(time.time())
        state = 'expired' if expired and row['state'] != 'consumed' else row['state']
        ready = state == 'ready'
        return dict(id=row['id'], mode=row['mode'], state=state, expires_at=row['expires'],
                    qr_version=row['updated'] if state == 'waiting_scan' and row['qr'] else None,
                    identity=json.loads(row['identity']) if ready and row['identity'] else None,
                    friends=json.loads(row['friends']) if ready and row['friends'] else [],
                    partial=bool(row['partial']),
                    error=ERRORS.get('expired' if state == 'expired' else row['error']))

    def start(self, sid, mode, revision):
        if mode not in ('qr', 'saved') or type(revision) is not int:
            raise ValidationError('操作类型或配置版本不正确。')
        now = int(time.time())
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            owner = self._owner(db, sid)
            account = db.execute('SELECT * FROM accounts WHERE id=?', (owner['account_id'],)).fetchone()
            if account['revision'] != revision:
                raise ConflictError('配置已变化，请刷新页面后重新开始。')
            if db.execute("SELECT 1 FROM binding_jobs WHERE account_id=? AND state IN ('queued','starting','waiting_scan','reading') AND expires>?", (owner['account_id'], now)).fetchone():
                raise ConflictError('这个账号已有扫码或读取操作，请先完成或取消。')
            if db.execute('SELECT COUNT(*) FROM binding_jobs WHERE account_id=? AND created>?',
                          (owner['account_id'], now - 900)).fetchone()[0] >= 5:
                raise ValidationError('操作较频繁，请 15 分钟后再试。')
            if db.execute("SELECT COUNT(*) FROM binding_jobs WHERE state IN ('queued','starting','waiting_scan','reading') AND expires>?", (now,)).fetchone()[0] >= 10:
                raise ValidationError('当前等待人数较多，请稍后再试。')
            config = json.loads(account['pending'] or account['active'] or '{}')
            if mode == 'saved' and not config.get('cookies'):
                raise ValidationError('尚未保存登录，请先扫码绑定。')
            # Replacing a ready result cancels its credential, even across tabs.
            db.execute("UPDATE binding_jobs SET state='cancelled',cookies=NULL,qr=NULL,friends=NULL,identity=NULL,error='cancelled' WHERE account_id=? AND state='ready'", (owner['account_id'],))
            job_id = secrets.token_hex(16)
            db.execute('''INSERT INTO binding_jobs(id,account_id,session_hash,revision,mode,state,created,expires,updated,expected_id,cookies)
                          VALUES(?,?,?,?,?,'queued',?,?,?,?,?)''',
                       (job_id, owner['account_id'], digest(sid), revision, mode, now, now + 900, now,
                        config.get('unique_id'), json.dumps(config['cookies']) if mode == 'saved' else None))
            return self._public(db.execute('SELECT * FROM binding_jobs WHERE id=?', (job_id,)).fetchone())

    def current(self, sid):
        with self.store.connect() as db:
            owner = self._owner(db, sid)
            row = db.execute('SELECT * FROM binding_jobs WHERE account_id=? AND session_hash=? ORDER BY created DESC,rowid DESC LIMIT 1',
                             (owner['account_id'], digest(sid))).fetchone()
            return self._public(row) if row else None

    def get(self, sid, job_id, image=False):
        with self.store.connect() as db:
            row = self._owned(db, sid, job_id)
            if image:
                if row['state'] != 'waiting_scan' or row['expires'] <= int(time.time()) or not row['qr']:
                    raise ValidationError('二维码尚未就绪或已过期。')
                return bytes(row['qr'])
            return self._public(row)

    def cancel(self, sid, job_id):
        with self.store.connect() as db:
            self._owned(db, sid, job_id)
            db.execute("UPDATE binding_jobs SET state='cancelled',error='cancelled',qr=NULL,cookies=NULL,friends=NULL,identity=NULL WHERE id=? AND state!='consumed'", (job_id,))

    @staticmethod
    def apply(db, sid, job_id, account_id, revision, payload):
        row = Bindings._owned(db, sid, job_id)
        if row['account_id'] != account_id or row['revision'] != revision:
            raise ConflictError('扫码后配置已变化，请重新扫码或读取。')
        if row['state'] != 'ready' or row['expires'] <= int(time.time()) or not row['cookies']:
            raise ConflictError('扫码结果尚未就绪、已过期或已使用，请重新开始。')
        identity = json.loads(row['identity'])
        if not isinstance(payload, dict) or payload.get('unique_id') != identity['unique_id'] or payload.get('cookies'):
            raise ValidationError('请使用页面核实的抖音号；扫码后无需再粘贴 Cookie。')
        payload = {**payload, 'cookies': json.loads(row['cookies'])}
        db.execute("UPDATE binding_jobs SET state='consumed',cookies=NULL,qr=NULL,friends=NULL,identity=NULL WHERE id=?", (job_id,))
        return payload

    def sweep(self, restart=False):
        now = int(time.time())
        with self.store.connect() as db:
            db.execute("UPDATE binding_jobs SET state='expired',error='expired',qr=NULL,cookies=NULL,friends=NULL,identity=NULL WHERE state!='consumed' AND (expires<=? OR NOT EXISTS(SELECT 1 FROM sessions WHERE token_hash=session_hash AND expires_at>?))", (now, now))
            if restart:
                db.execute("UPDATE binding_jobs SET state='failed',error='interrupted',qr=NULL,cookies=NULL,friends=NULL,identity=NULL WHERE state IN ('starting','waiting_scan','reading')")
            db.execute('DELETE FROM binding_jobs WHERE created<?', (now - 86400,))

    def claim(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM binding_jobs WHERE state='queued' AND expires>? ORDER BY created,rowid LIMIT 1", (int(time.time()),)).fetchone()
            if not row:
                return None
            db.execute("UPDATE binding_jobs SET state='starting',updated=? WHERE id=?", (int(time.time()), row['id']))
            return dict(row)

    def alive(self, job_id):
        with self.store.connect() as db:
            return bool(db.execute("SELECT 1 FROM binding_jobs j JOIN sessions s ON s.token_hash=j.session_hash WHERE j.id=? AND j.state IN ('starting','waiting_scan','reading') AND j.expires>? AND s.expires_at>?", (job_id, int(time.time()), int(time.time()))).fetchone())

    def update(self, job_id, state, *, qr=None, identity=None, friends=None, cookies=None, partial=True, error=None):
        if state not in ('waiting_scan', 'reading', 'ready', 'failed') or (error and error not in ERRORS):
            raise ValueError('Invalid binding state')
        if qr is not None and (not isinstance(qr, bytes) or len(qr) > 1024 * 1024 or not qr.startswith(b'\x89PNG')):
            raise ValueError('Invalid QR image')
        if state == 'ready':
            cookies = parse_cookies(cookies)
            from portal.store import ID
            if not identity or not ID.fullmatch(identity.get('unique_id', '')):
                raise ValueError('Missing verified identity')
            if not isinstance(friends, list) or len(friends) > 500:
                raise ValueError('Invalid conversation list')
        with self.store.connect() as db:
            # Cancel/expiry/revocation wins races with late browser callbacks.
            db.execute("""UPDATE binding_jobs SET state=?,updated=?,qr=?,identity=?,friends=?,cookies=?,partial=?,error=?
                WHERE id=? AND state IN ('starting','waiting_scan','reading') AND expires>?
                AND EXISTS(SELECT 1 FROM sessions WHERE token_hash=session_hash AND expires_at>?)""",
                (state, int(time.time()), qr, json.dumps(identity) if identity else None,
                 json.dumps(friends) if friends is not None else None,
                 json.dumps(cookies) if cookies else None, int(partial), error, job_id, int(time.time()), int(time.time())))
