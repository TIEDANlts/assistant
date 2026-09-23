"""pre-commit 钩子 scripts/forbid_sensitive_files.py 的黑盒测试。

gitleaks 按内容找密钥；这个钩子按路径兜底 Cookie、登录态、抓包和运行态数据。
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "forbid_sensitive_files.py"


def run_hook(*paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK), *paths],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "deploy/.env.production",
        "state/ehall/storage_state.json",
        "spikes/out/storage-state.json",
        "recon/session.har",
        "notes/Cookies.txt",
        "certs/server.pem",
        "id_ed25519",
        "state/assistant.sqlite",
        "backup/index.sqlite3",
        "debug/trace-login.zip",
        "tests\\fixtures\\recon.har",
    ],
)
def test_blocks_sensitive_paths(path: str) -> None:
    result = run_hook(path)
    assert result.returncode == 1
    assert path.replace("\\", "/") in result.stdout


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        "src/assistant/web/cookies.py",
        "docs/recon/services/example.md",
        "tests/fixtures/mail/sample.eml",
        "spikes/ehall_login.py",
        "docs/state-machine.md",
    ],
)
def test_allows_ordinary_paths(path: str) -> None:
    result = run_hook(path)
    assert result.returncode == 0, result.stdout


def test_reports_every_offender_once() -> None:
    result = run_hook("README.md", ".env", "a/b.har", ".env")
    assert result.returncode == 1
    assert result.stdout.count(".env") == 1
    assert "a/b.har" in result.stdout
    assert "README.md" not in result.stdout
