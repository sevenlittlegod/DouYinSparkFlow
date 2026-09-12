"""Read configured target identities without opening conversations or sending."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
from utils import norm

IDENTITY_FIELDS = ("short_id", "unique_id", "sec_uid", "nickname", "remark_name")
ID_FIELDS = IDENTITY_FIELDS[:3]
LIST_SELECTOR = ".conversationConversationListwrapper"
MAX_SECONDS = 150


class AuditError(RuntimeError):
    """An error message safe to display without credentials or page content."""


class IdentityStore:
    def __init__(self):
        self.records = {}
        self.responses = 0
        self.invalid_responses = 0

    def add(self, item):
        if not isinstance(item, dict):
            return
        record = {
            field: str(item[field]) if item.get(field) is not None else ""
            for field in IDENTITY_FIELDS
        }
        # A source identity, rather than a nickname, is the deduplication key.
        # Profiles sharing a nickname/remark therefore remain separate.
        key = next(
            ((field, record[field]) for field in ("sec_uid", "unique_id", "short_id")
             if record[field] and record[field] != "0"),
            None,
        )
        if key is None:
            return
        self.records[key] = record

    def on_response(self, response):
        if "aweme/v1/web/im/user/info" not in response.url:
            return
        try:
            payload = response.json()
            rows = payload.get("data", [])
            if not isinstance(rows, list):
                self.invalid_responses += 1
                return
            self.responses += 1
            for item in rows:
                self.add(item)
        except Exception:
            # Do not print response bodies, URLs, or Playwright exception data.
            self.invalid_responses += 1


def match_targets(targets, identities):
    """Prefer exact IDs; normalized display names are only a fallback."""
    display_title_counts = {}
    for item in identities:
        title = norm(item["remark_name"] or item["nickname"])
        if title:
            display_title_counts[title] = display_title_counts.get(title, 0) + 1
    results = []
    for target in targets:
        candidates = [
            item for item in identities
            if any(item[field] == target and item[field] not in ("", "0")
                   for field in ID_FIELDS)
        ]
        matched_by = "id"
        if not candidates:
            matched_by = "name"
            candidates = [
                item for item in identities
                if any(item[field] and norm(item[field]) == norm(target)
                       for field in ("nickname", "remark_name"))
            ]
        results.append({
            "target": target,
            "status": "missing" if not candidates else "matched" if len(candidates) == 1 else "ambiguous",
            "matched_by": matched_by if candidates else None,
            "identities": [{field: item[field] for field in IDENTITY_FIELDS} for item in candidates],
            # Report only whether a target has a shared display title. The other
            # person's identity is deliberately not included for ID matches.
            "sender_title_collision": any(
                display_title_counts.get(norm(item["remark_name"] or item["nickname"]), 0) > 1
                for item in candidates
            ),
        })
    return results


def remaining_ms(deadline, cap=45000):
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise AuditError("Read-only audit reached its time limit before the chat list loaded.")
    return min(cap, remaining)


def audit_account(browser, account, deadline):
    store = IdentityStore()
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    try:
        context.set_default_timeout(5000)
        try:
            context.add_cookies(account["cookies"])
        except Exception:
            raise AuditError("Saved login cookies could not be loaded.") from None
        page = context.new_page()
        page.on("response", store.on_response)
        try:
            page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded",
                      timeout=remaining_ms(deadline))
            page.wait_for_selector(LIST_SELECTOR, state="visible", timeout=remaining_ms(deadline))
        except Exception:
            raise AuditError("The chat list did not load. Check login, verification, or changed page structure.") from None

        page.wait_for_timeout(remaining_ms(deadline, 3000))
        scrollable = page.locator(LIST_SELECTOR).first
        empty_rounds = 0
        reached_bottom = False
        previous = None
        while time.monotonic() < deadline:
            # Only scroll geometry is read; no conversation titles or messages.
            geometry = scrollable.evaluate("el => ({top: el.scrollTop, height: el.scrollHeight, viewport: el.clientHeight})")
            state = (len(store.records), geometry["top"], geometry["height"])
            at_bottom = geometry["top"] + geometry["viewport"] >= geometry["height"] - 2
            if state == previous and at_bottom:
                empty_rounds += 1
            else:
                empty_rounds = 0
            if empty_rounds >= 10:
                reached_bottom = True
                break
            previous = state
            scrollable.evaluate("el => { el.scrollTop += Math.max(400, Math.min(800, el.clientHeight * 0.8)); }")
            # Playwright's wait dispatches network-response events; time.sleep does not.
            page.wait_for_timeout(max(1, min(2000, int((deadline - time.monotonic()) * 1000))))

        return {
            "unique_id": account["unique_id"],
            "scan_complete": reached_bottom,
            "identity_responses_received": store.responses,
            "identity_response_errors": store.invalid_responses,
            "targets": match_targets(account["targets"], list(store.records.values())),
        }
    finally:
        context.close()


def save_report(report, path=None):
    path = Path(path or APP_DIR / "logs" / "target-audit.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".target-audit-", dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    # This imports configuration/runtime setup only. It never imports core.tasks.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from runtime import load_configuration
    report = {"status": "failed", "updated_at": datetime.now(timezone.utc).isoformat(), "accounts": []}
    playwright = browser = None
    try:
        _, accounts = load_configuration()
        from core.browser import get_browser
        started = get_browser()
        if not started:
            raise AuditError("The audit browser could not start.")
        playwright, browser = started
        deadline = time.monotonic() + MAX_SECONDS
        for account in accounts:
            report["accounts"].append(audit_account(browser, account, deadline))
        report["status"] = "complete" if all(row["scan_complete"] for row in report["accounts"]) else "partial"
        save_report(report)
        print("Read-only target audit saved to logs/target-audit.json. No message was sent.", flush=True)
        return 0
    except Exception as error:
        report["error"] = str(error) if isinstance(error, AuditError) else f"Read-only audit failed ({type(error).__name__}); no message was sent."
        save_report(report)
        print(report["error"], file=sys.stderr, flush=True)
        return 1
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()


if __name__ == "__main__":
    sys.exit(main())
