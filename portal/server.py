"""Run behind an HTTPS reverse proxy, or access through a local SSH tunnel."""
import argparse
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from portal.store import AccessError, ConflictError, Store, ValidationError
from portal.auth import AdminAuth, Throttled
from portal.binding import Bindings


MAX_BODY = 192 * 1024
STATIC = Path(__file__).with_name("static")


class PortalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, admin_token, public_origin=None, base_path=""):
        self.store, self.admin_token = store, admin_token
        self.admin_auth = AdminAuth(store)
        self.bindings = Bindings(store)
        self.base_path = "/" + base_path.strip("/") if base_path.strip("/") else ""
        if self.base_path and not re.fullmatch(r"(/[a-zA-Z0-9_-]+)+", self.base_path):
            raise ValueError("PORTAL_BASE_PATH 无效。")
        super().__init__(address, Handler)
        self.public_origin = (public_origin or f"http://127.0.0.1:{self.server_port}").rstrip("/")
        parsed = urlsplit(self.public_origin)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
            raise ValueError("PORTAL_PUBLIC_ORIGIN 只能包含协议和主机。")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")):
            raise ValueError("公开地址必须使用 HTTPS；HTTP 仅可用于本机 SSH 隧道。")
        self.allowed_host = parsed.netloc.lower()
        self.secure_cookie = parsed.scheme == "https"
        self.auth_attempts = defaultdict(deque)
        self.auth_lock = threading.Lock()

    def login_allowed(self, address):
        now = time.monotonic()
        with self.auth_lock:
            attempts = self.auth_attempts[address]
            while attempts and attempts[0] < now - 60:
                attempts.popleft()
            if len(attempts) >= 20:
                return False
            attempts.append(now)
            return True


