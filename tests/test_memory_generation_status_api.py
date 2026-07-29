"""回忆录生成状态查询只返回业务前端可展示的安全摘要。"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.main import app
from app.services.memory_agent_binding_service import MemoryAgentBindingService
from app.services.memory_archive_service import FrozenMemoryInput, MemoryArchiveService


def _headers(path: str) -> dict[str, str]:
    """生成业务服务查询签名，空请求体也必须参与原始 hash。"""
    timestamp = str(int(datetime.now(UTC).timestamp()))
    canonical = f"GET\n{path}\n{timestamp}\n{hashlib.sha256(b'').hexdigest()}"
    signature = hmac.new(b"development-secret", canonical.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Agent-Client-Id": "couple-diary", "X-Agent-Key-Id": "dev",
        "X-Agent-Timestamp": timestamp, "X-Agent-Signature": signature,
    }


def test_generation_status_returns_safe_active_run_summary(client) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    app.state.session_factory = factory
    session = factory()
    archive = MemoryArchiveService(session, app.state.memory_snapshot_cipher).create_archives_for_relationship(
        FrozenMemoryInput(1, "space", 1, (1, 2), {}, datetime(2026, 7, 17, tzinfo=UTC), {}, {}, "v1")
    )[0]
    ref = MemoryAgentBindingService(session).bind(archive.archive_id, "run-status", 0)
    ref.status, ref.event_seq, ref.status_version = "running", 2, 3
    ref.updated_at = datetime(2026, 7, 17, 8, 30, tzinfo=UTC)
    ref.public_trace_json = [{"step": "generate_scenes", "status": "succeeded"}]
    archive.content_status = "running"
    session.commit()
    path = f"/api/v1/memory-archives/{archive.archive_id}/generation-status"

    response = client.get(path, headers=_headers(path))

    assert response.status_code == 200
    assert response.json() == {
        "output": {
            "archive_id": archive.archive_id, "content_status": "running",
            "enhancement_status": "disabled", "generation_epoch": 0,
            "published_revision": 0, "status_version": 3,
            "updated_at": "2026-07-17T08:30:00", "retry_after_ms": 2000,
            "active_run": {
                "run_id": "run-status", "status": "running", "event_seq": 2,
                "status_version": 3, "reconciliation_status": "not_needed",
                "public_trace": [{"step": "generate_scenes", "status": "succeeded"}],
            },
        },
        "schema_version": "1.0.0",
    }
