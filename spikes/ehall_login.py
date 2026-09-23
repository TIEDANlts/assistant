"""验证 2：ehall 登录方式、会话寿命与服务器可达性。

子命令（按这个顺序用）：
    reach   不登录，只用普通 HTTP 请求看能否访问 ehall 与统一身份认证（服务器上先跑它）
    login   打开登录页、记录页面结构（不含任何输入值），登录后保存登录态（权限 600）
              --method manual    有界面，你在弹出的浏览器里自己登录（本机推荐，默认）
              --method qr        可无界面：截图二维码，用南京大学 APP 扫码
              --method password  可无界面：用 .env 里的学号密码；24 小时内失败 3 次就拒绝
    check   用已保存的登录态访问一次，判断会话是否有效（退出码 0 有效，1 失效）
    watch   每隔一段时间 check 一次，记录会话能维持多久

截图、页面结构和 Cookie 元数据（不含值）写到 state/recon/ehall_login/；
登录态写到 state/ehall/storage_state.json。都在数据目录里，不入库。
"""

import argparse
import contextlib
import http.cookiejar
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import spike_common
from spike_common import SpikeError

DEFAULT_BASE_URL = "https://ehall.nju.edu.cn/"
DEFAULT_AUTH_HOST = "authserver.nju.edu.cn"
PASSWORD_KIND = "ehall-password"
ATTEMPT_WINDOW = timedelta(hours=24)
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 只读取结构：输入框的类型、name、id、占位符，短提示文字，疑似验证码或二维码的图片。不读任何值。
STRUCTURE_JS = """
() => {
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const attr = (el, name) => el.getAttribute(name) || '';
  const fields = [...document.querySelectorAll('input, select, textarea')].slice(0, 60);
  const inputs = fields.map((el) => ({
    tag: el.tagName.toLowerCase(),
    type: attr(el, 'type').toLowerCase(),
    name: attr(el, 'name'),
    id: el.id || '',
    placeholder: attr(el, 'placeholder'),
    visible: visible(el),
  }));
  const hintRe = /登录|扫码|二维码|验证码|记住|免登录|短信|动态码|忘记|找回|账号|密码/;
  const hints = [];
  const ownText = (el) => [...el.childNodes]
    .filter((node) => node.nodeType === Node.TEXT_NODE)
    .map((node) => node.textContent)
    .join('');
  for (const el of document.querySelectorAll('body *')) {
    if (hints.length >= 40) break;
    if (!visible(el)) continue;
    const text = norm(ownText(el) || (el.childElementCount === 0 ? el.value : ''));
    if (!text || text.length > 20 || !hintRe.test(text)) continue;
    const cls = attr(el, 'class').slice(0, 60);
    hints.push({ tag: el.tagName.toLowerCase(), text, id: el.id || '', cls });
  }
  const imageRe = /captcha|verify|yzm|code|qr/i;
  const describe = (el) => [el.id, attr(el, 'class'), attr(el, 'src'), attr(el, 'alt')].join(' ');
  const images = [...document.querySelectorAll('img, canvas')]
    .filter((el) => imageRe.test(describe(el)))
    .slice(0, 10)
    .map((el) => ({
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      cls: attr(el, 'class').slice(0, 60),
      src: attr(el, 'src').split('?')[0].slice(0, 80),
      visible: visible(el),
    }));
  const frames = [...document.querySelectorAll('iframe')].map((f) => {
    try {
      const u = new URL(f.src, location.href);
      return u.hostname + u.pathname;
    } catch (e) {
      return '';
    }
  });
  const url = location.hostname + location.pathname;
  return { url, title: document.title, inputs, hints, images, frames };
}
"""

