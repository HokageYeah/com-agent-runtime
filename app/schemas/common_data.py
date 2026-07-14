from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class PlatformEnum(StrEnum):
    """统一响应里的平台枚举。

    设计约定：
    1. `UNKNOWN` 表示当前接口还没有归属到明确的平台域
    2. 如果未来新增真实业务平台，直接在这里补充新的枚举值
    3. 如果只是本地调试、联调演示、教学示例，可以使用 `DEMO`

    扩展示例：
    - 如果未来新增日记平台，可以增加 `DIARY = "DIARY"`
    - 然后再去 `app/core/api_response.py` 的 `detect_platform_from_path()`
      里补路径识别规则，让 `/api/v1/diary/...` 自动映射到 `PlatformEnum.DIARY`

    使用示例：
    - 路由层手动指定平台：
      `build_api_response_from_request(
          request,
          data=result,
          ret=["SUCCESS::ok"],
          platform=PlatformEnum.DEMO,
      )`
    """

    UNKNOWN = "UNKNOWN"
    # 这个值是保留给测试、教学、联调用的示例平台。
    # 它不代表真实业务域，但非常适合告诉后续 LLM / 开发者：
    # “如果你要新增平台枚举，就按这个位置继续扩展”。
    DEMO = "DEMO"
    # 这个值表示正式业务域“日记模块”。
    # 当前先把它作为模板工程里的标准业务样板，方便后续继续扩展真实功能。
    DIARY = "DIARY"


class ApiResponseData(BaseModel):
    platform: PlatformEnum
    api: str
    data: Any
    ret: list[str]
    v: int
