"""本地假站点：模拟统一身份认证（localhost）与办事大厅（127.0.0.1），供浏览器测试使用。

同一个端口、两个主机名：浏览器按主机名隔离 Cookie，正好复现“认证服务器与业务系统是两个站点”
的票据流程。页面内容全部虚构。
"""

import html
import json
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>统一身份认证</title></head>
<body>
<div class="tabs"><span id="tab-pwd">账号登录</span><span id="tab-qr">扫码登录</span></div>
<form id="loginForm" method="post" action="/authserver/login">
  <input type="hidden" name="service" value="{service}">
  <input type="hidden" name="execution" value="e1s1">
  <input id="username" name="username" type="text" placeholder="学号/工号">
  <input id="password" name="password" type="password" placeholder="密码">
  <div id="captchaDiv" style="display:none">
    <input id="captcha" name="captcha" type="text" placeholder="验证码">
    <img id="captchaImg" src="/authserver/getCaptcha.htl">
  </div>
  <label><input type="checkbox" name="rememberMe"> 七天内免登录</label>
  <a id="login_submit" href="javascript:void(0)"
     onclick="document.getElementById('loginForm').submit()">登录</a>
  <p class="error-tip">{error}</p>
</form>
</body></html>"""

PORTAL_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>办事大厅</title></head>
<body><h1>办事大厅</h1><a href="/recon/form">在读证明申请</a></body></html>"""

RECON_FORM_PAGE = """<!doctype html><html>
<head><meta charset="utf-8"><title>在读证明申请</title></head>
<body>
<h1>在读证明申请</h1>
<form id="apply" onsubmit="return false">
  <div class="row"><label for="purpose">用途*</label>
    <input id="purpose" name="purpose" required></div>
  <div class="row"><span class="label">修改原因</span>
    <input id="reason" name="reason"></div>
  <div class="row"><label for="lang">语言</label>
    <select id="lang" name="lang"><option>中文</option><option>英文</option></select></div>
  <div class="row"><label for="att">附件</label><input id="att" name="att" type="file"></div>
  <input type="hidden" name="WID" value="hidden-wid-value">
</form>
<button id="query" type="button">查询</button>
<button id="save" type="button">暂存</button>
<button id="submit" type="button">提交</button>
<div id="withdraw" class="op">撤回</div>
<table><thead><tr><th>申请事项</th><th>状态</th></tr></thead>
<tbody><tr><td>在读证明</td><td>已通过</td></tr></tbody></table>
<script>
const post = (url, body) => fetch(url, {
  method: 'POST',
  headers: {'Content-Type': 'application/x-www-form-urlencoded'},
  body,
});
const on = (id, url, body, mark) => document.getElementById(id).addEventListener('click',
  async () => { await post(url, body); document.body.dataset.last = mark; });
on('query', '/api/getSqList.do', 'pageSize=10&pageNumber=1', 'query-ok');
on('save', '/api/doIt.do', 'x=1', 'save-sent');
on('submit', '/api/doIt.do', 'x=2', 'submit-sent');
on('withdraw', '/api/doIt.do', 'x=3', 'withdraw-sent');
</script>
</body></html>"""


def _first(values: dict[str, list[str]], key: str) -> str:
    return values.get(key, [""])[0]


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:  # 驱动起不来也当作不可用
        return False


REQUIRES_CHROMIUM = pytest.mark.skipif(
    not _chromium_available(), reason="需要 Playwright Chromium：uv run playwright install chromium"
)


