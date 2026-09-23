# 验证 1：smail（腾讯企业邮）

状态：2026-09-23 已运行只读和 `--send-self` 验证；两次都在 IMAP 认证阶段失败，未发送测试邮件。结论同步到 ADR 0003 与 0004。

## 要回答的问题

| 问题 | 结论 | 影响 |
| --- | --- | --- |
| 能否用客户端专用密码登录 IMAP / SMTP | 待验证 | 适配器配置 |
| “收取全部邮件”是否生效（有没有早于 31 天的邮件） | 待验证 | 能否关联历史往来 |
| 服务器能力：IDLE、UIDPLUS、MOVE、SPECIAL-USE | 待验证 | 轮询还是 IDLE；APPEND 后能否拿到 UID |
| “已发送”文件夹的名字与识别方式 | 待验证 | find_sent、append_sent |
| SMTP 发出的信会不会自动出现在“已发送”，延迟多久 | 待验证 | ADR 0003 的崩溃恢复路径 |
| Message-ID 是否原样保留 | 待验证 | 去重键与 find_sent |
| `SEARCH HEADER Message-ID` 是否可用 | 待验证 | find_sent 的实现方式 |
| 发给自己的信多久进收件箱 | 待验证 | tick 间隔与推送时延 |

## 脚本输出（可公开结论）

命令 `uv run python spikes/smail_probe.py` 返回退出码 `2`：

```text
✗ IMAP 登录失败：b'Login fail. Account is abnormal, service is not open, password is incorrect, login frequency limited, or system is busy.'
请确认：1) 用的是客户端专用密码，不是网页登录密码；2) 网页版“设置 → 客户端设置”已开启 IMAP/SMTP；3) 账户名是完整邮箱地址。
```

`uv run python spikes/smail_probe.py --send-self` 得到同一认证错误并返回退出码 `2`，所以没有进入 SMTP 发送阶段。邮箱地址和密码值不写入本文件。
原始日志位于私有数据目录 `state/recon/runs/20260923-1840-01-smail-readonly.log` 和 `state/recon/runs/20260923-1839-01-smail-send-self.log`。

首次只读运行因配置未填写在连接前退出，记录在 `state/recon/runs/20260923-180741-01-smail.log`；以上为随后配置出现后的结果。服务器提示不能区分服务开关、凭据、账号限制或临时故障，已停止继续尝试，待核对后重跑。这些提前退出的运行均未产生脚本末尾的“可公开结论”，此处保留实际错误输出。

## 人工确认

- [ ] 网页版“设置 → 客户端设置”已开启 IMAP/SMTP，收取范围为“全部”
- [ ] 客户端专用密码只保存在 `.env`；长期没有客户端登录时腾讯会关闭客户端服务，需要重新开启
- [ ] 测试邮件已删除（可选）
