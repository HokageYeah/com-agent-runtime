"""Task 6 的最小静态 WorkflowExecutor；真实 Tool/Model 节点后续以注入 Runner 接入。"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentCheckpoint, AgentPlan, AgentRun, AgentStep
from app.runtime.interfaces import AgentRunResult, LeaseContext
from app.runtime.state import AgentState
from app.services.lease_service import LeaseService


class WorkflowNodeRunner(Protocol):
    """受控节点适配器；不得把业务正文或工具原始 payload 返回给 Runtime 账本。"""

    def run_node(self, node: dict[str, object], run: AgentRun) -> dict[str, object]: ...


class WorkflowExecutor:
    """按已落库静态计划执行节点，并在每个成功节点后写安全 checkpoint。"""

    def __init__(self, session: Session, node_runner: WorkflowNodeRunner) -> None:
        self._session = session
        self._node_runner = node_runner
        self._lease = LeaseService(session)

    def run(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        """执行当前 execution attempt；所有状态写入前均复核 lease/fencing 边界。"""
        return self._execute(run_id, lease_context, completed_node_ids=set())

    def resume(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        """从最近兼容 checkpoint 恢复；只跳过已安全完成的静态节点。"""
        checkpoint = self._session.scalar(
            select(AgentCheckpoint)
            .where(AgentCheckpoint.run_id == run_id)
            .order_by(AgentCheckpoint.created_at.desc(), AgentCheckpoint.id.desc())
        )
        if checkpoint is None or checkpoint.privacy_version != lease_context.privacy_version:
            return AgentRunResult(
                run_id=run_id,
                status="failed",
                execution_attempt=lease_context.execution_attempt,
                error_code="CHECKPOINT_NOT_RESUMABLE",
            )
        summary = checkpoint.state_summary or {}
        raw_node_ids = summary.get("completed_node_ids", [])
        if not isinstance(raw_node_ids, list) or not all(
            isinstance(node_id, str) for node_id in raw_node_ids
        ):
            return AgentRunResult(
                run_id=run_id,
                status="failed",
                execution_attempt=lease_context.execution_attempt,
                error_code="CHECKPOINT_STATE_INVALID",
            )
        logging.info(
            "Workflow 从 checkpoint 恢复 run_id=%s completed_nodes=%s",
            run_id,
            len(raw_node_ids),
        )
        return self._execute(run_id, lease_context, completed_node_ids=set(raw_node_ids))

    def _execute(
        self,
        run_id: str,
        lease_context: LeaseContext,
        completed_node_ids: set[str],
    ) -> AgentRunResult:
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        plan = self._session.scalar(select(AgentPlan).where(AgentPlan.run_id == run_id))
        if run is None or plan is None:
            return AgentRunResult(
                run_id=run_id,
                status="failed",
                execution_attempt=lease_context.execution_attempt,
                error_code="WORKFLOW_PLAN_NOT_FOUND",
            )
        if not self._lease.can_write(run_id, lease_context):
            return AgentRunResult(
                run_id=run_id,
                status="failed",
                execution_attempt=lease_context.execution_attempt,
                error_code="LEASE_CONTEXT_INVALID",
            )
        run.status = "running"
        run.started_at = run.started_at or datetime.now(UTC)
        completed_steps = len(completed_node_ids)
        state = AgentState(
            run_input=run.input_json,
            completed_node_ids=sorted(completed_node_ids),
        )
        for raw_node in plan.steps_json:
            node = self._validated_node(raw_node)
            if node is None:
                return self._fail(run, lease_context, "WORKFLOW_NODE_INVALID")
            node_id = str(node["node_id"])
            if node_id in completed_node_ids:
                continue
            if not self._lease.can_write(run_id, lease_context):
                return self._fail(run, lease_context, "LEASE_CONTEXT_INVALID")
            step = AgentStep(
                step_id=str(uuid4()),
                run_id=run_id,
                step_name=node_id,
                step_type=str(node["node_type"]),
                status="running",
                execution_attempt=lease_context.execution_attempt,
                step_attempt=1,
                started_at=datetime.now(UTC),
            )
            self._session.add(step)
            self._session.flush()
            try:
                self._node_runner.run_node(node, run)
            except Exception:  # noqa: BLE001 - 节点异常必须转为安全错误码。
                logging.exception("Workflow 节点执行失败 run_id=%s node=%s", run_id, node["node_id"])
                step.status = "failed"
                step.error_code = "WORKFLOW_NODE_FAILED"
                step.finished_at = datetime.now(UTC)
                return self._fail(run, lease_context, "WORKFLOW_NODE_FAILED")
            if not self._lease.can_write(run_id, lease_context):
                return self._fail(run, lease_context, "LEASE_CONTEXT_INVALID")
            completed_steps += 1
            completed_node_ids.add(node_id)
            state.completed_node_ids = sorted(completed_node_ids)
            step.status = "succeeded"
            step.output_summary = {"node_id": node_id, "status": "ok"}
            step.finished_at = datetime.now(UTC)
            self._session.add(
                AgentCheckpoint(
                    checkpoint_id=str(uuid4()),
                    run_id=run_id,
                    checkpoint_key=f"attempt:{lease_context.execution_attempt}:step:{completed_steps}",
                    state_schema_version="1.0.0",
                    data_classification="runtime_internal",
                    privacy_version=lease_context.privacy_version,
                    # Task 10 接入加密完整恢复 state；Task 6 只持久化安全进度摘要。
                    encrypted_state_blob=None,
                    storage_ref=None,
                    state_summary={
                        "completed_steps": completed_steps,
                        **state.checkpoint_summary(),
                    },
                    content_digest=hashlib.sha256(
                        f"{run_id}:{lease_context.execution_attempt}:{completed_steps}".encode()
                    ).hexdigest(),
                    expires_at=run.run_deadline_at,
                    created_at=datetime.now(UTC),
                )
            )
        logging.info(
            "Workflow 静态计划执行完成 run_id=%s execution_attempt=%s steps=%s",
            run_id,
            lease_context.execution_attempt,
            completed_steps,
        )
        return AgentRunResult(
            run_id=run_id,
            status="succeeded",
            execution_attempt=lease_context.execution_attempt,
            output_summary={"completed_steps": completed_steps},
        )

    @staticmethod
    def _validated_node(raw_node: object) -> dict[str, object] | None:
        if not isinstance(raw_node, Mapping):
            return None
        node = dict(raw_node)
        if not isinstance(node.get("node_id"), str) or not isinstance(
            node.get("node_type"), str
        ):
            return None
        return node

    @staticmethod
    def _fail(
        run: AgentRun, lease_context: LeaseContext, error_code: str
    ) -> AgentRunResult:
        return AgentRunResult(
            run_id=run.run_id,
            status="failed",
            execution_attempt=lease_context.execution_attempt,
            error_code=error_code,
        )
