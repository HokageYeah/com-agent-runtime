"""回忆录原子发布内部接口：验签、active Run 与幂等重放。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.tool_security import tool_signature
from app.db.sqlalchemy_db import Base
from app.main import app
from app.models.memoir.memory_playback_document import MemoryPlaybackDocument
from app.models.memoir.memory_snapshot import MemorySnapshot
from app.services.memoir.memory_agent_binding_service import MemoryAgentBindingService
from app.services.memoir.memory_archive_service import (
    FrozenMemoryInput,
    MemoryArchiveService,
)


def _tool_headers(path: str, body: bytes, key: str) -> dict[str, str]:
    timestamp = str(int(datetime.now(UTC).timestamp()))
    return {
        "X-Agent-Runtime-Id": "agent-runtime", "X-Agent-Key-Id": "dev",
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Signature": tool_signature("POST", path, timestamp, body, "runtime-tool-development-secret"),
        "Idempotency-Key": key, "Content-Type": "application/json",
    }


def test_publish_tool_replays_identical_request_and_rejects_key_conflict(client) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    app.state.session_factory = factory
    session = factory()
    archive = MemoryArchiveService(session, app.state.memory_snapshot_cipher).create_archives_for_relationship(
        FrozenMemoryInput(1, "space", 1, (1, 2), {}, datetime(2026, 7, 16, tzinfo=UTC), {}, {}, "v1")
    )[0]
    archive_id = archive.archive_id
    snapshot_id = session.scalar(select(MemorySnapshot.snapshot_id).where(MemorySnapshot.archive_id == archive_id))
    assert isinstance(snapshot_id, str)
    ref = MemoryAgentBindingService(session).bind(archive.archive_id, "run-1", 0, snapshot_id=snapshot_id)
    ref.status = "pending"  # 模拟 Runtime start-held 已成功，允许 Worker 读取/发布。
    session.commit()
    session.close()
    path = "/api/v1/internal/agent-tools/memory.publish_playback_document"
    input_data = {"archive_id": archive_id, "run_id": "run-1", "snapshot_id": snapshot_id, "generation_epoch": 0, "document": {"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []}}
    body = json.dumps({"input": input_data}, separators=(",", ":")).encode()
    first = client.post(path, content=body, headers=_tool_headers(path, body, "publish-1"))
    second = client.post(path, content=body, headers=_tool_headers(path, body, "publish-1"))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert factory().scalars(select(MemoryPlaybackDocument)).all().__len__() == 3
    changed = json.dumps({"input": {**input_data, "document": {**input_data["document"], "scenes": [{"scene_id": "x"}]}}}, separators=(",", ":")).encode()
    assert client.post(path, content=changed, headers=_tool_headers(path, changed, "publish-1")).status_code == 409
    missing_snapshot = json.dumps({"input": {**input_data, "snapshot_id": "missing-snapshot"}}, separators=(",", ":")).encode()
    assert client.post(path, content=missing_snapshot, headers=_tool_headers(path, missing_snapshot, "publish-missing-snapshot")).status_code == 403
    unknown_ref = json.dumps({"input": {**input_data, "document": {**input_data["document"], "scenes": [{"scene_id": "s-1", "source_refs": ["diary:not-frozen"]}]} }}, separators=(",", ":")).encode()
    assert client.post(path, content=unknown_ref, headers=_tool_headers(path, unknown_ref, "publish-unknown-ref")).status_code == 403
    media_document = json.dumps({"input": {**input_data, "document": {**input_data["document"], "media_manifest": [{"asset_id": "unbound-media"}]}}}, separators=(",", ":")).encode()
    assert client.post(path, content=media_document, headers=_tool_headers(path, media_document, "publish-unbound-media")).status_code == 403
    result_path = "/api/v1/internal/agent-tools/memory.get_publish_result"
    result_body = json.dumps({"input": {"archive_id": archive_id, "run_id": "run-1"}}, separators=(",", ":")).encode()
    result = client.post(result_path, content=result_body, headers=_tool_headers(result_path, result_body, "publish-1"))
    assert result.status_code == 200
    assert result.json()["output"] == first.json()["output"]


def test_snapshot_tool_requires_bound_run_snapshot_and_generation_epoch(client, caplog) -> None:
    """读取冻结正文必须同时命中当前 Run、归档、快照和 generation，不能靠 archive_id 越权。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    app.state.session_factory = factory
    session = factory()
    archive = MemoryArchiveService(session, app.state.memory_snapshot_cipher).create_archives_for_relationship(
        FrozenMemoryInput(2, "space-2", 1, (3, 4), {}, datetime(2026, 7, 16, tzinfo=UTC), {}, {"diaries": [{"content": "不应进入日志的正文"}]}, "v1")
    )[0]
    snapshot_id = session.scalar(select(MemorySnapshot.snapshot_id).where(MemorySnapshot.archive_id == archive.archive_id))
    assert isinstance(snapshot_id, str)
    archive_id = archive.archive_id
    ref = MemoryAgentBindingService(session).bind(
        archive.archive_id, "run-snapshot", 0, snapshot_id=snapshot_id,
    )
    ref.status = "pending"
    session.commit()
    session.close()

    path = "/api/v1/internal/agent-tools/memory.get_snapshot"
    valid = {"archive_id": archive_id, "snapshot_id": snapshot_id, "run_id": "run-snapshot", "generation_epoch": 0}
    valid_body = json.dumps({"input": valid}, separators=(",", ":")).encode()
    with caplog.at_level(logging.INFO):
        assert client.post(path, content=valid_body, headers=_tool_headers(path, valid_body, "read-1")).status_code == 200
    assert "不应进入日志的正文" not in caplog.text

    wrong_epoch = json.dumps({"input": {**valid, "generation_epoch": 1}}, separators=(",", ":")).encode()
    assert client.post(path, content=wrong_epoch, headers=_tool_headers(path, wrong_epoch, "read-2")).status_code == 403
    wrong_run = json.dumps({"input": {**valid, "run_id": "other-run"}}, separators=(",", ":")).encode()
    assert client.post(path, content=wrong_run, headers=_tool_headers(path, wrong_run, "read-3")).status_code == 403
