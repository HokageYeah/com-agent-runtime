from __future__ import annotations

import sqlalchemy as sa

from app.db import alembic_schema_bootstrap


class _FakeOperations:
    def __init__(self, connection: sa.Connection) -> None:
        self._connection = connection

    def get_bind(self) -> sa.Connection:
        return self._connection


def test_head_schema_markers_are_connection_local(monkeypatch) -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        monkeypatch.setattr(
            alembic_schema_bootstrap,
            "op",
            _FakeOperations(connection),
        )

        alembic_schema_bootstrap.mark_head_schema(memory=True, runtime=True)

        assert alembic_schema_bootstrap.memory_schema_created_at_head() is True
        assert alembic_schema_bootstrap.runtime_schema_created_at_head() is True


def test_incomplete_schema_is_not_treated_as_current_head(monkeypatch) -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table("memory_archives", metadata, sa.Column("id", sa.Integer))
    sa.Table("agent_runs", metadata, sa.Column("id", sa.Integer))
    metadata.create_all(engine)

    with engine.connect() as connection:
        monkeypatch.setattr(
            alembic_schema_bootstrap,
            "op",
            _FakeOperations(connection),
        )

        assert alembic_schema_bootstrap.memory_schema_created_at_head() is False
        assert alembic_schema_bootstrap.runtime_schema_created_at_head() is False
