"""仓库结构的守护测试：分层包齐全、src 不引用 spikes、敏感路径不入库。

import-linter 负责“谁不能 import 谁”；这里补上它管不到的几条。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "assistant"

LAYER_PACKAGES = [
    "core",
    "workflows",
    "agent",
    "memory",
    "adapters",
    "adapters/smail",
    "adapters/ehall",
    "adapters/vault",
    "adapters/llm",
    "adapters/notify",
    "adapters/fakes",
    "ehall",
    "web",
    "runtime",
]


def test_layer_packages_exist() -> None:
    missing = [p for p in LAYER_PACKAGES if not (SRC / p / "__init__.py").is_file()]
    assert missing == [], f"缺少分层包：{missing}"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_src_never_imports_spikes() -> None:
    spike_modules = {p.stem for p in (ROOT / "spikes").glob("*.py")} | {"spikes"}
    offenders = {
        str(path.relative_to(ROOT)): sorted(_imported_modules(path) & spike_modules)
        for path in SRC.rglob("*.py")
        if _imported_modules(path) & spike_modules
    }
    assert offenders == {}, f"src 不得引用 spikes/ 下的一次性脚本：{offenders}"


def test_gitignore_blocks_secrets_and_state() -> None:
    patterns = set((ROOT / ".gitignore").read_text(encoding="utf-8").split())
    required = {".env", "*.har", "storage_state*.json", "state/", "*.sqlite"}
    assert required <= patterns, f".gitignore 缺少：{sorted(required - patterns)}"
    assert "!.env.example" in patterns
