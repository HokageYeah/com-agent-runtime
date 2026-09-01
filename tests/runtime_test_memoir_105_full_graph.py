"""C1 回归：memoir_agent@1.0.5 完整工作流图端到端执行到 publish_document。

此前缺陷：图声明了模型节点 repair_coverage_gaps，但 MemoirNodeRunner.run_node
无该分支，每个 1.0.5 Run 在循环产景后的第 6 步必抛
MEMOIR_NODE_NOT_IMPLEMENTED，永远到不了 generate_actions/publish。
本文件把真实 1.0.5 graph 交给 WorkflowExecutor + 真实 MemoirNodeRunner 执行，
模型层在网关边界打桩（与 runtime_test_memoir_loop_runner.py 同一模式），
覆盖三条路径：覆盖完整（repair 直通）、覆盖缺失（repair 补齐后发布）、
修复后仍缺失（Run failed，不发布）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.db.sqlalchemy_db import Base
from app.models import AgentDefinition, AgentPlan, AgentRun, AgentStep
from app.runtime.artifact import ArtifactStore
from app.runtime.checkpoint import CheckpointStore, FernetCheckpointCipher
from app.runtime.executor import WorkflowExecutor
from app.runtime.interfaces import LeaseContext
from app.services.agent_package_service import AgentPackageService
from app.services.tool_call_audit_service import ToolCallAuditService

# Run 冻结能力快照：bounded_loop 预算继承要求四项额度全部为有限正值。
CAPABILITY_SNAPSHOT = {
    "model_policy": {"max_model_calls": 6, "max_tokens": 100_000, "max_model_cost": 2.0},
    "execution_policy": {"max_run_seconds": 300, "max_steps": 32},
}

# 两类素材的小型 canonical 快照（方案 A 契约形状，含 text_digest 投影）。
SNAPSHOT_PAYLOAD = {
    "materials": [
        {
            "material_type": "diary", "source_ref": "diary:d1",
            "sanitized_payload": {"text_digest": "我们在江边散步看日落，聊到很晚的具体画面。"},
        },
        {
            "material_type": "completed_bet", "source_ref": "completed_bet:b1",
            "sanitized_payload": {"text_digest": "赌约是谁先跑完五公里，输的人做一周早餐。"},
        },
    ],
}


def _scene(scene_id: str, scene_type: str, refs: list[str], body: str) -> dict[str, object]:
    return {
        "scene_id": scene_id, "scene_type": scene_type,
        "source_refs": list(refs), "body": body,
    }


def _batch_payload(scenes: list[dict[str, object]]) -> str:
    return json.dumps({"scenes": scenes}, ensure_ascii=False)


class ScriptedModelGateway:
    """按 node_id 脚本化模型输出；记录全部调用供断言修复调用次数与输入。"""

    def __init__(self, outputs: dict[str, list[object]]) -> None:
        self._outputs = {key: list(value) for key, value in outputs.items()}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
        self.calls.append((node_id, request))
        queue = self._outputs.get(node_id)
        output = queue.pop(0) if queue else None
        if output is None:
            return SimpleNamespace(status="failed", data=None)
        return SimpleNamespace(status="succeeded", data=output)


class PublishingToolGateway:
    """受控业务工具替身：get_snapshot 返回合成快照，publish 记录发布文档。"""

    def __init__(self, snapshot_payload: dict[str, object]) -> None:
        self._snapshot_payload = snapshot_payload
        self.published_documents: list[dict[str, object]] = []

    def get_snapshot(self, *args: object) -> dict[str, object]:
        return self._snapshot_payload

    def publish_playback_document(
        self, *args: object, **kwargs: object,
    ) -> dict[str, object]:
        # 参数序：connector_id, archive_id, run_id, snapshot_id, epoch, document, ...
        self.published_documents.append(args[5])  # type: ignore[index]
        return {"revision": 1, "content_digest": "published-digest"}

    def get_publish_result(self, *args: object) -> dict[str, object]:
        return {"revision": 1, "content_digest": "published-digest"}


def _build_scenario(model_outputs: dict[str, list[object]]):
    """装配 1.0.5 全图执行夹具：真实 graph plan + 真实 Runner + 打桩网关。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    # 直接加载 1.0.5 包的正式 workflow.graph 声明（含 bounded_loop/repair 节点）。
    memoir_steps = [
        node.model_dump()
        for node in AgentPackageService._load_workflow_nodes(
            Path(__file__).resolve().parents[1]
            / "app/agents/memoir_agent/1.0.5/workflow.graph.py"
        )
    ]
    run = AgentRun(
        run_id="memoir-105-full", agent_id="memoir_agent", agent_version="1.0.5",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="claimed",
        input_json={"archive_id": "archive", "snapshot_id": "snapshot", "generation_epoch": 0},
        capability_snapshot_json=CAPABILITY_SNAPSHOT,
        authorization_version=1, caller_id="caller", tenant_id="tenant",
        create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace",
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
    )
    session.add(run)
    session.add(AgentDefinition(
        agent_id="memoir_agent", version="1.0.5", runtime_type="workflow",
        definition_json={}, package_digest="sha256:test", contract_version="1.0.0",
        status="active", status_changed_at=now, status_changed_by="test",
        status_change_reason="fixture",
    ))
    session.add(AgentPlan(
        plan_id="memoir-105-full-plan", run_id=run.run_id, strategy="static_workflow",
        steps_json=memoir_steps, stop_conditions_json={}, fallback_policy_json={},
        status="planned",
    ))
    session.commit()
    model_gateway = ScriptedModelGateway(model_outputs)
    tool_gateway = PublishingToolGateway(SNAPSHOT_PAYLOAD)
    runner = MemoirNodeRunner(tool_gateway, ToolCallAuditService(session), model_gateway=model_gateway)
    executor = WorkflowExecutor(
        session, runner,
        CheckpointStore(session, FernetCheckpointCipher.generate()),
        ArtifactStore(session),
    )
    lease = LeaseContext(
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
        authorization_version=1,
    )
    return executor, session, model_gateway, tool_gateway, lease


