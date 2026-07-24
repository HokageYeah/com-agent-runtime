"""Runtime callback 出站投递必须固定目标、签名并复用事件幂等键。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.core.tool_security import tool_signature
from app.db.sqlalchemy_db import Base
from app.models import AgentRun, CallbackEvent
from app.runtime.callback_gateway import CallbackGateway, CallbackTarget
from app.services.outbox_service import OutboxService


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
    payload = {"event": "run_cancelled", "event_id": "event-1", "event_seq": 3, "status_version": 2, "run_id": "run-1", "agent_id": "memoir_agent", "business_id": "archive-1", "status": "cancelled", "error": None, "public_trace": []}

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
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    assert headers["x-agent-signature"] == tool_signature(
        "POST", "/api/v1/internal/agent-callbacks/memory",
        headers["x-agent-timestamp"], body, "secret",
    )


def test_callback_outbox_drops_untrusted_trace_fields(caplog) -> None:
    """callback 只能投递步骤与状态，不能被调用方附带 prompt 等私密字段。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run = AgentRun(
        run_id="run-trace", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive-1", status="running", dispatch_state="claimed", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="memory", business_connector_id="connector", trace_id="trace",
        run_deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    session.add(run)
    session.flush()

    OutboxService(session).append_callback_event(
        run, "step_changed",
        [{"step": "generate_scenes", "status": "succeeded", "prompt": "private-marker"}],
    )
    callback = session.scalar(select(CallbackEvent).where(CallbackEvent.run_id == run.run_id))

    assert callback is not None
    assert callback.payload_json["public_trace"] == [
        {"step": "generate_scenes", "status": "succeeded"}
    ]
    assert "private-marker" not in str(callback.payload_json)
    assert "private-marker" not in caplog.text


@pytest.mark.parametrize(
    "private_field",
    [{"prompt": "private-marker"}, {"error": {"message": "private-marker"}}],
)
def test_callback_gateway_rejects_legacy_event_with_private_field(
    private_field: dict[str, object],
) -> None:
    """即使旧数据绕过 outbox，出站网关也不能把敏感 callback 发给业务端。"""
    gateway = CallbackGateway(
        {"memory": CallbackTarget("http://business.local/callback", "runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))),
    )

    with pytest.raises(ValueError, match="CALLBACK_PAYLOAD_UNSAFE"):
        gateway.send(
            "memory",
            {
                "event": "run_cancelled", "event_id": "event-private", "event_seq": 1, "status_version": 1,
                "run_id": "run-1", "business_id": "archive-1", **private_field,
            },
        )


def test_callback_gateway_signs_exact_original_body_and_requires_version_fields() -> None:
    captured: list[bytes] = []
    gateway = CallbackGateway(
        {"memory": CallbackTarget("https://business.local/callback", "runtime", "new", "secret")},
        httpx.Client(transport=httpx.MockTransport(lambda request: (captured.append(request.content), httpx.Response(200, request=request))[1])),
    )
    payload = {
        "event": "run_started", "event_id": "event-1", "event_seq": 1, "status_version": 2,
        "run_id": "run-1", "agent_id": "memoir_agent", "business_id": "archive-1", "status": "running",
        "error": None, "public_trace": [],
    }

    gateway.send("memory", payload)

    assert captured == [json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()]
    with pytest.raises(ValueError, match="CALLBACK_PAYLOAD_INVALID"):
        gateway.send("memory", {key: value for key, value in payload.items() if key != "status_version"})
