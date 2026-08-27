"""仅供 harness 的 Provider mock；控制面不读取或保存模型请求正文。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event


def serve(port: int, identity_id: str, *, announce_ready: bool = False) -> None:
    state = {"model_blocked": False, "model_started": False, "model_calls": 0}
    mode = {"repair_after_invalid": False}
    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/state":
                self._json(200, dict(state))
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/__harness__/block-next-model":
                if self.headers.get("X-Harness-Control") != identity_id:
                    self._json(403, {"status": "rejected"})
                    return
                state.update(model_blocked=True, model_started=False)
                mode["repair_after_invalid"] = False
                release.clear()
                self._json(202, {"status": "armed"})
                return
            if self.path == "/__harness__/block-repair-after-invalid":
                if self.headers.get("X-Harness-Control") != identity_id:
                    self._json(403, {"status": "rejected"})
                    return
                state.update(
                    model_blocked=True,
                    model_started=False,
                    model_calls=0,
                )
                mode["repair_after_invalid"] = True
                release.clear()
                self._json(202, {"status": "armed"})
                return
            if self.path == "/__harness__/release-model":
                if self.headers.get("X-Harness-Control") != identity_id:
                    self._json(403, {"status": "rejected"})
                    return
                release.set()
                self._json(202, {"status": "released"})
                return
            # 只消费 Content-Length 字节以保持 HTTP 流同步，绝不解析、记录或回显正文。
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["model_calls"] += 1
            if mode["repair_after_invalid"] and state["model_calls"] == 1:
                self._json(200, {"invalid": True})
                return
            if state["model_blocked"]:
                state["model_started"] = True
                if not release.wait(timeout=5):
                    self._json(503, {"status": "timed_out"})
                    return
                state["model_blocked"] = False
            if mode["repair_after_invalid"]:
                self._json(200, {"source_refs": []})
                return
            self._json(200, {"request_id": "harness-model"})

        def _json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if announce_ready:
        # Provider mock 同样只发送固定安全事件，避免探活连接占用后续服务端口。
        print('{"event":"ready","role":"mock_provider"}', flush=True)
    server.serve_forever()
