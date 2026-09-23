# spikes：Phase 0 的一次性验证脚本

这些脚本回答“外部系统到底怎么表现”，只由人手动运行；`src/` 永远不引用它们（`tests/unit/test_layers.py` 守护）。运行顺序、要记录什么、结论写到哪里，见 [docs/recon/README.md](../docs/recon/README.md)。

| 脚本 | 验证 | 会不会写外部系统 |
| --- | --- | --- |
| `smail_probe.py` | 1 邮箱 | 默认只读（EXAMINE + BODY.PEEK）；`--send-self` 在你确认后给自己发一封信 |
| `ehall_login.py` | 2 登录、会话、可达性 | 只登录，不办理任何事务 |
| `ehall_recon.py` | 3–4 服务目录与事务侦察 | 只读浏览；危险点击与疑似写请求被拦截 |
| `recon_summarize.py` | 3–4 整理 | 不访问网络；输出到数据仓库 `recon/` |
| `push_probe.py` | 5 推送 | 给你配置的通道各发一条测试消息 |
| `llm_probe.py` | 6 模型 | 调用模型接口；`--dry-run` 不调用 |

共同约定：

- 在代码仓库根目录运行，例如 `uv run python spikes/smail_probe.py --help`。
- 配置读 `.env`（测试从不读取它）；数据目录由 `ASSISTANT_DATA_DIR` 指定，必须在代码仓库之外。
- 原始输出写到 `$ASSISTANT_DATA_DIR/state/`，目录权限 700、文件 600，不入库。
- 屏幕最后打印的“可公开结论”不含个人信息，可以直接贴进 `docs/recon/`。
- 会触发登录的脚本有次数上限（记录在 `state/recon/attempts.jsonl`），失败后不要连续重试。