class Handler(BaseHTTPRequestHandler):
    server_version = "SparkPortal"
    sys_version = ""

    def log_message(self, format, *args):
        # Paths can accidentally contain tokens. Never log requests or bodies.
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def finish_response(self, status, body, content_type="application/json; charset=utf-8", cookie=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self.server.secure_cookie:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def route(self):
        if self.headers.get("Host", "").lower() != self.server.allowed_host:
            raise AccessError("访问地址与服务配置不一致。")
        path = urlsplit(self.path).path
        base = self.server.base_path
        if base and (path == base or path.startswith(base + "/")):
            path = path[len(base):] or "/"
        return path

    def sid(self):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            return cookies["spark_session"].value if "spark_session" in cookies else ""
        except Exception:
            return ""

    def session(self, required=True, admin=False):
        session = self.server.store.session(self.sid())
        if required and (not session or (admin and session["role"] != "admin")):
            raise AccessError("请先登录；该操作需要有效权限。")
        return session

    def session_cookie(self, sid, expiry=None):
        value = f"spark_session={sid}; Path={self.server.base_path}/; HttpOnly; SameSite=Strict; Max-Age={max(0, (expiry or 0) - int(time.time()))}"
        return value + ("; Secure" if self.server.secure_cookie else "")

    def body(self):
        if self.headers.get("Origin") != self.server.public_origin:
            raise AccessError("请求来源不正确，请从邀请页面提交。")
        if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
            raise ValidationError("只接受 JSON 请求。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValidationError("请求长度不正确。") from None
        if not 0 < length <= MAX_BODY:
            raise ValidationError("配置内容过大或为空。")
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise ValidationError("请求 JSON 不正确。") from None
        if not isinstance(data, dict):
            raise ValidationError("请求必须是对象。")
        return data

    def check_csrf(self, session):
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
            raise AccessError("页面凭据已失效，请刷新。")

    def do_GET(self):
        try:
            path = self.route()
            if path in ("/", "/admin", "/admin/"):
                html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__BASE_PATH__", self.server.base_path)
                return self.finish_response(200, html.encode(), "text/html; charset=utf-8")
            if path in ("/app.js", "/style.css"):
                return self.finish_response(200, (STATIC / path[1:]).read_bytes(), "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8")
            if path == "/api/session":
                return self.finish_response(200, {"session": self.session(False)})
            if path == '/api/auth/status':
                return self.finish_response(200, {'configured': self.server.admin_auth.configured()})
            if path == '/api/binding':
                return self.finish_response(200, {'binding': self.server.bindings.current(self.sid())})
            match = re.fullmatch(r'/api/binding/([0-9a-f]{32})(/qr)?', path)
            if match:
                result = self.server.bindings.get(self.sid(), match[1], image=bool(match[2]))
                return self.finish_response(200, result if match[2] else {'binding': result},
                                            'image/png' if match[2] else 'application/json; charset=utf-8')
            if path == "/api/account":
                session = self.session()
                if session["role"] != "owner":
                    raise AccessError("请通过该账号的邀请链接填写。")
                return self.finish_response(200, {"account": self.server.store.account(session["account_id"])})
            if path == "/api/admin/accounts":
                self.session(admin=True)
                return self.finish_response(200, {"accounts": self.server.store.accounts()})
            return self.finish_response(404, {"error": "页面不存在。"})
        except AccessError as exc:
            self.finish_response(403, {"error": str(exc)})
        except ValidationError as exc:
            self.finish_response(400, {"error": str(exc)})
        except Exception:
            self.finish_response(500, {"error": "服务暂时不可用，请稍后重试。"})

    def do_POST(self):
        try:
            path, data = self.route(), self.body()
            if path == '/api/auth/setup':
                return self.finish_response(200, self.server.admin_auth.begin(
                    data.get('token'), self.server.admin_token, 'SparkFlow ' + self.server.allowed_host))
            if path == '/api/auth/confirm':
                codes = self.server.admin_auth.finish(data.get('setup_token'), data.get('password'), data.get('code'))
                sid, session = self.server.store.new_session('admin')
                self.server.store.logout(self.sid())
                return self.finish_response(200, {'session': session, 'recovery_codes': codes},
                                            cookie=self.session_cookie(sid, session['expires_at']))
            if path in ("/api/login", "/api/exchange"):
                if not self.server.login_allowed(self.client_address[0]):
                    return self.finish_response(429, {"error": "尝试次数过多，请一分钟后重试。"})
                token = data.get("token", "")
                if not isinstance(token, str) or len(token) > 200:
                    raise AccessError("凭据无效。")
                if path == "/api/login":
                    self.server.admin_auth.verify(data.get('password'), data.get('code'), data.get('recovery') is True)
                    sid, session = self.server.store.new_session("admin")
                else:
                    sid, session = self.server.store.new_session("owner", token)
                self.server.store.logout(self.sid())
                return self.finish_response(200, {"session": session}, cookie=self.session_cookie(sid, session["expires_at"]))
            session = self.session(admin=path.startswith("/api/admin/"))
            self.check_csrf(session)
            if path == "/api/logout":
                self.server.store.logout(self.sid())
                return self.finish_response(200, {"ok": True}, cookie=self.session_cookie(""))
            if path == "/api/account":
                if session["role"] != "owner":
                    raise AccessError("仅可保存本人账号。")
                return self.finish_response(200, {"account": self.server.store.save(session["account_id"], data.get("config"), data.get("revision"), data.get('binding_id'), self.sid())})
            if path == '/api/binding/start':
                return self.finish_response(200, {'binding': self.server.bindings.start(self.sid(), data.get('mode'), data.get('revision'))})
            match = re.fullmatch(r'/api/binding/([0-9a-f]{32})/cancel', path)
            if match:
                self.server.bindings.cancel(self.sid(), match[1])
                return self.finish_response(200, {'ok': True})
            if path == "/api/admin/invites":
                invite = self.server.store.create_invite(data.get("label"), data.get("days", 7), data.get("account_id"))
                invite["url"] = self.server.public_origin + self.server.base_path + "/#invite=" + invite.pop("token")
                return self.finish_response(200, {"invite": invite})
            match = re.fullmatch(r"/api/admin/accounts/([0-9a-f]{32})/(approve|disable|enable)", path)
            if match:
                account_id, action = match.groups()
                if action == "approve":
                    account = self.server.store.approve(account_id, data.get("revision"))
                    return self.finish_response(200, {"account": account})
                if action == "disable":
                    self.server.store.disable(account_id)
                    return self.finish_response(200, {"ok": True})
                if action == "enable":
                    account = self.server.store.enable(account_id, data.get("active_revision"))
                    return self.finish_response(200, {"account": account})
            match = re.fullmatch(r"/api/admin/invites/([0-9a-f]{32})/revoke", path)
            if match:
                self.server.store.revoke_invite(match[1])
                return self.finish_response(200, {"ok": True})
            return self.finish_response(404, {"error": "操作不存在。"})
        except Throttled as exc:
            self.finish_response(429, {"error": str(exc)})
        except AccessError as exc:
            self.finish_response(403, {"error": str(exc)})
        except ConflictError as exc:
            self.finish_response(409, {"error": str(exc)})
        except ValidationError as exc:
            self.finish_response(400, {"error": str(exc)})
        except Exception:
            self.finish_response(500, {"error": "操作未完成，请刷新确认后重试。"})


def load_admin_token(path):
    path = Path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(secrets.token_urlsafe(36) + "\n")
    os.chmod(path, 0o600)
    token = path.read_text(encoding="ascii").strip()
    if len(token) < 32:
        raise ValueError("管理员凭据至少需要 32 个随机字符。")
    return token


def main():
    parser = argparse.ArgumentParser(description="邀请制抖音续火花配置页（仅监听本机）")
    parser.add_argument("--data-dir", default=os.environ.get("PORTAL_DATA_DIR", "portal-data"))
    parser.add_argument("--host", choices=("127.0.0.1",), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORTAL_PORT", "18766")))
    args = parser.parse_args()
    os.umask(0o077)
    store = Store(args.data_dir)
    token_path = os.environ.get("PORTAL_ADMIN_TOKEN_FILE", str(store.data_dir / "admin-token"))
    server = PortalServer((args.host, args.port), store, load_admin_token(token_path),
                          os.environ.get("PORTAL_PUBLIC_ORIGIN"), os.environ.get("PORTAL_BASE_PATH", ""))
    print(f"配置页监听 {args.host}:{args.port}；管理员凭据文件：{token_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