def test_full_graph_succeeds_when_coverage_complete() -> None:
    """路径一：循环产物已覆盖全部素材类型 → repair 直通，全图发布成功。"""
    executor, session, model_gateway, tool_gateway, lease = _build_scenario({
        "generate_scene_batch": [_batch_payload([
            _scene("s1-1", "cover", ["diary:d1"], "那年春天我们在江边老城散步看日落。"),
            _scene("s1-2", "diary_highlight", ["diary:d1"], "日记里写下的江边日落与晚风。"),
            _scene("s1-3", "summary", ["completed_bet:b1"], "这些小事一起写成我们的故事。"),
        ])],
    })

    result = executor.run("memoir-105-full", lease)

    # C1 回归断言：全图执行到 publish，而不是在第 6 步抛 MEMOIR_NODE_NOT_IMPLEMENTED。
    assert result.status == "succeeded", result.error_code
    assert len(tool_gateway.published_documents) == 1
    # repair 节点直通：模型只被循环体调用一次，无修复调用。
    assert [node_id for node_id, _ in model_gateway.calls] == ["generate_scene_batch"]
    repair_step = session.scalar(select(AgentStep).where(
        AgentStep.run_id == "memoir-105-full",
        AgentStep.step_name == "repair_coverage_gaps",
    ))
    assert repair_step is not None and repair_step.status == "succeeded"


def test_full_graph_repairs_missing_coverage_then_publishes() -> None:
    """路径二：循环未覆盖 completed_bet → repair 补齐 r1- 场景后发布。"""
    executor, session, model_gateway, tool_gateway, lease = _build_scenario({
        "generate_scene_batch": [_batch_payload([
            _scene("s1-1", "cover", ["diary:d1"], "那年春天我们在江边老城散步看日落。"),
            _scene("s1-2", "diary_highlight", ["diary:d1"], "日记里写下的江边日落与晚风。"),
            _scene("s1-3", "summary", ["diary:d1"], "这些小事一起写成我们的故事。"),
        ])],
        "repair_coverage_gaps": [_batch_payload([
            _scene("r1-1", "bet_highlight", ["completed_bet:b1"], "赌约是谁先跑完五公里，输的人做了一周早餐。"),
        ])],
    })

    result = executor.run("memoir-105-full", lease)

    assert result.status == "succeeded", result.error_code
    assert len(tool_gateway.published_documents) == 1
    # 修复调用恰好一次，输入只含缺失类型 completed_bet。
    repair_calls = [request for node_id, request in model_gateway.calls
                    if node_id == "repair_coverage_gaps"]
    assert len(repair_calls) == 1
    assert repair_calls[0]["input"]["missing_material_types"] == ["completed_bet"]  # type: ignore[index]
    # 发布文档包含修复场景，且 summary 仍是最后一张（修复只补中间场景）。
    document = tool_gateway.published_documents[0]
    scene_ids = [scene["scene_id"] for scene in document["scenes"]]  # type: ignore[index]
    assert "r1-1" in scene_ids
    assert scene_ids[-1] == "s1-3"
    covered_types = {
        ref.split(":", 1)[0]
        for scene in document["scenes"]  # type: ignore[index]
        for ref in scene["source_refs"]
    }
    assert {"diary", "completed_bet"} <= covered_types


def test_full_graph_fails_when_repair_cannot_cover() -> None:
    """路径三：修复输出仍未覆盖（素材不足以成卡返回空数组）→ Run failed 不发布。"""
    executor, session, model_gateway, tool_gateway, lease = _build_scenario({
        "generate_scene_batch": [_batch_payload([
            _scene("s1-1", "cover", ["diary:d1"], "那年春天我们在江边老城散步看日落。"),
            _scene("s1-2", "diary_highlight", ["diary:d1"], "日记里写下的江边日落与晚风。"),
            _scene("s1-3", "summary", ["diary:d1"], "这些小事一起写成我们的故事。"),
        ])],
        "repair_coverage_gaps": [_batch_payload([])],
    })

    result = executor.run("memoir-105-full", lease)

    assert result.status == "failed"
    assert tool_gateway.published_documents == []
    repair_step = session.scalar(select(AgentStep).where(
        AgentStep.run_id == "memoir-105-full",
        AgentStep.step_name == "repair_coverage_gaps",
    ))
    assert repair_step is not None and repair_step.status == "failed"
