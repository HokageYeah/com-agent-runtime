"""无端口的跨仓历史 Run 单链证据。

Runtime 与 business 都使用 ``app`` 顶级包，故 business provider 留在一个长期存活的
独立解释器。父进程是纯 pipe broker：它把 business 真正发出的、已签名的 Runtime
GET 交给 Runtime 的真实 FastAPI TestClient；随后又把 Runtime ToolGateway 的三次原始
HTTP 请求交给同一 provider TestClient。因此没有 loopback listener、没有手写 provider
JSON，也不会为每个 Tool 请求重新 seed SQLite。
"""

from __future__ import annotations

import base64
import json
import subprocess
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - register all Runtime tables
from app.core.config import settings
from app.main import create_runtime_app
from app.models import AgentDefinition, AgentRun, AgentToolCall
from app.runtime.checkpoint import CheckpointStore, FernetCheckpointCipher
from app.runtime.interfaces import LeaseContext
from app.runtime.planner import StaticPlanner
from app.runtime.test_harness import LoopbackTestTransport, RuntimeHarnessConfig
from app.runtime.tool_gateway import BusinessConnector, ToolGateway
from app.schemas.agent_package import AgentPackage
from app.services.agent_package_service import AgentPackageService

_BUSINESS_ROOT = Path("/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b")
_BUSINESS_PYTHON = _BUSINESS_ROOT / ".venv" / "bin" / "python"
_DIGEST_100 = "sha256:a6e2f53e223658fb648026335373d23f548232e5dd2c4c67a2c774df6e67833e"
_RUN_ID = "historical-cross-repo-1-0-0"
_ARCHIVE_ID = "01J00000000000000000000001"
_SNAPSHOT_ID = "01J00000000000000000000002"


# The child owns both its SQLite session and TestClient for its whole lifetime.
# Runtime GET is deliberately a transport request, not an authoritative adapter:
# MemoryRuntimeClient signs it and the parent routes it to Runtime's real endpoint.
_BUSINESS_SERVER = textwrap.dedent(
    """
    import base64, importlib.util, json, sys
    from pathlib import Path
    from unittest.mock import patch
    import httpx
    from fastapi.testclient import TestClient
    from pytest import MonkeyPatch
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    spec = importlib.util.spec_from_file_location(
        "provider_test_helpers", "tests/test_memory_agent_tools_api.py"
    )
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    from app.core.config import settings
    from app.db.sqlalchemy_db import get_sqlalchemy_db
    from app.main import app
    from app.services.memory.memory_runtime_adapter_service import MemoryRuntimeAdapterService
    from app.services.memory.memory_runtime_client import MemoryRuntimeClient
    from app.services.memory.memory_runref_identity_repair_service import repair_active_null_runref_identities

    class RuntimePipeTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            print(json.dumps({"kind": "runtime_request", "method": request.method,
                "path": request.url.raw_path.decode(), "headers": dict(request.headers),
                "body": base64.b64encode(request.content).decode()}), flush=True)
            reply = json.loads(sys.stdin.readline())
            assert reply["kind"] == "runtime_response"
            return httpx.Response(reply["status"], headers=reply.get("headers", {}),
                content=base64.b64decode(reply["body"]), request=request)

    monkeypatch = MonkeyPatch()
    session = helpers._create_test_session()
    monkeypatch.setattr(settings, "MEMORY_RUNTIME_CLIENT_ID", helpers._TEST_RUNTIME_ID)
    monkeypatch.setattr(settings, "MEMORY_RUNTIME_KEY_ID", helpers._TEST_KEY_ID)
    monkeypatch.setattr(settings, "MEMORY_RUNTIME_SECRET", helpers._TEST_SECRET)
    def override_db():
        yield session
    app.dependency_overrides[get_sqlalchemy_db] = override_db
    archive, snapshot, runref = helpers._seed_archive(session, run_id="historical-cross-repo-1-0-0")
    # Start from the actual predecessor physical shape, then run the real Alembic
    # upgrade in this *same* provider database. The migration itself must not guess.
    session.commit()
    connection = session.connection()
    for column in ("agent_id", "agent_version", "business_type"):
        connection.exec_driver_sql("ALTER TABLE memory_agent_run_refs DROP COLUMN " + column)
    migration_path = next(Path("alembic/versions").glob("*_add_memory_runref_identity.py"))
    migration_spec = importlib.util.spec_from_file_location("bridge_identity_migration", migration_path)
    migration = importlib.util.module_from_spec(migration_spec)
    migration_spec.loader.exec_module(migration)
    migration.op = Operations(MigrationContext.configure(connection))
    migration.upgrade()
    session.commit()
    session.expire_all()
    runref = session.get(type(runref), runref.id)
    assert runref is not None
    with patch("app.main.setup_logging"), patch("app.main.database.connect"), patch("app.main.database.close"), TestClient(app) as client:
        print(json.dumps({"kind":"ready", "archive_id":archive.archive_id,
            "snapshot_id":snapshot.snapshot_id, "epoch":archive.generation_epoch}), flush=True)
        for raw in sys.stdin:
            command = json.loads(raw)
            if command["kind"] == "repair":
                async_client = httpx.AsyncClient(transport=RuntimePipeTransport(), base_url="http://runtime.test")
                adapter = MemoryRuntimeAdapterService(MemoryRuntimeClient(
                    base_url="http://runtime.test", client_id="couple-diary", key_id="dev",
                    secret="development-secret", http_client=async_client))
                import asyncio
                result = asyncio.run(repair_active_null_runref_identities(session, adapter=adapter))
                session.refresh(runref)
                print(json.dumps({"kind":"repair_result", "repaired":result.repaired,
                    "identity":[runref.agent_id, runref.agent_version, runref.business_type]}), flush=True)
            elif command["kind"] == "provider_request":
                response = client.request(command["method"], command["path"],
                    headers=command["headers"], content=base64.b64decode(command["body"]))
                print(json.dumps({"kind":"provider_response", "status":response.status_code,
                    "body":base64.b64encode(response.content).decode()}), flush=True)
            elif command["kind"] == "close":
                break
    app.dependency_overrides.pop(get_sqlalchemy_db, None)
    session.close()
    monkeypatch.undo()
    """
)


