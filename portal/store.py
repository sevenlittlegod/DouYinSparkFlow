"""SQLite snapshots. Secrets stay on disk and never appear in public views."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit


class ValidationError(ValueError):
    pass


class ConflictError(ValidationError):
    pass


class AccessError(ValidationError):
    pass


ID = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def clean_text(value, label, maximum, required=True):
    if not isinstance(value, str):
        raise ValidationError(f"{label}必须是文字。")
    value = value.strip()
    if (required and not value) or len(value) > maximum or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValidationError(f"{label}长度或格式不正确。")
    return value


def parse_cookies(raw):
    """Accept exported cookies, Cookie header, or a sessionid; Douyin only."""
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith(("[", "{")):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raise ValidationError("Cookie JSON 格式不正确。") from None
        elif "=" in raw:
            pairs = [part.strip().split("=", 1) for part in raw.split(";") if part.strip()]
            if any(len(p) != 2 for p in pairs):
                raise ValidationError("请填写完整 Cookie，或只填写 sessionid 的值。")
            raw = [{"name": name.strip(), "value": value.strip(), "domain": ".douyin.com", "path": "/"} for name, value in pairs]
        else:
            raw = [{"name": "sessionid", "value": raw, "domain": ".douyin.com", "path": "/"}]
    if isinstance(raw, dict):
        raw = raw.get("cookies")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 150:
        raise ValidationError("请填写有效的抖音登录 Cookie 或 sessionid。")
    result = []
    for cookie in raw:
        if not isinstance(cookie, dict):
            raise ValidationError("Cookie 每一项必须是对象。")
        name, value = cookie.get("name"), cookie.get("value")
        # Chromium can export a nameless cookie. Preserve it as the runner does;
        # it still passes value/domain/path checks and cannot satisfy sessionid.
        if not isinstance(name, str) or not re.fullmatch(r"[\w-]{0,120}", name, flags=re.ASCII):
            raise ValidationError("Cookie 名称不正确。")
        if not isinstance(value, str) or len(value) > 12000 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValidationError("Cookie 值格式不正确。")
        domain = cookie.get("domain", ".douyin.com")
        if "url" in cookie:
            try:
                url = urlsplit(cookie["url"])
                if url.scheme != "https" or url.username or url.password or url.port not in (None, 443):
                    raise ValueError()
                domain = url.hostname or ""
            except (ValueError, TypeError):
                raise ValidationError("Cookie URL 必须是抖音 HTTPS 地址。") from None
        if not isinstance(domain, str) or not (domain.lstrip(".") == "douyin.com" or domain.lstrip(".").endswith(".douyin.com")) or not re.fullmatch(r"[.A-Za-z0-9-]+", domain):
            raise ValidationError("只接受 douyin.com 域名的 Cookie。")
        path = cookie.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 300 or any(ord(c) < 32 for c in path):
            raise ValidationError("Cookie path 格式不正确。")
        item = {"name": name, "value": value, "domain": domain, "path": path}
        for flag in ("httpOnly", "secure"):
            if flag in cookie:
                if not isinstance(cookie[flag], bool):
                    raise ValidationError("Cookie 标志必须为布尔值。")
                item[flag] = cookie[flag]
        # Exporters use incompatible SameSite / expiration formats. Session cookies
        # intentionally omit those optional attributes, matching the existing runner.
        result.append(item)
    if not any(c["name"] in ("sessionid", "sessionid_ss") and len(c["value"]) >= 8 for c in result):
        raise ValidationError("Cookie 缺少有效 sessionid；请先在抖音网页版登录。")
    return result


def validate_config(payload, previous=None):
    if not isinstance(payload, dict):
        raise ValidationError("配置必须是对象。")
    previous = previous or {}
    username = clean_text(payload.get("username", ""), "账号备注", 60)
    unique_id = clean_text(payload.get("unique_id", ""), "本人抖音号", 64)
    if not ID.fullmatch(unique_id):
        raise ValidationError("本人抖音号只能包含字母、数字、点、下划线或短横线。")
    raw_friends = payload.get("friends")
    if isinstance(raw_friends, str):
        rows = [line.strip() for line in raw_friends.splitlines() if line.strip()]
        raw_friends = []
        for row in rows:
            parts = re.split(r"[，,\t]+|\s+", row)
            if len(parts) < 2:
                raise ValidationError("好友每行填写“昵称 抖音号”，中间用空格或逗号隔开。")
            raw_friends.append({"nickname": " ".join(parts[:-1]), "douyin_id": parts[-1]})
    if not isinstance(raw_friends, list) or not 1 <= len(raw_friends) <= 200:
        raise ValidationError("请填写 1–200 位好友。")
    friends, seen = [], set()
    for friend in raw_friends:
        if not isinstance(friend, dict):
            raise ValidationError("好友格式不正确。")
        nickname = clean_text(friend.get("nickname", ""), "好友昵称", 80)
        douyin_id = clean_text(friend.get("douyin_id", ""), "好友抖音号", 64)
        if not ID.fullmatch(douyin_id) or douyin_id.lower() in seen:
            raise ValidationError("好友抖音号无效或重复；请检查名单。")
        if douyin_id.lower() == unique_id.lower():
            raise ValidationError("好友名单不能包含本人抖音号。")
        seen.add(douyin_id.lower())
        friends.append({"nickname": nickname, "douyin_id": douyin_id})
    message = clean_text(payload.get("message", ""), "消息内容", 2000)
    hour, minute = payload.get("hour", 9), payload.get("minute", 0)
    if type(hour) is not int or type(minute) is not int or not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValidationError("执行时间不正确。")
    cookies = parse_cookies(payload["cookies"]) if payload.get("cookies") else previous.get("cookies")
    if not cookies:
        raise ValidationError("首次配置需要登录 Cookie 或 sessionid。")
    if payload.get("clear_pushplus") is True:
        pushplus = ""
    else:
        pushplus = payload.get("pushplus_token", "") or previous.get("pushplus_token", "")
    if not isinstance(pushplus, str) or (pushplus and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", pushplus)):
        raise ValidationError("PushPlus Token 格式不正确。")
    return dict(username=username, unique_id=unique_id, cookies=cookies, friends=friends,
                message=message, hour=hour, minute=minute, pushplus_token=pushplus)


def public_config(config):
    if config is None:
        return None
    return {**{k: v for k, v in config.items() if k not in ("cookies", "pushplus_token")},
            "cookies_configured": bool(config.get("cookies")),
            "pushplus_configured": bool(config.get("pushplus_token"))}


class Store:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        self.path = self.data_dir / "portal.sqlite3"
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
              id TEXT PRIMARY KEY, label TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
              pending TEXT, active TEXT, active_revision INTEGER, enabled INTEGER NOT NULL DEFAULT 0,
              updated_at INTEGER NOT NULL, run_status TEXT);
            CREATE TABLE IF NOT EXISTS invites (
              id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), token_hash TEXT UNIQUE NOT NULL,
              expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY, role TEXT NOT NULL, account_id TEXT, invite_id TEXT,
              csrf TEXT NOT NULL, expires_at INTEGER NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_invite(self, label, days=7, account_id=None):
        label = clean_text(label, "邀请备注", 60)
        if type(days) is not int or not 1 <= days <= 30:
            raise ValidationError("邀请有效期为 1–30 天。")
        now = int(time.time())
        token, invite_id = secrets.token_urlsafe(32), secrets.token_hex(16)
        with self.connect() as db:
            if account_id:
                if not db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
                    raise ValidationError("账号不存在。")
            else:
                account_id = secrets.token_hex(16)
                db.execute("INSERT INTO accounts(id,label,updated_at) VALUES(?,?,?)", (account_id, label, now))
            db.execute("INSERT INTO invites(id,account_id,token_hash,expires_at) VALUES(?,?,?,?)",
                       (invite_id, account_id, digest(token), now + days * 86400))
        return {"id": invite_id, "account_id": account_id, "token": token, "expires_at": now + days * 86400}

    def revoke_invite(self, invite_id):
        with self.connect() as db:
            if db.execute("UPDATE invites SET revoked=1 WHERE id=?", (invite_id,)).rowcount != 1:
                raise ValidationError("邀请不存在。")
            db.execute("DELETE FROM sessions WHERE invite_id=?", (invite_id,))

    def new_session(self, role, token=None):
        now = int(time.time())
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        account_id, invite_id, expiry = None, None, now + 12 * 3600
        with self.connect() as db:
            if role == "owner":
                row = db.execute("SELECT * FROM invites WHERE token_hash=? AND revoked=0 AND expires_at>?", (digest(token or ""), now)).fetchone()
                if row is None:
                    raise AccessError("邀请无效、已过期或已撤销。")
                account_id, invite_id = row["account_id"], row["id"]
                expiry = min(row["expires_at"], now + 7 * 86400)
            elif role != "admin":
                raise AccessError("无效角色。")
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (digest(sid), role, account_id, invite_id, csrf, expiry))
        return sid, {"role": role, "account_id": account_id, "csrf": csrf, "expires_at": expiry}

    def session(self, sid):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE token_hash=? AND expires_at>?", (digest(sid), int(time.time()))).fetchone()
            return {k: row[k] for k in ("role", "account_id", "csrf", "expires_at")} if row else None

    def logout(self, sid):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(sid),))

    def account(self, account_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if row is None:
                raise ValidationError("账号不存在。")
            result = {k: row[k] for k in ("id", "label", "revision", "active_revision", "enabled", "updated_at")}
            result.update(pending=public_config(json.loads(row["pending"])) if row["pending"] else None,
                          active=public_config(json.loads(row["active"])) if row["active"] else None,
                          run_status=json.loads(row["run_status"]) if row["run_status"] else None)
            result["invites"] = [dict(i) for i in db.execute("SELECT id,expires_at,revoked FROM invites WHERE account_id=?", (account_id,))]
            return result

    def accounts(self):
        with self.connect() as db:
            ids = [r["id"] for r in db.execute("SELECT id FROM accounts ORDER BY updated_at DESC")]
        return [self.account(i) for i in ids]

    def save(self, account_id, payload, expected_revision):
        if type(expected_revision) is not int:
            raise ValidationError("缺少配置版本，请刷新页面后重试。")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if row is None:
                raise ValidationError("账号不存在。")
            if row["revision"] != expected_revision:
                raise ConflictError("配置已在其他窗口更新；请刷新后重新填写。")
            previous = json.loads(row["pending"] or row["active"]) if (row["pending"] or row["active"]) else None
            config = validate_config(payload, previous)
            revision = row["revision"] + 1
            db.execute("UPDATE accounts SET pending=?,revision=?,updated_at=? WHERE id=?",
                       (json.dumps(config, ensure_ascii=False), revision, int(time.time()), account_id))
        return self.account(account_id)

    def approve(self, account_id, revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if row is None or not row["pending"] or type(revision) is not int or row["revision"] != revision:
                raise ConflictError("待审核版本已变化；请刷新并重新核对。")
            unique_id = json.loads(row["pending"])["unique_id"].lower()
            for other in db.execute("SELECT active FROM accounts WHERE id<>? AND enabled=1", (account_id,)):
                if json.loads(other["active"])["unique_id"].lower() == unique_id:
                    raise ConflictError("这个抖音号已在其他启用账号中配置，请先停用重复账号。")
            db.execute("UPDATE accounts SET active=pending,active_revision=revision,pending=NULL,enabled=1,updated_at=? WHERE id=?", (int(time.time()), account_id))
        return self.account(account_id)

    def disable(self, account_id):
        with self.connect() as db:
            if db.execute("UPDATE accounts SET enabled=0,updated_at=? WHERE id=?", (int(time.time()), account_id)).rowcount != 1:
                raise ValidationError("账号不存在。")

    def enable(self, account_id, active_revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if row is None or not row["active"] or type(active_revision) is not int or row["active_revision"] != active_revision:
                raise ConflictError("已批准版本已变化；请刷新并核对后重新启用。")
            unique_id = json.loads(row["active"])["unique_id"].lower()
            for other in db.execute("SELECT active FROM accounts WHERE id<>? AND enabled=1", (account_id,)):
                if json.loads(other["active"])["unique_id"].lower() == unique_id:
                    raise ConflictError("这个抖音号已在其他启用账号中配置，请先停用重复账号。")
            db.execute("UPDATE accounts SET enabled=1,updated_at=? WHERE id=?", (int(time.time()), account_id))
        return self.account(account_id)

    def active_accounts(self):
        with self.connect() as db:
            return [{"id": row["id"], "revision": row["active_revision"], "config": json.loads(row["active"])}
                    for row in db.execute("SELECT id,active_revision,active FROM accounts WHERE enabled=1 AND active IS NOT NULL")]

    def update_run_status(self, account_id, summary):
        # Narrow, structured allowlist: never persist arbitrary logs or runner config.
        result = {}
        for key in ("status", "updated_at", "started_at"):
            if isinstance(summary.get(key), str):
                result[key] = summary[key][:80]
        for key in ("submitted", "failed", "unknown", "not_attempted", "total", "exit_code"):
            if type(summary.get(key)) is int:
                result[key] = summary[key]
        with self.connect() as db:
            db.execute("UPDATE accounts SET run_status=? WHERE id=?", (json.dumps(result), account_id))
