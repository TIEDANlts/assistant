# 架构决策记录（ADR）

每篇 ADR 记录一个决策的背景、决策、后果和放弃的方案。状态流转：提议 → 已接受 → 已废弃或被新 ADR 取代。改动架构边界、数据库结构或风险分级时，同时新增或修改 ADR（见 AGENTS.md）。新 ADR 从 [0000-template.md](0000-template.md) 复制。

| 编号 | 决策 | 状态 | 等待 |
| --- | --- | --- | --- |
| [0001](0001-layers-and-ports.md) | 分层 + 端口/适配器，核心不依赖任何外部系统 | 已接受 | Phase 1 补“只有闸门拿 Writer”的测试 |
| [0002](0002-sqlite-and-markdown.md) | 运行态存 SQLite（WAL），知识与记忆存 Markdown + Git | 已接受 | — |
| [0003](0003-action-gate.md) | 动作闸门：确认绑定内容哈希，执行幂等，结果未知转人工 | 已接受 | 验证 1：邮件核实路径 |
| [0004](0004-reconcile-loop.md) | 对账循环（tick）+ 可恢复工作流 | 已接受 | 验证 1：IDLE 与到达延迟 |
| [0005](0005-pwa-and-notifier.md) | 手机端用 PWA，推送走通知端口 | 提议 | 验证 5：默认通道 |
| [0006](0006-agent-runner.md) | Agent 运行器是端口，默认内置最小工具循环 | 提议 | 验证 6：模型与接入方式 |
| [0007](0007-ehall-catalog-and-engine.md) | ehall 服务目录 + 风险分级 + 通用事务引擎 + 靶场 | 提议 | 验证 2–4：登录、目录、事务分级 |
| [0008](0008-secret-placeholders.md) | 敏感字段以占位符交给模型 | 已接受 | — |
| [0009](0009-single-active-instance.md) | 单活实例，数据目录可迁移 | 提议 | 跨机器租约方案；验证 2：服务器可用性 |
