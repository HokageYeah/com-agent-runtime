from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    AgentArtifact,
    AgentCheckpoint,
    AgentModelUsage,
    AgentRun,
    AgentStep,
    AgentToolCall,
    MemoryArchive,
    MemoryMediaAsset,
    MemorySnapshot,
    RuntimeAuditRecord,
    RuntimeOutboxEvent,
    RuntimeTrafficEvent,
)
from app.reconciler import ReconcilerRunner
from app.runtime.postgres_harness import PostgresHarnessConfig, PostgresSchemaHarness
from app.runtime.process_harness import ProcessHarness
from app.services.callback_service import CallbackDeliveryService
from app.services.lease_service import LeaseService
from app.services.memoir.memory_agent_binding_service import MemoryAgentBindingService
from app.services.memoir.memory_archive_service import (
    FernetSnapshotCipher,
    FrozenMemoryInput,
    MemoryArchiveService,
)
from app.services.memoir.memory_player_service import MemoryPlayerService
from app.services.memoir.memory_snapshot_service import MemorySnapshotService
from app.services.outbox_service import OutboxService
from app.services.traffic_event_service import SqlAlchemyTrafficEventRecorder


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://runtime:secret@db.example.com/production",
        "mysql+pymysql://test_runtime:secret@127.0.0.1/test_runtime",
        "postgresql+psycopg://runtime:secret@127.0.0.1/test_runtime",
        "postgresql+psycopg://test_runtime:secret@db.example.com/test_runtime",
    ],
)
def test_postgres_harness_rejects_non_test_or_non_loopback_dsn(url: str) -> None:
    with pytest.raises(ValueError, match="TEST_POSTGRES_DSN_REJECTED"):
        PostgresHarnessConfig(url, "agent_runtime_test_123", 5)


@pytest.mark.parametrize("schema", ["public", "agent-runtime", "agent_runtime_prod"])
def test_postgres_harness_rejects_unsafe_schema(schema: str) -> None:
    with pytest.raises(ValueError, match="TEST_POSTGRES_SCHEMA_REJECTED"):
        PostgresHarnessConfig(
            "postgresql+psycopg://test_runtime:secret@127.0.0.1/test_runtime",
            schema,
            5,
        )


def test_postgres_harness_accepts_only_explicit_loopback_test_target() -> None:
    config = PostgresHarnessConfig(
        "postgresql+psycopg://test_runtime:secret@127.0.0.1/test_runtime",
        "agent_runtime_test_123",
        5,
    )

    assert config.database_name == "test_runtime"
    assert config.schema == "agent_runtime_test_123"


def test_postgres_schema_harness_creates_and_finally_drops_schema() -> None:
    """仅在操作者显式提供受限测试 DSN 时运行，CI 不得猜测或复用本地数据库。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    if not url:
        pytest.skip("未显式提供 AGENT_RUNTIME_TEST_POSTGRES_DSN")
    config = PostgresHarnessConfig(url, f"agent_runtime_test_{uuid4().hex[:12]}", 10)
    observer = create_engine(url)
    try:
        with PostgresSchemaHarness(config) as harness:
            assert harness.session_factory is not None
            with harness.session_factory() as session:
                assert session.execute(text("SELECT 1")).scalar() == 1
        with observer.connect() as connection:
            exists = connection.execute(
                text(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema"
                ),
                {"schema": config.schema},
            ).scalar()
        assert exists is None
    finally:
        observer.dispose()


def test_postgres_schema_harness_runs_real_reconciler_lease_once() -> None:
    """SQLite 会在跨 Session 续租时锁库；该用例必须在显式 Docker PostgreSQL 下执行。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    if not url:
        pytest.skip("未显式提供 AGENT_RUNTIME_TEST_POSTGRES_DSN")
    config = PostgresHarnessConfig(url, f"agent_runtime_test_{uuid4().hex[:12]}", 10)

    with PostgresSchemaHarness(config) as harness:
        assert harness.session_factory is not None
        report = ReconcilerRunner(
            harness.session_factory, "test-reconciler", interval_seconds=1
        ).run_once()

        assert report is not None
        assert report.scanned == 0


