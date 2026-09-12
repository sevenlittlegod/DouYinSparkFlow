"""Persist target outcomes without cookies, message bodies, or raw exceptions."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile


class TaskReport:
    def __init__(self, path, audit_path=None):
        self.path = Path(path)
        self.audit_path = Path(audit_path) if audit_path else self.path.parent / 'target-audit.json'
        self.data = {'status': 'running', 'started_at': self.now(), 'accounts': []}
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
                    row['reason'] = 'run_stopped'
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
