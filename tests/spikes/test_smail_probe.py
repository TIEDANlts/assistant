from email.header import Header
from typing import Any

import pytest

import smail_probe
from smail_probe import Folder

SENT_RAW = "&XfJT0ZAB-"  # “已发送”的修改版 UTF-7


def header_block(uid: int, subject: str, sender: str, message_id: str) -> bytes:
    encoded = Header(subject, "utf-8").encode()
    return (
        f"Message-ID: {message_id}\r\nDate: Mon, 21 Sep 2026 09:30:00 +0800\r\n"
        f"From: {sender}\r\nSubject: {encoded}\r\n\r\n"
    ).encode()


class FakeImap:
    """只实现本脚本用到的命令；断言所有 SELECT 都是只读、所有 FETCH 都用 BODY.PEEK。"""

    def __init__(self) -> None:
        self.mailboxes: dict[str, list[dict[str, Any]]] = {
            "INBOX": [
                self.message(3, "01-Jan-2026 08:00:00 +0800", "旧通知", "<old@x>"),
                self.message(7, "20-Sep-2026 10:00:00 +0800", "关于在读证明", "<a@x>"),
                self.message(9, "22-Sep-2026 11:00:00 +0800", "学号221220001的材料", "<b@x>"),
            ],
            SENT_RAW: [],
            "Drafts": [],
        }
        self.selected = ""
        self.selects: list[tuple[str, bool]] = []
        self.pending: list[tuple[str, dict[str, Any], int]] = []  # (文件夹, 邮件, 第几次轮询后出现)
        self.polls = 0

    @staticmethod
    def message(uid: int, internal: str, subject: str, message_id: str) -> dict[str, Any]:
        sender = "张老师 <zhang.teacher@nju.edu.cn>"
        return {
            "uid": uid,
            "internal": internal,
            "subject": subject,
            "message_id": message_id,
            "headers": header_block(uid, subject, sender, message_id),
        }

    def capability(self) -> tuple[str, list[bytes]]:
        return "OK", [b"IMAP4rev1 IDLE UIDPLUS ID"]

    def status(self, mailbox: str, names: str) -> tuple[str, list[bytes]]:
        items = self.mailboxes[smail_probe._unquote(mailbox)]
        uidnext = max((m["uid"] for m in items), default=0) + 1
        return "OK", [f"{mailbox} (MESSAGES {len(items)} UIDNEXT {uidnext} UIDVALIDITY 7)".encode()]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        name = smail_probe._unquote(mailbox)
        self.selects.append((name, readonly))
        assert readonly, "探测脚本只能 EXAMINE"
        if name == "INBOX":
            self.polls += 1
            for folder, message, after in list(self.pending):
                if self.polls > after:
                    self.mailboxes[folder].append(message)
                    self.pending.remove((folder, message, after))
        self.selected = name
        return "OK", [str(len(self.mailboxes[name])).encode()]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        items = self.mailboxes[self.selected]
        if command == "SEARCH":
            if args[0] == "ALL":
                hits = [m["uid"] for m in items]
            elif args[0] == "BEFORE":
                hits = [m["uid"] for m in items if m["internal"].endswith("2026 08:00:00 +0800")]
            elif args[0] == "HEADER":
                wanted = smail_probe._unquote(args[2])
                hits = [m["uid"] for m in items if m["message_id"] == wanted]
            else:
                token = smail_probe._unquote(args[1])
                hits = [m["uid"] for m in items if token in m["subject"]]
            return "OK", [" ".join(str(h) for h in hits).encode()]
        assert command == "FETCH"
        assert "BODY.PEEK" in args[1], "只能用 BODY.PEEK，避免把邮件标成已读"
        wanted_uids = {int(u) for u in args[0].split(",")}
        data: list[Any] = []
        for m in items:
            if m["uid"] in wanted_uids:
                head = f'1 (UID {m["uid"]} INTERNALDATE "{m["internal"]}" BODY[] {{9}}'
                data.extend([(head.encode(), m["headers"]), b" RFC822.SIZE 99)"])
        return "OK", data

    # 放在最后：方法名 list 会遮住类体里后面注解中的内置 list
    def list(self, directory: str = '""', pattern: str = "*") -> tuple[str, Any]:
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            f'(\\HasNoChildren) "/" "{SENT_RAW}"'.encode(),
            (b'(\\HasNoChildren) "/" {6}', b"Drafts"),
            b'(\\HasNoChildren \\Junk) "/" "&V4NXPpCuTvY-"',
        ]


