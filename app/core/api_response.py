from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from app.core.config import settings
from app.schemas.common_data import ApiResponseData, PlatformEnum


def detect_platform_from_path(path: str) -> PlatformEnum:
    """根据请求路径推断平台类型。

    当前项目已经约定：
    1. `/api/v1/demo/...` 归类为 `DEMO`
    2. `/api/v1/diary/...` 归类为 `DIARY`
    3. 其他暂未归类的平台路径，先统一回落到 `UNKNOWN`

    后续如果接入新的正式业务平台，只需要在这里集中扩展即可，
    这样平台识别规则就能始终收口在一个位置，也方便 LLM
    和新同事快速看懂“路径”和“platform 字段”之间的对应关系。
    """
    normalized_path = normalize_api_path(path)
    logging.info("开始根据请求路径识别平台，path=%s", normalized_path)

    if normalized_path.startswith("api/v1/demo/"):
        logging.info("识别到 demo 示例平台，path=%s", normalized_path)
        return PlatformEnum.DEMO

    if normalized_path.startswith("api/v1/diary/"):
        logging.info("识别到 diary 正式业务平台，path=%s", normalized_path)
        return PlatformEnum.DIARY

    logging.info("未匹配到明确平台，回落为 UNKNOWN，path=%s", normalized_path)
    return PlatformEnum.UNKNOWN


def normalize_api_path(path: str) -> str:
    """统一 api 字段的路径格式。"""
    return path.strip("/")


def build_api_response(
    *,
    path: str,
    data: Any,
    ret: list[str],
    platform: PlatformEnum | None = None,
) -> ApiResponseData:
    """构建标准成功响应。

    这里把统一响应格式收口到一个地方，避免每个路由手写一套字段，
    也避免再依赖中间件在响应发出前“事后修补”。
    """
    resolved_platform = platform or detect_platform_from_path(path)
    response = ApiResponseData(
        platform=resolved_platform,
        api=normalize_api_path(path),
        data=data,
        ret=ret,
        # 响应版本号属于“应用基础身份信息”的一部分。
        # 这里优先从 application 分组读取，避免统一响应逻辑继续散落依赖扁平字段。
        v=settings.application.response_version,
    )
    logging.info(
        "构建标准响应成功，platform=%s, api=%s",
        response.platform,
        response.api,
    )
    return response


def build_api_response_from_request(
    request: Request,
    *,
    data: Any,
    ret: list[str],
    platform: PlatformEnum | None = None,
) -> ApiResponseData:
    """根据 Request 构建标准响应。"""
    return build_api_response(
        path=request.url.path,
        data=data,
        ret=ret,
        platform=platform,
    )


def build_error_response(
    *,
    path: str,
    ret: list[str],
    data: Any,
    platform: PlatformEnum | None = None,
) -> dict[str, Any]:
    """构建标准错误响应。

    异常处理器返回 JSONResponse 时，直接使用 dict 更直接。
    """
    response = build_api_response(
        path=path,
        data=data,
        ret=ret,
        platform=platform,
    )
    return response.model_dump(mode="json")
