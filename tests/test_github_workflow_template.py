from __future__ import annotations

from pathlib import Path


def test_standalone_github_workflow_template_exists() -> None:
    """后端模板工程单独发布到 GitHub 时，也应自带可用的 CI workflow。"""

    workflow_path = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "com-agent-runtime.yml"
    )

    assert workflow_path.exists()

    content = workflow_path.read_text(encoding="utf-8")
    assert "name: com-agent-runtime" in content
    assert "actions/setup-python@v5" in content
    assert 'python-version: "3.13"' in content
    assert "poetry install --no-interaction" in content
    assert "poetry run ruff check app tests" in content
    assert "poetry run pytest" in content

    # 这是给“独立仓库”使用的 workflow，因此不应再依赖 monorepo 的子目录前缀。
    assert "backend/couple-diary-b/**" not in content
    assert "working-directory: backend/couple-diary-b" not in content