def test_postgres_traffic_event_recorder_uses_atomic_window_upsert() -> None:
    """与 SQLite 不同的 PostgreSQL 方言也必须保持唯一窗口聚合。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    if not url:
        pytest.skip("未显式提供 AGENT_RUNTIME_TEST_POSTGRES_DSN")
    config = PostgresHarnessConfig(url, f"agent_runtime_test_{uuid4().hex[:12]}", 10)
    with PostgresSchemaHarness(config) as harness:
        assert harness.session_factory is not None
        with harness.session_factory.begin() as session:
            recorder = SqlAlchemyTrafficEventRecorder(session)
            occurred_at = datetime(2026, 7, 28, 9, 30, tzinfo=UTC)
            recorder.record("permit_rejected", "summary", "rpm_exceeded", occurred_at=occurred_at)
            recorder.record("permit_rejected", "summary", "rpm_exceeded", occurred_at=occurred_at)
        with harness.session_factory() as session:
            event = session.scalar(select(RuntimeTrafficEvent))
        assert event is not None and event.count == 2


def test_postgres_memory_contract_freezes_envelope_and_keeps_media_disabled() -> None:
    """真实 PostgreSQL 必须保持冻结元数据、密文快照和媒体禁用写入边界。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    if not url:
        pytest.skip("未显式提供 AGENT_RUNTIME_TEST_POSTGRES_DSN")
    config = PostgresHarnessConfig(url, f"agent_runtime_test_{uuid4().hex[:12]}", 10)
    cipher = FernetSnapshotCipher(Fernet.generate_key())
    bound_at = datetime(2025, 3, 14, tzinfo=UTC)
    unbound_at = datetime(2026, 7, 29, tzinfo=UTC)

    with PostgresSchemaHarness(config) as harness:
        assert harness.session_factory is not None
        with harness.session_factory.begin() as session:
            archive = MemoryArchiveService(session, cipher).create_archives_for_relationship(
                FrozenMemoryInput(
                    relationship_id=91,
                    space_id="space-pg-contract",
                    relationship_segment_no=4,
                    owner_user_ids=(901, 902),
                    partner_names={901: "owner-901", 902: "owner-902"},
                    snapshot_cutoff_at=unbound_at,
                    source_manifest={"diary_ids": ["d-1"], "bet_ids": []},
                    snapshot_payload={"diaries": [{"id": "d-1"}], "bets": []},
                    privacy_filter_version="v1",
                    bound_at=bound_at,
                    partner_avatars={901: "avatar-901", 902: "avatar-902"},
                    stats={"diary_count": 1, "bet_count": 0},
                )
            )[0]
            archive_id = archive.archive_id

        with harness.session_factory() as session:
            archive = session.scalar(
                select(MemoryArchive).where(MemoryArchive.archive_id == archive_id)
            )
            snapshot = session.scalar(
                select(MemorySnapshot).where(MemorySnapshot.archive_id == archive_id)
            )
        assert archive is not None and snapshot is not None
        assert (
            archive.enhancement_status,
            archive.partner_nickname_snapshot,
            archive.partner_avatar_snapshot,
            archive.bound_at,
            archive.unbound_at,
        ) == ("disabled", "owner-902", "avatar-902", bound_at, unbound_at)
        envelope = cipher.decrypt_json(snapshot.encrypted_payload)
        assert set(envelope) == {
            "schema_version",
            "source_range",
            "diary_items",
            "bet_items",
            "stats",
        }
        assert b"d-1" not in snapshot.encrypted_payload


