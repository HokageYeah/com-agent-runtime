from __future__ import annotations

import logging

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live_health(request: Request) -> dict[str, str]:
    # liveness 只回答“进程是否还在响应”，不能把数据库或 Redis 故障混进来。
    runtime_id = request.app.state.settings.runtime_id
    logging.info("Runtime 存活检查 runtime_id=%s", runtime_id)
    return {"status": "live", "runtime_id": runtime_id}


@router.get("/health/ready")
async def ready_health(request: Request) -> JSONResponse:
    # readiness 才判断依赖配置、draining 与 handler 注册是否满足对外服务条件。
    ready, checks = request.app.state.runtime_health.check_ready()
    response_status = (
        status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    logging.info(
        "Runtime 就绪检查 runtime_id=%s ready=%s checks=%s",
        request.app.state.settings.runtime_id,
        ready,
        checks,
    )
    return JSONResponse(
        status_code=response_status,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )
