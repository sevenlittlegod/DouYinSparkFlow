from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from utils.task_report import TaskReport, prepare_resume


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'last-run.json'
        self.accounts = [{'unique_id': 'alice', 'username': 'Alice', 'cookies': ['PRIVATE_COOKIE'],
                          'targets': ['sent', 'uncertain', 'editor', 'pending', 'input_failed']}]
        self.report = TaskReport(self.path)
        self.report.initialize_accounts(self.accounts)
        self.report.data['started_at'] = '2026-09-14T01:00:00+00:00'
        self.report.update_target('alice', 'sent', 'submitted_unverified')
        self.report.update_target('alice', 'uncertain', 'unknown', 'submission_uncertain')
        self.report.update_target('alice', 'editor', 'failed', 'editor_unavailable')
        self.report.update_target('alice', 'input_failed', 'failed', 'message_input_failed')
        self.report.finish('failed', 'task_failed')
        self.now = datetime(2026, 9, 14, 2, tzinfo=timezone.utc)

    def test_resume_filters_uncertain_and_submitted_and_preserves_all_results(self):
        before, pending = prepare_resume(self.path, self.accounts, 'Asia/Shanghai', self.now)
        self.assertEqual(pending[0]['targets'], ['editor', 'pending'])
        report = TaskReport(self.path, previous=before)
        report.update_target('alice', 'editor', 'submitted_unverified')
        report.update_target('alice', 'pending', 'submitted_unverified')
        report.finish('failed', 'task_failed')
        rows = {r['target']: r for r in report.data['accounts'][0]['targets']}
        self.assertEqual(rows['sent']['status'], 'submitted_unverified')
        self.assertEqual(rows['uncertain']['status'], 'unknown')
        self.assertEqual(rows['input_failed']['reason'], 'message_input_failed')
        self.assertNotIn('PRIVATE_COOKIE', self.path.read_text())
        archive = list(self.path.parent.glob('before-resume-*.json'))
        self.assertEqual(len(archive), 1)
        self.assertEqual(json.loads(archive[0].read_text()), before)

    def test_old_day_and_changed_configuration_fail_without_overwriting_report(self):
        before = self.path.read_bytes()
        for accounts, now in [
            (self.accounts, datetime(2026, 9, 15, 2, tzinfo=timezone.utc)),
            ([{**self.accounts[0], 'targets': ['sent']}], self.now),
        ]:
            with self.assertRaises(ValueError):
                prepare_resume(self.path, accounts, 'Asia/Shanghai', now)
            self.assertEqual(self.path.read_bytes(), before)