def test_postgres_snapshot_compatibility_and_late_media_are_fenced() -> None:
    """真实 PostgreSQL 复核旧快照内存迁移、未来 major 拒绝与旧媒体隔离。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    if not url:
        pytest.skip("未显式提供 AGENT_RUNTIME_TEST_POSTGRES_DSN")
    config = PostgresHarnessConfig(
        url, f"agent_runtime_test_{uuid4().hex[:12]}", 10
    )
    cipher = FernetSnapshotCipher(Fernet.generate_key())

    with PostgresSchemaHarness(config) as harness:
        assert harness.session_factory is not None
        with harness.session_factory.begin() as session:
            archive = MemoryArchiveService(
                session, cipher
            ).create_archives_for_relationship(
                FrozenMemoryInput(
                    relationship_id=92,
                    space_id="space-pg-compat",
                    relationship_segment_no=1,
                    owner_user_ids=(911, 912),
                    partner_names={911: "用户甲", 912: "用户乙"},
                    snapshot_cutoff_at=datetime(2026, 7, 29, tzinfo=UTC),
                    source_manifest={"diary_ids": ["legacy-pg"], "bet_ids": []},
                    snapshot_payload={"diaries": [], "bets": []},
                    privacy_filter_version="v1",
                )
            )[0]
            snapshot = session.scalar(
                select(MemorySnapshot).where(
                    MemorySnapshot.archive_id == archive.archive_id
                )
            )
            assert snapshot is not None
            snapshot.encrypted_payload = cipher.encrypt_json(
                {"diaries": [{"id": "legacy-pg"}], "bets": []}
            )
            original_ciphertext = snapshot.encrypted_payload
            MemoryAgentBindingService(session).bind(
                archive.archive_id,
                "pg-snapshot-compat",
                0,
                snapshot_id=snapshot.snapshot_id,
            ).status = "pending"
            archive_id, snapshot_id = archive.archive_id, snapshot.snapshot_id

        with harness.session_factory.begin() as session:
            migrated = MemorySnapshotService(session, cipher).read_for_runtime(
                archive_id, snapshot_id, "pg-snapshot-compat", 0
            )
            snapshot = session.scalar(
                select(MemorySnapshot).where(MemorySnapshot.snapshot_id == snapshot_id)
            )
            assert snapshot is not None
            assert migrated["schema_version"] == "1.0.0"
            assert migrated["diary_items"] == [{"id": "legacy-pg"}]
            assert snapshot.encrypted_payload == original_ciphertext
            snapshot.schema_major = 2

        with harness.session_factory() as session:
            with pytest.raises(
                ValueError, match="MEMORY_SNAPSHOT_SCHEMA_UNSUPPORTED"
            ):
                MemorySnapshotService(session, cipher).read_for_runtime(
                    archive_id, snapshot_id, "pg-snapshot-compat", 0
                )

        with harness.session_factory.begin() as session:
            archive = session.scalar(
                select(MemoryArchive).where(MemoryArchive.archive_id == archive_id)
            )
            assert archive is not None
            archive.active_run_id = None
            service = MemoryArchiveService(session, cipher)
            first = service.publish_playback_document(
                archive_id,
                expected_generation_epoch=0,
                document={
                    "schema_version": "1.0.0",
                    "scenes": [],
                    "actions": [],
                    "media_manifest": [],
                },
            )
            second = service.publish_playback_document(
                archive_id,
                expected_generation_epoch=0,
                document={
                    "schema_version": "1.0.0",
                    "scenes": [],
                    "actions": [],
                    "media_manifest": [],
                },
            )
            session.add(
                MemoryMediaAsset(
                    asset_id="pg-late-old-revision",
                    archive_id=archive_id,
                    document_id=first.document_id,
                    media_type="image",
                    source_type="default_asset",
                    status="ready",
                    storage_key="private/pg-old-revision",
                )
            )
            session.flush()
            playback = MemoryPlayerService(session).get_published_playback(archive_id)
            assert playback.document.document_id == second.document_id
            assert playback.media_assets == []


def test_postgres_memory_contract_migrates_legacy_status_and_constraints() -> None:
    """真实 PostgreSQL 必须能从旧状态原地升级并拒绝未知增强状态。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    if not url:
        pytest.skip("未显式提供 AGENT_RUNTIME_TEST_POSTGRES_DSN")
    schema = f"agent_runtime_test_{uuid4().hex[:12]}"
    config = PostgresHarnessConfig(url, schema, 10)
    observer = create_engine(config.database_url)
    engine = create_engine(
        config.database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260729_1000_close_memory_contract.py"
    )
    spec = importlib.util.spec_from_file_location(
        "postgres_memory_contract_migration", migration_path
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    try:
        with observer.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE memory_archives ("
                    "id INTEGER PRIMARY KEY, "
                    "content_status VARCHAR(32) NOT NULL, "
                    "enhancement_status VARCHAR(32) NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE memory_agent_run_refs ("
                    "id INTEGER PRIMARY KEY, "
                    "run_id VARCHAR(80) NOT NULL UNIQUE, "
                    "archive_id VARCHAR(64) NOT NULL, "
                    "generation_epoch INTEGER NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO memory_archives "
                    "(id, content_status, enhancement_status) "
                    "VALUES (1, 'baseline', 'not_started')"
                )
            )
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            assert connection.execute(
                text(
                    "SELECT enhancement_status FROM memory_archives WHERE id = 1"
                )
            ).scalar_one() == "disabled"
            connection.execute(
                text(
                    "INSERT INTO memory_agent_run_refs "
                    "(id, run_id, archive_id, generation_epoch) "
                    "VALUES (1, 'run-a', 'archive-a', 2)"
                )
            )
            with pytest.raises(
                IntegrityError,
                match="ck_memory_archive_enhancement_status",
            ):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "INSERT INTO memory_archives "
                            "(id, content_status, enhancement_status) "
                            "VALUES (2, 'baseline', 'unknown')"
                        )
                    )
            with pytest.raises(
                IntegrityError,
                match="uq_memory_run_ref_archive_generation",
            ):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "INSERT INTO memory_agent_run_refs "
                            "(id, run_id, archive_id, generation_epoch) "
                            "VALUES (2, 'run-b', 'archive-a', 2)"
                        )
                    )
    finally:
        engine.dispose()
        with observer.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        observer.dispose()


