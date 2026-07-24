"""跨业务边界可见的最小运行轨迹契约。"""

from pydantic import BaseModel, ConfigDict, Field


class PublicTraceItem(BaseModel):
    """只允许节点标识、受控状态和 Package 配置的公开标签。"""

    model_config = ConfigDict(extra="forbid")

    step: str = Field(min_length=1, max_length=120)
    status: str = Field(min_length=1, max_length=32)
    label: str | None = Field(default=None, max_length=120)
