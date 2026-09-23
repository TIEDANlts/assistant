"""验证 3–4：受保护的只读侦察浏览器。

    uv run python spikes/ehall_recon.py --label catalog
    uv run python spikes/ehall_recon.py --label zaidu-zhengming --start-url <事务入口>

用已保存的登录态（先运行 ehall_login.py login）打开有界面的浏览器。
你像平时一样浏览，脚本在旁边记录：
- 每个请求的方法、路径、查询参数名和请求体参数名，不记录值 → requests.jsonl
- 学校站点返回的 JSON 原文（私密，供 recon_summarize.py 提取服务目录）→ responses/
- 页面右下角“📸 记录本页结构”：表单字段、按钮文字、表头，不含填写的值 → forms/

护栏只是兜底，侦察时仍然不要点任何会改变状态的按钮：
- 文字含“提交、保存、暂存、删除、撤销、撤回、退、取消、确认……”的点击会被拦下，页面顶部出现红色提示
- PUT/PATCH/DELETE、multipart 上传、路径像写接口（save、submit、delete、tj、bc……）的请求会被中止
- 查询接口被误拦时，用 --allow <正则> 放行（上传与写方法仍然拦截）

输出在 $ASSISTANT_DATA_DIR/state/recon/sessions/<时间>-<label>/，不入库。关掉浏览器窗口即结束。
"""

