"""Task 6：静态计划执行、步骤审计与安全 checkpoint 回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pytest import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import (
    AgentArtifact,
    AgentCheckpoint,
    AgentDefinition,
    AgentPlan,
    AgentRun,
    AgentStep,
    CallbackEvent,
    RuntimeAuditRecord,
)
from app.runtime.artifact import ArtifactStore
from app.runtime.checkpoint import (
    CheckpointError,
    CheckpointStore,
    FernetCheckpointCipher,
)
from app.runtime.executor import RetryableWorkflowNodeError, WorkflowExecutor
from app.runtime.interfaces import LeaseContext
from app.runtime.state import AgentState
from app.services.lease_service import LeaseService


class DeterministicNodeRunner:
    """不访问网络或业务数据的测试节点执行器。"""

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]:
        return {"node_id": node["node_id"], "result": "ok"}


class RecordingNodeRunner(DeterministicNodeRunner):
    def __init__(self) -> None:
        self.node_ids: list[str] = []

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]:
        self.node_ids.append(str(node["node_id"]))
        return super().run_node(node, run, state)


class RetryOnceNodeRunner(DeterministicNodeRunner):
    """首轮返回受控临时失败，第二轮才产生节点结果。"""

    def __init__(self) -> None:
        self.calls = 0

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            raise RetryableWorkflowNodeError("TRANSIENT_NODE_FAILURE")
        return super().run_node(node, run, state)


class OptionalFailureThenSuccessRunner(RecordingNodeRunner):
    """仅模拟发布后的可选媒体步骤首次失败。"""

    def __init__(self) -> None:
        super().__init__()
        self.optional_calls = 0

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]:
        self.node_ids.append(str(node["node_id"]))
        if node["node_id"] == "enqueue_media":
            self.optional_calls += 1
            if self.optional_calls == 1:
                raise RuntimeError("MEDIA_QUEUE_UNAVAILABLE")
        return {"node_id": node["node_id"], "result": "ok"}


class SnapshotNodeRunner(DeterministicNodeRunner):
    """模拟受信任工具返回私密快照，验证其只进入加密状态。"""

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]:
        state.snapshot = {"diaries": [{"content": "私密正文"}]}
        return {"node_id": node["node_id"], "snapshot_loaded": True}


class SensitiveStateNodeRunner(DeterministicNodeRunner):
    """R2 哨兵 runner：在每个节点塞入五类正文 + Scene + PlaybackDocument + tool payload。

    用来证明 checkpoint 即便加密也不含以下正文（L550 新增断言）：
    - 五类素材正文（日记/赌局/手账/愿望/清单）
    - Scene / PlaybackDocument / 模型中间文本 / sanitized_material / publish_result
    """

    SENTINELS = (
        "diary-private-marker",
        "completed_bet-private-marker",
        "handbook_note-private-marker",
        "matured_wish-private-marker",
        "bucket_list_completion-private-marker",
        "scene-private-marker",
        "playback-private-marker",
        "tool-payload-private-marker",
    )

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]:
        # 把所有可能的私密字段都塞进 state；R2 checkpoint 白名单必须全部剔除。
        state.snapshot = {
            "diaries": [{"id": "d1", "content": "diary-private-marker"}],
            "bet_items": [{"id": "b1", "content": "completed_bet-private-marker"}],
            "handbook_notes": [{"id": "h1", "content": "handbook_note-private-marker"}],
            "matured_wishes": [{"id": "w1", "content": "matured_wish-private-marker"}],
            "bucket_list_completions": [{"id": "c1", "content": "bucket_list_completion-private-marker"}],
        }
        state.sanitized_material = {"materials": [{"source_ref": "diary:d1", "summary": "diary-private-marker"}]}
        state.scenes = [{"scene_id": "scene-1", "scene_type": "summary", "source_refs": [], "body": "scene-private-marker"}]
        state.actions = [{"action_id": "a", "scene_id": "scene-1", "action_type": "show_card", "duration_ms": 1000}]
        state.chapter_plan = {"chapters": [{"chapter_id": "c", "source_refs": [], "kind": "memory_overview"}]}
        state.highlights = {"source_refs": [], "mode": "template"}
        state.playback_document = {"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": [], "private": "playback-private-marker"}
        state.publish_result = {"revision": 1, "content_digest": "tool-payload-private-marker"}
        state.safety_report = {"decision": "passed"}
        state.media_tasks = []
        state.trust_metadata = {"hint": "tool-payload-private-marker"}
        state.errors.append("tool-payload-private-marker")
        return {"node_id": node["node_id"], "result": "ok"}


def _executor(session, node_runner: DeterministicNodeRunner) -> WorkflowExecutor:
    """为每个测试注入独立密钥，避免依赖环境中的生产私钥。"""
    return WorkflowExecutor(
        session,
        node_runner,
        CheckpointStore(session, FernetCheckpointCipher.generate()),
        ArtifactStore(session),
    )


def _add_active_test_package(session, changed_at: datetime) -> None:
    """正常执行夹具显式装配与 Run 冻结身份一致的有效 Package。"""
    session.add(
        AgentDefinition(
            agent_id="memoir_agent",
            version="1.0.0",
            runtime_type="workflow",
            definition_json={},
            package_digest="sha256:test",
            contract_version="1.0.0",
            status="active",
            status_changed_at=changed_at,
            status_changed_by="test",
            status_change_reason="fixture",
        )
    )


def _add_claimed_two_node_run(
    session, run_id: str, *, include_active_package: bool = True
) -> datetime:
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id=run_id,
            agent_id="memoir_agent",
            agent_version="1.0.0",
            package_digest="sha256:test",
            contract_version="1.0.0",
            business_type="couple_memory",
            business_id="archive",
            status="pending",
            dispatch_state="claimed",
            input_json={},
            authorization_version=1,
            caller_id="caller",
            tenant_id="tenant",
            create_idempotency_key="key",
            callback_target_id="callback",
            business_connector_id="connector",
            trace_id="trace",
            execution_attempt=1,
            lease_owner="worker-a",
            fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60),
            run_deadline_at=now + timedelta(days=1),
        )
    )
    if include_active_package:
        _add_active_test_package(session, now)
    session.add(
        AgentPlan(
            plan_id=f"{run_id}-plan",
            run_id=run_id,
            strategy="static_workflow",
            steps_json=[
                {"node_id": "load_snapshot", "node_type": "tool"},
                {"node_id": "compute_stats", "node_type": "deterministic"},
            ],
            stop_conditions_json={},
            fallback_policy_json={},
            status="planned",
        )
    )
    session.commit()
    return now


def _lease_context(now: datetime) -> LeaseContext:
    return LeaseContext(
        execution_attempt=1,
        lease_owner="worker-a",
        fencing_token=1,
        lease_expires_at=now + timedelta(seconds=60),
        privacy_version=1,
        authorization_version=1,
    )


def test_executor_writes_step_and_checkpoint_for_every_static_plan_node() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="executor-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="claimed", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
    )
    session.add(run)
    session.add(
        AgentPlan(
            plan_id="executor-plan", run_id="executor-run", strategy="static_workflow",
            steps_json=[
                {"node_id": "load_snapshot", "node_type": "tool"},
                {"node_id": "compute_stats", "node_type": "deterministic"},
            ],
            stop_conditions_json={}, fallback_policy_json={}, status="planned",
        )
    )
    _add_active_test_package(session, now)
    session.commit()

    result = _executor(session, DeterministicNodeRunner()).run(
        "executor-run",
        LeaseContext(
            execution_attempt=1, lease_owner="worker-a", fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
            authorization_version=1,
        ),
    )

    assert result.status == "succeeded"
    assert result.output_summary == {"completed_steps": 2}
    assert [step.status for step in session.scalars(select(AgentStep)).all()] == [
        "succeeded",
        "succeeded",
    ]
    checkpoints = session.scalars(select(AgentCheckpoint)).all()
    assert len(checkpoints) == 2
    # 恢复状态必须是密文；安全摘要只保留节点进度，不能暴露运行私密数据。
    assert all(checkpoint.encrypted_state_blob is not None for checkpoint in checkpoints)
    assert [checkpoint.state_summary["completed_steps"] for checkpoint in checkpoints] == [1, 2]
    assert checkpoints[-1].state_summary["completed_node_ids"] == [
        "compute_stats",
        "load_snapshot",
    ]
    artifacts = session.scalars(select(AgentArtifact)).all()
    assert len(artifacts) == 2
    assert artifacts[0].summary_json == {
        "node_id": "load_snapshot",
        "result_keys": ["node_id", "result"],
    }
    assert artifacts[0].business_resource_ref == "business://couple_memory/archive"
    callbacks = session.scalars(select(CallbackEvent).order_by(CallbackEvent.event_seq)).all()
    assert [(event.event_type, event.payload_json["public_trace"]) for event in callbacks] == [
        ("run_started", []),
        ("step_changed", [{"step": "load_snapshot", "status": "succeeded"}]),
        ("step_changed", [{"step": "compute_stats", "status": "succeeded"}]),
    ]


def test_executor_accumulates_only_node_execution_time(monkeypatch: MonkeyPatch) -> None:
    """活跃预算只在节点运行区间累加，恢复/排队前后的间隔不参与计算。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(session, "active-time-run")
    ticks = iter((0.0, 0.0, 1.25, 1.25, 1.25, 3.0, 3.0))
    monkeypatch.setattr("app.runtime.executor.monotonic", lambda: next(ticks))

    result = _executor(session, DeterministicNodeRunner()).run(
        "active-time-run", _lease_context(now)
    )

    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "active-time-run"))
    assert result.status == "succeeded"
    assert run is not None and run.active_elapsed_ms == 3_000


