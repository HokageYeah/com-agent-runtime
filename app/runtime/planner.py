from __future__ import annotations

import logging
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import AgentPlan
from app.schemas.agent_package import AgentPackage, WorkflowNodeDefinition
from app.schemas.plan import AgentPlanDTO

DEFAULT_STOP_CONDITIONS: dict[str, int | float] = {
    "max_steps": 16,
    "max_model_calls": 8,
    "max_tool_calls": 20,
    "max_estimated_cost": 2.0,
    "max_run_seconds": 300,
    "held_ttl_seconds": 600,
    "queue_ttl_seconds": 900,
    "approval_ttl_seconds": 86_400,
    "max_wall_clock_seconds": 172_800,
}

DEFAULT_FALLBACK_POLICY: dict[str, str] = {
    "default": "failed",
    "media": "skipped(capability_disabled)",
}


class StaticPlanValidationError(ValueError):
    """注册定义未提供可信静态工作流时拒绝创建可执行计划。"""


class StaticPlanner:
    """仅将受信任 Package 的静态节点清单转换为可审计计划，不调用模型。"""

    def create_plan(self, run_id: str, package: AgentPackage) -> AgentPlanDTO:
        steps = [node.model_dump(mode="json") for node in package.workflow_nodes]
        logging.info("生成静态 AgentPlan run_id=%s steps=%s", run_id, len(steps))
        return AgentPlanDTO(
            plan_id=str(uuid4()),
            run_id=run_id,
            strategy="static_workflow",
            steps=steps,
            stop_conditions=DEFAULT_STOP_CONDITIONS.copy(),
            fallback_policy=DEFAULT_FALLBACK_POLICY.copy(),
            status="planned",
        )

    def create_plan_from_definition(
        self, run_id: str, definition: dict[str, object]
    ) -> AgentPlanDTO:
        """从已注册的受信任定义生成计划，避免 API 层再读取业务文件。"""
        raw_nodes = definition.get("workflow_nodes", [])
        steps = self._freeze_definition_steps(run_id, raw_nodes)
        if not steps:
            # 旧注册记录尚未写入节点摘要时保留明确的空计划，Worker 会安全失败，
            # 绝不凭 agent_id 猜测或执行任意工作流。
            logging.warning("AgentDefinition 缺少 workflow_nodes run_id=%s", run_id)
        fallback_policy = DEFAULT_FALLBACK_POLICY.copy()
        policy = definition.get("policy", {})
        timeout_action = (
            policy.get("waiting_human_timeout_action")
            if isinstance(policy, dict)
            else None
        )
        if timeout_action in {"fallback", "failed", "cancelled"}:
            fallback_policy["waiting_human_timeout_action"] = timeout_action
        reject_action = policy.get("reject_action") if isinstance(policy, dict) else None
        if reject_action in {"fallback", "failed"}:
            fallback_policy["reject_action"] = reject_action
        fallback_node_id = (
            policy.get("waiting_human_fallback_node")
            if isinstance(policy, dict)
            else None
        )
        step_node_ids = {
            node.get("node_id")
            for node in steps
            if isinstance(node, dict) and isinstance(node.get("node_id"), str)
        }
        if isinstance(fallback_node_id, str) and fallback_node_id in step_node_ids:
            fallback_policy["waiting_human_fallback_node"] = fallback_node_id
        return AgentPlanDTO(
            plan_id=str(uuid4()),
            run_id=run_id,
            strategy="static_workflow",
            steps=steps,
            stop_conditions=DEFAULT_STOP_CONDITIONS.copy(),
            fallback_policy=fallback_policy,
            status="planned",
        )

    @staticmethod
    def _freeze_definition_steps(run_id: str, raw_nodes: object) -> list[dict[str, object]]:
        """仅冻结已注册的静态节点；拒绝原样透传畸形定义。"""
        if not isinstance(raw_nodes, list):
            logging.warning("AgentDefinition workflow_nodes 格式无效 run_id=%s", run_id)
            return []
        try:
            # 复用 Package loader 相同的节点 schema，不解释字符串、更不执行 Python。
            nodes = [WorkflowNodeDefinition.model_validate(item) for item in raw_nodes]
        except (TypeError, ValueError):
            logging.warning("拒绝非静态 workflow_nodes run_id=%s", run_id)
            raise StaticPlanValidationError("workflow_nodes 必须是静态节点定义") from None
        # P1 legacy 缺键 guard：WorkflowNodeDefinition.safe_to_rerun 有默认 False，
        # 上方 model_validate 会把"缺键"悄悄补成"显式 False"并经 model_dump 落库——
        # 于是 Executor 的 PLAN_LEGACY_DEFINITION guard（safe_to_rerun not in node）永不
        # 触发，legacy 缺键被静默吞掉，resume 可能跳过本该重算的 memoir 节点。故此处
        # 必须回看原始 dict：节点未显式声明 safe_to_rerun 即视为 legacy 定义，无法安全
        # 判定 resume 跳过 vs 重算，一律 fail closed 交业务侧 undo/purge 重建。Planner
        # 不硬编码 memoir 节点名：任何缺键节点都拒绝，与 business_type 解耦。
        for item in raw_nodes:
            if not isinstance(item, dict) or "safe_to_rerun" not in item:
                logging.warning(
                    "拒绝 legacy workflow_nodes（节点缺 safe_to_rerun）run_id=%s", run_id
                )
                raise StaticPlanValidationError(
                    "workflow_nodes 节点必须显式声明 safe_to_rerun"
                )
        return [node.model_dump(mode="json") for node in nodes]

    def persist(self, session: Session, plan: AgentPlanDTO) -> AgentPlan:
        """Plan 与 Run 同属权威运行库；只保存节点摘要，不保存任何私密执行 state。"""
        record = AgentPlan(
            plan_id=plan.plan_id,
            run_id=plan.run_id,
            strategy=plan.strategy,
            steps_json=plan.steps,
            stop_conditions_json=plan.stop_conditions,
            fallback_policy_json=plan.fallback_policy,
            status=plan.status,
        )
        session.add(record)
        logging.info(
            "静态 AgentPlan 已落库 run_id=%s plan_id=%s", plan.run_id, plan.plan_id
        )
        return record
