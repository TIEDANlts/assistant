from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

import ehall_recon
from fake_site import REQUIRES_CHROMIUM, FakeSite
from spike_common import read_jsonl

pytestmark = [pytest.mark.browser, REQUIRES_CHROMIUM]

TRY_WRITES = """async () => {
  const attempt = async (url, init) => {
    try { await fetch(url, init); return 'sent'; } catch (e) { return 'blocked'; }
  };
  const form = new FormData();
  form.append('file', new Blob(['x']), 'a.pdf');
  return [
    await attempt('/api/T_SQ_SAVE.do', { method: 'POST', body: 'a=1' }),
    await attempt('/api/item/1', { method: 'DELETE' }),
    await attempt('/api/fj.do', { method: 'POST', body: form }),
  ];
}"""


def test_guard_blocks_danger_clicks_and_write_requests(fake_site: FakeSite, tmp_path: Path) -> None:
    session = tmp_path / "session"
    recorder = ehall_recon.Recorder(session, ["127.0.0.1"])
    with (
        sync_playwright() as playwright,
        ehall_recon.guarded_context(
            playwright, recorder, headless=True, storage_state=None, allow=[], har_path=None
        ) as context,
    ):
        page = context.new_page()
        page.goto(fake_site.url("/recon/form"))
        page.click("#query")
        page.wait_for_function("document.body.dataset.last === 'query-ok'")
        for selector in ("#save", "#submit", "#withdraw"):
            page.click(selector)
        page.wait_for_timeout(300)
        assert page.evaluate("document.body.dataset.last") == "query-ok"
        assert page.evaluate(TRY_WRITES) == ["blocked", "blocked", "blocked"]
        page.click("#reason")  # 标签含“修改”，但输入框本身必须可用
        page.keyboard.type("abc")
        assert page.input_value("#reason") == "abc"
        page.fill("#purpose", "SECRET-VALUE-123")
        assert page.evaluate("window.__reconTakeSnapshot()") == {"saved": "forms/001.json"}
        assert page.locator("#__recon_toolbar").count() == 1
    assert fake_site.paths("POST") == ["/api/getSqList.do"]
    assert fake_site.paths("DELETE") == []

    snapshot = (session / "forms" / "001.json").read_text(encoding="utf-8")
    for expected in ("用途*", "purpose", "修改原因", "英文", "申请事项", "WID"):
        assert expected in snapshot
    for private in ("SECRET-VALUE-123", "hidden-wid-value", "已通过", "abc"):
        assert private not in snapshot
    guard_words = {event["word"] for event in read_jsonl(session / "guard.jsonl")}
    assert {"暂存", "提交", "撤回"} <= guard_words
    requests = read_jsonl(session / "requests.jsonl")
    query = next(r for r in requests if r["path"] == "/api/getSqList.do")
    assert query["body_keys"] == ["pageNumber", "pageSize"]
    assert query["blocked"] is False
    assert "pageSize=10" not in (session / "requests.jsonl").read_text(encoding="utf-8")
    assert any(r.get("saved") for r in read_jsonl(session / "responses.jsonl"))
