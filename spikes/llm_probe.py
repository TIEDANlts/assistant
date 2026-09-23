"""验证 6：模型结构化输出（Pydantic 校验）、耗时与费用。

    uv run python spikes/llm_probe.py --dry-run   # 只打印将要发送的提示词与 schema，不调用接口
    uv run python spikes/llm_probe.py --runs 3    # 真实调用 3 次，看稳定性

读取 .env 的 LLM_PROVIDER（anthropic 或 openai 兼容接口）、LLM_MODEL、LLM_API_KEY、LLM_BASE_URL。
输入是一份虚构的讲座通知，不含任何真实个人信息。检查模型能否把“报名截止”和“活动开始”分开——
这正是 PLAN 里“不要把报名截止时间当成活动开始时间”这条纠正要防的错误。
结果写到 state/recon/llm_probe.jsonl。
"""

import argparse
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

import spike_common
from spike_common import SpikeError

TOOL_NAME = "record_notice"

NOTICE = """发件时间：2026-09-21 09:30（周一）
主题：关于举办“AI 与软件工程”系列讲座第 3 讲的通知

各位同学：
计算机学院将举办“AI 与软件工程”系列讲座第 3 讲，欢迎参加。
一、报名方式：请于本周五（9 月 25 日）17:00 前在 ehall“学术活动报名”中完成报名，逾期不予受理。
二、讲座时间：10 月 12 日（周一）14:00–16:00。
三、讲座地点：仙林校区计算机科学技术楼 229 报告厅。
四、材料提交：需要认定学分的同学，请于 10 月 19 日 24:00 前将听讲心得（不少于 800 字）
发送至 lecture@example.edu.cn。
五、参加对象：全日制本科生与研究生，限 120 人。
联系人：王老师，电话 025-0000-0000。
"""

SYSTEM = (
    "你是信息抽取器。只根据通知原文抽取字段，不推测原文没有的信息。"
    "所有时间用 ISO 8601 并带 +08:00 时区；原文只写月日时，年份按发件时间推断；"
    "“24:00”写成次日 00:00。报名截止等截止时间只能放进 deadlines，不能当作活动开始时间。"
)


class Deadline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="截止事项，例如“报名截止”“材料提交截止”")
    at: datetime = Field(description="截止时间，ISO 8601，带 +08:00")


class Channel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["ehall", "email", "offline", "other"]
    target: str = Field(description="ehall 事项名、邮箱地址或线下地点")


