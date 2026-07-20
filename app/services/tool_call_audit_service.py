"""副作用工具调用的最小持久审计服务。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentToolCall


class ToolCallAuditService:
    """记录物理 attempt；逻辑键和幂等键由调用方在重试时保持不变。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def begin_side_effect(
        self,
        *,
        run_id: str,
        execution_attempt: int,
        step_id: str,
        tool_name: str,
        tool_version: str | None,
        transport: str,
        logical_key: str,
        idempotency_key: str,
        request_digest: str,
        input_summary: Mapping[str, object],
    ) -> AgentToolCall:
        """在副作用请求发送前写入 ``running`` 审计记录。

        同一 Run 的逻辑操作可因 Worker 接管产生多条物理 attempt，但请求摘要与
        幂等键必须完全一致。这里提前拒绝漂移，避免重试意外执行另一项业务操作。
        调用方只能传入已脱敏的摘要，正文、prompt 和完整播放文档均不应进入本表。
        """
        existing = self._session.scalars(
            select(AgentToolCall).where(
                AgentToolCall.run_id == run_id,
                AgentToolCall.logical_operation_key == logical_key,
            )
        ).all()
        if any(
            item.idempotency_key != idempotency_key
            or item.request_digest != request_digest
            for item in existing
        ):
            logging.warning(
                "拒绝冲突的工具逻辑操作 run_id=%s step_id=%s tool=%s",
                run_id,
                step_id,
                tool_name,
            )
            raise ValueError("TOOL_CALL_OPERATION_CONFLICT")

        record = AgentToolCall(
            tool_call_id=str(uuid4()),
            run_id=run_id,
            step_id=step_id,
            tool_name=tool_name,
            tool_version=tool_version,
            transport=transport,
            side_effect=True,
            idempotency_key=idempotency_key,
            logical_operation_key=logical_key,
            request_digest=request_digest,
            execution_attempt=execution_attempt,
            status="running",
            input_summary=dict(input_summary),
            created_at=datetime.now(UTC),
        )
        self._session.add(record)
        self._session.flush()
        # 副作用发送前同步提交；复用 Worker Session，避免共享连接上的第二个
        # Session 回滚 Workflow ORM 状态。此处也是 publish 节点的安全提交边界。
        self._session.commit()
        logging.info(
            "写副作用工具审计开始 run_id=%s tool_call_id=%s step_id=%s tool=%s",
            run_id,
            record.tool_call_id,
            step_id,
            tool_name,
        )
        return record

    def begin_publish(
        self,
        run_id: str,
        execution_attempt: int,
        logical_key: str,
        idempotency_key: str,
        request_digest: str,
    ) -> AgentToolCall:
        """兼容发布节点：委托通用副作用审计，保持既有调用契约。"""
        return self.begin_side_effect(
            run_id=run_id,
            execution_attempt=execution_attempt,
            step_id="publish_document",
            tool_name="memory.publish_playback_document",
            tool_version="1.0.0",
            transport="http_business_tool",
            logical_key=logical_key,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            input_summary={"operation": "publish_playback_document"},
        )

    def succeed(
        self,
        record: AgentToolCall,
        output_summary: Mapping[str, object] | int,
        content_digest: str | None = None,
    ) -> None:
        """标记成功，只写入调用方提供的脱敏输出摘要。

        ``revision + content_digest`` 形态保留给已有发布节点，通用工具则直接传
        Mapping，避免为每个工具新建一套审计服务。
        """
        if isinstance(output_summary, int):
            if not isinstance(content_digest, str):
                raise ValueError("TOOL_CALL_SUCCESS_DIGEST_REQUIRED")
            safe_output = {"revision": output_summary, "content_digest": content_digest}
        else:
            safe_output = dict(output_summary)
        self._persist_result(record, status="succeeded", output_summary=safe_output)
        logging.info("写工具审计成功 tool_call_id=%s tool=%s", record.tool_call_id, record.tool_name)

    def fail(self, record: AgentToolCall, error_code: str, *, retryable: bool) -> None:
        """记录已确认失败；错误码可观测，错误正文禁止进入数据库。"""
        self._persist_result(
            record,
            status="failed",
            error_code=error_code,
            output_summary={"retryable": retryable},
        )
        logging.warning("写工具审计失败 tool_call_id=%s code=%s retryable=%s", record.tool_call_id, error_code, retryable)

    def unknown(self, record: AgentToolCall, error_code: str) -> None:
        """超时等无法判断业务端是否提交的情况必须保守标记。"""
        self._persist_result(
            record, status="outcome_unknown", error_code=error_code
        )
        logging.warning("写工具结果未知 tool_call_id=%s code=%s", record.tool_call_id, error_code)

    def latest_committed(
        self,
        run_id: str,
        logical_key: str,
        idempotency_key: str,
        request_digest: str,
    ) -> AgentToolCall | None:
        """用原逻辑键、幂等键和摘要查找必须先对账的已提交 attempt。"""
        return self._session.scalar(
            select(AgentToolCall)
            .where(
                AgentToolCall.run_id == run_id,
                AgentToolCall.logical_operation_key == logical_key,
                AgentToolCall.idempotency_key == idempotency_key,
                AgentToolCall.request_digest == request_digest,
                AgentToolCall.status.in_(
                    ("running", "outcome_unknown", "succeeded")
                ),
            )
            .order_by(AgentToolCall.id.desc())
        )

    def _persist_result(
        self,
        record: AgentToolCall,
        *,
        status: str,
        error_code: str | None = None,
        output_summary: dict[str, object] | None = None,
    ) -> None:
        """在副作用返回后同步持久化脱敏终态。"""
        record.status = status
        record.error_code = error_code
        record.output_summary = output_summary
        self._session.commit()