def test_postgres_persists_distinct_contentless_callback_rejection_audits() -> None:
    """真实 PostgreSQL 必须持久化固定拒绝码，且不需要读取或输出 callback body。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    if not url:
        pytest.skip("未显式提供 AGENT_RUNTIME_TEST_POSTGRES_DSN")
    config = PostgresHarnessConfig(url, f"agent_runtime_test_{uuid4().hex[:12]}", 10)

    class NoSend:
        def send(self, target_id: str, payload: dict[str, object]) -> None:
            raise AssertionError("rejected callback must not send")

    with PostgresSchemaHarness(config) as harness:
        assert harness.session_factory is not None
        with harness.session_factory() as session:
            for index, reason_code in enumerate(
                (
                    "CALLBACK_TARGET_MISSING",
                    "AUTHORIZATION_REVOKED",
                    "AUTHORIZATION_VERSION_CHANGED",
                )
            ):
                run = AgentRun(
                    run_id=f"pg-callback-rejection-{index}", agent_id="memoir_agent",
                    agent_version="1.0.0", package_digest="sha256:test",
                    contract_version="1.0.0", business_type="couple_memory",
                    business_id=f"archive-{index}", status="running",
                    dispatch_state="claimed", input_json={}, authorization_version=1,
                    caller_id="caller", tenant_id="tenant",
                    create_idempotency_key=f"key-{index}", callback_target_id="callback",
                    business_connector_id="connector", trace_id=f"trace-{index}",
                    run_deadline_at=datetime.now(UTC) + timedelta(days=1),
                )
                session.add(run)
                OutboxService(session).append_callback_event(run, "run_started")
                session.flush()
                outbox = session.scalar(
                    select(RuntimeOutboxEvent).where(
                        RuntimeOutboxEvent.aggregate_id == run.run_id
                    )
                )
                assert outbox is not None
                if reason_code == "CALLBACK_TARGET_MISSING":
                    outbox.payload_json = {
                        **outbox.payload_json,
                        "target_id": "removed-target",
                    }
                    authorize = None
                else:
                    def authorize_target(
                        current: AgentRun, code: str = reason_code
                    ) -> str:
                        del current
                        return code

                    authorize = authorize_target
                with pytest.raises(ValueError, match="CALLBACK_TARGET_REVOKED"):
                    CallbackDeliveryService(
                        session, NoSend(), authorize_target=authorize
                    ).deliver(outbox)
            session.commit()

        with harness.session_factory() as session:
            audits = list(
                session.scalars(
                    select(RuntimeAuditRecord).order_by(RuntimeAuditRecord.resource_id)
                )
            )
        assert [audit.reason_code for audit in audits] == [
            "CALLBACK_TARGET_MISSING",
            "AUTHORIZATION_REVOKED",
            "AUTHORIZATION_VERSION_CHANGED",
        ]
        assert all(
            set(audit.metadata_summary) == {"run_id", "status"} for audit in audits
        )


def test_postgres_draining_reaper_handover_fences_old_worker() -> None:
    """真实 PostgreSQL 上，draining 后只能由 reaper 交给唯一的新 fencing token。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    if not url:
        pytest.skip("未显式提供 AGENT_RUNTIME_TEST_POSTGRES_DSN")
    config = PostgresHarnessConfig(url, f"agent_runtime_test_{uuid4().hex[:12]}", 10)
    with PostgresSchemaHarness(config) as harness:
        assert harness.session_factory is not None
        with harness.session_factory() as setup:
            setup.add(
                AgentRun(
                    run_id="pg-drain-handover", agent_id="memoir_agent", agent_version="1.0.0",
                    package_digest="sha256:test", contract_version="1.0.0",
                    business_type="couple_memory", business_id="archive", status="pending",
                    dispatch_state="queued", input_json={}, authorization_version=1,
                    caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
                    callback_target_id="callback", business_connector_id="connector", trace_id="trace",
                    run_deadline_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            setup.commit()
        with harness.session_factory() as first_worker:
            first_lease = LeaseService(first_worker)
            old_context = first_lease.claim("pg-drain-handover", "worker-a")
            assert old_context is not None
            assert first_lease.release_for_drain("pg-drain-handover", old_context)
            with harness.session_factory() as reaper_session:
                assert LeaseService(reaper_session).reap_expired() == ["pg-drain-handover"]
            with harness.session_factory() as second_worker:
                new_context = LeaseService(second_worker).claim(
                    "pg-drain-handover", "worker-b"
                )
                assert new_context is not None
                assert new_context.execution_attempt == old_context.execution_attempt + 1
                assert new_context.fencing_token == old_context.fencing_token + 1
            assert first_lease.can_write("pg-drain-handover", old_context) is False


def test_postgres_two_workers_race_to_claim_only_one_attempt() -> None:
    """两个独立 PostgreSQL Session 同时认领时，只有一个 Worker 获得写权。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    if not url:
        pytest.skip("未显式提供 AGENT_RUNTIME_TEST_POSTGRES_DSN")
    config = PostgresHarnessConfig(url, f"agent_runtime_test_{uuid4().hex[:12]}", 10)
    with PostgresSchemaHarness(config) as harness:
        assert harness.session_factory is not None
        with harness.session_factory() as setup:
            setup.add(
                AgentRun(
                    run_id="pg-claim-race", agent_id="memoir_agent", agent_version="1.0.0",
                    package_digest="sha256:test", contract_version="1.0.0",
                    business_type="couple_memory", business_id="archive", status="pending",
                    dispatch_state="queued", input_json={}, authorization_version=1,
                    caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
                    callback_target_id="callback", business_connector_id="connector", trace_id="trace",
                    run_deadline_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            setup.commit()

        def claim(owner: str) -> object:
            session = harness.session_factory()
            try:
                return LeaseService(session).claim("pg-claim-race", owner)
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            contexts = list(workers.map(claim, ("worker-a", "worker-b")))
        claimed = [context for context in contexts if context is not None]

        assert len(claimed) == 1
        assert claimed[0].execution_attempt == 1
        assert claimed[0].fencing_token == 1


def test_postgres_processes_share_held_bind_start_publish_callback_and_reconcile() -> (
    None
):
    """真实 API、Worker、Reconciler 必须以同一临时 PostgreSQL schema 完成安全闭环。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    if not url:
        pytest.skip("未显式提供 AGENT_RUNTIME_TEST_POSTGRES_DSN")
    try:
        mock_port, api_port = _available_port(), _available_port()
    except PermissionError:
        pytest.skip("当前受限环境禁止绑定 loopback 端口")
    config = PostgresHarnessConfig(url, f"agent_runtime_test_{uuid4().hex[:12]}", 10)

    with (
        PostgresSchemaHarness(config) as database,
        ProcessHarness(timeout_seconds=10, postgres=config) as processes,
    ):
        assert database.session_factory is not None
        processes.start_mock_business(mock_port)
        processes.start_api(api_port, mock_port=mock_port)
        archive_id, snapshot_id = _create_bound_archive(database.session_factory)
        create_path = "/api/v1/runtime/agent-runs"
        create_body = json.dumps(
            {
                "agent_id": "memoir_agent",
                "agent_version": "1.0.0",
                "business_type": "couple_memory",
                "business_id": archive_id,
                "start_mode": "held",
                "input": {
                    "archive_id": archive_id,
                    "snapshot_id": snapshot_id,
                    "generation_epoch": 0,
                },
                "callback_target_id": "harness_callback",
                "business_connector_id": "harness_business",
            },
            separators=(",", ":"),
        ).encode()
        created = httpx.post(
            f"http://127.0.0.1:{api_port}{create_path}",
            content=create_body,
            headers=_headers(
                "POST",
                create_path,
                create_body,
                "harness-create",
                processes.identity_id,
            ),
            timeout=5,
        )
        assert created.status_code == 201
        run_id = created.json()["run_id"]
        _bind_run(database.session_factory, archive_id, snapshot_id, run_id)
        start_path = f"/api/v1/runtime/agent-runs/{run_id}/start"
        started = httpx.post(
            f"http://127.0.0.1:{api_port}{start_path}",
            content=b"{}",
            headers=_headers(
                "POST", start_path, b"{}", "harness-start", processes.identity_id
            ),
            timeout=5,
        )
        assert started.status_code == 200
        # 两个真实 Worker 子进程共享同一 PostgreSQL schema；条件 claim 必须让
        # 任一时刻只有一个进程执行这个 Run。
        first_worker = processes.start_worker(mock_port=mock_port)
        second_worker = processes.start_worker(mock_port=mock_port)
        assert processes.wait_for_exit(first_worker) == 0
        assert processes.wait_for_exit(second_worker) == 0
        with database.session_factory() as verify_session:
            executed = verify_session.scalar(
                select(AgentRun).where(AgentRun.run_id == run_id)
            )
        assert executed is not None and executed.execution_attempt == 1
        # 终态 callback 由下一轮 dispatcher 从独立 outbox 投递。
        assert processes.wait_for_exit(processes.start_worker(mock_port=mock_port)) == 0
        purge_path = f"/api/v1/runtime/agent-runs/{run_id}/purge-private-data"
        purge_response = httpx.post(
            f"http://127.0.0.1:{api_port}{purge_path}",
            content=b"{}",
            headers=_headers(
                "POST", purge_path, b"{}", "harness-purge", processes.identity_id
            ),
            timeout=5,
        )
        assert purge_response.status_code == 202
        assert purge_response.json()["privacy_state"] == "purge_requested"
        assert (
            processes.wait_for_exit(processes.start_reconciler(mock_port=mock_port))
            == 0
        )

        detail_path = f"/api/v1/runtime/agent-runs/{run_id}"
        detail = httpx.get(
            f"http://127.0.0.1:{api_port}{detail_path}",
            headers=_headers("GET", detail_path, b"", "unused", processes.identity_id),
            timeout=5,
        )
        state = httpx.get(f"http://127.0.0.1:{mock_port}/state", timeout=5).json()
        assert detail.status_code == 200
        assert detail.json()["status"] == "succeeded"
        assert detail.json()["privacy_state"] == "purged"
        assert state["callback_count"] >= 2
        assert state["last_status"] == "succeeded"
        assert state["published_revision"] == 1
        assert state["snapshot_reads"] == 1


def test_postgres_real_worker_audits_revoked_authorization_and_missing_target() -> None:
    """真实 Worker 派发 callback 前必须复核当前授权与 target，拒绝时不触网。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    if not url:
        pytest.skip("未显式提供 AGENT_RUNTIME_TEST_POSTGRES_DSN")
    try:
        mock_port, api_port = _available_port(), _available_port()
    except PermissionError:
        pytest.skip("当前受限环境禁止绑定 loopback 端口")
    config = PostgresHarnessConfig(url, f"agent_runtime_test_{uuid4().hex[:12]}", 10)

    with (
        PostgresSchemaHarness(config) as database,
        ProcessHarness(timeout_seconds=10, postgres=config) as processes,
    ):
        assert database.session_factory is not None
        processes.start_mock_business(mock_port)
        processes.start_api(api_port, mock_port=mock_port)
        run_ids: list[str] = []
        for index in range(2):
            archive_id, snapshot_id = _create_bound_archive(
                database.session_factory, relationship_id=index + 10
            )
            run_ids.append(
                _create_and_start_run(
                    api_port,
                    processes.identity_id,
                    archive_id,
                    snapshot_id,
                    database.session_factory,
                    key_suffix=f"callback-reject-{index}",
                )
            )

        first_worker = processes.start_worker(mock_port=mock_port)
        assert processes.wait_for_completed(first_worker, "worker") == "completed"
        assert processes.wait_for_exit(first_worker) == 0
        before = httpx.get(
            f"http://127.0.0.1:{mock_port}/state", timeout=5
        ).json()["callback_count"]

        with database.session_factory() as session:
            revoked = session.scalar(
                select(AgentRun).where(AgentRun.run_id == run_ids[0])
            )
            missing = session.scalar(
                select(AgentRun).where(AgentRun.run_id == run_ids[1])
            )
            assert revoked is not None and missing is not None
            revoked.caller_id = "revoked-caller"
            missing.callback_target_id = "removed-target"
            session.commit()

        dispatcher_worker = processes.start_worker(mock_port=mock_port)
        assert processes.wait_for_completed(dispatcher_worker, "worker") == "completed"
        assert processes.wait_for_exit(dispatcher_worker) == 0

        with database.session_factory() as session:
            audits = list(
                session.scalars(
                    select(RuntimeAuditRecord).where(
                        RuntimeAuditRecord.resource_id.in_(run_ids)
                    )
                )
            )
        assert {audit.reason_code for audit in audits} == {
            "AUTHORIZATION_REVOKED",
            "CALLBACK_TARGET_MISSING",
        }
        assert all(
            set(audit.metadata_summary) == {"run_id", "status"} for audit in audits
        )
        after = httpx.get(
            f"http://127.0.0.1:{mock_port}/state", timeout=5
        ).json()["callback_count"]
        assert after == before


def test_postgres_late_publish_after_cancel_and_purge_cannot_restore_private_state() -> None:
    """真实子进程中，请求已发出后的迟到 409 也不能越过 cancel/purge 屏障。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    if not url:
        pytest.skip("未显式提供 AGENT_RUNTIME_TEST_POSTGRES_DSN")
    try:
        mock_port, api_port = _available_port(), _available_port()
    except PermissionError:
        pytest.skip("当前受限环境禁止绑定 loopback 端口")
    config = PostgresHarnessConfig(url, f"agent_runtime_test_{uuid4().hex[:12]}", 10)

    with (
        PostgresSchemaHarness(config) as database,
        ProcessHarness(timeout_seconds=10, postgres=config) as processes,
    ):
        assert database.session_factory is not None
        processes.start_mock_business(mock_port)
        processes.start_api(api_port, mock_port=mock_port)
        archive_id, snapshot_id = _create_bound_archive(database.session_factory)
        run_id = _create_and_start_run(
            api_port, processes.identity_id, archive_id, snapshot_id, database.session_factory,
        )
        armed = httpx.post(
            f"http://127.0.0.1:{mock_port}/__harness__/block-next-publish",
            headers={"X-Harness-Control": processes.identity_id}, timeout=5,
        )
        assert armed.status_code == 202
        worker = processes.start_worker(mock_port=mock_port)
        _wait_for_mock_publish_start(mock_port)

        for path, key in (
            (f"/api/v1/runtime/agent-runs/{run_id}/cancel", "harness-cancel-late"),
            (f"/api/v1/runtime/agent-runs/{run_id}/purge-private-data", "harness-purge-late"),
        ):
            response = httpx.post(
                f"http://127.0.0.1:{api_port}{path}", content=b"{}",
                headers=_headers("POST", path, b"{}", key, processes.identity_id), timeout=5,
            )
            assert response.status_code in {200, 202}
        released = httpx.post(
            f"http://127.0.0.1:{mock_port}/__harness__/release-publish",
            headers={"X-Harness-Control": processes.identity_id}, timeout=5,
        )
        assert released.status_code == 202
        assert processes.wait_for_completed(worker, "worker") == "completed"
        assert processes.wait_for_exit(worker) == 0
        assert processes.wait_for_exit(processes.start_reconciler(mock_port=mock_port)) == 0

        with database.session_factory() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
            assert run is not None and run.privacy_state == "purged"
            assert session.scalars(select(AgentCheckpoint).where(AgentCheckpoint.run_id == run_id)).all() == []
            assert all(
                artifact.summary_json is None
                for artifact in session.scalars(select(AgentArtifact).where(AgentArtifact.run_id == run_id))
            )
            assert all(
                step.input_summary is None and step.output_summary is None
                for step in session.scalars(select(AgentStep).where(AgentStep.run_id == run_id))
            )
            assert all(
                call.input_summary is None and call.output_summary is None
                for call in session.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run_id))
            )
        state = httpx.get(f"http://127.0.0.1:{mock_port}/state", timeout=5).json()
        assert state["published_revision"] == 0


