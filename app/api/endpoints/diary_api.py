from __future__ import annotations

from fastapi import APIRouter, Request
from loguru import logger

from app.core.api_response import build_api_response_from_request
from app.schemas.common_data import ApiResponseData
from app.services.diary_service import get_diary_module_info

router = APIRouter()


@router.get("/ping", response_model=ApiResponseData)
async def diary_ping(request: Request):
    """日记业务模块骨架探针接口。

    这个接口的目的不是返回真实业务数据，而是提供一条最小正式业务链路，
    让后续开发者和 LLM 明白：
    1. 正式业务域应该独立成自己的路由模块
    2. 路由层继续负责统一响应包装
    3. 业务逻辑仍然进入 service 层，而不是散落在接口函数里
    """
    logger.info("收到 diary 模块骨架探针请求，path=%s", request.url.path)
    result = await get_diary_module_info()
    return build_api_response_from_request(
        request,
        data=result.model_dump(),
        ret=["SUCCESS::获取 diary 模块骨架信息成功"],
    )
