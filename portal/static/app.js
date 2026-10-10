"use strict";
const base = document.querySelector('meta[name="portal-base"]').content;
const $ = id => document.getElementById(id);
let session = null;
let currentAccount = null;
let setupToken = null;
let binding = null, bindingTimer = null;
let selectedFriends = new Set();
const bindingLive = new Set(['queued', 'starting', 'waiting_scan', 'reading']);

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = !message;
}

async function api(path, data) {
  const options = {credentials: "same-origin", headers: {}};
  if (data !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    if (session) options.headers["X-CSRF-Token"] = session.csrf;
    options.body = JSON.stringify(data);
  }
  const response = await fetch(base + "/api" + path, options);
  let result;
  try { result = await response.json(); }
  catch { throw new Error("服务暂时不可用，请稍后重试。"); }
  if (!response.ok) throw new Error(result.error || "操作未完成，请重试。");
  return result;
}

function element(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}

function action(text, callback, className = "secondary") {
  const button = element("button", text, className);
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try { await callback(); } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  });
  return button;
}

function countFriends() {
  $("friend-count").textContent = $("friends").value.split(/\r?\n/).filter(x => x.trim()).length + " 位";
}

function renderOwner(account) {
  currentAccount = account;
  const config = account.pending || account.active;
  $("username").value = config?.username || account.label;
  $("unique-id").value = config?.unique_id || "";
  $("cookies").value = "";
  $("cookie-saved").textContent = config?.cookies_configured ? "已保存 · 留空保留" : "首次必填";
  $("friends").value = (config?.friends || []).map(f => f.nickname + " " + f.douyin_id).join("\n");
  $("message").value = config?.message || "今日自动续火花[赞]";
  $("time").value = String(config?.hour ?? 9).padStart(2, "0") + ":" + String(config?.minute ?? 0).padStart(2, "0");
  $("pushplus").value = "";
  $("clear-push").checked = false;
  $("consent").checked = false;
  $("push-saved").textContent = config?.pushplus_configured ? "已配置 · 留空保留" : "未配置";
  $("owner-status").classList.toggle("pending", !!account.pending);
  $("owner-status").textContent = account.pending
    ? `版本 ${account.revision} 已提交，等待管理员审核。` + (account.enabled ? `\n当前仍按已批准的版本 ${account.active_revision} 执行。` : "\n审核启用前不会发送消息。")
    : account.enabled ? `已启用版本 ${account.active_revision}，每天按设置时间运行。修改后需要重新审核。`
    : account.active ? "账号已停用。可请管理员恢复已批准版本；也可以修改后重新提交审核。" : "尚未提交。完成下面四步后交给管理员审核。";
  countFriends();
  bindingButtons();
}

function bindingButtons() {
  const busy = bindingLive.has(binding?.state);
  const consent = $('binding-consent').checked;
  $('start-binding').disabled = busy || !consent;
  $('read-friends').disabled = busy || !consent || !(currentAccount?.pending || currentAccount?.active)?.cookies_configured;
  $('cancel-binding').hidden = !binding || ![...bindingLive, 'ready'].includes(binding.state);
}

function friendLines() {
  return $('friends').value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
}

function lineId(line) { return line.split(/[，,\t]+|\s+/).at(-1); }

function renderConversations() {
  const query = $('friend-search').value.trim().toLocaleLowerCase();
  const container = $('conversation-list'); container.replaceChildren();
  const rows = (binding?.friends || []).filter(f => (f.nickname + ' ' + f.douyin_id).toLocaleLowerCase().includes(query));
  for (const friend of rows) {
    const label = element('label', undefined, 'conversation-choice');
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox';
    checkbox.checked = selectedFriends.has(friend.douyin_id); checkbox.disabled = friend.ambiguous;
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) selectedFriends.add(friend.douyin_id); else selectedFriends.delete(friend.douyin_id);
      $('apply-friends').textContent = `应用所选 ${selectedFriends.size} 位好友`;
    });
    const text = element('span'); text.append(element('strong', friend.nickname), element('small', friend.douyin_id + (friend.ambiguous ? ' · 同名，请先设置不同备注' : '')));
    label.append(checkbox, text); container.append(label);
  }
  if (!rows.length) container.append(element('p', '没有匹配的会话。可以在下方手动填写好友。', 'muted'));
  $('apply-friends').textContent = `应用所选 ${selectedFriends.size} 位好友`;
}

