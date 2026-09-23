import os
from datetime import timedelta
from pathlib import Path

import pytest

import spike_common
from spike_common import SpikeError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("学号221220001的同学", "学号[学号]的同学"),
        ("研究生 MG21330001 提交", "研究生 [学号] 提交"),
        ("手机13812345678，", "手机[手机号]，"),
        ("身份证11010519491231002X", "身份证[身份证号]"),
        ("联系 zhang.san@smail.nju.edu.cn", "联系 z***@smail.nju.edu.cn"),
        ("邮箱abc@example.com", "邮箱a***@example.com"),
        ("JSESSIONID=abc123; route=xyz", "JSESSIONID=[已脱敏]; route=[已脱敏]"),
        ("ticket=ST-12345-abcdefGHIJ-cas", "ticket=ST-[已脱敏]"),
        ("id=3f2b8c1e-9d4a-4e2b-8f1a-2b3c4d5e6f70", "id=[令牌]"),
        ("T_XSXX_JBXX_QUERY_LIST_FOR_STUDENT", "T_XSXX_JBXX_QUERY_LIST_FOR_STUDENT"),
        ("时间戳 1727000000000", "时间戳 1727000000000"),
    ],
)
def test_redact(raw: str, expected: str) -> None:
    assert spike_common.redact(raw) == expected


def test_mask_path_merges_ids() -> None:
    assert spike_common.mask_path("/api/item/123456/detail") == "/api/item/{n}/detail"
    uuid_path = "/api/x/3f2b8c1e-9d4a-4e2b-8f1a-2b3c4d5e6f70"
    assert spike_common.mask_path(uuid_path) == "/api/x/{id}"
    assert spike_common.mask_path("/xsfw/sys/app.do") == "/xsfw/sys/app.do"


def test_load_dotenv_does_not_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# 注释\nSPIKE_A=1\nexport SPIKE_B="two words"\nSPIKE_C=\nSPIKE_D=from-file\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SPIKE_D", "from-shell")
    for name in ("SPIKE_A", "SPIKE_B", "SPIKE_C"):
        monkeypatch.delenv(name, raising=False)
    spike_common.load_dotenv(env_file)
    assert os.environ["SPIKE_A"] == "1"
    assert os.environ["SPIKE_B"] == "two words"
    assert spike_common.env("SPIKE_C", "默认") == "默认"
    assert os.environ["SPIKE_D"] == "from-shell"
    for name in ("SPIKE_A", "SPIKE_B", "SPIKE_C"):
        monkeypatch.delenv(name, raising=False)


def test_require_env_explains_missing_value() -> None:
    with pytest.raises(SpikeError, match="SMAIL_ADDRESS"):
        spike_common.require_env("SMAIL_ADDRESS")


def test_data_dir_must_exist_and_live_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_home: Path
) -> None:
    assert spike_common.data_dir() == data_home.resolve()
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(tmp_path / "missing"))
    with pytest.raises(SpikeError, match="不存在"):
        spike_common.data_dir()
    monkeypatch.setenv("ASSISTANT_DATA_DIR", "docs")
    with pytest.raises(SpikeError, match="代码仓库"):
        spike_common.data_dir()


@pytest.mark.skipif(os.name == "nt", reason="Windows 没有 POSIX 权限位")
def test_private_files_are_owner_only(data_home: Path) -> None:
    target = spike_common.state_dir("recon") / "x.json"
    spike_common.write_json_private(target, {"a": 1})
    spike_common.append_jsonl(spike_common.state_dir("recon") / "log.jsonl", {"b": 2})
    assert target.stat().st_mode & 0o777 == 0o600
    assert (data_home / "state" / "recon").stat().st_mode & 0o777 == 0o700
    assert (data_home / "state" / "recon" / "log.jsonl").stat().st_mode & 0o777 == 0o600


def test_attempt_limit_counts_only_selected_outcomes() -> None:
    window = timedelta(hours=24)
    spike_common.record_attempt("ehall-password", "ok")
    spike_common.record_attempt("ehall-password", "failed")
    spike_common.check_attempts("ehall-password", limit=2, window=window, outcomes=["failed"])
    spike_common.record_attempt("ehall-password", "failed")
    with pytest.raises(SpikeError, match="上限 2"):
        spike_common.check_attempts("ehall-password", limit=2, window=window, outcomes=["failed"])
    assert spike_common.count_attempts("ehall-password", window) == 3
    assert spike_common.count_attempts("smail-login", window) == 0
