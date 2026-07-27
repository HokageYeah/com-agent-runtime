"""Runtime privacy purge 的物理清理回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import (
    AgentArtifact,
    AgentCheckpoint,
    AgentModelUsage,
    AgentRun,
    AgentStep,
    AgentToolCall,
)
from app.services.agent_run_service import AgentRunService
from app.services.idempotency_service import IdempotencyService


def _run() -> AgentRun:
    """构造已建立 privacy 写屏障的最小 Run，不放入真实业务正文。"""
    now = datetime.now(UTC)
    return AgentRun(
        run_id="purge-run",
        agent_id="memoir_agent",
        agent_version="1.0.0",
        package_digest="sha256:test",
        contract_version="1.0.0",
        business_type="couple_memory",
        business_id="archive-1",
        status="cancelled",
        dispatch_state="finished",
        input_json={"private": "secret"},
        output_summary_json={"private": "secret"},
        authorization_version=1,
        caller_id="caller",
        tenant_id="tenant",
        create_idempotency_key="create-1",
        callback_target_id="memory_callback",
        business_connector_id="connector",
        trace_id="trace",
        run_deadline_at=now + timedelta(days=1),
        privacy_state="purge_requested",
        privacy_version=2,
    )


def test_complete_purge_removes_private_checkpoint_and_scrubs_runtime_summaries() -> None:
    """purge 完成后不得保留可承载私密内容的 checkpoint、artifact、step 或工具摘要。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = _run()
    session.add(run)
    session.add_all(
        [
            AgentCheckpoint(
                checkpoint_id="checkpoint-1",
                run_id=run.run_id,
                checkpoint_key="state",
                state_schema_version="1.0.0",
                data_classification="private",
                privacy_version=1,
                encrypted_state_blob=b"encrypted-private-state",
                state_summary={"private": "secret"},
                content_digest="digest",
                expires_at=now + timedelta(days=1),
                created_at=now,
            ),
            AgentArtifact(
                artifact_id="artifact-1",
                run_id=run.run_id,
                artifact_type="temporary",
                schema_version="1.0.0",
                content_digest="digest",
                summary_json={"private": "secret"},
                business_resource_ref="memory:archive-1",
            ),
            AgentStep(
                step_id="step-1",
                run_id=run.run_id,
                step_name="publish",
                step_type="tool",
                status="succeeded",
                execution_attempt=1,
                input_summary={"private": "secret"},
                output_summary={"private": "secret"},
            ),
            AgentToolCall(
                tool_call_id="tool-1",
                run_id=run.run_id,
                step_id="step-1",
                tool_name="memory.publish_playback_document",
                transport="http",
                side_effect=True,
                execution_attempt=1,
                input_summary={"private": "secret"},
                output_summary={"private": "secret"},
                status="succeeded",
                created_at=now,
            ),
            AgentModelUsage(
                id=1,
                usage_id="usage-1",
                run_id=run.run_id,
                step_id="step-1",
                execution_attempt=1,
                model_attempt=1,
                status="succeeded",
                # 模拟旧版本或异常写入的 JSON：purge 不能把这类载荷当作安全元数据保留。
                capability_snapshot_json={
                    "capabilities": ["structured_output"],
                    "prompt": "private-marker",
                    "tool_payload": {"private": "private-marker"},
                    "signed_url": "https://example.invalid/private-marker",
                },
                thinking_summary_json={
                    "thinking_enabled": False,
                    "max_output_tokens": 512,
                    "input_token_budget": 7680,
                    "normalization_version": "v1",
                    "hidden_reasoning": "private-marker",
                },
                input_tokens=1,
                output_tokens=1,
                estimated_cost=0.1,
            ),
        ]
    )
    session.commit()

    assert AgentRunService(session).complete_purge(run.run_id) is True
    session.commit()

    refreshed = session.scalar(select(AgentRun).where(AgentRun.run_id == run.run_id))
    artifact = session.scalar(select(AgentArtifact).where(AgentArtifact.artifact_id == "artifact-1"))
    step = session.scalar(select(AgentStep).where(AgentStep.step_id == "step-1"))
    tool_call = session.scalar(select(AgentToolCall).where(AgentToolCall.tool_call_id == "tool-1"))
    usage = session.scalar(select(AgentModelUsage).where(AgentModelUsage.usage_id == "usage-1"))
    assert refreshed is not None and refreshed.input_json == {} and refreshed.output_summary_json is None
    assert session.scalar(select(AgentCheckpoint).where(AgentCheckpoint.run_id == run.run_id)) is None
    assert artifact is not None and artifact.summary_json is None
    assert step is not None and step.input_summary is None and step.output_summary is None
    assert tool_call is not None and tool_call.input_summary is None and tool_call.output_summary is None
    assert usage is not None
    assert usage.capability_snapshot_json == {"capabilities": ["structured_output"]}
    assert usage.thinking_summary_json == {
        "thinking_enabled": False,
        "max_output_tokens": 512,
        "input_token_budget": 7680,
        "normalization_version": "v1",
    }


def test_expired_purge_idempotency_record_is_retained_until_run_is_confirmed_purged() -> None:
    """未确认完成的 purge 不能清掉原幂等记录，避免重放创建第二个副作用。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = _run()
    session.add(run)
    IdempotencyService(session).store(
        "caller",
        "purge",
        "purge-key",
        "request-digest",
        {"run_id": run.run_id, "privacy_state": "purge_requested"},
        run.run_id,
        ttl_days=-1,
    )
    session.commit()

    assert IdempotencyService(session).cleanup_expired(now) == 0
    assert IdempotencyService(session).replay(
        "caller", "purge", "purge-key", "request-digest"
    ) == {"run_id": run.run_id, "privacy_state": "purge_requested"}
    run.privacy_state = "purged"
    assert IdempotencyService(session).cleanup_expired(now) == 1
