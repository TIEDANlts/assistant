"""pre-commit 钩子：按路径拒绝提交密钥、登录态、抓包和运行态数据。

gitleaks 按内容找密钥；本钩子兜底 gitleaks 不一定认得的东西：Playwright 登录态、
HAR 抓包（含 Cookie 与请求体）、数据库和 state/ 下的运行态数据。

用法：python scripts/forbid_sensitive_files.py <路径> ...；有违规时退出码为 1。
"""

import re
import sys
from collections.abc import Callable
from pathlib import PurePosixPath

ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template"}

Rule = tuple[str, Callable[[PurePosixPath], bool]]


def _name_matches(pattern: str) -> Callable[[PurePosixPath], bool]:
    regex = re.compile(pattern, re.IGNORECASE)
    return lambda path: regex.search(path.name) is not None


def _is_env_file(path: PurePosixPath) -> bool:
    name = path.name
    return (name == ".env" or name.startswith(".env.")) and name not in ENV_EXAMPLES


def _in_state_dir(path: PurePosixPath) -> bool:
    return len(path.parts) > 1 and path.parts[0] == "state"


RULES: list[Rule] = [
    ("环境变量文件（内含密钥）", _is_env_file),
    ("Playwright 登录态（含 Cookie）", _name_matches(r"^storage[_-]?state.*\.json$")),
    ("HAR 抓包（含 Cookie 与请求体）", _name_matches(r"\.har$")),
    ("Cookie 导出文件", _name_matches(r"^cookies?\.(txt|json|sqlite)$|\.cookies$")),
    ("私钥或证书", _name_matches(r"\.(pem|key|p12|pfx)$|^id_(rsa|dsa|ecdsa|ed25519)$")),
    ("数据库文件（运行态数据）", _name_matches(r"\.(sqlite3?|db)$")),
    ("Playwright 轨迹（含截图与请求）", _name_matches(r"^trace.*\.zip$")),
    ("state/ 目录（运行态数据）", _in_state_dir),
]


def violations(raw_paths: list[str]) -> list[tuple[str, str]]:
    """返回 (规范化路径, 原因)；同一路径只报一次。"""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in raw_paths:
        normalized = raw.replace("\\", "/")
        if normalized in seen:
            continue
        seen.add(normalized)
        path = PurePosixPath(normalized)
        for reason, matches in RULES:
            if matches(path):
                found.append((normalized, reason))
                break
    return found


def main(argv: list[str]) -> int:
    found = violations(argv)
    for path, reason in found:
        print(f"✗ {path} —— {reason}")
    if found:
        print(
            f"共 {len(found)} 个文件被拦下：密钥放环境变量，登录态与运行态数据放数据目录的 state/。"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
