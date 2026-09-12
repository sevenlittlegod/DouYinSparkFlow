"""Set a PushPlus token through hidden terminal input or stdin, never argv."""
import argparse
import getpass
import os
from pathlib import Path
import sys
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.dotenv_writer import write_config


def configure(path, token):
    path = Path(path)
    if not path.is_file():
        raise ValueError('Existing configuration is required.')
    if not 16 <= len(token) <= 256 or not token.isascii() or not token.isalnum():
        raise ValueError('Token format is invalid; copy the PushPlus user/message token.')
    values = dict(dotenv_values(path, interpolate=False))
    values.update(NOTIFY_PROVIDER='pushplus', PUSHPLUS_TOKEN=token)
    write_config(path, values)
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stdin', action='store_true')
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()
    token = sys.stdin.readline().strip() if args.stdin else getpass.getpass('PushPlus Token (hidden): ')
    try:
        values = configure('/config/.env', token)
    except Exception:
        print('Configuration failed. Check the token and configuration permissions; no secret was printed.', file=sys.stderr)
        return 1
    print('PushPlus token saved privately. Douyin tasks and schedule were not started.', flush=True)
    if args.test:
        from utils.notify import notify
        os.environ['NOTIFY_PROVIDER'] = values['NOTIFY_PROVIDER']
        os.environ['PUSHPLUS_TOKEN'] = values['PUSHPLUS_TOKEN']
        result = notify({'test': True})
        print('PushPlus test request status: ' + result['status'], flush=True)
        if result['status'] != 'accepted':
            print('Reason: ' + result.get('reason', 'unknown'), flush=True)
            return 2
        print('Please confirm actual receipt in WeChat. No Douyin message was sent.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
