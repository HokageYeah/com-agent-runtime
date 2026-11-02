"""SOOTHE 专属装配：缺配置明确失败，沿用公共模型、工具与检查点安全边界。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.authorization import AuthorizationService
from app.core.config import settings
from app.models import AgentRun
from app.runtime.artifact import ArtifactStore
from app.runtime.executor import WorkflowExecutor
from app.runtime.interfaces import AgentRunResult, LeaseContext, RunExecutor
from app.runtime.test_harness import RuntimeDependencies
from app.services.tool_call_audit_service import ToolCallAuditService
from app.worker import build_checkpoint_store, build_model_gateway, build_tool_gateway


class _SootheNotConfiguredExecutor:
    """SOOTHE 缺配置也必须返回终态，由队列沿用 lease.finish 的安全收敛路径。"""

    def run(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        # 固定错误码和安全身份足够排障，绝不记录配置值或业务正文。
        logging.error("SOOTHE Worker 配置不可用 run_id=%s code=SOOTHE_AGENT_NOT_CONFIGURED", run_id)
        return AgentRunResult(
            run_id=run_id, status="failed",
            execution_attempt=lease_context.execution_attempt,
            error_code="SOOTHE_AGENT_NOT_CONFIGURED",
        )

    def resume(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        return self.run(run_id, lease_context)


def configured_soothe_executor(
    session: Session,
    run_id: str,
    lease_context: LeaseContext,
    *,
    is_draining: Callable[[], bool] = lambda: False,
    dependencies: RuntimeDependencies | None = None,
) -> RunExecutor:
    """装配 SOOTHE 固定身份和单节点模型；缺少任一必需配置都禁止占位器回退。"""
    runtime_settings = dependencies.settings if dependencies is not None else settings
    agent_run = session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    if (
        agent_run is None
        or agent_run.agent_id != "soothe_agent"
        or agent_run.business_type not in {"soothe_reply", "soothe_simulator"}
        or agent_run.business_connector_id != "couple_diary_backend"
    ):
        return _SootheNotConfiguredExecutor()
    try:
        config = runtime_settings.business_connectors.get("couple_diary_backend", {})
        required = ("base_url", "runtime_id", "key_id", "secret")
        if (
            not isinstance(config, dict)
            or config.get("enabled") is not True
            or any(
                not isinstance(config.get(key), str) or not cast(str, config[key]).strip()
                for key in required
            )
        ):
            return _SootheNotConfiguredExecutor()
        node_routes = runtime_settings.agent_model_node_routes("soothe_agent")
        # 通用配置只负责身份隔离；SOOTHE 仍要求唯一模型节点，缺键或多键均终态失败。
        if set(node_routes) != {"compose_turn"}:
            return _SootheNotConfiguredExecutor()
        available_routes = {route.route_id for route in runtime_settings.model_routes if route.enabled}
        if any(route_id not in available_routes for route_id in node_routes.values()):
            return _SootheNotConfiguredExecutor()
        gateway = build_model_gateway(
            session, runtime_settings, is_draining=is_draining, dependencies=dependencies,
        )
    except (ValueError, TypeError, AttributeError):
        # JSON/路由/凭据配置异常的原文可能含私有地址；仅返回固定配置错误。
        return _SootheNotConfiguredExecutor()
    if gateway is None:
        return _SootheNotConfiguredExecutor()

    from app.agents.soothe_agent.model_gateway import SootheModelGatewayAdapter
    from app.agents.soothe_agent.runner import SootheNodeRunner

    model_gateway = SootheModelGatewayAdapter(session, gateway, node_routes)
    model_gateway.bind_lease(lease_context)
    try:
        tool_gateway = build_tool_gateway(
            session, agent_run, config, lease_context,
            is_draining=is_draining, dependencies=dependencies, runtime_settings=runtime_settings,
        )
        checkpoint_store = build_checkpoint_store(session, runtime_settings)
    except ValueError:
        # 仅收敛装配时 URL/密钥配置拒绝；运行期节点与发送错误仍由执行器正常处理。
        return _SootheNotConfiguredExecutor()
    return WorkflowExecutor(
        session, SootheNodeRunner(tool_gateway, ToolCallAuditService(session), model_gateway),
        checkpoint_store,
        ArtifactStore(session),
        authorization_version_resolver=lambda run: AuthorizationService(
            runtime_settings.trusted_clients,
        ).authorization_version(run.caller_id),
        is_draining=is_draining,
    )
