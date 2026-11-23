"""Browser-only portal checks with synthetic accounts and every request intercepted."""
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


STATIC = Path(__file__).resolve().parents[1] / 'portal' / 'static'
ACCOUNT = 'a' * 32
DAY = '2026-11-23'


@unittest.skipUnless(os.getenv('RUN_BROWSER_TESTS') == '1', 'Requires the container browser runtime')
class ManualRunUiTests(unittest.TestCase):
    def setUp(self):
        engine = sync_playwright().start()
        self.addCleanup(engine.stop)
        browser = engine.chromium.launch(headless=True, channel=os.getenv('PLAYWRIGHT_CHANNEL'))
        self.addCleanup(browser.close)
        self.page = browser.new_page(viewport={'width': 360, 'height': 800})
        self.role = 'owner'
        self.posts = []
        self.account = {'id': ACCOUNT, 'label': '演示账号', 'revision': 2, 'active_revision': 2,
                        'enabled': True, 'invites': [], 'active': {'username': '演示账号', 'friends': [],
                        'cookies_configured': True, 'message': '模拟消息', 'hour': 9, 'minute': 0}}
        self.set_state(True, '可手动执行今日任务。')
        self.page.route('**/*', self.route)

    def set_state(self, available, reason, status=None):
        self.account['manual_run'] = {'available': available, 'reason': reason, 'day': DAY,
                                      'request': {'id': 9, 'status': status} if status else None}

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path.removeprefix('/spark')
        if path in ('/', '/admin'):
            return route.fulfill(content_type='text/html; charset=utf-8',
                                 body=(STATIC / 'index.html').read_text(encoding='utf-8').replace('__BASE_PATH__', '/spark'))
        if path in ('/app.js', '/style.css'):
            return route.fulfill(path=STATIC / path[1:])
        if path == '/api/session':
            return route.fulfill(json={'session': {'role': self.role, 'csrf': 'synthetic-test'}})
        if path.endswith('/binding'):
            return route.fulfill(json={'binding': None})
        if path == '/api/admin/accounts':
            return route.fulfill(json={'accounts': [self.account]})
        if path in ('/api/account', f'/api/admin/accounts/{ACCOUNT}'):
            return route.fulfill(json={'account': self.account})
        if request.method == 'POST' and path.endswith('/manual-run'):
            self.posts.append((path, request.post_data_json, request.headers.get('x-csrf-token')))
            self.set_state(False, '请求已排队，等待执行。', 'queued')
            return route.fulfill(json={'manual_run': self.account['manual_run']})
        return route.abort()

    def test_owner_refresh_after_approval_and_double_click_enqueues_once(self):
        self.set_state(False, '请先由管理员审核并启用最新配置，再手动续火花。')
        self.page.goto('https://portal.test/spark/')
        run = self.page.get_by_role('button', name='手动续火花')
        expect(run).to_be_disabled()
        self.set_state(True, '可手动执行今日任务。')
        self.page.get_by_role('button', name='刷新进度').click()
        expect(run).to_be_enabled()
        self.assertFalse(self.posts)
        run.evaluate('button => { button.click(); button.click(); }')
        expect(self.page.locator('.manual-status')).to_contain_text('已排队')
        expect(run).to_be_disabled()
        self.account['run_status'] = {'status': 'submitted_unverified', 'submitted': 2, 'total': 2}
        self.set_state(False, '今天的任务已经完成。', 'completed')
        expect(self.page.locator('.manual-status')).to_contain_text('已经完成', timeout=8000)
        expect(self.page.locator('.run-status')).to_contain_text('已提交 2 / 2')
        self.assertEqual(self.posts, [('/api/manual-run', {'day': DAY, 'revision': 2,
                                                          'previous_request_id': None}, 'synthetic-test')])
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))

    def test_admin_uses_selected_account_and_owner_edit_is_preserved(self):
        self.role = 'admin'
        self.account['run_status'] = {'status': 'failed', 'submitted': 0, 'total': 3}
        self.page.set_viewport_size({'width': 1100, 'height': 800})
        self.page.goto('https://portal.test/spark/admin')
        self.page.get_by_role('button', name='手动续火花').click()
        expect(self.page.locator('.manual-status')).to_contain_text('已排队')
        self.assertEqual(self.posts[0][0], f'/api/admin/accounts/{ACCOUNT}/manual-run')
        self.set_state(True, '可手动执行今日任务。', 'partial')
        self.account['run_status'] = {'status': 'partial', 'submitted': 2, 'total': 3, 'unknown': 1}
        self.page.get_by_role('button', name='刷新进度').click()
        expect(self.page.locator('.run-status')).to_contain_text('上次手动续火花仍有未完成的好友')
        expect(self.page.locator('.run-status')).to_contain_text('已提交 2 / 3 · 结果未知 1')
        self.page.goto('https://portal.test/spark/admin?account=' + ACCOUNT)
        self.page.locator('#message').fill('新草稿')
        self.page.get_by_role('button', name='刷新进度').click()
        expect(self.page.locator('#message')).to_have_value('新草稿')
        expect(self.page.get_by_role('button', name='手动续火花')).to_be_disabled()
        expect(self.page.locator('.manual-status')).to_contain_text('先提交')
        self.assertEqual(len(self.posts), 1)