def test_postgres_delayed_model_response_after_cancel_and_purge_stays_contentless() -> None:
    """真实 Worker 已发模型请求后失效，释放响应也只能结算无内容 usage。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    redis_url = os.environ.get("AGENT_RUNTIME_TEST_REDIS_URL")
    if not url or not redis_url:
        pytest.skip("未显式提供 PostgreSQL 与隔离 Redis harness")
    try:
        mock_port, provider_port, api_port = _available_port(), _available_port(), _available_port()
    except PermissionError:
        pytest.skip("当前受限环境禁止绑定 loopback 端口")
    config = PostgresHarnessConfig(url, f"agent_runtime_test_{uuid4().hex[:12]}", 10)

    with (
        PostgresSchemaHarness(config) as database,
        ProcessHarness(timeout_seconds=10, postgres=config, redis_url=redis_url) as processes,
    ):
        assert database.session_factory is not None
        processes.start_mock_business(mock_port)
        processes.start_mock_provider(provider_port)
        processes.start_api(
            api_port, mock_port=mock_port, provider_port=provider_port
        )
        archive_id, snapshot_id = _create_bound_archive(database.session_factory)
        run_id = _create_and_start_run(
            api_port, processes.identity_id, archive_id, snapshot_id, database.session_factory,
        )
        with database.session_factory() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
            assert run is not None
            assert run.capability_snapshot_json.get("allowed_model_route_ids") == [
                "harness-model"
            ]
        provider_base = f"http://127.0.0.1:{provider_port}"
        control = {"X-Harness-Control": processes.identity_id}
        assert httpx.post(
            f"{provider_base}/__harness__/block-next-model", headers=control, timeout=5,
        ).status_code == 202
        worker = processes.start_worker(mock_port=mock_port, provider_port=provider_port)
        _wait_for_mock_model_start(provider_base)

        for path, key in (
            (f"/api/v1/runtime/agent-runs/{run_id}/cancel", "harness-cancel-model"),
            (f"/api/v1/runtime/agent-runs/{run_id}/purge-private-data", "harness-purge-model"),
        ):
            response = httpx.post(
                f"http://127.0.0.1:{api_port}{path}", content=b"{}",
                headers=_headers("POST", path, b"{}", key, processes.identity_id), timeout=5,
            )
            assert response.status_code in {200, 202}
        assert httpx.post(
            f"{provider_base}/__harness__/release-model", headers=control, timeout=5,
        ).status_code == 202
        assert processes.wait_for_completed(worker, "worker") == "completed"
        assert processes.wait_for_exit(worker) == 0
        assert processes.wait_for_exit(processes.start_reconciler(mock_port=mock_port)) == 0

        with database.session_factory() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
            usages = session.scalars(select(AgentModelUsage).where(AgentModelUsage.run_id == run_id)).all()
            assert run is not None and run.privacy_state == "purged"
            assert usages and all(usage.status in {"outcome_unknown", "aborted_before_send"} for usage in usages)
            assert all(usage.thinking_summary_json is None or "prompt" not in str(usage.thinking_summary_json) for usage in usages)
            assert session.scalars(select(AgentCheckpoint).where(AgentCheckpoint.run_id == run_id)).all() == []
            assert all(
                step.input_summary is None and step.output_summary is None
                for step in session.scalars(select(AgentStep).where(AgentStep.run_id == run_id))
            )
        assert httpx.get(f"http://127.0.0.1:{mock_port}/state", timeout=5).json()["published_revision"] == 0


def test_postgres_delayed_repair_response_after_cancel_and_purge_stays_contentless() -> None:
    """真实 Worker 的第二个 repair attempt 迟到时不得恢复任何运行或业务内容。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    redis_url = os.environ.get("AGENT_RUNTIME_TEST_REDIS_URL")
    if not url or not redis_url:
        pytest.skip("未显式提供 PostgreSQL 与隔离 Redis harness")
    try:
        mock_port, provider_port, api_port = (
            _available_port(),
            _available_port(),
            _available_port(),
        )
    except PermissionError:
        pytest.skip("当前受限环境禁止绑定 loopback 端口")
    config = PostgresHarnessConfig(
        url,
        f"agent_runtime_test_{uuid4().hex[:12]}",
        10,
    )

    with (
        PostgresSchemaHarness(config) as database,
        ProcessHarness(
            timeout_seconds=10,
            postgres=config,
            redis_url=redis_url,
        ) as processes,
    ):
        assert database.session_factory is not None
        processes.start_mock_business(mock_port)
        processes.start_mock_provider(provider_port)
        processes.start_api(
            api_port,
            mock_port=mock_port,
            provider_port=provider_port,
        )
        archive_id, snapshot_id = _create_bound_archive(
            database.session_factory,
        )
        run_id = _create_and_start_run(
            api_port,
            processes.identity_id,
            archive_id,
            snapshot_id,
            database.session_factory,
            key_suffix="late-repair",
        )
        provider_base = f"http://127.0.0.1:{provider_port}"
        control = {"X-Harness-Control": processes.identity_id}
        assert httpx.post(
            f"{provider_base}/__harness__/block-repair-after-invalid",
            headers=control,
            timeout=5,
        ).status_code == 202
        worker = processes.start_worker(
            mock_port=mock_port,
            provider_port=provider_port,
        )
        _wait_for_mock_model_start(provider_base)

        for path, key in (
            (
                f"/api/v1/runtime/agent-runs/{run_id}/cancel",
                "harness-cancel-repair",
            ),
            (
                f"/api/v1/runtime/agent-runs/{run_id}/purge-private-data",
                "harness-purge-repair",
            ),
        ):
            response = httpx.post(
                f"http://127.0.0.1:{api_port}{path}",
                content=b"{}",
                headers=_headers(
                    "POST",
                    path,
                    b"{}",
                    key,
                    processes.identity_id,
                ),
                timeout=5,
            )
            assert response.status_code in {200, 202}
        assert httpx.post(
            f"{provider_base}/__harness__/release-model",
            headers=control,
            timeout=5,
        ).status_code == 202
        assert processes.wait_for_completed(worker, "worker") == "completed"
        assert processes.wait_for_exit(worker) == 0
        assert processes.wait_for_exit(
            processes.start_reconciler(mock_port=mock_port),
        ) == 0

        with database.session_factory() as session:
            run = session.scalar(
                select(AgentRun).where(AgentRun.run_id == run_id),
            )
            usages = session.scalars(
                select(AgentModelUsage)
                .where(AgentModelUsage.run_id == run_id)
                .order_by(AgentModelUsage.model_attempt),
            ).all()
            assert run is not None and run.privacy_state == "purged"
            assert len(usages) == 2
            assert [usage.model_attempt for usage in usages] == [1, 2]
            assert usages[0].status == "succeeded"
            assert usages[1].status in {
                "outcome_unknown",
                "aborted_before_send",
            }
            assert [usage.prompt_id for usage in usages] == [
                "highlight-extract",
                "structured-output-repair",
            ]
            assert session.scalars(
                select(AgentCheckpoint).where(
                    AgentCheckpoint.run_id == run_id,
                ),
            ).all() == []
            assert all(
                step.input_summary is None and step.output_summary is None
                for step in session.scalars(
                    select(AgentStep).where(
                        AgentStep.run_id == run_id,
                    ),
                )
            )
        assert httpx.get(
            f"http://127.0.0.1:{mock_port}/state",
            timeout=5,
        ).json()["published_revision"] == 0


