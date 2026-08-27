from __future__ import annotations

import re
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
    # 第三方 Action 必须固定到不可变提交，不能退回可漂移的 @v5/@v6 标签。
    assert re.search(
        r"^\s*uses: actions/setup-python@[0-9a-f]{40}\s*$", content, re.MULTILINE
    )
    assert 'python-version: "3.13"' in content
    assert "poetry install --no-interaction" in content
    assert "poetry run ruff check app tests" in content
    # 进程 harness 在干净解释器中单独运行，其余测试显式排除该文件，覆盖不重复也不遗漏。
    assert "poetry run pytest tests/test_runtime_process_harness.py" in content
    assert "poetry run pytest --ignore=tests/test_runtime_process_harness.py" in content

    # 这是给“独立仓库”使用的 workflow，因此不应再依赖 monorepo 的子目录前缀。
    assert "backend/couple-diary-b/**" not in content
    assert "working-directory: backend/couple-diary-b" not in content
