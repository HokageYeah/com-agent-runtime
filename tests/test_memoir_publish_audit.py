"""Memoir 发布节点的失败审计回归。"""

from __future__ import annotations

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.db.sqlalchemy_db import Base
from app.models import AgentToolCall
from app.runtime.state import AgentState
from app.services.tool_call_audit_service import ToolCallAuditService


def _run() -> object:
    return type(
        "Run",
        (),
        {
            "run_id": "run-1",
            "execution_attempt": 1,
            "business_connector_id": "connector",
            "input_json": {"archive_id": "archive", "snapshot_id": "snapshot", "generation_epoch": 0},
        },
    )()


def test_publish_http_5xx_persists_failed_audit_record() -> None:
    class Gateway:
        def publish_playback_document(self, *args: object) -> dict[str, object]:
            request = httpx.Request("POST", "http://business")
            raise httpx.HTTPStatusError(
                "failed", request=request, response=httpx.Response(503, request=request)
            )

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    state = AgentState(
        playback_document={
            "schema_version": "1.0.0",
            "scenes": [],
            "actions": [],
            "media_manifest": [],
        }
    )
    try:
        MemoirNodeRunner(Gateway(), ToolCallAuditService(session)).run_node(
            {"node_id": "publish_document"}, _run(), state
        )
    except httpx.HTTPStatusError:
        pass
    else:
        raise AssertionError("5xx 必须向上失败")
    record = session.scalar(select(AgentToolCall))
    assert record is not None and (record.status, record.error_code) == (
        "failed",
        "HTTP_503",
    )
    assert state.publish_result is None


def test_publish_timeout_persists_unknown_audit_record() -> None:
    class Gateway:
        def publish_playback_document(self, *args: object) -> dict[str, object]:
            raise httpx.ReadTimeout("timeout")

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    state = AgentState(
        playback_document={
            "schema_version": "1.0.0",
            "scenes": [],
            "actions": [],
            "media_manifest": [],
        }
    )
    try:
        MemoirNodeRunner(Gateway(), ToolCallAuditService(session)).run_node(
            {"node_id": "publish_document"}, _run(), state
        )
    except httpx.TimeoutException:
        pass
    else:
        raise AssertionError("超时必须向上失败")
    record = session.scalar(select(AgentToolCall))
    assert record is not None and (record.status, record.error_code) == (
        "outcome_unknown",
        "HTTP_TIMEOUT",
    )


def test_publish_retry_reconciles_unknown_result_before_replaying() -> None:
    class Gateway:
        def get_publish_result(self, *args: object) -> dict[str, object]:
            return {"revision": 2, "content_digest": "digest"}

        def publish_playback_document(self, *args: object) -> dict[str, object]:
            raise AssertionError("对账命中后不得重复发布")

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    audit = ToolCallAuditService(session)
    key = "run-1:publish_document:memory.publish_playback_document:0"
    record = audit.begin_publish("run-1", 1, key, key, "digest")
    audit.unknown(record, "HTTP_TIMEOUT")
    state = AgentState(playback_document={"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []})

    assert MemoirNodeRunner(Gateway(), audit).run_node(
        {"node_id": "publish_document"}, _run(), state
    ) == {"node_id": "publish_document", "published": True}
    assert state.publish_result == {"revision": 2, "content_digest": "digest"}
    assert record.status == "succeeded"


def test_publish_retry_reuses_original_key_when_reconciliation_misses() -> None:
    calls: list[tuple[object, ...]] = []

    class Gateway:
        def get_publish_result(self, *args: object) -> None:
            return None

        def publish_playback_document(self, *args: object) -> dict[str, object]:
            calls.append(args)
            return {"revision": 3, "content_digest": "digest"}

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    audit = ToolCallAuditService(session)
    key = "run-1:publish_document:memory.publish_playback_document:0"
    old = audit.begin_publish("run-1", 1, key, key, "digest")
    audit.unknown(old, "HTTP_TIMEOUT")
    # 模拟 lease 接管后的第二个 execution attempt。
    run = _run()
    run.execution_attempt = 2
    state = AgentState(playback_document={"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []})

    MemoirNodeRunner(Gateway(), audit).run_node({"node_id": "publish_document"}, run, state)

    records = session.scalars(select(AgentToolCall).order_by(AgentToolCall.id)).all()
    assert len(records) == 2
    assert all(record.logical_operation_key == key and record.idempotency_key == key for record in records)
    assert records[-1].execution_attempt == 2
    assert calls[0][-1] == key
