"""Phase 0 验证脚本的共用工具：读取 .env、定位数据目录、私有文件写入、脱敏、尝试次数限制。

spikes/ 是一次性脚本，只由人手动运行，src 不得引用（tests/unit/test_layers.py 守护）。
原始输出一律写到 $ASSISTANT_DATA_DIR/state/（目录 700、文件 600），不进任何仓库。
"""

import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")
# Playwright 资源类型里的静态资源：侦察时放行，整理时忽略
STATIC_RESOURCE_TYPES = frozenset(
    {"stylesheet", "image", "media", "font", "script", "manifest", "texttrack"}
)


class SpikeError(Exception):
    """给人看的错误：打印说明后以退出码 2 结束，不打印堆栈。"""


# ---------------------------------------------------------------------------- 配置


def load_dotenv(path: Path | None = None) -> None:
    """读取仓库根目录的 .env。已存在的环境变量优先，不覆盖；只在真实运行时调用。"""
    env_path = path or REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env(name: str, default: str = "") -> str:
    """读取环境变量；空字符串视为未设置。"""
    return os.environ.get(name, "").strip() or default


def require_env(name: str, hint: str = "") -> str:
    value = env(name)
    if not value:
        message = f"缺少环境变量 {name}：请在代码仓库根目录的 .env 中填写。"
        raise SpikeError(f"{message}\n{hint}" if hint else message)
    return value


