# 邀请制账号配置页

使用 Python 标准库和本地二维码库 qrcode，没有前端构建或外部 CDN。此页面只管理通过邀请接入的新账号；不会自动迁移旧 `.env` 账号，也不会抓取好友或提供抖音二维码登录。本轮采用本人粘贴 `sessionid`、完整 Cookie 或浏览器导出的 Cookie JSON，然后填写好友名单。

## 访问与使用

1. 管理员打开 `/spark/admin`。首次用 `portal-data/admin-token` 初始化，设置密码、扫描验证器二维码并输入动态码确认，保存 10 条一次性恢复码。之后必须用密码和动态码（或一条恢复码）登录；旧初始化凭据不能绕过 MFA。不要将密码、二维码或恢复码发给填写者。
2. 生成邀请链接，私下发给账号本人。邀请默认有效 7 天，可撤销，过期后管理员可以给同一账号重新生成。邀请持有人拥有该账号的填写权限。
3. 填写者在电脑抖音网页版登录，从开发者工具 Application / 应用 → Cookies → `https://www.douyin.com` 复制 `sessionid` 值到页面；填写本人的抖音号、好友昵称和抖音号、消息内容、北京时间及可选 PushPlus Token。
4. 提交后只是待审核。管理员核对完整的版本、好友和消息内容，点击“批准版本 N 并启用”。如果已过当天所设时间，worker 可能当天尽快排队执行。当前小内存服务器 08:30–09:10 优先运行旧账号，新账号在此窗口后排队。
5. 再次编辑生成新的待审核版本；已启用账号在批准新版本前仍使用原先批准的配置。登录凭据和推送 Token 保存后不回显，留空会保留，关闭通知需明确勾选移除。停用只阻止新的任务启动，已启动的任务可能继续完成。管理员可明确恢复指定的已批准版本；这个动作不会批准或使用尚待审核的修改。

网页收到保存成功并不表示 Cookie 有效，也不表示消息已送达。这里没有即时发送或推送测试按钮，执行结果由单独的账号 worker 回写。

## 本地启动

```sh
python -m portal.server --data-dir ./portal-data --host 127.0.0.1 --port 18766
```

启动前运行 `python -m pip install -r portal/requirements.txt`。首次启动生成随机初始化凭据到 `portal-data/admin-token`，只输出文件路径。直接打开 `http://127.0.0.1:18766/admin`。HTTP 只支持本机，分享给别人必须先使用 HTTPS 反向代理。

生产配置环境变量：

```ini
PORTAL_DATA_DIR=/opt/douyinsparkflow/portal-data
PORTAL_ADMIN_TOKEN_FILE=/opt/douyinsparkflow/portal-data/admin-token
PORTAL_PUBLIC_ORIGIN=https://YOUR_DOMAIN
PORTAL_BASE_PATH=/spark
PORTAL_PORT=18766
```

反向代理 `/spark/` 到 `http://127.0.0.1:18766/`，设置 `Host $host`，保留浏览器 `Origin`；Nginx 应设置 `client_max_body_size 192k`。服务仅监听 127.0.0.1。全站 Cookie Path 使用 `/spark/`，HTTPS 下添加 Secure，始终启用 HttpOnly 和 SameSite=Strict。不要把数据目录设为 Web 根目录。绑定后的凭据恢复必须经 SSH 执行专用重置命令，详见 [维护教程](../docs/多账号配置网页.md)，不能仅替换 `admin-token`。

## 运行器接口

```python
from portal.store import Store
store = Store('/opt/douyinsparkflow/portal-data')
for account in store.active_accounts():
    # account: {id: 随机hex, revision: 已批准版本, config: ...}
    # config: username, unique_id, cookies, friends[{nickname,douyin_id}],
    #         message, hour, minute, pushplus_token
    pass
store.update_run_status(account_id, {
    'status': 'submitted_unverified', 'updated_at': '2026-10-10T10:00:00+08:00',
    'submitted': 5, 'failed': 0, 'unknown': 0, 'not_attempted': 0, 'total': 5, 'exit_code': 0
})
```

Runner 可以读取秘密配置；HTTP 接口始终剔除 Cookie 和 Token。`active_accounts()` 每次从数据库读取当前启用快照。配置保存和批准使用 SQLite 事务与版本比较，多个进程可并发访问。

`update_run_status` 只接受字符串 `status/updated_at/started_at`（最多 80 字符），整数 `submitted/failed/unknown/not_attempted/total/exit_code`。不要将原始异常、浏览器页面、完整日志或配置作为 status 填入。新 worker 仍需排除旧配置账号的重复抖音号，避免双重发送。

SQLite 含登录凭据，数据目录权限 700、数据库和管理员凭据文件权限 600；服务器管理员和该系统用户可以读取。备份同样包含凭据，应限制访问，不能提交 Git。所有请求不记访问日志，邀请 Token 通过 URL fragment 进入页面后交换 HttpOnly 会话并立即从地址栏移除，不存 localStorage。端点校验 Host、Origin、CSRF、账号权限和请求大小。

验证：`python -m unittest discover -s tests -p test_portal.py -v`。
