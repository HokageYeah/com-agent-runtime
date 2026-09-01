"""Task 6 的最小静态 WorkflowExecutor；真实 Tool/Model 节点后续以注入 Runner 接入。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_uru import log_success
from app.models import AgentDefinition, AgentPlan, AgentRun, AgentStep
from app.runtime.artifact import ArtifactError, ArtifactStore
from app.runtime.bounded_loop import (
    InheritedLoopBudget,
    LoopBudgetError,
    LoopIterationResult,
    derive_inherited_budget,
    merge_unique_scenes,
    recompute_loop_remaining,
    usage_snapshot,
    validated_loop_policy,
)
from app.runtime.checkpoint import CheckpointError, CheckpointStore
from app.runtime.graph_builder import StaticWorkflowGraph, StaticWorkflowGraphError
from app.runtime.interfaces import AgentRunResult, LeaseContext
from app.runtime.policy_engine import ExecutionBudgetExceeded, PolicyEngine
from app.runtime.state import AgentState
from app.schemas.audit import RuntimeAuditEvent
from app.services.agent_run_service import AgentRunService, AgentRunServiceError
from app.services.audit_service import AuditService
from app.services.lease_service import LeaseService
from app.services.outbox_service import OutboxService


class WorkflowNodeRunner(Protocol):
    """受控节点适配器；不得把业务正文或工具原始 payload 返回给 Runtime 账本。"""

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]: ...


class RetryableWorkflowNodeError(RuntimeError):
    """节点明确声明的瞬时失败；Executor 才可按冻结额度重新执行该节点。"""


# R2 checkpoint 白名单：只允许写入恢复路由所必需的元数据。
# snapshot/sanitized_material/scenes/actions/playback_document/publish_result/
# media_tasks/safety_report/stats/highlights/chapter_plan/run_input/trust_metadata/
# errors 均含正文或可重算的中间内容，绝不进加密 checkpoint。
_SAFE_CHECKPOINT_KEYS = frozenset(
    {"completed_steps", "completed_node_ids", "fallback_flags", "resume_from_node_id"}
)


def _safe_checkpoint_state(state: AgentState, completed_steps: int) -> dict[str, object]:
    """构造恢复路由所需的最小 checkpoint 视图，剔除一切正文与中间内容。

    - ``completed_steps`` / ``completed_node_ids`` / ``fallback_flags``：路由与
      进度，恢复时由 Executor 直接读取。
    - 副作用重放控制由 Runner 通过 query-after-commit（audit + logical_key）
      独立完成；checkpoint 不再保存 ``publish_result`` 等业务字段，避免密文
      被解密后泄漏作品或发布结果正文。
    - 即便 Fernet 密钥被泄漏，五类素材正文、Scene、PlaybackDocument 也不在
      解密结果中（规格 L550 新增断言由此保证）。
    """
    return {
        "completed_steps": completed_steps,
        "completed_node_ids": list(state.completed_node_ids),
        "fallback_flags": list(state.fallback_flags),
    }


@dataclass
class _LoopProgress:
    """bounded_loop 执行进度：只含轮次/安全计数/原因码，绝不含正文。"""

    iterations: int = 0
    iteration_errors: int = 0
    output_count: int = 0
    coverage_count: int = 0
    exhausted: bool = False
    declared_partial: bool = False
    reason_code: str | None = None
    early: AgentRunResult | None = None


@dataclass
class _LoopNodeOutcome:
    """bounded_loop 节点执行结论；三态互斥。

    - ``node_result``：循环收敛（complete/partial），节点按正常完成处理。
    - ``early``：逐轮安全检查或结构性失败的提前返回（cancelled/failed/
      draining），由 _execute 原样上抛。
    - ``partial``：循环以 partial 收敛，Run 终态降级 partial。
    """

    node_result: dict[str, object] | None = None
    early: AgentRunResult | None = None
    partial: bool = False


def _loop_protocol(runner: object) -> tuple[Callable, Callable, Callable] | None:
    """解析 Runner 的受控循环三段接口；缺任一段即不支持（fail closed）。

    与 bind_lease_context 相同的 duck-typing 解析：现有 1.0.0-1.0.4 Runner
    不实现该接口，其节点类型也不含 bounded_loop，行为零变化。
    """
    begin = getattr(runner, "begin_loop", None)
    iterate = getattr(runner, "run_loop_iteration", None)
    finalize = getattr(runner, "finalize_loop", None)
    if not all(callable(item) for item in (begin, iterate, finalize)):
        return None
    return begin, iterate, finalize


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
        authorization_version_resolver: Callable[[AgentRun], int | None] | None = None,
    ) -> None:
        self._session = session
        self._node_runner = node_runner
        self._checkpoint_store = checkpoint_store
        self._artifact_store = artifact_store
        self._is_draining = is_draining
        self._authorization_version_resolver = authorization_version_resolver
        self._lease = LeaseService(session)
        self._outbox = OutboxService(session)
        self._policy = PolicyEngine(session)
        self._runs = AgentRunService(session)

    def run(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        """执行当前 execution attempt；所有状态写入前均复核 lease/fencing 边界。"""
        return self._execute(
            run_id, lease_context, completed_node_ids=set(), state_data=None
        )

    def resume(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        """从最近兼容 checkpoint 恢复；按节点级 ``safe_to_rerun`` 分类恢复。

        R2 安全模型（绝不从 checkpoint 恢复正文/中间内容）：
        - 旧版完整 checkpoint（含 snapshot/scenes/playback_document 等正文键）一律
          拒绝（CHECKPOINT_STATE_INVALID），必须走 purge 路径清理，不可作为恢复输入。
        - 合法 checkpoint 只含路由元数据（completed_node_ids/fallback_flags）。恢复时
          把 completed_node_ids 传给 _execute，按节点级 ``safe_to_rerun`` 分类处理：
          * memoir 读取/内容/发布节点声明 ``safe_to_rerun=True``，强制重跑：load_snapshot
            按当前 generation_epoch/privacy_version/authorization 重新取 Snapshot（授权
            撤销/隐私版本变更 → gateway 拒绝）；内容节点从 state 重算，不读 checkpoint
            正文；publish_document 走 query-after-commit（logical_key）幂等，已提交则
            不重发。
          * 非 memoir Agent 节点默认 ``safe_to_rerun=False``，已完成则跳过，只执行未
            完成节点——保护未声明幂等性的副作用不被盲目重放（partial 只重做未完成
            optional）。
        """
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
        # 旧版完整 checkpoint（含正文/中间内容键）= 从密文复活作品正文的风险。
        # 检测到必须立即 purge，让它永不作为新版恢复输入；purge 受 fencing/privacy/
        # authorization 保护（_require_writable_run），审计只记事实与条数不输出密文。
        # purge 成功或失败都 fail closed——本次 resume 绝不恢复，下次 resume 会因
        # checkpoint 不存在而走 fresh 路径或 CHECKPOINT_NOT_RESUMABLE。
        extra_keys = set(state) - _SAFE_CHECKPOINT_KEYS
        if extra_keys:
            # 日志隐私（P3）：只输出 run_id + 非白名单键计数 + purge 条数 + 稳定 code，
            # 绝不打印 sorted(extra_keys)（键名可暗示正文结构）或 purge_exc 原文（异常
            # 文本可能夹带库错误/密文片段）。事实细节由 checkpoint_purged 审计行安全记录。
            logging.warning(
                "Workflow checkpoint 含非白名单键 拒绝恢复并 purge run_id=%s extra_key_count=%d",
                run_id,
                len(extra_keys),
            )
            try:
                purged = self._checkpoint_store.purge_for_run(run_id, lease_context)
            except CheckpointError:
                logging.warning(
                    "Workflow legacy checkpoint purge 失败 fail closed run_id=%s code=CHECKPOINT_PURGE_FAILED",
                    run_id,
                )
            else:
                logging.info(
                    "Workflow legacy checkpoint 已 purge run_id=%s purged_count=%d",
                    run_id,
                    purged,
                )
            return AgentRunResult(
                run_id=run_id,
                status="failed",
                execution_attempt=lease_context.execution_attempt,
                error_code="CHECKPOINT_STATE_INVALID",
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
            "Workflow 从 checkpoint 恢复 run_id=%s 分类恢复 checkpoint_completed=%s",
            run_id,
            len(raw_node_ids),
        )
        # R2 分类恢复：把 checkpoint 的 completed_node_ids 传给 _execute，由节点级
        # safe_to_rerun 决定已完成节点是否重跑。memoir 读取/内容/发布节点声明
        # safe_to_rerun=True 强制重算（load_snapshot 重读 Snapshot、内容节点重算、
        # publish 走 query-after-commit 幂等）；其他 Agent 默认 False 跳过已完成
        # 节点，只执行未完成节点（保护非幂等副作用，partial 只重做未完成 optional）。
        # fallback 线性恢复（resume_from_node_id）路径不受影响。
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
        try:
            static_nodes = StaticWorkflowGraph.build(plan.steps_json).ordered_nodes()
        except StaticWorkflowGraphError:
            # 不记录 plan 原文或图编译异常；静态图无法验证时绝不退回动态循环。
            return self._fail(run, lease_context, "STATIC_GRAPH_INVALID")
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
        if self._authorization_changed(run):
            run.cancel_requested_at = datetime.now(UTC)
            self._record_authorization_change(run)
            return AgentRunResult(
                run_id=run_id, status="cancelled",
                execution_attempt=lease_context.execution_attempt,
                error_code="AUTHORIZATION_CHANGED",
            )
        if self._package_revoked(run):
            run.cancel_requested_at = datetime.now(UTC)
            self._record_package_revoked(run)
            return AgentRunResult(
                run_id=run_id,
                status="cancelled",
                execution_attempt=lease_context.execution_attempt,
                error_code="PACKAGE_REVOKED",
            )
        # Package/authorization 是 can_write 的一部分，但要先返回受控的业务
        # 结论，不能把不可执行 Package 伪装成泛化 lease 错误。
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
            # R2 白名单过滤（纵深防御）：resume() 已在调用前拒绝含正文键的旧版
            # checkpoint，此处再次按 _SAFE_CHECKPOINT_KEYS 收敛，确保即便上游漏判
            # 也不会把 snapshot/scenes/playback_document 等正文注入 AgentState。
            # completed_node_ids 用入参（resume 传 checkpoint 的已完成集）覆盖，
            # 由 _execute 主循环按节点级 safe_to_rerun 分类决定是否重跑。
            safe_payload = {
                key: value
                for key, value in state_data.items()
                if key in _SAFE_CHECKPOINT_KEYS
                and key not in {"completed_steps", "resume_from_node_id"}
            }
            state = AgentState.model_validate(safe_payload)
            state.completed_node_ids = sorted(completed_node_ids)
        skipping = resume_from_node_id is not None
        partial_optional_failure = False
        # M7：bounded_loop 以 partial 收敛（预算耗尽/单轮失败）时 Run 终态降级。
        partial_loop_failure = False
        # 只在本次实际执行范围计时；held/queued/waiting_human 从未进入此循环，
        # 不会被误算为活跃预算。历史累计值存放在 Run 的 active_elapsed_ms。
        active_started_at = monotonic()
        for raw_node in static_nodes:
            node = self._validated_node(raw_node)
            if node is None:
                return self._fail(run, lease_context, "WORKFLOW_NODE_INVALID")
            node_id = str(node["node_id"])
            if skipping:
                skipping = node_id != resume_from_node_id
                if skipping:
                    continue
            if node_id in completed_node_ids:
                if "safe_to_rerun" not in node:
                    # P2 legacy plan guard：在 safe_to_rerun 引入前冻结的 plan，节点缺该键。
                    # memoir checkpoint 不存正文，旧实现 node.get("safe_to_rerun") 把缺键当
                    # 默认 False 静默跳过 → resume 用空 state 产出残缺文档。缺键无法安全判定
                    # 跳过 vs 重算，一律 fail closed（PLAN_LEGACY_DEFINITION）交业务侧
                    # undo/purge 重建。Executor 不硬编码 memoir 节点名：任何缺键的已完成节点
                    # 都触发，与 business_type 解耦（不引入 Playback 业务事实）。
                    return self._fail(run, lease_context, "PLAN_LEGACY_DEFINITION")
                if not bool(node["safe_to_rerun"]):
                    # R2 分类恢复：已完成且显式声明 safe_to_rerun=False 的节点跳过——保护未声明
                    # 幂等性的副作用不被盲目重放（非 memoir Agent / partial 已完成 optional）。
                    # safe_to_rerun=True 的节点（memoir 读取/内容/发布）落到下方正常重算。
                    continue
            try:
                self._policy.assert_can_continue(
                    run,
                    {
                        "steps": completed_steps + 1,
                        "active_elapsed_ms": int((monotonic() - active_started_at) * 1000),
                    },
                )
            except ExecutionBudgetExceeded as exc:
                logging.warning("Workflow 执行预算已耗尽 run_id=%s code=%s", run_id, exc.code)
                return self._fail(run, lease_context, exc.code)
            if not self._lease.can_write(run_id, lease_context):
                return self._fail(run, lease_context, "LEASE_CONTEXT_INVALID")
            if self._authorization_changed(run):
                run.cancel_requested_at = datetime.now(UTC)
                self._record_authorization_change(run)
                return AgentRunResult(
                    run_id=run_id, status="cancelled",
                    execution_attempt=lease_context.execution_attempt,
                    error_code="AUTHORIZATION_CHANGED",
                )
            if self._package_revoked(run):
                run.cancel_requested_at = datetime.now(UTC)
                self._record_package_revoked(run)
                return AgentRunResult(
                    run_id=run_id,
                    status="cancelled",
                    execution_attempt=lease_context.execution_attempt,
                    error_code="PACKAGE_REVOKED",
                )
            if self._is_draining():
                return self._draining_result(run_id, lease_context, completed_steps)
            if not self._lease.heartbeat(run_id, lease_context):
                return self._fail(run, lease_context, "LEASE_CONTEXT_INVALID")
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
            node_active_elapsed_before = run.active_elapsed_ms
            try:
                bind_lease_context = getattr(self._node_runner, "bind_lease_context", None)
                if callable(bind_lease_context):
                    bind_lease_context(lease_context)
                if node.get("node_type") == "bounded_loop":
                    # M7 受控循环：循环体由 Runner 三段接口（begin/iteration/
                    # finalize）执行，Executor 只负责预算继承、逐轮安全检查、
                    # 按键去重合并与安全审计；循环错误语义与通用节点不同，
                    # 不进入通用 run_node 自动重试路径。
                    loop = self._run_bounded_loop_node(
                        node,
                        run,
                        state,
                        lease_context,
                        completed_steps,
                        active_started_at,
                    )
                    if loop.early is not None:
                        if loop.early.status == "failed":
                            step.status = "failed"
                            step.error_code = loop.early.error_code
                            step.finished_at = datetime.now(UTC)
                        return loop.early
                    node_result = loop.node_result or {"node_id": node_id}
                    partial_loop_failure = partial_loop_failure or loop.partial
                else:
                    while True:
                        try:
                            node_result = self._node_runner.run_node(node, run, state)
                            break
                        except RetryableWorkflowNodeError as exc:
                            try:
                                self._runs.record_auto_retry(run_id, step.step_id)
                            except AgentRunServiceError:
                                logging.warning(
                                    "Workflow 节点自动重试额度已耗尽 run_id=%s node=%s",
                                    run_id,
                                    node_id,
                                )
                                step.status = "failed"
                                step.error_code = "AUTO_RETRY_LIMIT_EXCEEDED"
                                step.finished_at = datetime.now(UTC)
                                return self._fail(
                                    run, lease_context, "AUTO_RETRY_LIMIT_EXCEEDED"
                                )
                            logging.info(
                                "Workflow 节点自动重试 run_id=%s node=%s "
                                "step_attempt=%s code=%s",
                                run_id,
                                node_id,
                                step.step_attempt,
                                str(exc),
                            )
            except Exception:  # noqa: BLE001 - 节点异常必须转为安全错误码。
                step.status = "failed"
                step.error_code = "WORKFLOW_NODE_FAILED"
                step.finished_at = datetime.now(UTC)
                # 可选后处理只能在主作品已完成发布后降级为 partial；不能把
                # 主链故障伪装为可重试媒体失败，也不记录异常正文。
                if bool(node.get("optional")) and "publish_document" in completed_node_ids:
                    partial_optional_failure = True
                    logging.warning(
                        "Workflow 可选节点失败，保留已发布作品 run_id=%s node=%s code=OPTIONAL_NODE_FAILED",
                        run_id,
                        node_id,
                    )
                    continue
                logging.warning(
                    "Workflow 节点执行失败 run_id=%s node=%s code=WORKFLOW_NODE_FAILED",
                    run_id,
                    node_id,
                )
                return self._fail(run, lease_context, "WORKFLOW_NODE_FAILED")
            if not self._lease.heartbeat(
                run_id, lease_context
            ) or not self._lease.can_write(run_id, lease_context):
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
            node_status = (
                "skipped" if node_result.get("skipped") is True else "succeeded"
            )
            step.status = node_status
            step.output_summary = {
                "node_id": node_id,
                "status": "skipped" if node_status == "skipped" else "ok",
            }
            if isinstance(node_result.get("loop_summary"), dict):
                # 循环摘要只含轮次/原因/安全计数/用量，不携带任何正文。
                step.output_summary = {
                    **step.output_summary,
                    "loop": node_result["loop_summary"],
                }
            step.finished_at = datetime.now(UTC)
            # 每个安全节点边界刷新累计活跃时间；不能用 Run.created_at，避免排队
            # 或人工审批时间错误消耗执行额度。
            node_elapsed_ms = int((monotonic() - active_started_at) * 1000)
            # 429 等待可能已由 ModelGateway 提前写入，节点边界只补未归集的部分。
            already_recorded_ms = max(0, run.active_elapsed_ms - node_active_elapsed_before)
            run.active_elapsed_ms += max(0, node_elapsed_ms - already_recorded_ms)
            active_started_at = monotonic()
            # R2 隐私铁律：checkpoint 只允许保存路由/fallback/进度元数据，
            # snapshot/sanitized_material/scenes/playback_document 等正文即便加密
            # 也不进密文 blob。旧的全量 model_dump 在迁移期由 purge 路径清除。
            checkpoint_state = _safe_checkpoint_state(state, completed_steps)
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
                [{"step": node_id, "status": node_status}],
            )
        # 关键节点：整个静态计划执行完成（绿色成功里程碑；最终成败分级
        # 由 lease_service.finish 按 Run 终态着色）。
        log_success(
            "Workflow 静态计划执行完成 run_id=%s execution_attempt=%s steps=%s",
            run_id,
            lease_context.execution_attempt,
            completed_steps,
        )
        return AgentRunResult(
            run_id=run_id,
            status=(
                "partial"
                if partial_optional_failure or partial_loop_failure
                else "succeeded"
            ),
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

    def _run_bounded_loop_node(
        self,
        node: dict[str, object],
        run: AgentRun,
        state: AgentState,
        lease_context: LeaseContext,
        completed_steps: int,
        active_started_at: float,
    ) -> _LoopNodeOutcome:
        """M7 bounded_loop 节点编排：策略/预算 fail closed → 循环 → 收尾审计。

        冻结语义（详见 app/runtime/bounded_loop.py）：预算继承 Run 级限额、
        迭代上限 = 剩余 model call、每轮逐项安全检查、产物按 scene_id 去重
        追加、中间正文不落 checkpoint（整节点 safe_to_rerun=True 重算）。
        """
        policy = validated_loop_policy(node)
        if policy is None:
            logging.warning(
                "bounded_loop loop_policy 非法 run_id=%s node=%s code=LOOP_POLICY_INVALID",
                run.run_id,
                node.get("node_id"),
            )
            return _LoopNodeOutcome(
                early=self._fail(run, lease_context, "LOOP_POLICY_INVALID")
            )
        try:
            budget = derive_inherited_budget(self._session, run)
        except LoopBudgetError as exc:
            logging.warning(
                "bounded_loop 预算继承 fail closed run_id=%s code=%s",
                run.run_id,
                exc.code,
            )
            return _LoopNodeOutcome(early=self._fail(run, lease_context, exc.code))
        protocol = _loop_protocol(self._node_runner)
        if protocol is None:
            logging.warning(
                "bounded_loop Runner 未实现循环接口 run_id=%s code=LOOP_RUNNER_UNSUPPORTED",
                run.run_id,
            )
            return _LoopNodeOutcome(
                early=self._fail(run, lease_context, "LOOP_RUNNER_UNSUPPORTED")
            )
        begin, iterate, finalize = protocol
        try:
            # begin_loop 只做循环初始化（批次/预算快照），不产生模型调用。
            begin(node, run, state, budget)
        except Exception:  # noqa: BLE001 - 初始化失败是结构性失败，fail closed。
            logging.warning(
                "bounded_loop begin_loop 失败 run_id=%s code=LOOP_BODY_FAILED", run.run_id
            )
            return _LoopNodeOutcome(
                early=self._fail(run, lease_context, "LOOP_BODY_FAILED")
            )
        usage_before, _, _ = usage_snapshot(self._session, run.run_id)
        progress = self._run_loop_iterations(
            node, run, state, lease_context, completed_steps, active_started_at,
            budget, iterate,
        )
        if progress.early is not None:
            return _LoopNodeOutcome(early=progress.early)
        return self._finalize_bounded_loop(
            node, run, state, lease_context, policy, progress, finalize, usage_before
        )

    def _run_loop_iterations(
        self,
        node: dict[str, object],
        run: AgentRun,
        state: AgentState,
        lease_context: LeaseContext,
        completed_steps: int,
        active_started_at: float,
        budget: InheritedLoopBudget,
        iterate: Callable,
    ) -> _LoopProgress:
        """逐轮执行循环体：安全检查 → 预算重算 → 单次调用 → 去重合并。"""
        progress = _LoopProgress()
        while True:
            # 每轮重新检查 cancel/purge、授权、package、lease/fencing、draining。
            early = self._loop_safety_check(run, lease_context, completed_steps)
            if early is not None:
                progress.early = early
                return progress
            live = recompute_loop_remaining(
                self._session, run, budget, monotonic() - active_started_at
            )
            # 全部预算逐轮重检：任一余额触底即额度耗尽，交 on_budget_exhausted 收敛。
            if (
                live.remaining_model_calls <= 0
                or live.remaining_tokens <= 0
                or live.remaining_cost <= 0
                or live.remaining_ms <= 0
            ):
                progress.exhausted = True
                progress.reason_code = "LOOP_BUDGET_EXHAUSTED"
                break
            # 迭代上限 = 启动时剩余 model call；上限本身就是额度边界。
            if progress.iterations >= budget.max_iterations:
                progress.exhausted = True
                progress.reason_code = "LOOP_BUDGET_EXHAUSTED"
                break
            calls_before, _, _ = usage_snapshot(self._session, run.run_id)
            try:
                raw = iterate(node, run, state, progress.iterations + 1, live)
                result = (
                    raw
                    if isinstance(raw, LoopIterationResult)
                    else LoopIterationResult.model_validate(raw)
                )
            except Exception:  # noqa: BLE001
                # on_iteration_error=continue：单轮失败只跳过该轮继续下一轮；
                # 异常正文可能夹带模型输出/请求体，不落任何日志或账本。
                progress.iterations += 1
                progress.iteration_errors += 1
                logging.warning(
                    "bounded_loop 迭代失败跳过 run_id=%s iteration=%s "
                    "code=LOOP_ITERATION_SKIPPED",
                    run.run_id,
                    progress.iterations,
                )
                continue
            calls_now, _, _ = usage_snapshot(self._session, run.run_id)
            if calls_now - calls_before > 1:
                # 每轮至多一次模型调用是冻结契约；违反即结构性失败 fail closed。
                logging.warning(
                    "bounded_loop 单轮模型调用超限 run_id=%s iteration=%s "
                    "code=LOOP_MODEL_CALL_CONTRACT_VIOLATED",
                    run.run_id,
                    progress.iterations + 1,
                )
                progress.early = self._fail(
                    run, lease_context, "LOOP_MODEL_CALL_CONTRACT_VIOLATED"
                )
                return progress
            progress.iterations += 1
            if result.outcome == "failed":
                logging.warning(
                    "bounded_loop 迭代声明结构性失败 run_id=%s iteration=%s "
                    "code=LOOP_BODY_FAILED",
                    run.run_id,
                    progress.iterations,
                )
                progress.early = self._fail(run, lease_context, "LOOP_BODY_FAILED")
                return progress
            progress.output_count += result.output_count
            progress.coverage_count += result.coverage_count
            # append_unique_by_key：迭代产物按 scene_id 去重后追加，禁止覆盖。
            merge_unique_scenes(state)
            if result.outcome in {"complete", "partial"}:
                progress.declared_partial = result.outcome == "partial"
                progress.reason_code = result.reason_code or (
                    "LOOP_COMPLETE" if result.outcome == "complete" else "LOOP_PARTIAL_DECLARED"
                )
                break
        return progress

    def _loop_safety_check(
        self, run: AgentRun, lease_context: LeaseContext, completed_steps: int
    ) -> AgentRunResult | None:
        """逐轮安全检查；终态结论与 _execute 节点级检查完全一致，不引入新语义。"""
        if run.cancel_requested_at is not None:
            return AgentRunResult(
                run_id=run.run_id,
                status="cancelled",
                execution_attempt=lease_context.execution_attempt,
                error_code="CANCEL_REQUESTED",
            )
        if self._authorization_changed(run):
            run.cancel_requested_at = datetime.now(UTC)
            self._record_authorization_change(run)
            return AgentRunResult(
                run_id=run.run_id,
                status="cancelled",
                execution_attempt=lease_context.execution_attempt,
                error_code="AUTHORIZATION_CHANGED",
            )
        if self._package_revoked(run):
            run.cancel_requested_at = datetime.now(UTC)
            self._record_package_revoked(run)
            return AgentRunResult(
                run_id=run.run_id,
                status="cancelled",
                execution_attempt=lease_context.execution_attempt,
                error_code="PACKAGE_REVOKED",
            )
        if not self._lease.heartbeat(
            run.run_id, lease_context
        ) or not self._lease.can_write(run_id=run.run_id, context=lease_context):
            # can_write 同时覆盖隐私状态/版本（purge 屏障）与 lease/fencing。
            return self._fail(run, lease_context, "LEASE_CONTEXT_INVALID")
        if self._is_draining():
            return self._draining_result(run.run_id, lease_context, completed_steps)
        return None

    def _finalize_bounded_loop(
        self,
        node: dict[str, object],
        run: AgentRun,
        state: AgentState,
        lease_context: LeaseContext,
        policy: dict[str, object],
        progress: _LoopProgress,
        finalize: Callable,
        usage_before: int,
    ) -> _LoopNodeOutcome:
        """收尾调用段：on_budget_exhausted=failed 直接失败，否则执行 finalize。"""
        if progress.exhausted and policy["on_budget_exhausted"] == "failed":
            # 额度耗尽且冻结策略为 failed：不发布部分结果，整节点失败。
            logging.warning(
                "bounded_loop 预算耗尽按 failed 收敛 run_id=%s code=LOOP_BUDGET_EXHAUSTED",
                run.run_id,
            )
            return _LoopNodeOutcome(
                early=self._fail(run, lease_context, "LOOP_BUDGET_EXHAUSTED")
            )
        try:
            raw_final = finalize(node, run, state)
            final_result = (
                raw_final
                if isinstance(raw_final, LoopIterationResult)
                else LoopIterationResult.model_validate(raw_final)
            )
        except Exception:  # noqa: BLE001 - 收尾失败/结论非法是结构性失败，fail closed。
            logging.warning(
                "bounded_loop finalize 失败或结论非法 run_id=%s code=LOOP_BODY_FAILED",
                run.run_id,
            )
            return _LoopNodeOutcome(
                early=self._fail(run, lease_context, "LOOP_BODY_FAILED")
            )
        if final_result.outcome != "complete" and final_result.outcome != "partial":
            # finalize 必须给出终态结论；failed/continue 均为结构性不完整。
            logging.warning(
                "bounded_loop finalize 结论非终态 run_id=%s outcome=%s code=LOOP_BODY_FAILED",
                run.run_id,
                final_result.outcome,
            )
            return _LoopNodeOutcome(
                early=self._fail(run, lease_context, "LOOP_BODY_FAILED")
            )
        return self._bounded_loop_outcome(
            node, run, lease_context, final_result, progress, usage_before
        )

    def _bounded_loop_outcome(
        self,
        node: dict[str, object],
        run: AgentRun,
        lease_context: LeaseContext,
        final_result: LoopIterationResult,
        progress: _LoopProgress,
        usage_before: int,
    ) -> _LoopNodeOutcome:
        """判定段：partial 触发条件合并 + 安全审计 + 循环摘要节点结果。"""
        # partial 的触发条件（冻结语义 7）：额度耗尽（策略=partial）、迭代
        # 声明 partial、存在被跳过的失败迭代、finalize 判定 partial。
        final_outcome = (
            "partial"
            if (
                progress.exhausted
                or progress.declared_partial
                or progress.iteration_errors > 0
                or final_result.outcome == "partial"
            )
            else "complete"
        )
        reason_code = (
            progress.reason_code
            or final_result.reason_code
            or ("LOOP_COMPLETE" if final_outcome == "complete" else "LOOP_PARTIAL")
        )
        usage_now, _, _ = usage_snapshot(self._session, run.run_id)
        loop_summary = {
            "iterations": progress.iterations,
            "outcome": final_outcome,
            "reason_code": reason_code,
            "output_count": progress.output_count,
            "coverage_count": progress.coverage_count,
            "model_calls_used": max(0, usage_now - usage_before),
        }
        self._record_bounded_loop_audit(run, final_outcome, reason_code)
        return _LoopNodeOutcome(
            node_result={
                "node_id": str(node["node_id"]),
                "loop_outcome": final_outcome,
                "loop_summary": loop_summary,
            },
            partial=final_outcome == "partial",
        )

    def _record_bounded_loop_audit(
        self, run: AgentRun, outcome: str, reason_code: str
    ) -> None:
        """bounded_loop 审计：只记结论/原因码与定位字段。

        计数明细（轮次/输出/覆盖/用量）落在 AgentStep.output_summary；两处都
        不含 Scene 正文、素材 digest、prompt 或模型原始输出。
        """
        AuditService(session=self._session).append(
            RuntimeAuditEvent(
                audit_id=str(uuid4()),
                actor_type="system",
                actor_id="workflow_executor",
                action="agent_run_bounded_loop",
                resource_type="agent_run",
                resource_id=run.run_id,
                reason_code=reason_code,
                outcome=outcome,
                occurred_at=datetime.now(UTC),
                trace_id=run.trace_id,
                metadata_summary={"run_id": run.run_id, "status": outcome},
            )
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

    def _authorization_changed(self, run: AgentRun) -> bool:
        if self._authorization_version_resolver is None:
            return False
        current = self._authorization_version_resolver(run)
        return current is None or current != run.authorization_version

    def _package_revoked(self, run: AgentRun) -> bool:
        """Run 的冻结 Package 缺失、非 active 或 digest 漂移时均禁止执行。"""
        definition = self._session.scalar(
            select(AgentDefinition).where(
                AgentDefinition.agent_id == run.agent_id,
                AgentDefinition.version == run.agent_version,
            )
        )
        return (
            definition is None
            or definition.status != "active"
            or definition.package_digest != run.package_digest
        )

    def _record_authorization_change(self, run: AgentRun) -> None:
        """只审计版本失配结论，不记录授权配置、输入或节点数据。"""
        AuditService(session=self._session).append(
            RuntimeAuditEvent(
                audit_id=str(uuid4()), actor_type="system", actor_id="workflow_executor",
                action="agent_run_authorization_changed", resource_type="agent_run",
                resource_id=run.run_id, reason_code="AUTHORIZATION_CHANGED",
                outcome="cancel_requested", occurred_at=datetime.now(UTC),
                trace_id=run.trace_id, metadata_summary={"status": run.status},
            )
        )

    def _record_package_revoked(self, run: AgentRun) -> None:
        """仅审计撤销结论，禁止携带 Package 内容或 Run 输入。"""
        AuditService(session=self._session).append(
            RuntimeAuditEvent(
                audit_id=str(uuid4()), actor_type="system", actor_id="workflow_executor",
                action="agent_run_package_revoked", resource_type="agent_run",
                resource_id=run.run_id, reason_code="PACKAGE_REVOKED",
                outcome="cancel_requested", occurred_at=datetime.now(UTC),
                trace_id=run.trace_id, metadata_summary={"status": run.status},
            )
        )
