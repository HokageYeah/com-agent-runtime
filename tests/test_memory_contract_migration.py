"""Task 6.5 权威状态与冻结元数据迁移回归。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError


def test_memory_contract_migration_normalizes_disabled_and_enforces_statuses() -> None:
    """旧 not_started 必须迁为 disabled，未知状态不能继续写入。"""
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "memory_archives",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("content_status", sa.String(32), nullable=False),
        sa.Column("enhancement_status", sa.String(32), nullable=False),
    )
    sa.Table(
        "memory_agent_run_refs",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(80), nullable=False, unique=True),
        sa.Column("archive_id", sa.String(64), nullable=False),
        sa.Column("generation_epoch", sa.Integer(), nullable=False),
    )
    metadata.create_all(engine)

    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260729_1000_close_memory_contract.py"
    )
    spec = importlib.util.spec_from_file_location("memory_contract_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO memory_archives "
                "(id, content_status, enhancement_status) "
                "VALUES (1, 'baseline', 'not_started')"
            )
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migrated = connection.execute(
            sa.text(
                "SELECT content_status, enhancement_status "
                "FROM memory_archives WHERE id = 1"
            )
        ).one()
        assert migrated == ("baseline", "disabled")
        columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("memory_archives")
        }
        assert {
            "partner_nickname_snapshot",
            "partner_avatar_snapshot",
            "bound_at",
            "unbound_at",
        } <= columns
        connection.execute(
            sa.text(
                "INSERT INTO memory_agent_run_refs "
                "(id, run_id, archive_id, generation_epoch) "
                "VALUES (1, 'run-a', 'archive-a', 2)"
            )
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO memory_archives "
                "(id, content_status, enhancement_status) "
                "VALUES (2, 'baseline', 'unknown')"
            )
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO memory_agent_run_refs "
                "(id, run_id, archive_id, generation_epoch) "
                "VALUES (2, 'run-b', 'archive-a', 2)"
            )
        )