class FakeSite:
    username = "fakeuser"
    password = "fake-pass-123"

    def __init__(self) -> None:
        self.hits: list[tuple[str, str]] = []
        self.sessions: set[str] = set()
        self.tgts: set[str] = set()
        self.tickets: set[str] = set()
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def portal_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/portal"

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def expire_all(self) -> None:
        """模拟会话与登录票据全部过期。"""
        with self._lock:
            self.sessions.clear()
            self.tgts.clear()

    def paths(self, method: str | None = None) -> list[str]:
        with self._lock:
            return [p for m, p in self.hits if method is None or m == method]

    # ------------------------------------------------------------------ 路由
    def handle(self, req: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(req.path)
        length = int(req.headers.get("Content-Length") or 0)
        body = req.rfile.read(length) if length else b""
        with self._lock:
            self.hits.append((req.command, parts.path))
        cookies = SimpleCookie(req.headers.get("Cookie") or "")
        if parts.path.startswith("/api/"):
            rows = {"code": "0", "datas": {"rows": [{"WID": "1", "FWMC": "在读证明"}]}}
            self._send(req, 200, json.dumps(rows, ensure_ascii=False), "application/json")
        elif parts.path == "/authserver/login":
            self._auth(req, parse_qs(parts.query), cookies, body)
        elif parts.path == "/portal":
            self._portal(req, parse_qs(parts.query), cookies)
        elif parts.path == "/recon/form":
            self._send(req, 200, RECON_FORM_PAGE, "text/html")
        else:
            self._send(req, 404, "not found", "text/plain")

    def _auth(
        self,
        req: BaseHTTPRequestHandler,
        query: dict[str, list[str]],
        cookies: SimpleCookie,
        body: bytes,
    ) -> None:
        if req.command == "POST":
            form = parse_qs(body.decode("utf-8"))
            service = _first(form, "service") or self.portal_url
            ok = _first(form, "username") == self.username and _first(form, "password") == (
                self.password
            )
            if ok:
                self._grant(req, service, new_tgt=True)
                return
            page = LOGIN_PAGE.format(service=html.escape(service), error="用户名或密码有误")
            self._send(req, 200, page, "text/html")
            return
        service = _first(query, "service") or self.portal_url
        tgt = cookies["CASTGC"].value if "CASTGC" in cookies else ""
        with self._lock:
            known = tgt in self.tgts
        if known:
            self._grant(req, service, new_tgt=False)
            return
        page = LOGIN_PAGE.format(service=html.escape(service), error="")
        self._send(req, 200, page, "text/html")

    def _grant(self, req: BaseHTTPRequestHandler, service: str, *, new_tgt: bool) -> None:
        ticket = "ST-" + secrets.token_hex(8)
        headers = []
        with self._lock:
            self.tickets.add(ticket)
            if new_tgt:
                tgt = "TGT-" + secrets.token_hex(8)
                self.tgts.add(tgt)
                headers.append(f"CASTGC={tgt}; Path=/authserver; HttpOnly")
        self._redirect(req, f"{service}?ticket={ticket}", headers)

    def _portal(
        self, req: BaseHTTPRequestHandler, query: dict[str, list[str]], cookies: SimpleCookie
    ) -> None:
        session = cookies["SESSION"].value if "SESSION" in cookies else ""
        ticket = _first(query, "ticket")
        with self._lock:
            valid_session = session in self.sessions
            valid_ticket = ticket in self.tickets
            if valid_ticket:
                self.tickets.discard(ticket)
                session = secrets.token_hex(8)
                self.sessions.add(session)
        if valid_session:
            self._send(req, 200, PORTAL_PAGE, "text/html")
        elif valid_ticket:
            self._redirect(req, "/portal", [f"SESSION={session}; Path=/; HttpOnly"])
        else:
            login = f"http://localhost:{self.port}/authserver/login"
            self._redirect(req, f"{login}?service={quote(self.portal_url)}", [])

    @staticmethod
    def _redirect(req: BaseHTTPRequestHandler, location: str, cookies: list[str]) -> None:
        req.send_response(302)
        req.send_header("Location", location)
        for cookie in cookies:
            req.send_header("Set-Cookie", cookie)
        req.send_header("Content-Length", "0")
        req.end_headers()

    @staticmethod
    def _send(req: BaseHTTPRequestHandler, status: int, text: str, content_type: str) -> None:
        data = text.encode("utf-8")
        req.send_response(status)
        req.send_header("Content-Type", f"{content_type}; charset=utf-8")
        req.send_header("Content-Length", str(len(data)))
        req.end_headers()
        req.wfile.write(data)


def _make_handler(site: FakeSite) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            site.handle(self)

        def do_POST(self) -> None:
            site.handle(self)

        def do_PUT(self) -> None:
            site.handle(self)

        def do_DELETE(self) -> None:
            site.handle(self)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler
