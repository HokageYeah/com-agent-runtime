"""Task 12 回环业务 mock；仅验证签名和安全投影，绝不保留请求正文。"""

from __future__ import annotations

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event
from typing import TypedDict

from app.core.tool_security import verify_runtime_tool

_CALLBACK_FIELDS = {
    "event",
    "event_id",
    "run_id",
    "event_seq",
    "status_version",
    "agent_id",
    "business_id",
    "status",
    "error",
    "public_trace",
}


class _MockState(TypedDict):
    callback_count: int
    last_status: str
    published_revision: int
    snapshot_reads: int
    publish_blocked: bool
    publish_started: bool


def _handler(identity_id: str) -> type[BaseHTTPRequestHandler]:
    """每个测试服务只接受其随机身份派生的 HMAC，不输出或保存该值。"""
    state: _MockState = {
        "callback_count": 0,
        "last_status": "none",
        "published_revision": 0,
        "snapshot_reads": 0,
        "publish_blocked": False,
        "publish_started": False,
    }
    publish_release = Event()
    # 仅保留不可逆内容摘要，供 query-after-commit fixture 返回；不写入 /state。
    published_digest: str | None = None
    runtimes: dict[str, dict[str, object]] = {
        "agent-runtime-harness": {"keys": {"test": f"harness-only-{identity_id}"}}
    }

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._json(200, {"status": "mock_ready"})
                return
            if self.path == "/state":
                # 只公开聚合状态，严禁 callback、工具入参或业务正文进入测试输出。
                self._json(200, dict(state))
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            nonlocal published_digest
            if self.path == "/__harness__/block-next-publish":
                if self.headers.get("X-Harness-Control") != identity_id:
                    self._json(403, {"status": "rejected"})
                    return
                state["publish_blocked"] = True
                state["publish_started"] = False
                publish_release.clear()
                self._json(202, {"status": "armed"})
                return
            if self.path == "/__harness__/release-publish":
                if self.headers.get("X-Harness-Control") != identity_id:
                    self._json(403, {"status": "rejected"})
                    return
                publish_release.set()
                self._json(202, {"status": "released"})
                return
            if self.path not in {
                "/callbacks",
                "/api/v1/internal/agent-tools/memory.get_snapshot",
                "/api/v1/internal/agent-tools/memory.publish_playback_document",
                "/api/v1/internal/agent-tools/memory.get_publish_result",
            }:
                self.send_error(404)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                verify_runtime_tool(
                    {key.lower(): value for key, value in self.headers.items()},
                    "POST",
                    self.path,
                    body,
                    runtimes,
                    30,
                )
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError("PAYLOAD_INVALID")
            except (ValueError, json.JSONDecodeError):
                self._json(403, {"status": "rejected"})
                return
            if self.path == "/callbacks":
                if not set(payload) <= _CALLBACK_FIELDS:
                    self._json(403, {"status": "rejected"})
                    return
                status = payload.get("status")
                if not isinstance(status, str):
                    self._json(403, {"status": "rejected"})
                    return
                state["callback_count"] += 1
                state["last_status"] = status
                self._json(202, {"status": "accepted"})
                return
            input_data = payload.get("input")
            if not isinstance(input_data, dict):
                self._json(403, {"status": "rejected"})
                return
            if self.path.endswith("memory.get_snapshot"):
                state["snapshot_reads"] += 1
                # 只返回最小 fixture；mock 不留存请求内的 archive/run/snapshot 标识。
                self._json(200, {"output": {"diaries": [], "bets": []}})
                return
            if self.path.endswith("memory.get_publish_result"):
                if state["published_revision"] == 0 or published_digest is None:
                    self._json(404, {"status": "unavailable"})
                    return
                self._json(200, {"output": {
                    "revision": state["published_revision"],
                    "content_digest": published_digest,
                }})
                return
            document = input_data.get("document")
            if not isinstance(document, dict):
                self._json(403, {"status": "rejected"})
                return
            if state["publish_blocked"]:
                # 该控制点只用于测试：请求已到业务边界，但业务提交尚未发生。
                # release 后模拟业务侧发现 generation/purge 已失效，绝不发布文档。
                state["publish_started"] = True
                if not publish_release.wait(timeout=5):
                    self._json(503, {"status": "timed_out"})
                    return
                state["publish_blocked"] = False
                self._json(409, {"status": "superseded"})
                return
            state["published_revision"] = 1
            digest = hashlib.sha256(
                json.dumps(
                    document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            published_digest = digest
            self._json(200, {"output": {"revision": 1, "content_digest": digest}})
            return

        def _json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            """mock 不记录请求路径、header 或 body，避免测试载荷进入 stdout。"""

    return _Handler


def serve(port: int, identity_id: str, *, announce_ready: bool = False) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(identity_id))
    if announce_ready:
        # 固定就绪事件不包含端口、身份或请求内容；父进程无需再发 TCP 自探针。
        print('{"event":"ready","role":"mock_business"}', flush=True)
    server.serve_forever()
