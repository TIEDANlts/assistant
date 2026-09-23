# 验证 6：模型与结构化输出

状态：2026-09-23 已完成无网络 dry-run；真实三次调用因缺少模型名在连接前退出。结论同步到 ADR 0006。

先 `uv run python spikes/llm_probe.py --dry-run` 看清楚会发出去什么（一份虚构通知，不含个人信息），再 `--runs 3` 真实调用。

| 问题 | 结论 |
| --- | --- |
| 提供方与模型 | `.env` 默认提供方为 anthropic；模型名未配置 |
| 接入方式（官方 SDK 强制工具调用 / OpenAI 兼容 JSON 模式） | dry-run 展示了两种实现的共同 schema；真实接入未验证 |
| 结构化输出一次通过率 | 未调用模型 |
| 能否区分报名截止与活动开始、正确处理“24:00” | dry-run 只检查提示词和 schema，不能证明模型行为 |
| 单次耗时 | 未调用模型 |
| 单次费用（按 .env 单价） | 未调用模型 |
| 能否把脱敏后的邮件片段发给该模型（境内外、合规） | 待你决定（SPEC 待定问题） |

## 脚本输出（可公开结论）

`uv run python spikes/llm_probe.py --dry-run` 返回退出码 `0`，只打印虚构通知、系统提示词和 Pydantic JSON Schema，不调用模型。

`uv run python spikes/llm_probe.py --runs 3` 返回退出码 `2`：

```text
✗ 缺少环境变量 LLM_MODEL：请在代码仓库根目录的 .env 中填写。
```

因此没有产生 token、耗时或费用数据。私有日志位于 `state/recon/runs/20260923-183747-06-llm-dry-run.log` 和 `state/recon/runs/20260923-183748-06-llm-runs3.log`。
