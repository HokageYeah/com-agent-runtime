from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.exceptions import (
    HTTPException,
    RequestValidationError,
    ResponseValidationError,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.api import api_router
from app.core.config import settings
from app.core.logging_uru import setup_logging
from app.db.sqlalchemy_db import database
from app.middleware.exception_handlers import (
    http_exception_handler,
    request_validation_error_handler,
    response_validation_error_handler,
)
from app.middleware.request_logging import request_logging_middleware

application_config = settings.application
server_config = settings.server
cors_config = settings.cors


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.info("应用生命周期启动，准备初始化日志与数据库连接")
    setup_logging()
    database.connect()
    try:
        yield
    finally:
        logging.info("应用生命周期结束，准备关闭数据库连接")
        database.close()


app = FastAPI(
    # 这里优先读取应用分组配置，能更清楚表达“这些字段属于服务身份信息”。
    title=application_config.project_name,
    description=application_config.project_description,
    version=application_config.project_version,
    openapi_url=f"{application_config.api_prefix}/openapi.json",
    lifespan=lifespan,
)

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
