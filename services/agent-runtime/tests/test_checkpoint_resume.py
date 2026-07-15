"""Task 10：checkpoint 加密、fencing 与恢复读取回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.base import Base
from app.models import AgentCheckpoint, AgentRun, RuntimeAuditRecord
from app.runtime.checkpoint import CheckpointStore, FernetCheckpointCipher
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
    assert [record.action for record in session.scalars(select(RuntimeAuditRecord)).all()] == [
        "checkpoint_saved",
        "checkpoint_loaded",
    ]
