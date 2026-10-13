"""Read-only Douyin login/conversation worker. No chat editor or send operation."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

from account_worker.runner import lock, LOCK_FDS, local_now
from portal.binding import Bindings
from portal.store import Store, ID
from utils import norm
from portal.verification import VerificationBridge, qr_exposed

LIST = '.conversationConversationListwrapper'
TITLES = '.conversationConversationItemtitle'
# The QR selector is verified against the public login page; never screenshot
# the full browser, chat panel, verification form, or another account's content.
QR = 'img[aria-label="二维码"],img.RhjdbXj8'


class BindingFailure(Exception):
    pass


class IdentityReader:
    def __init__(self):
        self.identity = None
        self.people = {}

    def response(self, response):
        url = urlsplit(response.url)
        if url.hostname != 'www.douyin.com':
            return
        try:
            if url.path == '/aweme/v1/web/user/profile/self/':
                user = response.json().get('user') or {}
                own_id = str(user.get('unique_id') or user.get('short_id') or '')
                if ID.fullmatch(own_id) and own_id != '0':
                    self.identity = {'unique_id': own_id, 'nickname': str(user.get('nickname') or '')[:80]}
            elif url.path.rstrip('/') == '/aweme/v1/web/im/user/info':
                for user in response.json().get('data') or []:
                    if not isinstance(user, dict):
                        continue
                    identity = str(user.get('sec_uid') or user.get('unique_id') or user.get('short_id') or '')
                    if identity and len(self.people) < 1000:
                        self.people[identity] = {key: str(user.get(key) or '') for key in
                                                ('unique_id', 'short_id', 'nickname', 'remark_name')}
        except Exception:
            pass  # No raw page response or credentials in logs.

    def friends(self, titles):
        own_id = self.identity['unique_id'] if self.identity else None
        people = []
        for user in self.people.values():
            uid = user['unique_id'] or user['short_id']
            title = norm(user['remark_name'] or user['nickname'])
            if uid != own_id and ID.fullmatch(uid) and uid != '0' and title in titles:
                people.append({'douyin_id': uid, 'nickname': title[:80]})
        counts = Counter(p['nickname'] for p in people)
        unique = {}
        for person in people:
            person['ambiguous'] = counts[person['nickname']] > 1
            unique[person['douyin_id']] = person
        return sorted(unique.values(), key=lambda p: (p['nickname'], p['douyin_id']))[:500]


def collect_friends(page, reader, alive, timeout=80):
    until = time.monotonic() + timeout
    titles, previous, stable = set(), None, 0
    scroll = page.locator(LIST).first
    while time.monotonic() < until and alive():
        titles.update(norm(t) for t in page.locator(TITLES).all_text_contents())
        geometry = scroll.evaluate('(el) => [el.scrollTop,el.scrollHeight,el.clientHeight]')
        state = (len(reader.people), len(titles), *geometry)
        bottom = geometry[0] + geometry[2] >= geometry[1] - 2
        stable = stable + 1 if state == previous and bottom else 0
        if stable >= 5:
            return reader.friends(titles), False
        previous = state
        scroll.evaluate('(el) => {el.scrollTop += Math.max(400,el.clientHeight * .8)}')
        page.wait_for_timeout(1000)
    return reader.friends(titles), True


def run_browser(bindings, job):
    from playwright.sync_api import sync_playwright
    alive = lambda: bindings.alive(job['id'])
    started = time.monotonic()
    reader = IdentityReader()
    qr_hash, qr_time = None, 0
    verification = VerificationBridge()
    verification_time = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        try:
            context = browser.new_context(viewport={'width': 1280, 'height': 800})
            context.set_default_timeout(5000)
            if job['mode'] == 'saved':
                context.add_cookies(json.loads(job['cookies']))
            page = context.new_page()
            page.on('response', reader.response)
            page.goto('https://www.douyin.com/chat', wait_until='domcontentloaded', timeout=90000)
            while alive() and time.monotonic() - started < 720:
                if urlsplit(page.url).hostname != 'www.douyin.com':
                    raise BindingFailure('page_unavailable')
                challenge = verification.inspect(page)
                if challenge:
                    if time.monotonic() - verification_time > 3:
                        bindings.update(job['id'], 'verification', qr=verification.screenshot(page), verification=challenge)
                        verification_time = time.monotonic()
                    command = bindings.take_command(job['id'])
                    if command:
                        try:
                            verification.perform(page, command)
                        except Exception:
                            pass  # Human can review the fresh view; never retry an OTP action.
                        finally:
                            command.clear()
                        verification_time = 0
                    page.wait_for_timeout(1000)
                    continue
                if page.locator(LIST).is_visible() and reader.identity:
                    if job['expected_id'] and job['expected_id'].lower() != reader.identity['unique_id'].lower():
                        raise BindingFailure('identity_mismatch')
                    bindings.update(job['id'], 'reading')
                    friends, partial = collect_friends(page, reader, alive)
                    if alive():
                        cookies = [c for c in context.cookies() if c['domain'].lstrip('.') == 'douyin.com'
                                   or c['domain'].lstrip('.').endswith('.douyin.com')]
                        bindings.update(job['id'], 'ready', identity=reader.identity,
                                        friends=friends, cookies=cookies, partial=partial)
                    return
                # Only the genuine QR image is exported. No browser-control API.
                qr = page.locator(QR)
                if qr.count() == 1 and qr_exposed(qr):
                    if job['mode'] == 'saved':
                        raise BindingFailure('login_required')
                    if time.monotonic() - qr_time > 4:
                        png = qr.screenshot(timeout=5000)
                        new_hash = hashlib.sha256(png).digest()
                        if new_hash != qr_hash:
                            bindings.update(job['id'], 'waiting_scan', qr=png)
                            qr_hash = new_hash
                        qr_time = time.monotonic()
                if job['mode'] == 'saved' and time.monotonic() - started > 100:
                    raise BindingFailure('login_required')
                page.wait_for_timeout(1000)
            if alive():
                raise BindingFailure('identity_unavailable' if page.locator(LIST).is_visible() else 'expired')
        finally:
            browser.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default='/portal-data')
    parser.add_argument('--legacy-logs', default='/legacy-logs')
    parser.add_argument('--job')
    args = parser.parse_args()
    store = Store(args.data_dir)
    bindings = Bindings(store)
    if args.job:
        with store.connect() as db:
            row = db.execute("SELECT * FROM binding_jobs WHERE id=? AND state='starting'", (args.job,)).fetchone()
        if not row or not bindings.alive(args.job):
            return
        try:
            run_browser(bindings, dict(row))
        except BindingFailure as error:
            bindings.update(args.job, 'failed', error=str(error))
        except Exception:
            bindings.update(args.job, 'failed', error='page_unavailable')
        return

    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    with lock(Path(args.data_dir) / 'binding-worker.lock') as acquired:
        if not acquired:
            raise SystemExit('Binding worker already running')
        bindings.sweep(restart=True)
        print('[binding] Read-only login worker ready; no message sending.', flush=True)
        while True:
            try:
                bindings.sweep()
                # Bound interactive login time and leave room before the morning
                # sending schedule. The shared lock prevents competing browsers.
                minute = local_now().hour * 60 + local_now().minute
                if not 8 * 60 + 20 <= minute < 9 * 60 + 10:
                    with lock(Path(args.legacy_logs) / 'task.lock') as browser_lock:
                        job = bindings.claim() if browser_lock else None
                        if job:
                            cmd = ['xvfb-run', '-a', '-s', '-screen 0 1280x800x24 -nolisten tcp', sys.executable,
                                   '-m', 'portal.binding_worker', '--data-dir', args.data_dir, '--job', job['id']]
                            child = subprocess.Popen(cmd, cwd=Path(__file__).resolve().parents[1],
                                                     start_new_session=True, pass_fds=tuple(LOCK_FDS),
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            try:
                                child.wait(timeout=810)
                            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                                os.killpg(child.pid, signal.SIGTERM)
                                try:
                                    child.wait(timeout=8)
                                except subprocess.TimeoutExpired:
                                    os.killpg(child.pid, signal.SIGKILL); child.wait()
                                bindings.update(job['id'], 'failed', error='interrupted')
                                if isinstance(error, KeyboardInterrupt):
                                    raise
                            if bindings.alive(job['id']):
                                bindings.update(job['id'], 'failed', error='browser_unavailable')
                time.sleep(3)
            except KeyboardInterrupt:
                return
            except Exception:
                print('[binding] Cycle unavailable; private credentials not logged.', flush=True)
                time.sleep(5)


if __name__ == '__main__':
    main()
