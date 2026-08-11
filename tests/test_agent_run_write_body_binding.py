"""D-1 回归保护：验证 /start /cancel /retry /purge 路由不会把 Pydantic body 重绑为原始 bytes。

背景：
- _caller 返回 tuple[str, str, bytes]，第三项是 await request.body() 的原始 bytes，
  专门用于 HMAC 验签与 request_hash 计算。
- start_run / cancel_run / retry_run / purge_run 历史上写 `caller, _, body = await _caller(...)`，
  把 FastAPI 已解析好的 Pydantic 形参 body 覆盖为 bytes；随后访问
  body.expected_status_version / body.reason_code 必然 AttributeError，真实路由必然 500。
- approve_run（human_approval）已用 raw_body 正确写法，不在回归范围。
- 唯一打这些路由的 test_runtime_postgres_harness.py 全部 skip（除非显式提供 DSN），
  所以 bug 在 CI 长期潜伏。purge 在 B8 孤儿补偿路径上，必须随修随测。
- 本测试不依赖 PostgreSQL，也不连真实 DB / HMAC：直接 await 路由函数，patch _caller
  与 _existing_write，用 Fake AgentRunService 捕获 expected_status_version / reason_code，
  断言请求体里的字段被正确传给服务层。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.api.endpoints import agent_runs_api
from app.contracts.api import (
    CancelAgentRunRequest,
    PurgePrivateDataRequest,
    RetryAgentRunRequest,
    StartAgentRunRequest,
)
from app.schemas.agent_run import RunSummary

# 路由内部不关心 RunSummary 字段含义，仅用于满足 model_dump / run_id 调用；
# 不进入断言文本，避免把任何模型原输出写进测试。
_RUN_SUMMARY = RunSummary(
    run_id="r1",
    business_id="b1",
    status="running",
    dispatch_state="queued",
    contract_version="1.0.0",
    package_digest="sha256:test",
    authorization_version=1,
    status_version=1,
)


def _make_request(path: str, idempotency_key: str = "k1") -> Request:
    """构造带最小 settings 的 Request；不进入真实 HMAC / DB 路径。

    trusted_clients 必须真实可读：retry_run 内部会调用
    AuthorizationService(trusted_clients).can_audit(caller)。
    """
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        trusted_clients={"couple-diary": {"tenant_id": "couple-diary"}},
        signature_tolerance_seconds=30,
        admission_max_held=10,
        admission_max_queued=10,
        admission_max_running=10,
    )
    # session_factory 仅在路由尾部被调用一次以执行幂等写存储；用 Mock 即可跳过真实 DB。
    app.state.session_factory = MagicMock()
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "headers": [(b"idempotency-key", idempotency_key.encode())],
        "query_string": b"",
        "app": app,
    }
    return Request(scope)


class _FakeAgentRunService:
    """捕获 start / cancel / retry 收到的关键字段，证明 body 未被 bytes 遮蔽。"""

    captured: dict[str, object] = {}

    def __init__(self, *args: object, **kwargs: object) -> None:
        # 路由会传 session / admission_limits / authorization_version_resolver 等，
        # Fake 一律忽略，专注断言目标字段。
        pass

    def start(
        self,
        run_id: str,
        caller_id: str,
        idempotency_key: str,
        expected_status_version: int | None = None,
    ) -> RunSummary:
        self.captured = {"expected_status_version": expected_status_version}
        return _RUN_SUMMARY

    def cancel(
        self,
        run_id: str,
        caller_id: str,
        reason_code: str = "SYSTEM_REQUEST",
    ) -> RunSummary:
        self.captured = {"reason_code": reason_code}
        return _RUN_SUMMARY

    def retry(
        self,
        run_id: str,
        caller_id: str,
        allow_auditor: bool = False,
        expected_status_version: int | None = None,
    ) -> RunSummary:
        self.captured = {"expected_status_version": expected_status_version}
        return _RUN_SUMMARY

    def purge(
        self,
        run_id: str,
        caller_id: str,
        reason_code: str = "SYSTEM_REQUEST",
    ) -> RunSummary:
        # purge 路由 response_model=RunDetail，但本测试只断言 captured 字段，
        # 返回 RunSummary 已能满足 model_dump / run_id 调用，无需构造完整 RunDetail。
        self.captured = {"reason_code": reason_code}
        return _RUN_SUMMARY


async def _fake_caller(request: Request, write: bool = True) -> tuple[str, str, bytes]:
    """跳过 HMAC；返回 bytes 模拟真实 _caller 第三项，触发路由内部 body 重绑 bug。

    返回 b"{}" 与真实 await request.body() 在类型上完全一致（都是 bytes），
    都会让 `caller, _, body = await _caller(...)` 中的 body 变成 bytes。
    """
    return "couple-diary", "couple-diary", b"{}"


def _install_patches(monkeypatch: pytest.MonkeyPatch) -> _FakeAgentRunService:
    """统一 patch：_caller 跳过 HMAC、_existing_write 跳过幂等重放、AgentRunService 替换为 Fake。"""
    fake = _FakeAgentRunService()
    monkeypatch.setattr(agent_runs_api, "_caller", _fake_caller)
    monkeypatch.setattr(agent_runs_api, "_existing_write", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent_runs_api, "AgentRunService", lambda *args, **kwargs: fake)
    return fake


def test_start_run_passes_expected_status_version_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start_run 必须把请求体里的 expected_status_version 透传给 AgentRunService.start。"""
    fake = _install_patches(monkeypatch)
    request = _make_request("/api/v1/runtime/agent-runs/r1/start")
    body = StartAgentRunRequest(expected_status_version=5)

    asyncio.run(agent_runs_api.start_run("r1", body, request))

    assert fake.captured == {"expected_status_version": 5}


def test_cancel_run_passes_reason_code_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel_run 必须把请求体里的 reason_code 透传给 AgentRunService.cancel。"""
    fake = _install_patches(monkeypatch)
    request = _make_request("/api/v1/runtime/agent-runs/r1/cancel")
    body = CancelAgentRunRequest(reason_code="USER_CANCEL")

    asyncio.run(agent_runs_api.cancel_run("r1", body, request))

    assert fake.captured == {"reason_code": "USER_CANCEL"}


def test_retry_run_passes_expected_status_version_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retry_run 必须把请求体里的 expected_status_version 透传给 AgentRunService.retry。"""
    fake = _install_patches(monkeypatch)
    request = _make_request("/api/v1/runtime/agent-runs/r1/retry")
    body = RetryAgentRunRequest(expected_status_version=7)

    asyncio.run(agent_runs_api.retry_run("r1", body, request))

    assert fake.captured == {"expected_status_version": 7}


def test_purge_run_passes_reason_code_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """purge_run 必须把请求体里的 reason_code 透传给 AgentRunService.purge。

    purge 在 B8 孤儿补偿路径上：绑定失败的 held Run 必须 cancel/purge 并对账，
    若 reason_code 被原始 bytes 遮蔽，整条补偿路径会以 500 失败。
    """
    fake = _install_patches(monkeypatch)
    request = _make_request("/api/v1/runtime/agent-runs/r1/purge-private-data")
    body = PurgePrivateDataRequest(reason_code="PRIVACY_PURGE")

    asyncio.run(agent_runs_api.purge_run("r1", body, request))

    assert fake.captured == {"reason_code": "PRIVACY_PURGE"}
