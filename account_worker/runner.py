"""One approved snapshot per account/day; no implicit retry after interruption.

Each browser task has a fresh subprocess, dotenv, report and notification token.
The legacy cron and this worker share flock, and cron has a protected time window.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time
from zoneinfo import ZoneInfo
from dotenv import dotenv_values

from portal.store import Store
from utils.dotenv_writer import write_config
from utils.task_report import TaskReport, prepare_resume

ZONE = ZoneInfo('Asia/Shanghai')
TIMEOUT = 25 * 60
APP = Path(__file__).resolve().parents[1]
LOCK_FDS = set()


def local_now():
    return datetime.now(ZONE)


def due(config, now):
    return (now.hour, now.minute) >= (config['hour'], config['minute'])


def legacy_window(now, hour=9, minute=0):
    """Reserve 30 minutes before and 10 after legacy cron's scheduled time."""
    delta = (now.hour * 60 + now.minute - hour * 60 - minute) % 1440
    return delta < 10 or delta >= 1410


@contextmanager
def lock(path):
    import fcntl  # Linux only; core scheduling/config tests also run on Windows.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            LOCK_FDS.add(stream.fileno())
            yield True
        finally:
            LOCK_FDS.discard(stream.fileno())
            fcntl.flock(stream, fcntl.LOCK_UN)


def account_values(account):
    """Use a safe internal key: actual Douyin IDs may contain dots or hyphens."""
    if not re.fullmatch(r'[a-f0-9]{32}', account['id']):
        raise ValueError('Invalid account ID')
    config = account['config']
    key = 'portal_' + account['id']
    return {
        'TASKS': json.dumps([{'unique_id': key, 'username': config['username'],
                              'targets': [f['douyin_id'] for f in config['friends']]}], ensure_ascii=False),
        'COOKIES_' + key.upper(): json.dumps(config['cookies'], ensure_ascii=False),
        'MESSAGE_TEMPLATE': config['message'].replace('\r\n', '\n').replace('\n', r'\n'),
        'CRON_HOUR': str(config['hour']), 'CRON_MINUTE': str(config['minute']),
        'CRON_SECOND': '0', 'TZ': 'Asia/Shanghai',
        'NOTIFY_PROVIDER': 'pushplus' if config['pushplus_token'] else 'off',
        'PUSHPLUS_TOKEN': config['pushplus_token'],
        'BROWSER_TIMEOUT': '120000', 'FRIEND_LIST_WAIT_TIME': '2000',
        'TASK_RETRY_TIMES': '3', 'LOG_LEVEL': 'INFO',
    }


class Ledger:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'runs.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS runs (account TEXT, day TEXT, revision INTEGER, status TEXT, PRIMARY KEY(account, day))')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, account, day):
        with self.connect() as db:
            return db.execute('INSERT OR IGNORE INTO runs VALUES (?,?,?,?)',
                              (account['id'], day, account['revision'], 'started')).rowcount == 1

    def lookup(self, account_id, day):
        with self.connect() as db:
            return db.execute('SELECT revision,status FROM runs WHERE account=? AND day=?', (account_id, day)).fetchone()

    def finish(self, account_id, day, status):
        with self.connect() as db:
            db.execute('UPDATE runs SET status=? WHERE account=? AND day=?', (status, account_id, day))


def summarize(path, exit_code):
    summary = {'status': 'worker_failed', 'exit_code': exit_code,
               'updated_at': datetime.now(timezone.utc).isoformat()}
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        rows = [r for a in data['accounts'] for r in a['targets']]
        counts = Counter(r['status'] for r in rows)
        summary.update(status=data['status'], started_at=data['started_at'],
                       submitted=counts['submitted_unverified'],
                       failed=counts['failed'] + counts['missing'],
                       unknown=counts['unknown'], not_attempted=counts['not_attempted'], total=len(rows))
        if exit_code in (124, 130) or data['status'] == 'running':
            summary['status'] = 'interrupted'
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return summary


def execute(config_path, logs, resume=False, action='run', timeout=TIMEOUT):
    # Never inherit another account's cookie, token, message or proxy settings.
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG') if key in os.environ}
    env.update(CONFIG_ENV_PATH=str(config_path), TASK_LOG_DIR=str(logs),
               PLAYWRIGHT_BROWSERS_PATH='/ms-playwright', PYTHONUNBUFFERED='1')
    command = [sys.executable, str(APP / 'docker/runtime.py'), action]
    if resume:
        command.append('--resume')
    with (logs / 'runtime.log').open('a', encoding='utf-8') as output:
        os.chmod(logs / 'runtime.log', 0o600)
        process = subprocess.Popen(command, cwd=APP, env=env, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True,
                                   pass_fds=tuple(LOCK_FDS))
        try:
            return process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return 130 if isinstance(error, KeyboardInterrupt) else 124


