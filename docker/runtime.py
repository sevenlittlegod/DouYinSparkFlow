"""Container configuration, validated scheduling, and one task invocation."""
import argparse
import os
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))


def schedule_settings(values):
    result = []
    for key, default, maximum in (("CRON_HOUR", "9", 23), ("CRON_MINUTE", "0", 59), ("CRON_SECOND", "0", 59)):
        value = str(values.get(key, default))
        if not re.fullmatch(r"[0-9]{1,2}", value) or not 0 <= int(value) <= maximum:
            raise ValueError(f"{key} must be an integer from 0 to {maximum}")
        result.append(int(value))
    zone = values.get("TZ", "Asia/Shanghai")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("TZ must be an installed IANA timezone such as Asia/Shanghai") from None
    return (*result, zone)


def load_configuration():
    path = Path(os.environ.get("CONFIG_ENV_PATH", "/app/.env"))
    if not path.is_file():
        raise ValueError("Configuration .env is missing; prepare it before starting the container")
    values = {key: value for key, value in dotenv_values(path, interpolate=False).items() if value is not None}
    schedule_settings(values)
    for key, value in values.items():
        os.environ[key] = value
    os.environ["GITHUB_ACTIONS"] = "true"  # Use the browser bundled in the container.
    # cron does not inherit Docker image ENV. Restore the image's browser cache
    # before starting Playwright, while retaining an explicitly configured path.
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/ms-playwright"
    os.chdir(APP_DIR)
    from utils import config
    config.config = None
    config.userData = None
    config.get_config()
    accounts = config.get_userData()
    return values, accounts


def prepare():
    values, accounts = load_configuration()
    hour, minute, second, zone = schedule_settings(values)
    zone_path = (Path("/usr/share/zoneinfo") / zone).resolve()
    if not zone_path.is_relative_to(Path("/usr/share/zoneinfo")) or not zone_path.is_file():
        raise ValueError("Timezone data is unavailable")
    localtime = Path("/etc/localtime")
    localtime.unlink(missing_ok=True)
    localtime.symlink_to(zone_path)
    Path("/etc/timezone").write_text(zone + "\n", encoding="utf-8")
    cron = Path("/etc/cron.d/douyin-spark-flow")
    cron.write_text(
        "SHELL=/bin/bash\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
        f"{minute} {hour} * * * root /app/docker/run-task.sh >> /proc/1/fd/1 2>> /proc/1/fd/2\n",
        encoding="utf-8",
    )
    cron.chmod(0o644)
    print(f"[docker] {len(accounts)} account(s); daily {hour:02}:{minute:02}:{second:02} {zone}", flush=True)


def run(resume=False):
    from utils.task_report import TaskReport, prepare_resume
    from utils.notify import notify
    from core.browser import BrowserStartupError
    report_path = APP_DIR / 'logs' / 'last-run.json'
    report = None if resume else TaskReport(report_path)
    phase = "configuration_invalid"
    try:
        # Load notification settings even when task validation later fails.
        path = Path(os.environ.get("CONFIG_ENV_PATH", "/app/.env"))
        if path.is_file():
            settings = dotenv_values(path, interpolate=False)
            for key in ("NOTIFY_PROVIDER", "PUSHPLUS_TOKEN"):
                if settings.get(key) is not None:
                    os.environ[key] = settings[key]
        values, accounts = load_configuration()
        pending = None
        if resume:
            previous, pending = prepare_resume(report_path, accounts, schedule_settings(values)[3])
            report = TaskReport(report_path, previous=previous)
            print(f'[docker] Resume: {sum(len(a["targets"]) for a in pending)} targets pending; previous submissions/uncertain outcomes retained.', flush=True)
        else:
            report.initialize_accounts(accounts)
        phase = "task_failed"
        _, _, second, _ = schedule_settings(values)
        if second and not resume:
            time.sleep(second)
        print("[docker] Starting configured task", flush=True)
        from core.tasks import runTasks
        if resume:
            runTasks(report=report, accounts=pending)
        else:
            runTasks(report=report)
        if any(row['status'] != 'submitted_unverified' for account in report.data['accounts'] for row in account['targets']):
            raise RuntimeError('仍有未完成或状态未知的目标，请核对结果。')
    except BaseException as error:
        reason = "browser_error" if isinstance(error, BrowserStartupError) else phase
        if report is not None:
            report.finish("failed", reason)
        print("[docker] Task failed; inspect the preceding application error. Do not resend blindly.", file=sys.stderr, flush=True)
        raise
    else:
        report.finish("submitted_unverified")
        print("[docker] Task finished; verify actual delivery in Douyin.", flush=True)
    finally:
        # Notification problems never retry or change the Douyin task result.
        if report is not None:
            try:
                report.data["notification"] = notify(report.data)
            except Exception:
                report.data["notification"] = {"status": "failed", "reason": "notification_internal_error"}
            report.save()
            print(f"[docker] Notification status: {report.data['notification']['status']}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("validate", "prepare", "run"))
    parser.add_argument('--resume', action='store_true', help='Resume only definitely unsubmitted targets from today, preserving previous results.')
    args = parser.parse_args()
    if args.action == "validate":
        _, accounts = load_configuration()
        print(f"Configuration is valid for {len(accounts)} account(s). No message was sent.")
    elif args.action == "prepare":
        prepare()
    else:
        run(resume=args.resume)
