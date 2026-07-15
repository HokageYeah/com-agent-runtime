from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models import AgentPlan
from app.runtime.planner import StaticPlanner
from app.services.agent_package_service import AgentPackageService


def test_memoir_static_plan_uses_package_workflow_nodes() -> None:
    package = AgentPackageService(__import__("pathlib").Path("app/agents")).load(
        "memoir_agent", "1.0.0"
    )
    plan = StaticPlanner().create_plan("run_1", package)

    assert plan.strategy == "static_workflow"
    assert plan.steps[0]["node_id"] == "load_snapshot"
    assert plan.steps[-1]["node_id"] == "publish_document"
    assert plan.stop_conditions["max_estimated_cost"] == 2.0
    assert plan.stop_conditions["max_wall_clock_seconds"] == 172_800
    assert plan.fallback_policy["media"] == "skipped(capability_disabled)"


def test_static_plan_can_be_persisted() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    package = AgentPackageService(__import__("pathlib").Path("app/agents")).load(
        "memoir_agent", "1.0.0"
    )
    plan = StaticPlanner().create_plan("run_persist", package)
    StaticPlanner().persist(session, plan)
    assert (
        session.scalar(select(AgentPlan).where(AgentPlan.plan_id == plan.plan_id))
        is not None
    )
