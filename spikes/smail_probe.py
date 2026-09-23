"""验证 1：smail（腾讯企业邮）能否用 IMAP/SMTP 连接，以及 find_sent 依赖的几项行为。

只读（默认）：
    uv run python spikes/smail_probe.py
    登录 IMAP，列出服务器能力、文件夹、收件箱最新几封的信头（EXAMINE + BODY.PEEK，
    不改变已读状态），并统计早于 31 天的邮件数，判断“收取全部邮件”是否生效。

发一封给自己（需确认）：
    uv run python spikes/smail_probe.py --send-self
    再轮询“已发送”和收件箱，回答 ADR 0003 的待验证项：SMTP 发出的信会不会自动出现在
    “已发送”、延迟多久、Message-ID 是否原样保留、SEARCH HEADER 能否按 Message-ID 找到它。

明细（含主题）只写到 state/recon/smail/；屏幕最后打印不含个人信息的“可公开结论”。
"""

import argparse
import base64
import contextlib
import imaplib
import re
import secrets
import smtplib
import ssl
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.policy import default as default_policy
from email.utils import formatdate
from typing import Any, Protocol, Self

import spike_common
from spike_common import SpikeError

HEADER_FIELDS = (
    "(UID RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE FROM SUBJECT)])"
)
INTERESTING_CAPABILITIES = ("IDLE", "UIDPLUS", "MOVE", "SPECIAL-USE", "CONDSTORE", "ID", "XLIST")
SPECIAL_FLAGS = {"sent": "\\Sent", "drafts": "\\Drafts", "trash": "\\Trash", "junk": "\\Junk"}
FALLBACK_NAMES = {
    "sent": ("Sent Messages", "Sent", "Sent Items", "已发送"),
    "drafts": ("Drafts", "草稿箱"),
    "trash": ("Deleted Messages", "Trash", "已删除"),
    "junk": ("Junk", "Spam", "垃圾邮件", "垃圾箱"),
}
ROLE_NAMES = {"sent": "已发送", "drafts": "草稿", "trash": "已删除", "junk": "垃圾邮件"}
LOGIN_HINT = (
    "请确认：1) 用的是客户端专用密码，不是网页登录密码；"
    "2) 网页版“设置 → 客户端设置”已开启 IMAP/SMTP；3) 账户名是完整邮箱地址。"
)


class ImapClient(Protocol):
    """本脚本用到的 imaplib.IMAP4 子集（测试里用假实现）。"""

    def capability(self) -> tuple[str, Any]: ...

    def status(self, mailbox: str, names: str) -> tuple[str, Any]: ...

    def select(self, mailbox: str = ..., readonly: bool = ...) -> tuple[str, Any]: ...

    def uid(self, command: str, *args: str) -> tuple[str, Any]: ...

    def list(self, directory: str = ..., pattern: str = ...) -> tuple[str, Any]: ...


@dataclass(frozen=True)
class MailConfig:
    address: str
    password: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int

    @classmethod
    def from_env(cls) -> Self:
        return cls(
            address=spike_common.require_env("SMAIL_ADDRESS"),
            password=spike_common.require_env("SMAIL_PASSWORD", LOGIN_HINT),
            imap_host=spike_common.env("SMAIL_IMAP_HOST", "imap.exmail.qq.com"),
            imap_port=int(spike_common.env("SMAIL_IMAP_PORT", "993")),
            smtp_host=spike_common.env("SMAIL_SMTP_HOST", "smtp.exmail.qq.com"),
            smtp_port=int(spike_common.env("SMAIL_SMTP_PORT", "465")),
        )


@dataclass(frozen=True)
class Folder:
    raw: str  # 服务器上的名字（修改版 UTF-7），SELECT 时要用它
    name: str  # 解码后的名字
    flags: tuple[str, ...]


@dataclass(frozen=True)
class MessageSummary:
    uid: int
    internal_date: str
    size: int
    message_id: str
    date: str
    sender: str
    subject: str


# ---------------------------------------------------------------------------- 协议解析


