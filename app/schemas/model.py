"""模型调用对外只暴露已验证、无正文的结果结构。"""

from pydantic import BaseModel, ConfigDict


class StructuredResultSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parse_status: str
    safety_status: str
    route_id: str | None = None
    fallback_used: bool = False
    error_codes: list[str] = []
