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


def test_project_structure_script_matches_current_template_direction() -> None:
    """项目结构脚手架不应再回退到旧的 wx / articles 模板语义。"""

    script_path = PROJECT_ROOT / "project_structure.sh"
    content = script_path.read_text(encoding="utf-8")

    assert "wx_public" not in content
    assert "微信公众号爬虫" not in content
    assert "articles" not in content
    assert "public_account" not in content

    assert "Couple Diary Backend" in content
    assert "demo_api.py" in content
    assert "diary_api.py" in content
    assert "diary_entries" in content


def test_agent_runtime_plans_forbid_nested_project() -> None:
    """开发计划必须固定 Runtime 在当前根工程内，避免再次生成子工程。"""
    for plan_path in (MASTER_RUNTIME_PLAN, BACKEND_RUNTIME_PLAN):
        content = plan_path.read_text(encoding="utf-8")
        assert "services/agent-runtime/" not in content
        assert "当前 com-agent-runtime 根工程" in content
