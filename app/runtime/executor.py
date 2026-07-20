"""Task 6 的最小静态 WorkflowExecutor；真实 Tool/Model 节点后续以注入 Runner 接入。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentPlan, AgentRun, AgentStep
from app.runtime.artifact import ArtifactError, ArtifactStore
from app.runtime.checkpoint import CheckpointError, CheckpointStore
from app.runtime.interfaces import AgentRunResult, LeaseContext
from app.runtime.state import AgentState
from app.services.lease_service import LeaseService
from app.services.outbox_service import OutboxService


class WorkflowNodeRunner(Protocol):
    """受控节点适配器；不得把业务正文或工具原始 payload 返回给 Runtime 账本。"""

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]: ...


class WorkflowExecutor:
    """按已落库静态计划执行节点，并在每个成功节点后写安全 checkpoint。"""

    def __init__(
        self,
        session: Session,
        node_runner: WorkflowNodeRunner,
        checkpoint_store: CheckpointStore,
        artifact_store: ArtifactStore,
        *,
        is_draining: Callable[[], bool] = lambda: False,
    ) -> None:
        self._session = session
        self._node_runner = node_runner
        self._checkpoint_store = checkpoint_store
        self._artifact_store = artifact_store
        self._is_draining = is_draining
        self._lease = LeaseService(session)
        self._outbox = OutboxService(session)

    def run(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        """执行当前 execution attempt；所有状态写入前均复核 lease/fencing 边界。"""
        return self._execute(
            run_id, lease_context, completed_node_ids=set(), state_data=None
        )

    def resume(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        """从最近兼容 checkpoint 恢复；只跳过已安全完成的静态节点。"""
        try:
            state = self._checkpoint_store.load_latest(run_id, lease_context)
        except CheckpointError as exc:
            logging.warning("Workflow checkpoint 不可恢复 run_id=%s error=%s", run_id, exc)
            return AgentRunResult(
                run_id=run_id,
                status="failed",
                execution_attempt=lease_context.execution_attempt,
                error_code="CHECKPOINT_NOT_RESUMABLE",
            )
        raw_node_ids = state.get("completed_node_ids", [])
        if not isinstance(raw_node_ids, list) or not all(
            isinstance(node_id, str) for node_id in raw_node_ids
        ):
            return AgentRunResult(
                run_id=run_id,
                status="failed",
                execution_attempt=lease_context.execution_attempt,
                error_code="CHECKPOINT_STATE_INVALID",
            )
        resume_from_node_id = state.get("resume_from_node_id")
        logging.info(
            "Workflow 从 checkpoint 恢复 run_id=%s completed_nodes=%s",
            run_id,
            len(raw_node_ids),
        )
        return self._execute(
            run_id,
            lease_context,
            completed_node_ids=set(raw_node_ids),
            state_data=state,
            resume_from_node_id=resume_from_node_id,
        )

    def _execute(
        self,
        run_id: str,
        lease_context: LeaseContext,
        completed_node_ids: set[str],
        state_data: dict[str, object] | None,
        resume_from_node_id: object | None = None,
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
        fallback_requested = run.error_code == "WAITING_HUMAN_FALLBACK"
        if fallback_requested:
            fallback_node_id = plan.fallback_policy_json.get(
                "waiting_human_fallback_node"
            )
            if (
                not isinstance(resume_from_node_id, str)
                or fallback_node_id != resume_from_node_id
                or resume_from_node_id
                not in {
                    node.get("node_id")
                    for node in plan.steps_json
                    if isinstance(node, Mapping)
                }
            ):
                return AgentRunResult(
                    run_id=run_id,
                    status="failed",
                    execution_attempt=lease_context.execution_attempt,
                    error_code="FALLBACK_NODE_INVALID",
                )
            # fallback 已被消费；公开状态和 callback 不暴露内部恢复原因。
            run.error_code = None
        else:
            # 人工 approve 即使 checkpoint 带有 fallback 目标，也只能线性恢复。
            resume_from_node_id = None
        if not self._lease.can_write(run_id, lease_context):
            return AgentRunResult(
                run_id=run_id,
                status="failed",
                execution_attempt=lease_context.execution_attempt,
                error_code="LEASE_CONTEXT_INVALID",
            )
        # draining 不接管 Run 状态；RunQueueService 会在这个安全边界释放 lease。
        if self._is_draining():
            return self._draining_result(run_id, lease_context, len(completed_node_ids))
        if run.status != "running":
            run.status = "running"
            run.status_version += 1
            self._outbox.append_callback_event(run, "run_started")
        run.started_at = run.started_at or datetime.now(UTC)
        completed_steps = len(completed_node_ids)
        # checkpoint 完整状态只存在 Fernet 密文中；恢复时丢弃仅供摘要展示的计数字段。
        if state_data is None:
            state = AgentState(run_input=run.input_json, completed_node_ids=sorted(completed_node_ids))
        else:
            state = AgentState.model_validate(
                {
                    key: value
                    for key, value in state_data.items()
                    if key not in {"completed_steps", "resume_from_node_id"}
                }
            )
            state.completed_node_ids = sorted(completed_node_ids)
        skipping = resume_from_node_id is not None
        for raw_node in plan.steps_json:
            node = self._validated_node(raw_node)
            if node is None:
                return self._fail(run, lease_context, "WORKFLOW_NODE_INVALID")
            node_id = str(node["node_id"])
            if skipping:
                skipping = node_id != resume_from_node_id
                if skipping:
                    continue
            if node_id in completed_node_ids:
                continue
            if not self._lease.can_write(run_id, lease_context):
                return self._fail(run, lease_context, "LEASE_CONTEXT_INVALID")
            if self._is_draining():
                return self._draining_result(run_id, lease_context, completed_steps)
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
                node_result = self._node_runner.run_node(node, run, state)
            except Exception:  # noqa: BLE001 - 节点异常必须转为安全错误码。
                logging.exception("Workflow 节点执行失败 run_id=%s node=%s", run_id, node["node_id"])
                step.status = "failed"
                step.error_code = "WORKFLOW_NODE_FAILED"
                step.finished_at = datetime.now(UTC)
                return self._fail(run, lease_context, "WORKFLOW_NODE_FAILED")
            if not self._lease.can_write(run_id, lease_context):
                return self._fail(run, lease_context, "LEASE_CONTEXT_INVALID")
            # 工具/模型副作用返回后已到节点安全边界：仍须完成脱敏 Artifact
            # 与加密 checkpoint，随后不再启动下一节点。
            draining_after_node = self._is_draining()
            try:
                self._artifact_store.save_node_result(
                    run, node_id, node_result, lease_context
                )
            except ArtifactError:
                logging.exception("Workflow 节点 Artifact 写入失败 run_id=%s node=%s", run_id, node_id)
                step.status = "failed"
                step.error_code = "ARTIFACT_WRITE_FAILED"
                step.finished_at = datetime.now(UTC)
                return self._fail(run, lease_context, "ARTIFACT_WRITE_FAILED")
            completed_steps += 1
            completed_node_ids.add(node_id)
            state.completed_node_ids = sorted(completed_node_ids)
            step.status = "succeeded"
            step.output_summary = {"node_id": node_id, "status": "ok"}
            step.finished_at = datetime.now(UTC)
            checkpoint_state = {
                "completed_steps": completed_steps,
                **state.model_dump(mode="json"),
            }
            if node_result.get("waiting_human") is True:
                fallback_node_id = plan.fallback_policy_json.get(
                    "waiting_human_fallback_node"
                )
                if fallback_node_id is not None:
                    if (
                        not isinstance(fallback_node_id, str)
                        or fallback_node_id
                        not in {
                            candidate.get("node_id")
                            for candidate in plan.steps_json
                            if isinstance(candidate, Mapping)
                        }
                    ):
                        return self._fail(run, lease_context, "FALLBACK_NODE_INVALID")
                    checkpoint_state["resume_from_node_id"] = fallback_node_id
            self._checkpoint_store.save(
                run_id,
                f"attempt:{lease_context.execution_attempt}:step:{completed_steps}",
                checkpoint_state,
                lease_context,
            )
            if draining_after_node or self._is_draining():
                return self._draining_result(run_id, lease_context, completed_steps)
            if node_result.get("waiting_human") is True:
                timeout = plan.stop_conditions_json.get("approval_ttl_seconds", 86400)
                if not isinstance(timeout, int) or timeout <= 0:
                    return self._fail(run, lease_context, "WAITING_HUMAN_TIMEOUT_INVALID")
                if not self._lease.pause_for_human(run_id, lease_context, timeout):
                    return self._fail(run, lease_context, "WAITING_HUMAN_PAUSE_FAILED")
                return AgentRunResult(
                    run_id=run_id,
                    status="waiting_human",
                    execution_attempt=lease_context.execution_attempt,
                    output_summary={"completed_steps": completed_steps},
                )
            # 过程事件只公开节点名和状态，不能包含工具返回、快照或播放文档。
            self._outbox.append_callback_event(
                run,
                "step_changed",
                [{"step": node_id, "status": "succeeded"}],
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
    def _draining_result(
        run_id: str, lease_context: LeaseContext, completed_steps: int
    ) -> AgentRunResult:
        """只返回可接管的非终态，不记录正文、prompt 或节点原始结果。"""
        logging.warning(
            "Workflow draining 到达安全边界 run_id=%s completed_steps=%s",
            run_id,
            completed_steps,
        )
        return AgentRunResult(
            run_id=run_id,
            status="pending",
            execution_attempt=lease_context.execution_attempt,
            error_code="WORKFLOW_DRAINING",
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
