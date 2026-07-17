from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.sqlalchemy_db import Base
from app.models import AgentDefinition, AgentModelUsage, AgentRun, AgentStep
from app.runtime.interfaces import LeaseContext
from app.runtime.model_gateway import (
    ModelCallContext,
    ModelGateway,
    ModelRoute,
    ModelRouteRegistry,
    ProviderTrafficController,
)
from app.runtime.policy_engine import PolicyEngine
from app.schemas.agent_run import CreateRunCommand
from app.services.agent_run_service import AgentRunService
from app.services.lease_service import LeaseService
from app.services.model_usage_service import ModelUsageService
from tests.test_provider_traffic_controller import FakeRedis


class RecordingProvider:
    def __init__(self, response: object = {"ok": True}) -> None:
        self.response = response
        self.calls = 0

    def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object:
        self.calls += 1
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class RevokingLease:
    def __init__(self, values: list[bool]) -> None:
        self._last = values[-1]
        self._values = iter(values)

    def can_write(self, run_id: str, context: LeaseContext) -> bool:
        try:
            self._last = next(self._values)
        except StopIteration:
            pass
        return self._last


def _route() -> ModelRoute:
    return ModelRoute(
        route_id="summary", provider="provider", model="small",
        endpoint="https://provider.example/v1/chat", rate_limit_key="provider:small",
        max_concurrency=2, rpm_limit=30, tpm_limit=10_000, timeout_seconds=5,
        permit_ttl_seconds=10, settle_margin_seconds=1,
        price_unit="usd_per_1k_tokens", input_price=1, output_price=2,
    )


def _run_session() -> tuple[object, LeaseContext]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(
        run_id="run-1", agent_id="agent", agent_version="1", package_digest="digest",
        contract_version="1", business_type="memoir", business_id="business", status="pending",
        dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller",
        tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace", execution_attempt=1,
        lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(minutes=1),
        capability_snapshot_json={"allowed_model_route_ids": ["summary"]},
        run_deadline_at=now + timedelta(days=1),
    ))
    session.add(AgentStep(
        step_id="step-1", run_id="run-1", step_name="summarize", step_type="model",
        status="running", execution_attempt=1, step_attempt=1,
        input_summary={"estimated_input_tokens": 20},
    ))
    session.commit()
    return session, LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=now + timedelta(minutes=1), privacy_version=1, authorization_version=1,
    )


def _gateway(session: object, lease: object, provider: RecordingProvider) -> ModelGateway:
    return ModelGateway(
        ModelRouteRegistry([_route()]), ProviderTrafficController(FakeRedis()),
        ModelUsageService(session), lease, provider, PolicyEngine(session),
    )


def _context(session: object, lease: LeaseContext) -> ModelCallContext:
    return ModelCallContext.from_authoritative(session, "run-1", "step-1", lease)


def test_cancellation_after_acquire_does_not_call_provider() -> None:
    session, lease = _run_session()
    provider = RecordingProvider()

    result = _gateway(session, RevokingLease([False]), provider).call(
        _context(session, lease), "summary", {"private": "must not persist"}
    )

    assert result.status == "aborted_before_send"
    assert provider.calls == 0


def test_draining_guard_rejects_before_policy_permit_usage_or_provider() -> None:
    class DrainingGuard:
        def permits_new_call(self, context: ModelCallContext) -> bool:
            return False

    session, lease = _run_session()
    provider = RecordingProvider()
    redis = FakeRedis()
    gateway = ModelGateway(
        ModelRouteRegistry([_route()]), ProviderTrafficController(redis),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
        call_guard=DrainingGuard(),
    )

    result = gateway.call(_context(session, lease), "summary", {"message": "private"})

    assert result.status == "aborted_before_send"
    assert provider.calls == 0
    assert session.scalar(select(AgentModelUsage)) is None
    assert "acquire" not in redis.operations


def test_draining_guard_after_permit_releases_usage_before_provider() -> None:
    class ChangesAfterPermit:
        def __init__(self) -> None:
            self.calls = 0

        def permits_new_call(self, context: ModelCallContext) -> bool:
            self.calls += 1
            return self.calls == 1

    session, lease = _run_session()
    provider = RecordingProvider()
    redis = FakeRedis()
    gateway = ModelGateway(
        ModelRouteRegistry([_route()]), ProviderTrafficController(redis),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
        call_guard=ChangesAfterPermit(),
    )

    result = gateway.call(_context(session, lease), "summary", {"message": "private"})

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "aborted_before_send"
    assert provider.calls == 0
    assert usage is not None and usage.status == "aborted_before_send"
    assert "acquire" in redis.operations
    assert "settle" in redis.operations


