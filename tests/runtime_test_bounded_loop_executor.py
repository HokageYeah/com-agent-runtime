"""M7：bounded_loop 受控循环 executor 执行语义回归测试。

只用合成 workflow 定义（bounded_loop 节点 + 普通 body/前置节点）驱动
WorkflowExecutor，不依赖 1.0.5 包目录；冻结语义见 app/runtime/bounded_loop.py。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from pytest import raises
from sqlalchemy import create_engine, select
from sqlalchemy.orm import object_session, sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import (
    AgentArtifact,
    AgentCheckpoint,
    AgentDefinition,
    AgentModelUsage,
    AgentPlan,
    AgentRun,
    AgentStep,
    RuntimeAuditRecord,
)
from app.runtime.artifact import ArtifactStore
from app.runtime.bounded_loop import InheritedLoopBudget, LoopIterationResult
from app.runtime.checkpoint import CheckpointStore, FernetCheckpointCipher
from app.runtime.executor import WorkflowExecutor
from app.runtime.interfaces import LeaseContext
from app.runtime.state import AgentState

# ---------------------------------------------------------------------------
# 合成夹具：Run 冻结额度 / 计划节点 / 循环 Runner 脚本
# ---------------------------------------------------------------------------

# Run 冻结能力快照：bounded_loop 要求四个必要额度字段全部为有限正值。
DEFAULT_LOOP_SNAPSHOT: dict[str, Any] = {
    "model_policy": {"max_model_calls": 4, "max_tokens": 10_000, "max_model_cost": 2.0},
    "execution_policy": {"max_run_seconds": 300, "max_steps": 16},
}

LOOP_NODE: dict[str, Any] = {
    "node_id": "generate_scene_batches",
    "node_type": "bounded_loop",
    "safe_to_rerun": True,
    "loop_policy": {
        "budget_strategy": "inherit_run_limits_v1",
        "merge_strategy": "append_unique_by_key",
        "merge_key": "scene_id",
        "on_iteration_error": "continue",
        "on_budget_exhausted": "partial",
        "body_node_ids": ["generate_scene_batch"],
    },
}

PREPARE_NODE: dict[str, Any] = {
    "node_id": "prepare_materials",
    "node_type": "tool",
    "safe_to_rerun": True,
}

# checkpoint 白名单键；循环中间产物（Scene/正文/素材 digest）绝不能出现在密文里。
SAFE_CHECKPOINT_KEYS = {
    "completed_steps",
    "completed_node_ids",
    "fallback_flags",
    "resume_from_node_id",
}

# RuntimeAuditEvent.metadata_summary 的 schema 级白名单（见 app/schemas/audit.py）。
AUDIT_METADATA_WHITELIST = {
    "content_digest_prefix",
    "decision",
    "dispatch_state",
    "manual_retry_count",
    "privacy_version",
    "run_id",
    "status",
}


@dataclass
class IterScript:
    """单轮迭代脚本：产出场景 / 抛错 / 副作用（如中途改授权）三选一驱动。"""

    outcome: str = "continue"
    scenes: tuple[dict[str, Any], ...] = ()
    raise_exc: Exception | None = None
    mutate: Callable[[AgentRun], None] | None = None
    reason_code: str | None = None
    output_count: int = 0
    coverage_count: int = 0


class ScriptedLoopRunner:
    """实现 begin_loop / run_loop_iteration / finalize_loop 三段接口的合成 Runner。"""

    def __init__(
        self,
        iterations: list[IterScript],
        *,
        finalize_outcome: str = "complete",
        extra_state_mutate: Callable[[AgentState], None] | None = None,
    ) -> None:
        self._iterations = iterations
        self._finalize_outcome = finalize_outcome
        self._extra_state_mutate = extra_state_mutate
        self.begin_calls = 0
        self.finalize_calls = 0
        self.iteration_calls: list[int] = []
        self.captured_state: AgentState | None = None
        self.iteration_budgets: list[InheritedLoopBudget] = []
        self.run_node_calls: list[str] = []
        self.auth_box = {"version": 1}

    # -- 通用节点路径（非循环节点） ------------------------------------------
    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]:
        self.run_node_calls.append(str(node["node_id"]))
        return {"node_id": node["node_id"], "result": "ok"}

    # -- 受控循环三段接口 ----------------------------------------------------
    def begin_loop(
        self,
        node: dict[str, object],
        run: AgentRun,
        state: AgentState,
        budget: InheritedLoopBudget,
    ) -> None:
        self.begin_calls += 1
        self.captured_state = state

    def run_loop_iteration(
        self,
        node: dict[str, object],
        run: AgentRun,
        state: AgentState,
        iteration_index: int,
        budget: InheritedLoopBudget,
    ) -> LoopIterationResult:
        self.iteration_calls.append(iteration_index)
        self.iteration_budgets.append(budget)
        if self._extra_state_mutate is not None:
            self._extra_state_mutate(state)
        script = self._iterations[iteration_index - 1]
        if script.mutate is not None:
            script.mutate(run)
        if script.raise_exc is not None:
            raise script.raise_exc
        if script.scenes:
            state.scenes = [*(state.scenes or []), *script.scenes]
        return LoopIterationResult(
            outcome=script.outcome,
            reason_code=script.reason_code,
            output_count=script.output_count,
            coverage_count=script.coverage_count,
        )

    def finalize_loop(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> LoopIterationResult:
        self.finalize_calls += 1
        return LoopIterationResult(
            outcome=self._finalize_outcome, reason_code="FINALIZE_DONE"
        )


@dataclass
class LoopScenario:
    """一次 bounded_loop 执行的完整夹具；每个用例独立 engine/session。"""

    runner: ScriptedLoopRunner
    snapshot: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_LOOP_SNAPSHOT))
    seed_usages: int = 0
    resolver: Callable[[AgentRun], int | None] | None = None

    def __post_init__(self) -> None:
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)()
        self.now = datetime.now(UTC)
        self.cipher = FernetCheckpointCipher.generate()
        self._add_run()
        self.executor = WorkflowExecutor(
            self.session,
            self.runner,
            CheckpointStore(self.session, self.cipher),
            ArtifactStore(self.session),
            authorization_version_resolver=self.resolver,
        )

    def _add_run(self) -> None:
        run_id = "loop-run"
        self.session.add(
            AgentRun(
                run_id=run_id,
                agent_id="memoir_agent",
                agent_version="1.0.5",
                package_digest="sha256:test",
                contract_version="1.0.0",
                business_type="couple_memory",
                business_id="archive",
                status="pending",
                dispatch_state="claimed",
                input_json={},
                capability_snapshot_json=self.snapshot,
                authorization_version=1,
                caller_id="caller",
                tenant_id="tenant",
                create_idempotency_key="key",
                callback_target_id="callback",
                business_connector_id="connector",
                trace_id="trace",
                execution_attempt=1,
                lease_owner="worker-a",
                fencing_token=1,
                lease_expires_at=self.now + timedelta(seconds=60),
                run_deadline_at=self.now + timedelta(days=1),
            )
        )
        self.session.add(
            AgentDefinition(
                agent_id="memoir_agent",
                version="1.0.5",
                runtime_type="workflow",
                definition_json={},
                package_digest="sha256:test",
                contract_version="1.0.0",
                status="active",
                status_changed_at=self.now,
                status_changed_by="test",
                status_change_reason="fixture",
            )
        )
        self.session.add(
            AgentPlan(
                plan_id=f"{run_id}-plan",
                run_id=run_id,
                strategy="static_workflow",
                steps_json=[dict(PREPARE_NODE), dict(LOOP_NODE)],
                stop_conditions_json={},
                fallback_policy_json={},
                status="planned",
            )
        )
        for index in range(self.seed_usages):
            self.session.add(
                AgentModelUsage(
                    id=uuid4().int >> 65,
                    usage_id=str(uuid4()),
                    run_id=run_id,
                    step_id=f"{run_id}-seed-{index}",
                    execution_attempt=1,
                    model_attempt=1,
                    status="succeeded",
                    capability_snapshot_json={},
                    provider="provider",
                    model="model",
                    pricing_config_version="v1",
                    cost_unit="USD",
                    reserved_estimated_cost=0.01,
                    reserved_tokens=100,
                    input_tokens=100,
                    output_tokens=10,
                    estimated_cost=0.01,
                    request_deadline_at=self.now + timedelta(minutes=5),
                )
            )
        self.session.commit()

    def lease(self) -> LeaseContext:
        return LeaseContext(
            execution_attempt=1,
            lease_owner="worker-a",
            fencing_token=1,
            lease_expires_at=self.now + timedelta(seconds=60),
            privacy_version=1,
            authorization_version=1,
        )

    def run(self):
        return self.executor.run("loop-run", self.lease())

    def checkpoint_state(self) -> dict[str, Any]:
        return CheckpointStore(
            self.session, self.cipher
        ).load_latest("loop-run", self.lease())

    def steps(self) -> list[AgentStep]:
        return self.session.scalars(select(AgentStep)).all()

    def loop_step(self) -> AgentStep:
        return next(step for step in self.steps() if step.step_name == LOOP_NODE["node_id"])


def _revoke_package(run: AgentRun) -> None:
    """迭代副作用：撤销 Run 冻结的 Package，模拟逐轮安全检查触发点。"""
    definition = object_session(run).scalar(
        select(AgentDefinition).where(
            AgentDefinition.agent_id == run.agent_id,
            AgentDefinition.version == run.agent_version,
        )
    )
    assert definition is not None
    definition.status = "revoked"


# ---------------------------------------------------------------------------
# 1. 循环生命周期与迭代结果封闭枚举
# ---------------------------------------------------------------------------

def test_bounded_loop_lifecycle_begin_iterate_finalize() -> None:
    """begin/iteration/finalize 三段按序执行；两轮 continue 后由 complete 收敛。"""
    runner = ScriptedLoopRunner(
        [
            IterScript(outcome="continue", output_count=2, coverage_count=5),
            IterScript(outcome="complete", output_count=1, coverage_count=3),
        ]
    )
    scenario = LoopScenario(runner)

    result = scenario.run()

    assert result.status == "succeeded"
    assert runner.begin_calls == 1
    assert runner.finalize_calls == 1
    assert runner.iteration_calls == [1, 2]
    # 非循环节点仍走通用 run_node 路径，循环节点不再进入 run_node。
    assert runner.run_node_calls == ["prepare_materials"]
    # 预算继承：迭代上限 = 剩余 model call（无既有 usage 时为 4）。
    assert all(budget.max_iterations == 4 for budget in runner.iteration_budgets)


def test_loop_iteration_result_outcome_enum_is_closed() -> None:
    """迭代结果只允许 continue/complete/partial/failed + 安全计数 + 完成原因。"""
    for outcome in ("continue", "complete", "partial", "failed"):
        assert LoopIterationResult(outcome=outcome).outcome == outcome
    # 未知 outcome / 越界自由字段（如 prompt 正文）都必须被拒绝。
    with raises(ValidationError):
        LoopIterationResult(outcome="retry")
    with raises(ValidationError):
        LoopIterationResult.model_validate({"outcome": "continue", "prompt": "leaked-body"})


# ---------------------------------------------------------------------------
# 2. 预算继承 fail closed
# ---------------------------------------------------------------------------

def test_bounded_loop_refuses_to_start_without_positive_frozen_limits() -> None:
    """Run 级额度字段缺失/零值/负值 → 拒绝启动置 failed，不进入循环体。"""
    missing_calls = dict(DEFAULT_LOOP_SNAPSHOT)
    missing_calls["model_policy"] = {"max_tokens": 10_000, "max_model_cost": 2.0}
    zero_tokens = dict(DEFAULT_LOOP_SNAPSHOT)
    zero_tokens["model_policy"] = {"max_model_calls": 4, "max_tokens": 0, "max_model_cost": 2.0}
    negative_cost = dict(DEFAULT_LOOP_SNAPSHOT)
    negative_cost["model_policy"] = {
        "max_model_calls": 4,
        "max_tokens": 10_000,
        "max_model_cost": -1.0,
    }

    for snapshot in (missing_calls, zero_tokens, negative_cost):
        runner = ScriptedLoopRunner([IterScript(outcome="complete")])
        scenario = LoopScenario(runner, snapshot=snapshot)
        result = scenario.run()
        assert result.status == "failed"
        assert result.error_code == "LOOP_BUDGET_PROFILE_INVALID"
        # fail closed：循环体一次都没有进入。
        assert runner.begin_calls == 0
        assert scenario.loop_step().status == "failed"
        assert scenario.loop_step().error_code == "LOOP_BUDGET_PROFILE_INVALID"


def test_bounded_loop_refuses_to_start_when_no_remaining_budget() -> None:
    """余额为零（model call 已耗尽）同样拒绝启动并置 failed。"""
    runner = ScriptedLoopRunner([IterScript(outcome="complete")])
    scenario = LoopScenario(runner, seed_usages=4)  # max_model_calls=4 全部用完

    result = scenario.run()

    assert result.status == "failed"
    assert result.error_code == "LOOP_BUDGET_EXHAUSTED"
    assert runner.begin_calls == 0


# ---------------------------------------------------------------------------
# 3. 迭代上限 = 剩余 model call；耗尽按 on_budget_exhausted=partial 收敛
# ---------------------------------------------------------------------------

def test_bounded_loop_iteration_cap_is_remaining_model_calls() -> None:
    """剩余 model call=3（4 限额-1 已用）→ 至多 3 轮；耗尽后按 partial 收敛。"""
    runner = ScriptedLoopRunner(
        [
            IterScript(outcome="continue", output_count=1, coverage_count=2),
            IterScript(outcome="continue", output_count=1, coverage_count=2),
            IterScript(outcome="continue", output_count=1, coverage_count=2),
            IterScript(outcome="continue", output_count=1, coverage_count=2),
        ]
    )
    scenario = LoopScenario(runner, seed_usages=1)

    result = scenario.run()

    # 4 轮脚本只执行 3 轮：上限来自启动时剩余 model call。
    assert runner.iteration_calls == [1, 2, 3]
    assert runner.finalize_calls == 1
    assert result.status == "partial"
    loop_step = scenario.loop_step()
    assert loop_step.status == "succeeded"
    summary = loop_step.output_summary["loop"]
    assert summary["iterations"] == 3
    assert summary["outcome"] == "partial"
    assert summary["reason_code"] == "LOOP_BUDGET_EXHAUSTED"
    assert summary["output_count"] == 3
    assert summary["coverage_count"] == 6


def test_bounded_loop_budget_exhausted_fails_when_policy_failed() -> None:
    """on_budget_exhausted=failed 时额度耗尽直接失败，不发布部分结果。"""
    loop_node = json.loads(json.dumps(LOOP_NODE))
    loop_node["loop_policy"]["on_budget_exhausted"] = "failed"
    runner = ScriptedLoopRunner(
        [IterScript(outcome="continue"), IterScript(outcome="continue")]
    )
    snapshot = dict(DEFAULT_LOOP_SNAPSHOT)
    snapshot["model_policy"] = dict(snapshot["model_policy"])
    snapshot["model_policy"]["max_model_calls"] = 1
    scenario = LoopScenario(runner, snapshot=snapshot)
    scenario.session.scalar(select(AgentPlan)).steps_json = [
        dict(PREPARE_NODE),
        loop_node,
    ]
    scenario.session.commit()

    result = scenario.run()

    assert result.status == "failed"
    assert result.error_code == "LOOP_BUDGET_EXHAUSTED"
    assert runner.iteration_calls == [1]
    assert runner.finalize_calls == 0


# ---------------------------------------------------------------------------
# 4. append_unique_by_key：迭代产物按 scene_id 去重后追加
# ---------------------------------------------------------------------------

def test_bounded_loop_merges_iterations_unique_by_scene_id() -> None:
    """两轮产出同 scene_id 只保留首次出现（稳定顺序追加，禁止覆盖）。"""
    runner = ScriptedLoopRunner(
        [
            IterScript(
                outcome="continue",
                scenes=(
                    {"scene_id": "scene-1", "body": "original"},
                    {"scene_id": "scene-2", "body": "keep"},
                ),
                output_count=2,
            ),
            IterScript(
                outcome="complete",
                scenes=(
                    {"scene_id": "scene-1", "body": "duplicate-must-not-win"},
                    {"scene_id": "scene-3", "body": "append"},
                ),
                output_count=2,
            ),
        ]
    )
    scenario = LoopScenario(runner)

    result = scenario.run()

    assert result.status == "succeeded"
    assert runner.captured_state is not None
    scenes = runner.captured_state.scenes or []
    assert [scene["scene_id"] for scene in scenes] == ["scene-1", "scene-2", "scene-3"]
    # 重复 key 保留第一轮产物：覆盖式合并被拒绝。
    assert scenes[0]["body"] == "original"


# ---------------------------------------------------------------------------
# 5. on_iteration_error=continue：单轮失败跳过继续，最终标记 partial
# ---------------------------------------------------------------------------

def test_bounded_loop_continues_after_iteration_error_and_marks_partial() -> None:
    """单迭代抛错只跳过该轮；异常正文不落库，最终结果标记 partial。"""
    runner = ScriptedLoopRunner(
        [
            IterScript(outcome="continue", output_count=1, coverage_count=2),
            IterScript(raise_exc=RuntimeError("MODEL_BODY_EXPLODED_SENTINEL")),
            IterScript(outcome="complete", output_count=1, coverage_count=2),
        ]
    )
    scenario = LoopScenario(runner)

    result = scenario.run()

    assert runner.iteration_calls == [1, 2, 3]
    assert runner.finalize_calls == 1
    assert result.status == "partial"
    loop_step = scenario.loop_step()
    assert loop_step.status == "succeeded"
    assert loop_step.output_summary["loop"]["outcome"] == "partial"
    # 异常正文绝不进入步骤/审计/产物任何落库面。
    persisted = json.dumps(
        [step.output_summary for step in scenario.steps()]
        + [step.error_message for step in scenario.steps()]
        + [
            record.metadata_summary
            for record in scenario.session.scalars(select(RuntimeAuditRecord)).all()
        ]
    )
    assert "MODEL_BODY_EXPLODED_SENTINEL" not in persisted


# ---------------------------------------------------------------------------
# 6. 逐轮安全检查：cancel / 授权变更 / package 撤销
# ---------------------------------------------------------------------------

def test_bounded_loop_stops_between_iterations_on_cancel_request() -> None:
    """迭代间检测到 cancel_requested → 循环终止，Run 置 cancelled。"""

    def request_cancel(run: AgentRun) -> None:
        run.cancel_requested_at = datetime.now(UTC)

    runner = ScriptedLoopRunner(
        [
            IterScript(outcome="continue", mutate=request_cancel),
            IterScript(outcome="complete"),
        ]
    )
    scenario = LoopScenario(runner)

    result = scenario.run()

    assert result.status == "cancelled"
    assert result.error_code == "CANCEL_REQUESTED"
    assert runner.iteration_calls == [1]
    assert runner.finalize_calls == 0


def test_bounded_loop_stops_between_iterations_on_authorization_change() -> None:
    """迭代间授权版本变更 → 循环终止并按既有语义置 cancelled + 审计。"""
    # 授权版本盒与 resolver 共享：迭代推进授权版本，下一轮守卫读到失配。
    auth_box = {"version": 1}

    def bump_authorization(run: AgentRun) -> None:
        auth_box["version"] = 99

    runner = ScriptedLoopRunner(
        [
            IterScript(outcome="continue", mutate=bump_authorization),
            IterScript(outcome="complete"),
        ]
    )
    scenario = LoopScenario(runner, resolver=lambda run: auth_box["version"])

    result = scenario.run()

    assert result.status == "cancelled"
    assert result.error_code == "AUTHORIZATION_CHANGED"
    assert runner.iteration_calls == [1]
    actions = [
        record.action
        for record in scenario.session.scalars(select(RuntimeAuditRecord)).all()
    ]
    assert "agent_run_authorization_changed" in actions


def test_bounded_loop_stops_between_iterations_on_package_revoked() -> None:
    """迭代间 Package 撤销 → 循环终止并按既有语义置 cancelled + 审计。"""
    runner = ScriptedLoopRunner(
        [
            IterScript(outcome="continue", mutate=_revoke_package),
            IterScript(outcome="complete"),
        ]
    )
    scenario = LoopScenario(runner)

    result = scenario.run()

    assert result.status == "cancelled"
    assert result.error_code == "PACKAGE_REVOKED"
    assert runner.iteration_calls == [1]
    actions = [
        record.action
        for record in scenario.session.scalars(select(RuntimeAuditRecord)).all()
    ]
    assert "agent_run_package_revoked" in actions


# ---------------------------------------------------------------------------
# 7. checkpoint 不含循环中间产物；resume 整节点重算
# ---------------------------------------------------------------------------

def test_bounded_loop_checkpoint_has_no_intermediates_and_resume_recomputes() -> None:
    """循环正文/场景不落 checkpoint；resume 后 bounded_loop 整节点重算而非续跑。"""
    runner = ScriptedLoopRunner(
        [
            IterScript(
                outcome="complete",
                scenes=(
                    {
                        "scene_id": "scene-1",
                        "body": "scene-private-marker",
                        "source_refs": ["diary:digest-sentinel"],
                    },
                ),
                output_count=1,
            ),
        ]
    )
    scenario = LoopScenario(runner)

    first = scenario.run()
    assert first.status == "succeeded"

    # 密文 checkpoint 只含恢复路由元数据；循环中间产物绝不进入。
    checkpoint_state = scenario.checkpoint_state()
    assert set(checkpoint_state) <= SAFE_CHECKPOINT_KEYS
    assert "scene-private-marker" not in json.dumps(checkpoint_state)
    assert "digest-sentinel" not in json.dumps(checkpoint_state)
    artifacts = scenario.session.scalars(select(AgentArtifact)).all()
    assert "scene-private-marker" not in json.dumps(
        [artifact.summary_json for artifact in artifacts]
    )

    # resume：bounded_loop 声明 safe_to_rerun=True → 整节点重算（begin 再次执行）。
    resumed = scenario.executor.resume("loop-run", scenario.lease())
    assert resumed.status == "succeeded"
    assert runner.begin_calls == 2
    assert runner.finalize_calls == 2
    # 重算后新增一轮步骤行：两次执行各 1 条循环节点步骤。
    loop_steps = [
        step for step in scenario.steps() if step.step_name == LOOP_NODE["node_id"]
    ]
    assert len(loop_steps) == 2
    # checkpoint 仍只有白名单键。
    assert set(scenario.checkpoint_state()) <= SAFE_CHECKPOINT_KEYS


# ---------------------------------------------------------------------------
# 8. 审计只记录轮次/原因/计数/用量，无正文哨兵
# ---------------------------------------------------------------------------

def test_bounded_loop_audit_records_only_safe_counts() -> None:
    """循环审计只含轮次/原因/输出覆盖计数/用量；Scene 正文与素材 digest 不落库。"""
    runner = ScriptedLoopRunner(
        [
            IterScript(
                outcome="continue",
                scenes=({"scene_id": "scene-1", "body": "scene-private-marker"},),
                output_count=1,
                coverage_count=7,
            ),
            IterScript(
                outcome="complete",
                scenes=({"scene_id": "scene-2", "body": "scene-private-marker"},),
                output_count=1,
                coverage_count=5,
            ),
        ]
    )
    scenario = LoopScenario(runner)

    result = scenario.run()

    assert result.status == "succeeded"
    loop_audits = [
        record
        for record in scenario.session.scalars(select(RuntimeAuditRecord)).all()
        if record.action == "agent_run_bounded_loop"
    ]
    assert len(loop_audits) == 1
    audit = loop_audits[0]
    # metadata_summary 受 schema 白名单约束，只允许定位字段。
    assert set(audit.metadata_summary) <= AUDIT_METADATA_WHITELIST
    assert audit.reason_code == "LOOP_COMPLETE"

    # 步骤摘要：只有安全计数键（轮次/结果/原因/输出/覆盖/用量）。
    summary = scenario.loop_step().output_summary
    assert set(summary) == {"node_id", "status", "loop"}
    assert set(summary["loop"]) == {
        "iterations",
        "outcome",
        "reason_code",
        "output_count",
        "coverage_count",
        "model_calls_used",
    }
    assert summary["loop"]["iterations"] == 2
    assert summary["loop"]["output_count"] == 2
    assert summary["loop"]["coverage_count"] == 12

    # 正文哨兵不出现在任何审计/步骤/checkpoint 落库面。
    persisted = json.dumps(
        [
            record.metadata_summary
            for record in scenario.session.scalars(select(RuntimeAuditRecord)).all()
        ]
        + [step.output_summary for step in scenario.steps()]
        + [checkpoint.state_summary for checkpoint in scenario.session.scalars(
            select(AgentCheckpoint)
        ).all()]
    )
    assert "scene-private-marker" not in persisted


# ---------------------------------------------------------------------------
# 9. 1.0.6 兼容：冻结循环语义对 1.0.6 Run 原样成立
# ---------------------------------------------------------------------------

def test_bounded_loop_frozen_semantics_hold_for_1_0_6_run() -> None:
    """1.0.6 兼容：迭代错误 continue / partial 降级 / 迭代上限对 1.0.6 Run 不变。

    1.0.6 的批次重试语义全部在 memoir runner 内按 agent_version 门控实现，
    executor 侧冻结语义必须与版本无关：单轮失败（如 runner 抛出的
    LOOP_BATCH_OUTPUT_INVALID 受控原因码）只跳过该轮继续，即使后续轮
    恢复收敛，按冻结语义 7 Run 终态仍降级 partial（发布链路继续走完）。
    """
    runner = ScriptedLoopRunner(
        [
            IterScript(outcome="continue", output_count=1, coverage_count=2),
            IterScript(raise_exc=RuntimeError("LOOP_BATCH_OUTPUT_INVALID")),
            IterScript(outcome="complete", output_count=1, coverage_count=2),
        ]
    )
    scenario = LoopScenario(runner)
    # 夹具默认建 1.0.5 Run；这里把 Run 与 AgentDefinition 一同切到 1.0.6，
    # 验证 executor 循环语义不随 agent_version 变化。
    run = scenario.session.scalar(select(AgentRun))
    run.agent_version = "1.0.6"
    definition = scenario.session.scalar(select(AgentDefinition))
    definition.version = "1.0.6"
    scenario.session.commit()

    result = scenario.run()

    assert runner.iteration_calls == [1, 2, 3]
    assert runner.finalize_calls == 1
    # 预算继承与迭代上限照常：无既有 usage 时上限 = max_model_calls = 4。
    assert all(budget.max_iterations == 4 for budget in runner.iteration_budgets)
    # 冻结语义 7：存在被跳过的失败迭代 → 循环收敛 partial，Run 终态降级 partial。
    assert result.status == "partial"
    loop_step = scenario.loop_step()
    assert loop_step.status == "succeeded"
    assert loop_step.output_summary["loop"]["outcome"] == "partial"
    assert loop_step.output_summary["loop"]["iterations"] == 3
