"""1.0.6 回归：memoir_agent@1.0.6 完整工作流图端到端执行到 publish_document。

1.0.6 图结构与 1.0.5 完全一致（十节点 DAG），差异只在 runner 按
agent_version 门控的批次重试语义：①批次候选游标（瞬时失败不消费素材，
下一轮同批重试）；②首末批在场硬校验；③定向结构修复 required_scene_type。
本文件把真实 1.0.6 graph 交给 WorkflowExecutor + 真实 MemoirNodeRunner 执行，
模型层在网关边界打桩（与 runtime_test_memoir_105_full_graph.py 同一模式），
覆盖：三条 1.0.5 既有路径（覆盖完整 / 覆盖修复 / 修复失败）+ 1.0.6 语义路径
（瞬时失败同批重试后发布、持续失败预算内收敛不发布、媒体单图失败降级发布）。
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
from app.services.memoir.memoir_media_service import (
    MemoirMediaConfig,
    MemoirMediaService,
    build_illustration_prompt,
)
from app.services.tool_call_audit_service import ToolCallAuditService
from app.utils.volcano.cv_client import MockCVClient

# Run 冻结能力快照：与 1.0.6 agent.yaml 权威值一致（max_model_calls 6→8，
# 为循环瞬时重试留出额度）。
CAPABILITY_SNAPSHOT = {
    "model_policy": {"max_model_calls": 8, "max_tokens": 100_000, "max_model_cost": 2.0},
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

# 媒体降级测试用：cover 场景正文（fail_prompts 按 prompt 全文精确匹配，
# 必须与场景 body 同源构建才会命中）。
COVER_BODY = "那年春天我们在江边老城散步看日落。"
PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-image-body"


def _scene(scene_id: str, scene_type: str, refs: list[str], body: str) -> dict[str, object]:
    return {
        "scene_id": scene_id, "scene_type": scene_type,
        "source_refs": list(refs), "body": body,
    }


def _batch_payload(scenes: list[dict[str, object]]) -> str:
    return json.dumps({"scenes": scenes}, ensure_ascii=False)


class ScriptedModelGateway:
    """按 node_id 脚本化模型输出；记录全部调用供断言重试批次与修复输入。"""

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


class FakeUploader:
    """满足 MediaObjectUploader 协议的假 OSS：只记录元数据，返回契约 URL。"""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []

    def upload_public_bytes(self, data: bytes, object_key: str, mime: str) -> str:
        self.uploads.append((object_key, mime))
        return f"https://bucket.oss-cn-hangzhou.aliyuncs.com/{object_key}"


def _media_config() -> MemoirMediaConfig:
    return MemoirMediaConfig(
        provider_name="mock",
        image_prefix="memoir/images/",
        url_host_suffixes=("aliyuncs.com",),
        node_budget_seconds=30.0,
    )


def _build_scenario(model_outputs: dict[str, list[object]], media_service=None):
    """装配 1.0.6 全图执行夹具：真实 graph plan + 真实 Runner + 打桩网关。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    # 直接加载 1.0.6 包的正式 workflow.graph 声明（与 1.0.5 同构十节点 DAG）。
    memoir_steps = [
        node.model_dump()
        for node in AgentPackageService._load_workflow_nodes(
            Path(__file__).resolve().parents[1]
            / "app/agents/memoir_agent/1.0.6/workflow.graph.py"
        )
    ]
    run = AgentRun(
        run_id="memoir-106-full", agent_id="memoir_agent", agent_version="1.0.6",
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
        agent_id="memoir_agent", version="1.0.6", runtime_type="workflow",
        definition_json={}, package_digest="sha256:test", contract_version="1.0.0",
        status="active", status_changed_at=now, status_changed_by="test",
        status_change_reason="fixture",
    ))
    session.add(AgentPlan(
        plan_id="memoir-106-full-plan", run_id=run.run_id, strategy="static_workflow",
        steps_json=memoir_steps, stop_conditions_json={}, fallback_policy_json={},
        status="planned",
    ))
    session.commit()
    model_gateway = ScriptedModelGateway(model_outputs)
    tool_gateway = PublishingToolGateway(SNAPSHOT_PAYLOAD)
    runner = MemoirNodeRunner(
        tool_gateway, ToolCallAuditService(session),
        model_gateway=model_gateway, media_service=media_service,
    )
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
            _scene("s1-1", "cover", ["diary:d1"], COVER_BODY),
            _scene("s1-2", "diary_highlight", ["diary:d1"], "日记里写下的江边日落与晚风。"),
            _scene("s1-3", "summary", ["completed_bet:b1"], "这些小事一起写成我们的故事。"),
        ])],
    })

    result = executor.run("memoir-106-full", lease)

    assert result.status == "succeeded", result.error_code
    assert len(tool_gateway.published_documents) == 1
    # repair 节点直通：模型只被循环体调用一次，无修复调用。
    assert [node_id for node_id, _ in model_gateway.calls] == ["generate_scene_batch"]
    repair_step = session.scalar(select(AgentStep).where(
        AgentStep.run_id == "memoir-106-full",
        AgentStep.step_name == "repair_coverage_gaps",
    ))
    assert repair_step is not None and repair_step.status == "succeeded"


