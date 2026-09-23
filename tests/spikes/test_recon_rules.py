import re

import pytest

import ehall_recon

BASE = "https://ehall.example.edu.cn"


@pytest.mark.parametrize(
    ("method", "path", "resource_type", "content_type", "blocked"),
    [
        ("GET", "/portal", "document", "", False),
        ("POST", "/api/getSqList.do", "xhr", "application/x-www-form-urlencoded", False),
        ("POST", "/api/T_SQ_SAVE.do", "xhr", "", True),
        ("POST", "/api/submitApply.do", "fetch", "", True),
        ("GET", "/api/deleteItem.do", "xhr", "", True),
        ("POST", "/xsfw/tjSq.do", "xhr", "", True),
        ("DELETE", "/api/item/1", "fetch", "", True),
        ("PUT", "/api/item/1", "fetch", "", True),
        ("POST", "/api/fj.do", "fetch", "multipart/form-data; boundary=x", True),
        ("GET", "/static/save.js", "script", "", False),
        ("POST", "/authserver/login", "document", "application/x-www-form-urlencoded", False),
    ],
)
def test_classify_request(
    method: str, path: str, resource_type: str, content_type: str, blocked: bool
) -> None:
    decision = ehall_recon.classify_request(method, BASE + path, resource_type, content_type)
    assert decision.blocked is blocked, decision.reason


def test_allow_list_overrides_tokens_but_never_uploads_or_write_methods() -> None:
    allow = [re.compile(r"applyList")]
    url = BASE + "/api/applyList.do"
    assert ehall_recon.classify_request("POST", url, "xhr", "", allow).reason == "白名单"
    assert ehall_recon.classify_request("POST", url, "xhr", "multipart/form-data", allow).blocked
    assert ehall_recon.classify_request("DELETE", url, "xhr", "", allow).blocked


def test_path_tokens_split_camel_case_and_underscores() -> None:
    assert ehall_recon.path_tokens(BASE + "/app/T_SQ_SAVE.do") == ["app", "t", "sq", "save"]
    assert ehall_recon.path_tokens(BASE + "/a/b/getSqList.do?x=1") == ["b", "get", "sq", "list"]


def test_body_keys_never_return_values() -> None:
    form = b"pageSize=10&querySetting=%5B%5D"
    assert ehall_recon.body_keys("application/x-www-form-urlencoded", form) == [
        "pageSize",
        "querySetting",
    ]
    payload = '{"name": "张三", "sid": "221220001"}'.encode()
    assert ehall_recon.body_keys("application/json", payload) == ["name", "sid"]
    assert ehall_recon.body_keys("multipart/form-data; boundary=x", b"--x") == ["<multipart>"]
    assert ehall_recon.body_keys("", None) == []


def test_default_target_suffix() -> None:
    assert ehall_recon.default_target_suffix("https://ehall.nju.edu.cn/") == "nju.edu.cn"
    assert ehall_recon.default_target_suffix("http://127.0.0.1:8080/portal") == "127.0.0.1"
    assert ehall_recon.default_target_suffix("https://portal.example.com/") == "example.com"


def test_safe_label() -> None:
    assert ehall_recon.safe_label("zaidu zhengming!") == "zaidu-zhengming"
    with pytest.raises(ehall_recon.SpikeError):
        ehall_recon.safe_label("在读证明")
