"""M7 bounded_loop 受控循环的冻结执行语义（数据与纯函数层）。

调度与逐轮安全检查在 :mod:`app.runtime.executor`；本模块只承载四件事：

- 迭代结果封闭模型（``LoopIterationResult``）：只允许
  continue/complete/partial/failed + 安全计数 + 完成原因，禁止自由字段。
- Run 级限额继承档案（``InheritedLoopBudget``）：首版唯一 budget_profile
  ``inherit_run_limits_v1``，从 Run 冻结的剩余
  max_model_calls/max_tokens/max_model_cost/max_run_seconds 导出上限；
  任一必要字段缺失/非有限正值/余额为零即 fail closed。
- loop_policy 冻结字段的执行期复验（plan 已由 schema 冻结，这里只防
  手工植入的畸形 plan）。
- ``append_unique_by_key``（merge_key=scene_id）合并的纯函数实现。

安全边界：本模块的返回值与日志都不得携带 Scene 正文、素材 digest、
prompt 或模型原始输出——只有轮次、原因码与安全计数。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.runtime.policy_engine import PolicyEngine
from app.runtime.state import AgentState


class LoopBudgetError(RuntimeError):
    """预算档案 fail closed；code 区分字段非法与余额为零。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LoopIterationResult(BaseModel):
    """单轮迭代/收尾结论；形状封闭，禁止夹带正文。

    - ``outcome``：continue=继续下一轮；complete=全部工作完成；
      partial=结构完整但未全部覆盖（按部分结果收尾）；failed=结构性不完整。
    - ``output_count`` / ``coverage_count``：本轮新增产物与覆盖素材的安全计数。
    - ``reason_code``：受控完成原因码，绝不携带模型输出或素材内容。
    """

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["continue", "complete", "partial", "failed"]
    reason_code: str | None = None
    output_count: int = Field(default=0, ge=0)
    coverage_count: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class InheritedLoopBudget:
    """继承 Run 冻结限额导出的循环预算快照。

    - ``max_iterations``：循环启动时冻结的迭代上限 = 剩余 model call；
      每轮迭代至多一次模型调用，由 Executor 逐轮核对 usage 账本。
    - ``remaining_*``：当前余额；begin_loop/每轮迭代收到的是最新重算值，
      Runner 据此切批（单批不得超过 ModelGateway.context_token_budget 与
      Run 剩余 token，单条仍超限则拒绝且不截断——Runner 侧职责）。
    """

    max_iterations: int
    remaining_model_calls: int
    remaining_tokens: int
    remaining_cost: float
    remaining_ms: int


# 首版唯一取值：预算继承 Run 级限额 + 按键去重追加 + 单轮错误跳过继续。
_FROZEN_LOOP_LITERALS: Mapping[str, str] = {
    "budget_strategy": "inherit_run_limits_v1",
    "merge_strategy": "append_unique_by_key",
    "merge_key": "scene_id",
    "on_iteration_error": "continue",
}
_VALID_ON_BUDGET_EXHAUSTED = frozenset({"partial", "failed"})


def validated_loop_policy(node: Mapping[str, object]) -> dict[str, object] | None:
    """执行期复验冻结 loop_policy；字段漂移一律按非法处理（fail closed）。"""
    policy = node.get("loop_policy")
    if not isinstance(policy, Mapping):
        return None
    for key, expected in _FROZEN_LOOP_LITERALS.items():
        if policy.get(key) != expected:
            return None
    if policy.get("on_budget_exhausted") not in _VALID_ON_BUDGET_EXHAUSTED:
        return None
    body_ids = policy.get("body_node_ids")
    if (
        not isinstance(body_ids, list)
        or not body_ids
        or not all(isinstance(item, str) and item for item in body_ids)
    ):
        return None
    return dict(policy)


def _positive_int(policy: Mapping[str, object], key: str) -> int | None:
    value = policy.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _positive_finite(policy: Mapping[str, object], key: str) -> float | None:
    value = policy.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        return None
    return float(value)