def test_revoked_before_send_marks_usage_aborted_without_cost() -> None:
    session, lease = _run_session()
    provider = RecordingProvider()

    result = _gateway(session, RevokingLease([True, False]), provider).call(
        _context(session, lease), "summary", {"private": "must not persist"}
    )

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "aborted_before_send"
    assert provider.calls == 0
    assert usage is not None and usage.status == "aborted_before_send"
    assert usage.estimated_cost == 0


def test_provider_429_settles_and_shares_retry_after() -> None:
    session, lease = _run_session()
    response = httpx.Response(429, headers={"Retry-After": "12"})
    provider = RecordingProvider(httpx.HTTPStatusError("rate limited", request=httpx.Request("POST", "https://p"), response=response))
    gateway = _gateway(session, RevokingLease([True, True]), provider)

    result = gateway.call(_context(session, lease), "summary", {"message": "private"})

    assert result.status == "rate_limited"
    assert provider.calls == 1
    assert session.scalar(select(AgentModelUsage)).status == "rate_limited"


def test_timeout_is_settled_as_unknown_not_zero_cost() -> None:
    session, lease = _run_session()
    provider = RecordingProvider(httpx.TimeoutException("timeout"))

    result = _gateway(session, RevokingLease([True, True]), provider).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "outcome_unknown"
    assert usage is not None and usage.status == "outcome_unknown"
    assert usage.estimated_cost == usage.reserved_estimated_cost
    assert usage.estimated_cost > 0


@pytest.mark.parametrize(
    "failure",
    [
        httpx.TimeoutException("timeout"),
        httpx.HTTPStatusError(
            "server error", request=httpx.Request("POST", "https://p"), response=httpx.Response(503)
        ),
    ],
    ids=("timeout", "http_5xx"),
)
def test_confirmed_provider_failure_opens_circuit_before_next_provider_call(failure: BaseException) -> None:
    session, lease = _run_session()
    guarded = ModelRoute(**{
        **_route().__dict__, "circuit_failure_threshold": 1, "circuit_open_seconds": 30,
    })
    provider = RecordingProvider(failure)
    gateway = ModelGateway(
        ModelRouteRegistry([guarded]), ProviderTrafficController(FakeRedis()),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
    )

    assert gateway.call(_context(session, lease), "summary", {"message": "private"}).status == "outcome_unknown"
    rejected = gateway.call(_context(session, lease), "summary", {"message": "private"})

    assert rejected.status == "circuit_open"
    assert rejected.retry_after_seconds > 0
    assert provider.calls == 1


def test_open_circuit_preflight_skips_policy_reservation_and_provider() -> None:
    class CountingPolicy(PolicyEngine):
        def __init__(self, session: object) -> None:
            super().__init__(session)
            self.reserve_calls = 0

        def reserve(self, context: ModelCallContext, route: ModelRoute) -> tuple[object, str | None]:
            self.reserve_calls += 1
            return super().reserve(context, route)

    session, lease = _run_session()
    guarded = ModelRoute(**{
        **_route().__dict__, "circuit_failure_threshold": 1, "circuit_open_seconds": 30,
    })
    redis = FakeRedis()
    traffic = ProviderTrafficController(redis)
    provider = RecordingProvider()
    policy = CountingPolicy(session)
    assert traffic.record_circuit_failure(guarded).status == "circuit_opened"
    gateway = ModelGateway(
        ModelRouteRegistry([guarded]), traffic, ModelUsageService(session),
        RevokingLease([True]), provider, policy,
    )

    rejected = gateway.call(_context(session, lease), "summary", {"message": "private"})

    assert rejected.status == "circuit_open"
    assert rejected.retry_after_seconds > 0
    assert provider.calls == 0
    assert policy.reserve_calls == 0
    assert session.scalar(select(AgentModelUsage)) is None