# 给唯一可见的账号框、密码框、验证码框和“登录”按钮打上标记，返回各自的数量。
MARK_LOGIN_FIELDS_JS = """
() => {
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const type = (el) => (el.getAttribute('type') || '').toLowerCase();
  const captchaRe = /captcha|yzm|verif|验证码/i;
  const isCaptcha = (el) => captchaRe.test([el.name, el.id, el.placeholder].join(' '));
  for (const el of document.querySelectorAll('[data-recon-role]')) {
    el.removeAttribute('data-recon-role');
  }
  const inputs = [...document.querySelectorAll('input')].filter(visible);
  const passwords = inputs.filter((el) => type(el) === 'password');
  const textTypes = ['', 'text', 'email', 'tel', 'number'];
  const textLike = inputs.filter((el) => textTypes.includes(type(el)));
  const captchas = textLike.filter(isCaptcha);
  const users = textLike.filter((el) => !isCaptcha(el));
  const clickable = 'button, input[type=submit], input[type=button], a, [role=button]';
  const loginRe = /^\\s*登\\s*录\\s*$/;
  const submits = [...document.querySelectorAll(clickable)]
    .filter((el) => visible(el) && loginRe.test(el.innerText || el.value || ''));
  const mark = (el, role) => el && el.setAttribute('data-recon-role', role);
  mark(passwords[0], 'password');
  mark(users[0], 'username');
  mark(captchas[0], 'captcha');
  mark(submits[0], 'submit');
  return {
    passwords: passwords.length,
    users: users.length,
    captchas: captchas.length,
    submits: submits.length,
  };
}
"""

ERROR_TEXT_JS = """
() => {
  const selector = '[id*=error i], [class*=error i], [class*=tip i], [id*=msg i]';
  return [...document.querySelectorAll(selector)]
    .filter((el) => el.offsetWidth || el.offsetHeight)
    .map((el) => (el.innerText || '').trim())
    .filter((text) => text && text.length <= 60)
    .slice(0, 3);
}
"""


@dataclass(frozen=True)
class EhallConfig:
    base_url: str
    auth_host: str

    @classmethod
    def from_env(cls) -> Self:
        return cls(
            base_url=spike_common.env("EHALL_BASE_URL", DEFAULT_BASE_URL),
            auth_host=spike_common.env("EHALL_AUTH_HOST", DEFAULT_AUTH_HOST),
        )


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class PageStatus:
    host: str
    path: str
    title: str
    on_auth_host: bool
    password_visible: bool

    @property
    def logged_in(self) -> bool:
        return not self.on_auth_host and not self.password_visible


def storage_state_path() -> Path:
    return spike_common.state_dir("ehall") / "storage_state.json"


def recon_dir() -> Path:
    return spike_common.state_dir("recon", "ehall_login")


def host_path(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname or ''}{parts.path or '/'}"


def is_auth_host(host: str, auth_host: str) -> bool:
    return host == auth_host or host.endswith("." + auth_host)


# ---------------------------------------------------------------------------- reach


def describe_error(reason: object) -> str:
    if isinstance(reason, socket.gaierror):
        return "DNS 解析失败"
    if isinstance(reason, ConnectionRefusedError):
        return "连接被拒绝"
    if isinstance(reason, TimeoutError):
        return "超时"
    if isinstance(reason, ssl.SSLError):
        return f"TLS 错误：{reason}"
    if isinstance(reason, OSError):
        return f"网络错误：{reason}"
    return str(reason)


def probe_url(url: str, timeout: float) -> dict[str, Any]:
    """普通 HTTP GET（跟随跳转、带 Cookie 罐），只记录能否拿到响应、状态码和最终落点。"""
    if urlsplit(url).scheme not in {"http", "https"}:
        raise SpikeError(f"只支持 http/https 地址：{url}")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    request = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    started = time.monotonic()
    result: dict[str, Any] = {"url": host_path(url)}
    try:
        with opener.open(request, timeout=timeout) as response:
            final = host_path(response.geturl())
            result.update(reachable=True, status=response.status, final=final)
    except urllib.error.HTTPError as exc:
        result.update(reachable=True, status=exc.code, final=host_path(exc.geturl() or url))
    except urllib.error.URLError as exc:
        result.update(reachable=False, error=describe_error(exc.reason))
    except OSError as exc:
        result.update(reachable=False, error=describe_error(exc))
    result["ms"] = round((time.monotonic() - started) * 1000)
    return result


