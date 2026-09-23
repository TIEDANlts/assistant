from pathlib import Path

import pytest

import ehall_login
import spike_common
from fake_site import REQUIRES_CHROMIUM, FakeSite
from spike_common import SpikeError

STRUCTURE = [
    {
        "url": "authserver.example/authserver/login",
        "inputs": [
            {"type": "text", "name": "username", "id": "u", "placeholder": "学号", "visible": True},
            {"type": "password", "name": "password", "id": "p", "placeholder": "", "visible": True},
            {"type": "text", "name": "yzm", "id": "c", "placeholder": "验证码", "visible": False},
        ],
        "hints": [{"text": "扫码登录"}, {"text": "七天内免登录"}],
        "images": [],
    }
]


def use_fake_site(monkeypatch: pytest.MonkeyPatch, site: FakeSite, password: str) -> None:
    monkeypatch.setenv("EHALL_BASE_URL", site.portal_url)
    monkeypatch.setenv("EHALL_AUTH_HOST", "localhost")
    monkeypatch.setenv("EHALL_USERNAME", site.username)
    monkeypatch.setenv("EHALL_PASSWORD", password)


def test_summarize_structure_reads_login_clues() -> None:
    summary = ehall_login.summarize_structure(STRUCTURE)
    assert summary == {
        "password_field": True,
        "captcha": False,
        "captcha_hidden": True,
        "qr": True,
        "remember_me": True,
        "sms": False,
    }


def test_cookie_metadata_drops_values() -> None:
    cookies = [
        {"name": "CASTGC", "value": "TOP-SECRET", "domain": "a.test", "path": "/", "expires": -1},
        {"name": "route", "value": "abc", "domain": "ehall.example", "path": "/", "expires": 0},
    ]
    rows = ehall_login.cookie_metadata(cookies)
    assert rows[0]["expires"] == "会话"
    assert "TOP-SECRET" not in repr(rows)
    assert all("value" not in row for row in rows)


def test_password_login_needs_credentials_before_opening_a_browser() -> None:
    with pytest.raises(SpikeError, match="EHALL_USERNAME"):
        ehall_login.main(["login", "--method", "password", "--headless"])


def test_manual_login_refuses_headless() -> None:
    with pytest.raises(SpikeError, match="有界面"):
        ehall_login.main(["login", "--headless"])


def test_attempt_limit_stops_password_login(
    monkeypatch: pytest.MonkeyPatch, fake_site: FakeSite
) -> None:
    use_fake_site(monkeypatch, fake_site, "wrong")
    for _ in range(3):
        spike_common.record_attempt(ehall_login.PASSWORD_KIND, "failed")
    with pytest.raises(SpikeError, match="上限 3"):
        ehall_login.main(["login", "--method", "password", "--headless", "--yes"])
    assert fake_site.paths() == []


def test_reach_reports_reachable_and_unreachable(fake_site: FakeSite) -> None:
    ok = ehall_login.probe_url(fake_site.portal_url, timeout=5)
    assert ok["reachable"] is True
    assert ok["status"] == 200
    assert ok["final"] == "localhost/authserver/login"
    fake_site.stop()
    down = ehall_login.probe_url(fake_site.portal_url, timeout=2)
    assert down["reachable"] is False
    assert down["error"]


@pytest.mark.browser
@REQUIRES_CHROMIUM
def test_password_login_saves_private_state_and_check_detects_expiry(
    monkeypatch: pytest.MonkeyPatch, fake_site: FakeSite, data_home: Path
) -> None:
    use_fake_site(monkeypatch, fake_site, fake_site.password)
    argv = ["login", "--method", "password", "--headless", "--yes", "--no-questions"]
    assert ehall_login.main(argv) == 0
    state = data_home / "state" / "ehall" / "storage_state.json"
    assert state.stat().st_mode & 0o777 == 0o600
    assert ehall_login.main(["check"]) == 0
    fake_site.expire_all()
    assert ehall_login.main(["check", "--no-save"]) == 1
    recon = data_home / "state" / "recon" / "ehall_login"
    observations = sorted(recon.glob("*-observations.json"))
    assert observations, "应当留下观察记录"
    text = observations[0].read_text(encoding="utf-8")
    assert '"remember_me": true' in text
    assert '"landing": "127.0.0.1/portal"' in text
    for path in (data_home / "state").rglob("*"):
        if path.is_file():
            assert fake_site.password.encode() not in path.read_bytes(), f"密码出现在 {path}"


@pytest.mark.browser
@REQUIRES_CHROMIUM
def test_wrong_password_counts_as_failed_attempt(
    monkeypatch: pytest.MonkeyPatch, fake_site: FakeSite, data_home: Path
) -> None:
    use_fake_site(monkeypatch, fake_site, "wrong-password")
    argv = ["login", "--method", "password", "--headless", "--yes", "--no-questions", "--wait", "1"]
    assert ehall_login.main(argv) == 1
    window = ehall_login.ATTEMPT_WINDOW
    assert spike_common.count_attempts(ehall_login.PASSWORD_KIND, window, ["failed"]) == 1
    assert not (data_home / "state" / "ehall" / "storage_state.json").exists()
