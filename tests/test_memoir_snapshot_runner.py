import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.contracts.tools import ToolError
from app.db.sqlalchemy_db import Base
from app.runtime.state import AgentState
from app.runtime.tool_gateway import ToolErrorRejected
from app.services.tool_call_audit_service import ToolCallAuditService


def test_load_snapshot_writes_only_runtime_memory_state():
    class Gateway:
        def get_snapshot(self, connector_id, archive_id, snapshot_id, run_id, generation_epoch, tool_context=None):
            return {"diaries": ["私密正文"]}
    run = type("Run", (), {"input_json": {"archive_id": "a", "snapshot_id": "s", "generation_epoch": 0}, "business_connector_id": "c", "run_id": "r", "agent_id": "memoir_agent", "agent_version": "1.0.1", "business_type": "couple_memory", "business_id": "a", "trace_id": "trace-1"})()
    state = AgentState()
    assert MemoirNodeRunner(Gateway()).run_node({"node_id": "load_snapshot"}, run, state) == {"node_id": "load_snapshot", "snapshot_loaded": True}
    assert state.snapshot == {"diaries": ["私密正文"]}


@pytest.mark.parametrize(
    ("code", "retryable", "expected"),
    [
        ("GENERATION_SUPERSEDED", False, "GENERATION_SUPERSEDED"),
        ("MEMORY_SNAPSHOT_UNAVAILABLE", False, "MEMORY_SNAPSHOT_UNAVAILABLE"),
        ("RUNTIME_SERVICE_UNAVAILABLE", True, "TOOL_RETRYABLE_FAILURE"),
    ],
)
def test_load_snapshot_consumes_validated_tool_error_without_body_leak(
    code: str, retryable: bool, expected: str, caplog: pytest.LogCaptureFixture,
) -> None:
    class Gateway:
        def get_snapshot(self, *args, **kwargs):
            raise ToolErrorRejected(ToolError(
                error_code=code, error_type="fixture", retryable=retryable,
                safe_message="private response must not escape", details_visible_to_model=False,
            ))

    run = type("Run", (), {"input_json": {"archive_id": "a", "snapshot_id": "s", "generation_epoch": 1}, "business_connector_id": "c", "run_id": "r", "agent_id": "memoir_agent", "agent_version": "1.0.1", "business_type": "couple_memory", "business_id": "a", "trace_id": "trace-1"})()
    with pytest.raises(RuntimeError, match=expected):
        MemoirNodeRunner(Gateway()).run_node({"node_id": "load_snapshot"}, run, AgentState())
    assert "private response must not escape" not in caplog.text


