"""Memoir 专属装配：保留节点路由、媒体/音频降级与 connector 缺失行为。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.memoir_agent.model_gateway import MemoirModelGatewayAdapter
from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.core.authorization import AuthorizationService
from app.core.config import settings
from app.models import AgentRun
from app.runtime.artifact import ArtifactStore
from app.runtime.executor import WorkflowExecutor
from app.runtime.interfaces import LeaseContext
from app.runtime.test_harness import RuntimeDependencies
from app.schemas.audit import CONNECTOR_DISABLED, RuntimeAuditEvent
from app.services.audit_service import AuditService
from app.services.evaluation_service import EvaluationService
from app.services.tool_call_audit_service import ToolCallAuditService
from app.worker import build_checkpoint_store, build_model_gateway, build_tool_gateway

# 模型网关装配的精确门禁基准：env 里 MEMOIR_MODEL_NODE_ROUTES_JSON 的键集合
# 必须与这里完全一致，缺键即整体禁用模型网关走模板 fallback。
# generate_scene_batch 是 M7 bounded_loop 的循环体模型节点；
# repair_coverage_gaps 是 M7 循环后的覆盖修复模型节点，须一并登记。
_MEMOIR_MODEL_NODES = frozenset(
    {
        "extract_highlights",
        "plan_chapters",
        "generate_scenes",
        "generate_scene_batch",
        "repair_coverage_gaps",
    }
)


def configured_model_gateway(
    session: Session,
    *,
    is_draining: Callable[[], bool] = lambda: False,
    dependencies: RuntimeDependencies | None = None,
) -> MemoirModelGatewayAdapter | None:
    """Memoir 保留完整节点路由门禁；公共网关不可用时继续使用模板。"""
    runtime_settings = dependencies.settings if dependencies is not None else settings
    try:
        routes = runtime_settings.model_routes
        node_routes = runtime_settings.memoir_model_node_routes
    except ValueError:
        logging.warning("Memoir Worker 模型配置无效，使用模板 fallback")
        return None
    if set(node_routes) != _MEMOIR_MODEL_NODES or any(
        route_id not in {route.route_id for route in routes}
        for route_id in node_routes.values()
    ):
        logging.warning("Memoir Worker 模型配置不完整，使用模板 fallback")
        return None
    gateway = build_model_gateway(
        session, runtime_settings, is_draining=is_draining, dependencies=dependencies,
    )
    return MemoirModelGatewayAdapter(session, gateway, node_routes) if gateway is not None else None


def configured_media_service(
    runtime_settings: object, session: Session,
) -> object | None:
    """按部署配置装配 M6 媒体服务；总开关关闭或依赖缺失时返回 None。

    返回 None 等价于媒体能力关闭：媒体节点按 CAPABILITY_DISABLED 跳过/降级，
    旧版本行为零变化。装配失败只记受控日志，绝不把凭证或 URL 写进日志。
    """
    if not bool(getattr(runtime_settings, "MEMOIR_MEDIA_ENABLED", False)):
        return None
    try:
        from app.services.memoir.memoir_media_service import (
            MemoirMediaConfig,
            MemoirMediaService,
        )
        from app.utils.aliyun.oss_client import AliyunOSSClient
        from app.utils.volcano.cv_client import MockCVClient, VolcanoCVClient

        provider_name = str(getattr(runtime_settings, "MEMOIR_MEDIA_PROVIDER", "mock"))
        if provider_name == "volcano":
            provider: object = VolcanoCVClient(
                access_key=str(getattr(runtime_settings, "VOLCANO_CV_ACCESS_KEY", "")),
                secret_key=str(getattr(runtime_settings, "VOLCANO_CV_SECRET_KEY", "")),
                region=str(getattr(runtime_settings, "VOLCANO_CV_REGION", "cn-north-1")),
                host=str(getattr(runtime_settings, "VOLCANO_CV_HOST", "visual.volcengineapi.com")),
                timeout_seconds=float(getattr(runtime_settings, "MEMOIR_MEDIA_IMAGE_TIMEOUT_SECONDS", 25.0)),
                max_retries=int(getattr(runtime_settings, "MEMOIR_MEDIA_IMAGE_MAX_RETRIES", 1)),
            )
        else:
            # mock provider：开发/测试同接口确定性输出，绝不触达计费 API。
            provider = MockCVClient()
        uploader = AliyunOSSClient(
            str(getattr(runtime_settings, "ACCESS_KEY_ID", "")),
            str(getattr(runtime_settings, "ACCESS_KEY_SECRET", "")),
            str(getattr(runtime_settings, "BUCKET_NAME", "")),
            str(getattr(runtime_settings, "REGION", "")),
            str(getattr(runtime_settings, "ENDPOINT", "")),
        )
        config = MemoirMediaConfig.from_settings(runtime_settings)

        return MemoirMediaService(
            provider, uploader, config,
            session=session,
        )
    except Exception as exc:
        # 装配失败按能力关闭处理；只记异常类名，不记正文（可能含配置值）。
        logging.warning(
            "MemoirAgent 媒体服务装配失败按能力关闭 code=%s",
            type(exc).__name__,
        )
        return None


def configured_audio_service(
    runtime_settings: object, session: Session,
) -> object | None:
    """按部署配置装配 M8 音频服务；总开关关闭或依赖缺失时返回 None。

    返回 None 等价于音频能力关闭：1.0.8 图的 enqueue_audio_tasks 节点按
    CAPABILITY_DISABLED 跳过（发布 2.0.0 空音频文档），旧版本图不含该
    节点，存量行为零变化。公共 Worker 消息/调度语义不变。
    """
    if not bool(getattr(runtime_settings, "MEMOIR_AUDIO_ENABLED", False)):
        return None
    try:
        from app.services.memoir.memoir_audio_service import (
            build_memoir_audio_service,
        )

        return build_memoir_audio_service(runtime_settings, session)
    except Exception as exc:
        # 装配失败按能力关闭处理；只记异常类名，不记可能含配置值的正文。
        logging.warning(
            "MemoirAgent 音频服务装配失败按能力关闭 code=%s",
            type(exc).__name__,
        )
        return None


def configured_memoir_executor(
    session: Session,
    run_id: str,
    lease_context: LeaseContext,
    *,
    is_draining: Callable[[], bool] = lambda: False,
    dependencies: RuntimeDependencies | None = None,
) -> WorkflowExecutor | None:
    """原样保留 Memoir 装配与 connector 缺失回退；每次调用新建 Runner/执行器。"""
    runtime_settings = (
        dependencies.settings if dependencies is not None else settings
    )
    agent_run = session.scalar(
        select(AgentRun).where(AgentRun.run_id == run_id)
    )
    # Run 的版本由创建时冻结；Worker 只按 Agent 身份选择执行器，绝不把
    # 新版本或历史版本重写为默认版本。Package 可用性由 Executor 和发包守卫复核。
    if agent_run is None or agent_run.agent_id != "memoir_agent":
        return None
    config = runtime_settings.business_connectors.get(
        agent_run.business_connector_id, {}
    )
    required = ("base_url", "runtime_id", "key_id", "secret")
    if not bool(config.get("enabled")) or any(
        not isinstance(config.get(key), str) for key in required
    ):
        logging.error("Memoir Worker connector 配置不完整 run_id=%s", run_id)
        AuditService(session=session).append(
            RuntimeAuditEvent(
                audit_id=str(uuid4()), actor_type="system",
                actor_id="tool_gateway", action="tool_gateway_rejected",
                resource_type="agent_run", resource_id=run_id,
                reason_code=CONNECTOR_DISABLED, outcome="rejected",
                occurred_at=datetime.now(UTC), trace_id=agent_run.trace_id,
                metadata_summary={"run_id": run_id},
            )
        )
        return None
    tool_gateway = build_tool_gateway(
        session, agent_run, config, lease_context,
        is_draining=is_draining, dependencies=dependencies, runtime_settings=runtime_settings,
    )
    model_gateway = configured_model_gateway(
        session, is_draining=is_draining, dependencies=dependencies
    )
    if model_gateway is not None:
        model_gateway.bind_lease(lease_context)
    return WorkflowExecutor(
        session,
        MemoirNodeRunner(
            tool_gateway,
            ToolCallAuditService(session),
            model_gateway,
            EvaluationService(session),
            # M6 媒体服务：总开关关闭/装配失败时为 None，媒体节点按
            # 能力关闭跳过，与 1.0.3 之前的行为完全一致。
            configured_media_service(runtime_settings, session),
            # M8 音频服务：总开关关闭/装配失败时为 None，1.0.8 图的
            # 音频节点按能力关闭跳过（发布 2.0.0 空音频文档）。
            configured_audio_service(runtime_settings, session),
        ),
        build_checkpoint_store(session, runtime_settings),
        ArtifactStore(session),
        authorization_version_resolver=lambda run: AuthorizationService(
            runtime_settings.trusted_clients
        ).authorization_version(run.caller_id),
        is_draining=is_draining,
    )


