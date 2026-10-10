from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dotenv import dotenv_values
from account_worker import runner
from portal.store import Store


class AccountWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'portal')
        self.ledger = runner.Ledger(self.root / 'runs')
        invite = self.store.create_invite('测试账号')
        self.aid = invite['account_id']
        self.payload = dict(username='账号甲', unique_id='owner.a', friends=[dict(nickname='朋友', douyin_id='friend.1')],
                            message="消息\n第二行 ' ${SOMETHING}", cookies='private_session_a',
                            hour=9, minute=0, pushplus_token='a' * 32)
        self.store.save(self.aid, self.payload, 0)
        self.store.approve(self.aid, 1)
        self.account = self.store.active_accounts()[0]

    def fake_execute(self, config, logs, resume=False):
        values = dotenv_values(config, interpolate=False)
        task = json.loads(values['TASKS'])[0]
        data = dict(status='submitted_unverified', started_at='2026-10-10T01:10:00+00:00', accounts=[{
            **task, 'targets': [dict(target=t, status='submitted_unverified') for t in task['targets']]}])
        (logs / 'last-run.json').write_text(json.dumps(data), encoding='utf-8')
        return 0

    def test_daily_claim_survives_restart_and_revision_change(self):
        with patch.object(runner, 'execute', side_effect=self.fake_execute) as execute:
            self.assertTrue(runner.run_account(self.store, self.ledger, self.account, '2026-10-10'))
            self.store.save(self.aid, {**self.payload, 'message': '已改内容'}, 1)
            self.store.approve(self.aid, 2)
            restarted = runner.Ledger(self.root / 'runs')
            self.assertFalse(runner.run_account(self.store, restarted, self.store.active_accounts()[0], '2026-10-10'))
            self.assertEqual(execute.call_count, 1)
            self.assertTrue(runner.run_account(self.store, restarted, self.store.active_accounts()[0], '2026-10-11'))

    def test_claim_before_launch_prevents_retry_after_crash(self):
        with patch.object(runner, 'execute', side_effect=RuntimeError('crash')) as execute:
            runner.run_account(self.store, self.ledger, self.account, '2026-10-10')
            self.assertFalse(runner.run_account(self.store, self.ledger, self.account, '2026-10-10'))
            self.assertEqual(execute.call_count, 2)
            self.assertEqual(execute.call_args.kwargs['action'], 'notify-interrupted')
            self.assertEqual(self.store.account(self.aid)['run_status']['status'], 'worker_failed')

    def test_config_isolation_roundtrip_and_no_secret_in_summary(self):
        with patch.object(runner, 'execute', side_effect=self.fake_execute):
            runner.run_account(self.store, self.ledger, self.account, '2026-10-10')
        values = dotenv_values(self.root / 'runs' / self.aid / '2026-10-10/.env', interpolate=False)
        self.assertEqual(values['MESSAGE_TEMPLATE'], self.payload['message'].replace('\n', r'\n'))
        self.assertEqual(values['PUSHPLUS_TOKEN'], 'a' * 32)
        self.assertIn('private_session_a', values['COOKIES_PORTAL_' + self.aid.upper()])
        summary = self.store.account(self.aid)['run_status']
        self.assertEqual(summary['submitted'], 1)
        self.assertNotIn('private_session_a', json.dumps(summary))
        second = self.store.create_invite('乙')['account_id']
        self.store.save(second, {**self.payload, 'unique_id': 'owner.b', 'cookies': 'private_session_b', 'pushplus_token': 'b' * 32}, 0)
        self.store.approve(second, 1)
        config_b = runner.account_values(next(a for a in self.store.active_accounts() if a['id'] == second))
        self.assertNotIn('private_session_a', json.dumps(config_b))
        self.assertEqual(config_b['PUSHPLUS_TOKEN'], 'b' * 32)

    def test_schedule_and_legacy_priority_wrap_midnight(self):
        def now(h, m):
            return datetime(2026, 10, 10, h, m, tzinfo=runner.ZONE)
        self.assertFalse(runner.due(self.account['config'], now(8, 59)))
        self.assertTrue(runner.due(self.account['config'], now(9, 0)))
        for h, m in ((8, 30), (8, 59), (9, 0), (9, 9)):
            self.assertTrue(runner.legacy_window(now(h, m)))
        self.assertFalse(runner.legacy_window(now(9, 10)))
        self.assertFalse(runner.legacy_window(now(8, 29)))
        self.assertTrue(runner.legacy_window(now(23, 55), 0, 10))

    def test_resume_requires_original_revision_and_unfinished_run(self):
        with patch.object(runner, 'execute') as execute:
            with self.assertRaises(ValueError):
                runner.run_account(self.store, self.ledger, self.account, '2026-10-10', resume=True)
            execute.assert_not_called()
        with patch.object(runner, 'execute', side_effect=self.fake_execute):
            runner.run_account(self.store, self.ledger, self.account, '2026-10-10')
        with self.assertRaises(ValueError):
            runner.run_account(self.store, self.ledger, self.account, '2026-10-10', resume=True)
        self.ledger.finish(self.aid, '2026-10-10', 'failed')
        with patch.object(runner, 'execute', side_effect=self.fake_execute) as execute:
            runner.run_account(self.store, self.ledger, self.account, '2026-10-10', resume=True)
            self.assertTrue(execute.call_args.kwargs['resume'])

    @unittest.skipIf(os.name == 'nt', 'Linux flock shared by host and containers')
    def test_lock_contends_between_independent_descriptors(self):
        with runner.lock(self.root / 'task.lock') as first:
            self.assertTrue(first)
            with runner.lock(self.root / 'task.lock') as second:
                self.assertFalse(second)
        with runner.lock(self.root / 'task.lock') as third:
            self.assertTrue(third)

    def test_subprocess_does_not_inherit_other_account_environment(self):
        logs = self.root / 'isolated'
        logs.mkdir()
        with patch.dict(os.environ, {'PUSHPLUS_TOKEN': 'LEAK', 'COOKIES_OLD': 'LEAK', 'MESSAGE_TEMPLATE': 'LEAK'}), \
             patch.object(runner.subprocess, 'Popen') as popen:
            popen.return_value.wait.return_value = 0
            runner.execute(logs / '.env', logs)
        env = popen.call_args.kwargs['env']
        self.assertNotIn('PUSHPLUS_TOKEN', env)
        self.assertNotIn('COOKIES_OLD', env)
        self.assertNotIn('MESSAGE_TEMPLATE', env)
        self.assertEqual(env['TASK_LOG_DIR'], str(logs))


if __name__ == '__main__':
    unittest.main()