def test_executor_retries_retryable_node_with_frozen_step_budget() -> None:
    """节点自动重试必须按 step_id 计数，且只消耗冻结的节点额度。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(session, "node-retry-run")
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "node-retry-run"))
    assert run is not None
    run.capability_snapshot_json = {"execution_policy": {"max_auto_retry_per_step": 1}}
    session.commit()
    runner = RetryOnceNodeRunner()

    result = _executor(session, runner).run("node-retry-run", _lease_context(now))

    steps = session.scalars(select(AgentStep).order_by(AgentStep.created_at)).all()
    assert result.status == "succeeded"
    assert runner.calls == 3
    assert run.auto_retry_count == 1
    assert steps[0].step_attempt == 2


def test_executor_refuses_revoked_package_before_starting_any_node() -> None:
    """已撤销 Package 不能在旧 lease 下继续启动模型、工具或其它节点。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(
        session, "revoked-package-run", include_active_package=False
    )
    session.add(
        AgentDefinition(
            agent_id="memoir_agent", version="1.0.0", runtime_type="workflow",
            definition_json={}, package_digest="sha256:test", contract_version="1.0.0",
            status="revoked", status_changed_at=now, status_changed_by="test",
            status_change_reason="fixture",
        )
    )
    session.commit()
    runner = RecordingNodeRunner()

    result = _executor(session, runner).run("revoked-package-run", _lease_context(now))

    assert (result.status, result.error_code) == ("cancelled", "PACKAGE_REVOKED")
    assert runner.node_ids == []


def test_executor_refuses_deprecated_package_before_starting_any_node() -> None:
    """deprecated Package 不是可执行 Package，旧 lease 只能安全取消。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(
        session, "deprecated-package-run", include_active_package=False
    )
    session.add(
        AgentDefinition(
            agent_id="memoir_agent", version="1.0.0", runtime_type="workflow",
            definition_json={}, package_digest="sha256:test", contract_version="1.0.0",
            status="deprecated", status_changed_at=now, status_changed_by="test",
            status_change_reason="fixture",
        )
    )
    session.commit()
    runner = RecordingNodeRunner()

    result = _executor(session, runner).run("deprecated-package-run", _lease_context(now))

    assert (result.status, result.error_code) == ("cancelled", "PACKAGE_REVOKED")
    assert runner.node_ids == []


def test_executor_refuses_missing_definition_before_starting_any_node() -> None:
    """Run 的冻结 Package Definition 缺失时，旧 lease 不能启动任何节点。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(
        session, "missing-package-definition-run", include_active_package=False
    )
    runner = RecordingNodeRunner()

    result = _executor(session, runner).run(
        "missing-package-definition-run", _lease_context(now)
    )

    assert (result.status, result.error_code) == ("cancelled", "PACKAGE_REVOKED")
    assert runner.node_ids == []


def test_executor_refuses_definition_digest_drift_before_starting_any_node() -> None:
    """同版本 Definition 的源码 digest 漂移时，Run 只能 fail closed。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(
        session, "drifted-package-definition-run", include_active_package=False
    )
    session.add(
        AgentDefinition(
            agent_id="memoir_agent", version="1.0.0", runtime_type="workflow",
            definition_json={}, package_digest="sha256:drifted", contract_version="1.0.0",
            status="active", status_changed_at=now, status_changed_by="test",
            status_change_reason="fixture",
        )
    )
    session.commit()
    runner = RecordingNodeRunner()

    result = _executor(session, runner).run(
        "drifted-package-definition-run", _lease_context(now)
    )

    assert (result.status, result.error_code) == ("cancelled", "PACKAGE_REVOKED")
    assert runner.node_ids == []


def test_executor_partial_resume_only_redoes_uncompleted_optional() -> None:
    """R2 分类恢复：通用 Agent 显式 safe_to_rerun=False，partial 只重做未完成 optional。

    enqueue_media 是 optional 节点，首轮失败 → partial，checkpoint 只记录已完成的
    load_snapshot/publish_document。resume 时这两者显式声明 safe_to_rerun=False
    故跳过（不盲目重放 publish 等非幂等副作用），只重做未完成的 enqueue_media
    （第二次成功）。保护非 memoir Agent 的非幂等副作用不被 resume 重放。
    注：legacy plan 缺 safe_to_rerun 键已改为 fail-closed（见
    test_executor_resume_rejects_legacy_plan_missing_safe_to_rerun），故此处必须显式 False。
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="partial-retry-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="claimed", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
    )
    session.add_all([
        run,
        AgentPlan(
            plan_id="partial-retry-plan", run_id=run.run_id, strategy="static_workflow",
            steps_json=[
                # 显式 safe_to_rerun=False：legacy 缺键已改为 fail-closed（见
                # test_executor_resume_rejects_legacy_plan_missing_safe_to_rerun），
                # 故通用 Agent 的"默认跳过"语义必须用显式 False 表达。
                {"node_id": "load_snapshot", "node_type": "tool", "safe_to_rerun": False},
                {"node_id": "publish_document", "node_type": "tool", "safe_to_rerun": False},
                {"node_id": "enqueue_media", "node_type": "tool", "optional": True, "safe_to_rerun": False},
            ], stop_conditions_json={}, fallback_policy_json={}, status="planned",
        ),
    ])
    _add_active_test_package(session, now)
    session.commit()
    context = _lease_context(now)
    runner = OptionalFailureThenSuccessRunner()
    store = CheckpointStore(session, FernetCheckpointCipher.generate())
    executor = WorkflowExecutor(session, runner, store, ArtifactStore(session))

    first = executor.run(run.run_id, context)
    assert first.status == "partial"
    assert runner.node_ids == ["load_snapshot", "publish_document", "enqueue_media"]
    failed = session.scalar(select(AgentStep).where(AgentStep.step_name == "enqueue_media"))
    assert failed is not None and failed.status == "failed"

    # 模拟 retry 已重新认领的新 execution attempt；checkpoint 仍是权威恢复边界。
    run.status, run.dispatch_state = "pending", "claimed"
    run.execution_attempt, run.fencing_token = 2, 2
    run.lease_owner = "worker-b"
    run.lease_expires_at = now + timedelta(seconds=60)
    session.commit()
    second_context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
        authorization_version=1,
    )
    second = executor.resume(run.run_id, second_context)

    assert second.status == "succeeded"
    # R2 分类恢复：通用 Agent 显式 safe_to_rerun=False，已完成的 load_snapshot/
    # publish_document 跳过（不重放副作用），只重做首轮失败的 enqueue_media（第二次成功）。
    assert runner.node_ids == [
        "load_snapshot", "publish_document", "enqueue_media",
        "enqueue_media",
    ]


