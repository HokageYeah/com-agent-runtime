from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.logging_uru import log_success
from app.models import AgentDefinition, AgentRun
from app.runtime.interfaces import AgentRunResult, LeaseContext
from app.services.admission_service import AdmissionService
from app.services.outbox_service import OutboxService


def _as_utc(value: datetime) -> datetime:
    """SQLite 会丢失 timezone；持久化时间一律按 UTC 解释。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class LeaseService:
    """数据库 fencing 是单写者真相；Redis 通知丢失不能影响本服务的正确性。"""

    def __init__(self, session: Session, lease_seconds: int = 90) -> None:
        self._session, self._lease_seconds = session, lease_seconds

    def claim(self, run_id: str, owner: str) -> LeaseContext | None:
        """条件更新是 Worker 所有权唯一真相，重复通知不会造成双认领。"""
        now = datetime.now(UTC)
        claimed = self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.run_id == run_id,
                AgentRun.dispatch_state == "queued",
                AgentRun.status.in_(("pending", "waiting_human")),
                AgentRun.cancel_requested_at.is_(None),
            )
            .values(
                dispatch_state="claimed",
                lease_owner=owner,
                execution_attempt=AgentRun.execution_attempt + 1,
                fencing_token=AgentRun.fencing_token + 1,
                lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                claimed_at=now,
            )
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            return None
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        assert run is not None
        assert run.lease_expires_at is not None
        AdmissionService(self._session).transition_run(run, "queued", "claimed")
        self._session.commit()
        logging.info(
            "Worker lease claim run_id=%s owner=%s fencing=%s",
            run_id,
            owner,
            run.fencing_token,
        )
        return LeaseContext(
            execution_attempt=run.execution_attempt,
            lease_owner=owner,
            fencing_token=run.fencing_token,
            lease_expires_at=run.lease_expires_at,
            privacy_version=run.privacy_version,
            authorization_version=run.authorization_version,
        )

    def heartbeat(self, run_id: str, context: LeaseContext) -> bool:
        now = datetime.now(UTC)
        renewed_until = now + timedelta(seconds=self._lease_seconds)
        renewed = self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.run_id == run_id,
                AgentRun.dispatch_state == "claimed",
                AgentRun.lease_owner == context.lease_owner,
                AgentRun.fencing_token == context.fencing_token,
                AgentRun.execution_attempt == context.execution_attempt,
                AgentRun.cancel_requested_at.is_(None),
                AgentRun.privacy_state == "active",
                AgentRun.privacy_version == context.privacy_version,
                AgentRun.authorization_version == context.authorization_version,
                AgentRun.lease_expires_at > now,
                AgentRun.run_deadline_at > now,
            )
            .values(lease_expires_at=renewed_until)
            .execution_options(synchronize_session=False)
        )
        if renewed.rowcount != 1:  # type: ignore[attr-defined]
            logging.warning("Worker heartbeat 被 fencing 拒绝 run_id=%s", run_id)
            return False
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        assert run is not None
        self._session.commit()
        context.lease_expires_at = renewed_until
        return True

    def can_write(self, run_id: str, context: LeaseContext) -> bool:
        """统一 fencing/privacy/authorization/cancel 写前检查。

        Executor、Checkpoint 和 Tool/Artifact 服务在写入前复用本方法；旧 Worker
        即使在 reaper 接管后恢复，也不能再推进任何 Runtime 状态。
        """
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        now = datetime.now(UTC)
        allowed = bool(
            run
            and run.dispatch_state == "claimed"
            and run.lease_owner == context.lease_owner
            and run.fencing_token == context.fencing_token
            and run.privacy_state == "active"
            and run.privacy_version == context.privacy_version
            and run.authorization_version == context.authorization_version
            and run.cancel_requested_at is None
            and run.lease_expires_at is not None
            and _as_utc(run.lease_expires_at) > now
            and _as_utc(context.lease_expires_at) > now
            and _as_utc(run.run_deadline_at) > now
            # Package 缺失、撤销或与 Run 冻结 digest 漂移时，所有复用
            # can_write 的 checkpoint/artifact/tool/model 写入一律停止。
            and self._package_executable(run)
        )
        if not allowed:
            logging.warning("Worker 写入被 fencing/状态边界拒绝 run_id=%s", run_id)
        return allowed

    def _package_executable(self, run: AgentRun) -> bool:
        """与 Run 冻结身份匹配且仍 active 的 Package 才可执行。

        can_write 与 reaper 共享本谓词，确保“不可执行 Package”在写闸门和失联接管
        两条路径上判定一致：缺失/废弃/digest 漂移均视为不可执行。
        """
        definition = self._session.scalar(
            select(AgentDefinition).where(
                AgentDefinition.agent_id == run.agent_id,
                AgentDefinition.version == run.agent_version,
            )
        )
        return (
            definition is not None
            and definition.status == "active"
            and definition.package_digest == run.package_digest
        )

    def finish(self, result: AgentRunResult, context: LeaseContext) -> bool:
        """仅有效 lease 能终结 Run，并在同一事务释放 Admission 与写 callback。"""
        terminal_states = {"succeeded", "partial", "failed", "cancelled"}
        if result.status not in terminal_states or not self.can_write(result.run_id, context):
            return False
        run = self._session.scalar(
            select(AgentRun).where(AgentRun.run_id == result.run_id)
        )
        assert run is not None
        run.status = result.status
        run.dispatch_state = "finished"
        run.lease_owner = None
        run.lease_expires_at = None
        run.finished_at = datetime.now(UTC)
        run.status_version += 1
        run.error_code = result.error_code
        AdmissionService(self._session).transition_run(run, "claimed", "finished")
        OutboxService(self._session).append_callback(run, result.status)
        self._session.commit()
        # 关键节点：Run 终态按结果分级显示——succeeded 绿、failed 红、
        # partial/cancelled 黄，控制台一眼分辨本次 Run 的最终结局。
        if run.status == "succeeded":
            log_success("Worker 终结 Run run_id=%s status=%s", run.run_id, run.status)
        elif run.status == "failed":
            logging.error("Worker 终结 Run run_id=%s status=%s", run.run_id, run.status)
        else:
            logging.warning("Worker 终结 Run run_id=%s status=%s", run.run_id, run.status)
        return True

    def release_for_drain(self, run_id: str, context: LeaseContext) -> bool:
        """优雅停机到达安全边界后让当前 lease 到期，reaper 再原子接管。"""
        if not self.can_write(run_id, context):
            return False
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        assert run is not None
        run.lease_expires_at = datetime.now(UTC)
        self._session.commit()
        logging.warning("draining 主动释放 lease run_id=%s", run_id)
        return True

    def finish_after_invalid_boundary(self, run_id: str, context: LeaseContext) -> bool:
        """在 cancel/purge 已使旧结果失效后，按 fencing 条件释放 claimed 归属。

        这不是对旧 Worker 结果的采纳：仅允许与原 lease 完全匹配且已发生
        cancel/privacy 屏障的 Run 进入 cancelled/finished，避免 90 秒租约阻塞
        purge。之后任何迟到 checkpoint、artifact 或工具结算仍会被 can_write 拒绝。
        """
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        if (
            run is None
            or run.dispatch_state != "claimed"
            or run.lease_owner != context.lease_owner
            or run.fencing_token != context.fencing_token
            or run.execution_attempt != context.execution_attempt
            or (run.cancel_requested_at is None and run.privacy_state == "active")
        ):
            return False
        run.status = "cancelled"
        run.dispatch_state = "finished"
        run.lease_owner = None
        run.lease_expires_at = None
        run.finished_at = datetime.now(UTC)
        run.status_version += 1
        AdmissionService(self._session).transition_run(run, "claimed", "finished")
        OutboxService(self._session).append_callback(run, "cancelled")
        self._session.commit()
        logging.warning("旧 lease 在取消/清理边界后释放 run_id=%s", run_id)
        return True

    def pause_for_human(
        self, run_id: str, context: LeaseContext, timeout_seconds: int
    ) -> bool:
        """在 checkpoint 已落库后原子进入人工等待，并释放 Worker 与 Admission 占用。"""
        if timeout_seconds <= 0 or not self.can_write(run_id, context):
            return False
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        assert run is not None
        run.status = "waiting_human"
        run.dispatch_state = "finished"
        run.waiting_expires_at = datetime.now(UTC) + timedelta(seconds=timeout_seconds)
        run.lease_owner = None
        run.lease_expires_at = None
        run.status_version += 1
        AdmissionService(self._session).transition_run(run, "claimed", "finished")
        OutboxService(self._session).append_callback_event(run, "waiting_human")
        self._session.commit()
        logging.info(
            "Worker 进入人工等待 run_id=%s timeout_seconds=%s",
            run_id,
            timeout_seconds,
        )
        return True

    def reap_expired(self, *, commit: bool = True) -> list[str]:
        """回收失效 lease 并回到 queued；旧 fencing token 之后不可再写入。

        Package 已不可执行（缺失/废弃/digest 漂移）的失联 Run 不再重新分发，直接按
        PACKAGE_REVOKED 终结，与 can_write 共享的 Package 谓词收敛，避免 worker 直连
        reap 时给已停止的 Package 产生 ``lease_reaped`` 假分发与无谓 execution_attempt
        抖动。
        """
        now = datetime.now(UTC)
        recovered: list[str] = []
        for run in self._session.scalars(
            select(AgentRun).where(
                AgentRun.dispatch_state == "claimed", AgentRun.lease_expires_at < now
            )
        ).all():
            if not self._package_executable(run):
                # 不可执行 Package 的失联 Run 不回 queued，直接以 PACKAGE_REVOKED 终结，
                # 释放 lease 并写 cancelled 回调；fencing 条件与 requeue 路径一致。
                terminated = self._session.execute(
                    update(AgentRun)
                    .where(
                        AgentRun.run_id == run.run_id,
                        AgentRun.status == run.status,
                        AgentRun.dispatch_state == "claimed",
                        AgentRun.lease_expires_at < now,
                    )
                    .execution_options(synchronize_session=False)
                    .values(
                        status="cancelled",
                        dispatch_state="finished",
                        lease_owner=None,
                        lease_expires_at=None,
                        finished_at=now,
                        status_version=AgentRun.status_version + 1,
                        error_code="PACKAGE_REVOKED",
                    )
                )
                if terminated.rowcount != 1:  # type: ignore[attr-defined]
                    continue
                self._session.refresh(run)
                AdmissionService(self._session).transition_run(run, "claimed", "finished")
                OutboxService(self._session).append_callback(run, "cancelled")
                logging.warning(
                    "reaper 终结不可执行 Package 的失联 Run run_id=%s", run.run_id
                )
                continue
            # `running/planning/evaluating` 不是 queued Run 的可认领状态；失联
            # 接管必须回到 pending，由下一 execution attempt 从安全恢复点重新执行。
            next_status = (
                "pending"
                if run.status in {"planning", "running", "evaluating"}
                else run.status
            )
            reaped = self._session.execute(
                update(AgentRun)
                .where(
                    AgentRun.run_id == run.run_id,
                    AgentRun.status == run.status,
                    AgentRun.dispatch_state == "claimed",
                    AgentRun.lease_expires_at < now,
                )
                .execution_options(synchronize_session=False)
                .values(
                    status=next_status,
                    dispatch_state="queued",
                    lease_owner=None,
                    lease_expires_at=None,
                    queued_at=now,
                    status_version=(
                        AgentRun.status_version + 1
                        if next_status != run.status
                        else AgentRun.status_version
                    ),
                )
            )
            if reaped.rowcount != 1:  # type: ignore[attr-defined]
                continue
            self._session.refresh(run)
            AdmissionService(self._session).transition_run(run, "claimed", "queued")
            OutboxService(self._session).append_run_dispatch(run.run_id, "lease_reaped")
            recovered.append(run.run_id)
            logging.warning("reaper 回收过期 Worker lease run_id=%s", run.run_id)
        if commit:
            self._session.commit()
        return recovered