def test_circuit_opened_between_preflight_and_acquire_cancels_reservation() -> None:
    class OpensOnAcquireRedis(FakeRedis):
        def eval(self, script: str, numkeys: int, *args: object) -> list[object]:
            if args[0] == "acquire":
                self.circuit_open_until[str(args[10])] = float(args[4]) + 30
            return super().eval(script, numkeys, *args)

    session, lease = _run_session()
    guarded = ModelRoute(**{
        **_route().__dict__, "circuit_failure_threshold": 1, "circuit_open_seconds": 30,
    })
    provider = RecordingProvider()
    gateway = ModelGateway(
        ModelRouteRegistry([guarded]), ProviderTrafficController(OpensOnAcquireRedis()),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
    )

    rejected = gateway.call(_context(session, lease), "summary", {"message": "private"})

    assert rejected.status == "circuit_open"
    assert provider.calls == 0
    assert session.scalar(select(AgentModelUsage)) is None


def test_gateway_success_resets_circuit_and_429_does_not_open_it() -> None:
    class SequenceProvider(RecordingProvider):
        def __init__(self, responses: list[object]) -> None:
            super().__init__()
            self._responses = iter(responses)

        def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object:
            self.calls += 1
            response = next(self._responses)
            if isinstance(response, BaseException):
                raise response
            return response

    session, lease = _run_session()
    guarded = ModelRoute(**{
        **_route().__dict__, "circuit_failure_threshold": 2, "circuit_open_seconds": 30,
    })
    provider = SequenceProvider([
        httpx.TimeoutException("first"), {"ok": True}, httpx.TimeoutException("second"),
        httpx.HTTPStatusError(
            "rate limited", request=httpx.Request("POST", "https://p"), response=httpx.Response(429)
        ),
        httpx.TimeoutException("third"),
    ])
    gateway = ModelGateway(
        ModelRouteRegistry([guarded]), ProviderTrafficController(FakeRedis()),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
    )

    statuses = [
        gateway.call(_context(session, lease), "summary", {"message": "private"}).status
        for _ in range(5)
    ]

    assert statuses == ["outcome_unknown", "succeeded", "outcome_unknown", "rate_limited", "outcome_unknown"]
    assert gateway.call(_context(session, lease), "summary", {"message": "private"}).status == "circuit_open"
    assert provider.calls == 5


def test_usage_settlement_is_idempotent() -> None:
    session, lease = _run_session()
    usage = ModelUsageService(session).create_running(_context(session, lease), _route(), "permit-1")

    assert ModelUsageService(session).settle(usage, "aborted_before_send") == "settled"
    assert ModelUsageService(session).settle(usage, "outcome_unknown") == "already_settled"
    persisted = session.scalar(select(AgentModelUsage))
    assert persisted is not None and persisted.status == "aborted_before_send"


def test_revocation_between_mark_started_and_http_send_does_not_call_provider() -> None:
    session, lease = _run_session()
    provider = RecordingProvider()

    result = _gateway(session, RevokingLease([True, True, False]), provider).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "aborted_before_send"
    assert provider.calls == 0
    assert usage is not None and usage.status == "aborted_before_send"


def test_expired_request_deadline_does_not_acquire_or_call_provider() -> None:
    session, lease = _run_session()
    provider = RecordingProvider()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.run_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    context = _context(session, lease)

    result = _gateway(session, RevokingLease([True]), provider).call(
        context, "summary", {"message": "private"}
    )

    assert result.status == "aborted_before_send"
    assert provider.calls == 0
    assert session.scalar(select(AgentModelUsage)) is None


def test_success_settles_provider_tokens_with_frozen_route_prices() -> None:
    session, lease = _run_session()
    provider = RecordingProvider({"ok": True, "usage": {"input_tokens": 100, "output_tokens": 50}})

    result = _gateway(session, RevokingLease([True, True, True, True]), provider).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "succeeded"
    assert usage is not None
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.estimated_cost == 0.2


def test_private_context_metadata_is_not_persisted() -> None:
    session, lease = _run_session()
    secret = "prompt body: do not store"
    _gateway(session, RevokingLease([True, True, True, True]), RecordingProvider()).call(
        _context(session, lease), "summary", {"message": secret}
    )

    usage = session.scalar(select(AgentModelUsage))
    assert usage is not None
    assert usage.capability_snapshot_json is None
    assert usage.prompt_id is None
    assert usage.prompt_version is None


def test_context_can_only_be_built_from_authoritative_running_step() -> None:
    session, lease = _run_session()

    with pytest.raises(TypeError):
        ModelCallContext("run-1", "step-forged", 9, lease, 1)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="MODEL_CALL_CONTEXT_UNTRUSTED"):
        ModelCallContext.from_authoritative(session, "run-1", "step-forged", lease)

    assert session.scalar(select(AgentModelUsage)) is None


