"""Import a retired single-account cron configuration without sending anything.

Operator must stop the legacy container and workers before apply, then recreate
the account worker because its legacy .env is a bind-mounted file. Run as the
portal data owner, or restore that ownership before restarting services.
"""
import argparse
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from account_worker.runner import Ledger, account_values, lock, summarize
from portal.store import Store, validate_config
from utils.dotenv_writer import write_config


def source_config(values, audit):
    tasks = json.loads(values.get('TASKS') or 'null')
    if not isinstance(tasks, list) or len(tasks) != 1:
        raise ValueError('Migration requires exactly one legacy account')
    task = tasks[0]
    targets = task['targets']
    if len(targets) != len(set(targets)):
        raise ValueError('Duplicate legacy targets require manual review')
    if values.get('TZ', 'Asia/Shanghai') != 'Asia/Shanghai' or int(values.get('CRON_SECOND', '0')) != 0:
        raise ValueError('Portal migration supports Shanghai time with zero seconds')
    labels = {}
    for account in audit.get('accounts', []):
        if account.get('unique_id') != task['unique_id']:
            continue
        for row in account.get('targets', []):
            people = row.get('identities') or []
            if row.get('status') == 'matched' and len(people) == 1:
                labels[row['target']] = people[0].get('remark_name') or people[0].get('nickname') or row['target']
    payload = dict(username=task['username'], unique_id=task['unique_id'],
                   friends=[dict(nickname=labels.get(t, t), douyin_id=t) for t in targets],
                   cookies=json.loads(values['COOKIES_' + task['unique_id'].upper()]),
                   message=values['MESSAGE_TEMPLATE'].replace(r'\n', '\n'),
                   hour=int(values.get('CRON_HOUR', '9')), minute=int(values.get('CRON_MINUTE', '0')),
                   pushplus_token=values.get('PUSHPLUS_TOKEN', '') if values.get('NOTIFY_PROVIDER') == 'pushplus' else '')
    return validate_config(payload)


