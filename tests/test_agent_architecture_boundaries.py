"""公共层只依赖中立协议与注册表；静态扫描也覆盖函数内的延迟导入。"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _agent_modules(agents_root: Path) -> frozenset[str]:
    # Agent 使用命名空间包，不要求 __init__.py；新增目录自动进入护栏。
    return frozenset(
        f"app.agents.{path.name}"
        for path in agents_root.iterdir()
        if path.is_dir() and path.name.isidentifier() and path.name != "__pycache__"
    )


def _public_python_files(app_root: Path) -> list[Path]:
    paths = [app_root / "worker.py"]
    for directory in ("runtime", "schemas", "api", "core", "services"):
        paths.extend(
            path for path in (app_root / directory).rglob("*.py")
            if not path.is_relative_to(app_root / "services" / "memoir")
        )
    return sorted(paths)


def _agent_imports(
    tree: ast.AST, package: str, agent_modules: frozenset[str],
) -> list[tuple[int, str]]:
    violations = []
    # ast.walk 遍历全部层级，不能因 import 位于函数/条件/类型检查块而漏报。
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            # 同时处理 from app.agents.x import y 与 from app.agents import x。
            modules = [module] + [f"{module}.{alias.name}" for alias in node.names]
        else:
            continue
        for module in modules:
            if any(
                module == agent or module.startswith(f"{agent}.")
                for agent in agent_modules
            ):
                violations.append((node.lineno, module))
    return violations


def test_public_layers_do_not_import_concrete_agents() -> None:
    agent_modules = _agent_modules(ROOT / "app" / "agents")
    assert agent_modules, "未发现 Agent 子包，拒绝空扫描"
    violations = []
    for path in _public_python_files(ROOT / "app"):
        relative = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        package = ".".join(relative.parent.parts)
        violations.extend(
            f"{relative}:{line}: {module}"
            for line, module in _agent_imports(tree, package, agent_modules)
        )
    assert not violations, "公共层直接依赖具体 Agent：\n" + "\n".join(violations)


@pytest.mark.parametrize("source", [
    "import app.agents.future_agent",
    "import app.agents.future_agent.runner as runner",
    "from app.agents.future_agent import runner",
    "from app.agents.future_agent.runner import Runner as Alias",
    "from app.agents import future_agent as alias",
    "def load():\n    import app.agents.future_agent",
    "def load():\n    from app.agents.future_agent import runner",
    "def load():\n    from app.agents import future_agent",
    "if TYPE_CHECKING:\n    from app.agents.future_agent import Runner",
    "from ..agents.future_agent import runner",
    "from ..agents import future_agent",
])
def test_ast_scan_detects_direct_and_lazy_imports(source: str) -> None:
    assert _agent_imports(ast.parse(source), "app.runtime", frozenset({"app.agents.future_agent"}))


@pytest.mark.parametrize("source", [
    "from app.agents.registry import REGISTRATIONS",
    "from app.agents import registry, output_targets",
    "import app.agents.future_agent_extra",
    "# import app.agents.future_agent\ntext = 'from app.agents import future_agent'",
])
def test_ast_scan_allows_registry_and_ignores_text(source: str) -> None:
    assert not _agent_imports(ast.parse(source), "app.runtime", frozenset({"app.agents.future_agent"}))


def test_agent_discovery_includes_namespace_packages(tmp_path: Path) -> None:
    (tmp_path / "future_agent").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "registry.py").touch()
    assert _agent_modules(tmp_path) == frozenset({"app.agents.future_agent"})


def test_scan_scope_only_excludes_memoir_services_and_agent_directories(tmp_path: Path) -> None:
    included = [
        "worker.py", "runtime/nested/a.py", "schemas/a.py", "api/endpoints/a.py",
        "core/a.py", "services/a.py", "services/other/a.py",
    ]
    excluded = [
        "services/memoir/a.py", "services/memoir/nested/a.py",
        "agents/registry.py", "agents/future_agent/runner.py",
    ]
    for name in included + excluded:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert set(_public_python_files(tmp_path)) == {tmp_path / name for name in included}
