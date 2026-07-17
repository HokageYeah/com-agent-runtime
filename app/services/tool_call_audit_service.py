"""副作用工具调用的最小持久审计服务。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentToolCall


class ToolCallAuditService:
    """记录物理 attempt；逻辑键和幂等键由调用方在重试时保持不变。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def begin_publish(self, run_id: str, execution_attempt: int, logical_key: str, idempotency_key: str, request_digest: str) -> AgentToolCall:
        """网络发送前写入 running，绝不保存完整作品正文。"""
        record = AgentToolCall(tool_call_id=str(uuid4()), run_id=run_id, step_id="publish_document", tool_name="memory.publish_playback_document", tool_version="1.0.0", transport="http_business_tool", side_effect=True, idempotency_key=idempotency_key, logical_operation_key=logical_key, request_digest=request_digest, execution_attempt=execution_attempt, status="running", input_summary={"operation": "publish_playback_document"}, created_at=datetime.now(UTC))
        self._session.add(record)
        self._session.flush()
        logging.info("写工具审计开始 run_id=%s tool_call_id=%s", run_id, record.tool_call_id)
        return record

    def succeed(self, record: AgentToolCall, revision: int, content_digest: str) -> None:
        """只保存 revision 与 digest，避免写入完整 document。"""
        record.status, record.output_summary = "succeeded", {"revision": revision, "content_digest": content_digest}
        logging.info("写工具审计成功 tool_call_id=%s revision=%s", record.tool_call_id, revision)

    def fail(self, record: AgentToolCall, error_code: str, *, retryable: bool) -> None:
        """记录已确认失败；错误码可观测，错误正文禁止进入数据库。"""
        record.status = "failed"
        record.error_code = error_code
        record.output_summary = {"retryable": retryable}
        logging.warning("写工具审计失败 tool_call_id=%s code=%s retryable=%s", record.tool_call_id, error_code, retryable)

    def unknown(self, record: AgentToolCall, error_code: str) -> None:
        """超时等无法判断业务端是否提交的情况必须保守标记。"""
        record.status = "outcome_unknown"
        record.error_code = error_code
        logging.warning("写工具结果未知 tool_call_id=%s code=%s", record.tool_call_id, error_code)

    def latest_unknown(self, run_id: str, logical_key: str) -> AgentToolCall | None:
        """查找同一逻辑操作最近的未知结果，供接管 Worker 优先对账。"""
        return self._session.scalar(select(AgentToolCall).where(
            AgentToolCall.run_id == run_id, AgentToolCall.logical_operation_key == logical_key,
            AgentToolCall.status == "outcome_unknown",
        ).order_by(AgentToolCall.id.desc()))
