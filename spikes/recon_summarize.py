"""把侦察会话整理成文档草稿，或从接口响应里导出服务目录。

    uv run python spikes/recon_summarize.py service --session <会话目录> --id zaidu-zhengming
        → $ASSISTANT_DATA_DIR/recon/services/<id>.md（自动脱敏的草稿，提交前人工检查）
    uv run python spikes/recon_summarize.py candidates --session <会话目录>
        → 列出 JSON 响应里像“记录列表”的数组，帮你找到服务目录接口
    uv run python spikes/recon_summarize.py catalog --response <文件> --list-path datas.rows \\
        --map id=<字段> --map name=<字段> --map category=<字段> --map online=<字段>
        → $ASSISTANT_DATA_DIR/recon/catalog-raw.json

代码仓库是公开的：事务文档和服务目录含学校内部接口，只写到私有的数据仓库（不是 state/，要提交）。
"""

import argparse
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import spike_common
from spike_common import SpikeError

SERVICE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{1,60}")
MANUAL_ROWS = (
    "入口（菜单路径或直达链接）",
    "分类 / 能否在线办理",
    "事务等级（Q / D / F / X，见 docs/recon/risk-tiers.md）",
    "提交后的页面与回执",
    "“我的申请”在哪里查看、状态有哪些",
    "能否撤回、在哪里撤回（撤回属 R4，只记录不自动化）",
    "附件：选择即上传，还是随提交上传",
    "填写过程中有没有自动保存或暂存请求",
    "审批流程与时长",
)


@dataclass
class Session:
    path: Path
    meta: dict[str, Any]
    requests: list[dict[str, Any]]
    guard: list[dict[str, Any]]
    forms: list[dict[str, Any]]


def load_session(path: Path) -> Session:
    if not path.is_dir():
        raise SpikeError(f"找不到侦察会话目录：{path}")
    meta_file = path / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.is_file() else {}
    forms = [
        json.loads(item.read_text(encoding="utf-8"))
        for item in sorted((path / "forms").glob("*.json"))
    ]
    return Session(
        path=path,
        meta=meta,
        requests=spike_common.read_jsonl(path / "requests.jsonl"),
        guard=spike_common.read_jsonl(path / "guard.jsonl"),
        forms=forms,
    )


def cell(value: object) -> str:
    """Markdown 表格单元：脱敏、去换行、转义竖线。"""
    text = spike_common.redact(str(value)).replace("\n", " ").replace("|", "\\|").strip()
    return text or "—"


def joined(values: Sequence[object], limit: int = 12) -> str:
    items = [str(v) for v in values if str(v)]
    extra = f" 等 {len(items)} 项" if len(items) > limit else ""
    return "、".join(items[:limit]) + extra if items else "—"