class NoticeFields(BaseModel):
    """Phase 6 notice 工作流字段的草稿版。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    publisher: str | None = None
    event_start: datetime | None = Field(default=None, description="活动开始时间，不是报名截止")
    event_end: datetime | None = None
    location: str | None = None
    deadlines: list[Deadline] = Field(default_factory=list)
    submission_channels: list[Channel] = Field(default_factory=list)
    required_materials: list[str] = Field(default_factory=list)
    eligibility: str | None = None


@dataclass(frozen=True)
class RawResult:
    data: Any  # 强制工具调用时，模型给出的参数对象
    text: str | None  # JSON 模式时，模型给出的文本
    input_tokens: int | None
    output_tokens: int | None
    seconds: float


Caller = Callable[[str], RawResult]


def inline_refs(schema: Mapping[str, Any]) -> dict[str, Any]:
    """把 $defs 里的定义就地展开：有的接口不接受 $ref。"""
    defs = dict(schema.get("$defs", {}))

    def expand(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                rest = {key: value for key, value in node.items() if key != "$ref"}
                return expand({**defs[ref.removeprefix("#/$defs/")], **rest})
            return {key: expand(value) for key, value in node.items() if key != "$defs"}
        if isinstance(node, list):
            return [expand(item) for item in node]
        return node

    return expand(dict(schema))


def notice_schema() -> dict[str, Any]:
    return inline_refs(NoticeFields.model_json_schema())


def user_prompt(feedback: str) -> str:
    prompt = f"从下面的通知中抽取字段：\n\n{NOTICE}"
    if feedback:
        prompt += f"\n上一次的输出没有通过校验，请修正：\n{feedback}"
    return prompt


def anthropic_caller(model: str, api_key: str, base_url: str | None) -> Caller:
    """强制工具调用：tool_choice 指定唯一工具，input_schema 由 Pydantic 生成。"""
    import anthropic
    from anthropic.types import ToolUseBlock

    client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
    schema = notice_schema()

    def call(feedback: str) -> RawResult:
        started = time.monotonic()
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=SYSTEM,
            messages=[{"role": "user", "content": user_prompt(feedback)}],
            tools=[{"name": TOOL_NAME, "description": "记录抽取的字段", "input_schema": schema}],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )
        data = next((b.input for b in response.content if isinstance(b, ToolUseBlock)), None)
        usage = response.usage
        seconds = time.monotonic() - started
        return RawResult(data, None, usage.input_tokens, usage.output_tokens, seconds)

    return call


def openai_caller(model: str, api_key: str, base_url: str | None) -> Caller:
    """OpenAI 兼容接口：JSON 模式，schema 写进系统提示词。"""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    schema_text = json.dumps(notice_schema(), ensure_ascii=False)
    instructions = f"{SYSTEM}\n只输出一个 JSON 对象，符合这个 JSON Schema：\n{schema_text}"

    def call(feedback: str) -> RawResult:
        started = time.monotonic()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": user_prompt(feedback)},
            ],
            response_format={"type": "json_object"},
        )
        usage = response.usage
        text = response.choices[0].message.content or ""
        prompt_tokens = usage.prompt_tokens if usage else None
        completion_tokens = usage.completion_tokens if usage else None
        seconds = time.monotonic() - started
        return RawResult(None, text, prompt_tokens, completion_tokens, seconds)

    return call


def parse_output(raw: RawResult) -> NoticeFields:
    if raw.data is not None:
        return NoticeFields.model_validate(raw.data)
    text = (raw.text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return NoticeFields.model_validate_json(text)


def run_once(call: Caller, max_attempts: int = 2) -> dict[str, Any]:
    """调用一次；校验失败时把错误信息回传，最多再试一次。"""
    feedback = ""
    attempts: list[dict[str, Any]] = []
    fields: NoticeFields | None = None
    for _ in range(max_attempts):
        raw = call(feedback)
        attempt: dict[str, Any] = {
            "seconds": round(raw.seconds, 2),
            "input_tokens": raw.input_tokens,
            "output_tokens": raw.output_tokens,
        }
        attempts.append(attempt)
        try:
            fields = parse_output(raw)
        except ValidationError as exc:
            feedback = str(exc)[:1500]
            attempt["error"] = feedback
            continue
        break
    return {"attempts": attempts, "valid": fields is not None, "fields": fields}


def _local(moment: datetime) -> datetime:
    """统一到北京时间；没有时区的按北京时间理解。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=spike_common.TZ)
    return moment.astimezone(spike_common.TZ)


def _at(moment: datetime | None, month: int, day: int, hour: int, minute: int = 0) -> bool:
    if moment is None:
        return False
    return (moment.month, moment.day, moment.hour, moment.minute) == (month, day, hour, minute)


def evaluate(fields: NoticeFields) -> dict[str, bool]:
    """对照虚构通知的标准答案逐项检查。"""
    deadlines = [_local(deadline.at) for deadline in fields.deadlines]
    start = _local(fields.event_start) if fields.event_start else None
    end = _local(fields.event_end) if fields.event_end else None
    moments = [*deadlines, *([start] if start else [])]
    kinds = {channel.kind for channel in fields.submission_channels}
    material_due = any(_at(d, 10, 20, 0) or _at(d, 10, 19, 23, 59) for d in deadlines)
    return {
        "活动开始是 10 月 12 日 14:00": _at(start, 10, 12, 14),
        "活动结束是 10 月 12 日 16:00": _at(end, 10, 12, 16),
        "报名截止是 9 月 25 日 17:00": any(_at(d, 9, 25, 17) for d in deadlines),
        "报名截止没有被当成活动开始": start is not None and not _at(start, 9, 25, 17),
        "材料截止是 10 月 19 日 24:00（次日 00:00 或当日 23:59）": material_due,
        "年份推断为 2026": bool(moments) and all(m.year == 2026 for m in moments),
        "提交渠道包含 ehall 和邮件": {"ehall", "email"} <= kinds,
    }


def estimate_cost(
    input_tokens: int | None,
    output_tokens: int | None,
    price_in: float | None,
    price_out: float | None,
) -> float | None:
    """按每百万 token 单价估算；任一项缺失就返回 None。"""
    if input_tokens is None or output_tokens is None or price_in is None or price_out is None:
        return None
    return input_tokens / 1_000_000 * price_in + output_tokens / 1_000_000 * price_out


