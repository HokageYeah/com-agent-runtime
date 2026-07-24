"""外部观测导出的最小治理屏障，默认不向第三方发送 Runtime 数据。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ExternalExporterPolicy:
    """只有完整治理声明和安全字段同时满足时才允许导出。"""

    enabled: bool = False
    data_classification: str | None = None
    sampled_fields: tuple[str, ...] = ()
    region: str | None = None
    retention_days: int | None = None
    audit_permission: str | None = None
    privacy_purge_supported: bool = False

    _FORBIDDEN_FIELDS = frozenset({
        "prompt", "diary", "content", "checkpoint", "tool_payload", "signed_url",
        "secret", "token", "key", "reasoning",
    })

    def allows_export(self, payload: Mapping[str, object]) -> bool:
        """仅允许白名单采样字段；任何敏感键或治理缺失一律拒绝。"""
        governed = (
            self.enabled
            and self.data_classification in {"public", "internal"}
            and bool(self.sampled_fields)
            and bool(self.region)
            and isinstance(self.retention_days, int) and self.retention_days > 0
            and bool(self.audit_permission)
            and self.privacy_purge_supported
        )
        if not governed:
            return False
        return all(
            isinstance(key, str)
            and key in self.sampled_fields
            and key not in self._FORBIDDEN_FIELDS
            for key in payload
        )


@dataclass(frozen=True)
class RuntimeObservabilityReport:
    """仅用于指标输出的安全聚合，不携带任意业务内容。"""

    evaluation_count: int
    evaluation_pass_rate: float
    fallback_count: int
    fallback_rate: float
    # model_cost 为兼容既有安全指标名称保留，值与 actual_model_cost 一致。
    model_cost: float
    actual_model_cost: float
    reserved_cost: float
    unknown_outcome_count: int
    tool_call_count: int
    model_attempt_count: int
    execution_attempt_count: int
    aborted_before_send_count: int
    model_elapsed_ms: int
    tool_elapsed_ms: int
    active_elapsed_ms: int
    schema_pass_rate: float
    grounding_pass_rate: float
    material_reference_pass_rate: float
    hallucination_rate: float
    emotional_safety_pass_rate: float

    @classmethod
    def from_counts(
        cls, *, evaluations: int, evaluation_passed: int, fallbacks: int,
        model_cost: float, reserved_cost: float, unknown_outcomes: int, tool_calls: int,
        model_attempts: int = 0, aborted_before_send: int = 0,
        model_elapsed_ms: int = 0, tool_elapsed_ms: int = 0, active_elapsed_ms: int = 0,
        schema_passed: int = 0, grounding_passed: int = 0, emotional_safety_passed: int = 0,
        actual_model_cost: float | None = None, execution_attempts: int = 0,
        material_reference_passed: int = 0, hallucinations: int = 0,
    ) -> RuntimeObservabilityReport:
        """由已脱敏的计数构建报告，非法负数输入按零处理。"""
        total = max(0, evaluations)
        actual_cost = max(0.0, model_cost if actual_model_cost is None else actual_model_cost)
        return cls(
            total, (max(0, min(evaluation_passed, total)) / total if total else 0.0),
            max(0, fallbacks), (max(0, min(fallbacks, total)) / total if total else 0.0),
            actual_cost, actual_cost, max(0.0, reserved_cost), max(0, unknown_outcomes),
            max(0, tool_calls), max(0, model_attempts), max(0, execution_attempts),
            max(0, aborted_before_send), max(0, model_elapsed_ms), max(0, tool_elapsed_ms),
            max(0, active_elapsed_ms),
            (max(0, min(schema_passed, total)) / total if total else 0.0),
            (max(0, min(grounding_passed, total)) / total if total else 0.0),
            (max(0, min(material_reference_passed, total)) / total if total else 0.0),
            (max(0, min(hallucinations, total)) / total if total else 0.0),
            (max(0, min(emotional_safety_passed, total)) / total if total else 0.0),
        )

    def as_dict(self) -> dict[str, int | float]:
        """导出固定白名单字段，供日志、指标或受治理 exporter 使用。"""
        return {
            "evaluation_count": self.evaluation_count,
            "evaluation_pass_rate": self.evaluation_pass_rate,
            "fallback_count": self.fallback_count,
            "fallback_rate": self.fallback_rate,
            "model_cost": self.model_cost,
            "actual_model_cost": self.actual_model_cost,
            "reserved_cost": self.reserved_cost,
            "unknown_outcome_count": self.unknown_outcome_count,
            "tool_call_count": self.tool_call_count,
            "model_attempt_count": self.model_attempt_count,
            "execution_attempt_count": self.execution_attempt_count,
            "aborted_before_send_count": self.aborted_before_send_count,
            "model_elapsed_ms": self.model_elapsed_ms,
            "tool_elapsed_ms": self.tool_elapsed_ms,
            "active_elapsed_ms": self.active_elapsed_ms,
            "schema_pass_rate": self.schema_pass_rate,
            "grounding_pass_rate": self.grounding_pass_rate,
            "material_reference_pass_rate": self.material_reference_pass_rate,
            "hallucination_rate": self.hallucination_rate,
            "emotional_safety_pass_rate": self.emotional_safety_pass_rate,
        }
