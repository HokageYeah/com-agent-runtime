"""Runtime callback 出站投递必须固定目标、签名并复用事件幂等键。"""

from __future__ import annotations

import json

import httpx

from app.runtime.callback_gateway import CallbackGateway, CallbackTarget


def test_callback_gateway_signs_registered_target_without_redirect() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, request=request)

    gateway = CallbackGateway(
        {"memory": CallbackTarget("http://business.local/api/v1/internal/agent-callbacks/memory", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    payload = {"event": "run_cancelled", "event_id": "event-1", "event_seq": 3, "run_id": "run-1", "business_id": "archive-1"}

    gateway.send("memory", payload)

    assert captured["url"] == "http://business.local/api/v1/internal/agent-callbacks/memory"
    assert captured["body"] == payload
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["x-agent-runtime-id"] == "agent-runtime"
    assert headers["x-agent-run-id"] == "run-1"
    assert headers["x-agent-business-id"] == "archive-1"
    assert headers["x-agent-event-id"] == "event-1"
    assert headers["x-agent-event-seq"] == "3"
    assert headers["idempotency-key"] == "callback:event-1"
