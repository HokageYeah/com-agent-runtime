"""Runtime P0 对账服务：只修复可由权威状态确定的异常。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    AdmissionBucket,
    AgentDefinition,
    AgentModelUsage,
    AgentPlan,
    AgentRun,
    RuntimeOutboxEvent,
)
from app.services.admission_service import AdmissionService
from app.services.lease_service import LeaseService
from app.services.model_usage_service import ModelUsageService
from app.services.outbox_service import OutboxService


@dataclass(frozen=True)
class ReconciliationReport:
    """单次对账的安全统计，不携带快照、callback 正文或模型数据。"""

    scanned: int
    repaired: int
    dead_letter_callbacks: int
    failures: int
    action_counts: dict[str, int] = field(default_factory=dict)
    alerts: int = 0


class ReconciliationService:
    """按 Run 与已注册 Definition 的权威状态安全修复滞留任务。"""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._outbox = OutboxService(session)
        self._failure_streaks: dict[str, int] = {}
        self._action_counts: dict[str, int] = {}
        self._alerts = 0

    def set_failure_streaks(self, failure_streaks: dict[str, int]) -> None:
        """由常驻 Runner 保存跨轮的安全失败计数。"""
        self._failure_streaks = failure_streaks

    def run_once(
        self,
        now: datetime | None = None,
        *,
        lease_guard: Callable[[], bool] | None = None,
    ) -> ReconciliationReport:
        """修复超时、过期 lease 与确定的 outbox 死信，不读取私密载荷。"""
        current = now or datetime.now(UTC)
        self._action_counts = {}
        self._alerts = 0
        if not self._has_lease(lease_guard):
            return self._aborted_report()
        expired_usage_count = len(self._session.scalars(
            select(AgentModelUsage.usage_id).where(
                AgentModelUsage.status == "running",
                AgentModelUsage.request_deadline_at.is_not(None),
                AgentModelUsage.request_deadline_at < current,
            )
        ).all())
        repaired = ModelUsageService(self._session).mark_expired_running_unknown(current)
        if repaired:
            self._record_action("model_usage_outcome_unknown", repaired)
        reaped_run_ids = LeaseService(self._session).reap_expired(commit=False)
        runs = list(self._session.scalars(select(AgentRun)))
        scanned_run_ids = set(reaped_run_ids)
        repaired += len(reaped_run_ids)
        # 在本轮其他修复前读取权威 dispatch 快照，避免把本轮刚创建的
        # Admission bucket 误认成历史漂移再次计入修复。
        admission_repaired, admission_failures = self._reconcile_admission_buckets(runs)
        repaired += admission_repaired
        if admission_repaired:
            self._record_action("admission_bucket_repaired", admission_repaired)
        if admission_failures:
            self._record_failure("admission_bucket", admission_failures)
        for run in runs:
            if not self._has_lease(lease_guard):
                return self._abort_scan()
            if run.dispatch_state in {"held", "queued", "claimed"} or (
                run.status == "waiting_human" and run.dispatch_state == "finished"
            ):
                scanned_run_ids.add(run.run_id)
            if self._repair_revoked_definition(run, current):
                repaired += 1
                continue
            if self._repair_held_timeout(run, current):
                repaired += 1
                continue
            if self._repair_queued_timeout(run, current):
                repaired += 1
                continue
            if self._repair_waiting_human_timeout(run, current):
                repaired += 1

        for event in self._session.scalars(
            select(RuntimeOutboxEvent).where(
                RuntimeOutboxEvent.event_type == "run_dispatch",
                RuntimeOutboxEvent.status == "dead_letter",
            )
        ):
            if not self._has_lease(lease_guard):
                return self._abort_scan()
            run = self._session.scalar(
                select(AgentRun).where(AgentRun.run_id == event.aggregate_id)
            )
            if run is None:
                continue
            scanned_run_ids.add(run.run_id)
            # 一个 dead dispatch 只能终结同一 aggregate 仍未被 Worker 接管的 Run。
            if run.status == "pending" and run.dispatch_state == "queued":
                if self._terminate(run, current, "failed", "DISPATCH_FAILED"):
                    repaired += 1
                    logging.warning("对账终结 dispatch 死信 run_id=%s", run.run_id)
        if not self._has_lease(lease_guard):
            return self._abort_scan()
        dead_letter_callbacks = len(
            self._session.scalars(
                select(RuntimeOutboxEvent.outbox_id).where(
                    RuntimeOutboxEvent.event_type == "callback",
                    RuntimeOutboxEvent.status == "dead_letter",
                )
            ).all()
        )
        self._session.commit()
        report = ReconciliationReport(
            scanned=len(scanned_run_ids) + expired_usage_count,
            repaired=repaired,
            dead_letter_callbacks=dead_letter_callbacks,
            failures=admission_failures,
            action_counts=dict(self._action_counts),
            alerts=self._alerts,
        )
        logging.info(
            "P0 对账完成 scanned=%s repaired=%s callback_dead_letter=%s failures=%s alerts=%s actions=%s",
            report.scanned,
            report.repaired,
            report.dead_letter_callbacks,
            report.failures,
            report.alerts,
            report.action_counts,
        )
        return report

    @staticmethod
    def _has_lease(lease_guard: Callable[[], bool] | None) -> bool:
        return lease_guard is None or lease_guard()

    @staticmethod
    def _aborted_report() -> ReconciliationReport:
        return ReconciliationReport(scanned=0, repaired=0, dead_letter_callbacks=0, failures=0)

    def _record_action(self, action: str, count: int = 1) -> None:
        self._action_counts[action] = self._action_counts.get(action, 0) + count

    def _record_failure(self, action: str, count: int = 1) -> None:
        streak = self._failure_streaks.get(action, 0) + count
        self._failure_streaks[action] = streak
        if streak >= 3:
            self._alerts += 1
            logging.warning(
                "reconciler_warning action=%s consecutive_failures=%s", action, streak
            )

    def _record_success(self, action: str) -> None:
        self._failure_streaks.pop(action, None)

    def _reconcile_admission_buckets(self, runs: list[AgentRun]) -> tuple[int, int]:
        """以 Run 的 dispatch_state 重建已有 bucket；版本冲突时不覆盖新写入。"""
        expected: dict[tuple[str, str], list[int]] = {}
        state_index = {"held": 0, "queued": 1, "claimed": 2}
        for run in runs:
            index = state_index.get(run.dispatch_state)
            if index is None:
                continue
            for scope in (
                ("global", "*"),
                ("caller", run.caller_id),
                ("tenant", run.tenant_id),
                ("agent", run.agent_id),
            ):
                expected.setdefault(scope, [0, 0, 0])[index] += 1

        repaired = failures = 0
        for bucket in self._session.scalars(select(AdmissionBucket)).all():
            counts = tuple(expected.get((bucket.scope_type, bucket.scope_key), [0, 0, 0]))
            actual = (bucket.held_count, bucket.queued_count, bucket.running_count)
            if actual == counts:
                continue
            if self._repair_admission_bucket(bucket, counts):
                repaired += 1
            else:
                failures += 1
        if repaired:
            self._record_success("admission_bucket")
        return repaired, failures

    def _repair_admission_bucket(
        self, bucket: AdmissionBucket, counts: tuple[int, int, int]
    ) -> bool:
        repaired = self._session.execute(
            update(AdmissionBucket)
            .where(AdmissionBucket.id == bucket.id, AdmissionBucket.version == bucket.version)
            .values(
                held_count=counts[0], queued_count=counts[1], running_count=counts[2],
                version=AdmissionBucket.version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if repaired.rowcount != 1:  # type: ignore[attr-defined]
            # 条件更新落败后，此 identity-map 实体仍是冲突前的快照。后续同轮
            # Run 修复会经 AdmissionService 再次取用它；先失效，确保不会 flush
            # 陈旧计数覆盖并发 dispatch 迁移。
            self._session.expire(bucket)
            return False
        self._session.expire(bucket)
        return True

    def _abort_scan(self) -> ReconciliationReport:
        """失租后丢弃本 Session 尚未提交的扫描写入。"""
        self._session.rollback()
        logging.warning("对账扫描已中止 operation=lease_lost")
        return self._aborted_report()

    def _repair_revoked_definition(self, run: AgentRun, current: datetime) -> bool:
        """Definition 已撤销是权威事实；claimed Run 只能请求 Worker 安全收敛。"""
        definition = self._session.scalar(
            select(AgentDefinition).where(
                AgentDefinition.agent_id == run.agent_id,
                AgentDefinition.version == run.agent_version,
            )
        )
        if definition is None or definition.status != "revoked":
            return False
        if run.dispatch_state in {"held", "queued"} or (
            run.status == "waiting_human" and run.dispatch_state == "finished"
        ):
            if self._terminate(run, current, "cancelled", "PACKAGE_REVOKED"):
                logging.warning("对账终结已撤销 Package 的 run_id=%s", run.run_id)
                return True
            return False
        if run.dispatch_state == "claimed" and run.cancel_requested_at is None:
            requested = self._session.execute(
                update(AgentRun)
                .where(
                    AgentRun.run_id == run.run_id,
                    AgentRun.status == run.status,
                    AgentRun.dispatch_state == "claimed",
                    AgentRun.status_version == run.status_version,
                    AgentRun.cancel_requested_at.is_(None),
                )
                .execution_options(synchronize_session=False)
                .values(cancel_requested_at=current)
            )
            if requested.rowcount == 1:  # type: ignore[attr-defined]
                self._session.refresh(run)
                logging.warning("对账请求取消已撤销 Package 的 claimed run_id=%s", run.run_id)
                return True
        return False

    def _repair_held_timeout(self, run: AgentRun, current: datetime) -> bool:
        if run.status != "pending" or run.dispatch_state != "held":
            return False
        if not self._is_expired(run.held_expires_at, current):
            return False
        if not self._terminate(run, current, "cancelled", "HELD_TIMEOUT"):
            return False
        logging.warning("对账终结 held 超时 run_id=%s", run.run_id)
        return True

    def _repair_queued_timeout(self, run: AgentRun, current: datetime) -> bool:
        if run.status != "pending" or run.dispatch_state != "queued":
            return False
        plan = self._session.scalar(select(AgentPlan).where(AgentPlan.run_id == run.run_id))
        ttl = plan.stop_conditions_json.get("queue_ttl_seconds") if plan else None
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            return False
        queued_at = self._as_utc(run.queued_at)
        if queued_at is None or queued_at + timedelta(seconds=ttl) > current:
            return False
        if not self._terminate(run, current, "failed", "QUEUE_TIMEOUT"):
            return False
        logging.warning("对账终结 queued 超时 run_id=%s", run.run_id)
        return True

    def _repair_waiting_human_timeout(self, run: AgentRun, current: datetime) -> bool:
        if run.status != "waiting_human" or run.dispatch_state != "finished":
            return False
        if not self._is_expired(run.waiting_expires_at, current):
            return False
        if self._timeout_action(run) == "fallback" and self._has_valid_fallback_target(run):
            repaired = self._requeue_timeout_fallback(run)
        elif self._timeout_action(run) == "fallback":
            repaired = self._terminate_timeout(run, current, "FALLBACK_NODE_INVALID")
        else:
            repaired = self._terminate_timeout(run, current, "WAITING_HUMAN_TIMEOUT")
        if not repaired:
            return False
        logging.warning("对账修复人工等待超时 run_id=%s", run.run_id)
        return True

    def _timeout_action(self, run: AgentRun) -> str | None:
        """只读取与 Run 同事务冻结的 Plan；缺失或非法策略安全降级失败。"""
        plan = self._session.scalar(select(AgentPlan).where(AgentPlan.run_id == run.run_id))
        policy = plan.fallback_policy_json if plan is not None else {}
        action = policy.get("waiting_human_timeout_action") if isinstance(policy, dict) else None
        return action if action in {"fallback", "cancelled", "failed"} else None

    def _timeout_terminal_status(self, run: AgentRun) -> str:
        action = self._timeout_action(run)
        if action == "cancelled":
            return "cancelled"
        return "failed"

    def _has_valid_fallback_target(self, run: AgentRun) -> bool:
        plan = self._session.scalar(select(AgentPlan).where(AgentPlan.run_id == run.run_id))
        if plan is None:
            return False
        target = plan.fallback_policy_json.get("waiting_human_fallback_node")
        return isinstance(target, str) and target in {
            node.get("node_id")
            for node in plan.steps_json
            if isinstance(node, dict)
        }

    def _terminate_timeout(self, run: AgentRun, current: datetime, error_code: str) -> bool:
        return self._terminate(run, current, self._timeout_terminal_status(run), error_code)

    def _terminate(
        self, run: AgentRun, current: datetime, status: str, error_code: str
    ) -> bool:
        previous_dispatch_state = run.dispatch_state
        terminated = self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.run_id == run.run_id,
                AgentRun.status == run.status,
                AgentRun.dispatch_state == previous_dispatch_state,
                AgentRun.status_version == run.status_version,
            )
            .execution_options(synchronize_session=False)
            .values(
                status=status,
                dispatch_state="finished",
                status_version=AgentRun.status_version + 1,
                error_code=error_code,
                finished_at=current,
            )
        )
        if terminated.rowcount != 1:  # type: ignore[attr-defined]
            return False
        self._session.refresh(run)
        AdmissionService(self._session).transition_run(
            run, previous_dispatch_state, "finished"
        )
        self._outbox.append_callback(run, status)
        return True

    def _requeue_timeout_fallback(self, run: AgentRun) -> bool:
        """保持 waiting_human，使 Worker 在新 lease 下走 resume 而不是 run。"""
        requeued = self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.run_id == run.run_id,
                AgentRun.status == run.status,
                AgentRun.dispatch_state == "finished",
                AgentRun.status_version == run.status_version,
            )
            .execution_options(synchronize_session=False)
            .values(
                dispatch_state="queued",
                waiting_expires_at=None,
                error_code="WAITING_HUMAN_FALLBACK",
                status_version=AgentRun.status_version + 1,
            )
        )
        if requeued.rowcount != 1:  # type: ignore[attr-defined]
            return False
        self._session.refresh(run)
        AdmissionService(self._session).transition_run(run, "finished", "queued")
        self._outbox.append_run_dispatch(run.run_id, "waiting_human_timeout_fallback")
        return True

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    def _is_expired(self, value: datetime | None, current: datetime) -> bool:
        expires_at = self._as_utc(value)
        return expires_at is not None and expires_at <= current