import argparse
import contextlib
import ipaddress
import json
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from playwright.sync_api import (
    BrowserContext,
    Playwright,
    Request,
    Response,
    Route,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError

import spike_common
from spike_common import SpikeError

DEFAULT_BASE_URL = "https://ehall.nju.edu.cn/"
MAX_SAVED_BODY = 5 * 1024 * 1024
WRITE_METHODS = frozenset({"PUT", "PATCH", "DELETE"})
DANGER_WORDS = (
    "提交", "确认", "确定", "保存", "暂存", "删除", "撤销", "撤回", "退", "取消", "注销", "修改",
    "上传", "发送", "报名", "预约", "同意", "签署", "绑定", "解绑", "缴费", "支付",
    "submit", "save", "delete", "remove", "confirm", "cancel", "withdraw", "upload", "pay",
)  # fmt: skip
WRITE_TOKENS = frozenset(
    {
        "save", "submit", "delete", "del", "remove", "cancel", "revoke", "withdraw", "update",
        "insert", "add", "create", "apply", "commit", "approve", "reject", "upload", "modify",
        "edit", "drop", "confirm", "pay", "bind", "unbind", "sign",
        # 常见的拼音缩写：提交、保存、删除、撤回、修改、退课、暂存、取消
        "tj", "bc", "sc", "ch", "xg", "tk", "zc", "qx",
    }
)  # fmt: skip
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_EXTENSION = re.compile(r"\.(do|action|json|jsp|php|aspx?|html?)$", re.IGNORECASE)

GUARD_JS = r"""
(() => {
  if (window.__reconGuardInstalled) return;
  window.__reconGuardInstalled = true;
  const DANGER = __DANGER_WORDS__;
  const CLICKABLE = [
    'button', 'a', 'input[type=submit]', 'input[type=button]', 'input[type=reset]',
    'input[type=image]', '[role=button]', '[role=menuitem]', '[onclick]',
  ].join(', ');
  const FORM_CONTROL = [
    'input:not([type=submit]):not([type=button]):not([type=reset]):not([type=image])',
    'textarea', 'select', 'option', 'label', '[contenteditable=true]',
  ].join(', ');
  const norm = (s) => (s || '').replace(/\s+/g, '');
  const labelOf = (el) => norm(
    el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '',
  ).slice(0, 40);
  const dangerIn = (text) => {
    const lower = text.toLowerCase();
    return DANGER.find((word) => lower.includes(word.toLowerCase())) || null;
  };
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);

  // ---------------------------------------------------------------- 点击护栏
  const findDanger = (target) => {
    if (!(target instanceof Element) || target.closest('[data-recon-ui]')) return null;
    if (target.closest(FORM_CONTROL)) return null;
    const clickable = target.closest(CLICKABLE);
    if (clickable) {
      const text = labelOf(clickable);
      const word = dangerIn(text);
      return word ? { word, text, tag: clickable.tagName.toLowerCase() } : null;
    }
    // 没有语义的可点击元素（div + 事件监听）：向上找最近的短文字
    let el = target;
    for (let depth = 0; el && depth < 4; depth += 1, el = el.parentElement) {
      const text = labelOf(el);
      if (!text || text.length > 20) continue;
      const word = dangerIn(text);
      if (word) return { word, text, tag: el.tagName.toLowerCase() };
    }
    return null;
  };
  const banner = (message) => {
    let el = document.getElementById('__recon_banner');
    if (!el) {
      el = document.createElement('div');
      el.id = '__recon_banner';
      el.setAttribute('data-recon-ui', '1');
      el.style.cssText = [
        'position:fixed', 'top:8px', 'left:50%', 'transform:translateX(-50%)',
        'z-index:2147483647', 'background:#b3261e', 'color:#fff', 'padding:8px 14px',
        'border-radius:6px', 'font:14px/1.4 sans-serif', 'pointer-events:none',
      ].join(';');
      (document.body || document.documentElement).appendChild(el);
    }
    el.textContent = message;
    el.style.display = 'block';
    clearTimeout(el.__reconTimer);
    el.__reconTimer = setTimeout(() => { el.style.display = 'none'; }, 3000);
  };
  let last = { key: '', at: 0 };
  const block = (event, hit) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    const now = Date.now();
    if (hit.text === last.key && now - last.at < 800) return;
    last = { key: hit.text, at: now };
    banner(`侦察模式：已拦截「${hit.text}」（含“${hit.word}”）`);
    if (typeof window.__reconGuardLog === 'function') {
      window.__reconGuardLog({ ...hit, event: event.type, page: location.pathname });
    }
  };
  const POINTER_EVENTS = [
    'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'dblclick',
    'touchstart', 'touchend',
  ];
  for (const type of POINTER_EVENTS) {
    window.addEventListener(type, (event) => {
      const hit = findDanger(event.target);
      if (hit) block(event, hit);
    }, { capture: true, passive: false });
  }
  window.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const hit = findDanger(event.target);
    if (hit) block(event, hit);
  }, { capture: true });
  window.addEventListener('submit', (event) => {
    const hit = event.submitter ? findDanger(event.submitter) : null;
    if (hit) block(event, hit);
  }, { capture: true });

  // ---------------------------------------------------------------- 结构快照（不读任何值）
  const fieldLabel = (el, doc) => {
    const id = el.getAttribute('id');
    if (id) {
      const byFor = doc.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (byFor) return norm(byFor.innerText);
    }
    const wrapper = el.closest('label');
    if (wrapper) return norm(wrapper.innerText);
    const own = el.getAttribute('aria-label') || el.getAttribute('placeholder')
      || el.getAttribute('title');
    if (own) return norm(own);
    let box = el.parentElement;
    for (let depth = 0; box && depth < 3; depth += 1, box = box.parentElement) {
      const cand = box.querySelector('label, th, dt, .label, [class*=label], [class*=title]');
      if (cand && !cand.contains(el)) {
        const text = norm(cand.innerText);
        if (text && text.length <= 30) return text;
      }
    }
    return '';
  };
  const describeField = (el, doc) => {
    const tag = el.tagName.toLowerCase();
    let type = tag;
    if (tag === 'input') type = (el.getAttribute('type') || 'text').toLowerCase();
    else if (tag !== 'select' && tag !== 'textarea') type = 'contenteditable';
    const label = fieldLabel(el, doc);
    const options = tag === 'select' ? [...el.options] : [];
    const required = !!el.required || el.getAttribute('aria-required') === 'true'
      || /[*＊]/.test(label);
    return {
      label,
      type,
      name: el.getAttribute('name') || '',
      id: el.getAttribute('id') || '',
      required,
      readonly: !!(el.readOnly || el.disabled),
      visible: visible(el),
      options: options.slice(0, 8).map((o) => norm(o.text)).filter(Boolean),
      option_count: options.length,
      accept: el.getAttribute('accept') || '',
    };
  };
  const collect = (doc, frame) => {
    const fields = [...doc.querySelectorAll('input, select, textarea, [contenteditable=true]')]
      .filter((el) => (el.getAttribute('type') || '').toLowerCase() !== 'hidden')
      .slice(0, 200)
      .map((el) => describeField(el, doc));
    const hidden = [...doc.querySelectorAll('input[type=hidden]')]
      .map((el) => el.getAttribute('name') || el.getAttribute('id') || '')
      .filter(Boolean)
      .slice(0, 50);
    const buttons = [...doc.querySelectorAll(CLICKABLE)]
      .filter(visible)
      .map((el) => ({ text: labelOf(el), tag: el.tagName.toLowerCase() }))
      .filter((b) => b.text)
      .slice(0, 80)
      .map((b) => ({ ...b, danger: dangerIn(b.text) }));
    const tables = [...doc.querySelectorAll('table')]
      .slice(0, 8)
      .map((t) => [...t.querySelectorAll('th')].map((th) => norm(th.innerText)).filter(Boolean))
      .filter((headers) => headers.length)
      .map((headers) => headers.slice(0, 20));
    const where = doc.location ? doc.location.hostname + doc.location.pathname : '';
    return { frame, url: where, title: doc.title, fields, hidden, buttons, tables };
  };
  window.__reconTakeSnapshot = () => {
    const frames = [];
    const visit = (win, path) => {
      try {
        frames.push(collect(win.document, path));
      } catch (e) {
        frames.push({ frame: path, error: 'cross-origin' });
        return;
      }
      for (let i = 0; i < win.frames.length; i += 1) visit(win.frames[i], `${path}/${i}`);
    };
    visit(window, 'top');
    const payload = { page: location.hostname + location.pathname, title: document.title, frames };
    if (typeof window.__reconSnapshot === 'function') return window.__reconSnapshot(payload);
    return payload;
  };

  // ---------------------------------------------------------------- 工具条（只在顶层页面）
  const mountToolbar = () => {
    if (window.top !== window || document.getElementById('__recon_toolbar')) return;
    const bar = document.createElement('div');
    bar.id = '__recon_toolbar';
    bar.setAttribute('data-recon-ui', '1');
    bar.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:2147483647;';
    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('data-recon-ui', '1');
    button.textContent = '📸 记录本页结构';
    button.style.cssText = 'padding:6px 10px;border:0;border-radius:6px;'
      + 'background:#1f6feb;color:#fff;font:13px sans-serif;cursor:pointer;';
    button.addEventListener('click', async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const result = await window.__reconTakeSnapshot();
      banner(result && result.saved ? `已记录：${result.saved}` : '已记录');
    });
    bar.appendChild(button);
    document.documentElement.appendChild(bar);
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountToolbar);
  } else {
    mountToolbar();
  }
})();
"""


@dataclass(frozen=True)
class Decision:
    blocked: bool
    reason: str


def guard_script() -> str:
    return GUARD_JS.replace("__DANGER_WORDS__", json.dumps(list(DANGER_WORDS), ensure_ascii=False))


def path_tokens(url: str) -> list[str]:
    """URL 路径最后两段拆成小写词：T_SQ_SAVE.do → t sq save；getSqList.do → get sq list。"""
    segments = [segment for segment in urlsplit(url).path.split("/") if segment][-2:]
    tokens: list[str] = []
    for segment in segments:
        for part in re.split(r"[^A-Za-z0-9]+", _EXTENSION.sub("", segment)):
            tokens.extend(token.lower() for token in _CAMEL.findall(part))
    return tokens


def classify_request(
    method: str,
    url: str,
    resource_type: str,
    content_type: str,
    allow: Sequence[re.Pattern[str]] = (),
) -> Decision:
    method = method.upper()
    if method in WRITE_METHODS:
        return Decision(True, f"写方法 {method}")
    if resource_type in spike_common.STATIC_RESOURCE_TYPES:
        return Decision(False, "静态资源")
    if method == "POST" and "multipart/form-data" in content_type.lower():
        return Decision(True, "文件上传（multipart）")
    if any(pattern.search(url) for pattern in allow):
        return Decision(False, "白名单")
    hits = [token for token in path_tokens(url) if token in WRITE_TOKENS]
    if hits:
        return Decision(True, f"疑似写接口（{hits[0]}）")
    return Decision(False, "放行")


def body_keys(content_type: str, raw: bytes | None) -> list[str]:
    """请求体里的参数名；永远不返回值。"""
    if not raw:
        return []
    kind = content_type.lower()
    if "multipart/form-data" in kind:
        return ["<multipart>"]
    text = raw[:200_000].decode("utf-8", errors="replace")
    if "json" in kind or text.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return ["<json>"]
        return sorted(str(key) for key in parsed) if isinstance(parsed, dict) else ["<json-array>"]
    if "x-www-form-urlencoded" in kind or "=" in text:
        return sorted({key for key, _ in parse_qsl(text, keep_blank_values=True)})
    return ["<raw>"]


def default_target_suffix(base_url: str) -> str:
    """默认只保存学校站点的 JSON：ehall.nju.edu.cn → nju.edu.cn。"""
    host = urlsplit(base_url).hostname or ""
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(host)
        return host
    labels = host.split(".")
    if host == "localhost" or len(labels) <= 2:
        return host
    keep = 3 if host.endswith((".edu.cn", ".com.cn", ".org.cn", ".gov.cn")) else 2
    return ".".join(labels[-keep:])


def safe_label(label: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_-]+", "-", label).strip("-")[:40]
    if not cleaned:
        raise SpikeError("--label 只能用字母、数字、- 和 _（例如 catalog、zaidu-zhengming）")
    return cleaned


class Recorder:
    """把请求、响应、护栏事件和结构快照写到会话目录（全部私密）。"""

    def __init__(self, session_dir: Path, target_suffixes: Sequence[str]) -> None:
        self.dir = session_dir
        self.target_suffixes = tuple(target_suffixes)
        self.counts = {"requests": 0, "blocked": 0, "responses": 0, "forms": 0, "guard": 0}
        session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def is_target(self, host: str) -> bool:
        return any(host == s or host.endswith("." + s) for s in self.target_suffixes)

    def on_request(self, request: Request, decision: Decision) -> None:
        parts = urlsplit(request.url)
        content_type = request.headers.get("content-type", "")
        record = {
            "ts": spike_common.now().isoformat(timespec="seconds"),
            "method": request.method,
            "host": parts.hostname or "",
            "path": parts.path,
            "query_keys": sorted({k for k, _ in parse_qsl(parts.query, keep_blank_values=True)}),
            "body_keys": body_keys(content_type, request.post_data_buffer),
            "resource_type": request.resource_type,
            "blocked": decision.blocked,
            "reason": decision.reason,
        }
        spike_common.append_jsonl(self.dir / "requests.jsonl", record)
        self.counts["requests"] += 1
        if decision.blocked:
            self.counts["blocked"] += 1
            print(f"✋ 已中止请求 {request.method} {parts.path}（{decision.reason}）")

    def on_response(self, response: Response) -> None:
        parts = urlsplit(response.url)
        host = parts.hostname or ""
        request = response.request
        if not self.is_target(host) or request.resource_type in spike_common.STATIC_RESOURCE_TYPES:
            return
        content_type = response.headers.get("content-type", "")
        record: dict[str, Any] = {
            "ts": spike_common.now().isoformat(timespec="seconds"),
            "method": request.method,
            "host": host,
            "path": parts.path,
            "status": response.status,
            "content_type": content_type.split(";")[0],
        }
        if "json" in content_type.lower():
            with contextlib.suppress(PlaywrightError):
                body = response.body()
                if len(body) <= MAX_SAVED_BODY:
                    self.counts["responses"] += 1
                    name = f"{self.counts['responses']:04d}.json"
                    spike_common.write_private(self.dir / "responses" / name, body)
                    record["saved"] = name
        spike_common.append_jsonl(self.dir / "responses.jsonl", record)

    def on_guard(self, info: dict[str, Any]) -> None:
        keys = ("word", "text", "tag", "event", "page")
        record = {"ts": spike_common.now().isoformat(timespec="seconds")}
        record.update({key: str(info.get(key, "")) for key in keys})
        spike_common.append_jsonl(self.dir / "guard.jsonl", record)
        self.counts["guard"] += 1
        print(f"✋ 已拦截点击「{record['text']}」（含“{record['word']}”）")

    def on_snapshot(self, payload: dict[str, Any]) -> dict[str, str]:
        self.counts["forms"] += 1
        name = f"forms/{self.counts['forms']:03d}.json"
        spike_common.write_json_private(self.dir / name, payload)
        print(f"📸 已记录页面结构：{payload.get('title', '')} → {name}")
        return {"saved": name}


def handle_route(route: Route, recorder: Recorder, allow: Sequence[re.Pattern[str]]) -> None:
    request = route.request
    content_type = request.headers.get("content-type", "")
    decision = classify_request(
        request.method, request.url, request.resource_type, content_type, allow
    )
    recorder.on_request(request, decision)
    if decision.blocked:
        route.abort("blockedbyclient")
    else:
        route.continue_()


@contextlib.contextmanager
def guarded_context(
    playwright: Playwright,
    recorder: Recorder,
    *,
    headless: bool,
    storage_state: Path | None,
    allow: Sequence[re.Pattern[str]],
    har_path: Path | None,
) -> Iterator[BrowserContext]:
    browser = playwright.chromium.launch(headless=headless)
    context: BrowserContext | None = None
    har: dict[str, Any] = {}
    if har_path is not None:
        har = {"record_har_path": str(har_path), "record_har_content": "embed"}
    try:
        context = browser.new_context(
            storage_state=str(storage_state) if storage_state else None,
            service_workers="block",
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1366, "height": 900},
            **har,
        )
        context.expose_binding("__reconGuardLog", lambda _source, info: recorder.on_guard(info))
        context.expose_binding(
            "__reconSnapshot", lambda _source, payload: recorder.on_snapshot(payload)
        )
        context.add_init_script(script=guard_script())
        context.route("**/*", lambda route: handle_route(route, recorder, allow))
        context.on("response", recorder.on_response)
        yield context
    finally:
        if context is not None:
            with contextlib.suppress(PlaywrightError):
                context.close()
        with contextlib.suppress(PlaywrightError):
            browser.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="验证 3–4：受保护的只读侦察浏览器")
    parser.add_argument("--label", required=True, help="会话标签，如 catalog 或事务 id")
    parser.add_argument("--start-url", help="起始页面，默认 EHALL_BASE_URL")
    parser.add_argument("--allow", action="append", default=[], help="放行的 URL 正则，可重复")
    parser.add_argument("--target", action="append", help="保存其 JSON 响应的域名后缀，可重复")
    parser.add_argument("--har", action="store_true", help="另存完整 HAR（含 Cookie，只在 state/）")
    parser.add_argument("--headless", action="store_true", help="无界面（仅用于测试）")
    parser.add_argument("--duration", type=float, help="最多运行的秒数（默认直到关闭窗口）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    label = safe_label(args.label)
    base_url = spike_common.env("EHALL_BASE_URL", DEFAULT_BASE_URL)
    state = spike_common.state_dir("ehall") / "storage_state.json"
    if not state.is_file():
        raise SpikeError("还没有登录态：先运行 uv run python spikes/ehall_login.py login")
    try:
        allow = [re.compile(pattern) for pattern in args.allow]
    except re.error as exc:
        raise SpikeError(f"--allow 不是合法的正则：{exc}") from exc
    session_dir = spike_common.state_dir("recon", "sessions", f"{spike_common.stamp()}-{label}")
    targets = args.target or [default_target_suffix(base_url)]
    start_url = args.start_url or base_url
    meta: dict[str, Any] = {
        "label": label,
        "start_url": start_url,
        "target_hosts": targets,
        "allow": args.allow,
        "started": spike_common.now().isoformat(timespec="seconds"),
    }
    spike_common.write_json_private(session_dir / "meta.json", meta)
    recorder = Recorder(session_dir, targets)
    print(f"侦察会话：{session_dir}")
    print("像平时一样浏览；右下角“📸 记录本页结构”保存当前表单结构。关闭浏览器窗口即结束。")
    print("护栏会拦截危险点击与疑似写请求，但它只是兜底：不要点任何会改变状态的按钮。")
    har_path = session_dir / "session.har" if args.har else None
    with (
        sync_playwright() as playwright,
        guarded_context(
            playwright,
            recorder,
            headless=args.headless,
            storage_state=state,
            allow=allow,
            har_path=har_path,
        ) as context,
    ):
        page = context.new_page()
        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
        deadline = time.monotonic() + args.duration if args.duration else None
        with contextlib.suppress(PlaywrightError):
            while context.pages and (deadline is None or time.monotonic() < deadline):
                context.pages[0].wait_for_timeout(500)
        with contextlib.suppress(PlaywrightError):
            spike_common.write_json_private(state, context.storage_state())
    meta.update(ended=spike_common.now().isoformat(timespec="seconds"), counts=recorder.counts)
    spike_common.write_json_private(session_dir / "meta.json", meta)
    counts = recorder.counts
    print(
        f"已结束：请求 {counts['requests']} 个（中止 {counts['blocked']} 个），"
        f"JSON 响应 {counts['responses']} 份，结构快照 {counts['forms']} 份，"
        f"拦截点击 {counts['guard']} 次。"
    )
    print("下一步：")
    script = "uv run python spikes/recon_summarize.py"
    print(f"  {script} service --session {session_dir} --id <事务id>")
    print(f"  {script} candidates --session {session_dir}")
    return 0


if __name__ == "__main__":
    spike_common.run(main)