def _frozen_limits(run: object) -> tuple[int, int, float, int]:
    """读取 Run 冻结的四个必要额度字段；缺失/非有限正值即 fail closed。"""
    snapshot = getattr(run, "capability_snapshot_json", None)
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    model_policy = snapshot.get("model_policy")
    execution_policy = snapshot.get("execution_policy")
    model_policy = model_policy if isinstance(model_policy, Mapping) else {}
    execution_policy = execution_policy if isinstance(execution_policy, Mapping) else {}
    max_calls = _positive_int(model_policy, "max_model_calls")
    max_tokens = _positive_int(model_policy, "max_tokens")
    max_cost = _positive_finite(model_policy, "max_model_cost")
    max_seconds = _positive_int(execution_policy, "max_run_seconds")
    if (
        max_calls is None
        or max_tokens is None
        or max_cost is None
        or max_seconds is None
    ):
        raise LoopBudgetError("LOOP_BUDGET_PROFILE_INVALID")
    return max_calls, max_tokens, max_cost, max_seconds


def usage_snapshot(session: Session, run_id: str) -> tuple[int, int, float]:
    """汇总同 Run 的持久 usage 账本（预留行也计入额度）。

    计量口径与 PolicyEngine 完全一致：token 优先已观察 input/output，
    未知调用保守回退预留输入 token；成本优先实际/估算值再回退预留值。
    """
    # 与 policy_engine 相同的延迟导入，避免 Settings 加载路径出现 ORM 导入环。
    from app.models import AgentModelUsage

    rows = session.scalars(
        select(AgentModelUsage).where(AgentModelUsage.run_id == run_id)
    ).all()
    used_tokens = sum(PolicyEngine._usage_tokens(usage) for usage in rows)
    used_cost = sum(PolicyEngine._usage_cost(usage) for usage in rows)
    return len(rows), used_tokens, used_cost


def derive_inherited_budget(session: Session, run: object) -> InheritedLoopBudget:
    """从 Run 冻结限额导出循环预算；字段非法或余额为零均拒绝启动。"""
    max_calls, max_tokens, max_cost, max_seconds = _frozen_limits(run)
    calls, used_tokens, used_cost = usage_snapshot(session, str(run.run_id))
    remaining_calls = max_calls - calls
    remaining_tokens = max_tokens - used_tokens
    remaining_cost = max_cost - used_cost
    remaining_ms = max_seconds * 1000 - int(getattr(run, "active_elapsed_ms", 0) or 0)
    if (
        remaining_calls <= 0
        or remaining_tokens <= 0
        or remaining_cost <= 0
        or remaining_ms <= 0
    ):
        # 余额为零即 fail closed：bounded_loop 节点拒绝启动并置 failed，
        # 即便 on_budget_exhausted=partial 也不得用零预算伪造空循环成功。
        raise LoopBudgetError("LOOP_BUDGET_EXHAUSTED")
    return InheritedLoopBudget(
        max_iterations=remaining_calls,
        remaining_model_calls=remaining_calls,
        remaining_tokens=remaining_tokens,
        remaining_cost=remaining_cost,
        remaining_ms=remaining_ms,
    )


def recompute_loop_remaining(
    session: Session, run: object, base: InheritedLoopBudget, loop_elapsed_ms: float
) -> InheritedLoopBudget:
    """逐轮重算余额（不抛错）；任一维度触底由调用方按 on_budget_exhausted 收敛。"""
    max_calls, max_tokens, max_cost, max_seconds = _frozen_limits(run)
    calls, used_tokens, used_cost = usage_snapshot(session, str(run.run_id))
    consumed_ms = int(getattr(run, "active_elapsed_ms", 0) or 0) + int(loop_elapsed_ms)
    return InheritedLoopBudget(
        max_iterations=base.max_iterations,
        remaining_model_calls=max_calls - calls,
        remaining_tokens=max_tokens - used_tokens,
        remaining_cost=max_cost - used_cost,
        remaining_ms=max_seconds * 1000 - consumed_ms,
    )


def merge_unique_scenes(state: AgentState) -> int:
    """append_unique_by_key：按 scene_id 去重，稳定保留首次出现。

    覆盖式合并被禁止——重复 key 的后续产物直接丢弃，返回被丢弃条数。
    只读取 scene_id 键，不触碰正文字段。
    """
    scenes = state.scenes
    if not scenes:
        return 0
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for scene in scenes:
        key = scene.get("scene_id") if isinstance(scene, dict) else None
        if isinstance(key, str):
            if key in seen:
                continue
            seen.add(key)
        merged.append(scene)
    removed = len(scenes) - len(merged)
    if removed:
        logging.info(
            "bounded_loop 迭代产物按键去重 merge_key=scene_id removed_count=%d",
            removed,
        )
        state.scenes = merged
    return removed
