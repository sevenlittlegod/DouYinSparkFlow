"""One private PushPlus notification per completed task, with no send retry."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import os
import re

import requests

PUSHPLUS_ENDPOINT = "https://www.pushplus.plus/send"
STATUS_LABELS = {
    "submitted_unverified": "已提交",
    "failed": "提交失败",
    "missing": "未找到",
    "not_attempted": "未尝试",
    "unknown": "状态未知",
}
# Only known categories become prose. Raw exceptions/provider responses are never
# suitable notification content, even when supplied under an error/reason key.
REASON_LABELS = {
    "login_required": "需要重新登录",
    "page_timeout": "聊天页面加载超时",
    "list_timeout": "好友列表加载超时",
    "editor_timeout": "聊天输入框加载超时",
    "target_missing": "网页会话列表未匹配到目标",
    "not_found": "网页会话列表未匹配到目标",
    "ambiguous_target": "目标身份存在歧义",
    "identity_ambiguous": "目标身份存在歧义",
    "send_failed": "提交消息时发生错误",
    "configuration_invalid": "任务配置有误",
    "browser_error": "浏览器运行出错",
    "previous_failure": "前面的步骤失败，本目标未执行",
    "editor_unavailable": "输入框不可用",
    "message_build_failed": "消息生成失败",
    "message_input_failed": "消息输入失败",
    "submission_in_progress": "提交结果不确定，请勿盲目重发",
    "submission_uncertain": "提交结果不确定，请勿盲目重发",
    "target_not_found": "网页会话未找到目标",
    "login_or_page_unavailable": "登录失效或页面未加载，请检查",
    "cookie_load_failed": "登录状态无法载入",
    "not_started": "尚未开始",
    "run_stopped": "前面步骤出错后停止，未尝试",
    "task_failed": "任务执行失败",
}


def _inline(value, fallback="未提供", limit=160):
    if not isinstance(value, (str, int)):
        return fallback
    text = re.sub(r"[\x00-\x1f\x7f\s]+", " ", str(value)).strip()[:limit]
    if not text:
        return fallback
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", text)


def _time_label(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S（北京时间）")
    except (AttributeError, TypeError, ValueError):
        return "时间未记录"


def build_notification(report):
    """Return a Chinese title and Markdown body from allowlisted report fields."""
    if not isinstance(report, dict):
        raise ValueError("Invalid notification report")
    if report.get("test") is True:
        return (
            "抖音续火花：微信推送测试",
            "PushPlus 通知已接通；这只是推送测试，没有运行抖音发送任务。",
        )
    accounts = report.get("accounts", [])
    if not isinstance(accounts, list):
        raise ValueError("Invalid notification accounts")
    rows = []
    for account in accounts:
        if not isinstance(account, dict) or not isinstance(account.get("targets", []), list):
            raise ValueError("Invalid notification account")
        targets = []
        for target in account.get("targets", []):
            if not isinstance(target, dict):
                raise ValueError("Invalid notification target")
            status = target.get("status", "unknown")
            status = status if isinstance(status, str) and status in STATUS_LABELS else "unknown"
            targets.append((status, target))
        rows.append((account, targets))

    counts = Counter(status for _, targets in rows for status, _ in targets)
    has_problems = not sum(counts.values()) or report.get("status") != "submitted_unverified" or any(
        counts[status] for status in STATUS_LABELS if status != "submitted_unverified"
    )
    title = "抖音续火花：有未完成项" if has_problems else "抖音续火花：本次已提交"
    # Fixed titles deliberately contain neither account text nor line breaks.
    title = title[:32]
    body = [
        f"任务时间：{_time_label(report.get('updated_at'))}",
        "",
        "**“已提交”只表示脚本按过发送键，实际送达未验证，请以抖音聊天为准。**",
        "",
        "；".join(f"{label} {counts[status]} 人" for status, label in STATUS_LABELS.items()),
    ]
    if not sum(counts.values()):
        body.extend(["", "本次没有可列出的好友结果；请检查服务器任务状态。"])

    overall_reason = REASON_LABELS.get(report.get("error")) if isinstance(report.get("error"), str) else None
    if overall_reason:
        body.extend(["", f"任务情况：{overall_reason}。"])
    for account, targets in rows:
        body.extend(["", f"账号：{_inline(account.get('username'))}（{_inline(account.get('unique_id'))}）"])
        # Problems precede submissions, but every configured target is included.
        ordered = sorted(targets, key=lambda row: row[0] == "submitted_unverified")
        for status, target in ordered:
            target_id = _inline(target.get("target"))
            label = _inline(target.get("label"), fallback="")
            identity = f"{label}（{target_id}）" if label and label != target_id else target_id
            reason = REASON_LABELS.get(target.get("reason")) if isinstance(target.get("reason"), str) else None
            detail = f"；{reason}" if reason and status != "submitted_unverified" else ""
            body.append(f"- {STATUS_LABELS[status]}：{identity}{detail}")
    return title, "\n".join(body)


def notify(report):
    """Attempt once and return safe delivery-to-provider status; never raise."""
    provider = os.environ.get("NOTIFY_PROVIDER", "off").strip().lower()
    if provider == "off":
        return {"status": "disabled", "provider": "off"}
    if provider != "pushplus":
        return {"status": "failed", "provider": "unsupported", "reason": "unsupported_provider"}
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        return {"status": "failed", "provider": provider, "reason": "missing_token"}
    try:
        title, body = build_notification(report)
    except Exception:
        return {"status": "failed", "provider": provider, "reason": "invalid_report"}

    try:
        response = requests.post(
            PUSHPLUS_ENDPOINT,
            json={"token": token, "title": title, "content": body,
                  "template": "markdown", "channel": "wechat"},
            timeout=12,
            allow_redirects=False,
        )
    except Exception:
        return {"status": "failed", "provider": provider, "reason": "request_failed"}
    try:
        if response.status_code != 200:
            return {"status": "failed", "provider": provider, "reason": "http_error"}
        try:
            payload = response.json()
        except Exception:
            return {"status": "failed", "provider": provider, "reason": "invalid_response"}
        if not isinstance(payload, dict) or payload.get("code") != 200:
            return {"status": "failed", "provider": provider, "reason": "provider_rejected"}
        # Acceptance by PushPlus is not confirmation of receipt in WeChat.
        return {"status": "accepted", "provider": provider}
    except Exception:
        return {"status": "failed", "provider": provider, "reason": "invalid_response"}
    finally:
        try:
            response.close()
        except Exception:
            pass