def build_caller(provider: str, model: str, api_key: str, base_url: str | None) -> Caller:
    if provider == "anthropic":
        return anthropic_caller(model, api_key, base_url)
    if provider == "openai":
        return openai_caller(model, api_key, base_url)
    raise SpikeError("LLM_PROVIDER 只能是 anthropic 或 openai（OpenAI 兼容接口）")


def summarize(records: list[dict[str, Any]], provider: str, model: str) -> list[str]:
    total = len(records)
    first_try = sum(1 for r in records if r["valid"] and r["attempts"] == 1)
    retried = sum(1 for r in records if r["valid"] and r["attempts"] > 1)
    seconds = sum(r["seconds"] for r in records) / total
    costs = [r["cost"] for r in records if r["cost"] is not None]
    cost_text = f"{sum(costs) / len(costs):.4f}（按 .env 单价）" if costs else "未配置单价"
    mode = "强制工具调用" if provider == "anthropic" else "JSON 模式"
    lines = [
        f"- 接入：{provider} / {model}（{mode}）",
        f"- 结构化输出：{total} 次中一次通过 {first_try} 次，重试后通过 {retried} 次",
        f"- 平均耗时 {seconds:.1f} 秒；平均单次费用 {cost_text}",
    ]
    names = list(records[0]["checks"]) if records[0]["checks"] else []
    for name in names:
        passed = sum(1 for r in records if r["checks"].get(name))
        lines.append(f"- {name}：{passed}/{total}")
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="验证 6：模型结构化输出、耗时与费用")
    parser.add_argument("--provider", choices=["anthropic", "openai"], help="默认读 LLM_PROVIDER")
    parser.add_argument("--model", help="默认读 LLM_MODEL")
    parser.add_argument("--runs", type=int, default=1, help="调用几次")
    parser.add_argument("--dry-run", action="store_true", help="只打印提示词与 schema")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.runs < 1:
        raise SpikeError("--runs 至少为 1")
    provider = args.provider or spike_common.env("LLM_PROVIDER", "anthropic")
    if args.dry_run:
        print(f"=== system ===\n{SYSTEM}\n\n=== user ===\n{user_prompt('')}")
        print(f"=== schema ===\n{json.dumps(notice_schema(), ensure_ascii=False, indent=2)}")
        return 0
    model = args.model or spike_common.require_env("LLM_MODEL")
    api_key = spike_common.require_env("LLM_API_KEY")
    base_url = spike_common.env("LLM_BASE_URL") or None
    price_in = spike_common.env_float("LLM_PRICE_INPUT_PER_MTOK")
    price_out = spike_common.env_float("LLM_PRICE_OUTPUT_PER_MTOK")
    call = build_caller(provider, model, api_key, base_url)
    log = spike_common.state_dir("recon") / "llm_probe.jsonl"
    records: list[dict[str, Any]] = []
    for number in range(1, args.runs + 1):
        try:
            outcome = run_once(call)
        except Exception as exc:  # SDK 的网络、鉴权、限流错误各不相同，统一转成可读的提示
            raise SpikeError(f"调用模型失败：{type(exc).__name__}: {exc}") from exc
        fields: NoticeFields | None = outcome["fields"]
        attempts = outcome["attempts"]
        tokens_in = sum(a["input_tokens"] or 0 for a in attempts)
        tokens_out = sum(a["output_tokens"] or 0 for a in attempts)
        record = {
            "ts": spike_common.now().isoformat(timespec="seconds"),
            "provider": provider,
            "model": model,
            "run": number,
            "valid": outcome["valid"],
            "attempts": len(attempts),
            "seconds": round(sum(a["seconds"] for a in attempts), 2),
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "cost": estimate_cost(tokens_in, tokens_out, price_in, price_out),
            "checks": evaluate(fields) if fields else {},
            "output": fields.model_dump(mode="json") if fields else None,
            "errors": [a["error"] for a in attempts if "error" in a],
        }
        spike_common.append_jsonl(log, record)
        records.append(record)
        verdict = "通过校验" if record["valid"] else "未通过校验"
        print(
            f"第 {number} 次：{verdict}（尝试 {record['attempts']} 次），{record['seconds']} 秒，"
            f"token {tokens_in}/{tokens_out}"
        )
        for name, passed in record["checks"].items():
            print(f"    {'✓' if passed else '✗'} {name}")
    print("\n=== 可公开结论（粘贴到 docs/recon/llm.md）===")
    print("\n".join(summarize(records, provider, model)))
    print(f"\n明细（含模型输出）：{log}")
    return 0 if all(r["valid"] for r in records) else 1


if __name__ == "__main__":
    spike_common.run(main)
