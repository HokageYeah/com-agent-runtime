"""将已冻结 AgentPlan 编译为受控的 LangGraph 线性 StateGraph。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

_NODE_ID = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


class StaticWorkflowGraphError(ValueError):
    """静态 Plan 不能安全映射为图时的受控错误。"""


class _GraphState(TypedDict):
    visited_node_ids: list[str]


class StaticWorkflowGraph:
    """仅接受计划库中已冻结的线性 DAG，图节点不承载动态指令或业务输入。

    LangGraph 在这里负责验证、编译和固定调度顺序；节点真实副作用仍由
    WorkflowExecutor 在每个图节点前后执行 lease/fencing 条件闸。因此 Package、
    模型输出或请求输入不能新增节点、边或改变路由。
    """

    def __init__(self, nodes: list[dict[str, object]]) -> None:
        self._nodes = nodes

    @classmethod
    def build(cls, raw_nodes: object) -> StaticWorkflowGraph:
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise StaticWorkflowGraphError("STATIC_GRAPH_PLAN_INVALID")
        nodes: list[dict[str, object]] = []
        for raw in raw_nodes:
            if not isinstance(raw, Mapping):
                raise StaticWorkflowGraphError("STATIC_GRAPH_NODE_INVALID")
            node = dict(raw)
            node_id, node_type = node.get("node_id"), node.get("node_type")
            if (
                not isinstance(node_id, str)
                or not _NODE_ID.fullmatch(node_id)
                or not isinstance(node_type, str)
            ):
                raise StaticWorkflowGraphError("STATIC_GRAPH_NODE_INVALID")
            nodes.append(node)
        identifiers = [str(node["node_id"]) for node in nodes]
        if len(set(identifiers)) != len(identifiers):
            raise StaticWorkflowGraphError("STATIC_GRAPH_NODE_DUPLICATE")
        # MVP 不允许 Package 声明 fork/join 或条件边。缺省 next_nodes 时按冻结
        # Plan 的顺序连接，兼容早期已持久化的线性 Plan。
        for index, node in enumerate(nodes):
            expected = [] if index + 1 == len(nodes) else [identifiers[index + 1]]
            declared = node.get("next_nodes", expected)
            # 早期 Plan 经 Pydantic 默认值持久化为 ``next_nodes=[]``，没有
            # 分支语义；仅对此历史线性形态补全相邻边，任何非空偏差都拒绝。
            if declared == [] and expected:
                declared = expected
            if declared != expected:
                raise StaticWorkflowGraphError("STATIC_GRAPH_EDGE_INVALID")
        return cls(nodes)

    def ordered_nodes(self) -> list[dict[str, object]]:
        """编译并运行无副作用图，返回经 LangGraph 验证的固定节点顺序。"""
        builder = StateGraph(_GraphState)
        identifiers = [str(node["node_id"]) for node in self._nodes]
        for node_id in identifiers:
            # 只写 node id；这里绝不接收或保留 Run 输入、prompt、模型/工具结果。
            def visit(state: _GraphState, *, frozen_id: str = node_id) -> _GraphState:
                return {"visited_node_ids": [*state.get("visited_node_ids", []), frozen_id]}

            builder.add_node(node_id, visit)
        builder.add_edge(START, identifiers[0])
        for current, following in zip(identifiers, identifiers[1:], strict=False):
            builder.add_edge(current, following)
        builder.add_edge(identifiers[-1], END)
        try:
            result = builder.compile().invoke({"visited_node_ids": []})
        except Exception as exc:  # LangGraph 内部错误不得携带到 API/日志。
            raise StaticWorkflowGraphError("STATIC_GRAPH_COMPILE_FAILED") from exc
        if result.get("visited_node_ids") != identifiers:
            raise StaticWorkflowGraphError("STATIC_GRAPH_ORDER_INVALID")
        return [dict(node) for node in self._nodes]