def test_full_graph_repairs_missing_coverage_then_publishes() -> None:
    """路径二：循环未覆盖 completed_bet → repair 补齐 r1- 场景后发布。"""
    executor, session, model_gateway, tool_gateway, lease = _build_scenario({
        "generate_scene_batch": [_batch_payload([
            _scene("s1-1", "cover", ["diary:d1"], COVER_BODY),
            _scene("s1-2", "diary_highlight", ["diary:d1"], "日记里写下的江边日落与晚风。"),
            _scene("s1-3", "summary", ["diary:d1"], "这些小事一起写成我们的故事。"),
        ])],
        "repair_coverage_gaps": [_batch_payload([
            _scene("r1-1", "bet_highlight", ["completed_bet:b1"], "赌约是谁先跑完五公里，输的人做了一周早餐。"),
        ])],
    })

    result = executor.run("memoir-106-full", lease)

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
            _scene("s1-1", "cover", ["diary:d1"], COVER_BODY),
            _scene("s1-2", "diary_highlight", ["diary:d1"], "日记里写下的江边日落与晚风。"),
            _scene("s1-3", "summary", ["diary:d1"], "这些小事一起写成我们的故事。"),
        ])],
        "repair_coverage_gaps": [_batch_payload([])],
    })

    result = executor.run("memoir-106-full", lease)

    assert result.status == "failed"
    assert tool_gateway.published_documents == []
    repair_step = session.scalar(select(AgentStep).where(
        AgentStep.run_id == "memoir-106-full",
        AgentStep.step_name == "repair_coverage_gaps",
    ))
    assert repair_step is not None and repair_step.status == "failed"


def test_transient_batch_failure_retries_same_batch_then_publishes() -> None:
    """1.0.6 候选游标：首批输出非法不消费素材，下一轮同批重试成功后照常发布。"""
    executor, session, model_gateway, tool_gateway, lease = _build_scenario({
        "generate_scene_batch": [
            "SENTINEL-RAW-OUTPUT 不是 JSON {{{",  # 批 1 首次尝试解析失败
            _batch_payload([
                _scene("s2-1", "cover", ["diary:d1"], COVER_BODY),
                _scene("s2-2", "diary_highlight", ["diary:d1"], "日记里写下的江边日落与晚风。"),
                _scene("s2-3", "summary", ["completed_bet:b1"], "这些小事一起写成我们的故事。"),
            ]),
        ],
    })

    result = executor.run("memoir-106-full", lease)

    # executor 冻结语义 7：存在被跳过的失败迭代 → 循环以 partial 收敛，
    # Run 终态降级 partial；但节点按正常完成处理，图继续走到发布。
    # 1.0.6 候选游标的价值在于：重试拿回同一批素材，最终仍发布完整文档。
    assert result.status == "partial", result.error_code
    assert len(tool_gateway.published_documents) == 1
    # 两次循环体调用拿到的是同一批素材（候选游标未前移），重试批次号 +1。
    batch_calls = [
        (node_id, request) for node_id, request in model_gateway.calls
        if node_id == "generate_scene_batch"
    ]
    assert len(batch_calls) == 2
    first_refs = [item["source_ref"] for item in batch_calls[0][1]["materials"]]  # type: ignore[union-attr]
    retry_refs = [item["source_ref"] for item in batch_calls[1][1]["materials"]]  # type: ignore[union-attr]
    assert first_refs == retry_refs == ["diary:d1", "completed_bet:b1"]
    assert batch_calls[1][1]["input"]["batch_index"] == 2  # type: ignore[index]
    # 发布文档使用重试批次的场景（s2 命名空间），结构完整。
    document = tool_gateway.published_documents[0]
    scene_ids = [scene["scene_id"] for scene in document["scenes"]]  # type: ignore[index]
    assert scene_ids[0] == "s2-1" and scene_ids[-1] == "s2-3"


