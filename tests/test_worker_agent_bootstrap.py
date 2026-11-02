"""R2 装配边界：公共 Worker 不加载具体 Agent，注册工厂归各 Agent 所有。"""

import ast
from pathlib import Path

import pytest

from app.agents.registry import get_executor_factory


@pytest.mark.parametrize("agent_id", ["memoir_agent", "qa_agent", "soothe_agent"])
def test_registered_factory_belongs_to_agent_bootstrap(agent_id: str) -> None:
    factory = get_executor_factory(agent_id)
    assert factory is not None
    assert factory.__module__ == f"app.agents.{agent_id}.bootstrap"


def test_worker_does_not_import_concrete_agents() -> None:
    tree = ast.parse(Path("app/worker.py").read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
            if node.module == "app.agents":
                imports.extend(f"app.agents.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    assert not [
        name for name in imports
        if name.startswith(("app.agents.memoir_agent", "app.agents.qa_agent"))
    ]


def test_worker_source_does_not_name_concrete_agents() -> None:
    # 同时约束字符串转发，避免无具体 import 却仍按 Agent 名称耦合装配。
    source = Path("app/worker.py").read_text()
    assert "memoir_agent" not in source and "qa_agent" not in source