def cmd_reach(args: argparse.Namespace, config: EhallConfig) -> int:
    targets = args.url or [config.base_url, f"https://{config.auth_host}/"]
    results = [probe_url(url, args.timeout) for url in targets]
    record = {
        "ts": spike_common.now().isoformat(timespec="seconds"),
        "where": args.where,
        "results": results,
    }
    spike_common.append_jsonl(spike_common.state_dir("recon") / "ehall_reach.jsonl", record)
    print(f"=== 可公开结论（{args.where}，粘贴到 docs/recon/ehall-login.md）===")
    for item in results:
        if item["reachable"]:
            where = f"HTTP {item['status']}，落点 {item['final']}"
            print(f"- {item['url']}：{where}，{item['ms']} ms")
        else:
            print(f"- {item['url']}：无法访问（{item['error']}），{item['ms']} ms")
    if all(item["reachable"] for item in results):
        print("都能拿到响应。下一步用 login 验证能否真正登录（校外访问可能返回提示页）。")
        return 0
    print("有地址无法访问：可能需要学校 VPN；ehall 能力也许只能在本机部署时启用。")
    return 1


# ---------------------------------------------------------------------------- 浏览器工具


@contextlib.contextmanager
def open_context(*, headless: bool, storage_state: Path | None = None) -> Iterator[BrowserContext]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            yield browser.new_context(
                storage_state=str(storage_state) if storage_state else None,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1280, "height": 860},
            )
        finally:
            with contextlib.suppress(PlaywrightError):
                browser.close()


def settle(page: Page, timeout_ms: int = 10_000) -> None:
    with contextlib.suppress(PlaywrightTimeoutError):
        page.wait_for_load_state("networkidle", timeout=timeout_ms)


def page_status(page: Page, auth_host: str) -> PageStatus:
    visible = False
    for frame in page.frames:
        with contextlib.suppress(PlaywrightError):
            visible = visible or frame.locator("input[type='password']:visible").count() > 0
    title = ""
    with contextlib.suppress(PlaywrightError):
        title = page.title()
    parts = urlsplit(page.url)
    host = parts.hostname or ""
    return PageStatus(host, parts.path or "/", title, is_auth_host(host, auth_host), visible)


def latest_page(context: BrowserContext) -> Page:
    if not context.pages:
        raise SpikeError("浏览器窗口都被关掉了。")
    return context.pages[-1]


def dump_structure(page: Page) -> list[dict[str, Any]]:
    structure: list[dict[str, Any]] = []
    for frame in page.frames:
        try:
            structure.append(frame.evaluate(STRUCTURE_JS))
        except PlaywrightError as exc:
            structure.append({"url": host_path(frame.url), "error": str(exc)[:200]})
    return structure


