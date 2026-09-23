# Phase 0 侦察

目标：回答 PLAN.md Phase 0 的五个问题。每跑完一个验证，把脚本最后打印的“可公开结论”贴进对应文件，再更新下面的总表和相关 ADR。

本仓库是公开的，这里只放不含个人信息、也不含学校内部接口细节的结论。事务侦察文档（字段、按钮、接口）和服务目录原始数据放在私有的数据仓库 `assistant-data/recon/`，原始抓包放在 `assistant-data/state/`（不入库）。

## 五个问题

| 问题 | 结论 | 依据 | ADR |
| --- | --- | --- | --- |
| 邮箱怎么连 | 待验证 1 | [smail.md](smail.md) | 0003、0004 |
| ehall 怎么登录 | 待验证 2 | [ehall-login.md](ehall-login.md) | 0007 |
| 服务器能不能用 | 待验证 2 | [ehall-login.md](ehall-login.md) | 0009 |
| 手机收不收得到推送 | 待验证 5 | [push.md](push.md) | 0005 |
| 先做哪个事务 | 待验证 3、4 | [risk-tiers.md](risk-tiers.md) | 0007 |

模型选型见 [llm.md](llm.md)（验证 6，ADR 0006）。

## 准备

1. 两个仓库并排放置；在代码仓库根目录 `cp .env.example .env`，按注释填写。
2. `uv sync`，`uv run playwright install chromium`（Linux 服务器加 `--with-deps`）。
3. 每个脚本都支持 `--help`；说明见 [spikes/README.md](../../spikes/README.md)。

## 顺序与命令

| 步骤 | 命令（都以 `uv run python spikes/` 开头） | 结论写到 |
| --- | --- | --- |
| 验证 1 邮箱（只读） | `smail_probe.py` | smail.md |
| 验证 1 发给自己 | `smail_probe.py --send-self` | smail.md、ADR 0003 |
| 验证 2 可达性（本机、服务器各一次） | `ehall_login.py reach --where 服务器` | ehall-login.md |
| 验证 2 登录（本机） | `ehall_login.py login` | ehall-login.md |
| 验证 2 登录（服务器） | `ehall_login.py login --headless --method qr --where 服务器` | ehall-login.md、ADR 0009 |
| 验证 2 会话寿命 | `ehall_login.py check --probe-url <需登录的页面>`，再 `watch` | ehall-login.md |
| 验证 3 服务目录 | `ehall_recon.py --label catalog`，再 `recon_summarize.py candidates` 与 `catalog` | 数据仓库 `recon/catalog-raw.json` |
| 验证 4 事务侦察（6–8 个） | `ehall_recon.py --label <id> --start-url <入口>`，再 `recon_summarize.py service` | 数据仓库 `recon/services/<id>.md`；risk-tiers.md |
| 验证 5 推送 | `push_probe.py --channel all` | push.md |
| 验证 6 模型 | `llm_probe.py --dry-run`，再 `llm_probe.py --runs 3` | llm.md |

## 侦察期间的规则

- 只看不提交。受保护浏览器会拦下“提交、保存、暂存、删除、撤销……”类点击和疑似写请求，但它只是兜底：不要点任何会改变状态的按钮。
- 附件控件不要选文件：有的事务选中就上传，上传本身就是写操作。
- 登录失败不要连续重试；密码登录 24 小时内失败 3 次，脚本就拒绝继续。
- 原始抓包（请求日志、JSON 响应、HAR、截图、登录态）含 Cookie 和个人信息，只在 `state/` 里，永不入库。
- 生成的事务文档已自动脱敏，提交到数据仓库前仍要人工看一遍姓名和选项里的个人信息。