def test_expired_database_lease_does_not_call_provider() -> None:
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    provider = RecordingProvider()

    result = _gateway(session, LeaseService(session), provider).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    assert (result.status, result.error_code) == (
        "policy_denied", "MODEL_RUN_NOT_EXECUTABLE",
    )
    assert provider.calls == 0


def test_missing_provider_usage_keeps_reserved_cost_and_unset_tokens() -> None:
    session, lease = _run_session()

    result = _gateway(session, RevokingLease([True]), RecordingProvider({"ok": True})).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "succeeded"
    assert usage is not None
    assert usage.input_tokens is None and usage.output_tokens is None
    assert usage.estimated_cost == usage.reserved_estimated_cost


def test_separate_session_cannot_overwrite_settled_usage(tmp_path: Path) -> None:
    database = str(tmp_path / "usage.db")
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    session_a, session_b = sessions(), sessions()
    now = datetime.now(UTC)
    session_a.add(AgentRun(
        run_id="run-atomic", agent_id="agent", agent_version="1", package_digest="digest",
        contract_version="1", business_type="memoir", business_id="business", status="pending",
        dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller",
        tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace", execution_attempt=1,
        lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(minutes=1),
        capability_snapshot_json={"allowed_model_route_ids": ["summary"]},
        run_deadline_at=now - timedelta(seconds=1),
    ))
    session_a.add(AgentStep(
        step_id="step-1", run_id="run-atomic", step_name="summarize", step_type="model",
        status="running", execution_attempt=1, step_attempt=1,
        input_summary={"estimated_input_tokens": 20},
    ))
    session_a.commit()
    lease = LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=now + timedelta(minutes=1), privacy_version=1,
        authorization_version=1,
    )
    usage_id = ModelUsageService(session_a).create_running(
        ModelCallContext.from_authoritative(session_a, "run-atomic", "step-1", lease),
        _route(), "permit-atomic",
    )
    session_a.commit()

    assert ModelUsageService(session_a).settle(usage_id, "aborted_before_send") == "settled"
    session_a.commit()
    assert ModelUsageService(session_b).settle(usage_id, "outcome_unknown") == "already_settled"
    persisted = session_b.scalar(select(AgentModelUsage).where(AgentModelUsage.usage_id == usage_id))
    assert persisted is not None
    assert persisted.status == "aborted_before_send"
    assert persisted.estimated_cost == 0


def test_reconciler_does_not_overwrite_usage_settled_by_another_session(tmp_path: Path) -> None:
    database = str(tmp_path / "reconcile.db")
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    session_a, session_b = sessions(), sessions()
    now = datetime.now(UTC)
    session_a.add(AgentRun(
        run_id="run-reconcile", agent_id="agent", agent_version="1", package_digest="digest",
        contract_version="1", business_type="memoir", business_id="business", status="pending",
        dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller",
        tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace", execution_attempt=1,
        lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(minutes=1),
        capability_snapshot_json={"allowed_model_route_ids": ["summary"]},
        run_deadline_at=now - timedelta(seconds=1),
    ))
    session_a.add(AgentStep(
        step_id="step-1", run_id="run-reconcile", step_name="summarize", step_type="model",
        status="running", execution_attempt=1, step_attempt=1, input_summary={},
    ))
    session_a.commit()
    lease = LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=now + timedelta(minutes=1), privacy_version=1,
        authorization_version=1,
    )
    context = ModelCallContext.from_authoritative(session_a, "run-reconcile", "step-1", lease)
    usage_id = ModelUsageService(session_a).create_running(context, _route(), "permit-reconcile")
    session_a.commit()

    assert ModelUsageService(session_b).settle(usage_id, "succeeded", input_tokens=1, output_tokens=1, route=_route()) == "settled"
    session_b.commit()
    assert ModelUsageService(session_a).mark_expired_running_unknown(now) == 0
    session_a.commit()
    persisted = session_a.scalar(select(AgentModelUsage).where(AgentModelUsage.usage_id == usage_id))
    assert persisted is not None and persisted.status == "succeeded"