def mapped_report(report, config, account_id, day):
    source_day = datetime.fromisoformat(report['started_at']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    if source_day != day or report.get('status') == 'running':
        raise ValueError('Migration needs a completed report from today; do not guess prior sends')
    accounts = report.get('accounts', [])
    if len(accounts) != 1 or accounts[0]['unique_id'] != config['unique_id']:
        raise ValueError('Report account does not match legacy configuration')
    rows = accounts[0]['targets']
    expected = {f['douyin_id'] for f in config['friends']}
    if len(rows) != len(expected) or {r['target'] for r in rows} != expected:
        raise ValueError('Report targets do not match; refusing unsafe daily history migration')
    if any(r['status'] not in {'submitted_unverified', 'unknown', 'failed', 'missing', 'not_attempted'} for r in rows):
        raise ValueError('Unrecognized target status')
    result = deepcopy(report)
    result['accounts'][0]['unique_id'] = 'portal_' + account_id
    names = {f['douyin_id']: f['nickname'] for f in config['friends']}
    for row in result['accounts'][0]['targets']:
        row['label'] = names[row['target']]
    result['migration'] = {'source': 'retired_legacy_cron', 'at': datetime.now(timezone.utc).isoformat(),
                           'prior_outcomes_preserved': True}
    return result


def migrate(root, apply=False, legacy_stopped=False, now=None):
    root = Path(root).resolve()
    marker = root / 'portal-data/legacy-import.json'
    if marker.exists():
        saved = json.loads(marker.read_text(encoding='utf-8'))
        if saved['state'] == 'complete':
            return {'account_id': saved['account_id'], 'already_imported': True}
        raise ValueError('An incomplete migration exists; inspect its private backup before proceeding')
    config_path = root / 'config/.env'
    values = dotenv_values(config_path, interpolate=False)
    audit_path = root / 'logs/target-audit.json'
    audit = json.loads(audit_path.read_text(encoding='utf-8')) if audit_path.exists() else {}
    config = source_config(values, audit)
    account_id = secrets.token_hex(16)
    day = (now or datetime.now(ZoneInfo('Asia/Shanghai'))).date().isoformat()
    source_report = root / 'logs/last-run.json'
    report = mapped_report(json.loads(source_report.read_text(encoding='utf-8')), config, account_id, day)
    summary = {'unique_id': config['unique_id'], 'targets': len(config['friends']),
               'hour': config['hour'], 'minute': config['minute'], 'day': day,
               'submitted': sum(r['status'] == 'submitted_unverified' for r in report['accounts'][0]['targets']),
               'not_attempted': sum(r['status'] == 'not_attempted' for r in report['accounts'][0]['targets'])}
    if not apply:
        return summary
    if not legacy_stopped:
        raise ValueError('Stop the legacy sender and workers before applying migration')
    with lock(root / 'logs/task.lock') as acquired:
        if not acquired:
            raise ValueError('A browser task is running; migration has not started')
        store = Store(root / 'portal-data')
        with store.connect() as db:
            for row in db.execute('SELECT pending,active FROM accounts'):
                for raw in row:
                    if raw and json.loads(raw)['unique_id'].lower() == config['unique_id'].lower():
                        raise ValueError('Legacy account already exists in portal; no duplicate imported')
        backup = root / 'private-migrations' / ('legacy-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
        backup.mkdir(parents=True, mode=0o700)
        backup.parent.chmod(0o700)
        shutil.copy2(config_path, backup / 'legacy.env')
        shutil.copy2(source_report, backup / 'last-run.json')
        if audit_path.exists(): shutil.copy2(audit_path, backup / 'target-audit.json')
        with closing(sqlite3.connect(store.path)) as src, closing(sqlite3.connect(backup / 'portal.sqlite3')) as dst:
            src.backup(dst)
        ledger_path = root / 'account-data/runs.sqlite3'
        if ledger_path.exists():
            with closing(sqlite3.connect(ledger_path)) as src, closing(sqlite3.connect(backup / 'runs.sqlite3')) as dst:
                src.backup(dst)
        for path in backup.iterdir(): path.chmod(0o600)
        state = {'state':'prepared', 'account_id':account_id, 'backup':str(backup.relative_to(root)), 'day':day}
        marker.write_text(json.dumps(state, indent=2), encoding='utf-8'); marker.chmod(0o600)
        with store.connect() as db:
            db.execute('INSERT INTO accounts(id,label,updated_at) VALUES(?,?,?)',
                       (account_id, config['username'], int(datetime.now().timestamp())))
        store.save(account_id, config, 0)
        # Existing settings were authorized; keep them as a disabled baseline.
        # A fresh login is still saved to pending and requires explicit approval.
        store.approve(account_id, 1)
        store.disable(account_id)
        account = {'id':account_id, 'revision':1, 'config':config}
        ledger = Ledger(root / 'account-data')
        folder = ledger.root / account_id / day
        folder.mkdir(parents=True, mode=0o700); folder.parent.chmod(0o700)
        write_config(folder / '.env', account_values(account))
        report_path = folder / 'last-run.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8'); report_path.chmod(0o600)
        if not ledger.claim(account, day): raise ValueError('Unexpected existing daily claim')
        ledger.finish(account_id, day, report['status'])
        store.update_run_status(account_id, summarize(report_path, report.get('exit_code', 1)))
        # Empty tasks also prevent a mistakenly recreated old container from sending.
        write_config(config_path, {'TASKS':'[]', 'TZ':'Asia/Shanghai', 'NOTIFY_PROVIDER':'off',
                                  'CRON_HOUR':str(config['hour']), 'CRON_MINUTE':str(config['minute']), 'CRON_SECOND':'0'})
        state['state'] = 'complete'
        marker.write_text(json.dumps(state, indent=2), encoding='utf-8'); marker.chmod(0o600)
        return {**summary, 'account_id':account_id, 'enabled':False, 'history_preserved':True}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Safely import a stopped legacy account; default is read-only preview')
    parser.add_argument('--root', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--legacy-stopped', action='store_true')
    args = parser.parse_args()
    print(json.dumps(migrate(args.root, args.apply, args.legacy_stopped), ensure_ascii=False))


if __name__ == '__main__':
    main()
