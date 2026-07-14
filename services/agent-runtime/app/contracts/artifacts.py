from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ArtifactEnvelope(BaseModel):
    """Stores only a digest, a safe summary and a business-owned reference."""

    model_config = ConfigDict(extra="forbid")

    # Artifact 不是回忆录正文权威副本，只保存摘要、摘要哈希和业务资源引用。
    schema_version: str = "1.0.0"
    artifact_type: str
    content_digest: str
    summary: dict[str, str] | None = None
    business_resource_ref: str