def is_target(host: str, suffixes: Sequence[str]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def aggregate_endpoints(
    requests: Sequence[Mapping[str, Any]], suffixes: Sequence[str]
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """按（方法、主机、合并 id 后的路径）聚合学校站点的接口；其他主机只计数。"""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    others: Counter[str] = Counter()
    for request in requests:
        if request.get("resource_type") in spike_common.STATIC_RESOURCE_TYPES:
            continue
        host = str(request.get("host", ""))
        if not is_target(host, suffixes):
            others[host] += 1
            continue
        method = str(request.get("method", ""))
        path = spike_common.mask_path(str(request.get("path", "")))
        group = groups.setdefault(
            (method, host, path),
            {
                "method": method,
                "host": host,
                "path": path,
                "query_keys": set(),
                "body_keys": set(),
                "count": 0,
                "blocked": set(),
            },
        )
        group["query_keys"].update(request.get("query_keys", []))
        group["body_keys"].update(request.get("body_keys", []))
        group["count"] += 1
        if request.get("blocked"):
            group["blocked"].add(str(request.get("reason", "")))
    ordered = sorted(groups.values(), key=lambda g: (g["path"], g["method"]))
    return ordered, others


def form_frames(forms: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [frame for form in forms for frame in form.get("frames", []) if "error" not in frame]


def merge_fields(forms: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    seen: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for frame in form_frames(forms):
        for field in frame.get("fields", []):
            key = (str(field.get("label", "")), str(field.get("name", "")), str(field.get("type")))
            seen.setdefault(key, field)
    return list(seen.values())


def merge_buttons(forms: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    seen: dict[str, Mapping[str, Any]] = {}
    for frame in form_frames(forms):
        for button in frame.get("buttons", []):
            seen.setdefault(str(button.get("text", "")), button)
    return list(seen.values())


def automatic_hints(session: Session, fields: Sequence[Mapping[str, Any]]) -> list[str]:
    hints: list[str] = []
    reasons = [str(r.get("reason", "")) for r in session.requests if r.get("blocked")]
    if any("multipart" in reason for reason in reasons):
        hints.append(
            "侦察中出现过文件上传请求（已中止）：附件可能是“选择即上传”，上传本身就是写操作。"
        )
    if any("疑似写接口" in reason for reason in reasons) and not session.guard:
        hints.append("有疑似写接口被中止，但你没有点过危险按钮：页面可能有自动保存或暂存请求。")
    if any(field.get("type") == "file" for field in fields):
        hints.append("表单里有文件字段：确认附件的上传时机。")
    return hints


def render_service(service_id: str, name: str, session: Session) -> str:
    suffixes = [str(s) for s in session.meta.get("target_hosts", [])]
    endpoints, others = aggregate_endpoints(session.requests, suffixes)
    fields = merge_fields(session.forms)
    buttons = merge_buttons(session.forms)
    frames = form_frames(session.forms)
    hidden = sorted({str(n) for frame in frames for n in frame.get("hidden", [])})
    tables = {" | ".join(map(str, t)) for frame in frames for t in frame.get("tables", [])}
    lines = [
        f"# 事务侦察：{cell(name)}（{service_id}）",
        "",
        f"> 由 `spikes/recon_summarize.py` 从侦察会话 `{session.path.name}` 生成的草稿，"
        "已自动脱敏（只有字段名、按钮文字、接口路径和参数名）。"
        "提交前请人工再看一遍标签和选项里有没有个人信息。",
        "",
        "## 基本信息（人工填写）",
        "",
        "| 项 | 内容 |",
        "| --- | --- |",
        *[f"| {row} | 待填写 |" for row in MANUAL_ROWS],
        "",
        "## 自动线索",
        "",
        *([f"- {hint}" for hint in automatic_hints(session, fields)] or ["- 无"]),
        "",
        "## 表单字段",
        "",
        "| 标签 | name | 类型 | 必填 | 只读 | 选项（前几个） |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for field in fields:
        options = list(field.get("options", []))
        count = int(field.get("option_count", 0) or 0)
        option_text = joined(options, 5) + (f"（共 {count} 项）" if count > len(options) else "")
        lines.append(
            f"| {cell(field.get('label', ''))} | {cell(field.get('name', ''))} "
            f"| {cell(field.get('type', ''))} | {'是' if field.get('required') else ''} "
            f"| {'是' if field.get('readonly') else ''} | {cell(option_text)} |"
        )
    if not fields:
        lines.append("| （没有结构快照：侦察时点右下角“📸 记录本页结构”） | | | | | |")
    lines += [
        "",
        f"隐藏字段（只有名字）：{cell(joined(hidden))}",
        "",
        f"表头：{cell('；'.join(sorted(tables)) or '—')}",
        "",
        "## 按钮与链接",
        "",
        "| 文字 | 元素 | 危险词 |",
        "| --- | --- | --- |",
    ]
    for button in buttons:
        danger = f"⚠ {button['danger']}" if button.get("danger") else ""
        text, tag = cell(button.get("text", "")), cell(button.get("tag", ""))
        lines.append(f"| {text} | {tag} | {danger} |")
    lines += [
        "",
        "## 接口",
        "",
        "| 方法 | 路径 | 查询参数 | 请求体参数 | 次数 | 护栏 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for group in endpoints:
        guard = "中止：" + "、".join(sorted(group["blocked"])) if group["blocked"] else ""
        lines.append(
            f"| {group['method']} | {cell(group['host'] + group['path'])} "
            f"| {cell(joined(sorted(group['query_keys'])))} "
            f"| {cell(joined(sorted(group['body_keys'])))} | {group['count']} | {guard} |"
        )
    if others:
        summary = "，".join(f"{host} × {count}" for host, count in others.most_common(8))
        lines += ["", f"其他站点的请求（未展开）：{cell(summary)}"]
    lines += [
        "",
        "## 护栏拦截的点击",
        "",
        "| 文字 | 命中词 | 页面 |",
        "| --- | --- | --- |",
    ]
    for event in session.guard:
        lines.append(
            f"| {cell(event.get('text', ''))} | {cell(event.get('word', ''))} "
            f"| {cell(event.get('page', ''))} |"
        )
    lines += ["", "## 结论与疑问（人工填写）", "", "- 待填写", ""]
    return "\n".join(lines)


def find_record_lists(
    obj: Any, path: str = "", min_items: int = 2
) -> list[tuple[str, int, list[str]]]:
    """找出 JSON 里“对象数组”的位置：服务目录、申请记录多半长这样。"""
    found: list[tuple[str, int, list[str]]] = []
    if isinstance(obj, list):
        records = [item for item in obj if isinstance(item, dict)]
        if len(records) >= min_items and len(records) >= 0.8 * len(obj):
            keys = sorted({str(key) for record in records[:20] for key in record})[:15]
            found.append((path or "$", len(obj), keys))
        for index, item in enumerate(obj[:3]):
            found.extend(find_record_lists(item, f"{path}.{index}" if path else str(index)))
    elif isinstance(obj, dict):
        for key, value in obj.items():
            found.extend(find_record_lists(value, f"{path}.{key}" if path else str(key)))
    return found


def get_path(obj: Any, dotted: str) -> Any:
    current = obj
    for part in [p for p in dotted.split(".") if p and p != "$"]:
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise SpikeError(f"路径 {dotted} 在响应里不存在（卡在 {part}）")
    return current


def extract_catalog(obj: Any, list_path: str, mapping: Mapping[str, str]) -> list[dict[str, Any]]:
    records = get_path(obj, list_path)
    if not isinstance(records, list):
        raise SpikeError(f"{list_path} 不是数组")
    return [
        {target: record.get(source) for target, source in mapping.items()}
        for record in records
        if isinstance(record, dict)
    ]


def parse_mapping(pairs: Sequence[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for pair in pairs:
        target, sep, source = pair.partition("=")
        if not sep or not target or not source:
            raise SpikeError(f"--map 的格式是 目标字段=响应字段，例如 name=FWMC；收到 {pair!r}")
        mapping[target] = source
    if "name" not in mapping:
        raise SpikeError("至少要映射 name（事项名称）")
    return mapping


def recon_output_dir(*parts: str) -> Path:
    path = spike_common.data_dir().joinpath("recon", *parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def cmd_service(args: argparse.Namespace) -> int:
    if not SERVICE_ID.fullmatch(args.id):
        raise SpikeError("--id 只能用小写字母、数字、- 和 _，例如 zaidu-zhengming")
    session = load_session(Path(args.session).expanduser())
    out = Path(args.out) if args.out else recon_output_dir("services") / f"{args.id}.md"
    if out.exists() and not args.force:
        raise SpikeError(f"{out} 已存在；确认要覆盖请加 --force")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_service(args.id, args.name or args.id, session), encoding="utf-8")
    print(f"草稿已写入 {out}")
    print("请补全“人工填写”部分并检查脱敏，然后在数据仓库提交；再把事务等级填进 risk-tiers.md。")
    return 0


def cmd_candidates(args: argparse.Namespace) -> int:
    files = [Path(args.response)] if args.response else []
    if args.session:
        files += sorted((Path(args.session) / "responses").glob("*.json"))
    if not files:
        raise SpikeError("请用 --session 或 --response 指定要检查的 JSON")
    rows: list[tuple[int, str, str, list[str]]] = []
    for file in files:
        try:
            obj = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows += [(count, file.name, path, keys) for path, count, keys in find_record_lists(obj)]
    for count, name, path, keys in sorted(rows, reverse=True)[:20]:
        print(f"{name}  {path}  共 {count} 条  字段：{', '.join(keys)}")
    if not rows:
        print("没有找到对象数组：服务目录可能在翻页接口里，换一个页面再侦察一次。")
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    source = Path(args.response)
    obj = json.loads(source.read_text(encoding="utf-8"))
    mapping = parse_mapping(args.map)
    services = extract_catalog(obj, args.list_path, mapping)
    out = Path(args.out) if args.out else recon_output_dir() / "catalog-raw.json"
    document = {
        "exported_at": spike_common.now().isoformat(timespec="seconds"),
        "source": {"response": source.name, "list_path": args.list_path, "map": mapping},
        "count": len(services),
        "services": services,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"导出 {len(services)} 个事项 → {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="整理侦察会话：事务文档草稿与服务目录")
    sub = parser.add_subparsers(dest="command", required=True)

    service = sub.add_parser("service", help="生成事务侦察文档草稿")
    service.add_argument("--session", required=True, help="state/recon/sessions/ 下的会话目录")
    service.add_argument("--id", required=True, help="事务 id，例如 zaidu-zhengming")
    service.add_argument("--name", help="事务名称，例如 在读证明")
    service.add_argument("--out", help="输出路径，默认数据仓库 recon/services/<id>.md")
    service.add_argument("--force", action="store_true", help="覆盖已有文件")

    candidates = sub.add_parser("candidates", help="找出 JSON 响应里的记录列表")
    candidates.add_argument("--session")
    candidates.add_argument("--response")

    catalog = sub.add_parser("catalog", help="从服务列表接口的响应导出服务目录")
    catalog.add_argument("--response", required=True)
    catalog.add_argument("--list-path", required=True, help="记录数组的位置，例如 datas.rows")
    catalog.add_argument("--map", action="append", default=[], help="目标字段=响应字段，可重复")
    catalog.add_argument("--out", help="输出路径，默认数据仓库 recon/catalog-raw.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    commands = {"service": cmd_service, "candidates": cmd_candidates, "catalog": cmd_catalog}
    return commands[args.command](args)


if __name__ == "__main__":
    spike_common.run(main)
