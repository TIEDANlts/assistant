# 开发日志

## 2026-09-23 Phase 0：准备与侦察工具

### 做了什么

- 审查 PLAN.md 与 AGENTS.md，修正写进方案和 ADR（见下一节）。
- 两个仓库：`assistant`（代码，公开）与 `assistant-data`（数据，私有）。
- 工具链：uv 项目、ruff、pyright（`src/assistant` 严格模式）、pytest（默认排除 live 与 eval）、import-linter 三条分层契约、pre-commit（gitleaks + 本项目的密钥规则 + 按路径拦截敏感文件）、GitHub Actions。
- ADR 0001–0009 骨架与索引；`docs/SPEC.md` 草案 v0，附 Grill Me 待定问题。
- `spikes/` 六个验证脚本，配本地假站点的测试；`docs/recon/` 侦察指南与结论模板。

### 方案审查发现的问题与处理

1. 两套等级缺少对应关系：映射表写进 PLAN「核心模型」、ADR 0007 和 AGENTS 第 6 条，代码里的唯一来源是 Phase 1 的 `core/policy.py`。
2. 邮件崩溃恢复依赖未验证的腾讯企业邮行为：验证 1 增加三项检查（`smail_probe.py --send-self`），ADR 0003 写明退路。
3. 单活锁挡不住两台机器同时运行：ADR 0009 提议跨机器租约，Phase 1 定方案。
4. 验证 2 先测可达性再测登录，并补上“APP 能否识别相册里的二维码”：`ehall_login.py reach` 与登录后的观察问题。
5. 附件“选择即上传”和自动暂存都是提交前的写操作：验证 4 逐个记录，PLAN Phase 5 增加“上传只在确认后进行”。
6. 代码仓库公开：事务侦察文档、服务目录原始数据和 backlog 改放数据仓库；Spec 与靶场是否公开列为 SPEC 待定问题 12。
7. 建议（未改方案）：Phase 2 最重，时间紧时服务器部署可以挪到 Phase 7。

### 验证情况

开发环境是离线沙箱，装不了依赖，所以 `uv sync`、ruff、pyright、import-linter、pre-commit 和 gitleaks 都没有实际运行，也没有生成 `uv.lock`。替代做法：

- 用与 pytest 兼容的最小运行器跑测试：96 个通过，其中 3 个浏览器测试用本地 Chromium 真实运行（假统一身份认证的密码登录与会话过期、侦察护栏）。
- `tests/spikes/test_llm_probe.py` 的 8 个测试因沙箱没有 pydantic 没能运行。
- 用近似 ruff 规则的脚本检查（行宽按中文双宽计、import 顺序、未用 import 等），0 个问题；ruff format 没有运行。
- gitleaks 自定义规则用正则逐条验证过正反例，也确认不会误报仓库自己的文件。

需要在本机补做：`uv sync` 并提交 `uv.lock` → `uv run pytest -q` → `uv run ruff check .`、`uv run pyright`、`uv run lint-imports` → `uv run pre-commit run --all-files`（格式化带来的改动单独提交为 `style:`）→ `bash scripts/verify_secret_guard.sh`。

### 依赖清单

| 依赖 | 分组 | 理由 |
| --- | --- | --- |
| pydantic | 运行 | 配置、LLM 结构化输出、事务 Spec 校验（PLAN 技术栈） |
| tzdata（仅 Windows） | 运行 | Windows 没有系统时区库，zoneinfo 需要它 |
| pytest、ruff、pyright[nodejs]、import-linter、pre-commit | dev | 质量工具；pyright 带 nodejs 附加项，不依赖系统 Node |
| playwright | spikes | 验证 2–4；Phase 4 起转为运行依赖 |
| anthropic、openai | spikes | 验证 6 对比两种接入；Phase 1–2 只保留选定的一个 |

### 待你完成（需要真实账号、设备或判断）

- 在本机补做上面的检查。
- 按 `docs/recon/README.md` 跑验证 1–6，把结论贴进 `docs/recon/`，更新 ADR 状态。
- 侦察 6–8 个事务并分级，选出第一个全自动事务。
- Grill Me：回答 SPEC.md 最后一节的 12 个问题，定稿 SPEC。

### 下一步

Phase 0 验收通过后进入 Phase 1：按 AGENTS.md 先给计划、接口草案和测试清单，确认后再写代码。

## 2026-09-23 Phase 0：实际环境验证结果

- 将 `PASSWORD_KIND` 改为 `LOGIN_ATTEMPT_KIND`，保留标签值和尝试次数逻辑；修复已提交为 `0068e07`。Ruff、Pyright、import-linter、pre-commit 和 GitHub CI 已通过。
- 本机 pytest：102 通过、1 失败、1 跳过；失败是 Windows 文件权限与测试要求 POSIX `0600` 不一致。另发现 secret guard 验收脚本会把钩子执行错误当作成功拦截；这两项已有问题本次未修改，不能据脚本退出码声称全部验收通过。
- 验证 1：首次因缺少配置退出；配置出现后，只读和发给自己均在 IMAP 认证阶段失败，未发送邮件。服务器返回通用错误，具体原因待核对。
- 验证 2：本机 reach 为 HTTP 200，用户完成手动登录；匿名访问服务入口进入统一认证，保存会话短测约 0.02 小时仍有效。服务器、完整寿命和手机相册扫码仍未验证。
- 验证 3：私有数据仓库导出 135 条当前可见在线应用；另补读一项官方指南，完整指南目录与应用映射尚未补齐。
- 验证 4：六个事务入口完成只读侦察。没有点击申请、发送验证码、保存、导出等业务按钮，也没有选择附件；两个查询范围暂定 Q，无犯罪证明据指南定为 F，其余三项按 X 处理。没有选定首个 D 级事务。
- 验证 5：四个推送通道均未配置，脚本退出码 2。
- 验证 6：dry-run 成功；三次真实模型调用因缺少 `LLM_MODEL` 在连接前退出，退出码 2。
- 具体脚本输出和限制见 `docs/recon/`，相关 ADR 已同步；原始响应、截图、登录态和日志仅存于数据仓库 `state/`，不入库。Phase 0 尚未验收通过。