class QueryAfterCommitPublishRunner(DeterministicNodeRunner):
    """模拟真实 memoir runner 的 publish_document：query-after-commit + logical_key 幂等。

    每次访问 publish 都先查 logical_key 是否已提交（等价于业务库查询 published_revision）；
    未提交才真正发布并记账，已提交则 no-op。证明 R2：executor 在 resume 时重访 publish
    节点不会触发第二次发布副作用——幂等由 runner 负责，不由 executor 跳过保护。
    """

    PUBLISH_LOGICAL_KEY = "memoir:publish:archive-pub-1"

    def __init__(self) -> None:
        self.node_ids: list[str] = []
        self.publish_visits = 0
        self.actual_publishes = 0
        self._committed: set[str] = set()

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]:
        self.node_ids.append(str(node["node_id"]))
        if node["node_id"] == "publish_document":
            self.publish_visits += 1
            # query-after-commit：先查 logical_key 是否已落到业务侧。
            if self.PUBLISH_LOGICAL_KEY not in self._committed:
                self._committed.add(self.PUBLISH_LOGICAL_KEY)
                self.actual_publishes += 1
        return {"node_id": node["node_id"], "result": "ok"}


def test_executor_resume_publish_idempotent_via_query_after_commit() -> None:
    """R2 分类恢复：memoir publish 声明 safe_to_rerun=True，resume 重访但不双发。

    memoir 读取/发布节点声明 safe_to_rerun=True，resume 时强制重访（重算 snapshot、
    重发判定）；executor 不靠跳过 publish 防双发，而由 runner 用 logical_key 查询
    业务侧是否已提交：首轮发布 1 次，resume 后节点被重访（visit==2）但
    actual_publishes 仍为 1（query-after-commit 幂等）。
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(
        run_id="pub-idempotent-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="claimed", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
    ))
    _add_active_test_package(session, now)
    session.add(AgentPlan(
        plan_id="pub-idempotent-plan", run_id="pub-idempotent-run", strategy="static_workflow",
        steps_json=[
            {"node_id": "load_snapshot", "node_type": "tool", "safe_to_rerun": True},
            {"node_id": "publish_document", "node_type": "tool", "safe_to_rerun": True},
            {"node_id": "compute_stats", "node_type": "deterministic", "safe_to_rerun": True},
        ], stop_conditions_json={}, fallback_policy_json={}, status="planned",
    ))
    session.commit()
    context = LeaseContext(
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1,
    )
    runner = QueryAfterCommitPublishRunner()
    store = CheckpointStore(session, FernetCheckpointCipher.generate())
    executor = WorkflowExecutor(session, runner, store, ArtifactStore(session))

    first = executor.run("pub-idempotent-run", context)
    assert first.status == "succeeded"
    assert runner.publish_visits == 1
    assert runner.actual_publishes == 1

    # 模拟 retry 已重新认领的新 execution attempt；checkpoint 是恢复边界。
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "pub-idempotent-run"))
    run.status, run.dispatch_state = "pending", "claimed"
    run.execution_attempt, run.fencing_token = 2, 2
    run.lease_owner = "worker-b"
    run.lease_expires_at = now + timedelta(seconds=60)
    session.commit()
    second_context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1,
    )
    second = executor.resume("pub-idempotent-run", second_context)

    assert second.status == "succeeded"
    # R2：publish_document 被重访（visit==2），但 query-after-commit 保证 actual_publishes==1。
    assert runner.publish_visits == 2
    assert runner.actual_publishes == 1
    # 全部节点重跑，证明 snapshot/内容由重算得到，不是 checkpoint 复活。
    assert runner.node_ids == [
        "load_snapshot", "publish_document", "compute_stats",
        "load_snapshot", "publish_document", "compute_stats",
    ]


def test_executor_heartbeats_same_context_before_and_after_every_node(
    monkeypatch: MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(session, "heartbeat-boundary-run")
    context = _lease_context(now)
    runner = RecordingNodeRunner()
    heartbeat_boundaries: list[tuple[tuple[str, ...], int]] = []
    original_heartbeat = LeaseService.heartbeat

    def record_heartbeat(
        lease: LeaseService, run_id: str, current_context: LeaseContext
    ) -> bool:
        heartbeat_boundaries.append((tuple(runner.node_ids), id(current_context)))
        return original_heartbeat(lease, run_id, current_context)

    monkeypatch.setattr(LeaseService, "heartbeat", record_heartbeat)

    result = _executor(session, runner).run("heartbeat-boundary-run", context)

    assert result.status == "succeeded"
    assert [nodes for nodes, _ in heartbeat_boundaries] == [
        (),
        ("load_snapshot",),
        ("load_snapshot",),
        ("load_snapshot", "compute_stats"),
    ]
    assert {context_id for _, context_id in heartbeat_boundaries} == {id(context)}


def test_executor_does_not_start_node_when_pre_node_heartbeat_is_fenced(
    monkeypatch: MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(session, "pre-heartbeat-fenced-run")
    runner = RecordingNodeRunner()
    monkeypatch.setattr(LeaseService, "heartbeat", lambda *_args: False)

    result = _executor(session, runner).run(
        "pre-heartbeat-fenced-run", _lease_context(now)
    )

    assert (result.status, result.error_code) == ("failed", "LEASE_CONTEXT_INVALID")
    assert runner.node_ids == []
    assert session.scalars(select(AgentStep)).all() == []
    assert session.scalars(select(AgentArtifact)).all() == []
    assert session.scalars(select(AgentCheckpoint)).all() == []


def test_executor_does_not_persist_node_when_post_node_heartbeat_is_fenced(
    monkeypatch: MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(session, "post-heartbeat-fenced-run")
    runner = RecordingNodeRunner()
    heartbeat_calls = 0

    def reject_post_node_heartbeat(*_args: object) -> bool:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        return heartbeat_calls == 1

    monkeypatch.setattr(LeaseService, "heartbeat", reject_post_node_heartbeat)

    result = _executor(session, runner).run(
        "post-heartbeat-fenced-run", _lease_context(now)
    )

    assert (result.status, result.error_code) == ("failed", "LEASE_CONTEXT_INVALID")
    assert runner.node_ids == ["load_snapshot"]
    assert session.scalars(select(AgentArtifact)).all() == []
    assert session.scalars(select(AgentCheckpoint)).all() == []


def test_executor_rejects_stale_fencing_context_before_creating_step() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id="fenced-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="pending", dispatch_state="claimed", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            execution_attempt=2, lease_owner="worker-b", fencing_token=2,
            lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
        )
    )
    session.add(
        AgentPlan(
            plan_id="fenced-plan", run_id="fenced-run", strategy="static_workflow",
            steps_json=[{"node_id": "load_snapshot", "node_type": "tool"}],
            stop_conditions_json={}, fallback_policy_json={}, status="planned",
        )
    )
    _add_active_test_package(session, now)
    session.commit()

    result = _executor(session, DeterministicNodeRunner()).run(
        "fenced-run",
        LeaseContext(
            execution_attempt=1, lease_owner="worker-a", fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
            authorization_version=1,
        ),
    )

    assert result.status == "failed"
    assert result.error_code == "LEASE_CONTEXT_INVALID"
    assert session.scalars(select(AgentStep)).all() == []


def test_executor_resume_reruns_all_nodes_to_recompute_content() -> None:
    """R2 分类恢复：memoir 节点声明 safe_to_rerun=True，resume 强制重跑重算。

    snapshot/内容中间状态绝不从 checkpoint 恢复：load_snapshot 必须按当前授权/隐私
    重读，内容节点必须重算。memoir 节点声明 safe_to_rerun=True，故即便已在
    completed_node_ids 中也强制重跑（非 memoir 默认 False 会跳过已完成节点，见
    partial 场景测试）。
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id="resume-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="pending", dispatch_state="claimed", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            execution_attempt=2, lease_owner="worker-b", fencing_token=2,
            lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
        )
    )
    session.add(
        AgentPlan(
            plan_id="resume-plan", run_id="resume-run", strategy="static_workflow",
            steps_json=[
                {"node_id": "load_snapshot", "node_type": "tool", "safe_to_rerun": True},
                {"node_id": "compute_stats", "node_type": "deterministic", "safe_to_rerun": True},
            ],
            stop_conditions_json={}, fallback_policy_json={}, status="planned",
        )
    )
    _add_active_test_package(session, now)
    session.commit()
    context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
        authorization_version=1,
    )
    checkpoint_store = CheckpointStore(session, FernetCheckpointCipher.generate())
    checkpoint_store.save(
        "resume-run",
        "attempt:1:step:1",
        {"completed_node_ids": ["load_snapshot"]},
        context,
    )
    session.commit()
    runner = RecordingNodeRunner()

    result = WorkflowExecutor(
        session, runner, checkpoint_store, ArtifactStore(session)
    ).resume(
        "resume-run",
        context,
    )

    assert result.status == "succeeded"
    # R2 分类恢复：memoir safe_to_rerun=True → 即便 load_snapshot 在 completed_node_ids
    # 也强制重跑（重读 Snapshot），compute_stats 重算。
    assert runner.node_ids == ["load_snapshot", "compute_stats"]


