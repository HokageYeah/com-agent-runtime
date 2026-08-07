from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import (
    AgentArtifact,
    AgentCheckpoint,
    AgentDefinition,
    AgentModelUsage,
    AgentPlan,
    AgentRun,
    AgentStep,
    AgentToolCall,
    RuntimeOutboxEvent,
)
from app.runtime.planner import StaticPlanner
from app.schemas.agent_package import PackagePolicy
from app.schemas.agent_run import CreateRunCommand, RunDetail, RunSummary, StepSummary
from app.schemas.audit import RuntimeAuditEvent
from app.services.admission_service import AdmissionLimits, AdmissionService
from app.services.audit_service import AuditService
from app.services.outbox_service import OutboxService


class AgentRunServiceError(ValueError):
    """面向 API 的安全状态错误；调用方不能从消息推断私密数据。"""


class AgentRunService:
    """held/start 生命周期服务；HTTP 层只负责认证与 DTO 转换。"""

    def __init__(
        self,
        session: Session,
        admission_limits: AdmissionLimits | None = None,
        *,
        trusted_model_route_ids: Iterable[str] = (),
        required_model_data_residency: str | None = None,
        authorization_version_resolver: Callable[[AgentRun], int | None] | None = None,
    ) -> None:
        self._session = session
        self._outbox = OutboxService(session)
        self._admission = AdmissionService(session, admission_limits)
        self._audit = AuditService(session=session)
        # 只接受应用启动时已验证的服务端 route registry；请求 command/input
        # 绝不能参与 capability 冻结或扩大权限。
        self._trusted_model_route_ids = tuple(
            sorted({route_id for route_id in trusted_model_route_ids if isinstance(route_id, str) and route_id})
        )
        if required_model_data_residency not in {None, "public", "private"}:
            raise ValueError("required_model_data_residency 非法")
        self._required_model_data_residency = required_model_data_residency
        self._authorization_version_resolver = authorization_version_resolver

    def create(
        self,
        command: CreateRunCommand,
        caller_id: str,
        tenant_id: str,
        idempotency_key: str,
        *,
        authorization_version: int = 1,
    ) -> RunSummary:
        definition = self._session.scalar(
            select(AgentDefinition).where(
                AgentDefinition.agent_id == command.agent_id,
                AgentDefinition.version == command.agent_version,
            )
        )
        if definition is None or definition.status != "active":
            raise AgentRunServiceError("AgentPackage 不可用于创建 Run")
        self._validate_input_schema(
            definition.definition_json.get("input_schema"), command.input
        )
        allowed = definition.definition_json.get("allowed_business_types", [])
        if allowed and command.business_type not in allowed:
            raise AgentRunServiceError("business_type 未获 AgentPackage 授权")
        run_id = str(uuid4())
        now = datetime.now(UTC)
        # definition 是注册后的权威来源；请求 input 不得扩大或伪造模型额度。
        definition_json = definition.definition_json
        definition_policy = (
            definition_json.get("policy", {}) if isinstance(definition_json, dict) else {}
        )
        policy = PackagePolicy.model_validate(definition_policy)
        model_policy = {
            key: value
            for key, value in {
                "max_model_calls": policy.max_model_calls,
                "max_model_cost": policy.max_model_cost,
                "max_tokens": policy.max_tokens,
            }.items()
            if value is not None
        }
        execution_policy = {
            key: value
            for key, value in {
                "max_steps": policy.max_steps,
                "max_tool_calls": policy.max_tool_calls,
                "max_run_seconds": policy.max_run_seconds,
                "max_auto_retry_per_step": policy.max_auto_retry_per_step,
            }.items()
            if value is not None
        }
        if isinstance(authorization_version, bool) or authorization_version < 1:
            raise AgentRunServiceError("authorization_version 非法")
        run = AgentRun(
            run_id=run_id,
            agent_id=command.agent_id,
            agent_version=command.agent_version,
            package_digest=definition.package_digest,
            contract_version=definition.contract_version,
            business_type=command.business_type,
            business_id=command.business_id,
            status="pending",
            dispatch_state="held" if command.start_mode == "held" else "queued",
            input_json=command.input,
            # 冻结创建时已验证的受信任能力身份；不记录 endpoint、密钥或请求正文。
            capability_snapshot_json={
                "agent_id": definition.agent_id,
                "agent_version": definition.version,
                "contract_version": definition.contract_version,
                "package_digest": definition.package_digest,
                "business_connector_id": command.business_connector_id,
                "allowed_model_route_ids": list(self._trusted_model_route_ids),
                **(
                    {
                        "required_model_data_residency": self._required_model_data_residency
                    }
                    if self._required_model_data_residency is not None
                    else {}
                ),
                "model_policy": model_policy,
                "execution_policy": execution_policy,
                # 注册 Package 时持久化的 ui-trace 是唯一可信公开投影策略，Run 创建后冻结。
                "ui_trace": definition.definition_json.get("ui_trace", {"mode": "status_only"}),
            },
            authorization_version=authorization_version,
            caller_id=caller_id,
            tenant_id=tenant_id,
            create_idempotency_key=idempotency_key,
            callback_target_id=command.callback_target_id,
            business_connector_id=command.business_connector_id,
            trace_id=str(uuid4()),
            held_expires_at=now + timedelta(minutes=10)
            if command.start_mode == "held"
            else None,
            queued_at=now if command.start_mode == "auto" else None,
            run_deadline_at=now + timedelta(days=1),
        )
        self._session.add(run)
        self._admission.transition_run(run, "none", run.dispatch_state)
        # 静态计划与 Run 同事务落库，不能让后续 Worker 看到无计划的 queued Run。
        plan = StaticPlanner().create_plan_from_definition(run_id, definition.definition_json)
        StaticPlanner().persist(self._session, plan)
        if command.start_mode == "auto":
            self._outbox.append_run_dispatch(run_id, "auto_create")
        logging.info(
            "创建 AgentRun run_id=%s start_mode=%s caller=%s",
            run_id,
            command.start_mode,
            caller_id,
        )
        return self._summary(run)

    def start(
        self,
        run_id: str,
        caller_id: str,
        idempotency_key: str,
        expected_status_version: int | None = None,
    ) -> RunSummary:
        run = self._owned_run(run_id, caller_id)
        self._assert_authorization_current(run)
        # 外部调用方提供版本时必须条件校验，避免旧 held 握手误启动新状态。
        if (
            expected_status_version is not None
            and run.status_version != expected_status_version
        ):
            raise AgentRunServiceError("状态版本冲突")
        definition = self._session.scalar(
            select(AgentDefinition).where(
                AgentDefinition.agent_id == run.agent_id,
                AgentDefinition.version == run.agent_version,
            )
        )
        if definition is None or definition.status == "revoked":
            raise AgentRunServiceError("AgentPackage 已撤销，禁止 start")
        if run.privacy_state != "active" or run.cancel_requested_at is not None:
            raise AgentRunServiceError("Run 已取消或处于私密数据清理状态")
        if run.dispatch_state in {"queued", "claimed"}:
            return self._summary(run)
        if run.dispatch_state != "held" or run.status != "pending":
            raise AgentRunServiceError("Run 当前状态不允许 start")
        # SQLite 会返回 naive datetime；生产库返回 aware datetime，比较前统一到 UTC。
        expires_at = run.held_expires_at
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at and expires_at < datetime.now(UTC):
            raise AgentRunServiceError("held Run 已过期")
        run.dispatch_state = "queued"
        run.queued_at = datetime.now(UTC)
        self._admission.transition_run(run, "held", "queued")
        self._outbox.append_run_dispatch(run_id, "explicit_start")
        logging.info("启动 held AgentRun run_id=%s caller=%s", run_id, caller_id)
        return self._summary(run)

    def get(
        self, run_id: str, caller_id: str, allow_auditor: bool = False
    ) -> RunDetail:
        run = self._owned_run(run_id, caller_id, allow_auditor)
        if allow_auditor and run.caller_id != caller_id:
            # 内部审计身份跨调用方读取仅有脱敏摘要时，也需留下可追溯的访问事实。
            self._append_audit(
                run,
                caller_id,
                "agent_run_audit_read",
                {"status": run.status},
                actor_type="auditor",
            )
        step_records = self._step_records(run_id)
        current = next(
            (record for record in reversed(step_records) if record.status == "running"),
            None,
        )
        plan = self._session.scalar(select(AgentPlan).where(AgentPlan.run_id == run_id))
        planned_count = len(plan.steps_json) if plan else len(step_records)
        completed_count = sum(
            record.status in {"succeeded", "skipped"} for record in step_records
        )
        progress = int(completed_count * 100 / planned_count) if planned_count else 0
        # summary 已携带 status_version；RunDetail 在此基础上扩展查询字段，
        # 因此显式覆盖时需先剔除 summary 中的同名字段，避免 keyword 冲突。
        summary_dump = self._summary(run).model_dump()
        summary_dump.pop("status_version", None)
        return RunDetail(
            **summary_dump,
            status_version=run.status_version,
            last_event_seq=run.last_event_seq,
            execution_attempt=run.execution_attempt,
            privacy_state=run.privacy_state,
            privacy_version=run.privacy_version,
            error_code=run.error_code,
            privacy_purge_requested_at=run.privacy_purge_requested_at,
            private_data_purged_at=run.private_data_purged_at,
            updated_at=run.updated_at,
            progress=progress,
            current_step=self._step_summary(current) if current else None,
            public_trace=[],
        )

    def steps(
        self, run_id: str, caller_id: str, allow_auditor: bool = False
    ) -> list[StepSummary]:
        """只返回步骤状态摘要，避免查询接口泄漏原始输入、输出和模型内容。"""
        self._owned_run(run_id, caller_id, allow_auditor)
        return [self._step_summary(record) for record in self._step_records(run_id)]

    def cancel(
        self,
        run_id: str,
        caller_id: str,
        reason_code: str = "SYSTEM_REQUEST",
    ) -> RunSummary:
        """held/queued 可同步终止；claimed 只写取消请求，由有效 Worker 收敛。"""
        run = self._owned_run(run_id, caller_id)
        run.cancel_requested_at = datetime.now(UTC)
        if run.dispatch_state in {"held", "queued"} or (
            run.status == "waiting_human" and run.dispatch_state == "finished"
        ):
            previous_dispatch_state = run.dispatch_state
            run.status, run.dispatch_state = "cancelled", "finished"
            run.status_version += 1
            self._admission.transition_run(run, previous_dispatch_state, "finished")
            self._outbox.append_callback(run, "cancelled")
        self._append_audit(
            run,
            caller_id,
            "agent_run_cancelled",
            {"dispatch_state": run.dispatch_state},
            reason_code=reason_code,
        )
        logging.info("请求取消 AgentRun run_id=%s state=%s", run_id, run.dispatch_state)
        return self._summary(run)

    def purge(
        self,
        run_id: str,
        caller_id: str,
        reason_code: str = "SYSTEM_REQUEST",
    ) -> RunDetail:
        """先建立 privacy 写屏障；Task 10 再负责加密 payload 的物理清理。"""
        run = self._owned_run(run_id, caller_id)
        if run.privacy_state == "active":
            run.privacy_state = "purge_requested"
            run.privacy_version += 1
            run.privacy_purge_requested_at = datetime.now(UTC)
            run.cancel_requested_at = datetime.now(UTC)
            self._append_audit(
                run,
                caller_id,
                "agent_run_purge_requested",
                {
                    "privacy_version": str(run.privacy_version),
                },
                reason_code=reason_code,
            )
        logging.warning(
            "申请私密数据清理 run_id=%s privacy_version=%s", run_id, run.privacy_version
        )
        return self.get(run_id, caller_id)

    def complete_purge(self, run_id: str) -> bool:
        """供清理 Worker 幂等调用：删除私密载荷并保持 privacy 写屏障。

        Runtime 只保留 Run/成本等无内容元数据；Checkpoint 直接删除，可能携带
        私密摘要的 Artifact、Step、ToolCall 和 Run 字段统一置空。调用方必须在
        同一事务提交，防止 tombstone 已完成但载荷仍残留。
        """
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        if run is None:
            raise AgentRunServiceError("Run 不存在")
        if run.privacy_state == "purged":
            return False
        if run.privacy_state != "purge_requested":
            raise AgentRunServiceError("Run 未请求私密数据清理")
        checkpoint_result = self._session.execute(
            delete(AgentCheckpoint).where(AgentCheckpoint.run_id == run_id)
        )
        deleted_checkpoints = int(getattr(checkpoint_result, "rowcount", 0) or 0)
        self._session.execute(
            update(AgentArtifact)
            .where(AgentArtifact.run_id == run_id)
            .values(summary_json=None)
        )
        self._session.execute(
            update(AgentStep)
            .where(AgentStep.run_id == run_id)
            .values(input_summary=None, output_summary=None)
        )
        self._session.execute(
            update(AgentToolCall)
            .where(AgentToolCall.run_id == run_id)
            .values(input_summary=None, output_summary=None)
        )
        # ModelUsage 正常写入已只存受控治理摘要；但 purge 同样必须清理旧版本或
        # 异常直写的 JSON，不能把它们当作无内容账本永久保留。
        for usage in self._session.scalars(
            select(AgentModelUsage).where(AgentModelUsage.run_id == run_id)
        ):
            usage.capability_snapshot_json = self._safe_usage_capability_snapshot(
                usage.capability_snapshot_json
            )
            usage.thinking_summary_json = self._safe_thinking_summary(
                usage.thinking_summary_json
            )
        run.input_json = {}
        run.output_summary_json = None
        run.privacy_state = "purged"
        run.private_data_purged_at = datetime.now(UTC)
        self._append_audit(
            run,
            "runtime_privacy_worker",
            "agent_run_purged",
            {"privacy_version": str(run.privacy_version)},
            actor_type="system",
        )
        logging.warning(
            "完成私密数据清理 run_id=%s deleted_checkpoints=%s code=PRIVATE_DATA_PURGED",
            run_id,
            deleted_checkpoints,
        )
        return True

    def retry(
        self,
        run_id: str,
        caller_id: str,
        allow_auditor: bool = False,
        expected_status_version: int | None = None,
    ) -> RunSummary:
        """仅原创建者或经 API 验证的内部审计身份可执行人工重试。"""
        run = self._owned_run(run_id, caller_id, allow_auditor)
        self._assert_authorization_current(run)
        # 外部调用方提供版本时必须条件校验，防止对账线程覆盖较新的终态。
        if (
            expected_status_version is not None
            and run.status_version != expected_status_version
        ):
            raise AgentRunServiceError("状态版本冲突")
        if run.status not in {"failed", "partial"} or run.manual_retry_count >= 3:
            raise AgentRunServiceError("Run 当前状态不允许 retry")
        if run.privacy_state != "active":
            raise AgentRunServiceError("私密数据清理中的 Run 禁止 retry")
        if self._session.scalar(
            select(AgentCheckpoint).where(AgentCheckpoint.run_id == run_id)
        ) is None:
            raise AgentRunServiceError("Run 缺少 checkpoint，禁止 retry")
        if run.status == "partial":
            self._assert_partial_retryable(run)
        definition = self._session.scalar(
            select(AgentDefinition).where(
                AgentDefinition.agent_id == run.agent_id,
                AgentDefinition.version == run.agent_version,
            )
        )
        if definition is None or definition.status == "revoked":
            raise AgentRunServiceError("AgentPackage 已撤销，禁止 retry")
        run.manual_retry_count += 1
        run.status, run.dispatch_state = "pending", "queued"
        run.queued_at = datetime.now(UTC)
        run.status_version += 1
        self._admission.transition_run(run, "finished", "queued")
        self._outbox.append_run_dispatch(run_id, "manual_retry")
        self._append_audit(
            run,
            caller_id,
            "agent_run_retried",
            {"manual_retry_count": str(run.manual_retry_count)},
        )
        return self._summary(run)

    def _assert_partial_retryable(self, run: AgentRun) -> None:
        """partial 只能补做发布后的失败可选步骤，禁止重放主作品副作用。"""
        plan = self._session.scalar(select(AgentPlan).where(AgentPlan.run_id == run.run_id))
        if plan is None:
            raise AgentRunServiceError("partial Run 缺少计划，禁止 retry")
        optional_node_ids = {
            node.get("node_id")
            for node in plan.steps_json
            if isinstance(node, dict)
            and node.get("optional") is True
            and isinstance(node.get("node_id"), str)
        }
        steps = list(
            self._session.scalars(
                select(AgentStep).where(AgentStep.run_id == run.run_id)
            )
        )
        failed_names = {step.step_name for step in steps if step.status == "failed"}
        published = any(
            step.step_name == "publish_document" and step.status == "succeeded"
            for step in steps
        )
        if not published or not failed_names or not failed_names <= optional_node_ids:
            raise AgentRunServiceError("partial Run 不满足可选步骤 retry 条件")

    def record_auto_retry(self, run_id: str, step_id: str | None = None) -> None:
        """记录节点级自动重试；传入 step 时按该物理节点单独限额。"""
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        snapshot = run.capability_snapshot_json if run is not None else None
        policy = snapshot.get("execution_policy") if isinstance(snapshot, dict) else None
        frozen_limit = policy.get("max_auto_retry_per_step") if isinstance(policy, dict) else None
        max_retries = frozen_limit if isinstance(frozen_limit, int) and not isinstance(frozen_limit, bool) else 2
        if run is None:
            raise AgentRunServiceError("自动重试次数已耗尽")
        step = None
        if step_id is not None:
            step = self._session.scalar(
                select(AgentStep).where(
                    AgentStep.run_id == run_id,
                    AgentStep.step_id == step_id,
                    AgentStep.status == "running",
                )
            )
            if step is None or step.step_attempt - 1 >= max_retries:
                raise AgentRunServiceError("自动重试次数已耗尽")
        elif run.auto_retry_count >= max_retries:
            # 兼容尚未传入节点身份的旧调用；新 Executor 调用必须带 step_id。
            raise AgentRunServiceError("自动重试次数已耗尽")
        if step is not None:
            step.step_attempt += 1
        run.auto_retry_count += 1
        logging.info(
            "记录 Runtime 自动重试 run_id=%s auto_retry_count=%s",
            run_id,
            run.auto_retry_count,
        )

    def approve(
        self, run_id: str, caller_id: str, decision: str, expected_version: int
    ) -> RunSummary:
        run = self._owned_run(run_id, caller_id)
        if (
            run.status != "waiting_human"
            or run.dispatch_state != "finished"
            or run.status_version != expected_version
        ):
            raise AgentRunServiceError("人工审批状态版本冲突")
        dispatch_reason: str | None = None
        terminal_reject = False
        if decision == "approve":
            # 正常 approve 只能按 checkpoint 已完成节点继续；不能把等待时预置的
            # fallback 目标当作恢复入口。
            values = {
                "error_code": None,
                "dispatch_state": "queued",
                "queued_at": datetime.now(UTC),
            }
            dispatch_reason = "human_approve"
        else:
            plan = self._session.scalar(select(AgentPlan).where(AgentPlan.run_id == run_id))
            policy = plan.fallback_policy_json if plan is not None else {}
            if isinstance(policy, dict) and policy.get("reject_action") == "fallback":
                values = {
                    "error_code": "WAITING_HUMAN_FALLBACK",
                    "dispatch_state": "queued",
                    "queued_at": datetime.now(UTC),
                }
                dispatch_reason = "human_reject_fallback"
            else:
                values = {"status": "failed", "dispatch_state": "finished"}
                terminal_reject = True
        approved = self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.run_id == run_id,
                AgentRun.status == "waiting_human",
                AgentRun.dispatch_state == "finished",
                AgentRun.status_version == expected_version,
            )
            .execution_options(synchronize_session=False)
            .values(status_version=AgentRun.status_version + 1, **values)
        )
        if approved.rowcount != 1:  # type: ignore[attr-defined]
            raise AgentRunServiceError("人工审批状态版本冲突")
        self._session.refresh(run)
        if dispatch_reason is not None:
            self._admission.transition_run(run, "finished", "queued")
            self._outbox.append_run_dispatch(run_id, dispatch_reason)
        elif terminal_reject:
            self._outbox.append_callback(run, "failed")
        self._append_audit(
            run,
            caller_id,
            "agent_run_approval_decided",
            {"decision": decision, "dispatch_state": run.dispatch_state},
        )
        logging.info("人工审批 AgentRun run_id=%s decision=%s", run_id, decision)
        return self._summary(run)

    def count_dispatch_events(self, run_id: str) -> int:
        return len(
            self._session.scalars(
                select(RuntimeOutboxEvent).where(
                    RuntimeOutboxEvent.aggregate_id == run_id,
                    RuntimeOutboxEvent.event_type == "run_dispatch",
                )
            ).all()
        )

    def _owned_run(
        self, run_id: str, caller_id: str, allow_auditor: bool = False
    ) -> AgentRun:
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        if run is None or (run.caller_id != caller_id and not allow_auditor):
            raise AgentRunServiceError("Run 不存在或无访问权限")
        return run

    def _assert_authorization_current(self, run: AgentRun) -> None:
        """控制面动作也必须以当前权威版本为准，避免撤权后的 start/retry。"""
        if self._authorization_version_resolver is None:
            return
        current = self._authorization_version_resolver(run)
        if current is None or current != run.authorization_version:
            raise AgentRunServiceError("授权版本已变化")

    def _append_audit(
        self,
        run: AgentRun,
        actor_id: str,
        action: str,
        metadata_summary: dict[str, str],
        actor_type: str = "caller",
        *,
        reason_code: str | None = None,
    ) -> None:
        """只记录安全元数据，确保审计记录与 Run 状态处在相同数据库事务。"""
        self._audit.append(
            RuntimeAuditEvent(
                audit_id=str(uuid4()),
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type="agent_run",
                resource_id=run.run_id,
                outcome="accepted",
                occurred_at=datetime.now(UTC),
                trace_id=run.trace_id,
                metadata_summary=metadata_summary,
                reason_code=reason_code,
            )
        )

    def _step_records(self, run_id: str) -> list[AgentStep]:
        return list(
            self._session.scalars(
                select(AgentStep)
                .where(AgentStep.run_id == run_id)
                .order_by(AgentStep.created_at, AgentStep.id)
            )
        )

    @staticmethod
    def _step_summary(record: AgentStep) -> StepSummary:
        return StepSummary(
            step_id=record.step_id,
            step_name=record.step_name,
            step_type=record.step_type,
            status=record.status,
            execution_attempt=record.execution_attempt,
            step_attempt=record.step_attempt,
            error_code=record.error_code,
        )

    @staticmethod
    def _safe_usage_capability_snapshot(value: object) -> dict[str, object] | None:
        """仅保留 ModelUsage 可对账的 route 治理摘要，未知字段一律清除。"""
        if not isinstance(value, dict):
            return None
        result: dict[str, object] = {}
        for key in ("route_config_version", "data_residency"):
            candidate = value.get(key)
            if AgentRunService._safe_summary_string(candidate, 64):
                result[key] = candidate
        capabilities = value.get("capabilities")
        if (
            isinstance(capabilities, list)
            and len(capabilities) <= 32
            and all(AgentRunService._safe_summary_string(item, 64) for item in capabilities)
        ):
            result["capabilities"] = list(capabilities)
        for key in ("max_context_tokens", "max_output_tokens"):
            candidate = value.get(key)
            if isinstance(candidate, int) and not isinstance(candidate, bool) and 0 <= candidate <= 10_000_000:
                result[key] = candidate
        return result or None

    @staticmethod
    def _safe_thinking_summary(value: object) -> dict[str, object] | None:
        """隐藏推理不可恢复；只保留受控开关、预算和归一化版本。"""
        if not isinstance(value, dict):
            return None
        enabled = value.get("thinking_enabled")
        output_budget = value.get("max_output_tokens")
        input_budget = value.get("input_token_budget")
        version = value.get("normalization_version")
        if (
            not isinstance(enabled, bool)
            or not isinstance(output_budget, int)
            or isinstance(output_budget, bool)
            or not isinstance(input_budget, int)
            or isinstance(input_budget, bool)
            or not 0 <= output_budget <= 10_000_000
            or not 0 <= input_budget <= 10_000_000
            or not AgentRunService._safe_summary_string(version, 64)
        ):
            return None
        return {
            "thinking_enabled": enabled,
            "max_output_tokens": output_budget,
            "input_token_budget": input_budget,
            "normalization_version": version,
        }

    @staticmethod
    def _safe_summary_string(value: object, maximum_length: int) -> bool:
        return isinstance(value, str) and bool(
            re.fullmatch(r"[A-Za-z0-9._-]{1," + str(maximum_length) + r"}", value)
        )

    @staticmethod
    def _summary(run: AgentRun) -> RunSummary:
        return RunSummary(
            run_id=run.run_id,
            business_id=run.business_id,
            status=run.status,
            dispatch_state=run.dispatch_state,
            contract_version=run.contract_version,
            package_digest=run.package_digest,
            authorization_version=run.authorization_version,
            status_version=run.status_version,
        )

    @staticmethod
    def _validate_input_schema(schema: object, value: object) -> None:
        """校验第一版冻结的 object/required/properties 基础 JSON Schema 子集。

        AgentPackage 的完整 schema 在注册时已被 CI 校验；运行期只需拒绝缺失
        必填字段、额外字段与明显类型不匹配，且永不将私密 input 写入错误日志。
        """
        if schema is None:
            return
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise AgentRunServiceError("input schema 配置非法")
        if not isinstance(value, dict):
            raise AgentRunServiceError("input schema 校验失败")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
            raise AgentRunServiceError("input schema 配置非法")
        if any(key not in value for key in required):
            raise AgentRunServiceError("input schema 校验失败")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise AgentRunServiceError("input schema 配置非法")
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in value
        ):
            raise AgentRunServiceError("input schema 校验失败")
        type_map: dict[str, type[object] | tuple[type[object], ...]] = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "object": dict,
            "array": list,
        }
        for key, field_schema in properties.items():
            if key not in value or not isinstance(field_schema, dict):
                continue
            expected = field_schema.get("type")
            python_type = type_map.get(expected) if isinstance(expected, str) else None
            # bool 是 int 的子类，integer/number 不能接受布尔值。
            if python_type and (
                not isinstance(value[key], python_type)
                or (expected in {"integer", "number"} and isinstance(value[key], bool))
            ):
                raise AgentRunServiceError("input schema 校验失败")
            # 数值字段需校验 minimum，避免 generation_epoch=0 绕过冻结契约。
            minimum = field_schema.get("minimum")
            if (
                expected in {"integer", "number"}
                and isinstance(minimum, (int, float))
                and not isinstance(minimum, bool)
                and isinstance(value[key], (int, float))
                and not isinstance(value[key], bool)
                and value[key] < minimum
            ):
                raise AgentRunServiceError("input schema 校验失败")