def run_account(store, ledger, account, day, resume=False):
    folder = ledger.root / account['id'] / day
    if resume:
        previous = ledger.lookup(account['id'], day)
        if previous is None or previous[0] != account['revision'] or previous[1] == 'submitted_unverified':
            raise ValueError('Cannot resume: no unfinished run with this approved revision today')
        if not (folder / '.env').is_file() or not (folder / 'last-run.json').is_file():
            raise ValueError('Cannot resume: original configuration/report missing')
    elif not ledger.claim(account, day):
        return False
    return execute_claimed(store, ledger, account, day, folder / '.env', resume=resume, write_snapshot=not resume)


def execute_claimed(store, ledger, account, day, config_path, *, resume, write_snapshot):
    folder = config_path.parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Claim before launch and retain even on crash. A restart cannot blindly resend.
    store.update_run_status(account['id'], {'status': 'running', 'started_at': datetime.now(timezone.utc).isoformat()})
    try:
        if write_snapshot:
            write_config(config_path, account_values(account))
        result = execute(config_path, folder, resume=resume)
    except KeyboardInterrupt:
        result = 130
    except Exception:
        result = 1
    if result != 0 and config_path.is_file():
        # Runtime checks its persisted notification marker and skips any prior
        # attempt, including uncertain delivery. This path never opens Douyin.
        try:
            execute(config_path, folder, action='notify-interrupted', timeout=45)
        except Exception:
            pass
    summary = summarize(folder / 'last-run.json', result)
    ledger.finish(account['id'], day, summary['status'])
    store.update_run_status(account['id'], summary)
    print(f"[worker] account={account['id']} status={summary['status']} exit={result}", flush=True)
    if result == 130:
        raise KeyboardInterrupt
    return True


def prepare_manual_resume(folder, account, original_revision, now):
    """Validate the active daily report against its private input before touching either."""
    report = TaskReport.open_existing(folder / 'last-run.json')
    if report.data.get('status') != 'failed' or report.data.get('error') == 'task_interrupted':
        return 'invalid_report'
    baseline = folder / '.env'
    roster_revision = report.data.get('manual_roster_revision')
    if roster_revision is not None:
        if type(roster_revision) is not int or roster_revision < 1:
            return 'invalid_report'
        baseline = folder / f'.manual-{roster_revision}.env'
    original, = json.loads(dotenv_values(baseline, interpolate=False)['TASKS'])
    values = account_values(account)
    expected, = json.loads(values['TASKS'])
    if (original['unique_id'] != expected['unique_id'] or len(report.data['accounts']) != 1 or
            not isinstance(original['targets'], list) or not original['targets'] or
            len(set(original['targets'])) != len(original['targets'])):
        return 'invalid_report'
    previous, pending = prepare_resume(report.path, [original], ZONE.key, now)
    # The runtime archives this report by timestamp before it executes a resume.
    stamp = datetime.fromisoformat(previous['updated_at']).strftime('%Y%m%dT%H%M%S%f')
    if set(original['targets']) == set(expected['targets']):
        return None if pending else 'no_safe_targets'
    if (roster_revision or original_revision) == account['revision']:
        return 'invalid_report'

    rows = previous['accounts'][0]['targets']
    login_failures = {'login_or_page_unavailable', 'cookie_load_failed'}
    if not rows or any(r['status'] != 'not_attempted' or r.get('reason') not in login_failures for r in rows):
        return 'configuration_changed'

    # Every original target stopped before any chat was opened. The newly approved
    # roster may be used once. Publish its input before atomically switching reports.
    # A crash leaves either the old pair or the new pair available for reconciliation.
    write_config(folder / f".manual-{account['revision']}.env", values)
    archive = folder / f'before-manual-roster-{stamp}.json'
    try:
        os.link(report.path, archive)
    except FileExistsError:
        if not archive.samefile(report.path):
            return 'invalid_report'
    existing = {row['target']: row for row in rows}
    targets = []
    for friend in account['config']['friends']:
        target = friend['douyin_id']
        targets.append(existing.get(target, {
            'target': target, 'label': friend['nickname'], 'status': 'not_attempted', 'reason': 'not_started',
        }))
    report.data['accounts'] = [{**expected, 'targets': targets}]
    report.data['manual_roster_revision'] = account['revision']
    report.save()
    return None


