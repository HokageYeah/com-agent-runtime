from __future__ import annotations

import logging

from fastapi import Request, status
from fastapi.exceptions import (
    HTTPException,
    RequestValidationError,
    ResponseValidationError,
)
from fastapi.responses import JSONResponse

from app.core.api_response import build_error_response
from app.middleware.request_logging import get_request_id_from_request


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """统一处理业务侧主动抛出的 HTTPException。"""
    request_id = get_request_id_from_request(request)
    logging.warning(
        "捕获 HTTPException，request_id=%s，status=%s，path=%s，detail=%s",
        request_id,
        exc.status_code,
        request.url.path,
        exc.detail,
    )

    return JSONResponse(
        status_code=exc.status_code,
        content=build_error_response(
            path=request.url.path,
            ret=[f"ERROR::{exc.detail}"],
            data={
                "request_id": request_id,
                "request_method": request.method,
                "status_code": exc.status_code,
            },
        ),
    )


async def request_validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """统一处理请求参数校验失败。

    这里保留更详细的校验错误明细，方便前端与调试日志直接消费。
    """
    validation_errors = exc.errors()
    request_id = get_request_id_from_request(request)
    logging.warning(
        "捕获 RequestValidationError，request_id=%s，path=%s，errors=%s",
        request_id,
        request.url.path,
        validation_errors,
    )

    missing_field_names: list[str] = []
    for error in validation_errors:
        location = error.get("loc", ())
        if not location:
            continue

        if location[0] == "query" and len(location) > 1:
            missing_field_names.append(f"查询参数:{location[1]}")
        elif location[0] == "body":
            body_path = ".".join(str(item) for item in location[1:]) or "请求体"
            missing_field_names.append(f"请求体:{body_path}")
        else:
            missing_field_names.append(".".join(str(item) for item in location))

    error_message = "缺少必需的参数或参数格式错误"
    if missing_field_names:
        error_message = f"{error_message}: {', '.join(missing_field_names)}"

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=build_error_response(
            path=request.url.path,
            ret=[f"ERROR::{error_message}"],
            data={
                "request_id": request_id,
                "request_method": request.method,
                "validation_errors": validation_errors,
            },
        ),
    )


async def response_validation_error_handler(
    request: Request,
    exc: ResponseValidationError,
) -> JSONResponse:
    """统一处理响应模型校验失败。

    这类错误本质上是服务端代码问题，不应该伪装成 200 成功。
    为了避免泄漏过多内部细节，对外只返回统一错误信息；
    详细的原始错误通过日志保留给开发排查。
    """
    request_id = get_request_id_from_request(request)
    logging.exception(
        "捕获 ResponseValidationError，request_id=%s，path=%s，errors=%s，body=%s",
        request_id,
        request.url.path,
        exc.errors(),
        exc.body,
    )

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=build_error_response(
            path=request.url.path,
            ret=["ERROR::服务端响应格式校验失败"],
            data={
                "request_id": request_id,
                "request_method": request.method,
                "validation_errors": exc.errors(),
            },
        ),
    )