def test_executor_resume_from_fallback_checkpoint_starts_at_fallback_node() -> None:
    """R2：fallback 路径由 error_code=WAITING_HUMAN_FALLBACK 驱动，线性跳到 fallback 节点。

    旧实现靠 checkpoint 的 completed_node_ids 跳过前置节点（误触）；R2 移除该跳过后，
    必须显式置 WAITING_HUMAN_FALLBACK，resume_from_node_id 才会被消费，执行才真正
    走 fallback 跳转分支而非全节点重跑。
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id="fallback-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="waiting_human", dispatch_state="claimed", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            error_code="WAITING_HUMAN_FALLBACK",
            execution_attempt=2, lease_owner="worker-b", fencing_token=2,
            lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
        )
    )
    session.add(
        AgentPlan(
            plan_id="fallback-plan", run_id="fallback-run", strategy="static_workflow",
            steps_json=[
                {"node_id": "prepare", "node_type": "tool"},
                {"node_id": "review", "node_type": "guardrail"},
                {"node_id": "fallback", "node_type": "deterministic"},
            ],
            stop_conditions_json={}, fallback_policy_json={"waiting_human_fallback_node": "fallback"},
            status="planned",
        )
    )
    _add_active_test_package(session, now)
    session.commit()
    context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
        authorization_version=1,
    )
    checkpoint_store = CheckpointStore(session, FernetCheckpointCipher.generate())
    checkpoint_store.save(
        "fallback-run", "attempt:1:step:2",
        {"completed_node_ids": ["prepare", "review"], "resume_from_node_id": "fallback"},
        context,
    )
    session.commit()
    runner = RecordingNodeRunner()

    result = WorkflowExecutor(
        session, runner, checkpoint_store, ArtifactStore(session)
    ).resume("fallback-run", context)

    assert runner.node_ids == ["fallback"]
    assert result.status == "succeeded"


def test_agent_state_checkpoint_summary_never_contains_private_run_input() -> None:
    state = AgentState(
        run_input={"snapshot_id": "snapshot-1", "diary_content": "私密正文"},
        completed_node_ids=["load_snapshot"],
        fallback_flags=["template_highlights"],
    )

    summary = state.checkpoint_summary()

    assert summary == {
        "completed_node_ids": ["load_snapshot"],
        "fallback_flags": ["template_highlights"],
    }
    assert "diary_content" not in summary


def test_executor_encrypts_node_state_without_leaking_snapshot_to_artifact() -> None:
    """R2：checkpoint 即便加密也只存恢复路由元数据；snapshot/正文永不进密文 blob。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(run_id="snapshot-run", agent_id="memoir_agent", agent_version="1.0.0", package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory", business_id="archive", status="pending", dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace", execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1)))
    _add_active_test_package(session, now)
    session.add(AgentPlan(plan_id="snapshot-plan", run_id="snapshot-run", strategy="static_workflow", steps_json=[{"node_id": "load_snapshot", "node_type": "tool"}], stop_conditions_json={}, fallback_policy_json={}, status="planned"))
    session.commit()
    context = LeaseContext(execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1)
    cipher = FernetCheckpointCipher.generate()
    store = CheckpointStore(session, cipher)
    assert WorkflowExecutor(session, SnapshotNodeRunner(), store, ArtifactStore(session)).run("snapshot-run", context).status == "succeeded"
    # 解密后的 checkpoint 不得包含 snapshot 字段或正文哨兵；旧的全量持久化已撤销。
    decrypted = store.load_latest("snapshot-run", context)
    assert "snapshot" not in decrypted
    assert "sanitized_material" not in decrypted
    assert "scenes" not in decrypted
    assert "playback_document" not in decrypted
    assert "私密正文" not in str(decrypted)
    assert decrypted["completed_node_ids"] == ["load_snapshot"]
    assert "私密正文" not in str(session.scalar(select(AgentArtifact)).summary_json)


def test_executor_pauses_for_human_after_checkpoint_and_emits_callback() -> None:
    """人工等待必须先持久化节点 checkpoint，再释放 Worker lease 与 Admission。"""
    class HumanReviewRunner(DeterministicNodeRunner):
        def run_node(
            self, node: dict[str, object], run: AgentRun, state: AgentState
        ) -> dict[str, object]:
            return {"node_id": node["node_id"], "waiting_human": True}

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(run_id="human-run", agent_id="memoir_agent", agent_version="1.0.0", package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory", business_id="archive", status="pending", dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace", execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1)))
    _add_active_test_package(session, now)
    session.add(AgentPlan(plan_id="human-plan", run_id="human-run", strategy="static_workflow", steps_json=[{"node_id": "review", "node_type": "guardrail"}, {"node_id": "fallback", "node_type": "deterministic"}], stop_conditions_json={"approval_ttl_seconds": 60}, fallback_policy_json={"waiting_human_fallback_node": "fallback"}, status="planned"))
    session.commit()
    context = LeaseContext(execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1)
    cipher = FernetCheckpointCipher.generate()
    store = CheckpointStore(session, cipher)

    result = WorkflowExecutor(session, HumanReviewRunner(), store, ArtifactStore(session)).run("human-run", context)

    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "human-run"))
    callback = session.scalar(select(CallbackEvent).where(CallbackEvent.run_id == "human-run", CallbackEvent.event_type == "waiting_human"))
    assert result.status == "waiting_human"
    assert run is not None and (run.status, run.dispatch_state, run.lease_owner) == ("waiting_human", "finished", None)
    assert run.waiting_expires_at is not None
    assert callback is not None
    checkpoint = session.scalar(select(AgentCheckpoint).where(AgentCheckpoint.run_id == "human-run"))
    assert checkpoint is not None and checkpoint.state_summary["completed_node_ids"] == ["review"]
    assert checkpoint.encrypted_state_blob is not None
    assert cipher.decrypt(checkpoint.encrypted_state_blob)["resume_from_node_id"] == "fallback"


