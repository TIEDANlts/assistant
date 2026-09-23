"""llm_probe 的离线测试：不调用任何真实模型。"""

import json
from typing import Any

import pytest

import llm_probe
from llm_probe import NoticeFields, RawResult
from spike_common import SpikeError

GOOD: dict[str, Any] = {
    "title": "“AI 与软件工程”系列讲座第 3 讲",
    "publisher": "计算机学院",
    "event_start": "2026-10-12T14:00:00+08:00",
    "event_end": "2026-10-12T16:00:00+08:00",
    "location": "仙林校区计算机科学技术楼 229 报告厅",
    "deadlines": [
        {"kind": "报名截止", "at": "2026-09-25T17:00:00+08:00"},
        {"kind": "材料提交截止", "at": "2026-10-20T00:00:00+08:00"},
    ],
    "submission_channels": [
        {"kind": "ehall", "target": "学术活动报名"},
        {"kind": "email", "target": "lecture@example.edu.cn"},
    ],
    "required_materials": ["听讲心得（不少于 800 字）"],
    "eligibility": "全日制本科生与研究生",
}


def test_schema_is_self_contained() -> None:
    schema = llm_probe.notice_schema()
    text = json.dumps(schema)
    assert "$ref" not in text
    assert "$defs" not in text
    deadline = schema["properties"]["deadlines"]["items"]
    assert deadline["properties"]["at"]["format"] == "date-time"


def test_correct_extraction_passes_every_check() -> None:
    checks = llm_probe.evaluate(NoticeFields.model_validate(GOOD))
    assert all(checks.values()), checks


def test_deadline_mistaken_for_event_start_is_caught() -> None:
    wrong = {**GOOD, "event_start": "2026-09-25T17:00:00+08:00"}
    checks = llm_probe.evaluate(NoticeFields.model_validate(wrong))
    assert checks["报名截止没有被当成活动开始"] is False
    assert checks["活动开始是 10 月 12 日 14:00"] is False


def test_validation_error_is_fed_back_once() -> None:
    outputs = [
        RawResult(None, '{"title": 1, "unexpected": true}', 10, 5, 0.1),
        RawResult(GOOD, None, 12, 30, 0.2),
    ]
    seen: list[str] = []

    def call(feedback: str) -> RawResult:
        seen.append(feedback)
        return outputs.pop(0)

    outcome = llm_probe.run_once(call)
    assert outcome["valid"] is True
    assert len(outcome["attempts"]) == 2
    assert seen[0] == ""
    assert "title" in seen[1]
    assert "unexpected" in seen[1]


def test_fenced_json_is_accepted() -> None:
    text = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    assert llm_probe.parse_output(RawResult(None, text, None, None, 0.1)).location


def test_estimate_cost() -> None:
    assert llm_probe.estimate_cost(1_000_000, 500_000, 3.0, 15.0) == pytest.approx(10.5)
    assert llm_probe.estimate_cost(None, 1, 1.0, 1.0) is None


def test_dry_run_needs_no_credentials() -> None:
    assert llm_probe.main(["--dry-run"]) == 0


def test_missing_model_is_reported() -> None:
    with pytest.raises(SpikeError, match="LLM_MODEL"):
        llm_probe.main([])
