"""迁移谱系必须能解析已部署数据库中的历史 revision。"""

from __future__ import annotations

from pathlib import Path


def test_memory_freeze_source_migration_precedes_runtime_launch_outbox() -> None:
    versions = Path(__file__).parents[1] / "alembic" / "versions"
    source_migration = versions / "20260720_1000_add_memory_freeze_source_fields.py"
    launch_migration = versions / "20260720_1100_add_memory_runtime_launch_outbox.py"

    assert 'revision = "20260720_1000"' in source_migration.read_text(encoding="utf-8")
    assert 'down_revision = "20260720_1000"' in launch_migration.read_text(encoding="utf-8")


def test_runtime_traffic_event_migration_extends_the_single_runtime_head() -> None:
    versions = Path(__file__).parents[1] / "alembic" / "versions"
    migration = versions / "20260728_0900_add_runtime_traffic_events.py"

    text = migration.read_text(encoding="utf-8")
    assert 'revision = "20260728_0900"' in text
    assert 'down_revision = "20260727_0900"' in text
    assert '"runtime_traffic_events"' in text


def test_memory_contract_closure_extends_tool_retention_revision() -> None:
    versions = Path(__file__).parents[1] / "alembic" / "versions"
    migration = versions / "20260729_1000_close_memory_contract.py"

    text = migration.read_text(encoding="utf-8")
    assert 'revision = "20260729_1000"' in text
    assert 'down_revision = "20260729_0900"' in text
    assert "partner_nickname_snapshot" in text