def test_executor_rejects_checkpoint_fallback_missing_from_frozen_plan() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(run_id="invalid-fallback-run", agent_id="memoir_agent", agent_version="1.0.0", package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory", business_id="archive", status="waiting_human", dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace", error_code="WAITING_HUMAN_FALLBACK", execution_attempt=2, lease_owner="worker", fencing_token=2, lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1)))
    session.add(AgentPlan(plan_id="invalid-fallback-plan", run_id="invalid-fallback-run", strategy="static_workflow", steps_json=[{"node_id": "review", "node_type": "guardrail"}], stop_conditions_json={}, fallback_policy_json={"waiting_human_fallback_node": "fallback"}, status="planned"))
    _add_active_test_package(session, now)
    session.commit()
    context = LeaseContext(execution_attempt=2, lease_owner="worker", fencing_token=2, lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1)
    store = CheckpointStore(session, FernetCheckpointCipher.generate())
    store.save("invalid-fallback-run", "attempt:1:step:1", {"completed_node_ids": ["review"], "resume_from_node_id": "fallback"}, context)
    session.commit()

    result = WorkflowExecutor(session, RecordingNodeRunner(), store, ArtifactStore(session)).resume("invalid-fallback-run", context)

    assert (result.status, result.error_code) == ("failed", "FALLBACK_NODE_INVALID")


def test_executor_stops_before_next_node_when_authorization_changes() -> None:
    """节点一完成即撤销授权时，Executor 不得启动后续节点。"""
    class RevokingRunner(RecordingNodeRunner):
        def run_node(
            self, node: dict[str, object], run: AgentRun, state: AgentState
        ) -> dict[str, object]:
            result = super().run_node(node, run, state)
            if node["node_id"] == "load_snapshot":
                run.authorization_version += 1
            return result

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(run_id="authorization-run", agent_id="memoir_agent", agent_version="1.0.0", package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory", business_id="archive", status="pending", dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace", execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1)))
    _add_active_test_package(session, now)
    session.add(AgentPlan(plan_id="authorization-plan", run_id="authorization-run", strategy="static_workflow", steps_json=[{"node_id": "load_snapshot", "node_type": "tool"}, {"node_id": "compute_stats", "node_type": "deterministic"}], stop_conditions_json={}, fallback_policy_json={}, status="planned"))
    session.commit()
    runner = RevokingRunner()
    result = _executor(session, runner).run("authorization-run", LeaseContext(execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1))

    assert result.error_code == "LEASE_CONTEXT_INVALID"
    assert runner.node_ids == ["load_snapshot"]
    assert session.scalars(select(AgentArtifact)).all() == []
    assert session.scalars(select(AgentCheckpoint)).all() == []


def test_executor_does_not_persist_tool_result_when_privacy_version_changes() -> None:
    """工具返回后隐私版本变化时，heartbeat 不得绕过统一写前复核。"""

    class PrivacyRevokingRunner(RecordingNodeRunner):
        def run_node(
            self, node: dict[str, object], run: AgentRun, state: AgentState
        ) -> dict[str, object]:
            result = super().run_node(node, run, state)
            if node["node_id"] == "load_snapshot":
                run.privacy_version += 1
            return result

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(session, "privacy-version-run")
    runner = PrivacyRevokingRunner()

    result = _executor(session, runner).run(
        "privacy-version-run", _lease_context(now)
    )

    assert (result.status, result.error_code) == ("failed", "LEASE_CONTEXT_INVALID")
    assert runner.node_ids == ["load_snapshot"]
    assert session.scalars(select(AgentArtifact)).all() == []
    assert session.scalars(select(AgentCheckpoint)).all() == []


def test_executor_draining_before_first_node_returns_safe_nonterminal_result() -> None:
    """draining 已开始时不得启动首个节点或工具调用。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id="draining-before-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="pending", dispatch_state="claimed", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            execution_attempt=1, lease_owner="worker", fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
        )
    )
    session.add(
        AgentPlan(
            plan_id="draining-before-plan", run_id="draining-before-run", strategy="static_workflow",
            steps_json=[{"node_id": "load_snapshot", "node_type": "tool"}],
            stop_conditions_json={}, fallback_policy_json={}, status="planned",
        )
    )
    _add_active_test_package(session, now)
    session.commit()
    runner = RecordingNodeRunner()

    result = WorkflowExecutor(
        session,
        runner,
        CheckpointStore(session, FernetCheckpointCipher.generate()),
        ArtifactStore(session),
        is_draining=lambda: True,
    ).run(
        "draining-before-run",
        LeaseContext(
            execution_attempt=1, lease_owner="worker", fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
            authorization_version=1,
        ),
    )

    assert (result.status, result.error_code) == ("pending", "WORKFLOW_DRAINING")
    assert runner.node_ids == []
    assert session.scalars(select(AgentStep)).all() == []
    assert session.scalars(select(AgentArtifact)).all() == []
    assert session.scalars(select(AgentCheckpoint)).all() == []


def test_executor_draining_after_checkpoint_does_not_start_next_node() -> None:
    """已完成节点安全 checkpoint 后，draining 不得继续启动下一工具节点。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id="draining-after-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="pending", dispatch_state="claimed", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            execution_attempt=1, lease_owner="worker", fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
        )
    )
    session.add(
        AgentPlan(
            plan_id="draining-after-plan", run_id="draining-after-run", strategy="static_workflow",
            steps_json=[
                {"node_id": "load_snapshot", "node_type": "tool"},
                {"node_id": "compute_stats", "node_type": "deterministic"},
            ],
            stop_conditions_json={}, fallback_policy_json={}, status="planned",
        )
    )
    _add_active_test_package(session, now)
    session.commit()
    draining = {"value": False}

    class DrainingRunner(RecordingNodeRunner):
        def run_node(
            self, node: dict[str, object], run: AgentRun, state: AgentState
        ) -> dict[str, object]:
            result = super().run_node(node, run, state)
            draining["value"] = True
            return result

    runner = DrainingRunner()
    result = WorkflowExecutor(
        session,
        runner,
        CheckpointStore(session, FernetCheckpointCipher.generate()),
        ArtifactStore(session),
        is_draining=lambda: draining["value"],
    ).run(
        "draining-after-run",
        LeaseContext(
            execution_attempt=1, lease_owner="worker", fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
            authorization_version=1,
        ),
    )

    assert (result.status, result.error_code) == ("pending", "WORKFLOW_DRAINING")
    assert runner.node_ids == ["load_snapshot"]
    checkpoint = session.scalar(
        select(AgentCheckpoint).where(AgentCheckpoint.run_id == "draining-after-run")
    )
    assert checkpoint is not None
    assert checkpoint.state_summary["completed_node_ids"] == ["load_snapshot"]
    assert len(session.scalars(select(AgentArtifact)).all()) == 1


