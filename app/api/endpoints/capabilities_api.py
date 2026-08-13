from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status

from app.contracts.api import CONTRACT_VERSION
from app.core.security import (
    SignatureError,
    assert_single_service_headers,
    verify_signature,
)
from app.runtime.model_gateway import (
    ModelCapabilityEvaluator,
    ModelPolicyRegistry,
    ProviderTrafficController,
)
from app.services.agent_package_service import (
    AgentPackageService,
    AgentPackageValidationError,
)

router = APIRouter(tags=["runtime"])


def _model_capability_summary(runtime_settings: object) -> tuple[bool, list[str]]:
    """只返回逻辑模型能力；配置或 Redis 任一异常都关闭增强能力。"""
    try:
        from redis import Redis

        routes = tuple(runtime_settings.model_routes)  # type: ignore[attr-defined]
        redis_url = runtime_settings.RUNTIME_REDIS_URL  # type: ignore[attr-defined]
        if not routes or not isinstance(redis_url, str) or not redis_url:
            return False, []
        traffic = ProviderTrafficController(Redis.from_url(redis_url))
        redis_available = any(
            traffic.preflight_circuit(route).status == "circuit_available" for route in routes
        )
        policies = ModelCapabilityEvaluator(ModelPolicyRegistry.default()).available_policy_names(
            routes, redis_available=redis_available,
        )
        return bool(policies), policies
    except Exception:
        # capability 响应不得因 Redis/部署配置问题泄露异常或 Provider 细节。
        return False, []


@router.get("/capabilities")
async def runtime_capabilities(request: Request) -> dict[str, object]:
    """只向已验签业务服务提供 Runtime 的安全能力摘要。"""
    body = await request.body()
    try:
        # 重复服务认证头必须在压平前拒绝：dict 推导会抹掉同名头基数，导致 fail-open。
        assert_single_service_headers(request.headers)
        verify_signature(
            {key.lower(): value for key, value in request.headers.items()},
            request.method, request.url.path, body,
            request.app.state.settings.trusted_clients,
            request.app.state.settings.signature_tolerance_seconds,
        )
    except SignatureError as exc:
        logging.warning("Runtime capability 查询验签失败")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid service signature",
        ) from exc
    # 不记录请求头中的身份、签名、Key ID 或其它凭据。
    try:
        # capabilities 必须暴露实际不可变 package digest，供业务侧立刻发现版本漂移。
        # 暴露当前活跃 Agent 版本 1.0.1：1.0.0 已恢复为冻结原貌、不再用于新建 Run，
        # 新 Run 固定使用 1.0.1；旧 Run 不走 capabilities，仍按其已绑定版本 resume/retry。
        package = AgentPackageService(Path(__file__).parents[2] / "agents").load(
            "memoir_agent", "1.0.1"
        )
    except AgentPackageValidationError as exc:
        logging.error("Runtime capabilities 无法加载 MemoirAgent 摘要")
        raise HTTPException(status_code=503, detail="agent package unavailable") from exc
    logging.info("Runtime capability 查询通过")
    model_enhancement_available, model_policies = _model_capability_summary(
        request.app.state.settings
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "package_digest": package.package_digest,
        "agents": [{"agent_id": package.agent_id, "version": package.version}],
        "model_policies": model_policies,
        "capabilities": {
            "workflow_agent": True,
            "native_sse": False,
            "media": False,
            "model_enhancement_available": model_enhancement_available,
        },
    }
