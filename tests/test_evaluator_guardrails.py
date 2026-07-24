from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.db.sqlalchemy_db import Base
from app.models import AgentEvaluation
from app.runtime.evaluator import MemoirPlaybackEvaluator
from app.runtime.policy_engine import ExecutionBudgetExceeded, PolicyEngine
from app.runtime.state import AgentState
from app.services.evaluation_service import EvaluationService


def _playback() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    scenes = [
        {"scene_id": f"scene-{index}", "scene_type": "summary", "safety_level": "normal", "source_refs": ["diary:d-1"] if index == 1 else []}
        for index in range(1, 4)
    ]
    actions = [
        {"action_id": f"action-{index}", "scene_id": f"scene-{index}", "action_type": "show_card", "duration_ms": 3000, "action_order": index}
        for index in range(1, 4)
    ]
    return scenes, actions


def test_evaluator_rejects_ungrounded_refs_and_sensitive_emotional_body() -> None:
    scenes, actions = _playback()
    scenes[0]["source_refs"] = ["diary:forged"]
    scenes[1]["body"] = "都怪你，应该复合"

    decision = MemoirPlaybackEvaluator().evaluate(
        scenes, actions, trusted_source_refs={"diary:d-1"}, enabled_capabilities=set(),
    )

    assert decision.decision == "fallback"
    assert set(decision.reasons) == {"UNKNOWN_SOURCE_REF", "EMOTIONAL_LANGUAGE_BLOCKED"}
    assert decision.safe_summary == {"scene_count": 3, "action_count": 3, "source_ref_count": 1}


def test_evaluator_enforces_scene_action_domain_order_and_closed_media_capability() -> None:
    scenes, actions = _playback()
    scenes[0]["scene_type"] = "unknown"
    actions[1]["action_order"] = 3
    actions[2]["action_type"] = "focus_image"

    decision = MemoirPlaybackEvaluator().evaluate(
        scenes, actions, trusted_source_refs={"diary:d-1"}, enabled_capabilities=set(),
    )

    assert decision.decision == "fallback"
    assert set(decision.reasons) == {
        "SCENE_TYPE_INVALID", "ACTION_ORDER_INVALID", "ACTION_CAPABILITY_DISABLED",
    }


def test_evaluation_service_persists_only_safe_evaluation_summary() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    scenes, actions = _playback()
    decision = MemoirPlaybackEvaluator().evaluate(
        scenes, actions, trusted_source_refs={"diary:d-1"}, enabled_capabilities=set(),
    )

    EvaluationService(session).record(
        run_id="run-1", step_id="safety_review", target_type="playback_document",
        target_id="draft", evaluator_type="memoir_playback", evaluation=decision,
    )
    session.commit()

    stored = session.scalar(select(AgentEvaluation))
    assert stored is not None
    assert stored.decision == "pass"
    assert stored.score_json == {"scene_count": 3, "action_count": 3, "source_ref_count": 1}
    assert (
        stored.schema_passed,
        stored.grounding_passed,
        stored.material_reference_passed,
        stored.hallucination_detected,
        stored.emotional_safety_passed,
    ) == (True, True, True, False, True)
    assert stored.reason_summary == "OK"


def test_evaluation_service_persists_reference_and_hallucination_flags() -> None:
    """素材引用越权只持久化布尔结论与受控码，不能保存伪造引用本身。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    scenes, actions = _playback()
    scenes[0]["source_refs"] = ["diary:forged"]
    decision = MemoirPlaybackEvaluator().evaluate(
        scenes, actions, trusted_source_refs={"diary:d-1"}, enabled_capabilities=set(),
    )

    EvaluationService(session).record(
        run_id="run-1", step_id="safety_review", target_type="playback_document",
        target_id="draft", evaluator_type="memoir_playback", evaluation=decision,
    )
    session.commit()

    stored = session.scalar(select(AgentEvaluation))
    assert stored is not None
    assert (stored.material_reference_passed, stored.hallucination_detected) == (False, True)
    assert "diary:forged" not in str(stored.score_json)


def test_safety_review_records_evaluation_without_playback_content() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    scenes, actions = _playback()
    runner = MemoirNodeRunner(object(), evaluation_service=EvaluationService(session))
    run = type("Run", (), {"run_id": "run-1"})()

    runner.run_node({"node_id": "safety_review"}, run, AgentState(scenes=scenes, actions=actions))
    session.commit()

    stored = session.scalar(select(AgentEvaluation))
    assert stored is not None
    assert stored.step_id == "safety_review"
    assert "body" not in str(stored.score_json)


def test_execution_budget_uses_active_elapsed_not_run_created_time() -> None:
    run = type("Run", (), {
        "capability_snapshot_json": {"execution_policy": {"max_steps": 3, "max_tool_calls": 2, "max_run_seconds": 5}},
        "active_elapsed_ms": 4_900,
    })()
    policy = PolicyEngine(Session(create_engine("sqlite://")))

    policy.assert_can_continue(run, {"steps": 3, "tool_calls": 2, "active_elapsed_ms": 100})
    try:
        policy.assert_can_continue(run, {"steps": 4})
    except ExecutionBudgetExceeded as exc:
        assert exc.code == "STEP_LIMIT_EXCEEDED"
    else:
        raise AssertionError("超过冻结 steps 额度必须拒绝")
    try:
        policy.assert_can_continue(run, {"active_elapsed_ms": 101})
    except ExecutionBudgetExceeded as exc:
        assert exc.code == "ACTIVE_TIME_LIMIT_EXCEEDED"
    else:
        raise AssertionError("活跃执行时间超限必须拒绝")
