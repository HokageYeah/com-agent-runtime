"""将确定性评价结果以最小安全摘要写入 Runtime 审计账本。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import AgentEvaluation
from app.schemas.evaluation import EvaluationDecisionDTO


class EvaluationService:
    """负责 `AgentEvaluation` 持久化，不保存播放文档、素材正文或 prompt。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self, *, run_id: str, step_id: str | None, target_type: str, target_id: str | None,
        evaluator_type: str, evaluation: EvaluationDecisionDTO,
    ) -> str:
        """持久化一次评价；reason 只允许受控错误码，避免正文泄漏到审计表。"""
        evaluation_id = str(uuid4())
        reason_summary = ",".join(evaluation.reasons) if evaluation.reasons else "OK"
        record = AgentEvaluation(
            # SQLite 对 BIGINT 主键不生成 rowid，主动提供安全的正整数主键。
            id=uuid4().int >> 65,
            evaluation_id=evaluation_id,
            run_id=run_id,
            step_id=step_id,
            target_type=target_type,
            target_id=target_id,
            evaluator_type=evaluator_type,
            score_json=evaluation.safe_summary,
            decision=evaluation.decision,
            reason_summary=reason_summary[:500],
            schema_passed=evaluation.scores.get("structure") == 1,
            grounding_passed=evaluation.scores.get("grounding") == 1,
            # UNKNOWN_SOURCE_REF 是冻结素材集外引用的唯一受控信号；不读取引用值。
            material_reference_passed=evaluation.scores.get("grounding") == 1,
            hallucination_detected="UNKNOWN_SOURCE_REF" in evaluation.reasons,
            emotional_safety_passed=not any(
                code in {"EMOTIONAL_LANGUAGE_BLOCKED", "SENSITIVE_IDENTIFIER_BLOCKED"}
                for code in evaluation.reasons
            ),
            created_at=datetime.now(UTC),
        )
        self._session.add(record)
        self._session.flush()
        logging.info("Runtime 评价已记录 run_id=%s decision=%s evaluator=%s", run_id, evaluation.decision, evaluator_type)
        return evaluation_id
