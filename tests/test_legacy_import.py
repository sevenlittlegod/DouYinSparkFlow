from contextlib import nullcontext
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from account_worker.runner import Ledger
from portal.legacy_import import migrate, mapped_report, source_config
from portal.store import Store
from utils.dotenv_writer import write_config


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root/'config').mkdir(); (self.root/'logs').mkdir()
        self.now = datetime(2026,10,13,16,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.values = {'TASKS':json.dumps([{'unique_id':'original','username':'原账号','targets':['a','b']}]),
                       'COOKIES_ORIGINAL':json.dumps([{'name':'sessionid','value':'original-private-session','domain':'.douyin.com','path':'/'}]),
                       'MESSAGE_TEMPLATE':r'测试续火花\n[API]', 'CRON_HOUR':'9','CRON_MINUTE':'0','CRON_SECOND':'0',
                       'TZ':'Asia/Shanghai','NOTIFY_PROVIDER':'pushplus','PUSHPLUS_TOKEN':'original_token_123456789'}
        write_config(self.root/'config/.env',self.values)
        self.report={'status':'failed','started_at':'2026-10-13T01:00:00+00:00','updated_at':'2026-10-13T01:05:00+00:00',
                     'accounts':[{'unique_id':'original','username':'原账号','targets':[
                         {'target':'a','status':'submitted_unverified','reason':None},
                         {'target':'b','status':'not_attempted','reason':'login_or_page_unavailable'}]}], 'exit_code':1,
                     'notification':{'status':'accepted','provider':'pushplus'}}
        (self.root/'logs/last-run.json').write_text(json.dumps(self.report),encoding='utf-8')
        self.store=Store(self.root/'portal-data')
        self.other=self.store.create_invite('现有账号')['account_id']

    def run_import(self, apply=False):
        with patch('portal.legacy_import.lock', return_value=nullcontext(True)):
            return migrate(self.root,apply,legacy_stopped=apply,now=self.now)

    def test_preview_is_read_only_and_reports_partial_history(self):
        before=(self.root/'config/.env').read_bytes()
        result=self.run_import()
        self.assertEqual((1,1),(result['submitted'],result['not_attempted']))
        self.assertEqual(before,(self.root/'config/.env').read_bytes())
        self.assertEqual(1,len(self.store.accounts()))
        self.assertFalse((self.root/'portal-data/legacy-import.json').exists())

    def test_apply_preserves_secrets_schedule_daily_outcomes_and_is_idempotent(self):
        result=self.run_import(True);aid=result['account_id']
        migrated=self.store.account(aid)
        self.assertFalse(migrated['enabled']);self.assertEqual(1,migrated['active_revision'])
        with self.store.connect() as db:
            config=json.loads(db.execute('SELECT active FROM accounts WHERE id=?',(aid,)).fetchone()[0])
        self.assertEqual(self.values['PUSHPLUS_TOKEN'],config['pushplus_token'])
        self.assertEqual('original-private-session',config['cookies'][0]['value'])
        self.assertEqual('测试续火花\n[API]',config['message'])
        self.assertEqual((9,0),(config['hour'],config['minute']))
        path=self.root/'account-data'/aid/'2026-10-13/last-run.json';report=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(['submitted_unverified','not_attempted'],[r['status'] for r in report['accounts'][0]['targets']])
        self.assertEqual('portal_'+aid,report['accounts'][0]['unique_id'])
        self.assertEqual((1,'failed'),Ledger(self.root/'account-data').lookup(aid,'2026-10-13'))
        self.assertEqual('[]',dotenv_values(self.root/'config/.env')['TASKS'])
        self.assertEqual('off',dotenv_values(self.root/'config/.env')['NOTIFY_PROVIDER'])
        self.assertTrue(self.run_import(True)['already_imported'])
        self.assertEqual(2,len(self.store.accounts()))
        self.assertIsNone(self.store.account(self.other)['active'])

    def test_missing_targets_or_wrong_day_abort_before_writes(self):
        self.report['accounts'][0]['targets'].pop()
        (self.root/'logs/last-run.json').write_text(json.dumps(self.report))
        with self.assertRaises(ValueError): self.run_import(True)
        self.assertEqual(1,len(self.store.accounts()))
        self.assertEqual(self.values['TASKS'],dotenv_values(self.root/'config/.env')['TASKS'])
        self.assertFalse((self.root/'portal-data/legacy-import.json').exists())

    def test_same_name_friend_labels_require_exact_audit_identity(self):
        audit={'accounts':[{'unique_id':'original','targets':[
            {'target':'a','status':'matched','identities':[{'nickname':'小明','remark_name':''}]},
            {'target':'b','status':'ambiguous','identities':[{'nickname':'误匹配'},{'nickname':'另一人'}]}]}]}
        config=source_config(self.values,audit)
        self.assertEqual(['小明','b'],[f['nickname'] for f in config['friends']])
        report={**self.report,'started_at':'2026-10-12T01:00:00+00:00'}
        with self.assertRaises(ValueError): mapped_report(report,config,'a'*32,'2026-10-13')
