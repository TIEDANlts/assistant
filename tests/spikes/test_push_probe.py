import base64
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

import push_probe
from spike_common import SpikeError


class Capture(BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[str, dict[str, str], Any]]] = []
    replies: ClassVar[dict[str, Any]] = {}

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        path = self.path.split("?")[0]
        Capture.received.append((path, dict(self.headers), body))
        data = json.dumps(Capture.replies.get(path, {"id": "n1"})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    Capture.received.clear()
    Capture.replies.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("PUSH_TEST_URL", "https://example.com/task/1")
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()


def configure_all(monkeypatch: pytest.MonkeyPatch, base: str) -> None:
    monkeypatch.setenv("WECOM_WEBHOOK", f"{base}/wecom?key=hook-key-123")
    monkeypatch.setenv("FEISHU_WEBHOOK", f"{base}/feishu")
    monkeypatch.setenv("FEISHU_SECRET", "sign-secret-456")
    monkeypatch.setenv("NTFY_SERVER", base)
    monkeypatch.setenv("NTFY_TOPIC", "assistant-test")
    monkeypatch.setenv("NTFY_TOKEN", "tk-789")
    monkeypatch.setenv("BARK_SERVER", base)
    monkeypatch.setenv("BARK_KEY", "bark-device-000")


def test_feishu_sign_is_base64_hmac_sha256() -> None:
    first = push_probe.feishu_sign(1_700_000_000, "secret")
    assert first == push_probe.feishu_sign(1_700_000_000, "secret")
    assert len(base64.b64decode(first)) == 32
    assert first != push_probe.feishu_sign(1_700_000_001, "secret")


def test_require_https_allows_only_local_http() -> None:
    assert push_probe.require_https("https://ntfy.sh", "X") == "https://ntfy.sh"
    assert push_probe.require_https("http://127.0.0.1:9/x", "X")
    with pytest.raises(SpikeError, match="https"):
        push_probe.require_https("http://ntfy.sh", "NTFY_SERVER")


def test_unconfigured_channels_are_skipped() -> None:
    for channel in push_probe.CHANNELS:
        assert push_probe.build_request(channel, "t", "https://example.com", 0) is None


def test_all_channels_end_to_end(
    capture: str, monkeypatch: pytest.MonkeyPatch, data_home: Path
) -> None:
    configure_all(monkeypatch, capture)
    Capture.replies.update(
        {
            "/wecom": {"errcode": 0, "errmsg": "ok"},
            "/feishu": {"code": 0, "msg": "success"},
            "/push": {"code": 200, "message": "success"},
        }
    )
    assert push_probe.main(["--channel", "all"]) == 0
    by_path = {path: (headers, body) for path, headers, body in Capture.received}
    assert by_path["/wecom"][1]["msgtype"] == "text"
    assert {"timestamp", "sign"} <= set(by_path["/feishu"][1])
    assert by_path["/"][1]["topic"] == "assistant-test"
    assert by_path["/"][0]["Authorization"] == "Bearer tk-789"
    assert by_path["/push"][1]["device_key"] == "bark-device-000"
    log = (data_home / "state" / "recon" / "push.jsonl").read_text(encoding="utf-8")
    for secret in ("hook-key-123", "sign-secret-456", "tk-789", "bark-device-000"):
        assert secret not in log


def test_business_error_code_counts_as_failure(
    capture: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WECOM_WEBHOOK", f"{capture}/wecom?key=k")
    Capture.replies["/wecom"] = {"errcode": 93000, "errmsg": "invalid webhook url"}
    assert push_probe.main(["--channel", "wecom"]) == 1
