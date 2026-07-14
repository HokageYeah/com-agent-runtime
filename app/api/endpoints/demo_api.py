from fastapi import APIRouter, HTTPException, Query, Request
from loguru import logger

from app.core.api_response import build_api_response_from_request
from app.decorators.cache_decorator import get_cache
from app.schemas.common_data import ApiResponseData
from app.services.demo_service import get_demo_hot_topics, search_demo_accounts

router = APIRouter()


@router.get("/hot-topics", response_model=ApiResponseData)
async def get_hot_topics(
    request: Request,
    count: int = Query(10, description="话题数量", ge=1, le=50),
):
    """获取热门话题（使用 TTL 缓存装饰器）

    此端点是推荐的示例路由写法，主要演示：
    1. 路由层如何负责返回统一响应结构
    2. 如何打印足够清晰的调试日志
    3. 如何查看缓存命中情况

    此端点演示了如何使用 loguru 日志和 cachetools TTL 缓存
    - 结果将被缓存60秒
    - 可以通过查看日志观察缓存是否生效
    """
    logger.info(f"API请求: 获取热门话题，数量: {count}")

    # 获取缓存信息（可选）
    cache = get_cache("test_hot_topics")
    cache_info = {
        "cache_name": "test_hot_topics",
        "cache_size": len(cache) if cache else 0,
        "cache_keys": list(cache.keys()) if cache else [],
    }
    logger.debug(f"缓存信息: {cache_info}")

    # 调用缓存装饰的函数
    result = await get_demo_hot_topics(count)

    # 在路由层显式返回标准响应，避免再依赖中间件事后修补。
    return build_api_response_from_request(
        request,
        data=result,
        ret=["SUCCESS::获取热门话题成功"],
    )


@router.get("/search-accounts", response_model=ApiResponseData)
async def search_accounts(
    request: Request,
    keyword: str = Query(..., description="搜索关键词"),
    limit: int = Query(5, description="结果数量限制", ge=1, le=20),
):
    """搜索示例账号列表（使用简单时间缓存装饰器）

    此端点演示了如何使用 loguru 日志和 cachetools 简单时间缓存
    - 结果将被缓存30秒
    - 可以通过查看日志观察缓存是否生效
    """
    logger.info(f"API请求: 搜索示例账号，关键词: {keyword}，限制: {limit}")

    # 调用缓存装饰的异步函数
    result = await search_demo_accounts(keyword, limit)

    return build_api_response_from_request(
        request,
        data=result,
        ret=["SUCCESS::搜索示例账号成功"],
    )


@router.get("/error-demo", response_model=ApiResponseData)
async def error_demo(request: Request):
    """错误返回格式演示接口。

    这个接口故意抛出一个 HTTPException，
    方便本地联调时快速确认统一错误响应格式是否符合预期。
    """
    logger.warning("进入错误演示接口，准备主动抛出 HTTPException 供调试使用")
    raise HTTPException(
        status_code=418,
        detail="这是一个用于调试错误返回格式的演示接口",
    )