class _PersistentBusinessBridge(httpx.BaseTransport):
    """Pipes all ToolGateway calls to one independent provider interpreter."""

    def __init__(self, runtime_client: TestClient) -> None:
        self._runtime_client = runtime_client
        self._process = subprocess.Popen(
            [_BUSINESS_PYTHON, "-c", _BUSINESS_SERVER], cwd=_BUSINESS_ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self.ready = self._read_until({"ready"})

    def _write(self, payload: dict[str, Any]) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(payload) + "\n")
        self._process.stdin.flush()

    def _read_until(self, expected: set[str]) -> dict[str, Any]:
        assert self._process.stdout is not None
        while line := self._process.stdout.readline():
            message = json.loads(line)
            if message["kind"] == "runtime_request":
                response = self._runtime_client.request(
                    message["method"], message["path"], headers=message["headers"],
                    content=base64.b64decode(message["body"]),
                )
                self._write({"kind": "runtime_response", "status": response.status_code,
                    "headers": dict(response.headers), "body": base64.b64encode(response.content).decode()})
                continue
            if message["kind"] in expected:
                return message
            raise AssertionError(f"unexpected bridge message {message['kind']}")
        raise AssertionError("business bridge exited before responding")

    def repair(self) -> dict[str, Any]:
        self._write({"kind": "repair"})
        return self._read_until({"repair_result"})

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._write({"kind": "provider_request", "method": request.method,
            "path": request.url.raw_path.decode(), "headers": dict(request.headers),
            "body": base64.b64encode(request.content).decode()})
        message = self._read_until({"provider_response"})
        return httpx.Response(message["status"], content=base64.b64decode(message["body"]), request=request)

    def close(self) -> None:
        if self._process.poll() is None:
            self._write({"kind": "close"})
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                self._process.wait(timeout=10)


def _package_100() -> AgentPackage:
    package = AgentPackageService(Path(__file__).parents[1] / "app" / "agents").load("memoir_agent", "1.0.0")
    assert package.package_digest == _DIGEST_100
    return package