function renderBinding(result) {
  const changed = binding?.id !== result?.id || binding?.state !== result?.state;
  binding = result;
  $('binding-panel').hidden = !result;
  const ready = result?.state === 'ready';
  $('unique-id').readOnly = ready;
  $('cookies').disabled = ready;
  $('binding-identity').hidden = !ready;
  $('conversation-picker').hidden = !ready;
  $('binding-qr').hidden = result?.state !== 'waiting_scan' || !result?.qr_version;
  if ($('binding-qr').hidden) $('binding-qr').removeAttribute('src');
  else {
    const src = `${base}/api/binding/${result.id}/qr?v=${result.qr_version}`;
    if ($('binding-qr').getAttribute('src') !== src) $('binding-qr').src = src;
  }
  if (result) {
    const labels = {queued:'正在排队。扫码与定时发送依次执行，早晨 08:20–09:10 优先保留给定时任务。',starting:'正在启动抖音登录页面，通常需要几十秒。',waiting_scan:'请用本人抖音 App 扫码，并在手机确认登录。',reading:'已登录，正在核对本人身份并读取最近会话。',ready:'账号已核实。请选择好友，再提交配置等待审核。',consumed:'登录凭据已随配置提交审核。',failed:'本次操作未完成。',expired:'本次结果已过期，请重新开始。',cancelled:'已取消本次操作。'};
    $('binding-status').textContent = result.error || labels[result.state] || '请重新开始。';
    const left = Math.max(0, Math.ceil((result.expires_at * 1000 - Date.now()) / 60000));
    $('binding-expiry').textContent = bindingLive.has(result.state) || ready ? `本次操作及未提交结果将在约 ${left} 分钟后过期。` : '';
    if (ready) {
      $('unique-id').value = result.identity.unique_id;
      $('cookies').value = '';
      $('cookie-saved').textContent = '扫码凭据已准备 · 提交后保存';
      $('binding-identity').textContent = `已核实：${result.identity.nickname} · 抖音号 ${result.identity.unique_id}`;
      $('conversation-summary').textContent = `读取到 ${result.friends.length} 位最近会话联系人。${result.partial ? '读取未完整结束，名单可能不全。' : '仅包含网页最近会话，不等于全部好友。'}尚未发送任何消息。`;
      if (changed) {
        const ids = new Set(friendLines().map(lineId));
        selectedFriends = new Set(result.friends.filter(f => !f.ambiguous && ids.has(f.douyin_id)).map(f => f.douyin_id));
        renderConversations();
      }
    }
  }
  bindingButtons();
}

function scheduleBindingPoll() {
  clearTimeout(bindingTimer);
  if (!binding || ![...bindingLive, 'ready'].includes(binding.state)) return;
  bindingTimer = setTimeout(async () => {
    try { renderBinding((await api(`/binding/${binding.id}`)).binding); scheduleBindingPoll(); }
    catch (error) { $('binding-status').textContent = error.message + ' 刷新页面可重新查看。'; }
  }, binding.state === 'ready' ? 15000 : 2500);
}

async function startBinding(mode) {
  if (!$('binding-consent').checked) return;
  $('start-binding').disabled = true; $('read-friends').disabled = true;
  try {
    renderBinding((await api('/binding/start', {mode, revision:currentAccount.revision})).binding);
    scheduleBindingPoll();
  } catch (error) { notice(error.message, true); bindingButtons(); }
}