@pytest.mark.parametrize(
    ("raw", "decoded"),
    [
        ("INBOX", "INBOX"),
        ("Sent Messages", "Sent Messages"),
        ("&XfJT0ZAB-", "已发送"),
        ("&V4NXPpCuTvY-", "垃圾邮件"),
        ("a&-b", "a&b"),
        ("Work/&XfJT0ZAB-", "Work/已发送"),
    ],
)
def test_decode_imap_utf7(raw: str, decoded: str) -> None:
    assert smail_probe.decode_imap_utf7(raw) == decoded


def test_parse_list_response_handles_quotes_atoms_and_literals() -> None:
    folders = smail_probe.parse_list_response(FakeImap().list()[1])
    assert [f.name for f in folders] == ["INBOX", "已发送", "Drafts", "垃圾邮件"]
    assert folders[1].raw == SENT_RAW
    assert "\\Junk" in folders[3].flags


def test_guess_special_folders_prefers_flags_then_names() -> None:
    folders = [
        Folder(raw="INBOX", name="INBOX", flags=()),
        Folder(raw=SENT_RAW, name="已发送", flags=()),
        Folder(raw="Archive", name="Archive", flags=("\\Sent",)),
    ]
    special = smail_probe.guess_special_folders(folders)
    assert special["sent"] == (folders[2], "标记")
    folders.pop()
    assert smail_probe.guess_special_folders(folders)["sent"] == (folders[1], "名字")


def test_quote_imap_escapes() -> None:
    assert smail_probe.quote_imap('Sent "x"\\y') == '"Sent \\"x\\"\\\\y"'


def test_make_probe_message_uses_own_domain() -> None:
    message = smail_probe.make_probe_message("someone@smail.nju.edu.cn", "abc123")
    assert message["Message-ID"] == "<assistant-probe-abc123@smail.nju.edu.cn>"
    assert message["X-Assistant-Probe"] == "abc123"
    assert message["To"] == message["From"]


def test_inspect_mailbox_is_read_only() -> None:
    imap = FakeImap()
    report: dict[str, Any] = {}
    smail_probe.inspect_mailbox(imap, report, limit=2)
    assert all(readonly for _, readonly in imap.selects)
    assert report["capabilities"] == ["ID", "IDLE", "IMAP4REV1", "UIDPLUS"]
    assert report["special"]["sent"] == {"name": "已发送", "raw": SENT_RAW, "via": "名字"}
    assert report["special"]["junk"]["via"] == "标记"
    inbox = report["inbox"]
    assert inbox["MESSAGES"] == 3
    assert inbox["older_than_31d"] == 1
    assert [m["uid"] for m in inbox["newest"]] == [9, 7]
    assert inbox["newest"][0]["subject"] == "学号221220001的材料"
    assert inbox["oldest_internaldate"] == "01-Jan-2026 08:00:00 +0800"


def test_track_probe_detects_autosaved_copy_and_preserved_message_id() -> None:
    imap = FakeImap()
    message_id = "<assistant-probe-t0k3n@smail.nju.edu.cn>"
    probe = FakeImap.message(
        20, "23-Sep-2026 10:00:00 +0800", "[assistant probe] t0k3n", message_id
    )
    imap.pending = [(SENT_RAW, probe, 1), ("INBOX", dict(probe, uid=10), 2)]
    folders = {"inbox": "INBOX", "sent": SENT_RAW}
    tracking = smail_probe.track_probe(
        imap, folders, message_id, "t0k3n", wait_seconds=5, interval=0
    )
    assert tracking["sent"]["found"] is True
    assert tracking["sent"]["header_search"] == "命中"
    assert tracking["sent"]["message_id_preserved"] is True
    assert tracking["inbox"]["found"] is True
    assert all(readonly for _, readonly in imap.selects)


def test_conclusions_flag_missing_sent_copy_and_contain_no_personal_data() -> None:
    imap = FakeImap()
    report: dict[str, Any] = {"imap_login": "成功", "imap_server": "imap.exmail.qq.com:993"}
    smail_probe.inspect_mailbox(imap, report, limit=3)
    folders = {"inbox": "INBOX", "sent": SENT_RAW}
    report["send_self"] = {
        "tracking": smail_probe.track_probe(imap, folders, "<p@x>", "nothing", 0, interval=0)
    }
    text = "\n".join(smail_probe.conclusions(report))
    assert "要 append_sent" in text
    assert "IDLE 有" in text
    assert "“收取全部邮件”看起来已生效" in text
    for private in ("在读证明", "221220001", "zhang.teacher", "张老师"):
        assert private not in text
