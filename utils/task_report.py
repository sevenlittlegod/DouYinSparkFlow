"""Persist target outcomes without cookies, message bodies, or raw exceptions."""
from datetime import datetime, timezone
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo


def prepare_resume(path, accounts, zone, now=None):
    """Only same-day, same-target, definitely pre-submission outcomes can retry."""
    previous = json.loads(Path(path).read_text(encoding='utf-8'))
    started = datetime.fromisoformat(previous['started_at'])
    now = now or datetime.now(timezone.utc)
    if started.tzinfo is None or started.astimezone(ZoneInfo(zone)).date() != now.astimezone(ZoneInfo(zone)).date():
        raise ValueError('只能补跑同一时区当天的任务，请先核对原运行记录。')
    rows = {}
    for account in previous['accounts']:
        for row in account['targets']:
            key = (account['unique_id'], row['target'])
            if key in rows:
                raise ValueError('原运行记录存在重复目标。')
            rows[key] = row
    expected = {(account['unique_id'], target) for account in accounts for target in account['targets']}
    if expected != set(rows):
        raise ValueError('当前账号或好友名单与原任务不同，不能直接断点补跑。')
    pending = []
    for account in accounts:
        targets = []
        for target in dict.fromkeys(account['targets']):
            row = rows[(account['unique_id'], target)]
            if row['status'] in {'not_attempted', 'missing'} or (
                row['status'] == 'failed' and row.get('reason') in {'editor_unavailable', 'conversation_unavailable'}
            ):
                targets.append(target)
            elif row['status'] not in {'submitted_unverified', 'unknown', 'failed'}:
                raise ValueError('原运行记录包含无法识别的状态。')
        if targets:
            pending.append({**account, 'targets': targets})
    return previous, pending


class TaskReport:
    def __init__(self, path, audit_path=None, previous=None):
        self.path = Path(path)
        self.audit_path = Path(audit_path) if audit_path else self.path.parent / 'target-audit.json'
        self.data = {'status': 'running', 'started_at': self.now(), 'accounts': []}
        if previous is not None:
            # Keep an immutable private snapshot before overwriting last-run.
            stamp = datetime.fromisoformat(previous['updated_at']).strftime('%Y%m%dT%H%M%S%fZ')
            archive = self.path.parent / f'before-resume-{stamp}.json'
            if not archive.exists():
                with archive.open('x', encoding='utf-8') as handle:
                    os.chmod(archive, 0o600)
                    json.dump(previous, handle, ensure_ascii=False, indent=2)
            self.data = deepcopy(previous)
            self.data.update(status='running', resumed_at=self.now(), resume_count=previous.get('resume_count', 0) + 1)
            for key in ('error', 'exit_code', 'notification'):
                self.data.pop(key, None)
        self.save()

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def initialize_accounts(self, accounts):
        labels = {}
        try:
            audit = json.loads(self.audit_path.read_text(encoding='utf-8'))
            for account in audit.get('accounts', []):
                for row in account.get('targets', []):
                    if row.get('status') == 'matched' and len(row.get('identities', [])) == 1:
                        identity = row['identities'][0]
                        labels[(account['unique_id'], row['target'])] = identity.get('remark_name') or identity.get('nickname') or row['target']
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Names are optional; configured IDs always remain available.
        self.data['accounts'] = [
            {'unique_id': account['unique_id'], 'username': account['username'], 'targets': [
                {'target': target, 'label': labels.get((account['unique_id'], target), target),
                 'status': 'not_attempted', 'reason': 'not_started'}
                for target in dict.fromkeys(account['targets'])
            ]} for account in accounts
        ]
        self.save()

    def update_target(self, unique_id, target, status, reason=None):
        allowed = {'submitted_unverified', 'failed', 'missing', 'not_attempted', 'unknown'}
        if status not in allowed:
            raise ValueError('Invalid target outcome')
        for account in self.data['accounts']:
            if account['unique_id'] == unique_id:
                for row in account['targets']:
                    if row['target'] == target:
                        row.update(status=status, reason=reason)
                        self.save()
                        return
        raise ValueError('Outcome target is not in this run')

    def finish(self, status, error=None):
        self.data['status'] = status
        self.data['exit_code'] = 0 if status == 'submitted_unverified' else 1
        if error:
            self.data['error'] = error
        for account in self.data['accounts']:
            for row in account['targets']:
                if row['status'] == 'not_attempted' and row['reason'] == 'not_started':
                    row['reason'] = error if error in {'browser_error', 'configuration_invalid'} else 'run_stopped'
        self.save()

    def save(self):
        self.data['updated_at'] = self.now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.last-run-', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