function configDetails(config) {
  const dl = element("dl", undefined, "config-list");
  for (const [key, value] of [
    ["本人抖音号", config.unique_id], ["好友数量", config.friends.length + " 位"],
    ["每天执行", String(config.hour).padStart(2, "0") + ":" + String(config.minute).padStart(2, "0") + "（北京时间）"],
    ["登录凭据", config.cookies_configured ? "已保存（有效性以执行结果为准）" : "缺失"],
    ["微信通知", config.pushplus_configured ? "已配置 PushPlus" : "未开启"], ["消息内容", config.message]
  ]) { dl.append(element("dt", key), element("dd", value)); }
  const box = element("div"); box.append(dl);
  const details = element("details");
  details.append(element("summary", `查看 ${config.friends.length} 位好友`), element("div", config.friends.map(f => f.nickname + " · " + f.douyin_id).join("\n"), "friend-list"));
  box.append(details);
  return box;
}

async function showInvitation(data) {
  const {invite} = await api("/admin/invites", data);
  $("invite-url").value = invite.url;
  $("invite-result").hidden = false;
  $("invite-result").scrollIntoView({block: "center", behavior: "smooth"});
  notice("邀请已创建。请复制链接，私下发送给账号本人。");
  await renderAdmin();
}

async function renderAdmin() {
  const {accounts} = await api("/admin/accounts");
  $("accounts").replaceChildren();
  if (!accounts.length) $("accounts").append(element("p", "还没有网页接入的账号。先生成一份邀请。", "empty"));
  for (const account of accounts) {
    const card = element("article", undefined, "card");
    const title = element("div", undefined, "account-title");
    title.append(element("h2", account.pending?.username || account.active?.username || account.label));
    title.append(element("span", account.pending ? "待审核" : account.enabled ? "已启用" : "未启用", "badge " + (account.pending ? "pending" : account.enabled ? "enabled" : "")));
    card.append(title, element("div", `配置版本 ${account.revision} · ` + (account.enabled ? `运行版本 ${account.active_revision}` : "定时执行已关闭"), "account-meta"));
    if (account.pending) {
      card.append(element("p", `以下是待审核版本 ${account.revision}。批准后进入定时队列；若已过今天设定时间，可能当天尽快执行。08:30–09:10 优先运行原有任务。`, "muted"), configDetails(account.pending));
      if (account.active) {
        const old = element("details"); old.append(element("summary", `对照已批准版本 ${account.active_revision}`), configDetails(account.active)); card.append(old);
      }
    } else if (account.active) card.append(configDetails(account.active));
    else card.append(element("p", "邀请已建立，等待账号本人填写。", "muted"));
    if (account.run_status) {
      const result = account.run_status;
      const labels = {submitted_unverified: "消息已提交（送达未验证）", failed: "执行失败或发送结果待核对", running: "执行中", partial: "部分未完成", success: "完成", worker_failed: "任务未能启动或执行异常", interrupted: "执行中断，请核对后再补跑", configuration_conflict: "与原有账号重复，请停用重复配置"};
      card.append(element("p", `最近执行：${labels[result.status] || result.status || "暂无"} · 已提交 ${result.submitted ?? "—"} / ${result.total ?? "—"}` + (result.updated_at ? " · " + result.updated_at : ""), "run-status"));
    }
    const actions = element("div", undefined, "actions");
    if (account.pending) actions.append(action(`批准版本 ${account.revision} 并启用`, async () => {
      if (!confirm(`已核对“${account.pending.username}”的本人抖音号、好友和消息内容？\n批准版本 ${account.revision} 后，服务器将排队自动发送。若已过今天的设定时间，可能当天尽快执行。`)) return;
      await api(`/admin/accounts/${account.id}/approve`, {revision: account.revision}); await renderAdmin(); notice("已批准启用。若今天尚未执行且设定时间已过，会尽快排队运行。");
    }, ""));
    if (account.enabled) actions.append(action("停用账号", async () => {
      if (!confirm("停用后将不再启动此账号的新定时任务，已经开始的任务可能继续完成。确认停用？")) return;
      await api(`/admin/accounts/${account.id}/disable`, {}); await renderAdmin(); notice("已停用此账号的后续定时任务。");
    }, "danger"));
    if (!account.enabled && account.active) actions.append(action(`恢复已批准版本 ${account.active_revision}`, async () => {
      if (!confirm(`确认恢复已批准版本 ${account.active_revision}？\n${account.pending ? "当前待审核修改不会生效。\n" : ""}若今天尚未执行且设定时间已过，可能当天尽快排队发送。`)) return;
      await api(`/admin/accounts/${account.id}/enable`, {active_revision: account.active_revision}); await renderAdmin(); notice("已恢复指定的已批准版本；待审核配置仍需另行批准。");
    }));
    actions.append(action("生成修改邀请", () => showInvitation({label: account.label, account_id: account.id, days: 7})));
    card.append(actions);
    const invites = element("details"); invites.append(element("summary", "管理已有邀请"));
    for (const invite of account.invites) {
      const row = element("div", undefined, "invite-row");
      const expired = invite.expires_at * 1000 <= Date.now();
      row.append(element("span", invite.revoked ? "已撤销" : expired ? "已过期" : `有效至 ${new Date(invite.expires_at * 1000).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai"})}`));
      if (!invite.revoked && !expired) row.append(action("撤销", async () => {
        if (!confirm("撤销此邀请并退出使用它登录的用户？已启用的定时任务不受影响。")) return;
        await api(`/admin/invites/${invite.id}/revoke`, {}); await renderAdmin(); notice("邀请已撤销。");
      }, "quiet"));
      invites.append(row);
    }
    card.append(invites); $("accounts").append(card);
  }
}

