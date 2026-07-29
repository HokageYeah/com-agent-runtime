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