def test_usage_creation_failure_releases_acquired_permit() -> None:
    session, lease = _run_session()
    redis = FakeRedis()
    traffic = ProviderTrafficController(redis)

    class FailingUsage:
        def activate_reservation(self, *args: object) -> bool:
            return False

    gateway = ModelGateway(
        ModelRouteRegistry([_route()]), traffic, FailingUsage(), RevokingLease([True]), RecordingProvider(), PolicyEngine(session)
    )
    result = gateway.call(_context(session, lease), "summary", {"message": "private"})

    assert result.status == "aborted_before_send"
    assert traffic.acquire(_route(), "permit-after-failure").granted


def test_route_not_frozen_in_run_capability_is_rejected_before_acquire() -> None:
    session, lease = _run_session()
    allowed = _route()
    forbidden = ModelRoute(**{**allowed.__dict__, "route_id": "other"})
    redis = FakeRedis()
    provider = RecordingProvider()
    gateway = ModelGateway(
        ModelRouteRegistry([allowed, forbidden]), ProviderTrafficController(redis),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
    )

    result = gateway.call(_context(session, lease), "other", {"message": "private"})

    assert result.status == "route_not_allowed"
    assert provider.calls == 0
    assert redis.permits == {}
    assert session.scalar(select(AgentModelUsage)) is None


def test_call_limit_denial_happens_before_permit_usage_and_provider() -> None:
    """已耗尽的冻结调用额度不得再占用 permit 或向 Provider 发送请求。"""
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.capability_snapshot_json = {
        "allowed_model_route_ids": ["summary"],
        "model_policy": {"max_model_calls": 1},
    }
    ModelUsageService(session).create_running(_context(session, lease), _route(), "existing")
    session.commit()
    provider = RecordingProvider()

    result = _gateway(session, RevokingLease([True]), provider).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    assert (result.status, result.error_code) == (
        "policy_denied", "MODEL_CALL_LIMIT_EXCEEDED",
    )
    assert provider.calls == 0
    assert len(session.scalars(select(AgentModelUsage)).all()) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda run: setattr(run, "cancel_requested_at", datetime.now(UTC)),
        lambda run: setattr(run, "privacy_state", "purge_requested"),
        lambda run: setattr(run, "authorization_version", run.authorization_version + 1),
        lambda run: setattr(run, "fencing_token", run.fencing_token + 1),
    ],
    ids=("cancel_requested", "privacy_not_active", "authorization_changed", "fencing_changed"),
)
def test_non_executable_run_is_policy_denied_before_traffic_usage_and_provider(change: object) -> None:
    """构造 Context 后 Run 失效时，准入必须在所有外部副作用之前拒绝。"""
    session, lease = _run_session()
    context = _context(session, lease)
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    change(run)  # type: ignore[operator]
    session.commit()
    provider = RecordingProvider()
    traffic = ProviderTrafficController(FakeRedis())
    acquire_calls = 0
    original_acquire = traffic.acquire

    def count_acquire(*args: object, **kwargs: object) -> object:
        nonlocal acquire_calls
        acquire_calls += 1
        return original_acquire(*args, **kwargs)

    traffic.acquire = count_acquire  # type: ignore[method-assign]
    gateway = ModelGateway(
        ModelRouteRegistry([_route()]), traffic, ModelUsageService(session),
        RevokingLease([True]), provider, PolicyEngine(session),
    )

    result = gateway.call(context, "summary", {"message": "private"})

    assert (result.status, result.error_code) == (
        "policy_denied", "MODEL_RUN_NOT_EXECUTABLE",
    )
    assert acquire_calls == 0
    assert provider.calls == 0
    assert session.scalar(select(AgentModelUsage)) is None


def test_policy_denial_does_not_acquire_traffic_permit() -> None:
    """已耗尽额度的拒绝不得调用 traffic.acquire。"""
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.capability_snapshot_json = {
        "allowed_model_route_ids": ["summary"],
        "model_policy": {"max_model_calls": 0},
    }
    session.commit()
    traffic = ProviderTrafficController(FakeRedis())
    acquire_calls = 0
    original_acquire = traffic.acquire

    def count_acquire(*args: object, **kwargs: object) -> object:
        nonlocal acquire_calls
        acquire_calls += 1
        return original_acquire(*args, **kwargs)

    traffic.acquire = count_acquire  # type: ignore[method-assign]
    result = ModelGateway(
        ModelRouteRegistry([_route()]), traffic, ModelUsageService(session),
        RevokingLease([True]), RecordingProvider(), PolicyEngine(session),
    ).call(_context(session, lease), "summary", {"message": "private"})

    assert (result.status, result.error_code) == (
        "policy_denied", "MODEL_CALL_LIMIT_EXCEEDED",
    )
    assert acquire_calls == 0


