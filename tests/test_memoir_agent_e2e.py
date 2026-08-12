"""MemoirAgent MVP 的最小端到端验收，不访问真实模型、工具服务或私密正文。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.db.sqlalchemy_db import Base
from app.models import (
    AgentDefinition,
    AgentRun,
    AgentStep,
    CallbackEvent,
    MemorySnapshot,
)
from app.runtime.artifact import ArtifactStore
from app.runtime.checkpoint import CheckpointStore, FernetCheckpointCipher
from app.runtime.executor import WorkflowExecutor
from app.schemas.agent_run import CreateRunCommand
from app.services.agent_run_service import AgentRunService
from app.services.memory_agent_binding_service import MemoryAgentBindingService
from app.services.memory_agent_callback_service import MemoryAgentCallbackService
from app.services.memory_archive_service import (
    FernetSnapshotCipher,
    FrozenMemoryInput,
    MemoryArchiveService,
)
from app.services.memory_player_service import MemoryPlayerService
from app.services.run_queue_service import RunQueueService
from app.services.tool_call_audit_service import ToolCallAuditService


class _MemoryToolFixture:
    """受控业务工具替身：只返回空素材与发布后的安全摘要。"""

    def __init__(
        self, session, archive_id: str, snapshot_id: str, snapshot_payload: dict[str, object]
    ) -> None:
        self._session = session
        self._archive_id = archive_id
        self._snapshot_id = snapshot_id
        self._snapshot_payload = snapshot_payload

    def get_snapshot(self, *_args: object) -> dict[str, object]:
        return self._snapshot_payload

    def publish_playback_document(
        self,
        _connector_id: str,
        archive_id: str,
        run_id: str,
        snapshot_id: str,
        epoch: int,
        document: dict[str, object],
        _idempotency_key: str,
        _tool_call: object,
        _tool_context: object = None,
    ) -> dict[str, object]:
        assert (archive_id, snapshot_id) == (self._archive_id, self._snapshot_id)
        snapshot = self._session.scalar(
            select(MemorySnapshot).where(MemorySnapshot.snapshot_id == snapshot_id)
        )
        published = MemoryArchiveService(
            self._session, FernetSnapshotCipher(Fernet.generate_key())
        ).publish_playback_document(
            archive_id,
            expected_generation_epoch=epoch,
            expected_run_id=run_id,
            snapshot=snapshot,
            document=document,
        )
        return {"revision": published.revision, "content_digest": published.content_digest}


class _InvalidModelFixture:
    """稳定返回脏结构，验收模型节点不会阻断模板作品发布。"""

    def call(self, _run_id: str, _node_id: str, _request: dict[str, object]) -> object:
        return type("ModelResult", (), {"status": "succeeded", "data": {"unexpected": True}})()


@pytest.mark.parametrize(
    "fixture_name",
    ["empty", "diary_only", "bet_only", "same_day", "blocked_language", "dirty_model"],
)
def test_memoir_mvp_e2e_publishes_only_complete_revision_then_projects_callback(
    fixture_name: str,
) -> None:
    """baseline 可先播放，Worker 终态后才发布完整作品并推进 RunRef。"""
    fixture_path = Path(__file__).parent / "fixtures" / "memoir_snapshots" / f"{fixture_name}.json"
    snapshot_payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    diaries = snapshot_payload.get("diaries", [])
    bets = snapshot_payload.get("bets", [])
    source_manifest = {
        "diary_ids": [item["id"] for item in diaries if isinstance(item, dict) and isinstance(item.get("id"), str)],
        "bet_ids": [item["id"] for item in bets if isinstance(item, dict) and isinstance(item.get("id"), str)],
    }
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive = MemoryArchiveService(session, FernetSnapshotCipher(Fernet.generate_key())).create_archives_for_relationship(
        FrozenMemoryInput(
            1, "space-e2e", 1, (1, 2), {}, datetime(2026, 7, 23, tzinfo=UTC),
            source_manifest, snapshot_payload, "v1",
        )
    )[0]
    baseline = MemoryPlayerService(session).get_published_playback(archive.archive_id)
    assert baseline.document.revision == 0
    snapshot = session.scalar(select(MemorySnapshot).where(MemorySnapshot.archive_id == archive.archive_id))
    assert snapshot is not None
    session.add(AgentDefinition(
        agent_id="memoir_agent", version="1.0.0", runtime_type="workflow",
        definition_json={
            "allowed_business_types": ["couple_memory"],
            "workflow_nodes": [
                {
                    "node_id": node_id,
                    "node_type": node_type,
                    # memoir 读取/内容/发布节点 safe_to_rerun=True；optional 后处理
                    # enqueue_media_tasks=False。必须显式声明，否则 Planner legacy 缺键
                    # guard 拒绝（与 app/agents/memoir_agent/1.0.0/workflow.graph.py 一致）。
                    "safe_to_rerun": node_id != "enqueue_media_tasks",
                    **(
                        {"optional": True}
                        if node_id == "enqueue_media_tasks"
                        else {}
                    ),
                }
                for node_id, node_type in (
                    ("load_snapshot", "tool"), ("sanitize_materials", "deterministic"),
                    ("compute_stats", "deterministic"), ("extract_highlights", "model"),
                    ("plan_chapters", "model"), ("generate_scenes", "model"),
                    ("generate_actions", "deterministic"), ("safety_review", "guardrail"),
                    ("publish_document", "tool"),
                    ("enqueue_media_tasks", "deterministic"),
                )
            ],
        },
        package_digest="sha256:memoir", contract_version="1.0.0", status="active",
        status_changed_at=datetime.now(UTC), status_changed_by="test", status_change_reason="e2e",
    ))
    session.commit()
    created = AgentRunService(session).create(
        CreateRunCommand(
            agent_id="memoir_agent", agent_version="1.0.0", business_type="couple_memory",
            business_id=archive.archive_id, start_mode="held",
            input={"archive_id": archive.archive_id, "snapshot_id": snapshot.snapshot_id, "generation_epoch": 0},
            callback_target_id="memory_callback", business_connector_id="couple_diary_backend",
        ),
        "caller", "tenant", "e2e-create",
    )
    MemoryAgentBindingService(session).bind(archive.archive_id, created.run_id, 0, snapshot_id=snapshot.snapshot_id)
    AgentRunService(session).start(created.run_id, "caller", "e2e-start")
    session.commit()

    executor = WorkflowExecutor(
        session,
        MemoirNodeRunner(
            _MemoryToolFixture(
                session, archive.archive_id, snapshot.snapshot_id, snapshot_payload
            ),
            ToolCallAuditService(session),
            model_gateway=_InvalidModelFixture(),
        ),
        CheckpointStore(session, FernetCheckpointCipher.generate()),
        ArtifactStore(session),
    )
    assert RunQueueService(session, executor, "e2e-worker").consume(created.run_id)

    run = session.scalar(select(AgentRun).where(AgentRun.run_id == created.run_id))
    assert run is not None and (run.status, run.dispatch_state) == ("succeeded", "finished")
    published = MemoryPlayerService(session).get_published_playback(archive.archive_id)
    assert published.document.revision == 1
    assert published.document.document_json["media_manifest"] == []
    # 模型输出的正常作品必须在 MVP 3～8 张范围内；最终发布硬上限仍由校验器守住。
    assert 3 <= len(published.scenes) <= 8
    assert all(
        not isinstance(scene.payload_json.get("body"), str)
        or len(scene.payload_json["body"]) <= 80
        for scene in published.scenes
    )
    allowed_refs = {
        *(f"diary:{source_id}" for source_id in source_manifest["diary_ids"]),
        # R2 后 Runtime 经 legacy reader 单向归一化为 completed_bet 前缀；
        # e2e allowed_refs 与发布端 memory_archive_service 校验同步使用规范前缀。
        *(f"completed_bet:{source_id}" for source_id in source_manifest["bet_ids"]),
    }
    # 发布的引用只能来自当前冻结 manifest，Document 不保存 fixture 的素材正文。
    assert all(set(scene.source_refs_json) <= allowed_refs for scene in published.scenes)
    assert "content" not in json.dumps(published.document.document_json, ensure_ascii=False)
    media_step = session.scalar(
        select(AgentStep).where(
            AgentStep.run_id == created.run_id,
            AgentStep.step_name == "enqueue_media_tasks",
        )
    )
    assert media_step is not None
    assert (media_step.status, media_step.output_summary) == (
        "skipped",
        {"node_id": "enqueue_media_tasks", "status": "skipped"},
    )
    callbacks = list(session.scalars(
        select(CallbackEvent).where(CallbackEvent.run_id == created.run_id).order_by(CallbackEvent.event_seq)
    ))
    # Runtime 只投影节点名与状态；不将快照、模型输出或完整作品发送给业务端。
    assert callbacks[0].event_type == "run_started"
    assert callbacks[-1].event_type == "run_succeeded"
    assert [item.event_seq for item in callbacks] == list(range(1, len(callbacks) + 1))
    assert all(
        set(item.payload_json) <= {
            "event", "event_id", "run_id", "event_seq", "status_version", "agent_id",
            "business_id", "status", "error", "public_trace",
        }
        and all(set(trace) <= {"step", "status"} for trace in item.payload_json["public_trace"])
        for item in callbacks
    )
    assert "private-marker" not in json.dumps(
        [item.payload_json for item in callbacks], ensure_ascii=False
    )
    step_callback = next(item for item in callbacks if item.event_type == "step_changed")
    success_callback = next(item for item in callbacks if item.event_type == "run_succeeded")
    projector = MemoryAgentCallbackService(session)
    assert projector.apply(callbacks[0].payload_json)
    assert projector.apply(step_callback.payload_json)
    assert step_callback.payload_json["public_trace"] == [{"step": "load_snapshot", "status": "succeeded"}]
    # 乱序旧事件和同一成功事件重放都不能倒退或重复写业务状态。
    assert projector.apply(callbacks[0].payload_json) is False
    assert projector.apply(success_callback.payload_json)
    assert projector.apply(success_callback.payload_json) is False
    session.commit()


def test_old_generation_cannot_publish_even_when_document_is_complete() -> None:
    """删除/重生后的迟到 Worker 不能用旧 epoch 覆盖 baseline。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    service = MemoryArchiveService(session, FernetSnapshotCipher(Fernet.generate_key()))
    archive = service.create_archives_for_relationship(
        FrozenMemoryInput(1, "space-e2e", 1, (1, 2), {}, datetime(2026, 7, 23, tzinfo=UTC), {}, {}, "v1")
    )[0]
    archive.generation_epoch = 1
    with pytest.raises(ValueError, match="GENERATION_SUPERSEDED"):
        service.publish_playback_document(
            archive.archive_id,
            expected_generation_epoch=0,
            document={"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []},
        )
    assert MemoryPlayerService(session).get_published_playback(archive.archive_id).document.revision == 0
