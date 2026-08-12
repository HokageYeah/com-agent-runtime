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
            "agent_id": "memoir_agent",
            "agent_version": "1.0.0",
            "run_id": "run-1",
            "execution_attempt": 1,
            "business_connector_id": "connector",
            "business_type": "couple_memory",
            "business_id": "archive",
            "trace_id": "trace-1",
            "input_json": {"archive_id": "archive", "snapshot_id": "snapshot", "generation_epoch": 0},
        },
    )()


def _publish_tool_context() -> dict[str, str]:
    return {
        "agent_id": "memoir_agent",
        "agent_version": "1.0.0",
        "run_id": "run-1",
        "step_id": "publish_document",
        "business_type": "couple_memory",
        "business_id": "archive",
        "trace_id": "trace-1",
    }


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
    assert reconciliation_calls == [
            ("connector", "archive", "snapshot", "run-1", 0, key, _publish_tool_context())
    ]
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
    assert reconciliation_calls == [
            ("connector", "archive", "snapshot", "run-1", 0, key, _publish_tool_context())
    ]
    assert publish_calls == []


def test_publish_resume_reconciles_first_commit_when_recomputed_digest_drifts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """模型重算让 playback_document digest 漂移时，按稳定 logical_key 复用首轮提交，不重发。

    query-after-commit 的首要查询坐标是稳定 logical_key，不是本次重算 digest：首轮已成功
    发布 document_A（digest_A），resume 重算得到 document_B（digest_B≠digest_A），仍能命中
    首轮 succeeded attempt、对账复用业务端权威 revision，绝不第二次物理写入。修复前用漂移
    digest 查 latest_committed 查不到 → begin_publish 触发 TOOL_CALL_OPERATION_CONFLICT。
    """
    first_document = {
        "schema_version": "1.0.0",
        "scenes": [{"scene_id": "s1", "scene_type": "summary", "source_refs": []}],
        "actions": [{"action_id": "a1", "scene_id": "s1", "action_type": "show_card", "duration_ms": 3000}],
        "media_manifest": [],
    }
    drifted_document = {
        "schema_version": "1.0.0",
        "scenes": [{"scene_id": "s1", "scene_type": "summary", "source_refs": [], "body": "RECOMPUTED_MODEL_BODY"}],
        "actions": [{"action_id": "a1", "scene_id": "s1", "action_type": "show_card", "duration_ms": 3000}],
        "media_manifest": [],
    }
    first_digest = hashlib.sha256(json.dumps(
        first_document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    drifted_digest = hashlib.sha256(json.dumps(
        drifted_document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert first_digest != drifted_digest  # 确认模型重算确实让 digest 漂移

    key = "run-1:publish_document:memory.publish_playback_document:0"
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    audit = ToolCallAuditService(session)
    # 首轮已成功发布 document_A，业务端权威 revision=5。
    committed = audit.begin_publish("run-1", 1, key, key, first_digest)
    audit.succeed(committed, 5, "business-published-digest")

    class Gateway:
        def __init__(self) -> None:
            self.reconcile_calls: list[tuple[object, ...]] = []

        def publish_playback_document(self, *args: object) -> dict[str, object]:
            raise AssertionError("首轮已提交，digest 漂移后不得第二次物理写入")

        def get_publish_result(self, *args: object) -> dict[str, object]:
            self.reconcile_calls.append(args)
            return {"revision": 5, "content_digest": "business-published-digest"}

    gateway = Gateway()
    state = AgentState(playback_document=drifted_document)
    with caplog.at_level(logging.DEBUG):
        assert MemoirNodeRunner(gateway, audit).run_node(
            {"node_id": "publish_document"}, _run(), state
        ) == {"node_id": "publish_document", "published": True}

    assert gateway.reconcile_calls == [
            ("connector", "archive", "snapshot", "run-1", 0, key, _publish_tool_context())
    ]
    assert state.publish_result == {"revision": 5, "content_digest": "business-published-digest"}
    records = session.scalars(select(AgentToolCall)).all()
    assert len(records) == 1  # 无第二次物理写入 attempt
    assert records[0].status == "succeeded"
    # 漂移后的模型正文不得进入任何级别日志。
    assert "RECOMPUTED_MODEL_BODY" not in caplog.text


class _ModelResult:
    """模型网关最小返回对象：status + data，沿 _model_data 的 getattr 契约。"""

    def __init__(self, status: str, data: object) -> None:
        self.status = status
        self.data = data


class _DriftModelGateway:
    """模拟 resume 时模型重算：generate_scenes 两轮故意产出不同 scenes，digest 因此漂移。

    首轮返回 3 个 scene（FIRST_ROUND_MODEL_BODY），resume 返回 4 个（RESUME_MODEL_BODY）。
    其它节点不可用，确保只有 generate_scenes 注入模型差异。
    """

    def __init__(self) -> None:
        self._seq = 0

    def call(self, run_id: str, node_id: str, request: object) -> _ModelResult:
        if node_id == "generate_scenes":
            self._seq += 1
            body = "FIRST_ROUND_MODEL_BODY" if self._seq == 1 else "RESUME_MODEL_BODY"
            count = 3 if self._seq == 1 else 4
            return _ModelResult("succeeded", {"scenes": [
                {"scene_id": f"s{i}", "scene_type": "summary", "source_refs": [], "body": body}
                for i in range(1, count + 1)
            ]})
        return _ModelResult("unavailable", None)

    def repair(self, *args: object) -> _ModelResult:
        return _ModelResult("unavailable", None)


def _document_digest(document: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def test_publish_resume_after_model_recompute_reuses_first_commit_without_double_publish(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """真实模型重算 → digest 漂移 → publish 不双发的端到端回归。

    非 model_gateway=None 模板：真实 MemoirNodeRunner + 真实 ToolCallAuditService +
    非 None model_gateway，走 generate_scenes→generate_actions→safety_review→publish_document
    尾节点链两轮。首轮模型产出 3 scenes、resume 重算产出 4 scenes，playback_document digest
    漂移。首轮 publish 物理写入 1 次；resume 必须走 get_publish_result 对账、复用首轮 revision，
    绝不第二次物理写入，两次模型正文不进任何级别日志。
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    audit = ToolCallAuditService(session)

    class Gateway:
        def __init__(self) -> None:
            self.publish_calls: list[str] = []
            self.reconcile_calls: list[str] = []

        def publish_playback_document(self, *args: object) -> dict[str, object]:
            self.publish_calls.append(args[6])  # idempotency_key=logical_key
            return {"revision": 1, "content_digest": "business-published-digest"}

        def get_publish_result(self, *args: object) -> dict[str, object]:
            self.reconcile_calls.append(args[5])  # idempotency_key
            return {"revision": 1, "content_digest": "business-published-digest"}

    gateway = Gateway()
    runner = MemoirNodeRunner(gateway, audit, model_gateway=_DriftModelGateway())
    # chapter_plan 预置（模拟 R2 resume 从 checkpoint 恢复到 generate_scenes），
    # source_refs=[] 让模型 scenes 的空引用集天然通过 grounding。
    chapter_plan = {"chapters": [{"chapter_id": "chapter-1", "source_refs": [], "kind": "memory_overview"}]}
    tail_nodes = [
        {"node_id": "generate_scenes"},
        {"node_id": "generate_actions"},
        {"node_id": "safety_review"},
        {"node_id": "publish_document"},
    ]
    run = _run()
    key = "run-1:publish_document:memory.publish_playback_document:0"

    # 首轮：模型产出 3 scenes（FIRST_ROUND_MODEL_BODY），publish 首次物理写入。
    state1 = AgentState(chapter_plan=chapter_plan)
    with caplog.at_level(logging.DEBUG):
        for node in tail_nodes:
            runner.run_node(node, run, state1)
    first_digest = _document_digest(state1.playback_document)
    assert state1.publish_result == {"revision": 1, "content_digest": "business-published-digest"}
    assert gateway.publish_calls == [key]

    # resume：R2 checkpoint 不存正文，state 重建；模型重算产出 4 scenes（RESUME_MODEL_BODY）。
    state2 = AgentState(chapter_plan=chapter_plan)
    with caplog.at_level(logging.DEBUG):
        for node in tail_nodes:
            runner.run_node(node, run, state2)
    resumed_digest = _document_digest(state2.playback_document)

    assert resumed_digest != first_digest  # 模型重算确实让 digest 漂移
    assert len(gateway.publish_calls) == 1  # resume 未第二次物理写入
    assert gateway.reconcile_calls == [key]  # resume 走 get_publish_result 对账
    assert state2.publish_result == {"revision": 1, "content_digest": "business-published-digest"}  # 复用首轮
    records = session.scalars(select(AgentToolCall)).all()
    assert len(records) == 1  # AgentToolCall 无第二次写入 attempt
    assert records[0].status == "succeeded"
    # 两次模型输出正文均不得进入任何级别日志。
    assert "FIRST_ROUND_MODEL_BODY" not in caplog.text
    assert "RESUME_MODEL_BODY" not in caplog.text
