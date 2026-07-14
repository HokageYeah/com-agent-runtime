from __future__ import annotations

from pydantic import BaseModel, Field


class DiaryModuleInfo(BaseModel):
    """日记业务模块的最小返回结构。

    这里故意保持轻量，不直接绑定数据库字段，
    只负责告诉后续开发者：
    正式业务域可以先从清晰的 schema 边界开始搭骨架，
    再逐步接入更复杂的读写逻辑。
    """

    module: str = Field(description="当前业务模块名")
    status: str = Field(description="当前模块状态")
    message: str = Field(description="给开发者或调用方的提示信息")