def test_persistent_batch_failures_converge_failed_without_busy_loop() -> None:
    """持续失败网关：失败轮计入迭代上限（max_model_calls=8），收敛 failed 不发布。"""
    executor, session, model_gateway, tool_gateway, lease = _build_scenario({
        # 队列为空 → 每次调用 status=failed → LOOP_BATCH_MODEL_UNAVAILABLE。
        "generate_scene_batch": [],
    })

    result = executor.run("memoir-106-full", lease)

    assert result.status == "failed"
    assert tool_gateway.published_documents == []
    # 有界收敛：循环体调用次数被迭代上限封顶（预算 8），无 busy loop；
    # 每轮重试的都是同一批素材（候选游标从未提交）。
    batch_calls = [
        request for node_id, request in model_gateway.calls
        if node_id == "generate_scene_batch"
    ]
    assert len(batch_calls) == CAPABILITY_SNAPSHOT["model_policy"]["max_model_calls"]
    first_refs = [item["source_ref"] for item in batch_calls[0]["materials"]]  # type: ignore[union-attr]
    last_refs = [item["source_ref"] for item in batch_calls[-1]["materials"]]  # type: ignore[union-attr]
    assert first_refs == last_refs == ["diary:d1", "completed_bet:b1"]


def test_media_single_image_failure_degrades_to_text_card_and_publishes() -> None:
    """媒体单图失败只降级该场景为文字卡，其余配图，文档照常发布。"""
    # cover 场景的配图 prompt 固定失败（fail_prompts 按 prompt 全文精确匹配）。
    media_service = MemoirMediaService(
        MockCVClient(
            text_image=PNG_BYTES,
            fail_prompts=frozenset([build_illustration_prompt(COVER_BODY)]),
        ),
        FakeUploader(),
        _media_config(),
    )
    executor, session, model_gateway, tool_gateway, lease = _build_scenario(
        {
            "generate_scene_batch": [_batch_payload([
                _scene("s1-1", "cover", ["diary:d1"], COVER_BODY),
                _scene("s1-2", "diary_highlight", ["diary:d1"], "日记里写下的江边日落与晚风。"),
                _scene("s1-3", "summary", ["completed_bet:b1"], "这些小事一起写成我们的故事。"),
            ])],
        },
        media_service=media_service,
    )

    result = executor.run("memoir-106-full", lease)

    assert result.status == "succeeded", result.error_code
    assert len(tool_gateway.published_documents) == 1
    document = tool_gateway.published_documents[0]
    scenes = document["scenes"]  # type: ignore[index]
    # 三张卡全部保留：失败的 cover 降级为纯文字卡（无 payload），其余两张带图。
    assert len(scenes) == 3
    assert "payload" not in scenes[0]
    assert [entry["scene_id"] for entry in document["media_manifest"]] == [  # type: ignore[index]
        "s1-2", "s1-3",
    ]
    for scene in scenes[1:]:
        assert scene["payload"]["image_url"].startswith("https://")  # type: ignore[index]