def test_executor_checkpoint_decrypted_blob_excludes_all_five_content_sentinels_and_playback() -> None:
    """R2 L550 新增断言：解密 checkpoint 也不含五类正文 + Scene + PlaybackDocument。

    即便 Fernet 密钥被泄漏，攻击者从密文里也只能拿到 completed_node_ids /
    fallback_flags / completed_steps 三类恢复路由元数据。旧的 ``**state.model_dump()``
    全量持久化路径已被白名单取代。
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(run_id="sentinel-run", agent_id="memoir_agent", agent_version="1.0.0", package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory", business_id="archive", status="pending", dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace", execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1)))
    _add_active_test_package(session, now)
    session.add(AgentPlan(plan_id="sentinel-plan", run_id="sentinel-run", strategy="static_workflow", steps_json=[{"node_id": "load_snapshot", "node_type": "tool"}, {"node_id": "compute_stats", "node_type": "deterministic"}], stop_conditions_json={}, fallback_policy_json={}, status="planned"))
    session.commit()
    context = LeaseContext(execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1)
    cipher = FernetCheckpointCipher.generate()
    store = CheckpointStore(session, cipher)
    runner = SensitiveStateNodeRunner()

    result = WorkflowExecutor(session, runner, store, ArtifactStore(session)).run("sentinel-run", context)

    assert result.status == "succeeded"
    checkpoints = session.scalars(select(AgentCheckpoint).where(AgentCheckpoint.run_id == "sentinel-run")).all()
    assert len(checkpoints) == 2
    for checkpoint in checkpoints:
        # 解密 checkpoint 不含五类正文 + Scene + PlaybackDocument + tool payload。
        decrypted = cipher.decrypt(checkpoint.encrypted_state_blob)
        decrypted_text = str(decrypted)
        for sentinel in SensitiveStateNodeRunner.SENTINELS:
            assert sentinel not in decrypted_text, f"checkpoint 泄漏哨兵 {sentinel}: {decrypted}"
        # 白名单字段必须仍在：恢复路由不能因为脱敏被破坏。
        assert set(decrypted.keys()) <= {"completed_steps", "completed_node_ids", "fallback_flags", "resume_from_node_id"}
        assert "snapshot" not in decrypted
        assert "sanitized_material" not in decrypted
        assert "scenes" not in decrypted
        assert "playback_document" not in decrypted
        assert "publish_result" not in decrypted
        assert "chapter_plan" not in decrypted
        assert "highlights" not in decrypted
        assert "stats" not in decrypted
        assert "safety_report" not in decrypted
        assert "media_tasks" not in decrypted
        assert "trust_metadata" not in decrypted
        assert "errors" not in decrypted
        assert "run_input" not in decrypted
    # 同样校验公共 summary 列；审计摘要早已剔除正文，本次回归继续守护。
    for checkpoint in checkpoints:
        summary_text = str(checkpoint.state_summary)
        for sentinel in SensitiveStateNodeRunner.SENTINELS:
            assert sentinel not in summary_text


def test_executor_resume_rejects_legacy_full_state_checkpoint_and_purges() -> None:
    """R2 checkpoint/resume：旧版完整 checkpoint（含 snapshot/scenes/playback_document 等正文键）拒绝并 purge。

    规格要求：旧完整状态 checkpoint 必须物理删除，不可作为新版恢复输入，也不可遗留在
    密文中等待后续误读。resume 见到任何非白名单键即返回 CHECKPOINT_STATE_INVALID，
    并经 CheckpointStore.purge_for_run（fencing 保护）删除该 run 全部 checkpoint 行，
    不进入执行循环，不读任何字段。
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(run_id="legacy-resume-run", agent_id="memoir_agent", agent_version="1.0.0", package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory", business_id="archive", status="pending", dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace", execution_attempt=2, lease_owner="worker-b", fencing_token=2, lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1)))
    session.add(AgentPlan(plan_id="legacy-resume-plan", run_id="legacy-resume-run", strategy="static_workflow", steps_json=[{"node_id": "load_snapshot", "node_type": "tool"}, {"node_id": "compute_stats", "node_type": "deterministic"}], stop_conditions_json={}, fallback_policy_json={}, status="planned"))
    _add_active_test_package(session, now)
    session.commit()
    context = LeaseContext(execution_attempt=2, lease_owner="worker-b", fencing_token=2, lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1)
    cipher = FernetCheckpointCipher.generate()
    store = CheckpointStore(session, cipher)
    # 手工塞入旧版完整 checkpoint：snapshot/sanitized_material/scenes/playback_document
    # 同时存在。resume 必须拒绝恢复并 purge，把私密字段挡在内存 AgentState 之外，
    # 并物理删除该 run 的 checkpoint 行（R2 checkpoint/resume purge 闭环）。
    store.save(
        "legacy-resume-run", "attempt:1:step:1",
        {
            "completed_node_ids": ["load_snapshot"],
            "fallback_flags": [],
            "snapshot": {"diaries": [{"content": "legacy-private-marker"}]},
            "sanitized_material": {"materials": [{"summary": "legacy-private-marker"}]},
            "scenes": [{"body": "legacy-private-marker"}],
            "playback_document": {"private": "legacy-private-marker"},
            "run_input": {"legacy": "legacy-private-marker"},
        },
        context,
    )
    session.commit()
    runner = RecordingNodeRunner()

    result = WorkflowExecutor(session, runner, store, ArtifactStore(session)).resume("legacy-resume-run", context)

    # R2 checkpoint/resume：旧版完整 checkpoint（含正文键）一律拒绝并 purge，不可作为恢复输入，也不可遗留。
    assert result.status == "failed"
    assert result.error_code == "CHECKPOINT_STATE_INVALID"
    assert runner.node_ids == []
    # purge 闭环：该 run 的 checkpoint 行必须被物理删除，不能留在密文中等待后续误读。
    leftover = session.scalars(
        select(AgentCheckpoint).where(AgentCheckpoint.run_id == "legacy-resume-run")
    ).all()
    assert leftover == []


