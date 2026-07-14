import asyncio
import time

from loguru import logger

from app.decorators.cache_decorator import timed_cache, ttl_cache


@logger.catch
@ttl_cache(ttl=60, cache_name="test_hot_topics")
async def get_demo_hot_topics(topic_count: int = 10):
    """获取示例热门话题（使用 TTL 缓存装饰器）

    此函数是项目里的标准示例服务函数，用来演示：
    1. 如何在服务层编写清晰的异步逻辑
    2. 如何配合缓存装饰器减少重复计算
    3. 如何补充日志，方便本地调试和后续 LLM 理解代码意图
    """
    logger.info(f"开始获取示例热门话题，数量: {topic_count}")
    # 1 / 0
    # 模拟耗时操作
    await asyncio.sleep(1)

    # 生成模拟数据
    topics = [
        {"id": i, "title": f"热门话题 {i}", "views": 1000 * i}
        for i in range(1, topic_count + 1)
    ]

    logger.success(f"成功获取 {len(topics)} 个示例热门话题")
    return {"topics": topics, "timestamp": time.time()}


@timed_cache(seconds=30)
async def search_demo_accounts(keyword: str, limit: int = 5):
    """搜索示例账号列表（使用简单时间缓存装饰器）

    这个函数保留为示例代码，重点演示：
    1. 参数如何进入服务层
    2. 如何打印调试日志
    3. 如何生成稳定、可预测的模拟数据供联调使用
    """
    cache_key = f"{keyword}_{limit}"
    logger.debug(
        "搜索示例账号，关键词: %s，限制: %s，缓存键: %s",
        keyword,
        limit,
        cache_key,
    )

    # 模拟API调用延迟
    await asyncio.sleep(0.5)

    # 生成模拟数据
    accounts = [
        {
            "id": f"account_{i}",
            "name": f"{keyword}_{i}",
            "followers": 10000 * i,
            "description": f"这是与 {keyword} 相关的示例账号 {i}",
        }
        for i in range(1, limit + 1)
    ]

    logger.info(f"找到 {len(accounts)} 个与 '{keyword}' 相关的示例账号")
    return {"accounts": accounts, "keyword": keyword, "timestamp": time.time()}
