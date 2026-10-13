"""Expose only human-selected authentication controls, never a remote browser."""
import re
from urllib.parse import urlsplit

BUTTONS = {
    'sms': re.compile(r'^接收短信(?:验证|验证码)?$'),
    'face': re.compile(r'^手机刷脸(?:验证)?$'),
    'send_code': re.compile(r'^(?:获取|发送|重新获取|重新发送)验证码$'),
    'confirm': re.compile(r'^(?:验证|确认|确定|提交|下一步|完成)$'),
}
CHALLENGE = re.compile(r'接收短信|刷脸|人脸(?:识别|验证|认证)|身份验证|验证身份|安全验证|扫码(?:完成|进行)?验证')


def verification_page(context, primary):
    # Authentication can open a separate trusted popup. No arbitrary URL or
    # browser navigation is accepted from the public control endpoint.
    for candidate in reversed(context.pages):
        if not candidate.is_closed() and trusted_frame(candidate.main_frame):
            return candidate
    return primary


def trusted_frame(frame):
    url = urlsplit(frame.url)
    return url.scheme == 'https' and any(url.hostname == host or (url.hostname or '').endswith('.'+host)
                                         for host in ('douyin.com','snssdk.com','bytedance.com'))


def exposed(locator):
    """Do not fill controls hidden behind the challenge overlay."""
    try:
        return locator.is_visible() and locator.evaluate('''el => {
            const r=el.getBoundingClientRect();
            const top=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
            return !!top && (top===el || el.contains(top));
        }''')
    except Exception:
        return False


def qr_exposed(locator):
    """The official QR has a separate 40px SVG logo covering its center."""
    try:
        return locator.is_visible() and locator.evaluate('''el => {
            const r=el.getBoundingClientRect();
            return [[.25,.25],[.75,.25],[.25,.75],[.75,.75]].every(([x,y])=>{
                const top=document.elementFromPoint(r.left+r.width*x,r.top+r.height*y);
                return top===el || el.contains(top);
            });
        }''')
    except Exception:
        return False


class VerificationBridge:
    def __init__(self):
        self.frame = None
        self.inputs = {}
        self.actions = {}

    def inspect(self, page):
        self.inputs, self.actions = {}, {}
        for frame in reversed(page.frames):
            if not trusted_frame(frame):
                continue
            try:
                if frame != page.main_frame and not exposed(frame.frame_element()):
                    continue
                body = frame.locator('body')
                text = body.inner_text(timeout=1200)
                if not CHALLENGE.search(text) and frame != self.frame:
                    continue
                actions, inputs = {}, {}
                for name, pattern in BUTTONS.items():
                    matches = [item for item in frame.get_by_text(pattern).all() if exposed(item)]
                    if len(matches) == 1:
                        actions[name] = matches[0]
                for item in frame.locator('input').all():
                    if not exposed(item) or not item.is_enabled():
                        continue
                    label = (item.get_attribute('placeholder') or '') + ' ' + (item.get_attribute('aria-label') or '')
                    kind = 'code' if '验证码' in label else 'phone' if re.search(r'手机|号码', label) else None
                    if kind:
                        if kind in inputs:  # Ambiguous fields are not operable.
                            inputs[kind] = None
                        else:
                            inputs[kind] = item
                inputs = {key:item for key,item in inputs.items() if item is not None}
                if not actions and not inputs and not CHALLENGE.search(text):
                    continue
                self.frame, self.inputs, self.actions = frame, inputs, actions
                return {
                    'actions':[{'id':key,'label':item.inner_text().strip()[:30]} for key,item in actions.items()],
                    'fields':[{'id':key,'label':'短信验证码' if key == 'code' else '手机号或页面要求的尾号',
                               'placeholder':(item.get_attribute('placeholder') or '')[:80]} for key,item in inputs.items()],
                    'message':'抖音要求本人完成身份验证。可按下方按钮继续，验证码只用于本次登录。',
                }
            except Exception:
                continue
        self.frame = None
        return None

    def screenshot(self, page):
        mask = list(self.inputs.values())
        if self.frame == page.main_frame:
            return page.screenshot(mask=mask)
        return self.frame.locator('body').screenshot(timeout=5000, mask=mask)

    def perform(self, page, command):
        # Reinspect immediately before action so stale buttons and fields cannot
        # target the normal chat or a different login form after navigation.
        if not self.inspect(page) or command['action'] not in self.actions:
            return False
        values = command.get('values', {})
        if any(key not in self.inputs for key in values):
            return False
        for key,value in values.items():
            if not exposed(self.inputs[key]): return False
            self.inputs[key].fill(value, timeout=3000)
        control = self.actions[command['action']]
        interactive = control.locator("xpath=ancestor-or-self::*[self::button or @role='button'][1]")
        if interactive.count() == 1 and exposed(interactive):
            control = interactive
        if not control.is_enabled(): return False
        if not exposed(control): return False
        control.click(timeout=5000)
        return True
