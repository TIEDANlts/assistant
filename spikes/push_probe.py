"""验证 5：推送通道。

    uv run python spikes/push_probe.py --channel all
    uv run python spikes/push_probe.py --channel ntfy --repeat 3 --gap 20

给每个已配置的通道发一条测试消息，记录接口返回和耗时；消息里没有任何个人信息。
接口返回成功只说明通道收下了。是否真的在 iOS 和安卓上弹出、锁屏显示什么、点链接能否打开，
要你在两台手机上确认后记到 docs/recon/push.md。webhook 地址本身就是密钥，屏幕和日志里都不出现。
"""

import argparse
import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import spike_common
from spike_common import SpikeError

CHANNELS = ("feishu", "wecom", "ntfy", "bark")
TITLE = "助手推送测试"


@dataclass(frozen=True)
class PushRequest:
    channel: str
    url: str = field(repr=False)
    body: dict[str, Any] = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)


def require_https(url: str, name: str) -> str:
    parts = urlsplit(url)
    local = parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}
    if parts.scheme == "https" or local:
        return url
    raise SpikeError(f"{name} 必须是 https 地址")


def feishu_sign(timestamp: int, secret: str) -> str:
    """飞书自定义机器人签名：以“时间戳\\n密钥”为 key 对空消息做 HMAC-SHA256，再 Base64。"""
    key = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode()


def build_request(channel: str, text: str, link: str, timestamp: int) -> PushRequest | None:
    """按环境变量构造请求；没配置的通道返回 None。"""
    env = spike_common.env
    message = f"{TITLE}\n{text}\n{link}"
    if channel == "wecom":
        hook = env("WECOM_WEBHOOK")
        if not hook:
            return None
        body: dict[str, Any] = {"msgtype": "text", "text": {"content": message}}
        return PushRequest(channel, require_https(hook, "WECOM_WEBHOOK"), body)
    if channel == "feishu":
        hook = env("FEISHU_WEBHOOK")
        if not hook:
            return None
        body = {"msg_type": "text", "content": {"text": message}}
        secret = env("FEISHU_SECRET")
        if secret:
            body.update(timestamp=str(timestamp), sign=feishu_sign(timestamp, secret))
        return PushRequest(channel, require_https(hook, "FEISHU_WEBHOOK"), body)
    if channel == "ntfy":
        topic = env("NTFY_TOPIC")
        if not topic:
            return None
        server = require_https(env("NTFY_SERVER", "https://ntfy.sh"), "NTFY_SERVER")
        token = env("NTFY_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        body = {"topic": topic, "title": TITLE, "message": text, "click": link, "tags": ["robot"]}
        return PushRequest(channel, server.rstrip("/") + "/", body, headers)
    if channel == "bark":
        key = env("BARK_KEY")
        if not key:
            return None
        server = require_https(env("BARK_SERVER", "https://api.day.app"), "BARK_SERVER")
        body = {"device_key": key, "title": TITLE, "body": text, "url": link, "group": "assistant"}
        return PushRequest(channel, server.rstrip("/") + "/push", body)
    raise SpikeError(f"未知通道：{channel}")


def interpret(channel: str, status: int, payload: Any) -> tuple[bool, str]:
    """各通道的“成功”标准不同：看 HTTP 状态和返回体里的业务码。"""
    if status >= 400:
        return False, f"HTTP {status}"
    if not isinstance(payload, dict):
        return 200 <= status < 300, f"HTTP {status}"
    if channel == "wecom":
        return payload.get("errcode") == 0, str(payload.get("errmsg", ""))
    if channel == "feishu":
        code = payload.get("code", payload.get("StatusCode"))
        return code == 0, str(payload.get("msg", payload.get("StatusMessage", "")))
    if channel == "bark":
        return payload.get("code") == 200, str(payload.get("message", ""))
    return "id" in payload, f"id={payload.get('id', '')}"


def send(request: PushRequest, timeout: float = 10.0) -> dict[str, Any]:
    data = json.dumps(request.body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", **request.headers}
    http_request = urllib.request.Request(request.url, data=data, headers=headers, method="POST")
    host = urlsplit(request.url).hostname or ""
    result: dict[str, Any] = {"channel": request.channel, "host": host}
    started = time.monotonic()
    try:
        with urllib.request.urlopen(http_request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except OSError as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        result.update(ok=False, detail=f"网络错误：{reason}")
        result["ms"] = round((time.monotonic() - started) * 1000)
        return result
    result["ms"] = round((time.monotonic() - started) * 1000)
    try:
        payload = json.loads(raw.decode("utf-8") or "null")
    except ValueError:
        payload = None
    ok, detail = interpret(request.channel, status, payload)
    result.update(ok=ok, status=status, detail=detail)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="验证 5：给已配置的推送通道各发一条测试消息")
    parser.add_argument("--channel", choices=[*CHANNELS, "all"], default="all")
    parser.add_argument("--repeat", type=int, default=1, help="每个通道发几条")
    parser.add_argument("--gap", type=float, default=15.0, help="两轮之间隔几秒")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    channels = CHANNELS if args.channel == "all" else (args.channel,)
    link = require_https(spike_common.env("PUSH_TEST_URL", "https://example.com/"), "PUSH_TEST_URL")
    log = spike_common.state_dir("recon") / "push.jsonl"
    results: list[dict[str, Any]] = []
    for number in range(1, args.repeat + 1):
        sent_at = spike_common.now()
        text = (
            f"第 {number} 条测试，发送于 {sent_at:%H:%M:%S}。"
            "请记录：是否弹出、锁屏显示什么、能否点开。"
        )
        for channel in channels:
            request = build_request(channel, text, link, int(time.time()))
            if request is None:
                print(f"- {channel}：未配置，跳过")
                continue
            result = send(request)
            results.append(result)
            record = {"ts": sent_at.isoformat(timespec="seconds"), "round": number, **result}
            spike_common.append_jsonl(log, record)
            verdict = "接口收下" if result["ok"] else "失败"
            detail = f"{verdict}，{result['detail']}，{result['ms']} ms"
            print(f"- {channel}（{result['host']}）：{detail}")
        if number < args.repeat:
            time.sleep(args.gap)
    if not results:
        raise SpikeError("一个通道都没有配置：请在 .env 里至少配置两个通道。")
    print("\n=== 在两台手机上确认后，填到 docs/recon/push.md ===")
    print("| 通道 | 接口 | iOS：收到？延迟 | 安卓：收到？延迟 | 锁屏显示 | 点开链接 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for result in results:
        verdict = "收下" if result["ok"] else "失败"
        print(f"| {result['channel']} | {verdict}（{result['ms']} ms） | | | | |")
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    spike_common.run(main)
