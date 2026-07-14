from __future__ import annotations

import logging
import time
from uuid import uuid4

from fastapi import Request, Response

from app.core.config import settings


def resolve_request_id(request: Request) -> str:
    """解析当前请求应使用的 request_id。

    优先级说明：
    1. 如果调用方已经通过请求头传了 `X-Request-ID`，优先复用，方便前后端联调和链路串联
    2. 如果没有，就由后端在入口处自动生成一个

    这样做的好处是：
    - 本地开发时可以快速把“某一次请求”的日志串起来
    - 后续如果接入网关、前端、任务系统，也能继续透传这条链路标识
    """
    request_logging_config = settings.request_logging
    request_id = request.headers.get(request_logging_config.request_id_header)
    if request_id:
        return request_id
    return uuid4().hex


def get_request_id_from_request(request: Request) -> str:
    """从 request.state 中安全读取 request_id。"""
    return getattr(request.state, "request_id", "unknown")


async def request_logging_middleware(
    request: Request,
    call_next,
) -> Response:
    """统一记录请求入口、出口和慢请求日志。

    这层中间件解决三件事：
    1. 给每个请求分配 request_id，方便把多条日志串起来
    2. 记录基础 access log，便于后续排查请求路径、状态码和耗时
    3. 对超过阈值的请求单独打 warning，帮助快速发现慢接口
    """
    request_logging_config = settings.request_logging
    request_id = resolve_request_id(request)
    request.state.request_id = request_id
    started_at = time.perf_counter()
    client_ip = request.client.host if request.client else "unknown"

    logging.info(
        "收到请求，request_id=%s，method=%s，path=%s，client_ip=%s",
        request_id,
        request.method,
        request.url.path,
        client_ip,
    )

    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        logging.exception(
            "请求处理异常，request_id=%s，method=%s，path=%s，duration_ms=%s",
            request_id,
            request.method,
            request.url.path,
            duration_ms,
        )
        raise

    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    response.headers[request_logging_config.request_id_header] = request_id

    if duration_ms >= request_logging_config.slow_request_threshold_ms:
        logging.warning(
            "检测到慢请求，request_id=%s，method=%s，path=%s，status_code=%s，duration_ms=%s，threshold_ms=%s",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request_logging_config.slow_request_threshold_ms,
        )
    else:
        logging.info(
            "请求处理完成，request_id=%s，method=%s，path=%s，status_code=%s，duration_ms=%s",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )

    return response
