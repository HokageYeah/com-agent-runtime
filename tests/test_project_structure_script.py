from __future__ import annotations

from pathlib import Path


def test_project_structure_script_matches_current_template_direction() -> None:
    """项目结构脚手架不应再回退到旧的 wx / articles 模板语义。"""

    script_path = Path(__file__).resolve().parents[1] / "project_structure.sh"
    content = script_path.read_text(encoding="utf-8")

    assert "wx_public" not in content
    assert "微信公众号爬虫" not in content
    assert "articles" not in content
    assert "public_account" not in content

    assert "Couple Diary Backend" in content
    assert "demo_api.py" in content
    assert "diary_api.py" in content
    assert "diary_entries" in content
