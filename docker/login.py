"""Manually sign in on the server, then save cookies without sending messages."""

import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.dotenv_writer import set_config_value


CHAT_URL = "https://www.douyin.com/chat"
CHAT_LIST_SELECTOR = ".conversationConversationListwrapper"
LOGIN_TIMEOUT_SECONDS = 15 * 60


def cookie_key_for_config(config_path):
    """Require an existing, single-account task; never create or change TASKS."""
    config_path = Path(config_path)
    if not config_path.is_file():
        raise ValueError("缺少配置 .env；请先建立包含单账号 TASKS 的配置。")
    values = dotenv_values(config_path, interpolate=False)
    try:
        tasks = json.loads(values.get("TASKS") or "null")
    except (TypeError, json.JSONDecodeError):
        raise ValueError("TASKS 必须是有效的 JSON 数组。") from None
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        raise ValueError("手动登录辅助仅支持已有 TASKS 中恰好一个账号。")
    unique_id = tasks[0].get("unique_id")
    if not isinstance(unique_id, str) or not re.fullmatch(r"[A-Za-z0-9_]+", unique_id):
        raise ValueError("TASKS 的 unique_id 必须是非空字母、数字或下划线组成的字符串。")
    return f"COOKIES_{unique_id}".upper()


def douyin_cookies(cookies):
    return [
        cookie for cookie in cookies
        if isinstance(cookie, dict)
        and isinstance(cookie.get("domain"), str)
        and (cookie["domain"].lstrip(".") == "douyin.com"
             or cookie["domain"].lstrip(".").endswith(".douyin.com"))
    ]


def has_session_cookie(cookies):
    return any(
        cookie.get("name") == "sessionid" and bool(cookie.get("value"))
        for cookie in douyin_cookies(cookies)
    )


def save_login(config_path, cookies, marker_path):
    """Save only Douyin cookies; marker contains status/time, never credentials."""
    config_path = Path(config_path)
    cookie_key = cookie_key_for_config(config_path)
    cookies = douyin_cookies(cookies)
    if not has_session_cookie(cookies):
        raise ValueError("尚未检测到抖音登录会话，未保存 Cookie。")
    os.chmod(config_path, 0o600)
    set_config_value(
        config_path, cookie_key,
        json.dumps(cookies, ensure_ascii=False, separators=(",", ":")),
    )
    os.chmod(config_path, 0o600)
    marker_path = Path(marker_path)
    with open(marker_path, "w", encoding="utf-8") as marker:
        os.chmod(marker_path, 0o600)
        json.dump(
            {"status": "ready", "saved_at": datetime.now(timezone.utc).isoformat()},
            marker,
        )
        marker.write("\n")


def main():
    config_dir = Path(os.getenv("LOGIN_CONFIG_DIR", "/config"))
    config_path = config_dir / ".env"
    marker_path = config_dir / "login-ready.json"
    cookie_key_for_config(config_path)
    marker_path.unlink(missing_ok=True)

    # Import lazily so configuration/save tests do not need a browser runtime.
    from playwright.sync_api import sync_playwright

    deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
    saved = False
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=120000)
            print("请通过 SSH 隧道打开登录窗口，在抖音页面手动扫码登录。此窗口不发送消息。", flush=True)
            while time.monotonic() < deadline:
                if page.is_closed():
                    break
                if not saved:
                    cookies = context.cookies()
                    if has_session_cookie(cookies) and page.locator(CHAT_LIST_SELECTOR).first.is_visible():
                        save_login(config_path, cookies, marker_path)
                        saved = True
                        print("已检测到登录和会话列表，Cookie 已保存。可关闭登录辅助；请单独确认后再运行续火花。", flush=True)
                page.wait_for_timeout(1000)
        finally:
            browser.close()
    if not saved:
        raise ValueError("登录窗口已结束，尚未检测到完整登录状态；未保存 Cookie，请重新登录。")
    print("登录辅助已结束。", flush=True)


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"[login] {error}", file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        # Browser error details may contain page/login data; print no raw exception.
        print("[login] 登录辅助失败，请检查配置权限、浏览器状态和服务器网络后重试。", file=sys.stderr)
        raise SystemExit(1)