def test_second_session_cannot_pass_call_limit_after_first_reserves_budget(tmp_path: Path) -> None:
    """独立 Session 的后到调用必须看到先到调用已占用的同 Run 预算。"""
    database = str(tmp_path / "policy-budget.db")
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    session_a, session_b = sessions(), sessions()
    now = datetime.now(UTC)
    session_a.add(AgentRun(
        run_id="run-budget", agent_id="agent", agent_version="1", package_digest="digest",
        contract_version="1", business_type="memoir", business_id="business", status="pending",
        dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller",
        tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace", execution_attempt=1,
        lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(minutes=1),
        capability_snapshot_json={
            "allowed_model_route_ids": ["summary"], "model_policy": {"max_model_calls": 1},
        }, run_deadline_at=now + timedelta(days=1),
    ))
    session_a.add(AgentStep(
        step_id="step-budget", run_id="run-budget", step_name="summarize", step_type="model",
        status="running", execution_attempt=1, step_attempt=1,
        input_summary={"estimated_input_tokens": 20},
    ))
    session_a.commit()
    lease = LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=now + timedelta(minutes=1), privacy_version=1, authorization_version=1,
    )
    first = _gateway(session_a, RevokingLease([True, True, True]), RecordingProvider())
    assert first.call(
        ModelCallContext.from_authoritative(session_a, "run-budget", "step-budget", lease),
        "summary", {"message": "private"},
    ).status == "succeeded"
    session_a.commit()
    traffic_b = ProviderTrafficController(FakeRedis())
    acquire_calls = 0
    original_acquire = traffic_b.acquire

    def count_acquire(*args: object, **kwargs: object) -> object:
        nonlocal acquire_calls
        acquire_calls += 1
        return original_acquire(*args, **kwargs)

    traffic_b.acquire = count_acquire  # type: ignore[method-assign]
    second = ModelGateway(
        ModelRouteRegistry([_route()]), traffic_b, ModelUsageService(session_b),
        RevokingLease([True]), RecordingProvider(), PolicyEngine(session_b),
    ).call(
        ModelCallContext.from_authoritative(session_b, "run-budget", "step-budget", lease),
        "summary", {"message": "private"},
    )

    assert (second.status, second.error_code) == (
        "policy_denied", "MODEL_CALL_LIMIT_EXCEEDED",
    )
    assert acquire_calls == 0


@pytest.mark.parametrize(
    ("model_policy", "expected_code"),
    [
        ({"max_model_calls": 1}, "MODEL_CALL_LIMIT_EXCEEDED"),
        ({"max_model_cost": 0.03}, "MODEL_COST_LIMIT_EXCEEDED"),
    ],
    ids=("call_limit", "cost_limit"),
)
def test_concurrent_sessions_only_one_call_acquires_traffic_for_one_run_budget(
    tmp_path: Path, model_policy: dict[str, float | int], expected_code: str,
) -> None:
    """先到调用未返回 Provider 时，另一 Session 也不得越过同 Run 的次数或成本额度。"""
    database = str(tmp_path / "policy-concurrent-budget.db")
    engine = create_engine(f"sqlite:///{database}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    setup = sessions()
    now = datetime.now(UTC)
    setup.add(AgentRun(
        run_id="run-concurrent", agent_id="agent", agent_version="1", package_digest="digest",
        contract_version="1", business_type="memoir", business_id="business", status="pending",
        dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller",
        tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace", execution_attempt=1,
        lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(minutes=1),
        capability_snapshot_json={
            "allowed_model_route_ids": ["summary"], "model_policy": model_policy,
        }, run_deadline_at=now + timedelta(days=1),
    ))
    setup.add(AgentStep(
        step_id="step-concurrent", run_id="run-concurrent", step_name="summarize", step_type="model",
        status="running", execution_attempt=1, step_attempt=1,
        input_summary={"estimated_input_tokens": 20},
    ))
    setup.commit()
    setup.close()
    lease = LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=now + timedelta(minutes=1), privacy_version=1, authorization_version=1,
    )
    first_sent, release_first, second_acquired = Event(), Event(), Event()

    class BlockingProvider(RecordingProvider):
        def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object:
            self.calls += 1
            first_sent.set()
            assert release_first.wait(3)
            return {"ok": True}

    provider = BlockingProvider()
    traffic = ProviderTrafficController(FakeRedis())
    acquire_calls = 0
    original_acquire = traffic.acquire

    def count_acquire(*args: object, **kwargs: object) -> object:
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls == 2:
            second_acquired.set()
        return original_acquire(*args, **kwargs)

    traffic.acquire = count_acquire  # type: ignore[method-assign]
    results: list[object] = []

    def invoke() -> None:
        session = sessions()
        try:
            context = ModelCallContext.from_authoritative(
                session, "run-concurrent", "step-concurrent", lease,
            )
            results.append(ModelGateway(
                ModelRouteRegistry([_route()]), traffic, ModelUsageService(session),
                RevokingLease([True, True, True]), provider, PolicyEngine(session),
            ).call(context, "summary", {"message": "private"}))
            session.commit()
        finally:
            session.close()

    first, second = Thread(target=invoke), Thread(target=invoke)
    first.start()
    assert first_sent.wait(3)
    second.start()
    try:
        assert not second_acquired.wait(0.5)
    finally:
        release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert acquire_calls == 1
    assert provider.calls == 1
    denied = [result for result in results if getattr(result, "status", None) == "policy_denied"]
    assert len(denied) == 1
    assert denied[0].error_code == expected_code


