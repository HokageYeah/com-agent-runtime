"""模型调用的只读、确定性预算准入。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models import AgentModelUsage
    from app.runtime.model_gateway import ModelCallContext, ModelRoute


@dataclass(frozen=True)
class PolicyDecision:
    """只返回受控 code，调用方不得从中获得请求或用量正文。"""

    allowed: bool
    code: str | None = None


class PolicyEngine:
    """仅依据权威 Run 快照和同 Run 的 usage 账本判断模型调用。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def evaluate(self, context: ModelCallContext, route: ModelRoute) -> PolicyDecision:
        # Settings 初始化会先加载 ModelRoute；ORM 必须延迟到真正评估时再导入。
        from app.models import AgentModelUsage, AgentRun

        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == context.run_id))
        if run is None or not self.context_is_authoritative(context, run=run):
            return PolicyDecision(False, "MODEL_RUN_NOT_EXECUTABLE")
        return self._evaluate_budget(run, context, route, AgentModelUsage)

    def context_is_authoritative(self, context: ModelCallContext, *, run: object | None = None) -> bool:
        """回读 Run/Step；陈旧 context 不得预留、取 permit 或触发 Provider。"""
        from app.models import AgentRun, AgentStep

        self._session.expire_all()
        authoritative_run = run or self._session.scalar(
            select(AgentRun).where(AgentRun.run_id == context.run_id)
        )
        step = self._session.scalar(
            select(AgentStep).where(
                AgentStep.step_id == context.step_id,
                AgentStep.run_id == context.run_id,
            )
        )
        return bool(
            authoritative_run is not None
            and self._run_is_executable(authoritative_run, context)
            and step is not None
            and step.status == "running"
            and step.execution_attempt == context.lease_context.execution_attempt
            and step.step_attempt == context.model_attempt
        )

    def reserve(self, context: ModelCallContext, route: ModelRoute) -> tuple[PolicyDecision, str | None]:
        """原子提交 usage 预留，令并发调用也能看见已占用的预算。"""
        from app.models import AgentModelUsage, AgentRun, AgentStep

        # 条件更新 Run 的版本把“读预算 + 写预留”串行化；预留先提交，不能把
        # 外部 Provider 调用放在数据库事务内，也不能让另一个 Session 看见旧账本。
        for _ in range(2):
            self._session.expire_all()
            run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == context.run_id))
            if run is None or not self.context_is_authoritative(context, run=run):
                return PolicyDecision(False, "MODEL_RUN_NOT_EXECUTABLE"), None
            decision = self._evaluate_budget(run, context, route, AgentModelUsage)
            if not decision.allowed:
                return decision, None
            guarded = self._session.execute(
                update(AgentRun)
                .where(
                    AgentRun.run_id == run.run_id,
                    AgentRun.status_version == run.status_version,
                    AgentRun.dispatch_state == "claimed",
                    AgentRun.cancel_requested_at.is_(None),
                    AgentRun.privacy_state == "active",
                    AgentRun.privacy_version == context.lease_context.privacy_version,
                    AgentRun.authorization_version == context.lease_context.authorization_version,
                    AgentRun.execution_attempt == context.lease_context.execution_attempt,
                    AgentRun.lease_owner == context.lease_context.lease_owner,
                    AgentRun.fencing_token == context.lease_context.fencing_token,
                    exists(select(AgentStep.id).where(
                        AgentStep.step_id == context.step_id,
                        AgentStep.run_id == context.run_id,
                        AgentStep.status == "running",
                        AgentStep.execution_attempt == context.lease_context.execution_attempt,
                        AgentStep.step_attempt == context.model_attempt,
                    )),
                )
                .values(status_version=AgentRun.status_version + 1)
                .execution_options(synchronize_session=False)
            )
            if guarded.rowcount != 1:  # type: ignore[attr-defined]
                self._session.rollback()
                continue
            reserved_cost = route.input_price * context.estimated_input_tokens / 1000
            usage_id = str(uuid4())
            self._session.add(AgentModelUsage(
                id=uuid4().int >> 65,
                usage_id=usage_id,
                run_id=run.run_id,
                step_id=context.step_id,
                execution_attempt=context.lease_context.execution_attempt,
                model_attempt=context.model_attempt,
                status="reserved",
                permit_id=None,
                capability_snapshot_json=None,
                prompt_id=None,
                prompt_version=None,
                provider=route.provider,
                model=route.model,
                pricing_config_version=None,
                cost_unit=route.price_unit,
                reserved_estimated_cost=reserved_cost,
                input_tokens=None,
                output_tokens=None,
                request_deadline_at=context.request_deadline_at,
            ))
            self._session.commit()
            return PolicyDecision(True), usage_id
        return PolicyDecision(False, "MODEL_RUN_NOT_EXECUTABLE"), None

    def _evaluate_budget(
        self, run: object, context: ModelCallContext, route: ModelRoute, usage_model: object,
    ) -> PolicyDecision:
        """只汇总同一 Run 的持久 usage；预留行也必须算入额度。"""
        # 运行时导入模型使 Settings 加载路径保持无 ORM 导入环。
        AgentModelUsage = usage_model
        snapshot = run.capability_snapshot_json
        policy = snapshot.get("model_policy") if isinstance(snapshot, Mapping) else None
        policy = policy if isinstance(policy, Mapping) else {}
        max_calls = self._nonnegative_int(policy.get("max_model_calls"))
        if max_calls is not None:
            usages = self._session.scalars(
                select(AgentModelUsage).where(AgentModelUsage.run_id == run.run_id)
            ).all()
            if len(usages) >= max_calls:
                return PolicyDecision(False, "MODEL_CALL_LIMIT_EXCEEDED")
        max_cost = self._nonnegative_finite(policy.get("max_model_cost"))
        if max_cost is not None:
            usages = self._session.scalars(
                select(AgentModelUsage).where(AgentModelUsage.run_id == run.run_id)
            ).all()
            used_cost = sum(self._usage_cost(usage) for usage in usages)
            reserved_cost = route.input_price * context.estimated_input_tokens / 1000
            if used_cost + reserved_cost >= max_cost:
                return PolicyDecision(False, "MODEL_COST_LIMIT_EXCEEDED")
        return PolicyDecision(True)

    @staticmethod
    def _run_is_executable(run: object, context: ModelCallContext) -> bool:
        """每次 permit 前回读 Run 的取消、隐私、授权和 lease/fencing 屏障。"""
        lease = context.lease_context
        expires_at = run.lease_expires_at
        return bool(
            run.dispatch_state == "claimed"
            and run.status not in {"cancelled", "failed", "succeeded", "partial", "waiting_human"}
            and run.cancel_requested_at is None
            and run.privacy_state == "active"
            and run.privacy_version == lease.privacy_version
            and run.authorization_version == lease.authorization_version
            and run.execution_attempt == lease.execution_attempt
            and run.lease_owner == lease.lease_owner
            and run.fencing_token == lease.fencing_token
            and expires_at is not None
            and PolicyEngine._as_utc(expires_at) > datetime.now(UTC)
            and PolicyEngine._as_utc(lease.lease_expires_at) > datetime.now(UTC)
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _nonnegative_int(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def _nonnegative_finite(value: object) -> float | None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            return None
        return float(value)

    @classmethod
    def _usage_cost(cls, usage: AgentModelUsage) -> float:
        """优先实际/估算成本，缺失或畸形时保守回退到预留成本。"""
        estimated = cls._nonnegative_finite(usage.estimated_cost)
        if estimated is not None:
            return estimated
        reserved = cls._nonnegative_finite(usage.reserved_estimated_cost)
        return reserved if reserved is not None else 0.0
