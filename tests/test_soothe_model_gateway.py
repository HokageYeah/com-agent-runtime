"""Soothe 模型适配器只发送受权威身份约束、分槽且有界的临时正文。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.soothe_agent.model_gateway import SootheModelGatewayAdapter
from app.db.sqlalchemy_db import Base
from app.models import AgentRun, AgentStep
from app.runtime.context_manager import ContextManager
from app.runtime.interfaces import LeaseContext
from app.runtime.model_gateway import ModelCallContext, ModelGateway, ModelGatewayResult
from app.runtime.prompt_registry import PromptDefinition


class RecordingGateway:
    """替换实际网络边界，不保存或打印收到的业务正文。"""

    def __init__(self, budget: int = 2000) -> None:
        self.budget = budget
        self.calls = 0
        self.inspect = lambda context, route_id, request, prompt: None
        self.available = True
        self.failure: Exception | None = None

    def context_token_budget(self, route_id: str, prompt: PromptDefinition) -> int:
        return self.budget

    def capability_available(
        self,
        route_id: str,
        prompt: PromptDefinition,
        estimated_input_tokens: int,
    ) -> bool:
        return self.available

    def call(
        self,
        context: ModelCallContext,
        route_id: str,
        request: dict[str, object],
        *,
        prompt: PromptDefinition,
    ) -> ModelGatewayResult:
        self.calls += 1
        if self.failure:
            raise self.failure
        self.inspect(context, route_id, request, prompt)
        return ModelGatewayResult(
            "succeeded", {"answer": "", "source_refs": [], "no_evidence": True}
        )


def _session() -> tuple[Session, LeaseContext]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    expires = datetime.now(UTC) + timedelta(minutes=1)
    session.add(
        AgentRun(
            run_id="soothe-model-run",
            agent_id="soothe_agent",
            agent_version="1.0.0",
            package_digest="digest",
            contract_version="1.0.0",
            business_type="soothe_reply",
            business_id="turn-1",
            status="running",
            dispatch_state="claimed",
            input_json={"conversation_id": "conversation-1", "turn_id": "turn-1"},
            authorization_version=1,
            caller_id="caller",
            tenant_id="tenant",
            create_idempotency_key="soothe-model-key",
            callback_target_id="callback",
            business_connector_id="qa_business_backend",
            trace_id="qa-trace",
            execution_attempt=1,
            lease_owner="worker",
            fencing_token=1,
            lease_expires_at=expires,
            capability_snapshot_json={"allowed_model_route_ids": ["soothe-small"]},
            run_deadline_at=expires,
        )
    )
    session.add(
        AgentStep(
            step_id="soothe-model-step",
            run_id="soothe-model-run",
            step_name="compose_turn",
            step_type="model",
            status="running",
            execution_attempt=1,
            step_attempt=1,
            input_summary={"estimated_input_tokens": 20},
        )
    )
    session.commit()
    return session, LeaseContext(
        execution_attempt=1,
        lease_owner="worker",
        fencing_token=1,
        lease_expires_at=expires,
        privacy_version=1,
        authorization_version=1,
    )


def _request() -> dict[str, object]:
    return {
        "turn_context": {
            "mode": "reply",
            "chat_text": "PRIVATE_CHAT_SENTINEL",
            "background": "PRIVATE_BACKGROUND",
            "tone": "light",
            "previous_replies": ["PRIVATE_PREVIOUS"],
        }
    }


def _simulator() -> dict[str, object]:
    return {
        "turn_context": {
            "mode": "simulator",
            "scenario": {
                "key": "k",
                "title": "场景",
                "anger_reason": "原因",
                "hidden_need": "HIDDEN_NEED_SENTINEL",
            },
            "partner_gender": "female",
            "partner_name": "昵称",
            "forgiveness": 42,
            "round_count": 1,
            "history": [{"user_message": "历史发言", "reply": "历史回复"}],
            "user_message": "本轮发言",
        }
    }


def _call(
    session: Session,
    lease: LeaseContext | None,
    gateway: RecordingGateway,
    request: dict[str, object] | None = None,
    routes: dict[str, str] | None = None,
    node: str = "compose_turn",
) -> ModelGatewayResult:
    return SootheModelGatewayAdapter(
        session,
        cast(ModelGateway, gateway),
        {"compose_turn": "soothe-small"} if routes is None else routes,
        lease,
    ).call("soothe-model-run", node, _request() if request is None else request)


@pytest.mark.parametrize("simulator", [False, True])
def test_exact_prompt_roles_private_slots_and_token_floor(
    simulator: bool, caplog: pytest.LogCaptureFixture
) -> None:
    session, lease = _session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "soothe-model-run"))
    assert run is not None
    if simulator:
        run.business_type = "soothe_simulator"
        session.commit()
    gateway = RecordingGateway()

    def inspect(
        context: ModelCallContext,
        route_id: str,
        request: dict[str, object],
        prompt: PromptDefinition,
    ) -> None:
        assert route_id == "soothe-small"
        assert prompt.prompt_id == (
            "compose-simulator" if simulator else "compose-reply"
        )
        assert prompt.version == "v1" and prompt.owner_agent == "soothe_agent"
        messages = request["messages"]
        assert isinstance(messages, list)
        assert [m["role"] for m in messages] == ["system", "user"]
        trusted = messages[0]["content"]
        assert "{{" not in trusted
        data = json.loads(messages[1]["content"].split("\n", 1)[1])
        assert data["candidate_input"] == {}
        refs = {item["source_ref"] for item in data["untrusted_items"]}
        if simulator:
            assert refs == {
                "soothe:history",
                "soothe:user_message",
                "soothe:partner_name",
            }
            assert "HIDDEN_NEED_SENTINEL" in trusted and "女朋友" in trusted
            assert (
                "昵称" not in trusted
                and "本轮发言" not in trusted
                and "历史发言" not in trusted
            )
        else:
            assert refs == {
                "soothe:chat",
                "soothe:background",
                "soothe:previous_replies",
            }
            assert "PRIVATE" not in trusted and "tone=light" in trusted
        assert request["response_format"] == {"type": "json_object"}
        assert (
            context.estimated_input_tokens
            >= (len(json.dumps(request, ensure_ascii=False, separators=(",", ":"))) + 3)
            // 4
        )

    gateway.inspect = inspect
    assert (
        _call(session, lease, gateway, _simulator() if simulator else _request()).status
        == "succeeded"
    )
    assert gateway.calls == 1
    assert "SENTINEL" not in caplog.text
    assert "SENTINEL" not in str(run.input_json)
    step = session.scalar(
        select(AgentStep).where(AgentStep.step_id == "soothe-model-step")
    )
    assert step is not None and "SENTINEL" not in str(step.input_summary)


@pytest.mark.parametrize(
    "routes", [{}, {"compose_turn": ""}, {"compose_turn": "soothe-small", "extra": "x"}]
)
def test_exact_routes(routes: dict[str, str]) -> None:
    session, lease = _session()
    gateway = RecordingGateway()
    assert (
        _call(session, lease, gateway, routes=routes).error_code
        == "SOOTHE_MODEL_ROUTE_UNAVAILABLE"
    )
    assert gateway.calls == 0


@pytest.mark.parametrize(
    "field,value",
    [("agent_id", "other"), ("agent_version", "latest"), ("business_type", "other")],
)
def test_identity_rejected(field: str, value: str) -> None:
    session, lease = _session()
    setattr(
        session.scalar(select(AgentRun).where(AgentRun.run_id == "soothe-model-run")),
        field,
        value,
    )
    session.commit()
    gateway = RecordingGateway()
    assert _call(session, lease, gateway).error_code == "SOOTHE_MODEL_CONTEXT_UNTRUSTED"
    assert gateway.calls == 0


def test_lease_step_and_node_rejected() -> None:
    session, lease = _session()
    gateway = RecordingGateway()
    assert _call(session, None, gateway).error_code == "SOOTHE_MODEL_ROUTE_UNAVAILABLE"
    assert (
        _call(session, lease, gateway, node="other").error_code
        == "SOOTHE_MODEL_ROUTE_UNAVAILABLE"
    )
    assert (
        _call(
            session, lease.model_copy(update={"fencing_token": 2}), gateway
        ).error_code
        == "SOOTHE_MODEL_CALL_REJECTED"
    )
    step = session.scalar(
        select(AgentStep).where(AgentStep.step_id == "soothe-model-step")
    )
    assert step is not None
    step.execution_attempt = 2
    session.commit()
    assert _call(session, lease, gateway).error_code == "SOOTHE_MODEL_CONTEXT_UNTRUSTED"
    assert gateway.calls == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"turn_context": {"mode": "other"}},
        _simulator(),
        {
            "turn_context": {
                **cast(dict[str, object], _request()["turn_context"]),
                "route_id": "forged",
            }
        },
    ],
)
def test_invalid_input(payload: dict[str, object]) -> None:
    session, lease = _session()
    gateway = RecordingGateway()
    assert (
        _call(session, lease, gateway, payload).error_code
        == "SOOTHE_MODEL_INPUT_INVALID"
    )
    assert gateway.calls == 0


@pytest.mark.parametrize("budget,expected", [(10000, 2000), (32, 128)])
def test_route_budget_clamps_materials(
    budget: int, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_budget = ContextManager.node_token_budget
    observed: list[int] = []

    def capture_budget(
        manager: ContextManager, node_id: str, model_token_budget: int
    ) -> int:
        result = original_budget(manager, node_id, model_token_budget)
        observed.append(result)
        return result

    monkeypatch.setattr(ContextManager, "node_token_budget", capture_budget)
    session, lease = _session()
    gateway = RecordingGateway(budget)
    request: dict[str, object] = {
        "turn_context": {
            "mode": "reply",
            "chat_text": "x" * 2000,
            "background": None,
            "tone": "any",
            "previous_replies": [],
        }
    }

    def inspect(
        context: ModelCallContext,
        route_id: str,
        request: dict[str, object],
        prompt: PromptDefinition,
    ) -> None:
        messages = request["messages"]
        assert isinstance(messages, list)
        data = json.loads(messages[1]["content"].split("\n", 1)[1])
        assert sum(len(item["content"]) for item in data["untrusted_items"]) == expected

    gateway.inspect = inspect
    assert _call(session, lease, gateway, request).status == "succeeded"
    assert observed == [min(4096, budget)]


@pytest.mark.parametrize(
    "failure,code",
    [
        (ValueError("HIDDEN_NEED_SENTINEL"), "SOOTHE_MODEL_CALL_REJECTED"),
        (RuntimeError("HIDDEN_NEED_SENTINEL"), "SOOTHE_MODEL_OUTCOME_UNKNOWN"),
    ],
)
def test_exception_privacy(
    failure: Exception, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    session, lease = _session()
    gateway = RecordingGateway()
    gateway.failure = failure
    result = _call(session, lease, gateway)
    assert result.error_code == code
    assert "HIDDEN_NEED_SENTINEL" not in str(result) + caplog.text


def test_capability_unavailable() -> None:
    session, lease = _session()
    gateway = RecordingGateway()
    gateway.available = False
    assert _call(session, lease, gateway).error_code == "MODEL_CAPABILITY_UNAVAILABLE"
    assert gateway.calls == 0


def test_lease_rebind_and_missing_or_duplicate_running_steps() -> None:
    session, lease = _session()
    gateway = RecordingGateway()
    adapter = SootheModelGatewayAdapter(
        session, cast(ModelGateway, gateway), {"compose_turn": "soothe-small"}
    )
    adapter.bind_lease(lease)
    assert (
        adapter.call("soothe-model-run", "compose_turn", _request()).status
        == "succeeded"
    )
    step = session.scalar(
        select(AgentStep).where(AgentStep.step_id == "soothe-model-step")
    )
    assert step is not None
    step.status = "succeeded"
    session.commit()
    assert (
        adapter.call("soothe-model-run", "compose_turn", _request()).error_code
        == "SOOTHE_MODEL_CONTEXT_UNTRUSTED"
    )
    step.status = "running"
    session.add(
        AgentStep(
            step_id="duplicate",
            run_id="soothe-model-run",
            step_name="compose_turn",
            step_type="model",
            status="running",
            execution_attempt=1,
            step_attempt=2,
            input_summary={},
        )
    )
    session.commit()
    assert (
        adapter.call("soothe-model-run", "compose_turn", _request()).error_code
        == "SOOTHE_MODEL_CONTEXT_UNTRUSTED"
    )
    assert gateway.calls == 1


def test_trusted_replacement_is_single_pass_and_preserves_json() -> None:
    session, lease = _session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "soothe-model-run"))
    assert run is not None
    run.business_type = "soothe_simulator"
    session.commit()
    gateway = RecordingGateway()
    payload = _simulator()
    private = cast(dict[str, object], payload["turn_context"])
    scenario = cast(dict[str, object], private["scenario"])
    scenario["title"] = "保留 {{hidden_need}} 与 {JSON}"

    def inspect(
        context: ModelCallContext,
        route_id: str,
        request: dict[str, object],
        prompt: PromptDefinition,
    ) -> None:
        assert "保留 {{hidden_need}} 与 {JSON}" in prompt.template
        assert '"delta_suggestion"' in prompt.template

    gateway.inspect = inspect
    assert _call(session, lease, gateway, payload).status == "succeeded"
