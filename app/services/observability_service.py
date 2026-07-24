"""从权威账本聚合不含内容的 Runtime 观测指标。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentEvaluation, AgentModelUsage, AgentRun, AgentToolCall
from app.runtime.observability import RuntimeObservabilityReport


class ObservabilityService:
    """只读取评价、用量和工具的安全状态/计量字段。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def report_for_run(self, run_id: str) -> RuntimeObservabilityReport:
        """按 Run 聚合指标，禁止读取输入、输出、prompt 或工具载荷。"""
        evaluations = list(self._session.scalars(select(AgentEvaluation).where(AgentEvaluation.run_id == run_id)))
        usages = list(self._session.scalars(select(AgentModelUsage).where(AgentModelUsage.run_id == run_id)))
        tools = list(self._session.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run_id)))
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        # 只有已观察到输入、输出 token 的 attempt 才是实际成本；超时或崩溃不伪造。
        actual_cost = sum(
            float(item.estimated_cost or 0)
            for item in usages
            if item.input_tokens is not None and item.output_tokens is not None
        )
        # 预留成本只表示仍可能已发出的调用，已结算和发送前取消不重复累计。
        reserved = sum(
            float(item.reserved_estimated_cost or 0)
            for item in usages
            if item.status in {"running", "started", "outcome_unknown"}
        )
        return RuntimeObservabilityReport.from_counts(
            evaluations=len(evaluations),
            evaluation_passed=sum(item.decision == "pass" for item in evaluations),
            fallbacks=sum(item.decision == "fallback" for item in evaluations),
            model_cost=actual_cost,
            actual_model_cost=actual_cost,
            reserved_cost=reserved,
            unknown_outcomes=sum(item.status == "outcome_unknown" for item in usages),
            tool_calls=len(tools),
            model_attempts=len(usages),
            execution_attempts=len({item.execution_attempt for item in usages}),
            aborted_before_send=sum(item.status == "aborted_before_send" for item in usages),
            # 仅使用 ORM 时间戳差值，不读取 provider 返回或任何模型内容。
            model_elapsed_ms=sum(
                max(0, int((item.updated_at - item.created_at).total_seconds() * 1000))
                for item in usages if item.updated_at and item.created_at
            ),
            tool_elapsed_ms=sum(max(0, item.duration_ms or 0) for item in tools),
            active_elapsed_ms=max(0, run.active_elapsed_ms) if run is not None else 0,
            schema_passed=sum(item.schema_passed is True for item in evaluations),
            grounding_passed=sum(item.grounding_passed is True for item in evaluations),
            material_reference_passed=sum(
                item.material_reference_passed is True for item in evaluations
            ),
            hallucinations=sum(item.hallucination_detected is True for item in evaluations),
            emotional_safety_passed=sum(item.emotional_safety_passed is True for item in evaluations),
        )