def env_float(name: str) -> float | None:
    raw = env(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise SpikeError(f"{name} 应当是数字，实际是 {raw!r}") from exc


def data_dir() -> Path:
    """数据目录（assistant-data）。必须已存在，且不能位于代码仓库内部。"""
    raw = env("ASSISTANT_DATA_DIR", str(REPO_ROOT.parent / "assistant-data"))
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    path = path.resolve()
    if path == REPO_ROOT or REPO_ROOT in path.parents:
        raise SpikeError(f"数据目录不能放在代码仓库里：{path}")
    if not path.is_dir():
        raise SpikeError(
            f"数据目录不存在：{path}\n"
            "请先创建 assistant-data 仓库，或在 .env 设置 ASSISTANT_DATA_DIR。"
        )
    return path


def state_dir(*parts: str) -> Path:
    """$ASSISTANT_DATA_DIR/state/<parts>，不存在就以 700 权限创建。"""
    path = data_dir().joinpath("state", *parts)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------- 私有文件


def write_private(path: Path, data: str | bytes) -> Path:
    """写文件并把权限设为 600（登录态、抓包、截图都用它）。"""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    path.chmod(0o600)
    return path


def write_json_private(path: Path, obj: Any) -> Path:
    return write_private(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------- 时间


def now() -> datetime:
    return datetime.now(TZ)


def stamp() -> str:
    return now().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------- 脱敏
# 用环视而不是 \b：Python 把汉字算作单词字符，“学号221220001”里的数字前面没有 \b。

_NOT_ALNUM_BEFORE = r"(?<![0-9A-Za-z])"
_NOT_ALNUM_AFTER = r"(?![0-9A-Za-z])"
_COOKIE_NAMES = "jsessionid|mod_auth_cas|castgc|iplanetdirectorypro|_weu|asessionid|route|session"
_EMAIL = r"(?<![0-9A-Za-z._%+-])([0-9A-Za-z])[0-9A-Za-z._%+-]*@([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)+)"


def _mask_token(match: re.Match[str]) -> str:
    text = match.group(0)
    has_digit = any(ch.isdigit() for ch in text)
    looks_hex = re.fullmatch(r"[0-9a-fA-F-]+", text) is not None
    mixed_case = any(ch.islower() for ch in text) and any(ch.isupper() for ch in text)
    return "[令牌]" if has_digit and (looks_hex or mixed_case) else text


_REDACTIONS: list[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    (re.compile(r"(?<![0-9A-Za-z])(ST|TGT|PGT|PT)-\d+-[0-9A-Za-z._-]+"), r"\1-[已脱敏]"),
    (re.compile(rf"(?i)(?<![0-9A-Za-z_])({_COOKIE_NAMES})=[^;\s&]+"), r"\1=[已脱敏]"),
    (re.compile(_EMAIL), r"\1***@\2"),
    (re.compile(_NOT_ALNUM_BEFORE + r"\d{17}[\dXx]" + _NOT_ALNUM_AFTER), "[身份证号]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    (re.compile(_NOT_ALNUM_BEFORE + r"(?:[A-Za-z]{2}\d{7,8}|\d{9})" + _NOT_ALNUM_AFTER), "[学号]"),
    (re.compile(_NOT_ALNUM_BEFORE + r"[0-9A-Za-z+/_-]{32,}={0,2}"), _mask_token),
]


def redact(text: str) -> str:
    """去掉票据、Cookie 值、邮箱本地部分、身份证号、手机号、学号和随机令牌。

    姓名无法可靠识别：生成的文档仍需人工检查一遍再提交。
    """
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def mask_path(path: str) -> str:
    """把 URL 路径里的数字 id 和随机 id 换成占位符，便于把同类接口合并。"""
    parts: list[str] = []
    for segment in path.split("/"):
        if re.fullmatch(r"\d{4,}", segment):
            parts.append("{n}")
        elif re.fullmatch(r"[0-9a-fA-F-]{16,}", segment) and any(c.isdigit() for c in segment):
            parts.append("{id}")
        else:
            parts.append(redact(segment))
    return "/".join(parts)


# ---------------------------------------------------------------------------- 尝试次数


def attempts_path() -> Path:
    return state_dir("recon") / "attempts.jsonl"


def count_attempts(kind: str, window: timedelta, outcomes: Sequence[str] | None = None) -> int:
    since = now() - window
    total = 0
    for record in read_jsonl(attempts_path()):
        if record.get("kind") != kind:
            continue
        if outcomes is not None and record.get("outcome") not in outcomes:
            continue
        if datetime.fromisoformat(str(record["ts"])) >= since:
            total += 1
    return total


def check_attempts(
    kind: str, *, limit: int, window: timedelta, outcomes: Sequence[str] | None = None
) -> None:
    """超过上限就拒绝继续，避免账号因反复登录失败被锁。"""
    used = count_attempts(kind, window, outcomes)
    if used >= limit:
        hours = window.total_seconds() / 3600
        raise SpikeError(
            f"{kind} 在最近 {hours:g} 小时内已尝试 {used} 次（上限 {limit}）。"
            f"为避免账号被锁，请稍后再试；记录见 {attempts_path()}"
        )


def record_attempt(kind: str, outcome: str, detail: str = "") -> None:
    record = {
        "ts": now().isoformat(timespec="seconds"),
        "kind": kind,
        "outcome": outcome,
        "detail": detail,
    }
    append_jsonl(attempts_path(), record)


# ---------------------------------------------------------------------------- 交互


def confirm(question: str, *, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    answer = input(f"{question} [y/N] ").strip().lower()
    return answer in {"y", "yes", "是"}


def choose(question: str, options: Sequence[str]) -> str:
    """在终端里让人选一项；非交互环境返回“未回答”。"""
    if not sys.stdin.isatty():
        return "未回答"
    print(question)
    for index, option in enumerate(options, 1):
        print(f"  {index}. {option}")
    while True:
        raw = input("选择序号（直接回车跳过）：").strip()
        if not raw:
            return "未回答"
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print("请输入列表里的序号。")


def run(main: Callable[[list[str] | None], int]) -> None:
    """脚本入口：读取 .env，把 SpikeError 变成友好的提示和退出码。"""
    load_dotenv()
    try:
        code = main(None)
    except SpikeError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        code = 130
    raise SystemExit(code)
