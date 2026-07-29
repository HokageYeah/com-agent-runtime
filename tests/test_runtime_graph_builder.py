"""LangGraph 静态编译边界：Plan 只能决定已冻结的线性节点顺序。"""

from __future__ import annotations

import pytest

from app.runtime.graph_builder import StaticWorkflowGraph, StaticWorkflowGraphError


def test_static_graph_compiles_frozen_plan_and_returns_only_declared_node_order() -> None:
    graph = StaticWorkflowGraph.build([
        {"node_id": "load_snapshot", "node_type": "tool", "next_nodes": ["review"]},
        {"node_id": "review", "node_type": "guardrail", "next_nodes": []},
    ])

    assert [node["node_id"] for node in graph.ordered_nodes()] == [
        "load_snapshot", "review",
    ]


def test_static_graph_rejects_branching_or_dynamic_edges() -> None:
    with pytest.raises(StaticWorkflowGraphError, match="STATIC_GRAPH_EDGE_INVALID"):
        StaticWorkflowGraph.build([
            {"node_id": "first", "node_type": "tool", "next_nodes": ["second", "third"]},
            {"node_id": "second", "node_type": "tool", "next_nodes": []},
            {"node_id": "third", "node_type": "tool", "next_nodes": []},
        ])
