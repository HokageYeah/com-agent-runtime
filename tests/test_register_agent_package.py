"""共享注册入口只给受信执行身份启用 active，保留原加载与 upsert 行为。"""

from __future__ import annotations

from pathlib import Path
from shutil import copytree

import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import AgentDefinition
from app.scripts import register_agent_package as registration
from app.services.agent_package_service import AgentPackageValidationError


@pytest.fixture
def deployed_package(tmp_path: Path, monkeypatch):
    """只复制冻结包到临时目录调整身份/状态；不改仓库包与真实数据库。"""
    package_root = tmp_path / "agents"
    monkeypatch.setattr(registration, "PACKAGE_ROOT", package_root)

    def deploy(agent_id: str, status: str) -> None:
        target = package_root / agent_id / "1.0.0"
        copytree(Path(__file__).parents[1] / "app/agents/memoir_agent/1.0.0", target)
        manifest = target / "agent.yaml"
        definition = yaml.safe_load(manifest.read_text())
        definition.update(agent_id=agent_id, status=status)
        manifest.write_text(yaml.safe_dump(definition), encoding="utf-8")

    return deploy


@pytest.mark.parametrize("agent_id", ["agent"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_unregistered_active_package_is_rejected_before_database_access(
    deployed_package, monkeypatch, caplog, agent_id: str, dry_run: bool,
) -> None:
    deployed_package(agent_id, "active")

    def database_must_not_be_accessed() -> str:
        pytest.fail("未登记的 active 包必须在访问数据库之前拒绝")

    monkeypatch.setattr(registration, "get_database_url", database_must_not_be_accessed)

    assert registration.register(agent_id, "1.0.0", dry_run=dry_run) == 1
    assert "AGENT_EXECUTOR_NOT_REGISTERED" in caplog.text


@pytest.mark.parametrize(
    ("agent_id", "status"),
    [("memoir_agent", "active"), ("memoir_agent", "deprecated"), ("agent", "deprecated")],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_allowed_registration_keeps_dry_run_and_idempotent_upsert(
    deployed_package, tmp_path: Path, monkeypatch,
    agent_id: str, status: str, dry_run: bool,
) -> None:
    deployed_package(agent_id, status)
    url = f"sqlite:///{tmp_path / 'runtime-registration.db'}"
    engine = create_engine(url)
    AgentDefinition.__table__.create(engine)
    monkeypatch.setattr(registration, "get_database_url", lambda: url)
    try:
        assert registration.register(agent_id, "1.0.0", dry_run=dry_run) == 0
        assert registration.register(agent_id, "1.0.0", dry_run=dry_run) == 0
        with Session(engine) as session:
            definitions = list(session.scalars(select(AgentDefinition)))
            if dry_run:
                assert definitions == []
            else:
                assert len(definitions) == 1
                definition = definitions[0]
                assert (definition.agent_id, definition.version, definition.status) == (
                    agent_id, "1.0.0", status,
                )
                package = registration.AgentPackageService(registration.PACKAGE_ROOT).load(
                    agent_id, "1.0.0"
                )
                assert definition.package_digest == package.package_digest
                assert definition.definition_json["workflow_nodes"] == [
                    node.model_dump(mode="json") for node in package.workflow_nodes
                ]
    finally:
        engine.dispose()


@pytest.mark.parametrize("agent_id", ["memoir_agent", "agent"])
def test_revoked_package_keeps_existing_loader_rejection(
    deployed_package, agent_id: str,
) -> None:
    # revoked 已在 load 阶段拒绝；active 新门禁不得放松这条既有生命周期校验。
    deployed_package(agent_id, "revoked")

    with pytest.raises(AgentPackageValidationError, match="已 revoked"):
        registration.register(agent_id, "1.0.0", dry_run=False)


@pytest.mark.parametrize("dry_run", [False, True])
def test_real_qa_package_can_register_active(tmp_path: Path, monkeypatch, dry_run: bool) -> None:
    """T4 新身份使用真实 QA 包通过共享 active 门禁；重复注册仍然幂等。"""
    url = f"sqlite:///{tmp_path / 'qa-registration.db'}"
    engine = create_engine(url)
    AgentDefinition.__table__.create(engine)
    monkeypatch.setattr(registration, "get_database_url", lambda: url)
    try:
        assert registration.register("qa_agent", "1.0.0", dry_run=dry_run) == 0
        assert registration.register("qa_agent", "1.0.0", dry_run=dry_run) == 0
        with Session(engine) as session:
            definitions = list(session.scalars(select(AgentDefinition)))
            if dry_run:
                assert definitions == []
            else:
                assert len(definitions) == 1
                definition = definitions[0]
                assert (definition.agent_id, definition.version, definition.status) == (
                    "qa_agent", "1.0.0", "active",
                )
                assert definition.definition_json["allowed_business_types"] == ["business_qa"]
    finally:
        engine.dispose()