def _create_and_start_run(
    api_port: int, identity_id: str, archive_id: str, snapshot_id: str, factory: object,
    *,
    key_suffix: str = "late",
) -> str:
    create_path = "/api/v1/runtime/agent-runs"
    create_body = json.dumps(
        {
            "agent_id": "memoir_agent", "agent_version": "1.0.0",
            "business_type": "couple_memory", "business_id": archive_id,
            "start_mode": "held",
            "input": {"archive_id": archive_id, "snapshot_id": snapshot_id, "generation_epoch": 0},
            "callback_target_id": "harness_callback", "business_connector_id": "harness_business",
        }, separators=(",", ":"),
    ).encode()
    created = httpx.post(
        f"http://127.0.0.1:{api_port}{create_path}", content=create_body,
        headers=_headers(
            "POST", create_path, create_body, f"harness-create-{key_suffix}", identity_id
        ),
        timeout=5,
    )
    assert created.status_code == 201
    run_id = created.json()["run_id"]
    _bind_run(factory, archive_id, snapshot_id, run_id)
    start_path = f"/api/v1/runtime/agent-runs/{run_id}/start"
    started = httpx.post(
        f"http://127.0.0.1:{api_port}{start_path}", content=b"{}",
        headers=_headers(
            "POST", start_path, b"{}", f"harness-start-{key_suffix}", identity_id
        ),
        timeout=5,
    )
    assert started.status_code == 200
    return run_id