def decode_imap_utf7(name: str) -> str:
    """IMAP 修改版 UTF-7（RFC 3501 §5.1.3）解码，例如 &XfJT0ZAB- → 已发送。"""
    out: list[str] = []
    index = 0
    while index < len(name):
        if name[index] != "&":
            out.append(name[index])
            index += 1
            continue
        end = name.find("-", index)
        if end == -1:
            out.append(name[index:])
            break
        chunk = name[index + 1 : end]
        if not chunk:
            out.append("&")
        else:
            padded = chunk.replace(",", "/") + "=" * (-len(chunk) % 4)
            try:
                out.append(base64.b64decode(padded).decode("utf-16-be"))
            except ValueError:
                out.append(name[index : end + 1])
        index = end + 1
    return "".join(out)


def quote_imap(text: str) -> str:
    """IMAP 引号字符串。Python 3 的 imaplib 不会自动加引号，含空格的文件夹名必须手动引。"""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _as_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return text


_LIST_LINE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$')


def parse_list_response(data: Sequence[object]) -> list[Folder]:
    """解析 LIST 响应；名字可能是原子、引号串，或以字面量（元组）返回。"""
    folders: list[Folder] = []
    for item in data:
        literal: str | None = None
        if isinstance(item, tuple) and len(item) >= 2:
            line, literal = _as_text(item[0]), _as_text(item[1])
        elif isinstance(item, bytes | str):
            line = _as_text(item)
        else:
            continue
        match = _LIST_LINE.match(line)
        if match is None:
            continue
        raw = literal if literal is not None else _unquote(match.group("name"))
        flags = tuple(match.group("flags").split())
        folders.append(Folder(raw=raw, name=decode_imap_utf7(raw), flags=flags))
    return folders


def guess_special_folders(folders: Sequence[Folder]) -> dict[str, tuple[Folder, str]]:
    """按 SPECIAL-USE 标记找特殊文件夹，找不到再按常见名字找。返回 角色 → (文件夹, 依据)。"""
    found: dict[str, tuple[Folder, str]] = {}
    for role, flag in SPECIAL_FLAGS.items():
        flagged = [f for f in folders if flag.lower() in {x.lower() for x in f.flags}]
        if flagged:
            found[role] = (flagged[0], "标记")
            continue
        names = {n.lower() for n in FALLBACK_NAMES[role]}
        named = [f for f in folders if f.name.lower() in names]
        if named:
            found[role] = (named[0], "名字")
    return found


_STATUS_ITEM = re.compile(r"(MESSAGES|UIDNEXT|UIDVALIDITY|UNSEEN)\s+(\d+)")


def parse_status(data: Sequence[object]) -> dict[str, int]:
    text = " ".join(_as_text(item) for item in data if item is not None)
    return {key: int(value) for key, value in _STATUS_ITEM.findall(text)}


def parse_uids(data: Sequence[object]) -> list[int]:
    text = " ".join(_as_text(item) for item in data if item is not None)
    return [int(token) for token in text.split() if token.isdigit()]


_FETCH_UID = re.compile(rb"UID (\d+)")
_FETCH_SIZE = re.compile(rb"RFC822\.SIZE (\d+)")
_FETCH_DATE = re.compile(rb'INTERNALDATE "([^"]+)"')


def parse_fetch_response(data: Sequence[object]) -> list[MessageSummary]:
    """解析 UID FETCH 的信头响应。各数据项的顺序由服务器决定，可能出现在字面量之后。"""
    summaries: list[MessageSummary] = []
    for index, item in enumerate(data):
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        meta, body = item[0], item[1]
        if not isinstance(meta, bytes) or not isinstance(body, bytes):
            continue
        following = data[index + 1] if index + 1 < len(data) else b""
        if isinstance(following, bytes):
            meta += following
        uid = _FETCH_UID.search(meta)
        size = _FETCH_SIZE.search(meta)
        internal = _FETCH_DATE.search(meta)
        headers = BytesHeaderParser(policy=default_policy).parsebytes(body)
        summaries.append(
            MessageSummary(
                uid=int(uid.group(1)) if uid else 0,
                internal_date=internal.group(1).decode() if internal else "",
                size=int(size.group(1)) if size else 0,
                message_id=str(headers.get("Message-ID", "")).strip(),
                date=str(headers.get("Date", "")),
                sender=str(headers.get("From", "")),
                subject=str(headers.get("Subject", "")),
            )
        )
    return summaries


