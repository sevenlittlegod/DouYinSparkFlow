from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import nullcontext

from dotenv import dotenv_values
from account_worker import runner
from portal.store import Store
from utils.dotenv_writer import write_config
from utils.task_report import prepare_resume


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

    def test_retired_legacy_does_not_delay_nine_oclock_portal_tasks(self):
        now=datetime(2026,10,13,9,0,tzinfo=runner.ZONE)
        with patch.object(runner,'local_now',return_value=now), patch.object(runner,'lock',return_value=nullcontext(True)), patch.object(runner,'run_account') as run:
            runner.tick(self.store,self.ledger,self.root/'logs',legacy_enabled=True)
            run.assert_not_called()
            runner.tick(self.store,self.ledger,self.root/'logs',legacy_enabled=False)
            self.assertEqual(1,run.call_count)

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

    def manual_request(self, day='2026-10-10', protected=(), revision=None):
        request = {'id': 'test-request', 'account_id': self.aid, 'day': day,
                   'revision': self.store.active_accounts()[0]['revision'] if revision is None else revision}
        with patch.object(runner, 'local_now', return_value=datetime(2026, 10, 10, 8, tzinfo=runner.ZONE)):
            return runner.run_manual_request(self.store, self.ledger, request, protected)

    def failed_report(self, account, rows):
        day = '2026-10-10'
        self.assertTrue(self.ledger.claim(account, day))
        self.ledger.finish(self.aid, day, 'failed')
        folder = self.ledger.root / self.aid / day
        folder.mkdir(parents=True)
        write_config(folder / '.env', runner.account_values(account))
        report = {'status': 'failed', 'started_at': '2026-10-10T01:00:00+08:00',
                  'updated_at': '2026-10-10T01:01:00+08:00',
                  'accounts': [{'unique_id': 'portal_' + self.aid, 'targets': rows}]}
        (folder / 'last-run.json').write_text(json.dumps(report), encoding='utf-8')
        return folder

    def test_manual_before_schedule_claims_today_and_never_repeats_completed(self):
        with patch.object(runner, 'execute', side_effect=self.fake_execute) as execute:
            self.assertEqual('completed', self.manual_request())
            self.assertEqual('already_complete', self.manual_request())
            self.assertFalse(runner.run_account(self.store, self.ledger, self.account, '2026-10-10'))
            self.assertEqual(1, execute.call_count)

    def test_manual_after_approved_login_uses_new_cookie_and_only_safe_targets(self):
        friends = self.payload['friends'] + [
            {'nickname': '已提交', 'douyin_id': 'submitted'}, {'nickname': '待核实', 'douyin_id': 'uncertain'}]
        self.store.save(self.aid, {**self.payload, 'friends': friends}, 1)
        self.store.approve(self.aid, 2)
        original = self.store.active_accounts()[0]
        rows = [{'target': 'friend.1', 'status': 'not_attempted', 'reason': 'login_or_page_unavailable'},
                {'target': 'submitted', 'status': 'submitted_unverified'},
                {'target': 'uncertain', 'status': 'unknown', 'reason': 'submission_uncertain'}]
        folder = self.failed_report(original, rows)
        original_input = (folder / '.env').read_bytes()
        self.store.save(self.aid, {**self.payload, 'friends': friends, 'cookies': 'new_private_login'}, 2)
        self.store.approve(self.aid, 3)

        def resume(config, logs, resume=False):
            self.assertTrue(resume)
            values = dotenv_values(config, interpolate=False)
            self.assertIn('new_private_login', values['COOKIES_PORTAL_' + self.aid.upper()])
            report, pending = prepare_resume(logs / 'last-run.json', json.loads(values['TASKS']), values['TZ'],
                                              runner.local_now())
            self.assertEqual(['friend.1'], pending[0]['targets'])
            report['accounts'][0]['targets'][0]['status'] = 'submitted_unverified'
            (logs / 'last-run.json').write_text(json.dumps(report), encoding='utf-8')
            return 0

        with patch.object(runner, 'execute', side_effect=resume) as execute:
            self.assertEqual('partial', self.manual_request())
            self.assertEqual('no_safe_targets', self.manual_request())
            self.assertEqual(1, execute.call_count)
        self.assertEqual(original_input, (folder / '.env').read_bytes())
        self.assertEqual((2, 'failed'), self.ledger.lookup(self.aid, '2026-10-10'))

    def test_manual_rejects_expired_configuration_changes_and_protected_accounts(self):
        self.failed_report(self.account, [{'target': 'friend.1', 'status': 'not_attempted'}])
        with patch.object(runner, 'execute') as execute:
            self.assertEqual('expired', self.manual_request(day='2026-10-09'))
            self.assertEqual('unavailable', self.manual_request(protected={'owner.a'}))
            self.store.save(self.aid, {**self.payload, 'unique_id': 'owner.b'}, 1)
            self.store.approve(self.aid, 2)
            self.assertEqual('configuration_changed', self.manual_request(revision=1))
            self.assertEqual('configuration_changed', self.manual_request())
            self.store.save(self.aid, {**self.payload, 'friends': [{'nickname': '新好友','douyin_id': 'different'}]}, 2)
            self.store.approve(self.aid, 3)
            self.assertEqual('configuration_changed', self.manual_request())
            execute.assert_not_called()

    def test_manual_missing_or_mismatched_report_never_opens_browser(self):
        folder = self.failed_report(self.account, [{'target': 'wrong', 'status': 'not_attempted'}])
        with patch.object(runner, 'execute') as execute:
            self.assertEqual('invalid_report', self.manual_request())
            (folder / 'last-run.json').unlink()
            self.assertEqual('invalid_report', self.manual_request())
            execute.assert_not_called()

    def test_new_friend_after_login_failure_is_included_even_after_worker_restart(self):
        folder = self.failed_report(self.account, [
            {'target': 'friend.1', 'status': 'not_attempted', 'reason': 'login_or_page_unavailable'}])
        before = (folder / 'last-run.json').read_bytes()
        friends = self.payload['friends'] + [{'nickname': '新增好友', 'douyin_id': 'friend.2'}]
        self.store.save(self.aid, {**self.payload, 'friends': friends, 'cookies': 'refreshed_cookie'}, 1)
        self.store.approve(self.aid, 2)
        account = self.store.active_accounts()[0]
        now = datetime(2026, 10, 10, 8, tzinfo=runner.ZONE)
        # Reopen after a process crash between installing the new roster and dispatch.
        self.assertIsNone(runner.prepare_manual_resume(folder, account, 1, now))
        def resumed_sender(config, logs, resume=False):
            self.assertTrue(resume)
            values = dotenv_values(config, interpolate=False)
            self.assertIn('refreshed_cookie', values['COOKIES_PORTAL_' + self.aid.upper()])
            previous, pending = prepare_resume(logs / 'last-run.json', json.loads(values['TASKS']), values['TZ'], now)
            self.assertEqual(['friend.1', 'friend.2'], pending[0]['targets'])
            report = runner.TaskReport(logs / 'last-run.json', previous=previous)
            for target in pending[0]['targets']:
                report.update_target('portal_' + self.aid, target, 'submitted_unverified')
            report.finish('submitted_unverified')
            return 0
        with patch.object(runner, 'execute', side_effect=resumed_sender) as execute:
            self.assertEqual('completed', self.manual_request())
            self.assertEqual('already_complete', self.manual_request())
            execute.assert_called_once()
        archive, = folder.glob('before-manual-roster-*.json')
        self.assertEqual(before, archive.read_bytes())
        self.assertEqual((1, 'submitted_unverified'), self.ledger.lookup(self.aid, '2026-10-10'))

    def test_changed_roster_with_uncertain_submission_never_uses_fresh_plan(self):
        folder = self.failed_report(self.account, [
            {'target': 'friend.1', 'status': 'unknown', 'reason': 'submission_uncertain'}])
        before = (folder / 'last-run.json').read_bytes()
        friends = self.payload['friends'] + [{'nickname': '新增好友', 'douyin_id': 'friend.2'}]
        self.store.save(self.aid, {**self.payload, 'friends': friends}, 1)
        self.store.approve(self.aid, 2)
        with patch.object(runner, 'execute') as execute:
            self.assertEqual('configuration_changed', self.manual_request())
            execute.assert_not_called()
        self.assertEqual(before, (folder / 'last-run.json').read_bytes())

    def test_interrupted_run_with_unsettled_target_never_restarts_from_button(self):
        folder = self.failed_report(self.account, [{'target': 'friend.1', 'status': 'not_attempted'}])
        path = folder / 'last-run.json'
        report = json.loads(path.read_text(encoding='utf-8'))
        with patch.object(runner, 'execute') as execute:
            for ledger_state, report_state, error in (
                    ('interrupted', 'failed', 'task_failed'), ('started', 'failed', 'task_failed'),
                    ('failed', 'running', None), ('failed', 'failed', 'task_interrupted')):
                self.ledger.finish(self.aid, '2026-10-10', ledger_state)
                path.write_text(json.dumps({**report, 'status': report_state, 'error': error}), encoding='utf-8')
                self.assertEqual('invalid_report', self.manual_request())
            execute.assert_not_called()

    def test_manual_queue_consumed_under_lock_before_scheduled_run(self):
        request = {'id': 'job', 'account_id': self.aid, 'day':'2026-10-10', 'revision': 1}
        now = datetime(2026, 10, 10, 10, tzinfo=runner.ZONE)
        with patch.object(self.store, 'claim_manual_request', return_value=request) as claim, \
             patch.object(self.store, 'finish_manual_request') as finish, \
             patch.object(runner, 'execute', side_effect=self.fake_execute) as execute, \
             patch.object(runner, 'local_now', return_value=now):
            with patch.object(runner, 'lock', return_value=nullcontext(False)):
                runner.tick(self.store, self.ledger, self.root / 'logs', legacy_enabled=False)
                claim.assert_not_called()
            with patch.object(runner, 'lock', return_value=nullcontext(True)):
                runner.tick(self.store, self.ledger, self.root / 'logs', legacy_enabled=False)
            finish.assert_called_once_with('job', 'completed')
            execute.assert_called_once()

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
