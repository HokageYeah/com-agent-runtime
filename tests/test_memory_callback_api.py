"""业务侧回忆录 callback 接收：验签、幂等与事件序号边界。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.tool_security import tool_signature
from app.db.sqlalchemy_db import Base
from app.main import app
from app.models.memory_agent_run_ref import MemoryAgentRunRef
from app.services.memory_agent_binding_service import MemoryAgentBindingService
from app.services.memory_archive_service import FrozenMemoryInput, MemoryArchiveService


def _headers(path: str, body: bytes, event_id: str, archive_id: str) -> dict[str, str]:
    """构造 Runtime callback 固定签名头；body 保持原始字节参与验签。"""
    timestamp = str(int(datetime.now(UTC).timestamp()))
    return {
        "X-Agent-Runtime-Id": "agent-runtime", "X-Agent-Key-Id": "dev",
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Signature": tool_signature("POST", path, timestamp, body, "runtime-tool-development-secret"),
        "X-Agent-Run-Id": "run-callback", "X-Agent-Business-Id": archive_id,
        "X-Agent-Event-Id": event_id, "X-Agent-Event-Seq": "1",
        "Idempotency-Key": f"callback:{event_id}", "Content-Type": "application/json",
    }


def test_memory_callback_updates_run_ref_once_and_replays_same_event(client) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    app.state.session_factory = factory
    session = factory()
    archive = MemoryArchiveService(session, app.state.memory_snapshot_cipher).create_archives_for_relationship(
        FrozenMemoryInput(1, "space", 1, (1, 2), {}, datetime(2026, 7, 16, tzinfo=UTC), {}, {}, "v1")
    )[0]
    MemoryAgentBindingService(session).bind(archive.archive_id, "run-callback", 0)
    session.commit()
    path = "/api/v1/internal/agent-callbacks/memory"
    event_id = "event-cancelled"
    payload = {
        "event": "run_cancelled", "event_id": event_id, "event_seq": 1,
        "status_version": 2, "run_id": "run-callback", "agent_id": "memoir_agent",
        "business_id": archive.archive_id, "status": "cancelled", "error": None,
        "public_trace": [],
    }
    body = json.dumps(payload, separators=(",", ":")).encode()

    first = client.post(path, content=body, headers=_headers(path, body, event_id, archive.archive_id))
    second = client.post(path, content=body, headers=_headers(path, body, event_id, archive.archive_id))

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"output": {"applied": True}, "schema_version": "1.0.0"}
    ref = factory().query(MemoryAgentRunRef).filter_by(run_id="run-callback").one()
    assert (ref.status, ref.event_seq, ref.status_version) == ("cancelled", 1, 2)