async function showPage() {
  for (const id of ["welcome", "admin-login", "auth-setup", "auth-recovery", "owner", "admin"]) $(id).hidden = true;
  $("logout").hidden = !session;
  if (session?.role === "admin") {
    $("heading").textContent = "账号管理"; $("intro").textContent = "发出邀请，核对配置，再批准启用。";
    $("admin").hidden = false; await renderAdmin();
  } else if (location.pathname.replace(/\/$/, "").endsWith("/admin")) {
    $("heading").textContent = "安全登录";
    $("intro").textContent = "密码与手机验证器，共同保护账号管理。";
    const auth = await api("/auth/status");
    $(auth.configured ? "admin-login" : "auth-setup").hidden = false;
  } else if (session?.role === "owner") {
    $("owner").hidden = false; renderOwner((await api("/account")).account);
    renderBinding((await api('/binding')).binding); scheduleBindingPoll();
  } else $("welcome").hidden = false;
}

$("login-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    session = (await api("/login", {password: $("admin-password").value, code: $("admin-code").value.trim(), recovery: $("use-recovery").checked})).session;
    $("admin-password").value = ""; $("admin-code").value = ""; notice(""); await showPage();
  }
  catch (error) { notice(error.message, true); } finally { button.disabled = false; }
});
$("use-recovery").addEventListener("change", () => {
  const recovery = $("use-recovery").checked;
  $("code-label").textContent = recovery ? "一次性恢复码" : "验证器动态码";
  $("admin-code").value = "";
  $("admin-code").inputMode = recovery ? "text" : "numeric";
  $("admin-code").maxLength = recovery ? 32 : 6;
  if (recovery) $("admin-code").removeAttribute("pattern");
  else $("admin-code").pattern = "[0-9]{6}";
  $("admin-code").placeholder = recovery ? "输入一条尚未使用的恢复码" : "6 位数字";
  $("code-help").textContent = recovery ? "密码仍然必填；每条恢复码只能使用一次。" : "动态码每 30 秒更新，同一个码只能使用一次。请开启手机自动设置时间。";
});
$("setup-start-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const setup = await api("/auth/setup", {token: $("bootstrap-token").value});
    $("bootstrap-token").value = ""; setupToken = setup.setup_token;
    $("totp-qr").src = setup.qr; $("totp-secret").value = setup.secret;
    $("setup-start-form").hidden = true; $("setup-confirm-form").hidden = false; notice("");
  } catch (error) { notice(error.message, true); } finally { button.disabled = false; }
});
$("setup-confirm-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter;
  if ($("new-password").value !== $("repeat-password").value) { notice("两次输入的密码不一致，请重新核对。", true); $("repeat-password").focus(); return; }
  button.disabled = true;
  try {
    const result = await api("/auth/confirm", {setup_token: setupToken, password: $("new-password").value, code: $("setup-code").value.trim()});
    session = result.session; setupToken = null;
    for (const id of ["new-password", "repeat-password", "setup-code", "totp-secret"]) $(id).value = "";
    $("totp-qr").removeAttribute("src"); $("auth-setup").hidden = true; $("auth-recovery").hidden = false;
    $("recovery-codes").value = result.recovery_codes.join("\n"); notice("密码和验证器已启用。请先保存恢复码。 ");
  } catch (error) { notice(error.message, true); } finally { button.disabled = false; }
});
$("recovery-saved").addEventListener("change", () => { $("finish-setup").disabled = !$("recovery-saved").checked; });
$("finish-setup").addEventListener("click", async () => {
  if (!$("recovery-saved").checked) return;
  $("recovery-codes").value = ""; notice(""); await showPage();
});
$("account-form").addEventListener("submit", async event => {
  event.preventDefault(); $("save").disabled = true;
  try {
    const [hour, minute] = $("time").value.split(":").map(Number);
    const config = {username: $("username").value, unique_id: $("unique-id").value, cookies: $("cookies").value,
      friends: $("friends").value, message: $("message").value, hour, minute,
      pushplus_token: $("pushplus").value.trim(), clear_pushplus: $("clear-push").checked};
    const saved = await api("/account", {config, revision: currentAccount.revision, binding_id: binding?.state === 'ready' ? binding.id : undefined});
    clearTimeout(bindingTimer); renderBinding(null); renderOwner(saved.account);
    notice("配置已提交，等待管理员审核。修改后的版本尚未启用。"); window.scrollTo({top: 0, behavior: "smooth"});
  } catch (error) { notice(error.message, true); $("notice").scrollIntoView({block: "center"}); }
  finally { $("save").disabled = false; }
});
$("invite-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try { await showInvitation({label: $("invite-label").value, days: Number($("invite-days").value)}); }
  catch (error) { notice(error.message, true); } finally { button.disabled = false; }
});
$("copy-invite").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("invite-url").value); notice("邀请链接已复制。"); }
  catch { $("invite-url").select(); notice("请长按或按 Ctrl+C 复制选中的完整链接。"); }
});
$("refresh").addEventListener("click", async () => { try { await renderAdmin(); notice("状态已刷新。"); } catch (error) { notice(error.message, true); } });
$("friends").addEventListener("input", countFriends);
$('binding-consent').addEventListener('change', bindingButtons);
$('start-binding').addEventListener('click', () => startBinding('qr'));
$('read-friends').addEventListener('click', () => startBinding('saved'));
$('friend-search').addEventListener('input', renderConversations);
$('cancel-binding').addEventListener('click', async () => {
  if (!binding) return;
  try { await api(`/binding/${binding.id}/cancel`, {}); clearTimeout(bindingTimer); renderBinding((await api(`/binding/${binding.id}`)).binding); }
  catch (error) { notice(error.message, true); }
});
$('apply-friends').addEventListener('click', () => {
  if (binding?.state !== 'ready') return;
  const available = new Set(binding.friends.filter(f => !f.ambiguous).map(f => f.douyin_id));
  const lines = friendLines().filter(line => !available.has(lineId(line)));
  for (const friend of binding.friends) if (!friend.ambiguous && selectedFriends.has(friend.douyin_id)) lines.push(friend.nickname + ' ' + friend.douyin_id);
  if (lines.length > 200) { notice('最多选择 200 位好友，请减少名单。', true); return; }
  $('friends').value = lines.join('\n'); countFriends();
  notice('所选好友已填入名单。核对消息与时间后，提交配置等待审核。');
});
$("logout").addEventListener("click", async () => {
  try { await api("/logout", {}); session = null; location.replace(base + "/"); } catch (error) { notice(error.message, true); }
});

(async () => {
  try {
    const fragment = new URLSearchParams(location.hash.slice(1));
    const invitation = fragment.get("invite");
    if (location.hash) history.replaceState(null, "", location.pathname);
    session = invitation ? (await api("/exchange", {token: invitation})).session : (await api("/session")).session;
    await showPage();
  } catch (error) { notice(error.message, true); $("welcome").hidden = false; }
})();
