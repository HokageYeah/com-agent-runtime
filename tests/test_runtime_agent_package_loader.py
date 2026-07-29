from __future__ import annotations

from pathlib import Path
from shutil import copytree

import pytest

from app.services.agent_package_service import (
    AgentPackageService,
    AgentPackageValidationError,
)


def test_loads_frozen_memoir_agent_package() -> None:
    package_root = Path(__file__).parents[1] / "app" / "agents"

    package = AgentPackageService(package_root).load("memoir_agent", "1.0.0")

    assert package.agent_id == "memoir_agent"
    assert package.version == "1.0.0"
    assert package.contract_version == "1.0.0"
    assert package.package_digest.startswith("sha256:")
    assert len(package.evals) >= 5
    assert package.tools[0].relative_path.startswith("/")
    enqueue_tts = next(
        tool for tool in package.tools if tool.name == "memory.enqueue_tts"
    )
    assert enqueue_tts.enabled is False
    assert enqueue_tts.side_effect is True
    assert enqueue_tts.cancellation_behavior == "query_after_commit"


def test_rejects_implicit_latest_version() -> None:
    package_root = Path(__file__).parents[1] / "app" / "agents"
    service = AgentPackageService(package_root)

    with pytest.raises(AgentPackageValidationError, match="必须精确指定"):
        service.load("memoir_agent", "latest")


def test_rejects_digest_change_for_same_registered_version(tmp_path: Path) -> None:
    source_root = Path(__file__).parents[1] / "app" / "agents"
    package_root = tmp_path / "agents"
    copytree(source_root, package_root)
    service = AgentPackageService(package_root)

    first = service.load("memoir_agent", "1.0.0")
    prompt = package_root / "memoir_agent" / "1.0.0" / "prompts" / "chapter-plan.v1.md"
    prompt.write_text("版本内容变化", encoding="utf-8")

    with pytest.raises(AgentPackageValidationError, match="digest 不可变"):
        service.load("memoir_agent", "1.0.0")
    assert first.package_digest.startswith("sha256:")


def test_rejects_missing_required_package_file(tmp_path: Path) -> None:
    source_root = Path(__file__).parents[1] / "app" / "agents"
    package_root = tmp_path / "agents"
    copytree(source_root, package_root)
    (package_root / "memoir_agent" / "1.0.0" / "callbacks.yaml").unlink()

    with pytest.raises(AgentPackageValidationError, match="缺少文件"):
        AgentPackageService(package_root).load("memoir_agent", "1.0.0")


def test_package_lifecycle_blocks_revoked_and_new_deprecated_runs() -> None:
    with pytest.raises(AgentPackageValidationError, match="revoked"):
        AgentPackageService.validate_lifecycle("revoked", operation="start")
    with pytest.raises(AgentPackageValidationError, match="deprecated"):
        AgentPackageService.validate_lifecycle("deprecated", operation="create")