def summarize_structure(structure: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
    inputs = [item for frame in structure for item in frame.get("inputs", [])]
    images = [item for frame in structure for item in frame.get("images", [])]
    hints = " ".join(item["text"] for frame in structure for item in frame.get("hints", []))
    captcha_re = re.compile(r"captcha|yzm|验证码", re.IGNORECASE)

    def described(item: Mapping[str, Any], *keys: str) -> str:
        return " ".join(str(item.get(key, "")) for key in keys)

    captcha_inputs = [
        i for i in inputs if captcha_re.search(described(i, "name", "id", "placeholder"))
    ]
    captcha_images = [i for i in images if captcha_re.search(described(i, "id", "cls", "src"))]
    qr_images = [i for i in images if re.search("qr", described(i, "id", "cls", "src"), re.I)]
    return {
        "password_field": any(i["type"] == "password" and i["visible"] for i in inputs),
        "captcha": any(i["visible"] for i in [*captcha_inputs, *captcha_images]),
        "captcha_hidden": any(not i["visible"] for i in captcha_inputs),
        "qr": bool(re.search("扫码|二维码", hints)) or bool(qr_images),
        "remember_me": bool(re.search("记住|免登录|天内", hints)),
        "sms": bool(re.search("短信|动态码", hints)),
    }


def cookie_metadata(cookies: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """只保留名字、域、路径和有效期；Cookie 的值永远不写出来。"""
    rows: list[dict[str, Any]] = []
    for cookie in cookies:
        expires = cookie.get("expires", -1)
        if expires is None or expires < 0:
            lifetime = "会话"
        else:
            moment = datetime.fromtimestamp(expires, spike_common.TZ)
            lifetime = moment.isoformat(timespec="minutes")
        rows.append(
            {
                "name": cookie.get("name"),
                "domain": cookie.get("domain"),
                "path": cookie.get("path"),
                "expires": lifetime,
                "http_only": cookie.get("httpOnly"),
                "secure": cookie.get("secure"),
            }
        )
    return rows


def save_storage_state(context: BrowserContext, meta: dict[str, Any]) -> Path:
    path = spike_common.write_json_private(storage_state_path(), context.storage_state())
    spike_common.write_json_private(path.with_name("storage_state.meta.json"), meta)
    return path


# ---------------------------------------------------------------------------- login


def goto_login(page: Page, config: EhallConfig) -> PageStatus:
    page.goto(config.base_url, wait_until="domcontentloaded", timeout=45_000)
    settle(page)
    status = page_status(page, config.auth_host)
    if status.on_auth_host or status.password_visible:
        return status
    # 门户首页不一定直接跳到登录页：点一次“登录”入口（只是导航）
    entry = page.get_by_role("link", name=re.compile("登录")).or_(
        page.get_by_role("button", name=re.compile("登录"))
    )
    if entry.count() > 0:
        entry.first.click()
        settle(page)
    return page_status(latest_page(page.context), config.auth_host)


def login_manual(context: BrowserContext, config: EhallConfig) -> PageStatus:
    print("浏览器已打开。请在浏览器里完成登录（学号密码或南京大学 APP 扫码）。")
    print("看到办事大厅页面后回到这里按回车；想放弃就按 Ctrl+C。")
    input()
    page = latest_page(context)
    settle(page)
    return page_status(page, config.auth_host)


def login_qr(
    context: BrowserContext, config: EhallConfig, timeout: float, shot: Path
) -> PageStatus:
    page = latest_page(context)
    tab = page.get_by_text(re.compile("扫码登录|二维码登录|APP扫码")).first
    with contextlib.suppress(PlaywrightError):
        if tab.count() > 0:
            tab.click(timeout=5_000)
    deadline = time.monotonic() + timeout
    last_shot = 0.0
    while time.monotonic() < deadline:
        page = latest_page(context)
        if time.monotonic() - last_shot >= 30:
            spike_common.write_private(shot, page.screenshot(full_page=True))
            last_shot = time.monotonic()
            print(f"二维码截图已更新：{shot}（服务器上可用 scp 取回后扫码）")
        page.wait_for_timeout(2_000)
        status = page_status(page, config.auth_host)
        if status.logged_in:
            return status
    return page_status(latest_page(context), config.auth_host)


def login_password(
    context: BrowserContext,
    config: EhallConfig,
    credentials: Credentials,
    args: argparse.Namespace,
    shot: Path,
) -> tuple[PageStatus, list[str]]:
    page = latest_page(context)
    marked: dict[str, int] | None = None
    frame = page.main_frame
    for candidate in page.frames:
        with contextlib.suppress(PlaywrightError):
            counts = candidate.evaluate(MARK_LOGIN_FIELDS_JS)
            if counts["passwords"] == 1 and counts["users"] >= 1:
                marked, frame = counts, candidate
                break
    if marked is None:
        raise SpikeError("没找到唯一可见的密码框和账号框，不猜。请改用 --method manual 或 qr。")
    captcha = ""
    if marked["captchas"]:
        spike_common.write_private(shot, page.screenshot(full_page=True))
        print(f"登录页要求验证码，截图：{shot}")
        if not spike_common.confirm("已查看截图，现在输入验证码？"):
            raise SpikeError("需要验证码但没有输入，已放弃（本次不计入尝试次数）。")
        captcha = input("验证码：").strip()
    failures = spike_common.count_attempts(PASSWORD_KIND, ATTEMPT_WINDOW, ["failed"])
    question = (
        f"将在 {host_path(frame.url)} 填写账号 {spike_common.redact(credentials.username)} "
        f"和密码并点击登录（24 小时内已失败 {failures} 次，上限 {args.max_attempts}），继续？"
    )
    if not spike_common.confirm(question, assume_yes=args.yes):
        raise SpikeError("已取消。")
    frame.locator("[data-recon-role=username]").fill(credentials.username)
    frame.locator("[data-recon-role=password]").fill(credentials.password)
    if captcha:
        frame.locator("[data-recon-role=captcha]").fill(captcha)
    if marked["submits"]:
        frame.locator("[data-recon-role=submit]").click()
    else:
        frame.locator("[data-recon-role=password]").press("Enter")
    deadline = time.monotonic() + args.wait
    status = page_status(latest_page(context), config.auth_host)
    while time.monotonic() < deadline and not status.logged_in:
        latest_page(context).wait_for_timeout(500)
        status = page_status(latest_page(context), config.auth_host)
    errors: list[str] = []
    if not status.logged_in:
        with contextlib.suppress(PlaywrightError):
            texts = latest_page(context).evaluate(ERROR_TEXT_JS)
            errors = [spike_common.redact(str(text)) for text in texts]
    outcome = "ok" if status.logged_in else "failed"
    spike_common.record_attempt(PASSWORD_KIND, outcome, "；".join(errors))
    return status, errors


OBSERVATION_QUESTIONS = (
    ("method_used", "这次实际用的登录方式？", ("学号 + 密码", "南京大学 APP 扫码", "其他")),
    ("challenge", "登录时出现了什么验证？", ("没有", "图片验证码", "滑块或拼图", "短信或动态码")),
    ("remember_me", "有没有“记住我”之类的选项？", ("没有", "有，勾选了", "有，没勾选")),
    ("album_qr", "南京大学 APP 能否识别相册里的二维码？", ("能", "不能", "没试")),
)


def ask_observations() -> dict[str, str]:
    print("\n补充几项观察（直接回车可跳过）：")
    return {key: spike_common.choose(text, options) for key, text, options in OBSERVATION_QUESTIONS}


def _has(flag: object) -> str:
    return "有" if flag else "无"


def login_conclusions(obs: Mapping[str, Any]) -> list[str]:
    summary = obs["structure_summary"]
    hidden = "（另有隐藏的验证码框，可能在失败后出现）" if summary["captcha_hidden"] else ""
    result = "成功" if obs["logged_in"] else "未成功"
    clues = (
        f"密码框{_has(summary['password_field'])}，验证码{_has(summary['captcha'])}{hidden}，"
        f"扫码入口{_has(summary['qr'])}，记住我{_has(summary['remember_me'])}，"
        f"短信/动态码{_has(summary['sms'])}"
    )
    lines = [
        f"- 环境：{obs['where']}；方式：{obs['method']}；结果：{result}，用时 {obs['seconds']} 秒",
        f"- 登录页：{obs['login_page']}（从 {obs['landing']} 进入）",
        f"- 页面线索：{clues}",
    ]
    cookies = "；".join(f"{c['name']}@{c['domain']}（{c['expires']}）" for c in obs["cookies"])
    lines.append(f"- 登录后的 Cookie（名字@域（有效期），不含值）：{cookies or '无'}")
    if obs.get("errors"):
        lines.append(f"- 登录页提示：{'；'.join(obs['errors'])}")
    labels = {key: text for key, text, _ in OBSERVATION_QUESTIONS}
    for key, answer in obs.get("answers", {}).items():
        if answer != "未回答":
            lines.append(f"- {labels[key]} {answer}")
    return lines


def cmd_login(args: argparse.Namespace, config: EhallConfig) -> int:
    if args.method == "manual" and args.headless:
        raise SpikeError("manual 需要有界面的浏览器；服务器上请用 --method qr 或 password。")
    credentials: Credentials | None = None
    if args.method == "password":
        credentials = Credentials(
            spike_common.require_env("EHALL_USERNAME"),
            spike_common.require_env("EHALL_PASSWORD"),
        )
        spike_common.check_attempts(
            PASSWORD_KIND, limit=args.max_attempts, window=ATTEMPT_WINDOW, outcomes=["failed"]
        )
    out = recon_dir()
    ts = spike_common.stamp()
    errors: list[str] = []
    with open_context(headless=args.headless) as context:
        page = context.new_page()
        started = time.monotonic()
        entry = goto_login(page, config)
        structure = dump_structure(latest_page(context))
        screenshot = latest_page(context).screenshot(full_page=True)
        spike_common.write_private(out / f"{ts}-login.png", screenshot)
        if entry.logged_in:
            print("打开后没有进入登录页：可能已登录，或 EHALL_BASE_URL 不需要登录。")
        if args.method == "manual":
            final = login_manual(context, config)
        elif args.method == "qr":
            final = login_qr(context, config, args.timeout, out / f"{ts}-qr.png")
        elif credentials is not None:
            shot = out / f"{ts}-captcha.png"
            final, errors = login_password(context, config, credentials, args, shot)
        else:
            raise SpikeError(f"未知的登录方式：{args.method}")
        seconds = round(time.monotonic() - started, 1)
        cookies = cookie_metadata(context.cookies())
        if final.logged_in:
            meta = {
                "saved_at": spike_common.now().isoformat(timespec="seconds"),
                "method": args.method,
                "where": args.where,
                "base_url": config.base_url,
            }
            saved = save_storage_state(context, meta)
            print(f"登录态已保存（权限 600）：{saved}")
    observations = {
        "ts": ts,
        "where": args.where,
        "method": args.method,
        "landing": host_path(config.base_url),
        "redirected_to_login": entry.on_auth_host,
        "login_page": next((s.get("url", "") for s in structure if "inputs" in s), ""),
        "structure": structure,
        "structure_summary": summarize_structure(structure),
        "logged_in": final.logged_in,
        "final_page": f"{final.host}{final.path}",
        "seconds": seconds,
        "cookies": cookies,
        "errors": errors,
        "answers": {} if args.no_questions else ask_observations(),
    }
    path = spike_common.write_json_private(out / f"{ts}-observations.json", observations)
    print("\n=== 可公开结论（粘贴到 docs/recon/ehall-login.md）===")
    print("\n".join(login_conclusions(observations)))
    print(f"\n页面结构、截图与观察记录（私密）：{path.parent}")
    return 0 if final.logged_in else 1


# ---------------------------------------------------------------------------- check / watch


def check_session(config: EhallConfig, target: str, *, save: bool) -> dict[str, Any]:
    state = storage_state_path()
    if not state.is_file():
        raise SpikeError("还没有登录态：先运行 login。")
    with open_context(headless=True, storage_state=state) as context:
        page = context.new_page()
        started = time.monotonic()
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=45_000)
        except PlaywrightError as exc:
            raise SpikeError(f"访问 {host_path(target)} 失败：{exc}") from exc
        settle(page)
        status = page_status(latest_page(context), config.auth_host)
        if status.logged_in and save:
            meta = {"refreshed_at": spike_common.now().isoformat(timespec="seconds")}
            save_storage_state(context, meta)
        return {
            "ts": spike_common.now().isoformat(timespec="seconds"),
            "valid": status.logged_in,
            "page": f"{status.host}{status.path}",
            "seconds": round(time.monotonic() - started, 1),
        }


def cmd_check(args: argparse.Namespace, config: EhallConfig) -> int:
    result = check_session(config, args.probe_url or config.base_url, save=not args.no_save)
    verdict = "有效" if result["valid"] else "已失效（被带到统一身份认证）"
    print(f"会话{verdict}；落点 {result['page']}，用时 {result['seconds']} 秒")
    if result["valid"] and not args.probe_url:
        print("提示：依据是有没有被带去登录页。请用 --probe-url 指向必须登录才能看的页面。")
    return 0 if result["valid"] else 1


def cmd_watch(args: argparse.Namespace, config: EhallConfig) -> int:
    log = spike_common.state_dir("recon") / "ehall_session.jsonl"
    started = time.monotonic()
    target = args.probe_url or config.base_url
    while True:
        result = check_session(config, target, save=True)
        hours = round((time.monotonic() - started) / 3600, 2)
        record = {**result, "elapsed_hours": hours, "interval_min": args.interval}
        spike_common.append_jsonl(log, record)
        print(f"[{result['ts']}] {'有效' if result['valid'] else '失效'}（已观察 {hours} 小时）")
        if not result["valid"]:
            print(
                f"=== 可公开结论 === 每 {args.interval:g} 分钟访问一次时，会话约 {hours} 小时后失效"
                "（访问本身可能延长会话：结论是“保活时能维持多久”）。"
            )
            return 1
        if hours >= args.max_hours:
            print(f"=== 可公开结论 === 每 {args.interval:g} 分钟访问一次，{hours} 小时后仍有效。")
            return 0
        time.sleep(args.interval * 60)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="验证 2：ehall 登录方式、会话寿命与服务器可达性")
    sub = parser.add_subparsers(dest="command", required=True)

    reach = sub.add_parser("reach", help="不登录，只看能否访问 ehall 与统一身份认证")
    reach.add_argument("--url", action="append", help="要探测的地址，可重复")
    reach.add_argument("--timeout", type=float, default=10.0)
    reach.add_argument("--where", default="本机", help="环境标签，例如 本机、服务器")

    login = sub.add_parser("login", help="登录并保存登录态")
    login.add_argument("--method", choices=["manual", "qr", "password"], default="manual")
    login.add_argument("--headless", action="store_true", help="无界面（服务器）")
    login.add_argument("--timeout", type=float, default=300.0, help="扫码最多等待的秒数")
    login.add_argument("--wait", type=float, default=20.0, help="密码登录后最多等待的秒数")
    login.add_argument("--max-attempts", type=int, default=3, help="24 小时内允许的失败次数")
    login.add_argument("--yes", action="store_true", help="密码登录前不再询问")
    login.add_argument("--no-questions", action="store_true", help="结束后不问观察问题")
    login.add_argument("--where", default="本机", help="环境标签")

    check = sub.add_parser("check", help="判断已保存的会话是否有效")
    check.add_argument("--probe-url", help="用来判断的页面，最好必须登录才能看")
    check.add_argument("--no-save", action="store_true", help="不回写刷新后的登录态")

    watch = sub.add_parser("watch", help="定期 check，记录会话寿命")
    watch.add_argument("--interval", type=float, default=30.0, help="检查间隔（分钟）")
    watch.add_argument("--max-hours", type=float, default=24.0)
    watch.add_argument("--probe-url")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = EhallConfig.from_env()
    commands = {"reach": cmd_reach, "login": cmd_login, "check": cmd_check, "watch": cmd_watch}
    return commands[args.command](args, config)


if __name__ == "__main__":
    spike_common.run(main)
