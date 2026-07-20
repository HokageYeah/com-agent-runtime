"""schema 通过后仍必须执行的确定性业务语义校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SemanticValidationResult:
    valid: bool
    error_codes: tuple[str, ...]
    safe_summary: dict[str, int]
    normalized_value: dict[str, object] | None


class SemanticValidator:
    """只允许受信任来源与有限的播放动作；URL/connector 等控制字段永不放行。"""

    # 结构化模型输出不能携带 Runtime 的控制面或认证材料；业务正文不在此名单内。
    _FORBIDDEN_CONTROL_FIELDS = {
        "url", "endpoint", "connector", "connector_id", "permission", "callback_target",
        "authorization", "authorization_version", "privacy", "privacy_version", "generation",
        "generation_epoch", "fencing_token", "execution_attempt", "run_id", "package_digest",
        "contract_version", "secret", "token", "access_token", "api_key", "password",
    }

    def validate(
        self, value: object, schema: object = None, trusted_refs: set[str] | None = None,
        policy: object = None,
    ) -> SemanticValidationResult:
        del schema, policy  # schema 已在解析层校验；这里仅做确定性领域约束。
        if not isinstance(value, Mapping):
            return SemanticValidationResult(False, ("SEMANTIC_VALUE_INVALID",), {}, None)
        refs = trusted_refs or set()
        errors: list[str] = []
        normalized = dict(value)
        if _contains_forbidden_control_field(value, self._FORBIDDEN_CONTROL_FIELDS):
            errors.append("FORBIDDEN_CONTROL_FIELD")
        source_refs = value.get("source_refs", [])
        if not isinstance(source_refs, list) or any(not isinstance(ref, str) or ref not in refs for ref in source_refs):
            errors.append("UNKNOWN_SOURCE_REF")
        duration = value.get("duration_ms")
        if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 30_000):
            errors.append("INVALID_DURATION")
        return SemanticValidationResult(
            not errors, tuple(dict.fromkeys(errors)),
            {"source_ref_count": len(source_refs) if isinstance(source_refs, list) else 0},
            normalized if not errors else None,
        )


def _contains_forbidden_control_field(value: object, forbidden_fields: set[str]) -> bool:
    """递归检查结构化结果键名，不读取也不输出私密字段值。"""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and key.lower() in forbidden_fields:
                return True
            if _contains_forbidden_control_field(nested, forbidden_fields):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_forbidden_control_field(item, forbidden_fields) for item in value)
    return False
