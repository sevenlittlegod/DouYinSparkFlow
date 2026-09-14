import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from utils.task_report import TaskReport

spec = importlib.util.spec_from_file_location('report_runtime', Path(__file__).resolve().parents[1] / 'docker/runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
config_spec = importlib.util.spec_from_file_location('configure_notify', Path(__file__).resolve().parents[1] / 'docker/configure-notify.py')
configure_notify = importlib.util.module_from_spec(config_spec)
config_spec.loader.exec_module(configure_notify)


class TaskReportingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.account = {'unique_id': 'alice', 'username': 'Alice', 'targets': ['Bob', 'Carol'], 'cookies': [{'value': 'PRIVATE_COOKIE'}]}
        for replacement in (
            patch.dict(os.environ, {'CONFIG_ENV_PATH': str(self.root / '.env')}, clear=True),
            patch.object(runtime, 'APP_DIR', self.root),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_outcomes_and_labels_persist_without_cookie(self):
        (self.root / 'target-audit.json').write_text(json.dumps({'accounts': [{'unique_id': 'alice', 'targets': [
            {'target': 'Bob', 'status': 'matched', 'identities': [{'remark_name': '朋友甲', 'nickname': 'Bob'}]}
        ]}]}), encoding='utf-8')
        report = TaskReport(self.root / 'last-run.json')
        report.initialize_accounts([self.account])
        report.update_target('alice', 'Bob', 'submitted_unverified')
        report.update_target('alice', 'Carol', 'unknown', 'submission_uncertain')
        report.finish('failed', 'task_failed')
        data = json.loads(report.path.read_text(encoding='utf-8'))
        self.assertEqual(data['accounts'][0]['targets'][0]['label'], '朋友甲')
        self.assertEqual([r['status'] for r in data['accounts'][0]['targets']], ['submitted_unverified', 'unknown'])
        self.assertNotIn('PRIVATE_COOKIE', report.path.read_text(encoding='utf-8'))

    def test_partial_failure_notifies_once_and_preserves_outcomes(self):
        def task(report):
            report.update_target('alice', 'Bob', 'submitted_unverified')
            report.update_target('alice', 'Carol', 'failed', 'editor_unavailable')
            raise RuntimeError('PRIVATE_EXCEPTION')
        with patch.object(runtime, 'load_configuration', return_value=({}, [self.account])):
            with patch('core.tasks.runTasks', side_effect=task) as execute, patch('utils.notify.notify', return_value={'status': 'accepted'}) as notify:
                with self.assertRaisesRegex(RuntimeError, 'PRIVATE_EXCEPTION'):
                    runtime.run()
        execute.assert_called_once()
        notify.assert_called_once()
        saved = (self.root / 'logs/last-run.json').read_text(encoding='utf-8')
        self.assertNotIn('PRIVATE_EXCEPTION', saved)
        self.assertNotIn('PRIVATE_COOKIE', saved)
        data = json.loads(saved)
        self.assertEqual(data['status'], 'failed')
        self.assertEqual(data['accounts'][0]['targets'][0]['status'], 'submitted_unverified')
        self.assertEqual(data['accounts'][0]['targets'][1]['reason'], 'editor_unavailable')

    def test_notification_failure_does_not_repeat_successful_task(self):
        def task(report):
            for target in ('Bob', 'Carol'):
                report.update_target('alice', target, 'submitted_unverified')
        with patch.object(runtime, 'load_configuration', return_value=({}, [self.account])):
            with patch('core.tasks.runTasks', side_effect=task) as execute, patch('utils.notify.notify', side_effect=RuntimeError('PRIVATE_TOKEN')) as notify:
                runtime.run()
        execute.assert_called_once()
        notify.assert_called_once()
        saved = (self.root / 'logs/last-run.json').read_text(encoding='utf-8')
        self.assertNotIn('PRIVATE_TOKEN', saved)
        data = json.loads(saved)
        self.assertEqual(data['status'], 'submitted_unverified')
        self.assertEqual(data['notification']['status'], 'failed')

    def test_configuration_failure_can_notify_without_starting_browser(self):
        (self.root / '.env').write_text('NOTIFY_PROVIDER=pushplus\nPUSHPLUS_TOKEN=PRIVATE_TOKEN\n', encoding='utf-8')
        with patch.object(runtime, 'load_configuration', side_effect=ValueError('Bad TASKS')):
            with patch('core.tasks.runTasks') as execute, patch('utils.notify.notify', return_value={'status': 'accepted'}) as notify:
                with self.assertRaises(ValueError):
                    runtime.run()
        execute.assert_not_called()
        notify.assert_called_once()
        self.assertEqual(os.environ['PUSHPLUS_TOKEN'], 'PRIVATE_TOKEN')
        self.assertEqual(notify.call_args.args[0]['error'], 'configuration_invalid')

    def test_token_configuration_preserves_existing_tasks_and_cookie(self):
        from dotenv import dotenv_values
        path = self.root / '.env'
        path.write_text('TASKS=original\nCOOKIES_ALICE=PRIVATE_COOKIE\n', encoding='utf-8')
        configure_notify.configure(path, 'a' * 32)
        values = dotenv_values(path, interpolate=False)
        self.assertEqual(values['TASKS'], 'original')
        self.assertEqual(values['COOKIES_ALICE'], 'PRIVATE_COOKIE')
        self.assertEqual(values['NOTIFY_PROVIDER'], 'pushplus')
        with self.assertRaises(ValueError):
            configure_notify.configure(path, 'bad\nvalue')
        self.assertEqual(dotenv_values(path)['PUSHPLUS_TOKEN'], 'a' * 32)

    def test_browser_startup_failure_notifies_specific_cause_without_sending(self):
        from core.browser import BrowserStartupError
        with patch.object(runtime, 'load_configuration', return_value=({}, [self.account])):
            with patch('core.tasks.runTasks', side_effect=BrowserStartupError('Browser unavailable')) as execute:
                with patch('utils.notify.notify', return_value={'status': 'accepted'}) as notify:
                    with self.assertRaises(BrowserStartupError):
                        runtime.run()
        execute.assert_called_once()
        notify.assert_called_once()
        result = notify.call_args.args[0]
        self.assertEqual(result['error'], 'browser_error')
        self.assertTrue(all(row['status'] == 'not_attempted' and row['reason'] == 'browser_error'
                            for row in result['accounts'][0]['targets']))

    def test_resume_sends_only_pending_and_notifies_combined_results(self):
        report = TaskReport(self.root / 'logs/last-run.json')
        report.initialize_accounts([self.account])
        report.update_target('alice', 'Bob', 'submitted_unverified')
        report.update_target('alice', 'Carol', 'failed', 'editor_unavailable')
        report.finish('failed', 'task_failed')

        def task(report, accounts):
            self.assertEqual(accounts[0]['targets'], ['Carol'])
            report.update_target('alice', 'Carol', 'submitted_unverified')

        with patch.object(runtime, 'load_configuration', return_value=({}, [self.account])):
            with patch('core.tasks.runTasks', side_effect=task) as execute:
                with patch('utils.notify.notify', return_value={'status': 'accepted'}) as notify:
                    runtime.run(resume=True)
        execute.assert_called_once()
        notify.assert_called_once()
        result = notify.call_args.args[0]
        self.assertEqual(result['status'], 'submitted_unverified')
        self.assertEqual(result['resume_count'], 1)
        self.assertTrue(all(r['status'] == 'submitted_unverified' for r in result['accounts'][0]['targets']))

    def test_invalid_resume_leaves_report_intact_and_does_not_notify_or_send(self):
        report = TaskReport(self.root / 'logs/last-run.json')
        report.initialize_accounts([self.account])
        report.data['started_at'] = '2000-01-01T00:00:00+00:00'
        report.save()
        before = report.path.read_bytes()
        with patch.object(runtime, 'load_configuration', return_value=({}, [self.account])):
            with patch('core.tasks.runTasks') as execute, patch('utils.notify.notify') as notify:
                with self.assertRaises(ValueError):
                    runtime.run(resume=True)
        self.assertEqual(report.path.read_bytes(), before)
        execute.assert_not_called()
        notify.assert_not_called()
