from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, status
from fastapi.exceptions import (
    HTTPException,
    RequestValidationError,
    ResponseValidationError,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.api import api_router
from app.api.endpoints.health_api import RuntimeHealth
from app.core.config import settings
from app.core.logging_uru import setup_logging, shutdown_logging
from app.db.sqlalchemy_db import database
from app.middleware.exception_handlers import (
    http_exception_handler,
    request_validation_error_handler,
    response_validation_error_handler,
)
from app.middleware.request_logging import request_logging_middleware
from app.runtime.test_harness import RuntimeDependencies
from app.services.memory_agent_adapter import (
    MemoryAgentAdapter,
    MemoryRuntimeClientConfig,
)
from app.services.memory_archive_service import FernetSnapshotCipher
from app.services.memory_s3_media_proxy import MemoryS3MediaProxy

application_config = settings.application
server_config = settings.server
cors_config = settings.cors


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.info("应用生命周期启动，准备初始化日志与数据库连接")
    setup_logging()
    database.connect()
    # Runtime API 与 Worker 共享根工程唯一 Session 工厂；每次请求/任务仍各自创建事务。
    app.state.session_factory = database.get_session_factory()
    logging.info("已向 Runtime 注入根数据库 Session 工厂")
    # 用户侧 retry 与对账任务共用服务身份；连接仅在生命周期结束时关闭。
    app.state.memory_runtime_gateway = MemoryAgentAdapter(
        MemoryRuntimeClientConfig(
            settings.MEMORY_RUNTIME_BASE_URL,
            settings.MEMORY_RUNTIME_CLIENT_ID,
            settings.MEMORY_RUNTIME_KEY_ID,
            settings.MEMORY_RUNTIME_SECRET,
            settings.MEMORY_RUNTIME_TIMEOUT_SECONDS,
            settings.MEMORY_RUNTIME_CAPABILITY_TTL_SECONDS,
        ),
        httpx.Client(),
    )
    try:
        # S3/MinIO/COS 等兼容桶仅在完整配置时启用；未配置保持媒体 API fail-closed。
        # 放在 finally 覆盖范围内，半配置启动失败时也释放已创建的 Runtime HTTP client。
        app.state.memory_media_proxy = MemoryS3MediaProxy.from_settings(settings)
        yield
    finally:
        gateway = getattr(app.state, "memory_runtime_gateway", None)
        if gateway is not None:
            gateway.close()
        logging.info("应用生命周期结束，准备关闭数据库连接")
        database.close()
        shutdown_logging()


app = FastAPI(
    # 这里优先读取应用分组配置，能更清楚表达“这些字段属于服务身份信息”。
    title=application_config.project_name,
    description=application_config.project_description,
    version=application_config.project_version,
    openapi_url=f"{application_config.api_prefix}/openapi.json",
    lifespan=lifespan,
)
app.state.settings = settings
app.state.memory_snapshot_cipher = FernetSnapshotCipher(settings.MEMORY_SNAPSHOT_FERNET_KEY.encode())
app.state.runtime_health = RuntimeHealth(settings, database_ready=database.check_ready)

app.add_middleware(
    CORSMiddleware,
    # CORS 改为配置化，方便模板工程在不同环境、不同前端端口下复用。
    # 如果后续接 Web、H5、管理后台，只需要调环境变量，不需要再改代码。
    allow_origins=cors_config.allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_exception_handler(RequestValidationError, request_validation_error_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(ResponseValidationError, response_validation_error_handler)
app.middleware("http")(request_logging_middleware)
app.include_router(api_router, prefix=application_config.api_prefix)


def create_runtime_app(
    *,
    runtime_settings: Any | None = None,
    session_factory: Callable[[], Any] | None = None,
    dependencies: RuntimeDependencies | None = None,
) -> FastAPI:
    """创建仅供进程 harness 注入依赖的 Runtime API，绝不改写全局环境配置。"""
    if dependencies is not None:
        runtime_settings, session_factory = (
            dependencies.settings,
            dependencies.session_factory,
        )
    if runtime_settings is None or session_factory is None:
        raise ValueError("RUNTIME_DEPENDENCIES_REQUIRED")
    test_app = FastAPI(title="AgentRuntime test harness")
    test_app.state.settings = runtime_settings
    test_app.state.session_factory = session_factory
    test_app.state.memory_snapshot_cipher = FernetSnapshotCipher(
        runtime_settings.MEMORY_SNAPSHOT_FERNET_KEY.encode()
    )
    test_app.state.runtime_health = RuntimeHealth(
        runtime_settings, database_ready=lambda: (True, {"database": "ready"})
    )
    test_app.include_router(api_router, prefix=runtime_settings.application.api_prefix)
    return test_app


@app.get("/")
async def root():
    return {"message": f"{application_config.project_name} API"}


@app.get("/healthz")
async def healthz():
    """基础健康检查接口。

    这个接口故意保持轻量：
    1. 不做数据库探活，避免把“服务进程存活”与“依赖是否就绪”混在一起
    2. 返回尽量稳定，方便本地开发、容器探针、反向代理和 CI 自检直接复用
    """
    logging.info(
        "收到健康检查请求，environment=%s",
        application_config.environment,
    )
    return {
        "status": "ok",
        "service": application_config.project_name,
        "environment": application_config.environment,
        "version": application_config.project_version,
    }


@app.get("/readyz")
async def readyz():
    """依赖就绪检查接口。

    和 `/healthz` 不同，这里要回答的是：
    “当前服务除了进程活着之外，是否已经具备对外提供能力”。
    当前先检查数据库依赖，后续如果接入 Redis、消息队列、对象存储，
    也可以继续往这里追加。
    """
    logging.info("收到 readyz 请求，开始检查关键依赖")
    database_ready, database_payload = database.check_ready()

    payload = {
        "status": "ready" if database_ready else "not_ready",
        "service": application_config.project_name,
        "environment": application_config.environment,
        "version": application_config.project_version,
        "checks": [database_payload],
    }

    response_status = (
        status.HTTP_200_OK if database_ready else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    logging.info("readyz 检查完成，status=%s", payload["status"])
    return JSONResponse(status_code=response_status, content=payload)


if __name__ == "__main__":
    import uvicorn

    logging.info("启动应用服务器...")
    uvicorn.run(
        "app.main:app",
        host=server_config.host,
        port=server_config.port,
        reload=server_config.reload,
    )
