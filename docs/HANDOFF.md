# Agent 交接：Phase 0 实测与 SPEC 定稿

更新日期：2026-09-23。本文交接已完成的改动、真实验证结果和继续实现的边界；不代表 Phase 0 已通过验收。

## 1. 从哪里开始

- 代码仓库：[TIEDANlts/assistant](https://github.com/TIEDANlts/assistant)，公开；数据仓库：[TIEDANlts/assistant-data](https://github.com/TIEDANlts/assistant-data)，私有。两个目录并排放置。
- 上一轮已推送基线：代码 `38be7e4`，数据 `a491e41`，均在 `main`。
- 提交身份已按用户要求设为 `TIEDANlts <147322216+TIEDANlts@users.noreply.github.com>`，原有 Claude 署名已改写；改写前备份在两仓库之外的 `../.git-backups/20260923-before-publish/`。
- 先读 [AGENTS.md](../AGENTS.md)、[SPEC.md](SPEC.md)、[侦察总表](recon/README.md)，需要阶段细节再读 [PLAN.md](PLAN.md)。SPEC 正通过 Grill Me 定稿；已确认首版按课程必需范围验收，扩展项放后续，设备、场景等答案尚未齐备。
- `src/assistant/` 目前主要是各层的 `__init__.py` 骨架。CLI、tick、动作闸门、真实适配器和手机 Web 界面尚未实现；AGENTS 中的运行命令是后续目标，不能当作已有功能。

## 2. 实际修改了什么

| 仓库 / 提交 | 改动 | 影响 |
| --- | --- | --- |
| assistant / `fa07c53` | 新增并提交 `uv.lock` | 锁定依赖 |
| assistant / `b549b8d` | 格式化 `tests/spikes/test_spike_common.py` 的字符串 | 单独的 style 提交 |
| assistant / `0068e07` | `spikes/ehall_login.py` 的 `PASSWORD_KIND` 改名为 `LOGIN_ATTEMPT_KIND`，同步测试引用 | 消除 Ruff S105 误报；值仍为 `ehall-password`，登录尝试计数与限速逻辑不变 |
| assistant / `38be7e4` | 更新 `docs/recon/` 六项结论、ADR 0003–0007 和 0009、开发日志 | 记录成功证据、失败原因和未验证范围 |
| assistant-data / `a491e41` | 新增 `recon/catalog-raw.json`、六份 `recon/services/*.md`，更新 `recon/README.md` | 归档目录和经人工脱敏的入口侦察；内部接口留在私有仓库 |

没有修改业务架构或开始 Phase 1。没有为通过检查关闭 Ruff 规则，也没有扩宽真实学校接口的写权限。

## 3. 检查结果与尚未修复的问题

- Ruff、Pyright、import-linter 已通过；代码修复提交的 [GitHub CI](https://github.com/TIEDANlts/assistant/actions/runs/35846949322) 通过。侦察文档提交前，两仓库的 `pre-commit run --all-files` 均通过。
- Windows 本机 pytest：**102 通过、1 失败、1 跳过**。失败位于 `tests/spikes/test_ehall_login.py` 的登录态权限断言：要求 POSIX `0600`，Windows 返回 `0666`。本次没有调整测试或权限实现。
- 本机运行 Python 工具需注意中文编码：`$env:PYTHONUTF8='1'`。默认 pytest 临时目录曾不可访问，可用新的 `--basetemp` 目录；不要复用可能含其他任务数据的目录。
- `scripts/verify_secret_guard.sh` 有验收误判：把钩子的任意非零退出都视作“成功拦截”，工具启动失败也会报 PASS。不能仅凭该脚本的成功输出认定密钥护栏验收通过；此问题已反馈，尚未修复。
- 交接时唯一原有未提交改动：`scripts/verify_secret_guard.sh` 的文件模式由 `100755` 变为 `100644`。本轮没有暂存或恢复它，后续 Agent 不要顺手覆盖。

## 4. 验证 1–6 的真实进度

| 验证 | 已做 | 未完成 / 阻断 |
| --- | --- | --- |
| 1 smail | 运行只读命令与 `--send-self`；配置出现后的两次运行都进入了 IMAP 认证 | 服务器返回通用认证失败，已停止重试；未发邮件，未验证历史邮件、已发送保存、Message-ID 或延迟 |
| 2 ehall 登录 | 本机 reach 返回 HTTP 200；用户手动登录并保存会话；匿名对照与受保护入口复用成功 | watch 仅每 0.5 分钟检查、三次共约一分钟，脚本显示 0.02 小时；未证明完整寿命、无 VPN 可用或服务器可用；手机相册扫码未知 |
| 3 服务目录 | 导出 135 条当前可见在线应用；补读一项官方指南 | 不等于全校全部事项或全部办理权限；完整指南目录与应用映射未完成 |
| 4 事务侦察 | 六个入口页和部分指南已记录，人工去除姓名、个人课程号 | 没有取得完整申请表、上传/暂存行为、撤回规则或实际提交回执；尚未选出首个 D 级事务 |
| 5 手机推送 | 运行 `push_probe.py --channel all` | 四个通道均未配置，退出码 2；没有发送测试消息或手机到达证据 |
| 6 模型 | `llm_probe.py --dry-run` 成功 | `--runs 3` 因缺少 `LLM_MODEL` 在调用前退出；模型、费用与输出质量未验证 |

六项事务当前分级：

- Q：研究生总课表、成绩查询，仅限读取与查询，不包括保存查询方案或切换角色。
- F：无犯罪证明。官方指南明确院系/部门初审、保卫处复审；分级不代表已可自动提交。
- X：空闲教室（仅到身份选择页）、证明书申请、居住证办理，尚不开放执行。
- “证明书申请”的关联说明是毕业/学位证件遗失补证，不能误认为通用在读证明。

## 5. 数据位置与操作边界

- `.env`、登录态、JSON 响应、截图和完整命令输出均已排除在 Git 之外。另一个 Agent 换机器后不会从 Git 自动获得这些文件，也不能假设登录态仍有效。
- 可提交的内部侦察文档：`../assistant-data/recon/`；原始证据：`../assistant-data/state/recon/`；会话：`../assistant-data/state/ehall/storage_state.json`。
- 公开仓库只存脱敏结论和通用实现；不要把学校内部接口、个人资料、Cookie 或密钥写进交接消息、公开测试或代码。
- 用户明确要求：侦察时不要点任何改变业务状态的按钮，附件控件不要选文件。护栏只是兜底，不是试点危险按钮的理由。
- 本轮没有点击申请、发送验证码、保存、导出等业务按钮，没有选附件。页面仍可能自动发出使用记录、角色上下文等请求，不能声称网页后台完全零写入。
- 生成器不会可靠识别人名，且会把部分长路径替换为 `[令牌]`；生成文档要人工复核，不能直接用被替换的路径构造请求。

## 6. 继续推进的顺序

1. 继续 Grill Me 定稿 SPEC：用户已选择“课程必需优先，扩展放后续”。首版保留五项基础能力和一条真实组合流程；iOS/安卓双端验收、本机↔服务器迁移、D/F 两类自动提交作为扩展。接下来确认实际设备/部署、模型预算与数据边界、邮件分拣、反复使用的 ehall 场景和完整流程。
2. 明确处理 Windows 权限测试与 secret guard 验收误判这两个已知问题；不要绕过它们后声称检查全部通过。
3. 核对邮箱客户端服务和专用密码；提供推送、模型及服务器/手机条件后，只补跑对应未完成步骤，避免重做已归档的侦察。
4. Phase 1 目标仍是**用假适配器跑通“来信 → 草稿 → 确认 → 发送 → 归档”**。按 AGENTS 先给计划、接口草案和测试清单；尤其验证重复 tick 不重复、确认后编辑使旧确认失效、发送后崩溃不会重发。
5. 是否允许真实验证未全部完成时先推进假适配器阶段，由本轮 SPEC 的范围决策明确；当前不能自行把 Phase 0 标为完成。

给接手 Agent 的首条指令可直接使用：

> 阅读 AGENTS.md、docs/HANDOFF.md 和 docs/SPEC.md，以已确认的 SPEC 为准继续工作。先说明尚未实现的接口和本阶段验收测试，不要重做已有侦察，不要把入口观察当成完整事务验证，不要提交敏感数据。当前真实系统验证存在阻断，是否推进 Phase 1 依 SPEC 的阶段入口条件决定。
