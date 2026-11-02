"""QA 模型适配器只发送受权威身份约束、分槽且有界的临时正文。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.qa_agent.model_gateway import QaModelGatewayAdapter
from app.db.sqlalchemy_db import Base
from app.models import AgentRun, AgentStep
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
        self, route_id: str, prompt: PromptDefinition, estimated_input_tokens: int,
    ) -> bool:
        return self.available

    def call(
        self, context: ModelCallContext, route_id: str, request: dict[str, object],
        *, prompt: PromptDefinition,
    ) -> ModelGatewayResult:
        self.calls += 1
        if self.failure:
            raise self.failure
        self.inspect(context, route_id, request, prompt)
        return ModelGatewayResult("succeeded", {"answer": "", "source_refs": [], "no_evidence": True})


def _session() -> tuple[Session, LeaseContext]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    expires = datetime.now(UTC) + timedelta(minutes=1)
    session.add(AgentRun(
        run_id="qa-model-run", agent_id="qa_agent", agent_version="1.0.0",
        package_digest="digest", contract_version="1.0.0", business_type="business_qa",
        business_id="turn-1", status="running", dispatch_state="claimed",
        input_json={"conversation_id": "conversation-1", "turn_id": "turn-1"},
        authorization_version=1, caller_id="caller", tenant_id="tenant",
        create_idempotency_key="qa-model-key", callback_target_id="callback",
        business_connector_id="qa_business_backend", trace_id="qa-trace",
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=expires, capability_snapshot_json={"allowed_model_route_ids": ["qa-small"]},
        run_deadline_at=expires,
    ))
    session.add(AgentStep(
        step_id="qa-model-step", run_id="qa-model-run", step_name="compose_answer",
        step_type="model", status="running", execution_attempt=1, step_attempt=1,
        input_summary={"estimated_input_tokens": 20},
    ))
    session.commit()
    return session, LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=expires, privacy_version=1, authorization_version=1,
    )


def _request() -> dict[str, object]:
    return {"question_context": {"question": "QA_QUESTION_SENTINEL", "evidence": [
        {"source_ref": "evidence-1", "text": "QA_EVIDENCE_SENTINEL"},
    ]}}


def test_qa_adapter_uses_exact_prompt_and_authoritative_identity_with_data_slots(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session, lease = _session()
    gateway = RecordingGateway()

    def inspect(context: ModelCallContext, route_id: str, request: dict[str, object], prompt: PromptDefinition) -> None:
        assert context.step_id == "qa-model-step"
        assert context.allowed_route_ids == frozenset({"qa-small"})
        assert route_id == "qa-small"
        assert (prompt.prompt_id, prompt.version, prompt.owner_agent) == ("compose-answer", "v1", "qa_agent")
        messages = request["messages"]
        assert isinstance(messages, list)
        assert "QA_QUESTION_SENTINEL" not in messages[0]["content"]
        assert "QA_EVIDENCE_SENTINEL" not in messages[0]["content"]
        data = json.loads(messages[1]["content"].split("\n", 1)[1])
        assert data["candidate_input"]["allowed_source_refs"] == ["evidence-1"]
        assert any(item["content"] == "QA_QUESTION_SENTINEL" for item in data["untrusted_items"])
        assert any(item["content"] == "QA_EVIDENCE_SENTINEL" for item in data["untrusted_items"])
        assert request["response_format"] == {"type": "json_object"}

    gateway.inspect = inspect
    result = QaModelGatewayAdapter(session, cast(ModelGateway, gateway), {"compose_answer": "qa-small"}, lease).call(
        "qa-model-run", "compose_answer", _request(),
    )

    assert result.status == "succeeded"
    assert gateway.calls == 1
    assert "QA_QUESTION_SENTINEL" not in caplog.text
    assert "QA_EVIDENCE_SENTINEL" not in caplog.text
    # Store 中只保留 Run 的业务引用与 Step 的安全摘要。
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "qa-model-run"))
    step = session.scalar(select(AgentStep).where(AgentStep.step_id == "qa-model-step"))
    assert run is not None and "SENTINEL" not in str(run.input_json)
    assert step is not None and "SENTINEL" not in str(step.input_summary)


@pytest.mark.parametrize("budget, expected_chars", [(2000, 4096), (32, 128)])
def test_qa_context_budget_only_tightens_route_window(budget: int, expected_chars: int) -> None:
    session, lease = _session()
    gateway = RecordingGateway(budget)

    def inspect(context: ModelCallContext, route_id: str, request: dict[str, object], prompt: PromptDefinition) -> None:
        messages = request["messages"]
        assert isinstance(messages, list)
        data = json.loads(messages[1]["content"].split("\n", 1)[1])
        assert sum(len(item["content"]) for item in data["untrusted_items"]) == expected_chars
        assert data["candidate_input"]["allowed_source_refs"] == []

    gateway.inspect = inspect
    result = QaModelGatewayAdapter(session, cast(ModelGateway, gateway), {"compose_answer": "qa-small"}, lease).call(
        "qa-model-run", "compose_answer", {"question_context": {
            "question": "q" * 5000, "evidence": [{"source_ref": "ref-1", "text": "e" * 5000}],
        }},
    )
    assert result.status == "succeeded"


@pytest.mark.parametrize("routes", [{}, {"compose_answer": ""}, {"compose_answer": "qa-small", "extra": "qa-small"}])
def test_qa_route_mapping_requires_exact_node_set(routes: dict[str, str]) -> None:
    session, lease = _session()
    gateway = RecordingGateway()
    result = QaModelGatewayAdapter(session, cast(ModelGateway, gateway), routes, lease).call(
        "qa-model-run", "compose_answer", _request(),
    )
    assert (result.status, result.error_code) == ("route_not_allowed", "QA_MODEL_ROUTE_UNAVAILABLE")
    assert gateway.calls == 0


def test_qa_adapter_requires_current_lease_and_one_running_step() -> None:
    session, lease = _session()
    gateway = RecordingGateway()
    adapter = QaModelGatewayAdapter(session, cast(ModelGateway, gateway), {"compose_answer": "qa-small"})
    assert adapter.call("qa-model-run", "compose_answer", _request()).status == "route_not_allowed"
    adapter.bind_lease(lease.model_copy(update={"fencing_token": 2}))
    assert adapter.call("qa-model-run", "compose_answer", _request()).status == "aborted_before_send"
    adapter.bind_lease(lease)
    step = session.scalar(select(AgentStep).where(AgentStep.step_id == "qa-model-step"))
    assert step is not None
    step.status = "succeeded"
    session.commit()
    assert adapter.call("qa-model-run", "compose_answer", _request()).status == "aborted_before_send"
    assert gateway.calls == 0


def test_qa_model_capability_failure_has_no_template_or_provider_call() -> None:
    session, lease = _session()
    gateway = RecordingGateway()
    gateway.available = False
    result = QaModelGatewayAdapter(session, cast(ModelGateway, gateway), {"compose_answer": "qa-small"}, lease).call(
        "qa-model-run", "compose_answer", _request(),
    )
    assert (result.status, result.error_code) == ("capability_disabled", "MODEL_CAPABILITY_UNAVAILABLE")
    assert result.data is None
    assert gateway.calls == 0


def test_qa_adapter_drops_provider_exception_body(caplog: pytest.LogCaptureFixture) -> None:
    session, lease = _session()
    gateway = RecordingGateway()
    gateway.failure = RuntimeError("QA_QUESTION_SENTINEL QA_EVIDENCE_SENTINEL QA_ANSWER_SENTINEL")
    result = QaModelGatewayAdapter(session, cast(ModelGateway, gateway), {"compose_answer": "qa-small"}, lease).call(
        "qa-model-run", "compose_answer", _request(),
    )
    assert (result.status, result.error_code) == ("outcome_unknown", "QA_MODEL_OUTCOME_UNKNOWN")
    assert "SENTINEL" not in caplog.text
    assert "SENTINEL" not in str(result)


def test_qa_adapter_keeps_question_label_out_of_evidence_refs() -> None:
    session, lease = _session()
    gateway = RecordingGateway()

    def inspect(context: ModelCallContext, route_id: str, request: dict[str, object], prompt: PromptDefinition) -> None:
        messages = request["messages"]
        assert isinstance(messages, list)
        data = json.loads(messages[1]["content"].split("\n", 1)[1])
        candidate = data["candidate_input"]
        assert candidate["allowed_source_refs"] == ["qa:question"]
        assert candidate["question_ref"] not in candidate["allowed_source_refs"]

    gateway.inspect = inspect
    result = QaModelGatewayAdapter(session, cast(ModelGateway, gateway), {"compose_answer": "qa-small"}, lease).call(
        "qa-model-run", "compose_answer", {"question_context": {"question": "q", "evidence": [
            {"source_ref": "qa:question", "text": "e"},
        ]}},
    )
    assert result.status == "succeeded"


@pytest.mark.parametrize("question_context", [
    {"question": "q", "evidence": [], "route_id": "forged"},
    {"question": "q", "evidence": [{"source_ref": "ref-1", "text": "e", "run_id": "forged"}]},
    {"question": "q", "evidence": [{"source_ref": "ref-1", "text": 1}]},
])
def test_qa_model_input_cannot_carry_nested_control_fields(question_context: object) -> None:
    session, lease = _session()
    gateway = RecordingGateway()
    result = QaModelGatewayAdapter(session, cast(ModelGateway, gateway), {"compose_answer": "qa-small"}, lease).call(
        "qa-model-run", "compose_answer", {"question_context": question_context},
    )
    assert (result.status, result.error_code) == ("aborted_before_send", "QA_MODEL_INPUT_INVALID")
    assert gateway.calls == 0