def test_executor_resume_rejects_legacy_plan_missing_safe_to_rerun() -> None:
    """P2：legacy memoir plan（safe_to_rerun 引入前冻结）resume 时不得静默跳过已完成节点。

    memoir checkpoint 不存正文，resume 必须靠 safe_to_rerun=True 重算读取/内容节点。
    在 safe_to_rerun 引入前冻结的 legacy plan，节点缺该键；旧实现 ``node.get("safe_to_rerun")``
    把缺键当作默认 False 静默跳过 → resume 用空 state 产出残缺文档。新实现把"缺键"与
    "显式 False"分离：缺键无法安全判定跳过 vs 重算，一律 fail closed
    （PLAN_LEGACY_DEFINITION）交业务侧 undo/purge 重建；显式 False（非 memoir /
    partial 已完成 optional）仍正常跳过。Executor 不硬编码 memoir 节点名——任何缺键
    的已完成节点都触发 fail closed，与 business_type 解耦（不引入 Playback 业务事实）。
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(
        run_id="legacy-plan-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="claimed", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant",
        create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace", execution_attempt=2,
        lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
    ))
    _add_active_test_package(session, now)
    # legacy plan：节点在 safe_to_rerun 引入前冻结，缺该键（区别于显式 False）。
    session.add(AgentPlan(
        plan_id="legacy-plan-def", run_id="legacy-plan-run", strategy="static_workflow",
        steps_json=[
            {"node_id": "load_snapshot", "node_type": "tool"},
            {"node_id": "compute_stats", "node_type": "deterministic"},
        ],
        stop_conditions_json={}, fallback_policy_json={}, status="planned",
    ))
    session.commit()
    context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1,
    )
    cipher = FernetCheckpointCipher.generate()
    store = CheckpointStore(session, cipher)
    # 合法新版 checkpoint：仅白名单键，load_snapshot 已完成。plan 是 legacy 缺键 →
    # resume 不得静默跳过 load_snapshot，必须在跳过判定处 fail closed。
    store.save(
        "legacy-plan-run", "attempt:1:step:1",
        {"completed_node_ids": ["load_snapshot"], "fallback_flags": []},
        context,
    )
    session.commit()
    runner = RecordingNodeRunner()

    result = WorkflowExecutor(session, runner, store, ArtifactStore(session)).resume(
        "legacy-plan-run", context
    )

    assert result.status == "failed"
    assert result.error_code == "PLAN_LEGACY_DEFINITION"
    # fail closed 在首个已完成缺键节点（load_snapshot）的跳过判定处立即触发，不执行任何节点。
    assert runner.node_ids == []


def test_resume_legacy_checkpoint_purge_persists_across_session_via_finish_chain() -> None:
    """P3：legacy checkpoint purge 经真实 resume→finish 链路 commit，跨 Session 可见。

    规格要求：purge_for_run 只 flush 不 commit（注释「调用方自行决定事务提交时机」）；
    生产 consume()→resume()→finish() 链路里，finish() 的 session.commit() 才让 purge
    删除与 checkpoint_purged 审计真正落库。本测试用独立 Session B 验证 purge 不是同
    Session 的 flush 幻象：AgentCheckpoint 行物理消失、checkpoint_purged 审计持久化且
    metadata 只含 run_id/privacy_version/content_digest_prefix，绝不含正文/密文/键名。
    legacy purge 属 R2 checkpoint/resume 收口（与 R3 反复活旧 checkpoint 的语义无关）。
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_a = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session_a.add(AgentRun(
        run_id="purge-commit-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="claimed", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant",
        create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace", execution_attempt=2,
        lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
    ))
    session_a.add(AgentPlan(
        plan_id="purge-commit-plan", run_id="purge-commit-run", strategy="static_workflow",
        steps_json=[
            {"node_id": "load_snapshot", "node_type": "tool", "safe_to_rerun": True},
            {"node_id": "compute_stats", "node_type": "deterministic", "safe_to_rerun": True},
        ],
        stop_conditions_json={}, fallback_policy_json={}, status="planned",
    ))
    _add_active_test_package(session_a, now)
    session_a.commit()
    cipher = FernetCheckpointCipher.generate()
    store = CheckpointStore(session_a, cipher)
    context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1,
    )
    # 旧完整状态 checkpoint：含非白名单哨兵键。resume 必须拒绝恢复并 purge。
    store.save(
        "purge-commit-run", "attempt:1:step:1",
        {
            "completed_node_ids": ["load_snapshot"],
            "fallback_flags": [],
            "sentinel_legacy_key": {"private": "purge-commit-marker"},
        },
        context,
    )
    session_a.commit()

    runner = RecordingNodeRunner()
    # resume 在 session_a 上 flush purge（删行 + 写审计）但不 commit；模拟生产
    # consume() 调 finish() 才把 purge 与审计提交落库。
    result = WorkflowExecutor(session_a, runner, store, ArtifactStore(session_a)).resume(
        "purge-commit-run", context
    )
    assert result.status == "failed"
    assert result.error_code == "CHECKPOINT_STATE_INVALID"
    assert runner.node_ids == []
    LeaseService(session_a).finish(result, context)

    # 新 Session 验证 purge 真正跨事务落库（不是同 Session 的 flush 幻象）。
    session_b = sessionmaker(bind=engine)()
    leftover = session_b.scalars(
        select(AgentCheckpoint).where(AgentCheckpoint.run_id == "purge-commit-run")
    ).all()
    assert leftover == []
    purged = session_b.scalars(
        select(RuntimeAuditRecord).where(
            RuntimeAuditRecord.action == "checkpoint_purged",
            RuntimeAuditRecord.resource_type == "agent_checkpoint",
        )
    ).all()
    assert len(purged) >= 1
    for audit in purged:
        meta = audit.metadata_summary
        # 审计只含安全定位字段，绝不携带正文/密文/键名。
        assert set(meta) <= {"run_id", "privacy_version", "content_digest_prefix"}
        assert meta.get("run_id") == "purge-commit-run"
        assert "purge-commit-marker" not in str(meta)
        assert "sentinel_legacy_key" not in str(meta)
    # run 终态经 finish() 提交落库。
    run_b = session_b.scalar(select(AgentRun).where(AgentRun.run_id == "purge-commit-run"))
    assert run_b.status == "failed"
    assert run_b.error_code == "CHECKPOINT_STATE_INVALID"


def test_resume_legacy_checkpoint_log_privacy_success_and_failure_fails_closed(caplog) -> None:
    """P3：legacy checkpoint resume 日志只含 run_id/extra_key_count/purged_count/稳定 code。

    规格要求：不得打印 sorted(extra_keys)（键名暗示正文结构）或 purge_exc 原文（异常文本
    可夹带库错误/密文片段）。本测试注入哨兵键名与哨兵异常文本，断言二者均不出现在日志，
    只出现计数与稳定 code。覆盖两条 purge 路径：(1) 成功 purge 输出 purged_count；
    (2) purge 失败仍 fail closed——返回 CHECKPOINT_STATE_INVALID、不执行任何节点。
    """
    import logging as _logging

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    cipher = FernetCheckpointCipher.generate()
    store = CheckpointStore(session, cipher)
    context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1,
    )
    _add_active_test_package(session, now)

    def _seed(run_id: str) -> None:
        session.add(AgentRun(
            run_id=run_id, agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="pending", dispatch_state="claimed", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant",
            create_idempotency_key="key", callback_target_id="callback",
            business_connector_id="connector", trace_id="trace", execution_attempt=2,
            lease_owner="worker-b", fencing_token=2,
            lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
        ))
        session.add(AgentPlan(
            plan_id=f"{run_id}-plan", run_id=run_id, strategy="static_workflow",
            steps_json=[
                {"node_id": "load_snapshot", "node_type": "tool", "safe_to_rerun": True},
            ],
            stop_conditions_json={}, fallback_policy_json={}, status="planned",
        ))
        # 哨兵键名：若日志泄漏 sorted(extra_keys)，这个词会被命中。
        store.save(
            run_id, "attempt:1:step:1",
            {
                "completed_node_ids": ["load_snapshot"],
                "fallback_flags": [],
                "sentinel_legacy_extra_key": {"private": "log-privacy-marker"},
            },
            context,
        )
        session.commit()

    # (1) 成功 purge 路径：真实 store。
    _seed("log-success-run")
    runner_ok = RecordingNodeRunner()
    with caplog.at_level(_logging.INFO):
        result_ok = WorkflowExecutor(
            session, runner_ok, store, ArtifactStore(session)
        ).resume("log-success-run", context)
    assert result_ok.status == "failed"
    assert result_ok.error_code == "CHECKPOINT_STATE_INVALID"
    success_text = caplog.text
    assert "extra_key_count=" in success_text
    assert "purged_count=" in success_text
    assert "sentinel_legacy_extra_key" not in success_text
    assert "log-privacy-marker" not in success_text

    # (2) purge 失败路径：包装 store 让 purge 抛带哨兵文本的 CheckpointError。
    class _PurgeFailingStore(CheckpointStore):
        def purge_for_run(self, run_id: str, context: LeaseContext) -> int:  # noqa: A002
            raise CheckpointError("sentinel_purge_failure_text")

    caplog.clear()
    _seed("log-fail-run")
    failing_store = _PurgeFailingStore(session, cipher)
    runner_fail = RecordingNodeRunner()
    with caplog.at_level(_logging.WARNING):
        result_fail = WorkflowExecutor(
            session, runner_fail, failing_store, ArtifactStore(session)
        ).resume("log-fail-run", context)
    # purge 失败也 fail closed：不执行任何节点，返回 CHECKPOINT_STATE_INVALID。
    assert result_fail.status == "failed"
    assert result_fail.error_code == "CHECKPOINT_STATE_INVALID"
    assert runner_fail.node_ids == []
    fail_text = caplog.text
    assert "CHECKPOINT_PURGE_FAILED" in fail_text
    assert "extra_key_count=" in fail_text
    assert "sentinel_purge_failure_text" not in fail_text
    assert "sentinel_legacy_extra_key" not in fail_text


