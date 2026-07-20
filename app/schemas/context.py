"""跨模型边界可序列化的安全上下文结构。"""

from pydantic import BaseModel, ConfigDict, Field


class NodeContextSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trusted_instructions: str = Field(min_length=1)
    untrusted_items: list[dict[str, str]] = Field(default_factory=list)
    token_budget: int = Field(gt=0)
    source_refs: list[str] = Field(default_factory=list)
    redaction_summary: dict[str, int] = Field(default_factory=dict)
