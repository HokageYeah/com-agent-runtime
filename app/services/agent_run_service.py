from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    AgentCheckpoint,
    AgentDefinition,
    AgentPlan,
    AgentRun,
    AgentStep,
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

    def create(
        self,
        command: CreateRunCommand,
        caller_id: str,
        tenant_id: str,
        idempotency_key: str,
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
            }.items()
            if value is not None
        }
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
                "model_policy": model_policy,
            },
            authorization_version=1,
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

    def start(self, run_id: str, caller_id: str, idempotency_key: str) -> RunSummary:
        run = self._owned_run(run_id, caller_id)
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
        return RunDetail(
            **self._summary(run).model_dump(),
            status_version=run.status_version,
            last_event_seq=run.last_event_seq,
            execution_attempt=run.execution_attempt,
            privacy_state=run.privacy_state,
            privacy_version=run.privacy_version,
            error_code=run.error_code,
            error_message=run.error_message,
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

    def cancel(self, run_id: str, caller_id: str) -> RunSummary:
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
            run, caller_id, "agent_run_cancelled", {"dispatch_state": run.dispatch_state}
        )
        logging.info("请求取消 AgentRun run_id=%s state=%s", run_id, run.dispatch_state)
        return self._summary(run)

    def purge(self, run_id: str, caller_id: str) -> RunDetail:
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
                {"privacy_version": str(run.privacy_version)},
            )
        logging.warning(
            "申请私密数据清理 run_id=%s privacy_version=%s", run_id, run.privacy_version
        )
        return self.get(run_id, caller_id)

    def complete_purge(self, run_id: str) -> bool:
        """供清理 Worker 幂等调用：移除私密输入并保持 privacy 写屏障。"""
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        if run is None:
            raise AgentRunServiceError("Run 不存在")
        if run.privacy_state == "purged":
            return False
        if run.privacy_state != "purge_requested":
            raise AgentRunServiceError("Run 未请求私密数据清理")
        run.input_json = {}
        run.privacy_state = "purged"
        run.private_data_purged_at = datetime.now(UTC)
        self._append_audit(
            run,
            "runtime_privacy_worker",
            "agent_run_purged",
            {"privacy_version": str(run.privacy_version)},
            actor_type="system",
        )
        logging.warning("完成私密数据清理 run_id=%s", run_id)
        return True

    def retry(
        self, run_id: str, caller_id: str, allow_auditor: bool = False
    ) -> RunSummary:
        """仅原创建者或经 API 验证的内部审计身份可执行人工重试。"""
        run = self._owned_run(run_id, caller_id, allow_auditor)
        if run.status not in {"failed", "partial"} or run.manual_retry_count >= 3:
            raise AgentRunServiceError("Run 当前状态不允许 retry")
        if run.privacy_state != "active":
            raise AgentRunServiceError("私密数据清理中的 Run 禁止 retry")
        if self._session.scalar(
            select(AgentCheckpoint).where(AgentCheckpoint.run_id == run_id)
        ) is None:
            raise AgentRunServiceError("Run 缺少 checkpoint，禁止 retry")
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

    def record_auto_retry(self, run_id: str, max_retries: int = 2) -> None:
        """供后续 Executor 的节点级重试调用，不消耗人工 Run 重试额度。"""
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        if run is None or run.auto_retry_count >= max_retries:
            raise AgentRunServiceError("自动重试次数已耗尽")
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
            values = {"error_code": None, "dispatch_state": "queued"}
            dispatch_reason = "human_approve"
        else:
            plan = self._session.scalar(select(AgentPlan).where(AgentPlan.run_id == run_id))
            policy = plan.fallback_policy_json if plan is not None else {}
            if isinstance(policy, dict) and policy.get("reject_action") == "fallback":
                values = {
                    "error_code": "WAITING_HUMAN_FALLBACK",
                    "dispatch_state": "queued",
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

    def _append_audit(
        self,
        run: AgentRun,
        actor_id: str,
        action: str,
        metadata_summary: dict[str, str],
        actor_type: str = "caller",
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
            error_message=record.error_message,
        )

    @staticmethod
    def _summary(run: AgentRun) -> RunSummary:
        return RunSummary(
            run_id=run.run_id,
            status=run.status,
            dispatch_state=run.dispatch_state,
            contract_version=run.contract_version,
            package_digest=run.package_digest,
            authorization_version=run.authorization_version,
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
