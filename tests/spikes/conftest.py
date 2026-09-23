"""spikes 测试的公共夹具：隔离环境变量，数据目录指向临时目录。

测试从不读取 .env；这里再把可能来自开发者 shell 的真实凭据全部清掉，
保证默认测试不会碰到真实的 smail、ehall、推送或模型。
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from fake_site import FakeSite

SENSITIVE_PREFIXES = (
    "SMAIL_",
    "EHALL_",
    "LLM_",
    "FEISHU_",
    "WECOM_",
    "NTFY_",
    "BARK_",
    "PUSH_",
    "ANTHROPIC_",
    "OPENAI_",
)


@pytest.fixture(autouse=True)
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(os.environ):
        if name.startswith(SENSITIVE_PREFIXES) or name == "ASSISTANT_DATA_DIR":
            monkeypatch.delenv(name, raising=False)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    home = tmp_path / "assistant-data"
    home.mkdir()
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(home))
    return home


@pytest.fixture
def fake_site() -> Iterator[FakeSite]:
    site = FakeSite()
    site.start()
    try:
        yield site
    finally:
        site.stop()
