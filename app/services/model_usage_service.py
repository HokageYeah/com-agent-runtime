"""安全的模型调用用量账本；绝不保存 prompt 或 Provider 响应正文。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import AgentModelUsage, AgentRun, AgentStep
from app.runtime.model_gateway import ModelCallContext, ModelRoute


class ModelUsageService:
    """为每一次物理 Provider attempt 保存最小且可审计的计量事实。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_running(
        self, context: ModelCallContext, route: ModelRoute, permit_id: str
    ) -> str:
        if not permit_id:
            raise ValueError("permit_id 不能为空")
        # Context 不是数据库事实来源。至少将 run ownership 与 execution attempt
        # 回读权威 Run，避免自由构造的上下文生成伪造 usage 账本。
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == context.run_id))
        step = self._session.scalar(
            select(AgentStep).where(
                AgentStep.step_id == context.step_id,
                AgentStep.run_id == context.run_id,
            )
        )
        if (
            run is None
            or step is None
            or step.status != "running"
            or step.execution_attempt != context.lease_context.execution_attempt
            or step.step_attempt != context.model_attempt
            or run.execution_attempt != context.lease_context.execution_attempt
            or run.lease_owner != context.lease_context.lease_owner
            or run.fencing_token != context.lease_context.fencing_token
        ):
            raise ValueError("MODEL_CALL_CONTEXT_UNTRUSTED")
        summary = step.input_summary if isinstance(step.input_summary, dict) else {}
        expected_tokens = summary.get("estimated_input_tokens", 0)
        if isinstance(expected_tokens, bool) or not isinstance(expected_tokens, int) or expected_tokens < 0:
            expected_tokens = 0
        if (
            context.estimated_input_tokens != expected_tokens
            or context.request_deadline_at != run.run_deadline_at
            or context.allowed_route_ids
            != ModelCallContext.allowed_routes_from_snapshot(run.capability_snapshot_json)
            or route.route_id not in context.allowed_route_ids
        ):
            raise ValueError("MODEL_CALL_CONTEXT_UNTRUSTED")
        reserved = context.estimated_input_tokens * route.input_price / 1000
        usage = AgentModelUsage(
            # SQLite 只有 INTEGER PRIMARY KEY 才映射 rowid；生产 MySQL 可自增，
            # 但服务端主动提供随机 63-bit ID 能保持两端一致且不依赖方言行为。
            id=uuid4().int >> 65,
            usage_id=str(uuid4()),
            run_id=context.run_id,
            step_id=context.step_id,
            execution_attempt=context.lease_context.execution_attempt,
            model_attempt=context.model_attempt,
            status="running",
            permit_id=permit_id,
            # 调用上下文可能携带运行期私密素材；usage 账本只保存可审计的
            # 路由与计量事实，绝不能把 capability/prompt 元数据当作可信输入落库。
            capability_snapshot_json=None,
            prompt_id=None,
            prompt_version=None,
            provider=route.provider,
            model=route.model,
            pricing_config_version=None,
            cost_unit=route.price_unit,
            reserved_estimated_cost=reserved,
            input_tokens=None,
            output_tokens=None,
            request_deadline_at=context.request_deadline_at,
        )
        self._session.add(usage)
        self._session.flush()
        return usage.usage_id

    def mark_started(self, usage_id: str) -> bool:
        transitioned = self._session.execute(
            update(AgentModelUsage)
            .where(
                AgentModelUsage.usage_id == usage_id,
                AgentModelUsage.status == "running",
            )
            .values(status="started")
            .execution_options(synchronize_session=False)
        )
        self._session.flush()
        return transitioned.rowcount == 1  # type: ignore[attr-defined]

    def activate_reservation(self, usage_id: str, permit_id: str) -> bool:
        """permit 已获批后才将已提交的预算预留转为实际 in-flight usage。"""
        transitioned = self._session.execute(
            update(AgentModelUsage)
            .where(AgentModelUsage.usage_id == usage_id, AgentModelUsage.status == "reserved")
            .values(status="running", permit_id=permit_id)
            .execution_options(synchronize_session=False)
        )
        self._session.flush()
        return transitioned.rowcount == 1  # type: ignore[attr-defined]

    def attach_prompt_ref(self, usage_id: str, prompt_id: str, prompt_version: str) -> bool:
        """只给尚未发送的预留 attempt 写入版本引用，绝不保存模板正文。"""
        if not prompt_id or not prompt_version:
            raise ValueError("prompt_id 与 prompt_version 不能为空")
        updated = self._session.execute(
            update(AgentModelUsage)
            .where(AgentModelUsage.usage_id == usage_id, AgentModelUsage.status == "reserved")
            .values(prompt_id=prompt_id, prompt_version=prompt_version)
            .execution_options(synchronize_session=False)
        )
        self._session.flush()
        return updated.rowcount == 1  # type: ignore[attr-defined]

    def cancel_reservation(self, usage_id: str) -> bool:
        """未获 permit 的预留没有物理调用，安全删除以释放预算。"""
        deleted = self._session.execute(
            delete(AgentModelUsage).where(
                AgentModelUsage.usage_id == usage_id,
                AgentModelUsage.status == "reserved",
            )
        )
        self._session.commit()
        return deleted.rowcount == 1  # type: ignore[attr-defined]

    def settle(
        self,
        usage_id: str,
        status: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        route: ModelRoute | None = None,
    ) -> str:
        if input_tokens is not None and input_tokens < 0:
            raise ValueError("input_tokens 不可为负")
        if output_tokens is not None and output_tokens < 0:
            raise ValueError("output_tokens 不可为负")
        if status == "aborted_before_send":
            cost = 0.0
        elif status == "succeeded" and input_tokens is not None and output_tokens is not None:
            if route is None:
                raise ValueError("成功结算必须提供冻结的 route")
            cost = (input_tokens * route.input_price + output_tokens * route.output_price) / 1000
        else:
            # 超时/网络中断不能伪造成免费调用，保留已预留的保守成本。
            usage = self._get(usage_id)
            cost = usage.reserved_estimated_cost
        # 条件 UPDATE 是跨 worker 的最终裁决：只有第一个 running/started
        # 结算者能写入结果，后到者不会覆盖 outcome 或成本。
        settled = self._session.execute(
            update(AgentModelUsage)
            .where(
                AgentModelUsage.usage_id == usage_id,
                AgentModelUsage.status.in_(("running", "started")),
            )
            .values(
                status=status,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=cost,
            )
            .execution_options(synchronize_session=False)
        )
        self._session.flush()
        return "settled" if settled.rowcount == 1 else "already_settled"  # type: ignore[attr-defined]

    def mark_expired_running_unknown(self, now: datetime | None = None) -> int:
        """供后续 Reconciler 使用：仅将过期 in-flight 调用标为未知结果。"""
        current = now or datetime.now(UTC)
        expired = self._session.execute(
            update(AgentModelUsage)
            .where(
                AgentModelUsage.status == "running",
                AgentModelUsage.request_deadline_at.is_not(None),
                AgentModelUsage.request_deadline_at < current,
            )
            .values(
                status="outcome_unknown",
                estimated_cost=AgentModelUsage.reserved_estimated_cost,
            )
            .execution_options(synchronize_session=False)
        )
        self._session.flush()
        return expired.rowcount  # type: ignore[attr-defined]

    def _get(self, usage_id: str) -> AgentModelUsage:
        usage = self._session.scalar(
            select(AgentModelUsage).where(AgentModelUsage.usage_id == usage_id)
        )
        if usage is None:
            raise ValueError("MODEL_USAGE_NOT_FOUND")
        return usage
