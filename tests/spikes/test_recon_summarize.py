import json
from pathlib import Path

import pytest

import recon_summarize
from spike_common import SpikeError

TARGET = "ehall.example.edu.cn"


def make_session(root: Path) -> Path:
    session = root / "20260923-101010-demo"
    (session / "forms").mkdir(parents=True)
    meta = {"label": "demo", "target_hosts": ["example.edu.cn"]}
    (session / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    base = {"query_keys": [], "body_keys": [], "resource_type": "xhr", "blocked": False}
    requests = [
        {**base, "method": "POST", "host": TARGET, "path": "/api/getSqList.do", "body_keys": ["p"]},
        {**base, "method": "GET", "host": TARGET, "path": "/api/item/123456", "query_keys": ["t"]},
        {**base, "method": "GET", "host": TARGET, "path": "/api/item/654321"},
        {
            **base,
            "method": "POST",
            "host": TARGET,
            "path": "/api/fj.do",
            "blocked": True,
            "reason": "文件上传（multipart）",
        },
        {
            **base,
            "method": "GET",
            "host": "cdn.other.test",
            "path": "/x.js",
            "resource_type": "script",
        },
        {**base, "method": "POST", "host": "stats.other.test", "path": "/collect"},
    ]
    lines = [json.dumps(r, ensure_ascii=False) for r in requests]
    (session / "requests.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    guard = {"word": "提交", "text": "提交", "tag": "button", "event": "click", "page": "/form"}
    (session / "guard.jsonl").write_text(json.dumps(guard, ensure_ascii=False) + "\n", "utf-8")
    field = {
        "label": "联系电话（13812345678）",
        "name": "phone",
        "type": "text",
        "required": True,
        "readonly": False,
        "options": [],
        "option_count": 0,
    }
    frame = {
        "fields": [field, {**field, "label": "附件", "name": "att", "type": "file"}],
        "buttons": [{"text": "提交", "tag": "button", "danger": "提交"}],
        "tables": [["申请事项", "状态"]],
        "hidden": ["WID"],
    }
    form = json.dumps({"frames": [frame]}, ensure_ascii=False)
    (session / "forms" / "001.json").write_text(form, encoding="utf-8")
    return session


def test_render_service_merges_ids_and_redacts(tmp_path: Path) -> None:
    session = recon_summarize.load_session(make_session(tmp_path))
    markdown = recon_summarize.render_service("zaidu-zhengming", "在读证明", session)
    assert f"| GET | {TARGET}/api/item/{{n}} | t | — | 2 |" in markdown
    assert "13812345678" not in markdown
    assert "[手机号]" in markdown
    assert "选择即上传" in markdown
    assert "stats.other.test × 1" in markdown
    assert "cdn.other.test" not in markdown
    assert "⚠ 提交" in markdown
    assert "申请事项 \\| 状态" in markdown


def test_service_command_writes_to_data_repo_and_refuses_overwrite(
    tmp_path: Path, data_home: Path
) -> None:
    argv = ["service", "--session", str(make_session(tmp_path)), "--id", "zaidu-zhengming"]
    assert recon_summarize.main(argv) == 0
    out = data_home / "recon" / "services" / "zaidu-zhengming.md"
    assert out.is_file()
    with pytest.raises(SpikeError, match="已存在"):
        recon_summarize.main(argv)
    assert recon_summarize.main([*argv, "--force"]) == 0
    with pytest.raises(SpikeError, match="小写字母"):
        recon_summarize.main(["service", "--session", str(tmp_path), "--id", "在读证明"])


def test_catalog_export(tmp_path: Path, data_home: Path) -> None:
    rows = [
        {"WID": "a1", "FWMC": "在读证明", "SFZXBL": 1},
        {"WID": "b2", "FWMC": "成绩单", "SFZXBL": 0},
    ]
    response = tmp_path / "0001.json"
    response.write_text(json.dumps({"code": "0", "datas": {"rows": rows}}), encoding="utf-8")
    argv = ["catalog", "--response", str(response), "--list-path", "datas.rows"]
    argv += ["--map", "id=WID", "--map", "name=FWMC", "--map", "online=SFZXBL"]
    assert recon_summarize.main(argv) == 0
    document = json.loads((data_home / "recon" / "catalog-raw.json").read_text(encoding="utf-8"))
    assert document["count"] == 2
    assert document["services"][0] == {"id": "a1", "name": "在读证明", "online": 1}
    with pytest.raises(SpikeError, match="不存在"):
        recon_summarize.main([*argv[:4], "datas.items", "--map", "name=FWMC"])


def test_find_record_lists() -> None:
    obj = {"code": "0", "datas": {"rows": [{"a": 1}, {"a": 2, "b": 3}], "meta": {"x": [1, 2]}}}
    assert recon_summarize.find_record_lists(obj) == [("datas.rows", 2, ["a", "b"])]
