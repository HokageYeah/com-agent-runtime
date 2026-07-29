"""Runtime P0 对账服务：只修复可由权威状态确定的异常。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    AdmissionBucket,
    AgentDefinition,
    AgentModelUsage,
    AgentPlan,
    AgentRun,
    AgentToolCall,
    RuntimeOutboxEvent,
)
from app.schemas.audit import RuntimeAuditEvent
from app.services.admission_service import AdmissionService
from app.services.agent_run_service import AgentRunService
from app.services.audit_service import AuditService
from app.services.idempotency_service import IdempotencyService
from app.services.lease_service import LeaseService
from app.services.memory_deletion_compensation_service import (
    MemoryDeletionMaintenanceReport,
)
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
    # 回忆录删除补偿只返回安全聚合数，不暴露 archive、Run 输入或播放内容。
    memory_deletion_delivered_events: int = 0
    memory_deletion_confirmed_purges: int = 0
    memory_deletion_deleted_revisions: int = 0
    memory_deletion_aborted: bool = False

    def as_dict(self) -> dict[str, int | bool | dict[str, int]]:
        """输出固定安全指标，禁止把扫描对象或私密 payload 放入日志。"""
        return {
            "scanned": self.scanned,
            "repaired": self.repaired,
            "dead_letter_callbacks": self.dead_letter_callbacks,
            "failures": self.failures,
            "alerts": self.alerts,
            "action_counts": dict(self.action_counts),
            "memory_deletion_delivered_events": self.memory_deletion_delivered_events,
            "memory_deletion_confirmed_purges": self.memory_deletion_confirmed_purges,
            "memory_deletion_deleted_revisions": self.memory_deletion_deleted_revisions,
            "memory_deletion_aborted": self.memory_deletion_aborted,
        }

    def with_memory_deletion_maintenance(
        self, maintenance: MemoryDeletionMaintenanceReport
    ) -> ReconciliationReport:
        """合并同一租约窗口的回忆录删除维护安全计数。"""
        return replace(
            self,
            memory_deletion_delivered_events=maintenance.delivered_events,
            memory_deletion_confirmed_purges=maintenance.confirmed_purges,
            memory_deletion_deleted_revisions=maintenance.deleted_revisions,
            memory_deletion_aborted=maintenance.aborted,
        )


class ReconciliationService:
    """按 Run 与已注册 Definition 的权威状态安全修复滞留任务。"""

    # 未知副作用不能凭超时直接重放；留给原逻辑键/idempotency key 的业务查询接管。
    _TOOL_CALL_RUNNING_TIMEOUT = timedelta(minutes=5)

    def __init__(self, session: Session) -> None:
        self._session = session
        self._outbox = OutboxService(session)
        self._audit = AuditService(session=session)
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
        authorization_version_resolver: Callable[[AgentRun], int | None] | None = None,
    ) -> ReconciliationReport:
        """修复超时、过期 lease 与确定的 outbox 死信，不读取私密载荷。"""
        current = now or datetime.now(UTC)
        self._action_counts = {}
        self._alerts = 0
        if not self._has_lease(lease_guard):
            return self._aborted_report()
        # expires_at 是幂等响应的审计保留截止线；purge 还必须由服务确认
        # 对应 Run 已完成物理清理，才能随常规记录一起删除。
        expired_idempotency = IdempotencyService(self._session).cleanup_expired(current)
        repaired = expired_idempotency
        if expired_idempotency:
            self._record_action("idempotency_expired_deleted", expired_idempotency)
        expired_usage_count = len(
            self._session.scalars(
                select(AgentModelUsage.usage_id).where(
                    # started 表示 Worker 已取得发送权；崩溃后同样可能已有上游副作用。
                    AgentModelUsage.status.in_(("running", "started")),
                    AgentModelUsage.request_deadline_at.is_not(None),
                    AgentModelUsage.request_deadline_at < current,
                )
            ).all()
        )
        expired_usage_repaired = ModelUsageService(
            self._session
        ).mark_expired_running_unknown(current)
        repaired += expired_usage_repaired
        if expired_usage_repaired:
            self._record_action("model_usage_outcome_unknown", expired_usage_repaired)
        stale_tools = self._mark_stale_tool_calls_unknown(current)
        repaired += stale_tools
        if stale_tools:
            self._record_action("tool_call_outcome_unknown", stale_tools)
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
        for run in runs:
            if not self._has_lease(lease_guard):
                return self._abort_scan()
            # purge 先由 API 建立版本写屏障；终态 Run 没有在途执行者后才可在
            # 对账事务中物理清理，避免清理与 Worker 的迟到写入相互覆盖。
            if (
                run.privacy_state == "purge_requested"
                and run.dispatch_state == "finished"
                and AgentRunService(self._session).complete_purge(run.run_id)
            ):
                repaired += 1
                self._record_action("privacy_purge_completed")
                continue
            if run.dispatch_state in {"held", "queued", "claimed"} or (
                run.status == "waiting_human" and run.dispatch_state == "finished"
            ):
                scanned_run_ids.add(run.run_id)
            if self._repair_run_deadline(run, current):
                repaired += 1
                continue
            if self._repair_active_elapsed_timeout(run, current):
                repaired += 1
                continue
            if self._repair_authorization_changed(
                run, current, authorization_version_resolver
            ):
                repaired += 1
                continue
            if self._repair_revoked_definition(run, current):
                repaired += 1
                continue
            if self._repair_held_timeout(run, current):
                repaired += 1
                continue
            if self._repair_queued_timeout(run, current):
                repaired += 1
                continue
            if self._repair_missing_run_dispatch(run, current):
                repaired += 1
                self._record_action("run_dispatch_repaired")
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
        if dead_letter_callbacks:
            # 仅输出聚合提示；原 CallbackEvent/outbox 身份保持不变供管理员重放。
            self._record_action("callback_reconciliation_needed", dead_letter_callbacks)
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
            report.as_dict(),
        )
        return report

    def _mark_stale_tool_calls_unknown(self, current: datetime) -> int:
        """把超过安全窗口的运行中工具调用标未知，禁止盲目重放副作用。"""
        stale_before = current - self._TOOL_CALL_RUNNING_TIMEOUT
        updated = self._session.execute(
            update(AgentToolCall)
            .where(
                AgentToolCall.status == "running",
                AgentToolCall.created_at < stale_before,
            )
            .values(status="outcome_unknown", error_code="TOOL_CALL_TIMEOUT")
            .execution_options(synchronize_session=False)
        )
        return updated.rowcount  # type: ignore[attr-defined]

    @staticmethod
    def _has_lease(lease_guard: Callable[[], bool] | None) -> bool:
        return lease_guard is None or lease_guard()

    @staticmethod
    def _aborted_report() -> ReconciliationReport:
        return ReconciliationReport(
            scanned=0, repaired=0, dead_letter_callbacks=0, failures=0
        )

    def _record_action(self, action: str, count: int = 1) -> None:
        self._action_counts[action] = self._action_counts.get(action, 0) + count

    @staticmethod
    def _failure_key(action: str, object_key: str) -> str:
        """失败历史按权威对象隔离，绝不把对象标识输出到日志或报告。"""
        return f"{action}:{object_key}"

    def _record_failure(self, action: str, *, object_key: str) -> None:
        key = self._failure_key(action, object_key)
        streak = self._failure_streaks.get(key, 0) + 1
        self._failure_streaks[key] = streak
        # 只在首次跨过阈值时升级，避免第四轮起每次扫描重复告警。
        if streak == 3:
            self._alerts += 1
            logging.warning(
                "reconciler_warning action=%s consecutive_failures=%s", action, streak
            )

    def _record_success(self, action: str, *, object_key: str) -> None:
        self._failure_streaks.pop(self._failure_key(action, object_key), None)

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
            counts = cast(
                tuple[int, int, int],
                tuple(expected.get((bucket.scope_type, bucket.scope_key), [0, 0, 0])),
            )
            actual = (bucket.held_count, bucket.queued_count, bucket.running_count)
            if actual == counts:
                continue
            if self._repair_admission_bucket(bucket, counts):
                repaired += 1
                self._record_success("admission_bucket", object_key=str(bucket.id))
            else:
                failures += 1
                self._record_failure("admission_bucket", object_key=str(bucket.id))
        return repaired, failures

    def _repair_admission_bucket(
        self, bucket: AdmissionBucket, counts: tuple[int, int, int]
    ) -> bool:
        repaired = self._session.execute(
            update(AdmissionBucket)
            .where(
                AdmissionBucket.id == bucket.id,
                AdmissionBucket.version == bucket.version,
            )
            .values(
                held_count=counts[0],
                queued_count=counts[1],
                running_count=counts[2],
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
                logging.warning(
                    "对账请求取消已撤销 Package 的 claimed run_id=%s", run.run_id
                )
                return True
        return False

    def _repair_authorization_changed(
        self,
        run: AgentRun,
        current: datetime,
        resolver: Callable[[AgentRun], int | None] | None,
    ) -> bool:
        """仅依据业务注入的权威版本撤销旧 Run，未配置解析器时不做猜测。"""
        if resolver is None:
            return False
        current_version = resolver(run)
        if current_version is None or current_version == run.authorization_version:
            return False
        if run.dispatch_state in {"held", "queued"} or (
            run.status == "waiting_human" and run.dispatch_state == "finished"
        ):
            if self._terminate(run, current, "cancelled", "AUTHORIZATION_CHANGED"):
                self._append_authorization_audit(run, current, "terminated")
                self._record_action("authorization_changed_terminated")
                return True
            return False
        if run.dispatch_state == "claimed" and run.cancel_requested_at is None:
            requested = self._session.execute(
                update(AgentRun)
                .where(
                    AgentRun.run_id == run.run_id,
                    AgentRun.status_version == run.status_version,
                    AgentRun.dispatch_state == "claimed",
                    AgentRun.cancel_requested_at.is_(None),
                )
                .values(cancel_requested_at=current)
                .execution_options(synchronize_session=False)
            )
            if requested.rowcount == 1:  # type: ignore[attr-defined]
                self._session.refresh(run)
                self._append_authorization_audit(run, current, "cancel_requested")
                self._record_action("authorization_changed_cancel_requested")
                logging.warning("对账请求取消授权已变化的 run_id=%s", run.run_id)
                return True
        return False

    def _append_authorization_audit(
        self, run: AgentRun, current: datetime, outcome: str
    ) -> None:
        """授权版本变化只记录受控结论，禁止将权限或业务载荷写入审计。"""
        self._audit.append(
            RuntimeAuditEvent(
                audit_id=str(uuid4()),
                actor_type="system",
                actor_id="reconciler",
                action="agent_run_authorization_changed",
                resource_type="agent_run",
                resource_id=run.run_id,
                reason_code="AUTHORIZATION_CHANGED",
                outcome=outcome,
                occurred_at=current,
                trace_id=run.trace_id,
                metadata_summary={"status": run.status},
            )
        )

    def _repair_held_timeout(self, run: AgentRun, current: datetime) -> bool:
        if run.status != "pending" or run.dispatch_state != "held":
            return False
        if not self._is_expired(run.held_expires_at, current):
            return False
        if not self._terminate(run, current, "cancelled", "HELD_TIMEOUT"):
            return False
        logging.warning("对账终结 held 超时 run_id=%s", run.run_id)
        return True

    def _repair_run_deadline(self, run: AgentRun, current: datetime) -> bool:
        """wall-clock 到期后不再允许执行；claimed Run 只请求安全边界停止。"""
        deadline = self._as_utc(run.run_deadline_at)
        if deadline is None or deadline > current:
            return False
        return self._apply_execution_timeout(
            run, current, "RUN_DEADLINE_EXCEEDED", "run_deadline"
        )

    def _repair_active_elapsed_timeout(self, run: AgentRun, current: datetime) -> bool:
        """累计 active 时间只由执行边界记账，排队和人工等待不消耗该额度。"""
        plan = self._session.scalar(
            select(AgentPlan).where(AgentPlan.run_id == run.run_id)
        )
        limit = plan.stop_conditions_json.get("max_run_seconds") if plan else None
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 0
            or run.active_elapsed_ms < limit * 1000
        ):
            return False
        return self._apply_execution_timeout(
            run, current, "ACTIVE_TIME_LIMIT_EXCEEDED", "active_time_limit"
        )

    def _apply_execution_timeout(
        self, run: AgentRun, current: datetime, error_code: str, action: str
    ) -> bool:
        """未被 Worker 持有的 Run 可终结；claimed Run 只能留下取消请求。"""
        if run.status in {"succeeded", "partial", "failed", "cancelled"}:
            return False
        if run.dispatch_state == "claimed":
            requested = self._session.execute(
                update(AgentRun)
                .where(
                    AgentRun.run_id == run.run_id,
                    AgentRun.status == run.status,
                    AgentRun.dispatch_state == "claimed",
                    AgentRun.status_version == run.status_version,
                    AgentRun.cancel_requested_at.is_(None),
                )
                .values(cancel_requested_at=current, error_code=error_code)
                .execution_options(synchronize_session=False)
            )
            if requested.rowcount != 1:  # type: ignore[attr-defined]
                return False
            self._session.refresh(run)
            self._record_action(f"{action}_cancel_requested")
            logging.warning(
                "对账请求执行超时取消 run_id=%s code=%s", run.run_id, error_code
            )
            return True
        if run.dispatch_state not in {"held", "queued", "finished"}:
            return False
        if run.dispatch_state == "finished" and run.status != "waiting_human":
            return False
        if not self._terminate(run, current, "failed", error_code):
            return False
        self._record_action(f"{action}_terminated")
        logging.warning("对账终结执行超时 run_id=%s code=%s", run.run_id, error_code)
        return True

    def _repair_queued_timeout(self, run: AgentRun, current: datetime) -> bool:
        if run.status != "pending" or run.dispatch_state != "queued":
            return False
        plan = self._session.scalar(
            select(AgentPlan).where(AgentPlan.run_id == run.run_id)
        )
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

    def _repair_missing_run_dispatch(self, run: AgentRun, current: datetime) -> bool:
        """修复 queued Run 的 dispatch 投递缺口，绝不创建第二个既有事件身份。"""
        if run.status != "pending" or run.dispatch_state != "queued":
            return False
        events = list(
            self._session.scalars(
                select(RuntimeOutboxEvent)
                .where(
                    RuntimeOutboxEvent.event_type == "run_dispatch",
                    RuntimeOutboxEvent.aggregate_id == run.run_id,
                )
                .order_by(RuntimeOutboxEvent.id)
            ).all()
        )
        # pending/有效 delivering 均仍由 dispatcher 负责。dead letter 则由下方终止
        # 分支收敛，不能擅自绕过投递上限重放。
        if any(event.status in {"pending", "dead_letter"} for event in events):
            return False
        active_delivery = False
        for event in events:
            lease_expires_at = self._as_utc(event.lease_expires_at)
            if (
                event.status == "delivering"
                and lease_expires_at is not None
                and lease_expires_at > current
            ):
                active_delivery = True
                break
        if active_delivery:
            return False
        if not events:
            self._outbox.append_run_dispatch(
                run.run_id, "reconciliation_missing_dispatch"
            )
            logging.warning("对账补建缺失 run_dispatch run_id=%s", run.run_id)
            return True
        # Dispatcher 已标 delivered 但进程在 Worker claim 前退出，或 delivering lease
        # 已过期时，只把原 outbox 恢复为 pending，保持其 outbox_id 与 payload 不变。
        event = events[-1]
        replay_condition = RuntimeOutboxEvent.status == "delivered"
        if event.status == "delivering":
            replay_condition = RuntimeOutboxEvent.status == "delivering"
            if event.lease_expires_at is None:
                replay_condition = (
                    replay_condition & RuntimeOutboxEvent.lease_expires_at.is_(None)
                )
            else:
                replay_condition = replay_condition & (
                    RuntimeOutboxEvent.lease_expires_at == event.lease_expires_at
                )
        replayed = self._session.execute(
            update(RuntimeOutboxEvent)
            .where(RuntimeOutboxEvent.outbox_id == event.outbox_id, replay_condition)
            .values(
                status="pending",
                next_attempt_at=None,
                lease_owner=None,
                lease_expires_at=None,
                delivered_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if replayed.rowcount != 1:  # type: ignore[attr-defined]
            return False
        self._session.expire(event)
        logging.warning("对账重放原 run_dispatch outbox run_id=%s", run.run_id)
        return True

    def _repair_waiting_human_timeout(self, run: AgentRun, current: datetime) -> bool:
        if run.status != "waiting_human" or run.dispatch_state != "finished":
            return False
        if not self._is_expired(run.waiting_expires_at, current):
            return False
        if self._timeout_action(run) == "fallback" and self._has_valid_fallback_target(
            run
        ):
            repaired = self._requeue_timeout_fallback(run, current)
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
        plan = self._session.scalar(
            select(AgentPlan).where(AgentPlan.run_id == run.run_id)
        )
        policy = plan.fallback_policy_json if plan is not None else {}
        action = (
            policy.get("waiting_human_timeout_action")
            if isinstance(policy, dict)
            else None
        )
        return action if action in {"fallback", "cancelled", "failed"} else None

    def _timeout_terminal_status(self, run: AgentRun) -> str:
        action = self._timeout_action(run)
        if action == "cancelled":
            return "cancelled"
        return "failed"

    def _has_valid_fallback_target(self, run: AgentRun) -> bool:
        plan = self._session.scalar(
            select(AgentPlan).where(AgentPlan.run_id == run.run_id)
        )
        if plan is None:
            return False
        target = plan.fallback_policy_json.get("waiting_human_fallback_node")
        return isinstance(target, str) and target in {
            node.get("node_id") for node in plan.steps_json if isinstance(node, dict)
        }

    def _terminate_timeout(
        self, run: AgentRun, current: datetime, error_code: str
    ) -> bool:
        return self._terminate(
            run, current, self._timeout_terminal_status(run), error_code
        )

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

    def _requeue_timeout_fallback(self, run: AgentRun, current: datetime) -> bool:
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
                queued_at=current,
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
