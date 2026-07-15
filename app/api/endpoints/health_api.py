from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.core.config import Settings

router = APIRouter(tags=["health"])


@dataclass
class RuntimeHealth:
    """Runtime 就绪状态，集中管理 draining 与基础依赖配置检查。"""

    settings: Settings
    # 根数据库探活由主应用注入，避免 Runtime 私自创建第二套连接或连接池。
    database_ready: Callable[[], tuple[bool, dict[str, object]]] | None = None
    draining: bool = False

    def check_ready(self) -> tuple[bool, dict[str, str]]:
        """只输出安全的配置状态，不回传密钥、连接串或业务数据。"""
        database_ok, _ = (
            self.database_ready() if self.database_ready is not None else (True, {})
        )
        checks = {
            "database": "ready" if database_ok else "not_ready",
            "agent_package": "configured",
            "trusted_clients": "configured" if self.settings.trusted_clients else "missing",
            "draining": "true" if self.draining else "false",
        }
        ready = (
            database_ok
            and checks["trusted_clients"] == "configured"
            and not self.draining
        )
        logging.info("Runtime readiness 检查 ready=%s checks=%s", ready, checks)
        return ready, checks


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
