from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status

from app.contracts.api import CONTRACT_VERSION
from app.core.security import SignatureError, verify_signature
from app.services.agent_package_service import (
    AgentPackageService,
    AgentPackageValidationError,
)

router = APIRouter(tags=["runtime"])


@router.get("/capabilities")
async def runtime_capabilities(request: Request) -> dict[str, object]:
    """只向已验签业务服务提供 Runtime 的安全能力摘要。"""
    body = await request.body()
    try:
        client_id = verify_signature(
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
    # 不记录请求头中可能出现的签名、Key ID 或其它凭据，只保留客户端标识。
    try:
        # capabilities 必须暴露实际不可变 package digest，供业务侧立刻发现版本漂移。
        package = AgentPackageService(Path(__file__).parents[2] / "agents").load(
            "memoir_agent", "1.0.0"
        )
    except AgentPackageValidationError as exc:
        logging.error("Runtime capabilities 无法加载 MemoirAgent 摘要")
        raise HTTPException(status_code=503, detail="agent package unavailable") from exc
    logging.info("Runtime capability 查询通过 client_id=%s", client_id)
    return {
        "contract_version": CONTRACT_VERSION,
        "package_digest": package.package_digest,
        "agents": [{"agent_id": package.agent_id, "version": package.version}],
        "model_policies": [
            "reasoning",
            "balanced",
            "emotional_writing",
            "cheap_structured",
            "strict",
            "private_first",
        ],
        "capabilities": {"workflow_agent": True, "native_sse": False, "media": False},
    }
