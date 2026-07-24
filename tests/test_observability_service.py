from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import AgentEvaluation, AgentModelUsage, AgentRun, AgentToolCall
from app.services.observability_service import ObservabilityService


def test_observability_service_aggregates_only_safe_numeric_facts() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all([
        AgentRun(run_id="run", agent_id="agent", agent_version="1", package_digest="digest", contract_version="1", business_type="memoir", business_id="business", status="succeeded", dispatch_state="finished", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace", active_elapsed_ms=321, run_deadline_at=datetime.now(UTC) + timedelta(minutes=1)),
        AgentEvaluation(id=1, evaluation_id="eval-1", run_id="run", step_id=None, target_type="x", target_id=None, evaluator_type="x", score_json={"prompt": "private-marker"}, decision="pass", reason_summary="OK", schema_passed=True, grounding_passed=True, material_reference_passed=True, hallucination_detected=False, emotional_safety_passed=True, created_at=datetime.now(UTC)),
        AgentEvaluation(id=2, evaluation_id="eval-2", run_id="run", step_id=None, target_type="x", target_id=None, evaluator_type="x", score_json={"content": "private-marker"}, decision="fallback", reason_summary="UNKNOWN_SOURCE_REF", schema_passed=False, grounding_passed=False, material_reference_passed=False, hallucination_detected=True, emotional_safety_passed=True, created_at=datetime.now(UTC)),
        AgentModelUsage(id=1, usage_id="usage-1", run_id="run", step_id="step", execution_attempt=1, model_attempt=1, status="outcome_unknown", reserved_estimated_cost=2.0, capability_snapshot_json={"tool_payload": "private-marker"}),
        AgentModelUsage(id=2, usage_id="usage-2", run_id="run", step_id="step", execution_attempt=1, model_attempt=2, status="aborted_before_send", reserved_estimated_cost=3.0),
        AgentModelUsage(id=3, usage_id="usage-3", run_id="run", step_id="step", execution_attempt=2, model_attempt=1, status="succeeded", reserved_estimated_cost=0.5, estimated_cost=1.25, input_tokens=100, output_tokens=50),
        AgentToolCall(tool_call_id="tool-1", run_id="run", step_id="step", tool_name="tool", transport="http", side_effect=False, execution_attempt=1, status="succeeded", duration_ms=25, created_at=datetime.now(UTC)),
    ])
    session.commit()

    report = ObservabilityService(session).report_for_run("run")

    assert report.as_dict()["evaluation_count"] == 2
    assert report.as_dict()["unknown_outcome_count"] == 1
    assert report.as_dict()["tool_call_count"] == 1
    assert report.as_dict()["reserved_cost"] == 2.0
    assert report.as_dict()["actual_model_cost"] == 1.25
    assert report.as_dict()["model_attempt_count"] == 3
    assert report.as_dict()["execution_attempt_count"] == 2
    assert report.as_dict()["aborted_before_send_count"] == 1
    assert report.as_dict()["tool_elapsed_ms"] == 25
    assert report.as_dict()["active_elapsed_ms"] == 321
    assert report.as_dict()["schema_pass_rate"] == 0.5
    assert report.as_dict()["material_reference_pass_rate"] == 0.5
    assert report.as_dict()["hallucination_rate"] == 0.5
    assert report.as_dict()["fallback_rate"] == 0.5
    assert "private-marker" not in str(report.as_dict())
