# assistant：会成长的个人助手

《生成式软件工程》Lab 1。连接 smail、ehall、个人资料库和手机端；能力在使用中通过纠正、技能和事务 Spec 持续成长，全部以可读文件保存在 Git 里。

路线见 [docs/PLAN.md](docs/PLAN.md)，验收标准见 [docs/SPEC.md](docs/SPEC.md)，决策见 [docs/adr/](docs/adr/)，进度见 [docs/journal.md](docs/journal.md)。给编码 Agent 的约定在 [AGENTS.md](AGENTS.md)。

当前阶段：**Phase 0 准备与侦察**。还没有业务代码；`spikes/` 里是验证外部系统行为的一次性脚本，只能由人手动运行。

## 两个仓库

| 仓库 | 内容 | 注意 |
| --- | --- | --- |
| `assistant`（本仓库，公开） | 代码、文档、测试 | 不得出现个人数据、密钥、登录态和学校系统的侦察细节 |
| `assistant-data`（私有） | `vault/` 资料库、`memory/` 规则与技能、`recon/` 侦察文档、`config.toml` | 其中 `state/`（数据库、登录态、日志、抓包）永不入库 |

两个仓库建议并排放置，`.env` 里的 `ASSISTANT_DATA_DIR` 默认指向 `../assistant-data`。

## 开始

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync                               # 首次会生成 uv.lock，请提交它
uv run playwright install chromium    # Linux 服务器加 --with-deps
uv run pre-commit install
cp .env.example .env                  # 按注释填写；.env 永不入库
```

## 检查

```bash
uv run pytest -q                      # 默认跳过 live 与 eval 标记的测试
uv run ruff check . && uv run pyright && uv run lint-imports
uv run pre-commit run --all-files
bash scripts/verify_secret_guard.sh   # 验收：假密钥、.env、登录态、HAR 都会被拦下
```

## Phase 0 侦察

按 [docs/recon/README.md](docs/recon/README.md) 的顺序运行 `spikes/` 下的脚本。它们需要真实账号、手机和服务器，原始输出只写到数据目录的 `state/`，脱敏后的结论才写进 `docs/recon/`。

## 国内网络

- PyPI 下载慢：`export UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`
- gitleaks 钩子构建失败：`export GOPROXY=https://goproxy.cn,direct` 后重试；或把 `.pre-commit-config.yaml` 里的钩子 id 改为 `gitleaks-system`，自行安装 gitleaks 可执行文件
- Playwright 浏览器下载慢：设置环境变量 `PLAYWRIGHT_DOWNLOAD_HOST` 指向可用的镜像