def test_publish_document_writes_only_publish_result():
    class Gateway:
        def publish_playback_document(self, *args):
            return {"revision": 1, "content_digest": "digest"}
    run = type("Run", (), {"input_json": {"archive_id": "a", "snapshot_id": "s", "generation_epoch": 1}, "business_connector_id": "c", "run_id": "r", "execution_attempt": 1, "agent_id": "memoir_agent", "agent_version": "1.0.1", "business_type": "couple_memory", "business_id": "a", "trace_id": "trace-1"})()
    state = AgentState(playback_document={"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []})
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    assert MemoirNodeRunner(Gateway(), ToolCallAuditService(session)).run_node({"node_id": "publish_document"}, run, state) == {"node_id": "publish_document", "published": True}
    assert state.publish_result == {"revision": 1, "content_digest": "digest"}


def test_compute_stats_uses_snapshot_counts_and_empty_fallback():
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r"})()
    state = AgentState(snapshot={"diaries": [{"content": "私密 1"}, {"content": "私密 2"}], "bets": [{"title": "私密"}]})
    assert runner.run_node({"node_id": "compute_stats"}, run, state) == {"node_id": "compute_stats", "stats_ready": True}
    assert state.stats == {"diary_count": 2, "bet_count": 1, "has_material": True}
    empty = AgentState(snapshot={})
    runner.run_node({"node_id": "compute_stats"}, run, empty)
    assert empty.stats == {"diary_count": 0, "bet_count": 0, "has_material": False}


def test_snapshot_envelope_is_consumed_without_copying_control_fields():
    """Runner 只消费版本化素材槽，冻结关系元数据不得进入模型素材摘要。"""
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r"})()
    state = AgentState(
        snapshot={
            "schema_version": "1.0.0",
            "source_range": {
                "relationship_segment_no": 3,
                "user_snapshots": [{"user_id": 1, "nickname": "snapshot-name"}],
            },
            "diary_items": [{"id": "d1", "content": "safe fixture"}],
            "bet_items": [{"id": "b1", "content": "safe fixture"}],
            "stats": {"diary_count": 1, "bet_count": 1},
        }
    )

    runner.run_node({"node_id": "compute_stats"}, run, state)
    runner.run_node({"node_id": "sanitize_materials"}, run, state)

    assert state.stats == {"diary_count": 1, "bet_count": 1, "has_material": True}
    # R2 后 legacy bet_items 经 legacy reader 单向归一化为 completed_bet 前缀，
    # 不再向 sanitized_material / 下游 allowlist 回写 bet: 形状。
    assert [item["source_ref"] for item in state.sanitized_material["materials"]] == [
        "diary:d1",
        "completed_bet:b1",
    ]
    assert "snapshot-name" not in str(state.sanitized_material)


def test_disabled_media_node_is_skipped_without_calling_gateway():
    """第一版媒体节点必须显式跳过，不能借预留 TTS 契约触网。"""

    class NoNetworkGateway:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"disabled media must not call {name}")

    state = AgentState()
    result = MemoirNodeRunner(NoNetworkGateway()).run_node(
        {"node_id": "enqueue_media_tasks"},
        type("Run", (), {"run_id": "r"})(),
        state,
    )

    assert result == {
        "node_id": "enqueue_media_tasks",
        "skipped": True,
        "reason_code": "CAPABILITY_DISABLED",
    }
    assert state.media_tasks == []


def test_sanitize_materials_redacts_identifiers_and_keeps_safe_reference():
    """普通素材只保留可追溯引用与最多 80 字的脱敏摘要。"""
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r"})()
    state = AgentState(
        snapshot={"diaries": [{"id": "d1", "content": "小明电话13800138000"}]}
    )

    assert runner.run_node({"node_id": "sanitize_materials"}, run, state) == {
        "node_id": "sanitize_materials",
        "sanitized": True,
    }
    assert state.sanitized_material == {
        "materials": [
            {
                "source_ref": "diary:d1",
                "type": "diary",
                "sensitive": False,
                "summary": "我电话[REDACTED]",
            }
        ]
    }


def test_sanitize_materials_drops_sensitive_item_text():
    """明确敏感素材仍可追溯，但绝不复制正文。"""
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r"})()
    state = AgentState(
        snapshot={"diaries": [{"id": "d1", "content": "私密正文", "sensitive": True}]}
    )

    runner.run_node({"node_id": "sanitize_materials"}, run, state)

    assert state.sanitized_material == {
        "materials": [{"source_ref": "diary:d1", "type": "diary", "sensitive": True}]
    }
    assert "私密正文" not in str(state.sanitized_material)


def test_extract_highlights_uses_stable_source_ids_without_copying_content():
    """高光只能使用脱敏后的非敏感引用，不能回读原始快照。"""
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r", "agent_version": "1.0.0"})()
    state = AgentState(
        snapshot={"diaries": [{"id": "forged", "content": "私密正文"}]},
        sanitized_material={"materials": [
            {"source_ref": "diary:d-1", "type": "diary", "sensitive": False, "summary": "摘要"},
            {"source_ref": "diary:d-2", "type": "diary", "sensitive": False, "summary": "摘要"},
            {"source_ref": "completed_bet:b-1", "type": "completed_bet", "sensitive": False, "summary": "摘要"},
        ]},
    )
    assert runner.run_node({"node_id": "extract_highlights"}, run, state) == {"node_id": "extract_highlights", "fallback": True}
    assert state.highlights == {"source_refs": ["diary:d-1", "diary:d-2", "completed_bet:b-1"], "mode": "template"}
    assert "私密正文" not in str(state.highlights)


def test_highlights_ignore_sensitive_material_reference():
    """敏感素材不进入高光候选，即使原始快照仍在内存中。"""
    state = AgentState(
        snapshot={"diaries": [{"id": "d1", "content": "私密正文"}]},
        sanitized_material={"materials": [
            {"source_ref": "diary:d1", "type": "diary", "sensitive": True},
        ]},
    )

    MemoirNodeRunner(object()).run_node(
        {"node_id": "extract_highlights"},
        type("Run", (), {"run_id": "r", "agent_version": "1.0.0"})(),
        state,
    )

    assert state.highlights == {"source_refs": [], "mode": "template"}


def test_template_chapters_scenes_and_actions_form_playable_fallback():
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r", "agent_version": "1.0.0"})()
    state = AgentState(
        stats={"diary_count": 2, "bet_count": 1, "has_material": True},
        highlights={"source_refs": ["diary:d-1", "completed_bet:b-1"], "mode": "template"},
    )
    assert runner.run_node({"node_id": "plan_chapters"}, run, state) == {"node_id": "plan_chapters", "fallback": True}
    assert runner.run_node({"node_id": "generate_scenes"}, run, state) == {"node_id": "generate_scenes", "fallback": True}
    assert runner.run_node({"node_id": "generate_actions"}, run, state) == {"node_id": "generate_actions", "fallback": True}
    assert state.chapter_plan == {"chapters": [{"chapter_id": "chapter-1", "source_refs": ["diary:d-1", "completed_bet:b-1"], "kind": "memory_overview"}]}
    assert state.scenes == [
        {"scene_id": "scene-1", "scene_type": "summary", "source_refs": ["diary:d-1", "completed_bet:b-1"]},
        {"scene_id": "scene-2", "scene_type": "summary", "source_refs": []},
        {"scene_id": "scene-3", "scene_type": "summary", "source_refs": []},
    ]
    assert state.actions == [
        {"action_id": "action-1", "scene_id": "scene-1", "action_type": "show_card", "duration_ms": 3000},
        {"action_id": "action-2", "scene_id": "scene-2", "action_type": "show_card", "duration_ms": 3000},
        {"action_id": "action-3", "scene_id": "scene-3", "action_type": "show_card", "duration_ms": 3000},
    ]


def test_safety_review_builds_complete_document_and_falls_back_for_invalid_actions():
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r"})()
    state = AgentState(
        scenes=[
            {"scene_id": "scene-1", "scene_type": "summary", "source_refs": ["diary:d-1"]},
            {"scene_id": "scene-2", "scene_type": "summary", "source_refs": []},
            {"scene_id": "scene-3", "scene_type": "summary", "source_refs": []},
        ],
        actions=[
            {"action_id": f"action-{index}", "scene_id": f"scene-{index}", "action_type": "show_card", "duration_ms": 3000}
            for index in range(1, 4)
        ],
    )
    assert runner.run_node({"node_id": "safety_review"}, run, state) == {"node_id": "safety_review", "safe": True}
    assert state.playback_document == {"schema_version": "1.0.0", "scenes": state.scenes, "actions": state.actions, "media_manifest": []}
    invalid = AgentState(scenes=[], actions=[{"scene_id": "missing", "duration_ms": -1}])
    runner.run_node({"node_id": "safety_review"}, run, invalid)
    assert invalid.safety_report == {"decision": "fallback", "reason": "INVALID_PLAYBACK_STRUCTURE"}
    assert invalid.playback_document["scenes"][0]["scene_id"] == "scene-1"


def test_safety_review_replaces_overlong_or_too_many_scenes():
    """发布审核拒绝超过 16 张或正文超过 80 字的场景。"""
    scenes = [
        {"scene_id": f"scene-{index}", "scene_type": "summary", "source_refs": [], "body": "x" * 81}
        for index in range(1, 18)
    ]
    actions = [
        {"action_id": f"action-{index}", "scene_id": f"scene-{index}", "action_type": "show_card", "duration_ms": 3000}
        for index in range(1, 18)
    ]
    state = AgentState(scenes=scenes, actions=actions)

    result = MemoirNodeRunner(object()).run_node({"node_id": "safety_review"}, type("Run", (), {"run_id": "r"})(), state)

    assert result == {"node_id": "safety_review", "safe": False}
    assert len(state.playback_document["scenes"]) == 3
    assert all(scene["source_refs"] == [] for scene in state.playback_document["scenes"])
    assert state.playback_document["media_manifest"] == []


def test_safety_review_replaces_forbidden_emotional_wording_without_logging_body(caplog: object):
    """关系失败、责备与复合暗示等禁语必须回退，日志不得回显正文。"""
    scenes = [
        {"scene_id": f"scene-{index}", "scene_type": "summary", "source_refs": [], "body": "都怪你" if index == 1 else "安全摘要"}
        for index in range(1, 4)
    ]
    actions = [
        {"action_id": f"action-{index}", "scene_id": f"scene-{index}", "action_type": "show_card", "duration_ms": 3000}
        for index in range(1, 4)
    ]
    state = AgentState(scenes=scenes, actions=actions)

    MemoirNodeRunner(object()).run_node({"node_id": "safety_review"}, type("Run", (), {"run_id": "r"})(), state)

    assert state.safety_report == {"decision": "fallback", "reason": "INVALID_PLAYBACK_STRUCTURE"}
    assert "都怪你" not in caplog.text  # type: ignore[attr-defined]


def test_sanitize_materials_fails_closed_when_legacy_and_canonical_bet_envelope_coexist() -> None:
    """新旧 bet envelope 字段同时出现时 sanitize_materials 必须 fail closed。

    R2 legacy reader：bet_items/bets 与 completed_bet_items/completed_bets 同现
    会双计数并污染 allowlist；Runner 在 sanitize_materials 入口直接拒绝，
    Executor 把 LegacyEnvelopeError 转为 WORKFLOW_NODE_FAILED，不进入 checkpoint。
    """
    from app.runtime.material_schema import LegacyEnvelopeError

    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r"})()
    state = AgentState(
        snapshot={
            "bet_items": [{"id": "b1", "content": "safe fixture"}],
            "completed_bet_items": [{"id": "b1", "content": "safe fixture"}],
        }
    )

    with pytest.raises(LegacyEnvelopeError, match="LEGACY_ENVELOPE_MIXED_WITH_CANONICAL"):
        runner.run_node({"node_id": "sanitize_materials"}, run, state)


def test_sanitize_materials_emits_completed_bet_for_legacy_envelope_only() -> None:
    """只有 legacy bet_items/bets 时正常归一化为 completed_bet 前缀。"""
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r"})()
    state = AgentState(
        snapshot={
            "diary_items": [{"id": "d1", "content": "safe fixture"}],
            "bets": [{"id": "b-legacy", "content": "safe fixture"}],
        }
    )

    runner.run_node({"node_id": "sanitize_materials"}, run, state)

    refs = [item["source_ref"] for item in state.sanitized_material["materials"]]
    assert refs == ["diary:d1", "completed_bet:b-legacy"]
    # 旧 bet: 前缀绝不进入 sanitized_material / 下游 allowlist。
    assert all(not ref.startswith("bet:") for ref in refs)


def test_sanitize_materials_recognizes_all_five_canonical_material_types() -> None:
    """handbook_note/matured_wish/bucket_list_completion 三类只产出稳定 source_ref。

    正文不进入 sanitized_material（sensitive=True 占位），allowlist/Scene 仍可引用。
    """
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r"})()
    state = AgentState(
        snapshot={
            "diary_items": [{"id": "d1", "content": "safe"}],
            "completed_bet_items": [{"id": "b1", "content": "safe"}],
            "handbook_notes": [{"id": "h1", "content": "handbook-private"}],
            "matured_wishes": [{"id": "w1", "content": "wish-private"}],
            "bucket_list_completions": [{"id": "c1", "content": "checklist-private"}],
        }
    )

    runner.run_node({"node_id": "sanitize_materials"}, run, state)

    refs = [item["source_ref"] for item in state.sanitized_material["materials"]]
    assert set(refs) == {
        "diary:d1",
        "completed_bet:b1",
        "handbook_note:h1",
        "matured_wish:w1",
        "bucket_list_completion:c1",
    }
    serialized = str(state.sanitized_material)
    # 后三类正文不进入 sanitized 视图。
    assert "handbook-private" not in serialized
    assert "wish-private" not in serialized
    assert "checklist-private" not in serialized


def test_compute_stats_prefers_canonical_materials_without_double_counting() -> None:
    """方案 A 契约：顶层 materials 列表是唯一计数源，legacy envelope 键被遮蔽。

    业务端 get_snapshot 透传 canonical materials 后，diary/bet 计数只看
    material_type，同一份素材不会因 legacy 键同时存在而双计数。
    """
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r"})()
    state = AgentState(
        snapshot={
            # legacy 键若被读取会得到 5+5；canonical 真值是 2+1。
            "diaries": [{"content": "legacy"}] * 5,
            "bets": [{"title": "legacy"}] * 5,
            "materials": [
                {"material_type": "diary", "source_ref": "diary:d1", "sanitized_payload": {"id": "d1"}},
                {"material_type": "diary", "source_ref": "diary:d2", "sanitized_payload": {"id": "d2"}},
                {"material_type": "completed_bet", "source_ref": "completed_bet:b1", "sanitized_payload": {"id": "b1"}},
            ],
        }
    )

    runner.run_node({"node_id": "compute_stats"}, run, state)

    assert state.stats == {"diary_count": 2, "bet_count": 1, "has_material": True}


def test_sanitize_materials_consumes_canonical_materials_contract() -> None:
    """方案 A 契约：canonical materials 逐类收敛为最小视图。

    - diary/completed_bet + Mapping payload：sensitive=False + 80 字元数据摘要
      （source_ref 可进入模型 allowlist），摘要内敏感标识符仍被 redact；
    - 其余三类：ref-only sensitive=True，payload 不进入视图；
    - 非法项（非 Mapping / 缺 source_ref）被丢弃且不影响其余素材。
    """
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r"})()
    state = AgentState(
        snapshot={
            "materials": [
                {
                    "material_type": "diary",
                    "source_ref": "diary:d1",
                    "sanitized_payload": {"id": "d1", "entry_date": "2026-08-01", "tags": ["电话13800138000"]},
                },
                {
                    "material_type": "completed_bet",
                    "source_ref": "completed_bet:b1",
                    "sanitized_payload": {"id": "b1", "winner_user_id": 1},
                },
                {
                    "material_type": "handbook_note",
                    "source_ref": "handbook_note:h1",
                    "sanitized_payload": {"id": "h1"},
                },
                "not-a-mapping",
                {"material_type": "matured_wish"},
            ]
        }
    )

    assert runner.run_node({"node_id": "sanitize_materials"}, run, state) == {
        "node_id": "sanitize_materials",
        "sanitized": True,
    }
    materials = state.sanitized_material["materials"]
    assert materials[0] == {
        "source_ref": "diary:d1",
        "type": "diary",
        "sensitive": False,
        "summary": '{"id":"d1","entry_date":"2026-08-01","tags":["电话[REDACTED]"]}',
    }
    assert materials[1] == {
        "source_ref": "completed_bet:b1",
        "type": "completed_bet",
        "sensitive": False,
        "summary": '{"id":"b1","winner_user_id":1}',
    }
    assert materials[2] == {"source_ref": "handbook_note:h1", "type": "handbook_note", "sensitive": True}
    # 非法项被丢弃：只剩 3 条合法素材。
    assert len(materials) == 3
    # 三类敏感素材的 payload 不进入 sanitized 视图。
    assert '"id":"h1"' not in str(state.sanitized_material)


def test_canonical_material_refs_flow_into_model_allowlist() -> None:
    """canonical sensitive=False 的 source_ref 必须能进入模型 allowlist（反空壳关键链路）。"""
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r", "agent_version": "1.0.0"})()
    state = AgentState(
        snapshot={
            "materials": [
                {"material_type": "diary", "source_ref": "diary:d1", "sanitized_payload": {"id": "d1"}},
                {"material_type": "completed_bet", "source_ref": "completed_bet:b1", "sanitized_payload": {"id": "b1"}},
                {"material_type": "matured_wish", "source_ref": "matured_wish:w1", "sanitized_payload": {"id": "w1"}},
            ]
        }
    )

    runner.run_node({"node_id": "sanitize_materials"}, run, state)
    runner.run_node({"node_id": "extract_highlights"}, run, state)

    assert state.highlights == {
        "source_refs": ["diary:d1", "completed_bet:b1"],
        "mode": "template",
    }