def run_manual_request(store, ledger, request, protected_ids):
    now = local_now()
    day = now.date().isoformat()
    if request['day'] != day:
        return 'expired'
    account = next((a for a in store.active_accounts() if a['id'] == request['account_id']), None)
    if account is None or account['config']['unique_id'].lower() in protected_ids:
        return 'unavailable'
    if account['revision'] != request['revision']:
        return 'configuration_changed'

    previous = ledger.lookup(account['id'], day)
    if previous is None:
        run_account(store, ledger, account, day)
    else:
        revision, status = previous
        if status == 'submitted_unverified':
            return 'already_complete'
        if status != 'failed':
            return 'invalid_report'
        if (revision != account['revision'] and
                store.approved_identity(account['id'], revision) != account['config']['unique_id'].lower()):
            return 'configuration_changed'

        folder = ledger.root / account['id'] / day
        try:
            blocked = prepare_manual_resume(folder, account, revision, now)
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            return 'invalid_report'
        if blocked:
            return blocked
        # Keep the first approved revision's private input and the original day claim.
        # The sender rechecks the report immediately before opening a browser.
        config_path = folder / f".manual-{account['revision']}.env"
        execute_claimed(store, ledger, account, day, config_path, resume=True, write_snapshot=True)

    return 'completed' if ledger.lookup(account['id'], day)[1] == 'submitted_unverified' else 'partial'


def consume_manual_request(store, ledger, protected_ids):
    request = store.claim_manual_request()
    if request is None:
        return
    try:
        outcome = run_manual_request(store, ledger, request, protected_ids)
    except (Exception, KeyboardInterrupt):
        store.finish_manual_request(request['id'], 'interrupted')
        raise
    store.finish_manual_request(request['id'], outcome)


def tick(store, ledger, legacy_logs, legacy_hour=9, legacy_minute=0, only=None, resume=False, protected_ids=(), legacy_enabled=True):
    now = local_now()
    if legacy_enabled and legacy_window(now, legacy_hour, legacy_minute):
        if only:
            raise ValueError('Legacy task has priority now; retry after its reserved window')
        return
    with lock(Path(legacy_logs) / 'task.lock') as acquired:
        if not acquired:
            if only:
                raise ValueError('Another browser task is still running')
            return
        if not only:
            consume_manual_request(store, ledger, protected_ids)
        # Read approvals AFTER obtaining the execution lock, not from a stale queue.
        accounts = store.active_accounts()
        if only and not any(a['id'] == only for a in accounts):
            raise ValueError('Account is not enabled')
        for account in accounts:
            now = local_now()
            if legacy_enabled and legacy_window(now, legacy_hour, legacy_minute):
                break
            if only and account['id'] != only:
                continue
            if not only and not due(account['config'], now):
                continue
            # Recheck revocation/revision immediately before each new execution.
            fresh = {a['id']: a for a in store.active_accounts()}.get(account['id'])
            if fresh is None:
                continue
            if not only and not due(fresh['config'], now):
                continue
            if fresh['config']['unique_id'].lower() in protected_ids:
                store.update_run_status(account['id'], {'status': 'configuration_conflict',
                    'updated_at': datetime.now(timezone.utc).isoformat()})
                continue
            run_account(store, ledger, fresh, now.date().isoformat(), resume=resume)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default='/portal-data')
    parser.add_argument('--runtime-dir', default='/accounts')
    parser.add_argument('--legacy-logs', default='/legacy-logs')
    parser.add_argument('--legacy-config', default='/legacy.env')
    parser.add_argument('--legacy-hour', type=int, default=9)
    parser.add_argument('--legacy-minute', type=int, default=0)
    parser.add_argument('--once', action='store_true')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--run-account')
    group.add_argument('--resume-account')
    args = parser.parse_args()
    store, ledger = Store(args.data_dir), Ledger(args.runtime_dir)
    only = args.run_account or args.resume_account
    if only and not re.fullmatch(r'[a-f0-9]{32}', only):
        raise SystemExit('Invalid account ID')
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    print('[worker] Approved accounts only; daily claims and shared browser lock enabled.', flush=True)
    while True:
        try:
            with lock(ledger.root / 'worker.lock') as acquired:
                if acquired:
                    legacy = dotenv_values(args.legacy_config, interpolate=False)
                    protected = {a['unique_id'].lower() for a in json.loads(legacy['TASKS'])}
                    tick(store, ledger, args.legacy_logs, args.legacy_hour, args.legacy_minute,
                         only=only, resume=bool(args.resume_account), protected_ids=protected,
                         legacy_enabled=bool(protected))
        except KeyboardInterrupt:
            return
        except Exception:
            # No raw exception: configuration or subprocess errors can contain secrets.
            print('[worker] Cycle failed; inspect private per-account report. No automatic resend.', flush=True)
            if only or args.once:
                raise SystemExit(1)
        if args.once or only:
            return
        try:
            time.sleep(30)
        except KeyboardInterrupt:
            return
