from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

from app.contracts.api import CONTRACT_VERSION

router = APIRouter(tags=["runtime"])


@router.get("/capabilities")
async def runtime_capabilities(request: Request) -> dict[str, object]:
    client_id = request.headers.get("X-Agent-Client-Id")
    # 这里只做骨架阶段的服务身份识别；Task 5 会补齐时间戳、HMAC 与可见性校验。
    if client_id not in request.app.state.settings.trusted_clients:
        logging.warning("Runtime capability 查询被拒绝：未知 client_id=%s", client_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unknown client",
        )
    # 不记录请求头中可能出现的签名、Key ID 或其它凭据，只保留客户端标识。
    logging.info("Runtime capability 查询通过 client_id=%s", client_id)
    return {
        "contract_version": CONTRACT_VERSION,
        "agents": [{"agent_id": "memoir_agent", "version": "1.0.0"}],
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
