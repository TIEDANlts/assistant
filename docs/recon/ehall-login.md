# 验证 2：ehall 登录、会话与服务器可达性

状态：2026-09-23 已完成本机 HTTP 可达性探测、用户手动登录、受保护入口复用检查和短时会话观察；服务器验证、完整寿命和手机扫码识别仍待完成。结论同步到 ADR 0007 与 0009。

## 步骤

1. 本机和服务器各跑一次 `uv run python spikes/ehall_login.py reach --where <本机|服务器>`：不登录，只看能否访问、是否需要学校 VPN。
2. 本机：`uv run python spikes/ehall_login.py login`，在弹出的浏览器里登录，结束时回答几个观察问题。
3. 服务器：`login --headless --method qr --where 服务器`（二维码截图用 scp 取回后扫码），或 `--method password`（24 小时内失败 3 次就拒绝）。
4. 找一个必须登录才能看的页面：`check --probe-url <地址>`；再 `watch --interval 30 --max-hours 24`，记录会话能保持多久。
5. 在手机上试：南京大学 APP 能否识别相册里的二维码截图。这决定只靠手机能否完成扫码登录。

## 要回答的问题

| 问题 | 本机 | 服务器 |
| --- | --- | --- |
| 能否直接访问 ehall 与统一身份认证（reach） | 当前网络配置下均返回 HTTP 200；未单独验证关闭系统代理或 VPN 后的访问 | 待验证 |
| 登录方式：学号密码 / 扫码 | 用户手动登录成功；登录前页面可见密码框与扫码入口，具体使用方式待用户确认 | 待验证 |
| 验证码形式（没有 / 图片 / 滑块 / 短信） | 本次登录前未显示验证码；存在隐藏验证码元素 | 待验证 |
| 服务器 IP 是否触发额外验证 | — | 待验证 |
| 会话能保持多久（watch 的间隔与结果） | 每 0.5 分钟检查一次，约 0.02 小时后仍有效；不是 24 小时结论 | 待验证 |
| 登录后的关键 Cookie（名字、域、有效期） | 已记录不含值的 Cookie 元数据，见下方输出 | 待验证 |
| APP 能否识别相册里的二维码 | 待验证 | — |

## 脚本输出（可公开结论）

2026-09-23，本机执行 `uv run python spikes/ehall_login.py reach --where 本机`，退出码 `0`：

```text
=== 可公开结论（本机，粘贴到 docs/recon/ehall-login.md）===
- ehall.nju.edu.cn/：HTTP 200，落点 ehall.nju.edu.cn/ywtb-portal/official/index.html，47 ms
- authserver.nju.edu.cn/：HTTP 200，落点 authserver.nju.edu.cn/authserver/login，79 ms
都能拿到响应。下一步用 login 验证能否真正登录（校外访问可能返回提示页）。
```

原始记录位于私有数据目录 `state/recon/ehall_reach.jsonl`，命令输出位于
`state/recon/runs/20260923-180801-02-ehall-reach.log`。
本结果仅证明当前网络下能取得 HTTP 响应，不能据此认定已登录、无需 VPN 或服务器可用。

随后执行 `uv run python spikes/ehall_login.py login --no-questions`，用户在浏览器完成登录并确认后，退出码 `0`：

```text
=== 可公开结论（粘贴到 docs/recon/ehall-login.md）===
- 环境：本机；方式：manual；结果：成功，用时 88.1 秒
- 登录页：authserver.nju.edu.cn/authserver/login（从 ehall.nju.edu.cn/ 进入）
- 页面线索：密码框有，验证码无（另有隐藏的验证码框，可能在失败后出现），扫码入口有，记住我无，短信/动态码无
- 登录后的 Cookie（名字@域（有效期），不含值）：_WEU@ehall.nju.edu.cn（会话）；route@authserver.nju.edu.cn（会话）；JSESSIONID@authserver.nju.edu.cn（会话）；CASTGC@authserver.nju.edu.cn（会话）；_WEU@ehallapp.nju.edu.cn（会话）；route@ehall.nju.edu.cn（会话）；amp.locale@ehall.nju.edu.cn（会话）；JSESSIONID@ehall.nju.edu.cn（会话）；org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE@authserver.nju.edu.cn（2027-10-28T18:09+08:00）；MULTIFACTOR_BROWSER_FINGERPRINT@authserver.nju.edu.cn（2027-10-28T18:09+08:00）；MOD_AUTH_CAS@ehall.nju.edu.cn（会话）；asessionid@ehall.nju.edu.cn（会话）；MOD_AUTH_CAS@search.nju.edu.cn（会话）；happyVoyage@authserver.nju.edu.cn（会话）；MOD_AUTH_CAS@njuccs.nju.edu.cn（会话）；MOD_AUTH_CAS@ehallapp.nju.edu.cn（会话）；route@njuccs.nju.edu.cn（会话）；JSESSIONID@njuccs.nju.edu.cn（会话）；_sop_session_@.search.nju.edu.cn（会话）；route@ehallapp.nju.edu.cn（会话）
```

私有原始记录：`state/recon/ehall_login/20260923-180902-observations.json`。
登录态位于 `state/ehall/storage_state.json`，已确认被 Git 忽略；不提交 Cookie 值、截图或登录态。

随后执行 `uv run python spikes/ehall_login.py check --no-save`，退出码 `0`：

```text
会话有效；落点 ehall.nju.edu.cn/ywtb-portal/official/index.html，用时 1.4 秒
提示：依据是有没有被带去登录页。请用 --probe-url 指向必须登录才能看的页面。
```

此检查仍以门户为目标，只能作为初步复用证据。Cookie 标记为“会话”也不能用来推算服务器端登录有效期；下面的短时 `watch` 不等同于完整寿命测试。

随后用已观察到的服务入口做了匿名对照和短时受保护检查。匿名访问被重定向到统一身份认证；带保存会话访问落到 `ehall.nju.edu.cn/portal/html/select_role.html`，退出码 `0`。以 `--interval 0.5 --max-hours 0.02` 运行 `watch`，脚本最后输出：

```text
[2026-09-23T18:37:10+08:00] 有效（已观察 0.0 小时）
[2026-09-23T18:37:42+08:00] 有效（已观察 0.01 小时）
[2026-09-23T18:38:13+08:00] 有效（已观察 0.02 小时）
=== 可公开结论 === 每 0.5 分钟访问一次，0.02 小时后仍有效。
```

这是一次短时、带访问保活的观察，不代表会话能持续 24 小时；服务器目标和完整寿命测试仍缺少条件。私有证据位于 `state/recon/sessions/20260923-183706-protected-session-check/`。

## 部署结论

- 服务器能否正常登录并访问 ehall：待定。不能的话，ehall 能力只在本机部署时启用（PLAN Phase 0 验收标准）。
