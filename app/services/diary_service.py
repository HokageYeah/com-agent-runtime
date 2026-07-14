from __future__ import annotations

from loguru import logger

from app.schemas.diary import DiaryModuleInfo


async def get_diary_module_info() -> DiaryModuleInfo:
    """返回日记业务模块的骨架说明。

    这里不接数据库、不做假业务数据拼装，
    只保留最小正式业务样板，方便后续继续往里接真实能力。
    """
    logger.info("开始构建 diary 正式业务模块骨架说明")
    payload = DiaryModuleInfo(
        module="diary",
        status="skeleton_ready",
        message="这是正式业务模块样板，可在此基础上继续扩展真实日记能力",
    )
    logger.success("diary 正式业务模块骨架说明构建完成")
    return payload
