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
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Claim before launch and retain even on crash. A restart cannot blindly resend.
    store.update_run_status(account['id'], {'status': 'running', 'started_at': datetime.now(timezone.utc).isoformat()})
    try:
        if not resume:
            write_config(folder / '.env', account_values(account))
        result = execute(folder / '.env', folder, resume=resume)
    except KeyboardInterrupt:
        result = 130
    except Exception:
        result = 1
    if result != 0 and (folder / '.env').is_file():
        # Runtime checks its persisted notification marker and skips any prior
        # attempt, including uncertain delivery. This path never opens Douyin.
        try:
            execute(folder / '.env', folder, action='notify-interrupted', timeout=45)
        except Exception:
            pass
    summary = summarize(folder / 'last-run.json', result)
    ledger.finish(account['id'], day, summary['status'])
    store.update_run_status(account['id'], summary)
    print(f"[worker] account={account['id']} status={summary['status']} exit={result}", flush=True)
    if result == 130:
        raise KeyboardInterrupt
    return True


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
