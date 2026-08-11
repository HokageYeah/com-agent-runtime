"""Task 10：checkpoint 加密、fencing 与恢复读取回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import AgentCheckpoint, AgentRun, RuntimeAuditRecord
from app.runtime.checkpoint import (
    CheckpointError,
    CheckpointStore,
    FernetCheckpointCipher,
)
from app.runtime.interfaces import LeaseContext


def test_checkpoint_store_encrypts_state_and_loads_latest_for_valid_lease() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id="checkpoint-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="running", dispatch_state="claimed", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            execution_attempt=1, lease_owner="worker-a", fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
        )
    )
    session.commit()
    context = LeaseContext(
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
        authorization_version=1,
    )
    store = CheckpointStore(session, FernetCheckpointCipher.generate())

    checkpoint_id = store.save(
        "checkpoint-run",
        "load_snapshot",
        {"completed_node_ids": ["load_snapshot"], "private_snapshot": "never-public"},
        context,
    )
    session.commit()

    record = session.scalar(
        select(AgentCheckpoint).where(AgentCheckpoint.checkpoint_id == checkpoint_id)
    )
    assert record is not None
    assert record.encrypted_state_blob is not None
    assert b"never-public" not in record.encrypted_state_blob
    assert store.load_latest("checkpoint-run", context) == {
        "completed_node_ids": ["load_snapshot"],
        "private_snapshot": "never-public",
    }
    audit_records = list(session.scalars(select(RuntimeAuditRecord)).all())
    assert [record.action for record in audit_records] == [
        "checkpoint_saved",
        "checkpoint_loaded",
    ]
    # 受控解密读取仅记录资源版本和摘要前缀，严禁将恢复状态进入审计记录。
    assert audit_records[-1].metadata_summary == {
        "run_id": "checkpoint-run",
        "privacy_version": "1",
        "content_digest_prefix": record.content_digest[:12],
    }
    assert "never-public" not in str(audit_records[-1].metadata_summary)


def _seed_run_for_purge(session, run_id: str) -> datetime:
    """构造一个 active run + 2 个 checkpoint 的最小夹具，供 purge 测试使用。"""
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id=run_id, agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="running", dispatch_state="claimed", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            execution_attempt=1, lease_owner="worker-a", fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
        )
    )
    session.commit()
    return now


def test_purge_for_run_deletes_all_checkpoints_and_audits_fact_only() -> None:
    """R2 purge 能力：删除 Run 全部 checkpoint，审计只记事实，不写正文/密文。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _seed_run_for_purge(session, "purge-run")
    context = LeaseContext(
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
        authorization_version=1,
    )
    store = CheckpointStore(session, FernetCheckpointCipher.generate())
    store.save(
        "purge-run", "attempt:1:step:1",
        {"completed_node_ids": ["load_snapshot"], "private_body": "purge-marker"},
        context,
    )
    store.save(
        "purge-run", "attempt:1:step:2",
        {"completed_node_ids": ["load_snapshot", "compute_stats"], "private_body": "purge-marker"},
        context,
    )
    session.commit()
    assert len(list(session.scalars(select(AgentCheckpoint).where(AgentCheckpoint.run_id == "purge-run")))) == 2

    purged = store.purge_for_run("purge-run", context)
    session.commit()

    assert purged == 2
    assert list(session.scalars(select(AgentCheckpoint).where(AgentCheckpoint.run_id == "purge-run"))) == []
    audit_records = list(session.scalars(select(RuntimeAuditRecord)).all())
    purge_audits = [record for record in audit_records if record.action == "checkpoint_purged"]
    assert len(purge_audits) == 2
    # 审计只记事实（run_id/版本/摘要前缀），不写私载正文或密文。
    assert "purge-marker" not in str([record.metadata_summary for record in purge_audits])


def test_purge_for_run_returns_zero_when_no_checkpoint_exists() -> None:
    """purge 是幂等能力；无可清理 checkpoint 时返回 0，不抛错。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _seed_run_for_purge(session, "purge-empty-run")
    context = LeaseContext(
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
        authorization_version=1,
    )
    store = CheckpointStore(session, FernetCheckpointCipher.generate())

    assert store.purge_for_run("purge-empty-run", context) == 0
    # 无 checkpoint 时不写 purge 审计行。
    audit_records = list(session.scalars(select(RuntimeAuditRecord)).all())
    assert all(record.action != "checkpoint_purged" for record in audit_records)


def test_purge_for_run_refuses_invalid_lease() -> None:
    """purge 同样受 fencing/privacy/authorization 保护，无效 lease 拒绝。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _seed_run_for_purge(session, "purge-fenced-run")
    store = CheckpointStore(session, FernetCheckpointCipher.generate())
    invalid_context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
        authorization_version=1,
    )
    with pytest.raises(CheckpointError):
        store.purge_for_run("purge-fenced-run", invalid_context)