def _runtime_run(session: Session, package: AgentPackage, monkeypatch: Any) -> AgentRun:
    session.add(AgentDefinition(agent_id=package.agent_id, version=package.version,
        runtime_type="workflow", definition_json=package.model_dump(mode="json"),
        package_digest=package.package_digest, contract_version=package.contract_version,
        status="active", status_changed_at=datetime.now(UTC), status_changed_by="test",
        status_change_reason="historical bridge"))
    run = AgentRun(run_id=_RUN_ID, agent_id=package.agent_id, agent_version=package.version,
        package_digest=package.package_digest, contract_version=package.contract_version,
        business_type="couple_memory", business_id=_ARCHIVE_ID, status="waiting_human",
        dispatch_state="claimed", input_json={"archive_id":_ARCHIVE_ID, "snapshot_id":_SNAPSHOT_ID, "generation_epoch":1},
        authorization_version=1, caller_id="couple-diary", tenant_id="couple-diary",
        create_idempotency_key="historical-cross", callback_target_id="memory_callback",
        business_connector_id="couple_diary_backend", trace_id="historical-cross-trace",
        execution_attempt=1, lease_owner="bridge-worker", fencing_token=1,
        lease_expires_at=datetime.now(UTC)+timedelta(minutes=5), run_deadline_at=datetime.now(UTC)+timedelta(days=1))
    session.add(run)
    StaticPlanner().persist(session, StaticPlanner().create_plan(run.run_id, package))
    session.flush()
    lease = LeaseContext(execution_attempt=1, lease_owner="bridge-worker", fencing_token=1,
        lease_expires_at=run.lease_expires_at, privacy_version=run.privacy_version,
        authorization_version=run.authorization_version)
    CheckpointStore(session, FernetCheckpointCipher(settings.MEMORY_SNAPSHOT_FERNET_KEY.encode())).save(run.run_id, "resume", {"completed_node_ids": []}, lease)
    session.commit()
    # Real persisted checkpoint is loaded by the Worker-configured executor. Only the
    # external node I/O is replaced, so this cross-process test never emits model/tool data.
    # 节点显式请求 waiting_human，让历史单链停在真实活跃态；后续 Tool 调用不再
    # 依赖测试私自把 succeeded Run 改回 running。
    class _Runner:
        def run_node(self, node: dict[str, object], run: AgentRun, state: object) -> dict[str, object]:
            del run, state
            return {"node_id": node["node_id"], "waiting_human": True}
    import app.worker as worker
    monkeypatch.setattr(worker.settings, "RUNTIME_BUSINESS_CONNECTORS_JSON",
        '{"couple_diary_backend":{"enabled":true,"base_url":"https://business.example.test",'
        '"runtime_id":"agent-runtime","key_id":"dev","secret":"test-secret"}}', raising=False)
    monkeypatch.setattr(worker, "MemoirNodeRunner", lambda *args: _Runner())
    result = worker.configured_executor(session).resume(run.run_id, lease)
    assert result.status == "waiting_human"
    session.refresh(run)
    assert run.status == "waiting_human"
    return run


def test_historical_1_0_0_cross_repo_single_persistent_chain(monkeypatch: Any) -> None:
    """Migration-shaped NULL ref → signed Runtime identity query → three persistent Tool calls."""
    assert _BUSINESS_PYTHON.is_file(), "cross-repo bridge requires the isolated business venv"
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    from app.db.sqlalchemy_db import Base
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    run = _runtime_run(session, _package_100(), monkeypatch)
    runtime_app = create_runtime_app(runtime_settings=settings, session_factory=factory)
    with TestClient(runtime_app) as runtime_client:
        bridge = _PersistentBusinessBridge(runtime_client)
        try:
            assert bridge.ready["archive_id"] == _ARCHIVE_ID
            repaired = bridge.repair()
            assert repaired == {"kind":"repair_result", "repaired":1,
                "identity":["memoir_agent", "1.0.0", "couple_memory"]}
            harness = RuntimeHarnessConfig(factory, {"test":{"keys":{"test":"test-agent-tool-secret"}}}, "couple-diary-test", "http://127.0.0.1:8765")
            gateway = ToolGateway({"couple_diary_backend": BusinessConnector("http://127.0.0.1:8765", "couple-diary-test", "test-key-1", "test-agent-tool-secret")}, httpx.Client(transport=bridge), test_transport=LoopbackTestTransport(harness))
            context = ToolGateway.build_tool_context(run, "historical-cross-step")
            snapshot = gateway.get_snapshot("couple_diary_backend", _ARCHIVE_ID, _SNAPSHOT_ID, _RUN_ID, 1, context)
            assert snapshot["snapshot_id"] == _SNAPSHOT_ID
            tool_call = AgentToolCall(tool_call_id="historic-publish", run_id=_RUN_ID, step_id="historical-cross-step", tool_name="memory.publish_playback_document", tool_version="1.0.0", transport="http", side_effect=True, idempotency_key="historic-publish", logical_operation_key="historic-publish", request_digest="safe", execution_attempt=1, tool_attempt=1, input_summary=None, output_summary=None, status="started", created_at=datetime.now(UTC))
            document = {"schemaVersion":"1.0.0", "title":"Historical", "scenes":[{"scene_id":"scene_cover", "scene_type":"cover", "order":1, "safety_level":"normal", "payload":{"hint":"safe"}, "source_refs":[]}], "actions":[{"action_id":"action_1", "scene_id":"scene_cover", "action_type":"advance", "duration_ms":1000, "order":1, "payload":{}}], "mediaManifest":[]}
            published = gateway.publish_playback_document("couple_diary_backend", _ARCHIVE_ID, _RUN_ID, _SNAPSHOT_ID, 1, document, "historic-publish", tool_call, context)
            assert published["revision"] == 1
            # Historical v1.0 uses the legacy two-field query shape.
            observed = gateway.get_publish_result("couple_diary_backend", _ARCHIVE_ID, _RUN_ID, "historic-publish", tool_context=context)
            assert observed is not None and observed["revision"] == 1
        finally:
            bridge.close()
            session.close()