def parse_internaldate(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


def imap_date(moment: datetime) -> str:
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{moment.day:02d}-{months[moment.month - 1]}-{moment.year}"


def make_probe_message(address: str, token: str) -> EmailMessage:
    domain = address.rsplit("@", 1)[-1]
    message = EmailMessage()
    message["From"] = address
    message["To"] = address
    message["Subject"] = f"[assistant probe] {token}"
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = f"<assistant-probe-{token}@{domain}>"
    message["X-Assistant-Probe"] = token
    message.set_content(f"个人助手 Phase 0 验证 1 的测试邮件，可以直接删除。\ntoken: {token}\n")
    return message


# ---------------------------------------------------------------------------- IMAP 步骤


def _check(typ: str, what: str) -> None:
    if typ != "OK":
        raise SpikeError(f"IMAP {what} 返回 {typ}")


def inspect_mailbox(imap: ImapClient, report: dict[str, Any], limit: int) -> list[Folder]:
    typ, data = imap.capability()
    _check(typ, "CAPABILITY")
    caps = sorted({token.upper() for token in _as_text(data[0]).split()})
    report["capabilities"] = caps

    typ, data = imap.list()
    _check(typ, "LIST")
    folders = parse_list_response(data)
    report["folders"] = [{"name": f.name, "raw": f.raw, "flags": list(f.flags)} for f in folders]
    special = guess_special_folders(folders)
    report["special"] = {
        role: {"name": folder.name, "raw": folder.raw, "via": via}
        for role, (folder, via) in special.items()
    }

    typ, data = imap.status(quote_imap("INBOX"), "(MESSAGES UIDNEXT UIDVALIDITY)")
    _check(typ, "STATUS INBOX")
    inbox: dict[str, Any] = dict(parse_status(data))
    typ, _ = imap.select(quote_imap("INBOX"), readonly=True)
    _check(typ, "EXAMINE INBOX")
    typ, data = imap.uid("SEARCH", "ALL")
    _check(typ, "UID SEARCH ALL")
    uids = parse_uids(data)
    cutoff = imap_date(spike_common.now() - timedelta(days=31))
    typ, data = imap.uid("SEARCH", "BEFORE", cutoff)
    inbox["older_than_31d"] = len(parse_uids(data)) if typ == "OK" else None
    newest: list[MessageSummary] = []
    oldest: list[MessageSummary] = []
    if uids:
        recent = uids[-limit:] if limit > 0 else []
        wanted = sorted({*recent, uids[0]})
        typ, data = imap.uid("FETCH", ",".join(str(u) for u in wanted), HEADER_FIELDS)
        _check(typ, "UID FETCH")
        fetched = {m.uid: m for m in parse_fetch_response(data)}
        newest = [fetched[u] for u in reversed(recent) if u in fetched]
        oldest = [fetched[uids[0]]] if uids[0] in fetched else []
    inbox["newest"] = [vars(m) for m in newest]
    inbox["oldest_internaldate"] = oldest[0].internal_date if oldest else None
    report["inbox"] = inbox
    return folders


def search_probe(imap: ImapClient, folder_raw: str, message_id: str, token: str) -> dict[str, Any]:
    """在一个文件夹里分别用 HEADER Message-ID 和 SUBJECT 找测试信。"""
    result: dict[str, Any] = {"header_search": "未命中", "subject_search": "未命中", "uids": []}
    typ, _ = imap.select(quote_imap(folder_raw), readonly=True)
    if typ != "OK":
        result["error"] = f"EXAMINE 返回 {typ}"
        return result
    try:
        typ, data = imap.uid("SEARCH", "HEADER", "Message-ID", quote_imap(message_id))
        if typ == "OK":
            uids = parse_uids(data)
            result["header_search"] = "命中" if uids else "未命中"
            result["uids"] = uids
        else:
            result["header_search"] = f"返回 {typ}"
    except imaplib.IMAP4.error as exc:
        result["header_search"] = f"不可用：{exc}"
    typ, data = imap.uid("SEARCH", "SUBJECT", quote_imap(token))
    if typ == "OK":
        uids = parse_uids(data)
        result["subject_search"] = "命中" if uids else "未命中"
        result["uids"] = sorted({*result["uids"], *uids})
    if result["uids"]:
        typ, data = imap.uid("FETCH", str(result["uids"][0]), HEADER_FIELDS)
        found = parse_fetch_response(data) if typ == "OK" else []
        if found:
            result["message_id_preserved"] = found[0].message_id == message_id
            result["seen_message_id"] = found[0].message_id
    return result


def track_probe(
    imap: ImapClient,
    folders: dict[str, str],
    message_id: str,
    token: str,
    wait_seconds: float,
    interval: float = 3.0,
) -> dict[str, dict[str, Any]]:
    """发信后轮询各文件夹，记录测试信第一次出现的时间。folders 为 角色 → 服务器上的名字。"""
    started = time.monotonic()
    results: dict[str, dict[str, Any]] = {role: {"found": False} for role in folders}
    while True:
        for role, raw in folders.items():
            if results[role]["found"]:
                continue
            outcome = search_probe(imap, raw, message_id, token)
            if outcome["uids"]:
                outcome["found"] = True
                outcome["after_seconds"] = round(time.monotonic() - started, 1)
                results[role] = outcome
            else:
                results[role].update(outcome)
        if all(r["found"] for r in results.values()):
            return results
        if time.monotonic() - started >= wait_seconds:
            return results
        time.sleep(interval)


# ---------------------------------------------------------------------------- 真实连接


def imap_connect(config: MailConfig) -> imaplib.IMAP4_SSL:
    try:
        imap = imaplib.IMAP4_SSL(
            config.imap_host,
            config.imap_port,
            ssl_context=ssl.create_default_context(),
            timeout=30,
        )
    except (OSError, imaplib.IMAP4.error) as exc:
        raise SpikeError(f"连接 IMAP {config.imap_host}:{config.imap_port} 失败：{exc}") from exc
    try:
        imap.login(config.address, config.password)
    except imaplib.IMAP4.error as exc:
        spike_common.record_attempt("smail-login", "failed", "imap")
        with contextlib.suppress(imaplib.IMAP4.error, OSError):
            imap.logout()
        raise SpikeError(f"IMAP 登录失败：{exc}\n{LOGIN_HINT}") from exc
    spike_common.record_attempt("smail-login", "ok", "imap")
    return imap


def smtp_send(config: MailConfig, message: EmailMessage) -> float:
    started = time.monotonic()
    try:
        with smtplib.SMTP_SSL(
            config.smtp_host,
            config.smtp_port,
            context=ssl.create_default_context(),
            timeout=30,
        ) as smtp:
            smtp.login(config.address, config.password)
            refused = smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        spike_common.record_attempt("smail-login", "failed", "smtp")
        raise SpikeError(f"SMTP 登录失败：{exc}\n{LOGIN_HINT}") from exc
    except (OSError, smtplib.SMTPException) as exc:
        raise SpikeError(f"SMTP 发送失败：{exc}") from exc
    if refused:
        raise SpikeError(f"服务器拒收了这些收件人：{sorted(refused)}")
    return time.monotonic() - started


def send_and_track(
    imap: ImapClient, config: MailConfig, report: dict[str, Any], *, wait: float, yes: bool
) -> None:
    sent = report.get("special", {}).get("sent")
    question = f"将用 SMTP 给自己（{spike_common.redact(config.address)}）发一封测试信，继续？"
    if not spike_common.confirm(question, assume_yes=yes):
        report["send_self"] = {"skipped": "未确认"}
        return
    token = secrets.token_hex(6)
    message = make_probe_message(config.address, token)
    message_id = str(message["Message-ID"])
    elapsed = smtp_send(config, message)
    print(f"已发送测试信（{elapsed:.1f} 秒），开始在文件夹里查找，最多等待 {wait:g} 秒……")
    folders = {"inbox": "INBOX"}
    if sent:
        folders["sent"] = sent["raw"]
    report["send_self"] = {
        "token": token,
        "message_id": message_id,
        "smtp_seconds": round(elapsed, 1),
        "tracking": track_probe(imap, folders, message_id, token, wait),
    }


# ---------------------------------------------------------------------------- 报告


def _yes_no(value: object) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "未知"


def conclusions(report: dict[str, Any]) -> list[str]:
    """不含个人信息的结论，可以直接粘贴到 docs/recon/smail.md。"""
    caps = set(report.get("capabilities", []))
    login = report.get("imap_login", "未尝试")
    lines = [f"- IMAP 登录：{login}（{report.get('imap_server', '')}）"]
    lines.append(
        "- 服务器能力："
        + "，".join(f"{cap} {'有' if cap in caps else '无'}" for cap in INTERESTING_CAPABILITIES)
    )
    special = report.get("special", {})
    parts = [
        f"{ROLE_NAMES[role]}=“{info['name']}”（按{info['via']}识别）"
        for role, info in special.items()
    ]
    lines.append("- 特殊文件夹：" + ("；".join(parts) if parts else "都没有识别出来，请人工查看"))
    lines.append(f"- 文件夹总数：{len(report.get('folders', []))}")
    inbox = report.get("inbox", {})
    if inbox:
        oldest = parse_internaldate(inbox.get("oldest_internaldate") or "")
        older = inbox.get("older_than_31d")
        if older:
            verdict = "有早于 31 天的邮件，“收取全部邮件”看起来已生效"
        elif older == 0 and inbox.get("MESSAGES"):
            verdict = "没有早于 31 天的邮件：可能只收取最近 30 天，请到网页版设置里确认"
        else:
            verdict = "无法判断"
        lines.append(
            f"- 收件箱：{inbox.get('MESSAGES', '?')} 封；最早一封 "
            f"{oldest.date().isoformat() if oldest else '未知'}；{verdict}"
        )
    tracking = report.get("send_self", {}).get("tracking")
    if tracking:
        sent = tracking.get("sent")
        if sent is None:
            lines.append("- 已发送：没有识别出“已发送”文件夹，无法判断是否自动保存")
        elif sent.get("found"):
            lines.append(
                f"- SMTP 发出的信自动出现在“已发送”：是，约 {sent['after_seconds']} 秒；"
                f"Message-ID 原样保留：{_yes_no(sent.get('message_id_preserved'))}"
            )
        else:
            lines.append("- SMTP 发出的信自动出现在“已发送”：否（等待期内没出现），要 append_sent")
        if sent is not None:
            lines.append(f"- SEARCH HEADER Message-ID：{sent.get('header_search')}")
            lines.append(f"- SEARCH SUBJECT：{sent.get('subject_search')}")
        inbox_track = tracking.get("inbox", {})
        if inbox_track.get("found"):
            lines.append(f"- 发给自己的信进入收件箱：是，约 {inbox_track['after_seconds']} 秒")
        else:
            lines.append("- 发给自己的信进入收件箱：等待期内没有出现")
    return lines


def print_private_view(report: dict[str, Any], show_subjects: bool) -> None:
    print("文件夹：")
    for folder in report.get("folders", []):
        flags = " ".join(folder["flags"])
        print(f"  {folder['name']}  [{flags}]")
    newest = report.get("inbox", {}).get("newest", [])
    if newest:
        print(f"收件箱最新 {len(newest)} 封（只取信头，不改已读状态）：")
    for item in newest:
        subject = item["subject"] if show_subjects else spike_common.redact(item["subject"])[:16]
        sender = item["sender"] if show_subjects else spike_common.redact(item["sender"])
        print(f"  UID {item['uid']}  {item['internal_date']}  {sender}  {subject}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="验证 1：smail IMAP/SMTP 探测（默认只读）")
    parser.add_argument("--limit", type=int, default=5, help="列出收件箱最新几封的信头")
    parser.add_argument("--send-self", action="store_true", help="给自己发一封测试信并检查“已发送”")
    parser.add_argument("--wait", type=float, default=120, help="发信后最多等待多少秒")
    parser.add_argument("--yes", action="store_true", help="发信前不再询问")
    parser.add_argument("--show-subjects", action="store_true", help="屏幕上显示完整主题与发件人")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = MailConfig.from_env()
    spike_common.check_attempts(
        "smail-login", limit=3, window=timedelta(hours=1), outcomes=["failed"]
    )
    report: dict[str, Any] = {
        "started": spike_common.now().isoformat(timespec="seconds"),
        "imap_server": f"{config.imap_host}:{config.imap_port}",
    }
    imap = imap_connect(config)
    report["imap_login"] = "成功"
    try:
        inspect_mailbox(imap, report, args.limit)
        if args.send_self:
            send_and_track(imap, config, report, wait=args.wait, yes=args.yes)
    finally:
        with contextlib.suppress(imaplib.IMAP4.error, OSError):
            imap.logout()
    path = spike_common.write_json_private(
        spike_common.state_dir("recon", "smail") / f"{spike_common.stamp()}.json", report
    )
    print_private_view(report, args.show_subjects)
    print("\n=== 可公开结论（粘贴到 docs/recon/smail.md）===")
    print("\n".join(conclusions(report)))
    print(f"\n明细（含主题，私密）：{path}")
    if not args.send_self:
        print("下一步：确认只读结果无误后，加 --send-self 验证“已发送”与 Message-ID。")
    return 0


if __name__ == "__main__":
    spike_common.run(main)