def test_cost_limit_counts_unknown_reserved_cost_and_current_input_reservation() -> None:
    """未知结果仍占保守成本，当前调用也必须先预留输入成本。"""
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.capability_snapshot_json = {
        "allowed_model_route_ids": ["summary"],
        "model_policy": {"max_model_cost": 1.0},
    }
    session.add(AgentModelUsage(
        id=101, usage_id="unknown-usage", run_id="run-1", step_id="step-1",
        execution_attempt=1, model_attempt=1, status="outcome_unknown",
        reserved_estimated_cost=0.99, estimated_cost=None,
    ))
    session.commit()
    provider = RecordingProvider()

    result = _gateway(session, RevokingLease([True]), provider).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    assert (result.status, result.error_code) == (
        "policy_denied", "MODEL_COST_LIMIT_EXCEEDED",
    )
    assert provider.calls == 0
    assert len(session.scalars(select(AgentModelUsage)).all()) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda step: setattr(step, "status", "succeeded"),
        lambda step: setattr(step, "execution_attempt", step.execution_attempt + 1),
        lambda step: setattr(step, "step_attempt", step.step_attempt + 1),
    ],
    ids=("step_not_running", "execution_attempt_changed", "model_attempt_changed"),
)
def test_stale_step_context_cannot_reserve_or_acquire(change: object) -> None:
    """Context 构造后 Step 改变时，预留与 permit 都必须保持零副作用。"""
    session, lease = _run_session()
    context = _context(session, lease)
    step = session.scalar(select(AgentStep).where(AgentStep.step_id == "step-1"))
    assert step is not None
    change(step)  # type: ignore[operator]
    session.commit()
    redis = FakeRedis()
    provider = RecordingProvider()

    result = ModelGateway(
        ModelRouteRegistry([_route()]), ProviderTrafficController(redis),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
    ).call(context, "summary", {"private": "must not persist"})

    assert (result.status, result.error_code) == (
        "policy_denied", "MODEL_RUN_NOT_EXECUTABLE",
    )
    assert session.scalar(select(AgentModelUsage)) is None
    assert "acquire" not in redis.operations
    assert provider.calls == 0


def test_step_revoked_after_reservation_cannot_reach_provider() -> None:
    """HTTP 紧邻检查必须重新验证权威 Step，而非只相信 lease mock。"""
    session, lease = _run_session()
    provider = RecordingProvider()

    class RevokingUsage(ModelUsageService):
        def activate_reservation(self, usage_id: str, permit_id: str) -> bool:
            activated = super().activate_reservation(usage_id, permit_id)
            step = self._session.scalar(select(AgentStep).where(AgentStep.step_id == "step-1"))
            assert step is not None
            step.status = "succeeded"
            self._session.commit()
            return activated

    result = ModelGateway(
        ModelRouteRegistry([_route()]), ProviderTrafficController(FakeRedis()),
        RevokingUsage(session), RevokingLease([True]), provider, PolicyEngine(session),
    ).call(_context(session, lease), "summary", {"private": "must not persist"})

    assert result.status == "aborted_before_send"
    assert provider.calls == 0


