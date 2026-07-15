from __future__ import annotations

from app.db.metadata import get_target_metadata


def test_root_metadata_contains_runtime_tables() -> None:
    """Runtime 表必须由根工程唯一的 SQLAlchemy metadata 管理。"""
    table_names = set(get_target_metadata().tables)

    assert {"agent_runs", "agent_steps", "runtime_outbox_events"} <= table_names
