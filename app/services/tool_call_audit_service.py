"""副作用工具调用的最小持久审计服务。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.tools import ToolError
from app.models import AgentToolCall
from app.runtime.interfaces import LeaseContext
from app.runtime.policy_engine import ExecutionBudgetExceeded, PolicyEngine
from app.schemas.audit import RuntimeAuditEvent
from app.services.audit_service import AuditService
from app.services.lease_service import LeaseService


class ToolCallAuditService:
    """记录物理 attempt；逻辑键和幂等键由调用方在重试时保持不变。"""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._policy = PolicyEngine(session)
        self._audit = AuditService(session=session)

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
        return self._begin(
            run_id=run_id, execution_attempt=execution_attempt, step_id=step_id,
            tool_name=tool_name, tool_version=tool_version, transport=transport,
            logical_key=logical_key, idempotency_key=idempotency_key,
            request_digest=request_digest, input_summary=input_summary, side_effect=True,
        )

    def begin_native(
        self, *, run_id: str, execution_attempt: int, step_id: str,
        tool_name: str, logical_key: str, request_digest: str,
    ) -> AgentToolCall:
        """Native Tool 同样受冻结工具预算与无正文物理 attempt 审计约束。"""
        return self._begin(
            run_id=run_id, execution_attempt=execution_attempt, step_id=step_id,
            tool_name=tool_name, tool_version="1.0.0", transport="native",
            logical_key=logical_key, idempotency_key=logical_key,
            request_digest=request_digest, input_summary={"operation": tool_name}, side_effect=False,
        )

    def _begin(
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
        side_effect: bool,
    ) -> AgentToolCall:
        """写入物理 attempt；Native 与 HTTP 共用预算，但副作用语义不可混淆。"""
        try:
            self._policy.assert_tool_call_allowed(run_id, step_id)
        except ExecutionBudgetExceeded as exc:
            logging.warning("拒绝超出工具预算的调用 run_id=%s code=%s", run_id, exc.code)
            raise ValueError(exc.code) from None
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
            self._audit.append(RuntimeAuditEvent(
                audit_id=str(uuid4()), actor_type="system", actor_id="tool_gateway",
                action="tool_call_operation_conflict", resource_type="agent_run",
                resource_id=run_id, reason_code="TOOL_CALL_OPERATION_CONFLICT",
                outcome="rejected", occurred_at=datetime.now(UTC),
                metadata_summary={"run_id": run_id},
            ))
            self._session.commit()
            logging.warning(
                "拒绝冲突的工具逻辑操作 run_id=%s step_id=%s tool=%s",
                run_id, step_id, tool_name,
            )
            raise ValueError("TOOL_CALL_OPERATION_CONFLICT")

        created_at = datetime.now(UTC)
        record = AgentToolCall(
            tool_call_id=str(uuid4()), run_id=run_id, step_id=step_id,
            tool_name=tool_name, tool_version=tool_version, transport=transport,
            side_effect=side_effect, idempotency_key=idempotency_key,
            logical_operation_key=logical_key, request_digest=request_digest,
            execution_attempt=execution_attempt, tool_attempt=len(existing) + 1,
            status="running", input_summary=dict(input_summary), created_at=created_at,
            retention_until=created_at + timedelta(days=30),
        )
        self._session.add(record)
        self._session.flush()
        self._session.commit()
        logging.info(
            "写工具审计开始 run_id=%s tool_call_id=%s step_id=%s tool=%s",
            run_id, record.tool_call_id, step_id, tool_name,
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
        *, lease_context: LeaseContext | None = None,
    ) -> bool:
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
        persisted = self._persist_result(
            record, status="succeeded", output_summary=safe_output, lease_context=lease_context
        )
        if not persisted:
            return False
        logging.info("写工具审计成功 tool_call_id=%s tool=%s", record.tool_call_id, record.tool_name)
        return True

    def fail(
        self, record: AgentToolCall, error_code: str, *, retryable: bool,
        error_type: str = "tool_request_failed",
        lease_context: LeaseContext | None = None,
    ) -> bool:
        """记录只含受控字段的结构化错误，禁止错误正文进入数据库。"""
        safe_error = ToolError(
            error_code=error_code,
            error_type=error_type,
            retryable=retryable,
            safe_message="TOOL_REQUEST_REJECTED",
            details_visible_to_model=False,
        )
        persisted = self._persist_result(
            record,
            status="failed",
            error_code=error_code,
            output_summary=safe_error.model_dump(),
            lease_context=lease_context,
        )
        if not persisted:
            return False
        logging.warning("写工具审计失败 tool_call_id=%s code=%s retryable=%s", record.tool_call_id, error_code, retryable)
        return True

    def unknown(
        self, record: AgentToolCall, error_code: str, *, lease_context: LeaseContext | None = None,
    ) -> bool:
        """超时等无法判断业务端是否提交的情况必须保守标记。"""
        persisted = self._persist_result(
            record, status="outcome_unknown", error_code=error_code, lease_context=lease_context
        )
        if not persisted:
            return False
        logging.warning("写工具结果未知 tool_call_id=%s code=%s", record.tool_call_id, error_code)
        return True

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

    def find_publish_attempt(
        self, run_id: str, logical_key: str
    ) -> AgentToolCall | None:
        """按稳定 logical_key 查 publish 已提交/结果未知的 attempt，不依赖本次文档 digest。

        这是 publish query-after-commit 的**首要查询坐标**。memoir 在 resume 时模型
        会重算 scenes，让 playback_document 的 request_digest 漂移；若查询坐标带上
        本次 digest（见 ``latest_committed``），已提交的首轮 publish 会因 digest 不匹配
        而查不到，被迫重发 → 双发。本方法只按 run_id + logical_key 命中
        running/outcome_unknown/succeeded，让首轮权威提交总能被定位、对账复用。

        failed attempt 不返回：首次确定失败说明业务端未提交，不阻止后续按新 digest
        重新 begin_publish 写入。digest 冲突保护（同一 logical_key 被另一文档复用必须
        拒绝）留在 ``_begin``/runner 的 409 分支，与"按稳定键复用首次提交"彻底分离。
        """
        return self._session.scalar(
            select(AgentToolCall)
            .where(
                AgentToolCall.run_id == run_id,
                AgentToolCall.logical_operation_key == logical_key,
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
        lease_context: LeaseContext | None = None,
    ) -> bool:
        """在副作用返回后同步持久化脱敏终态。"""
        if lease_context is not None and not LeaseService(self._session).can_write(
            record.run_id, lease_context
        ):
            logging.warning(
                "迟到工具审计结算被执行边界拒绝 tool_call_id=%s code=TOOL_RESULT_LEASE_INVALID",
                record.tool_call_id,
            )
            return False
        record.status = status
        record.error_code = error_code
        record.output_summary = output_summary
        self._session.commit()
        return True