def test_resume_anti_revival_when_authorization_version_changed() -> None:
    """P3：授权版本变化后 resume 不得经 checkpoint 复活旧 Run。

    规格要求：授权撤销/变更后，旧 lease 不得继续执行。resume 经 _execute 里
    _authorization_changed 检测 resolver 返回的当前版本 ≠ run.authorization_version，
    立即返回 AUTHORIZATION_CHANGED（cancelled），不进入节点循环、不读 checkpoint 正文。
    checkpoint 合法（仅白名单键）以保证 resume 能进入 _execute 触发授权检查。
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(
        run_id="auth-revival-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="claimed", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant",
        create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace", execution_attempt=2,
        lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
    ))
    session.add(AgentPlan(
        plan_id="auth-revival-plan", run_id="auth-revival-run", strategy="static_workflow",
        steps_json=[
            {"node_id": "load_snapshot", "node_type": "tool", "safe_to_rerun": True},
            {"node_id": "compute_stats", "node_type": "deterministic", "safe_to_rerun": True},
        ],
        stop_conditions_json={}, fallback_policy_json={}, status="planned",
    ))
    _add_active_test_package(session, now)
    session.commit()
    cipher = FernetCheckpointCipher.generate()
    store = CheckpointStore(session, cipher)
    context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1,
    )
    store.save(
        "auth-revival-run", "attempt:1:step:1",
        {"completed_node_ids": ["load_snapshot"], "fallback_flags": []},
        context,
    )
    session.commit()

    runner = RecordingNodeRunner()
    # resolver 返回 2 ≠ run.authorization_version=1 → 授权已变更，拒绝复活。
    executor = WorkflowExecutor(
        session, runner, store, ArtifactStore(session),
        authorization_version_resolver=lambda run: 2,
    )
    result = executor.resume("auth-revival-run", context)

    assert result.status == "cancelled"
    assert result.error_code == "AUTHORIZATION_CHANGED"
    assert runner.node_ids == []


def test_resume_anti_revival_when_privacy_version_changed() -> None:
    """P3：隐私版本变化后 checkpoint 不得作为恢复输入复活旧 Run。

    规格要求：隐私版本变更（用户撤销素材授权）后，旧 checkpoint 不可恢复。load_latest
    检测 checkpoint.privacy_version ≠ context.privacy_version 抛 CheckpointError，resume
    返回 CHECKPOINT_NOT_RESUMABLE，不读 checkpoint 正文、不执行任何节点。这是隐私防复活
    的第一道闸门（在白名单键检查之前），确保授权撤销的素材不被旧 checkpoint 复读。
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(
        run_id="privacy-revival-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="claimed", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant",
        create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace", execution_attempt=2,
        lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
    ))
    _add_active_test_package(session, now)
    session.commit()
    cipher = FernetCheckpointCipher.generate()
    store = CheckpointStore(session, cipher)
    save_context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1,
    )
    store.save(
        "privacy-revival-run", "attempt:1:step:1",
        {"completed_node_ids": ["load_snapshot"], "fallback_flags": []},
        save_context,
    )
    session.commit()

    runner = RecordingNodeRunner()
    # resume 用 privacy_version=2（≠ checkpoint 的 1）→ load_latest 拒绝 → CHECKPOINT_NOT_RESUMABLE。
    resume_context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=2, authorization_version=1,
    )
    result = WorkflowExecutor(session, runner, store, ArtifactStore(session)).resume(
        "privacy-revival-run", resume_context
    )

    assert result.status == "failed"
    assert result.error_code == "CHECKPOINT_NOT_RESUMABLE"
    assert runner.node_ids == []


def test_executor_resume_real_memoir_runner_publishes_once_via_query_after_commit() -> None:
    """R2 端到端回归：真实 MemoirNodeRunner + 真实 ToolCallAuditService + countable gateway。

    完整 memoir workflow 首轮 run 发布 1 次；resume 时所有节点 safe_to_rerun=True 全部
    重跑（重读 snapshot、重算内容、重访 publish_document），但真实 runner 经
    audit.latest_committed + gateway.get_publish_result 对账，不重发
    publish_playback_document。证明 executor resume 分类恢复 + 真实 query-after-commit
    在端到端流程下发布不双发（非 QueryAfterCommitPublishRunner 的简化模拟）。
    """
    from pathlib import Path

    from app.agents.memoir_agent.runner import MemoirNodeRunner
    from app.services.agent_package_service import AgentPackageService
    from app.services.tool_call_audit_service import ToolCallAuditService

    # 直接加载正式 workflow.graph 声明，保证测试用的是真实节点配置（含 safe_to_rerun）。
    memoir_steps = [
        node.model_dump()
        for node in AgentPackageService._load_workflow_nodes(
            Path(__file__).resolve().parents[1]
            / "app/agents/memoir_agent/1.0.0/workflow.graph.py"
        )
    ]
    snapshot_payload = {"diary_items": [{"id": "d1", "content": "今天阳光很好"}]}

    class _CountableMemoirGateway:
        def __init__(self) -> None:
            self.publish_calls: list[tuple[object, ...]] = []
            self.reconciliation_calls: list[tuple[object, ...]] = []
            self.snapshot_calls = 0

        def get_snapshot(self, *args: object) -> dict[str, object]:
            self.snapshot_calls += 1
            return snapshot_payload

        def publish_playback_document(self, *args: object) -> dict[str, object]:
            self.publish_calls.append(args)
            return {"revision": 1, "content_digest": "published-digest"}

        def get_publish_result(self, *args: object) -> dict[str, object]:
            self.reconciliation_calls.append(args)
            return {"revision": 1, "content_digest": "published-digest"}

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    gateway = _CountableMemoirGateway()
    audit = ToolCallAuditService(session)
    runner = MemoirNodeRunner(gateway, audit, model_gateway=None)
    run = AgentRun(
        run_id="memoir-resume-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="claimed",
        input_json={"archive_id": "archive", "snapshot_id": "snapshot", "generation_epoch": 0},
        authorization_version=1, caller_id="caller", tenant_id="tenant",
        create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace",
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
    )
    session.add(run)
    _add_active_test_package(session, now)
    session.add(AgentPlan(
        plan_id="memoir-resume-plan", run_id=run.run_id, strategy="static_workflow",
        steps_json=memoir_steps, stop_conditions_json={}, fallback_policy_json={},
        status="planned",
    ))
    session.commit()

    executor = _executor(session, runner)
    first = executor.run(run.run_id, _lease_context(now))
    assert first.status == "succeeded"
    # 首轮：publish_playback_document 实际写 1 次；尚未触发对账。
    assert len(gateway.publish_calls) == 1
    assert gateway.reconciliation_calls == []

    # 模拟 retry 已重新认领的新 execution attempt；checkpoint completed_node_ids 已含全部节点。
    run.status, run.dispatch_state = "pending", "claimed"
    run.execution_attempt, run.fencing_token = 2, 2
    run.lease_owner = "worker-b"
    run.lease_expires_at = now + timedelta(seconds=60)
    session.commit()
    second_context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1,
    )
    second = executor.resume(run.run_id, second_context)

    assert second.status == "succeeded"
    # R2 端到端：resume 重访 publish_document（snapshot 重读、内容重算），但真实 runner
    # 经 latest_committed + get_publish_result 对账，不重发 publish_playback_document。
    assert len(gateway.publish_calls) == 1
    assert len(gateway.reconciliation_calls) == 1
    # load_snapshot 首轮 + resume 各读一次（safe_to_rerun=True 强制重读）。
    assert gateway.snapshot_calls == 2