def _wait_for_mock_publish_start(mock_port: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = httpx.get(f"http://127.0.0.1:{mock_port}/state", timeout=1).json()
        if state.get("publish_started") is True:
            return
        time.sleep(0.05)
    raise AssertionError("mock publish did not reach delayed boundary")


def _wait_for_mock_model_start(provider_base: str) -> None:
    deadline = time.monotonic() + 5
    model_calls = 0
    while time.monotonic() < deadline:
        try:
            state = httpx.get(
                f"{provider_base}/state",
                timeout=1,
            ).json()
        except httpx.TransportError:
            # Worker 与控制面同时访问单进程回环 mock 时，短暂连接排队不代表
            # Provider 请求未发生；继续在总 deadline 内读取无内容聚合状态。
            time.sleep(0.05)
            continue
        model_calls = int(state.get("model_calls", 0))
        if state.get("model_started") is True:
            return
        time.sleep(0.05)
    # 失败消息仅包含聚合调用计数，不能携带请求正文或 Provider 响应。
    raise AssertionError(
        "mock provider did not reach delayed boundary "
        f"calls={model_calls}"
    )


def _create_bound_archive(
    factory: object, *, relationship_id: int = 1
) -> tuple[str, str]:
    session = factory()
    try:
        archive = MemoryArchiveService(
            session, FernetSnapshotCipher(Fernet.generate_key())
        ).create_archives_for_relationship(
            FrozenMemoryInput(
                relationship_id,
                f"harness-space-{relationship_id}",
                1,
                (1, 2),
                {},
                datetime(2026, 7, 28, tzinfo=UTC),
                {},
                {},
                "v1",
            )
        )[0]
        snapshot = session.scalar(
            select(MemorySnapshot).where(
                MemorySnapshot.archive_id == archive.archive_id
            )
        )
        assert snapshot is not None
        session.commit()
        return archive.archive_id, snapshot.snapshot_id
    finally:
        session.close()


def _bind_run(factory: object, archive_id: str, snapshot_id: str, run_id: str) -> None:
    session = factory()
    try:
        MemoryAgentBindingService(session).bind(
            archive_id, run_id, 0, snapshot_id=snapshot_id
        )
        session.commit()
    finally:
        session.close()


def _headers(
    method: str, path: str, body: bytes, idempotency_key: str, identity_id: str
) -> dict[str, str]:
    timestamp = str(int(datetime.now(UTC).timestamp()))
    canonical = f"{method}\n{path}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}"
    signature = hmac.new(
        f"harness-only-{identity_id}".encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()
    return {
        "X-Agent-Client-Id": identity_id,
        "X-Agent-Key-Id": "test",
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Signature": signature,
        "Idempotency-Key": idempotency_key,
        "Content-Type": "application/json",
    }


def _available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
