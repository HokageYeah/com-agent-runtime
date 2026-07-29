"""Memoir 发布节点的失败审计回归。"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import httpx
import pytest
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


def test_publish_persists_running_audit_before_http_call(tmp_path: Path) -> None:
    """写请求开始时，独立事务已经能够查询到 running 审计。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'publish-audit.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    session = sessions()
    observed: list[tuple[str, str, str]] = []

    class Gateway:
        def publish_playback_document(self, *args: object) -> dict[str, object]:
            with sessions() as observer:
                record = observer.scalar(select(AgentToolCall))
                assert record is not None
                observed.append(
                    (record.status, record.logical_operation_key, record.request_digest)
                )
            return {"revision": 1, "content_digest": "published-digest"}

    document = {
        "schema_version": "1.0.0",
        "scenes": [],
        "actions": [],
        "media_manifest": [],
    }
    expected_digest = hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    MemoirNodeRunner(Gateway(), ToolCallAuditService(session)).run_node(
        {"node_id": "publish_document"}, _run(), AgentState(playback_document=document)
    )

    key = "run-1:publish_document:memory.publish_playback_document:0"
    assert observed == [("running", key, expected_digest)]


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


def test_publish_idempotency_conflict_reconciles_matching_digest_without_replay() -> None:
    """业务端已提交同一逻辑键时，409 只能经安全摘要对账恢复。"""
    document = {"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []}
    digest = hashlib.sha256(json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()

    class Gateway:
        def publish_playback_document(self, *args: object) -> dict[str, object]:
            request = httpx.Request("POST", "http://business")
            raise httpx.HTTPStatusError(
                "conflict", request=request, response=httpx.Response(409, request=request)
            )

        def get_publish_result(self, *args: object) -> dict[str, object]:
            return {"revision": 7, "content_digest": digest}

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    state = AgentState(playback_document=document)

    assert MemoirNodeRunner(Gateway(), ToolCallAuditService(session)).run_node(
        {"node_id": "publish_document"}, _run(), state
    ) == {"node_id": "publish_document", "published": True}
    record = session.scalar(select(AgentToolCall))
    assert record is not None and record.status == "succeeded"
    assert record.output_summary == {"revision": 7, "content_digest": digest}


def test_publish_idempotency_conflict_rejects_different_digest_without_body_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """409 返回其他作品的 digest 不得被视为本次发布成功。"""
    sensitive = "完整播放文档 prompt 私有URL"

    class Gateway:
        def publish_playback_document(self, *args: object) -> dict[str, object]:
            request = httpx.Request("POST", "http://business")
            raise httpx.HTTPStatusError(
                sensitive, request=request, response=httpx.Response(409, request=request)
            )

        def get_publish_result(self, *args: object) -> dict[str, object]:
            return {"revision": 7, "content_digest": "different-digest"}

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    state = AgentState(playback_document={
        "schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": [],
    })

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="TOOL_IDEMPOTENCY_CONFLICT"):
        MemoirNodeRunner(Gateway(), ToolCallAuditService(session)).run_node(
            {"node_id": "publish_document"}, _run(), state
        )
    record = session.scalar(select(AgentToolCall))
    assert record is not None and (record.status, record.error_code) == (
        "failed", "TOOL_IDEMPOTENCY_CONFLICT"
    )
    assert sensitive not in caplog.text


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


def test_publish_failure_log_does_not_include_exception_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_body = "日记正文 prompt 完整播放文档"

    class Gateway:
        def publish_playback_document(self, *args: object) -> dict[str, object]:
            raise RuntimeError(sensitive_body)

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

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError):
        MemoirNodeRunner(Gateway(), ToolCallAuditService(session)).run_node(
            {"node_id": "publish_document"}, _run(), state
        )

    assert sensitive_body not in caplog.text
    assert "TOOL_CALL_FAILED" in caplog.text


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
    document = {
        "schema_version": "1.0.0",
        "scenes": [],
        "actions": [],
        "media_manifest": [],
    }
    digest = hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    record = audit.begin_publish("run-1", 1, key, key, digest)
    audit.unknown(record, "HTTP_TIMEOUT")
    state = AgentState(playback_document=document)

    assert MemoirNodeRunner(Gateway(), audit).run_node(
        {"node_id": "publish_document"}, _run(), state
    ) == {"node_id": "publish_document", "published": True}
    assert state.publish_result == {"revision": 2, "content_digest": "digest"}
    saved = session.scalar(
        select(AgentToolCall).where(
            AgentToolCall.tool_call_id == record.tool_call_id
        )
    )
    assert saved is not None and saved.status == "succeeded"


def test_publish_takeover_only_reconciles_unknown_and_never_replays_write() -> None:
    reconciliation_calls: list[tuple[object, ...]] = []
    publish_calls: list[tuple[object, ...]] = []

    class Gateway:
        def get_publish_result(self, *args: object) -> None:
            reconciliation_calls.append(args)
            return None

        def publish_playback_document(self, *args: object) -> dict[str, object]:
            publish_calls.append(args)
            return {"revision": 3, "content_digest": "digest"}

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    audit = ToolCallAuditService(session)
    key = "run-1:publish_document:memory.publish_playback_document:0"
    document = {"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []}
    digest = hashlib.sha256(json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    # 已知未知结果与接管后重试必须表示同一份规范化播放文档。
    old = audit.begin_publish("run-1", 1, key, key, digest)
    audit.unknown(old, "HTTP_TIMEOUT")
    # 模拟 lease 接管后的第二个 execution attempt。
    run = _run()
    run.execution_attempt = 2
    state = AgentState(playback_document=document)

    with pytest.raises(RuntimeError, match="PUBLISH_OUTCOME_UNKNOWN"):
        MemoirNodeRunner(Gateway(), audit).run_node(
            {"node_id": "publish_document"}, run, state
        )

    records = session.scalars(select(AgentToolCall).order_by(AgentToolCall.id)).all()
    assert len(records) == 1
    assert all(record.logical_operation_key == key and record.idempotency_key == key for record in records)
    assert records[0].request_digest == digest
    assert reconciliation_calls == [("connector", "archive", "run-1", key)]
    assert publish_calls == []


def test_publish_takeover_reconciles_committed_success_without_replaying_write() -> None:
    reconciliation_calls: list[tuple[object, ...]] = []
    publish_calls: list[tuple[object, ...]] = []

    class Gateway:
        def get_publish_result(self, *args: object) -> None:
            reconciliation_calls.append(args)
            return None

        def publish_playback_document(self, *args: object) -> dict[str, object]:
            publish_calls.append(args)
            return {"revision": 4, "content_digest": "unexpected"}

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    audit = ToolCallAuditService(session)
    key = "run-1:publish_document:memory.publish_playback_document:0"
    document = {
        "schema_version": "1.0.0",
        "scenes": [],
        "actions": [],
        "media_manifest": [],
    }
    digest = hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    committed = audit.begin_publish("run-1", 1, key, key, digest)
    audit.succeed(committed, 3, "published-digest")
    run = _run()
    run.execution_attempt = 2

    with pytest.raises(RuntimeError, match="PUBLISH_OUTCOME_UNKNOWN"):
        MemoirNodeRunner(Gateway(), audit).run_node(
            {"node_id": "publish_document"},
            run,
            AgentState(playback_document=document),
        )

    records = session.scalars(select(AgentToolCall)).all()
    assert len(records) == 1
    assert records[0].status == "succeeded"
    assert reconciliation_calls == [("connector", "archive", "run-1", key)]
    assert publish_calls == []
