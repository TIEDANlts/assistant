"""ADR 骨架的守护测试：0001–0009 齐全、结构完整、都登记在索引里。"""

from pathlib import Path

import pytest

ADR_DIR = Path(__file__).resolve().parents[2] / "docs" / "adr"
REQUIRED_SECTIONS = ("## 背景", "## 决策", "## 后果", "## 放弃的方案", "## 待验证")


@pytest.mark.parametrize("number", range(1, 10))
def test_adr_has_required_structure(number: int) -> None:
    matches = sorted(ADR_DIR.glob(f"{number:04d}-*.md"))
    assert len(matches) == 1, f"ADR {number:04d} 应当恰好有一个文件，实际：{matches}"
    text = matches[0].read_text(encoding="utf-8")
    assert text.startswith(f"# ADR {number:04d}：")
    assert "- 状态：" in text
    assert "- 日期：" in text
    missing = [section for section in REQUIRED_SECTIONS if section not in text]
    assert missing == [], f"{matches[0].name} 缺少章节：{missing}"


def test_every_adr_is_listed_in_index() -> None:
    index = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    unlisted = [
        path.name
        for path in sorted(ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))
        if path.name != "0000-template.md" and path.name not in index
    ]
    assert unlisted == []
