"""Confirm visible sender-side evidence; never retry a send or claim delivery."""
import time


class SubmissionUnconfirmed(RuntimeError):
    reason = 'submission_unconfirmed'


class SubmissionRejected(SubmissionUnconfirmed):
    reason = 'send_rejected'


_SNAPSHOT = r"""message => {
    const clean = s => (s || '').replace(/[\s\u200b-\u200d\ufeff]+/g, '');
    const expected = clean(message);
    const textOf = el => {
        const copy = el.cloneNode(true);
        copy.querySelectorAll('img').forEach(img => img.replaceWith(img.alt || img.title || ''));
        return clean(copy.textContent);
    };
    const boxes = [...document.querySelectorAll('.messageMessageBoxcontentBox.messageMessageBoxisFromMe')];
    const matching = boxes.filter(box => {
        const text = box.querySelector('.TextMessageTexttextInnerContent');
        return text && expected && textOf(text) === expected;
    });
    const failure = /发送失败|无法发送|发送频繁|发送过于频繁|操作频繁|网络异常|私信功能.*限制/;
    const notices = [...document.querySelectorAll('[role="alert"],[class*="toast" i]')]
        .filter(el => el.getClientRects().length).some(el => failure.test(el.innerText || ''));
    // The real chat renders newest first. Inspect all matching rows rather than
    // assuming the last DOM node is the message just sent; ambiguity stays safe.
    const rows = matching.map(box => box.closest('.messageMessageBoxmessageBox') || box);
    const failed = rows.some(row => failure.test(row.innerText || '') ||
        !!row.querySelector('[class*="fail" i],[class*="error" i],[aria-label="发送失败"]'));
    const pending = rows.some(row => !!row.querySelector('[class*="sending" i],[class*="loading" i],[class*="spinner" i]'));
    const editors = [...document.querySelectorAll('[contenteditable="true"][data-slate-editor="true"]')];
    return {matches: matching.length, editor_empty: editors.length === 1 && !clean(editors[0].innerText),
            rejected: notices || failed, pending};
}"""


def snapshot(page, message):
    return page.evaluate(_SNAPSHOT, message.replace(r'\n', '\n'))


def confirm_submission(page, message, before, timeout_ms=15000):
    """Require a new matching outgoing bubble and cleared editor, then settle.

    This is not a receiver acknowledgement. Missing/ambiguous evidence must stop
    the task after its one Enter attempt; never click another send control here.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    visible_since = None
    while time.monotonic() < deadline:
        current = snapshot(page, message)
        if current['rejected']:
            raise SubmissionRejected('抖音页面显示发送失败或限制，未自动重试。')
        evidence = (current['editor_empty'] and current['matches'] > before['matches']
                    and not current['pending'])
        if evidence:
            visible_since = visible_since or time.monotonic()
            if time.monotonic() - visible_since >= 1.5:
                return
        else:
            visible_since = None
        page.wait_for_timeout(250)
    raise SubmissionUnconfirmed('按键后未确认新消息及输入框清空，停止执行，请核对抖音。')
