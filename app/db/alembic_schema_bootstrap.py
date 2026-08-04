"""从无 Runtime 表的旧库启动时，标记 create_all 已一次创建当前 schema。

该标记只存在于同一次 Alembic 连接，不写入数据库。旧库已存在
memory/runtime 表时不设置，后续历史迁移仍会正常执行。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

_MEMORY_SCHEMA_AT_HEAD = "agent_runtime_memory_schema_created_at_head"
_RUNTIME_SCHEMA_AT_HEAD = "agent_runtime_core_schema_created_at_head"


def mark_head_schema(*, memory: bool, runtime: bool) -> None:
    bind_info = op.get_bind().info
    bind_info[_MEMORY_SCHEMA_AT_HEAD] = memory
    bind_info[_RUNTIME_SCHEMA_AT_HEAD] = runtime


def memory_schema_created_at_head(bind: Connection | None = None) -> bool:
    bind = bind or op.get_bind()
    if bool(bind.info.get(_MEMORY_SCHEMA_AT_HEAD, False)):
        return True
    inspector = sa.inspect(bind)
    required_tables = (
        "memory_archives",
        "memory_agent_run_refs",
        "memory_source_references",
        "memory_runtime_compensation_events",
        "memory_passwords",
    )
    if not all(inspector.has_table(table_name) for table_name in required_tables):
        return False
    archive_columns = {
        column["name"] for column in inspector.get_columns("memory_archives")
    }
    run_ref_columns = {
        column["name"]
        for column in inspector.get_columns("memory_agent_run_refs")
    }
    return {
        "partner_nickname_snapshot",
        "partner_avatar_snapshot",
        "bound_at",
        "unbound_at",
    }.issubset(archive_columns) and {
        "reconciliation_status",
        "public_trace_json",
        "row_version",
        "snapshot_id",
    }.issubset(run_ref_columns)


def runtime_schema_created_at_head(bind: Connection | None = None) -> bool:
    bind = bind or op.get_bind()
    if bool(bind.info.get(_RUNTIME_SCHEMA_AT_HEAD, False)):
        return True
    inspector = sa.inspect(bind)
    required_tables = (
        "agent_model_usages",
        "agent_evaluations",
        "agent_tool_calls",
        "runtime_traffic_events",
        "runtime_reconciliation_leases",
    )
    if not all(inspector.has_table(table_name) for table_name in required_tables):
        return False
    model_usage_columns = {
        column["name"]
        for column in inspector.get_columns("agent_model_usages")
    }
    evaluation_columns = {
        column["name"]
        for column in inspector.get_columns("agent_evaluations")
    }
    tool_call_columns = {
        column["name"] for column in inspector.get_columns("agent_tool_calls")
    }
    return {
        "reserved_tokens",
        "provider_request_id",
        "thinking_summary_json",
    }.issubset(model_usage_columns) and {
        "schema_passed",
        "grounding_passed",
        "emotional_safety_passed",
        "material_reference_passed",
        "hallucination_detected",
    }.issubset(evaluation_columns) and "retention_until" in tool_call_columns
