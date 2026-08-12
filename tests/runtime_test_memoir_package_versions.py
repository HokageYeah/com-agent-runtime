"""MemoirAgent 双版本冻结兼容回归。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import AgentDefinition, AgentPlan, AgentRun, AgentStep
from app.runtime.artifact import ArtifactStore
from app.runtime.checkpoint import CheckpointStore, FernetCheckpointCipher
from app.runtime.executor import WorkflowExecutor
from app.runtime.interfaces import LeaseContext
from app.runtime.planner import StaticPlanner
from app.runtime.state import AgentState
from app.schemas.agent_package import AgentPackage
from app.schemas.agent_run import CreateRunCommand
from app.services.agent_package_service import AgentPackageService
from app.services.agent_run_service import AgentRunService

_MEMOIR_1_0_0_DIGEST = (
    "sha256:a6e2f53e223658fb648026335373d23f548232e5dd2c4c67a2c774df6e67833e"
)
_MEMOIR_1_0_1_DIGEST = (
    "sha256:e92ae977220e02f3956d821b0c5ff6adc2320359970d9989863324ab11349c06"
)


class _RecordingPackageRunner:
    """只替代外部 Tool/Model I/O，保留真实 Package、Plan 与 Executor 路径。"""

    def __init__(self) -> None:
        self.node_ids: list[str] = []

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]:
        del run, state
        node_id = str(node["node_id"])
        self.node_ids.append(node_id)
        return {"node_id": node_id}


def _loaded_packages() -> tuple[AgentPackage, AgentPackage]:
    package_root = Path(__file__).parents[1] / "app" / "agents"
    loader = AgentPackageService(package_root)
    packages = (
        loader.load("memoir_agent", "1.0.0"),
        loader.load("memoir_agent", "1.0.1"),
    )
    # 1.0.0 已是历史冻结内容；任何同 version 文件改写都必须先发新版本，不能
    # 让该 digest 漂移。1.0.1 也以同一不可变规则固定其发布内容。
    assert packages[0].package_digest == _MEMOIR_1_0_0_DIGEST
    assert packages[1].package_digest == _MEMOIR_1_0_1_DIGEST
    return packages


def _register_package(session: Session, package: AgentPackage) -> None:
    """以 Loader 的真实输出注册定义，禁止测试伪造 workflow 或 digest。"""
    session.add(
        AgentDefinition(
            agent_id=package.agent_id,
            version=package.version,
            runtime_type="workflow",
            definition_json=package.model_dump(mode="json"),
            package_digest=package.package_digest,
            contract_version=package.contract_version,
            status="active",
            status_changed_at=datetime.now(UTC),
            status_changed_by="test",
            status_change_reason="loaded-package-fixture",
        )
    )


def _lease_for(run: AgentRun) -> LeaseContext:
    now = datetime.now(UTC)
    run.status = "running"
    run.dispatch_state = "claimed"
    run.execution_attempt = 1
    run.lease_owner = "version-test-worker"
    run.fencing_token = 1
    run.lease_expires_at = now + timedelta(minutes=5)
    run.run_deadline_at = now + timedelta(days=1)
    return LeaseContext(
        execution_attempt=1,
        lease_owner="version-test-worker",
        fencing_token=1,
        lease_expires_at=run.lease_expires_at,
        privacy_version=run.privacy_version,
        authorization_version=run.authorization_version,
    )


def test_new_create_uses_loaded_1_0_1_and_executes_full_real_workflow(
    monkeypatch,
) -> None:
    """新建 Run 固定 1.0.1，并经 Worker 的真实 adapter 选择执行完整 workflow。"""
    import app.worker as worker

    package_100, package_101 = _loaded_packages()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _register_package(session, package_100)
    _register_package(session, package_101)
    session.commit()

    created = AgentRunService(session).create(
        CreateRunCommand(
            agent_id="memoir_agent",
            agent_version="1.0.1",
            business_type="couple_memory",
            business_id="archive-version-test",
            start_mode="held",
            input={
                "archive_id": "archive-version-test",
                "snapshot_id": "snapshot-version-test",
                "generation_epoch": 1,
            },
            callback_target_id="memory_callback",
            business_connector_id="couple_diary_backend",
        ),
        caller_id="couple-diary",
        tenant_id="couple-diary",
        idempotency_key="memoir-1-0-1-create",
    )
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == created.run_id))
    plan = session.scalar(select(AgentPlan).where(AgentPlan.run_id == created.run_id))

    assert run is not None and plan is not None
    assert (run.agent_version, run.package_digest, run.contract_version) == (
        package_101.version,
        package_101.package_digest,
        "1.0.0",
    )
    assert [step["node_id"] for step in plan.steps_json] == [
        node.node_id for node in package_101.workflow_nodes
    ]
    assert len(plan.steps_json) == 10

    # Worker 仍走 configured_executor 的 connector、授权与 Package 选择路径；只替换
    # 会产生真实 HTTP/模型 I/O 的节点 Runner，避免单测触网或读取业务正文。
    monkeypatch.setattr(
        worker.settings,
        "RUNTIME_BUSINESS_CONNECTORS_JSON",
        '{"couple_diary_backend":{"enabled":true,'
        '"base_url":"https://business.example.test","runtime_id":"agent-runtime",'
        '"key_id":"dev","secret":"test-secret"}}',
        raising=False,
    )
    runner = _RecordingPackageRunner()
    monkeypatch.setattr(worker, "MemoirNodeRunner", lambda *args: runner)
    lease = _lease_for(run)
    session.commit()

    result = worker.configured_executor(session).run(run.run_id, lease)

    assert (result.status, result.error_code) == ("succeeded", None)
    assert runner.node_ids == [node.node_id for node in package_101.workflow_nodes]


def test_historical_1_0_0_run_uses_its_frozen_package_plan_and_executor() -> None:
    """历史 1.0.0 Run 不受 1.0.1 状态影响，按自身 digest+完整 Plan 执行。"""
    package_100, package_101 = _loaded_packages()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _register_package(session, package_100)
    _register_package(session, package_101)
    session.commit()

    # 1.0.0 的文件内容保持 legacy 冻结原貌，不用于新 create；历史 Run 的 Plan
    # 则是在当时由真实 StaticPlanner 从该版本 Package 冻结并持久化的完整图。
    run = AgentRun(
        run_id="historical-memoir-1-0-0",
        agent_id=package_100.agent_id,
        agent_version=package_100.version,
        package_digest=package_100.package_digest,
        contract_version=package_100.contract_version,
        business_type="couple_memory",
        business_id="archive-historical",
        status="pending",
        dispatch_state="held",
        input_json={
            "archive_id": "archive-historical",
            "snapshot_id": "snapshot-historical",
            "generation_epoch": 1,
        },
        authorization_version=1,
        caller_id="couple-diary",
        tenant_id="couple-diary",
        create_idempotency_key="historical-create",
        callback_target_id="memory_callback",
        business_connector_id="couple_diary_backend",
        trace_id="historical-trace",
        run_deadline_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(run)
    StaticPlanner().persist(session, StaticPlanner().create_plan(run.run_id, package_100))
    session.flush()
    # 1.0.1 被撤销只应停止绑定 1.0.1 的 Run，不能错误阻断 1.0.0 历史 Run。
    definition_101 = session.scalar(
        select(AgentDefinition).where(AgentDefinition.version == "1.0.1")
    )
    assert definition_101 is not None
    definition_101.status = "revoked"
    lease = _lease_for(run)
    session.commit()

    runner = _RecordingPackageRunner()
    result = WorkflowExecutor(
        session,
        runner,
        CheckpointStore(session, FernetCheckpointCipher.generate()),
        ArtifactStore(session),
    ).run(run.run_id, lease)

    assert (result.status, result.error_code) == ("succeeded", None)
    assert runner.node_ids == [node.node_id for node in package_100.workflow_nodes]
    assert session.scalar(select(AgentStep).where(AgentStep.run_id == run.run_id))
    assert package_100.package_digest != package_101.package_digest
    assert package_100.contract_version == package_101.contract_version == "1.0.0"


def test_historical_1_0_0_run_resumes_real_checkpoint_with_configured_executor(
    monkeypatch,
) -> None:
    """历史 Run 从真实 checkpoint 恢复，仍只使用其冻结的 1.0.0 Package。

    这是 ``resume`` 而非 direct ``WorkflowExecutor.run`` 的证据：Package、完整
    StaticPlanner 图、CheckpointStore 与 Worker 装配路径都是真实实现；仅替换会触发
    外部模型/Tool I/O 的节点执行器。
    """
    import app.worker as worker

    package_100, package_101 = _loaded_packages()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _register_package(session, package_100)
    _register_package(session, package_101)
    run = AgentRun(
        run_id="historical-memoir-1-0-0-checkpoint-resume",
        agent_id=package_100.agent_id,
        agent_version=package_100.version,
        package_digest=package_100.package_digest,
        contract_version=package_100.contract_version,
        business_type="couple_memory",
        business_id="archive-historical-resume",
        status="waiting_human",
        dispatch_state="claimed",
        input_json={
            "archive_id": "archive-historical-resume",
            "snapshot_id": "snapshot-historical-resume",
            "generation_epoch": 1,
        },
        authorization_version=1,
        caller_id="couple-diary",
        tenant_id="couple-diary",
        create_idempotency_key="historical-resume-create",
        callback_target_id="memory_callback",
        business_connector_id="couple_diary_backend",
        trace_id="historical-resume-trace",
        execution_attempt=1,
        lease_owner="version-test-worker",
        fencing_token=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        run_deadline_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(run)
    StaticPlanner().persist(session, StaticPlanner().create_plan(run.run_id, package_100))
    # 后续 package 被撤销不得改变该历史 Run 的恢复可执行性。
    package_101_definition = session.scalar(
        select(AgentDefinition).where(AgentDefinition.version == package_101.version)
    )
    assert package_101_definition is not None
    package_101_definition.status = "revoked"
    lease = LeaseContext(
        execution_attempt=1,
        lease_owner="version-test-worker",
        fencing_token=1,
        lease_expires_at=run.lease_expires_at,
        privacy_version=run.privacy_version,
        authorization_version=run.authorization_version,
    )
    # checkpoint 必须使用 Worker 实际装配的 cipher，才能证明 resume 真正读取了
    # 已冻结的持久化状态，而不是靠一个无法解密的伪 checkpoint 退化。
    CheckpointStore(
        session,
        FernetCheckpointCipher(worker.settings.MEMORY_SNAPSHOT_FERNET_KEY.encode()),
    ).save(
        run.run_id, "resume", {"completed_node_ids": []}, lease
    )
    session.commit()

    monkeypatch.setattr(
        worker.settings,
        "RUNTIME_BUSINESS_CONNECTORS_JSON",
        '{"couple_diary_backend":{"enabled":true,"base_url":"https://business.example.test",'
        '"runtime_id":"agent-runtime","key_id":"dev","secret":"test-secret"}}',
        raising=False,
    )
    runner = _RecordingPackageRunner()
    monkeypatch.setattr(worker, "MemoirNodeRunner", lambda *args: runner)

    result = worker.configured_executor(session).resume(run.run_id, lease)

    assert (result.status, result.error_code) == ("succeeded", None)
    assert runner.node_ids == [node.node_id for node in package_100.workflow_nodes]
    assert package_100.package_digest != package_101.package_digest