def test_model_governance_denial_logs_exclude_request_body(caplog: pytest.LogCaptureFixture) -> None:
    """policy、熔断与 draining 的拒绝日志只含安全标识，不能回显请求正文。"""
    caplog.set_level(logging.INFO)
    request = {"prompt": "private-body-987", "token": "secret-token-456"}

    policy_session, policy_lease = _run_session()
    policy_run = policy_session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert policy_run is not None
    policy_run.capability_snapshot_json = {
        "allowed_model_route_ids": ["summary"], "model_policy": {"max_model_calls": 0},
    }
    policy_session.commit()
    assert _gateway(policy_session, RevokingLease([True]), RecordingProvider()).call(
        _context(policy_session, policy_lease), "summary", request,
    ).status == "policy_denied"

    circuit_session, circuit_lease = _run_session()
    guarded = ModelRoute(**{
        **_route().__dict__, "circuit_failure_threshold": 1, "circuit_open_seconds": 30,
    })
    traffic = ProviderTrafficController(FakeRedis())
    assert traffic.record_circuit_failure(guarded).status == "circuit_opened"
    assert ModelGateway(
        ModelRouteRegistry([guarded]), traffic, ModelUsageService(circuit_session),
        RevokingLease([True]), RecordingProvider(), PolicyEngine(circuit_session),
    ).call(_context(circuit_session, circuit_lease), "summary", request).status == "circuit_open"

    class DrainingGuard:
        def permits_new_call(self, context: ModelCallContext) -> bool:
            return False

    drain_session, drain_lease = _run_session()
    assert ModelGateway(
        ModelRouteRegistry([_route()]), ProviderTrafficController(FakeRedis()),
        ModelUsageService(drain_session), RevokingLease([True]), RecordingProvider(),
        PolicyEngine(drain_session), call_guard=DrainingGuard(),
    ).call(_context(drain_session, drain_lease), "summary", request).status == "aborted_before_send"

    assert "模型策略拒绝" in caplog.text
    assert "模型熔断拒绝" in caplog.text
    assert "worker_draining" in caplog.text
    assert "private-body-987" not in caplog.text
    assert "secret-token-456" not in caplog.text


def test_created_run_freezes_server_routes_and_rejects_command_route_override() -> None:
    session, _ = _run_session()
    session.add(AgentDefinition(
        agent_id="created-agent", version="1", runtime_type="workflow",
        definition_json={"allowed_business_types": ["memoir"], "workflow_nodes": []},
        package_digest="digest", contract_version="1", status="active",
        status_changed_at=datetime.now(UTC), status_changed_by="test", status_change_reason="fixture",
    ))
    created = AgentRunService(session, trusted_model_route_ids=("summary",)).create(
        CreateRunCommand(
            agent_id="created-agent", agent_version="1", business_type="memoir",
            business_id="business", start_mode="auto",
            input={"allowed_model_route_ids": ["other"]}, callback_target_id="callback",
            business_connector_id="connector",
        ), "caller", "tenant", "create-server-routes",
    )
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == created.run_id))
    assert run is not None
    assert run.capability_snapshot_json is not None
    assert run.capability_snapshot_json["allowed_model_route_ids"] == ["summary"]
    run.dispatch_state = "claimed"
    run.execution_attempt = 1
    run.lease_owner = "worker"
    run.fencing_token = 1
    run.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    session.add(AgentStep(
        step_id="created-step", run_id=run.run_id, step_name="model", step_type="model",
        status="running", execution_attempt=1, step_attempt=1, input_summary={},
    ))
    session.commit()
    lease = LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1), privacy_version=1,
        authorization_version=1,
    )
    context = ModelCallContext.from_authoritative(session, run.run_id, "created-step", lease)
    allowed = _route()
    forbidden = ModelRoute(**{**allowed.__dict__, "route_id": "other"})
    redis = FakeRedis()
    provider = RecordingProvider()
    gateway = ModelGateway(
        ModelRouteRegistry([allowed, forbidden]), ProviderTrafficController(redis),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
    )

    assert gateway.call(context, "summary", {"message": "private"}).status == "succeeded"
    assert gateway.call(context, "other", {"message": "private"}).status == "route_not_allowed"
    assert provider.calls == 1
