"""模型结构化输出的最小解析与确定性引用校验。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.runtime.json_repair import parse_json_once
from app.runtime.semantic_validation import SemanticValidator

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    """原始模型文本不跨越此 DTO，调用方只可获得已验证对象或受控错误码。"""

    validated_value: T | None
    parse_status: str
    safety_status: str
    error_codes: tuple[str, ...] = ()


class StructuredOutputParser:
    """先严格 JSON，随后仅做一次无执行能力的字面量修复。"""

    def parse_and_validate(
        self, raw: str, schema: type[T], *, trusted_source_refs: set[str],
    ) -> StructuredResult[T]:
        if not isinstance(raw, str):
            return StructuredResult(None, "failed", "parse_failed", ("JSON_PARSE_FAILED",))
        value, status = parse_json_once(raw)
        if value is None:
            return StructuredResult(None, "failed", "parse_failed", ("JSON_PARSE_FAILED",))
        try:
            validated = schema.model_validate(value)
        except ValidationError:
            return StructuredResult(None, status, "schema_validation_failed", ("SCHEMA_VALIDATION_FAILED",))
        semantic = SemanticValidator().validate(
            validated.model_dump(mode="json"), trusted_refs=trusted_source_refs,
        )
        if not semantic.valid:
            return StructuredResult(None, status, "semantic_validation_failed", semantic.error_codes)
        return StructuredResult(validated, status, "passed")
