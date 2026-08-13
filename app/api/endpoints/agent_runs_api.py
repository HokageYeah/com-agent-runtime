"""AgentRun 生命周期 HTTP API；所有输出均为安全摘要。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.contracts.api import (
    CancelAgentRunRequest,
    PurgePrivateDataRequest,
    RetryAgentRunRequest,
    StartAgentRunRequest,
    StepSummary,
)
from app.core.authorization import AuthorizationError, AuthorizationService
from app.core.connectors import ConnectorRegistry, ConnectorValidationError
from app.core.security import (
    SignatureError,
    assert_single_service_headers,
    request_hash,
    verify_signature,
)
from app.schemas.agent_run import (
    ApprovalCommand,
    CreateRunCommand,
    RunDetail,
    RunSummary,
)
from app.services.admission_service import AdmissionLimits, AdmissionRejected
from app.services.agent_run_service import AgentRunService, AgentRunServiceError
from app.services.idempotency_service import IdempotencyConflict, IdempotencyService

router = APIRouter(prefix="/agent-runs", tags=["agent-runs"])


def _admission_limits(request: Request) -> AdmissionLimits:
    settings = request.app.state.settings
    return AdmissionLimits(
        max_held=settings.admission_max_held,
        max_queued=settings.admission_max_queued,
        max_running=settings.admission_max_running,
    )


async def _caller(request: Request, write: bool) -> tuple[str, str, bytes]:
    """所有调用统一验签；body 取原始 bytes，防止重序列化导致签名漂移。"""
    body = await request.body()
    try:
        # 重复服务认证头检测与验签必须在同一 try 内：重复头也要被映射为 401，
        # 而不是穿透成 500。检测在压平前完成，dict 推导会抹掉同名头基数。
        assert_single_service_headers(request.headers)
        headers = {key.lower(): value for key, value in request.headers.items()}
        client_id = verify_signature(
            headers,
            request.method,
            request.url.path,
            body,
            request.app.state.settings.trusted_clients,
            request.app.state.settings.signature_tolerance_seconds,
        )
    except SignatureError as exc:
        raise HTTPException(
            status_code=401, detail="invalid service signature"
        ) from exc
    client = request.app.state.settings.trusted_clients[client_id]
    if write and not request.headers.get("Idempotency-Key"):
        raise HTTPException(status_code=400, detail="Idempotency-Key required")
    return client_id, client.get("tenant_id", client_id), body


def _replay(
    request: Request, client: str, scope: str, body: bytes
) -> dict[str, Any] | None:
    """查询已有安全响应；不同 request hash 立即拒绝，避免 key 被错误复用。"""
    session = request.app.state.session_factory()
    try:
        return IdempotencyService(session).replay(
            client,
            scope,
            request.headers["Idempotency-Key"],
            request_hash(request.method, request.url.path, body),
        )
    finally:
        session.close()


def _write_result(
    request: Request,
    client: str,
    scope: str,
    body: bytes,
    result: RunSummary | RunDetail,
) -> RunSummary | RunDetail:
    """统一持久化写响应，避免每个 HTTP 动作产生不同的幂等语义。"""
    session = request.app.state.session_factory()
    try:
        IdempotencyService(session).store(
            client,
            scope,
            request.headers["Idempotency-Key"],
            request_hash(request.method, request.url.path, body),
            result.model_dump(mode="json"),
            result.run_id,
        )
        session.commit()
        return result
    finally:
        session.close()


def _existing_write(
    request: Request, client: str, scope: str, body: bytes
) -> RunSummary | RunDetail | None:
    try:
        payload = _replay(request, client, scope, body)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT") from exc
    if payload is None:
        return None
    return (
        RunDetail.model_validate(payload)
        if "privacy_state" in payload
        else RunSummary.model_validate(payload)
    )


@router.post("", response_model=RunSummary, status_code=status.HTTP_201_CREATED)
async def create_run(request: Request, command: CreateRunCommand) -> RunSummary:
    caller, tenant, body = await _caller(request, write=True)
    try:
        replay = _replay(request, caller, "create", body)
        if replay:
            return RunSummary.model_validate(replay)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT") from exc
    session = request.app.state.session_factory()
    try:
        authorization = AuthorizationService(request.app.state.settings.trusted_clients)
        authorization.authorize_create(
            client_id=caller,
            agent_id=command.agent_id,
            business_type=command.business_type,
            callback_target_id=command.callback_target_id,
            connector_id=command.business_connector_id,
            data_domain=command.data_domain,
        )
        ConnectorRegistry(request.app.state.settings.business_connectors).require_enabled(
            command.business_connector_id
        )
        result = AgentRunService(
            session,
            _admission_limits(request),
            trusted_model_route_ids=(route.route_id for route in request.app.state.settings.model_routes),
            required_model_data_residency=authorization.model_data_residency(caller),
        ).create(
            command, caller, tenant, request.headers["Idempotency-Key"],
            authorization_version=authorization.authorization_version(caller),
        )
        IdempotencyService(session).store(
            caller,
            "create",
            request.headers["Idempotency-Key"],
            request_hash(request.method, request.url.path, body),
            result.model_dump(mode="json"),
            result.run_id,
        )
        session.commit()
        return result
    except AgentRunServiceError as exc:
        logging.warning("AgentRun create 被拒绝 reason=%s", exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (AuthorizationError, ConnectorValidationError) as exc:
        session.rollback()
        logging.warning("AgentRun create 授权拒绝 reason=%s", exc)
        raise HTTPException(status_code=403, detail="run authorization denied") from exc
    except AdmissionRejected as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="RUNTIME_OVERLOADED",
            headers={"Retry-After": "5"},
        ) from exc
    finally:
        session.close()


@router.post("/{run_id}/start", response_model=RunSummary)
async def start_run(
    run_id: str, body: StartAgentRunRequest, request: Request
) -> RunSummary:
    # raw_body 是验签用的原始 bytes；body 是 FastAPI 解析好的 Pydantic 模型，
    # 二者不能同名，否则下面 body.expected_status_version 会访问 bytes 属性而崩溃。
    caller, _, raw_body = await _caller(request, write=True)
    existing = _existing_write(request, caller, "start", raw_body)
    if existing:
        return RunSummary.model_validate(existing)
    session = request.app.state.session_factory()
    try:
        result = AgentRunService(
            session,
            _admission_limits(request),
            authorization_version_resolver=lambda run: AuthorizationService(
                request.app.state.settings.trusted_clients
            ).authorization_version(run.caller_id),
        ).start(
            run_id,
            caller,
            request.headers["Idempotency-Key"],
            body.expected_status_version,
        )
        IdempotencyService(session).store(
            caller, "start", request.headers["Idempotency-Key"],
            request_hash(request.method, request.url.path, raw_body),
            result.model_dump(mode="json"), result.run_id,
        )
        session.commit()
        return result
    except AgentRunServiceError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        session.close()


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(run_id: str, request: Request) -> RunDetail:
    caller, _, _ = await _caller(request, write=False)
    session = request.app.state.session_factory()
    try:
        auditor = AuthorizationService(
            request.app.state.settings.trusted_clients
        ).can_audit(caller)
        return AgentRunService(session).get(run_id, caller, allow_auditor=auditor)
    except AgentRunServiceError as exc:
        raise HTTPException(status_code=404, detail="run unavailable") from exc
    finally:
        session.close()


@router.get("/{run_id}/steps", response_model=list[StepSummary])
async def get_run_steps(run_id: str, request: Request) -> list[StepSummary]:
    caller, _, _ = await _caller(request, write=False)
    session = request.app.state.session_factory()
    try:
        auditor = AuthorizationService(
            request.app.state.settings.trusted_clients
        ).can_audit(caller)
        return AgentRunService(session).steps(run_id, caller, allow_auditor=auditor)
    except AgentRunServiceError as exc:
        raise HTTPException(status_code=404, detail="run unavailable") from exc
    finally:
        session.close()


@router.post("/{run_id}/cancel", response_model=RunSummary)
async def cancel_run(
    run_id: str, body: CancelAgentRunRequest, request: Request
) -> RunSummary:
    # raw_body 是验签用的原始 bytes；body 是 Pydantic 模型，二者不能同名。
    caller, _, raw_body = await _caller(request, write=True)
    existing = _existing_write(request, caller, "cancel", raw_body)
    if existing:
        return RunSummary.model_validate(existing)
    session = request.app.state.session_factory()
    try:
        result = AgentRunService(session).cancel(run_id, caller, body.reason_code)
        IdempotencyService(session).store(
            caller, "cancel", request.headers["Idempotency-Key"],
            request_hash(request.method, request.url.path, raw_body),
            result.model_dump(mode="json"), result.run_id,
        )
        session.commit()
        return result
    finally:
        session.close()


@router.post("/{run_id}/retry", response_model=RunSummary)
async def retry_run(
    run_id: str, body: RetryAgentRunRequest, request: Request
) -> RunSummary:
    # raw_body 是验签用的原始 bytes；body 是 Pydantic 模型，二者不能同名。
    caller, _, raw_body = await _caller(request, write=True)
    existing = _existing_write(request, caller, "retry", raw_body)
    if existing:
        return RunSummary.model_validate(existing)
    session = request.app.state.session_factory()
    try:
        auditor = AuthorizationService(
            request.app.state.settings.trusted_clients
        ).can_audit(caller)
        result = AgentRunService(
            session,
            _admission_limits(request),
            authorization_version_resolver=lambda run: AuthorizationService(
                request.app.state.settings.trusted_clients
            ).authorization_version(run.caller_id),
        ).retry(
            run_id,
            caller,
            allow_auditor=auditor,
            expected_status_version=body.expected_status_version,
        )
        IdempotencyService(session).store(
            caller, "retry", request.headers["Idempotency-Key"],
            request_hash(request.method, request.url.path, raw_body),
            result.model_dump(mode="json"), result.run_id,
        )
        session.commit()
        return result
    except AgentRunServiceError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        session.close()


@router.post("/{run_id}/human-approval", response_model=RunSummary)
async def approve_run(
    run_id: str, body: ApprovalCommand, request: Request
) -> RunSummary:
    caller, _, raw_body = await _caller(request, write=True)
    existing = _existing_write(request, caller, "human_approval", raw_body)
    if existing:
        return RunSummary.model_validate(existing)
    session = request.app.state.session_factory()
    try:
        result = AgentRunService(session, _admission_limits(request)).approve(
            run_id, caller, body.decision, body.expected_status_version
        )
        IdempotencyService(session).store(
            caller, "human_approval", request.headers["Idempotency-Key"],
            request_hash(request.method, request.url.path, raw_body),
            result.model_dump(mode="json"), result.run_id,
        )
        session.commit()
        return result
    except AgentRunServiceError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        session.close()


@router.post("/{run_id}/purge-private-data", response_model=RunDetail, status_code=202)
async def purge_run(
    run_id: str, body: PurgePrivateDataRequest, request: Request
) -> RunDetail:
    # raw_body 是验签用的原始 bytes；body 是 Pydantic 模型，二者不能同名。
    # 否则下面 body.reason_code 会访问 bytes 属性而崩溃（B8 孤儿补偿依赖 purge）。
    caller, _, raw_body = await _caller(request, write=True)
    existing = _existing_write(request, caller, "purge", raw_body)
    if existing:
        return RunDetail.model_validate(existing)
    session = request.app.state.session_factory()
    try:
        result = AgentRunService(session).purge(run_id, caller, body.reason_code)
        IdempotencyService(session).store(
            caller, "purge", request.headers["Idempotency-Key"],
            request_hash(request.method, request.url.path, raw_body),
            result.model_dump(mode="json"), result.run_id, ttl_days=3650,
        )
        session.commit()
        return result
    finally:
        session.close()
