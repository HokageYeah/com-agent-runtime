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
    isolated_process_test = (
        "tests/test_runtime_process_harness.py::"
        "test_process_harness_starts_api_worker_and_reconciler_with_safe_readiness"
    )
    # 该用例会启动真实 API / Worker / Reconciler 子进程，必须在全新
    # pytest 进程中运行；后续全量测试精确排除它，避免重复执行。
    assert re.search(
        rf"poetry run pytest\s+{re.escape(isolated_process_test)}", content
    )
    assert re.search(
        rf"poetry run pytest\s+--deselect={re.escape(isolated_process_test)}", content
    )

    # 这是给“独立仓库”使用的 workflow，因此不应再依赖 monorepo 的子目录前缀。
    assert "backend/couple-diary-b/**" not in content
    assert "working-directory: backend/couple-diary-b" not in content
