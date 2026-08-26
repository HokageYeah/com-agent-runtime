from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MASTER_RUNTIME_PLAN = (
    PROJECT_ROOT
    / "头脑风暴/docs/AgentRuntime/plans/2026-07-07-AgentRuntime-总控开发计划.md"
)
BACKEND_RUNTIME_PLAN = (
    PROJECT_ROOT
    / "头脑风暴/docs/AgentRuntime/backend/2026-07-07-AgentRuntime-后端开发计划.md"
)


def test_project_structure_script_matches_current_runtime_direction() -> None:
    """项目结构脚本应描述公共 Runtime，而不是历史业务模板。"""

    script_path = PROJECT_ROOT / "project_structure.sh"
    content = script_path.read_text(encoding="utf-8")

    assert "wx_public" not in content
    assert "微信公众号爬虫" not in content
    assert "articles" not in content
    assert "public_account" not in content

    assert "com-agent-runtime（公共 Agent 执行服务）" in content
    assert "contracts/" in content
    assert "runtime/" in content
    assert "dispatcher.py" in content
    assert "worker.py" in content
    assert "reconciler.py" in content
    assert "demo/diary 模块不是新增 Runtime 能力的推荐样板" in content
    assert "Couple Diary Backend" not in content


def test_agent_runtime_plans_forbid_nested_project() -> None:
    """开发计划必须固定 Runtime 在当前根工程内，避免再次生成子工程。"""
    for plan_path in (MASTER_RUNTIME_PLAN, BACKEND_RUNTIME_PLAN):
        content = plan_path.read_text(encoding="utf-8")
        assert "services/agent-runtime/" not in content
        assert "当前 com-agent-runtime 根工程" in content


def test_memoir_business_modules_are_grouped_under_named_subpackages() -> None:
    """回忆录迁移代码应与公共 Runtime 模块物理分开。"""

    expected_packages = (
        PROJECT_ROOT / "app/api/endpoints/memoir",
        PROJECT_ROOT / "app/services/memoir",
        PROJECT_ROOT / "app/models/memoir",
    )
    assert all(path.is_dir() for path in expected_packages)

    assert not list((PROJECT_ROOT / "app/api/endpoints").glob("memory*_api.py"))
    assert not list((PROJECT_ROOT / "app/services").glob("memory_*.py"))
    assert not list((PROJECT_ROOT / "app/services").glob("memoir_*.py"))
    assert not (PROJECT_ROOT / "app/services/relationship_archive_service.py").exists()
    assert not list((PROJECT_ROOT / "app/models").glob("memory_*.py"))
    assert not any(
        (PROJECT_ROOT / "app/models" / filename).exists()
        for filename in ("bet.py", "couple_relationship.py", "diary_entry.py")
    )
    assert (PROJECT_ROOT / "app/agents/memoir_agent/model_gateway.py").is_file()
    assert not (PROJECT_ROOT / "app/runtime/memoir_model_gateway.py").exists()
