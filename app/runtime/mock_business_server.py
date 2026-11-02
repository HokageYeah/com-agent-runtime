"""Task 12 回环业务 mock；仅验证签名和安全投影，绝不保留请求正文。"""

from __future__ import annotations

import hashlib
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock
from typing import TypedDict, cast
from uuid import uuid4

from app.contracts.tools import TOOL_ERROR_SPECS_V1_1
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
    # QA 读取与成功提交的聚合次数，不含业务引用和正文。
    qa_context_reads: int
    qa_submissions: int
    # harness 提交后阻塞开关与已到达提交点状态。
    qa_submit_blocked: bool
    qa_submit_started: bool
    # 帮哄同样只保留聚合计数，不在服务状态保存上下文或草稿。
    soothe_context_reads: int
    soothe_submissions: int


def _handler(identity_id: str) -> type[BaseHTTPRequestHandler]:
    """每个测试服务只接受其随机身份派生的 HMAC，不输出或保存该值。"""
    state: _MockState = {
        "callback_count": 0,
        "last_status": "none",
        "published_revision": 0,
        "snapshot_reads": 0,
        "publish_blocked": False,
        "publish_started": False,
        "qa_context_reads": 0,
        "qa_submissions": 0,
        "qa_submit_blocked": False,
        "qa_submit_started": False,
        "soothe_context_reads": 0,
        "soothe_submissions": 0,
    }
    publish_release = Event()
    qa_submit_release = Event()
    # 只保存 scope 与正文的不可逆摘要；锁保证并发重发不会重复提交。
    qa_answers: dict[str, str] = {}
    qa_lock = Lock()
    soothe_results: dict[str, str] = {}
    soothe_lock = Lock()
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
            if self.path in {"/__harness__/block-next-submit", "/__harness__/release-submit"}:
                if self.headers.get("X-Harness-Control") != identity_id:
                    self._json(403, {"status": "rejected"})
                    return
                if self.path.endswith("block-next-submit"):
                    state["qa_submit_blocked"] = True
                    state["qa_submit_started"] = False
                    qa_submit_release.clear()
                    self._json(202, {"status": "armed"})
                else:
                    qa_submit_release.set()
                    self._json(202, {"status": "released"})
                return
            if self.path not in {
                "/callbacks",
                "/api/v1/internal/agent-tools/qa.get_question_context",
                "/api/v1/internal/agent-tools/qa.submit_answer",
                "/api/v1/internal/agent-tools/qa.get_answer_result",
                "/api/v1/internal/agent-tools/soothe.get_turn_context",
                "/api/v1/internal/agent-tools/soothe.submit_turn_result",
                "/api/v1/internal/agent-tools/soothe.get_turn_result",
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
            if "/qa." in self.path:
                self._qa_tool(payload, input_data)
                return
            if "/soothe." in self.path:
                self._soothe_tool(payload, input_data)
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

        def _soothe_tool(self, payload: dict[str, object], input_data: dict[str, object]) -> None:
            """三种帮哄工具复用验签入口；按轮次前缀选择固定结构，正文只在当前栈。"""
            context = payload.get("context")
            names = ("session_id", "turn_id", "run_id")
            if (
                any(not isinstance(input_data.get(name), str) or not input_data[name] for name in names)
                or not isinstance(context, dict)
                or context.get("agent_id") != "soothe_agent"
                or context.get("business_type") not in {"soothe_reply", "soothe_simulator"}
                or context.get("business_id") != input_data["turn_id"]
                or context.get("run_id") != input_data["run_id"]
                or self.headers.get("X-Agent-Tool-Contract-Version") != "1.1.0"
            ):
                self._qa_error("BUSINESS_DATA_INVALID")
                return
            if self.path.endswith("soothe.get_turn_context"):
                state["soothe_context_reads"] += 1
                # 测试正文运行时生成，避免真实素材或固定私密 fixture 进入源码/输出。
                def material() -> str:
                    return uuid4().hex.translate(str.maketrans("0123456789", "ghijklmnop"))

                if str(input_data["turn_id"]).startswith("reply-"):
                    turn_context: dict[str, object] = {
                        "mode": "reply", "chat_text": material(), "background": None,
                        "tone": "any", "previous_replies": [],
                    }
                elif str(input_data["turn_id"]).startswith(("simulator-", "sim-")):
                    turn_context = {
                        "mode": "simulator", "scenario": {
                            "key": "forgot_anniversary", "title": material(),
                            "anger_reason": material(), "hidden_need": material(),
                        }, "partner_gender": "female", "partner_name": "TA",
                        "forgiveness": 20, "round_count": 0, "history": [], "user_message": material(),
                    }
                else:
                    self._qa_error("BUSINESS_DATA_INVALID")
                    return
                self._json(200, {"output": {"turn_context": turn_context}, "schema_version": "1.0.0"})
                return
            key = self.headers.get("Idempotency-Key", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key):
                self._qa_error("BUSINESS_DATA_INVALID")
                return
            scope = hashlib.sha256(json.dumps(
                [input_data[name] for name in names] + [key], separators=(",", ":")
            ).encode()).hexdigest()
            if self.path.endswith("soothe.get_turn_result"):
                with soothe_lock:
                    submitted = scope in soothe_results
                output = {"submitted": True, "revision": 1} if submitted else {"submitted": False}
                self._json(200, {"output": output, "schema_version": "1.0.0"})
                return
            result = input_data.get("turn_result")
            if set(input_data) != {*names, "turn_result"} or not isinstance(result, dict):
                self._qa_error("BUSINESS_DATA_INVALID")
                return
            digest = hashlib.sha256(json.dumps(
                result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest()
            with soothe_lock:
                previous = soothe_results.get(scope)
                if previous is not None and previous != digest:
                    self._qa_error("IDEMPOTENCY_CONFLICT")
                    return
                if previous is None:
                    soothe_results[scope] = digest
                    state["soothe_submissions"] += 1
            self._json(200, {"output": {"submitted": True, "revision": 1}, "schema_version": "1.0.0"})

        def _qa_tool(self, payload: dict[str, object], input_data: dict[str, object]) -> None:
            """模拟业务授权引用、逻辑幂等与提交后查询；正文只停留在当前栈。"""
            context = payload.get("context")
            names = ("conversation_id", "turn_id", "run_id")
            if (
                any(not isinstance(input_data.get(name), str) or not input_data[name] for name in names)
                or not isinstance(context, dict)
                or context.get("agent_id") != "qa_agent"
                or context.get("business_type") != "business_qa"
                or context.get("business_id") != input_data["turn_id"]
                or context.get("run_id") != input_data["run_id"]
                or self.headers.get("X-Agent-Tool-Contract-Version") != "1.1.0"
            ):
                self._qa_error("BUSINESS_DATA_INVALID")
                return
            if self.path.endswith("qa.get_question_context"):
                state["qa_context_reads"] += 1
                # 运行时生成测试正文，不将问题和证据作为持久 fixture 或服务状态保存。
                self._json(200, {"output": {
                    "question": uuid4().hex.translate(str.maketrans("0123456789", "ghijklmnop")),
                    "evidence": [{"source_ref": "qa-evidence-1", "text": uuid4().hex.translate(str.maketrans("0123456789", "ghijklmnop"))}],
                }})
                return
            key = self.headers.get("Idempotency-Key", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key):
                self._qa_error("BUSINESS_DATA_INVALID")
                return
            # 同一个 key 在不同会话/轮次/Run 下也不能读取其它业务提交结果。
            scope = hashlib.sha256(json.dumps(
                [input_data[name] for name in names] + [key], separators=(",", ":")
            ).encode()).hexdigest()
            if self.path.endswith("qa.get_answer_result"):
                with qa_lock:
                    submitted = scope in qa_answers
                output = {"submitted": True, "revision": 1} if submitted else {"submitted": False}
                self._json(200, {"output": output})
                return
            document = input_data.get("document")
            if not isinstance(document, dict):
                self._qa_error("BUSINESS_DATA_INVALID")
                return
            digest = hashlib.sha256(json.dumps(
                document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest()
            with qa_lock:
                previous = qa_answers.get(scope)
                if previous is not None and previous != digest:
                    self._qa_error("IDEMPOTENCY_CONFLICT")
                    return
                if previous is None:
                    qa_answers[scope] = digest
                    state["qa_submissions"] += 1
                blocked = state["qa_submit_blocked"]
                if blocked:
                    state["qa_submit_started"] = True
            if blocked:
                # 与 Memoir 的发布前阻塞不同：先持久化摘要，再阻塞响应；并发查询仍可见。
                if not qa_submit_release.wait(timeout=5):
                    self._qa_error("RUNTIME_SERVICE_UNAVAILABLE")
                    return
                state["qa_submit_blocked"] = False
            self._json(200, {"output": {"submitted": True, "revision": 1}})

        def _qa_error(self, code: str) -> None:
            """直接复用冻结 v1.1 错误矩阵，不新增业务 wire 错误码。"""
            spec = TOOL_ERROR_SPECS_V1_1[code]
            self._json(cast(int, spec["http_status"]), {
                "error_code": code, "error_type": spec["error_type"],
                "retryable": spec["retryable"], "safe_message": spec["safe_message"],
                "details_visible_to_model": False,
            })

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
