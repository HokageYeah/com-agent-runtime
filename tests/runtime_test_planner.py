from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.sqlalchemy_db import Base
from app.models import AgentPlan
from app.runtime.planner import StaticPlanner, StaticPlanValidationError
from app.services.agent_package_service import AgentPackageService


def test_memoir_static_plan_uses_package_workflow_nodes() -> None:
    """AgentPlan 只能冻结 Package AST 已解析出的节点顺序。"""
    package = AgentPackageService(__import__("pathlib").Path("app/agents")).load(
        "memoir_agent", "1.0.0"
    )
    plan = StaticPlanner().create_plan("run_1", package)

    assert plan.strategy == "static_workflow"
    assert [step["node_id"] for step in plan.steps] == [
        "load_snapshot",
        "sanitize_materials",
        "compute_stats",
        "extract_highlights",
        "plan_chapters",
        "generate_scenes",
        "generate_actions",
        "safety_review",
        "publish_document",
        "enqueue_media_tasks",
    ]
    assert plan.steps[-1]["optional"] is True
    assert plan.stop_conditions["max_estimated_cost"] == 2.0
    assert plan.stop_conditions["max_wall_clock_seconds"] == 172_800
    assert plan.fallback_policy["media"] == "skipped(capability_disabled)"


@pytest.mark.parametrize(
    "workflow_nodes",
    [
        [{"node_id": "unsafe", "node_type": "python"}],
        ["__import__('os').system('unexpected')"],
    ],
)
def test_definition_malformed_nodes_cannot_create_executable_plan(
    workflow_nodes: list[object],
) -> None:
    """注册定义中的节点也必须重走静态节点 schema，绝不原样落库。"""
    with pytest.raises(StaticPlanValidationError, match="workflow_nodes"):
        StaticPlanner().create_plan_from_definition(
            "run_reject", {"workflow_nodes": workflow_nodes}
        )


def test_legacy_definition_missing_safe_to_rerun_rejected_at_planner() -> None:
    """P1：legacy definition（节点缺 safe_to_rerun）必须在 Planner 冻结时 fail closed。

    三种语义必须严格区分：
    - 缺键（legacy）：在 safe_to_rerun 引入前注册的旧定义，无法安全判定 resume 时
      跳过还是重算 → 一律 fail closed 交业务侧重建；
    - 显式 False：非 memoir / partial 已完成 optional，resume 跳过；
    - 显式 True：memoir 读取/内容/发布节点，resume 强制重算。

    根因：``WorkflowNodeDefinition.safe_to_rerun`` 有默认 ``False``，Planner 用
    ``model_validate`` 会把"缺键"悄悄补成"显式 False"并经 ``model_dump`` 落库。于是
    Executor 的 ``PLAN_LEGACY_DEFINITION`` guard（``if "safe_to_rerun" not in node``）
    永不触发——新冻结的 step 已带显式 False，legacy 缺键被静默吞掉。故必须在
    ``_freeze_definition_steps`` 入口回看原始 dict 拒绝缺键 definition。
    """
    with pytest.raises(StaticPlanValidationError):
        StaticPlanner().create_plan_from_definition(
            "run_legacy_definition",
            {"workflow_nodes": [{"node_id": "load_snapshot", "node_type": "tool"}]},
        )


def test_definition_explicit_safe_to_rerun_freezes_normally() -> None:
    """显式 True / False 不触发 legacy 拒绝，正常冻结并保留原值。

    防止 legacy guard 过拟合：只要节点显式声明 safe_to_rerun（无论 True/False），
    Planner 必须照常冻结，语义交给 Executor 的 R2 分类恢复判定。
    """
    plan_true = StaticPlanner().create_plan_from_definition(
        "run_explicit_true",
        {"workflow_nodes": [
            {"node_id": "load_snapshot", "node_type": "tool", "safe_to_rerun": True}
        ]},
    )
    assert plan_true.steps[0]["safe_to_rerun"] is True

    plan_false = StaticPlanner().create_plan_from_definition(
        "run_explicit_false",
        {"workflow_nodes": [
            {"node_id": "enqueue_media", "node_type": "tool",
             "optional": True, "safe_to_rerun": False}
        ]},
    )
    assert plan_false.steps[0]["safe_to_rerun"] is False
    assert plan_false.steps[0]["optional"] is True


def test_static_plan_can_be_persisted() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    package = AgentPackageService(__import__("pathlib").Path("app/agents")).load(
        "memoir_agent", "1.0.0"
    )
    plan = StaticPlanner().create_plan("run_persist", package)
    StaticPlanner().persist(session, plan)
    record = session.scalar(select(AgentPlan).where(AgentPlan.plan_id == plan.plan_id))

    assert record is not None
    assert [step["node_id"] for step in record.steps_json] == [
        step["node_id"] for step in plan.steps
    ]


def test_plan_freezes_bounded_loop_policy_into_steps() -> None:
    """M7：bounded_loop 节点的 loop_policy 必须完整冻结进 plan steps。

    Executor 只按已冻结计划执行；loop_policy 若在 Planner 冻结时被丢弃，
    受控循环就会退化成不可审计的自由循环。策略逐字段断言防静默降级。
    """
    plan = StaticPlanner().create_plan_from_definition(
        "run_bounded_loop",
        {
            "workflow_nodes": [
                {
                    "node_id": "load_snapshot",
                    "node_type": "tool",
                    "safe_to_rerun": True,
                },
                {
                    "node_id": "generate_scene_batches",
                    "node_type": "bounded_loop",
                    "safe_to_rerun": True,
                    "loop_policy": {
                        "budget_strategy": "inherit_run_limits_v1",
                        "merge_strategy": "append_unique_by_key",
                        "merge_key": "scene_id",
                        "on_iteration_error": "continue",
                        "on_budget_exhausted": "partial",
                        "body_node_ids": ["generate_scene_batch"],
                    },
                },
            ]
        },
    )

    loop_step = next(
        step for step in plan.steps if step["node_id"] == "generate_scene_batches"
    )
    frozen_policy = loop_step["loop_policy"]
    assert frozen_policy["budget_strategy"] == "inherit_run_limits_v1"
    assert frozen_policy["merge_strategy"] == "append_unique_by_key"
    assert frozen_policy["merge_key"] == "scene_id"
    assert frozen_policy["on_iteration_error"] == "continue"
    assert frozen_policy["on_budget_exhausted"] == "partial"
    assert frozen_policy["body_node_ids"] == ["generate_scene_batch"]
