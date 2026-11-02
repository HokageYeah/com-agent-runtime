"""公共装配函数保留实时工具守卫、受控 transport 与 checkpoint 加密边界。"""

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

import app.worker as worker
from app.models import AgentDefinition, RuntimeAuditRecord
from app.runtime.checkpoint import CheckpointError
from app.runtime.test_harness import (
    LoopbackTestTransport,
    RuntimeDependencies,
    RuntimeHarnessConfig,
)
from app.runtime.tool_gateway import ToolGateway
from app.schemas.agent_package import ToolManifest
from app.services.lease_service import LeaseService
from tests.test_qa_factory import qa_run as qa_run


@pytest.fixture
def builder_run(qa_run):
    session, run = qa_run
    lease = LeaseService(session).claim(run.run_id, "builder-worker")
    assert lease is not None
    configuration = worker.settings.model_copy(update={
        "RUNTIME_TRUSTED_CLIENTS_JSON": '{"caller":{"authorization_version":1}}',
    })
    return session, run, lease, configuration


def _manifest():
    return ToolManifest(
        name="qa.get_question_context", version="1.0.0",
        connector_id="qa_business_backend", method="POST",
        relative_path="/api/v1/internal/agent-tools/qa.get_question_context",
        input_from="input", output_to="question_context", side_effect=False,
        cancellation_behavior="cancellable",
    )


def _context(run):
    return {**run.input_json, "run_id": run.run_id,
            "tool_context": ToolGateway.build_tool_context(run, "read")}


def _connector(base_url="https://8.8.8.8"):
    return {"base_url": base_url, "runtime_id": "runtime", "key_id": "fixture", "secret": "fixture"}


def _dependencies(configuration, client, verifier=None):
    return RuntimeDependencies(
        settings=configuration, session_factory=None, clock=None,
        callback_client=None, tool_client=client, transport_verifier=verifier,
    )


@pytest.mark.parametrize("boundary,code", [
    ("draining", "TOOL_CALL_DRAINING"),
    ("authorization", "TOOL_AUTHORIZATION_REVOKED"),
    ("authorization_version", "TOOL_AUTHORIZATION_REVOKED"),
    ("fencing", "TOOL_EXECUTION_CONTEXT_INVALID"),
    ("cancel", "TOOL_EXECUTION_CONTEXT_INVALID"),
    ("package", "TOOL_EXECUTION_CONTEXT_INVALID"),
])
def test_built_tool_gateway_rechecks_live_boundaries(builder_run, boundary, code):
    session, run, lease, configuration = builder_run
    requests = []
    draining = False

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"output": {"revision": 1}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = worker.build_tool_gateway(
            session, run, _connector(), lease, is_draining=lambda: draining,
            dependencies=_dependencies(configuration, client), runtime_settings=configuration,
        )
        context = _context(run)
        assert gateway.call(_manifest(), context) == {"revision": 1}
        if boundary == "draining":
            draining = True
        elif boundary == "authorization":
            configuration.RUNTIME_TRUSTED_CLIENTS_JSON = '{"other":{"authorization_version":1}}'
        elif boundary == "authorization_version":
            configuration.RUNTIME_TRUSTED_CLIENTS_JSON = '{"caller":{"authorization_version":2}}'
        elif boundary == "fencing":
            run.fencing_token += 1
        elif boundary == "cancel":
            run.cancel_requested_at = datetime.now(UTC)
        else:
            session.scalar(select(AgentDefinition)).status = "revoked"
        session.flush()
        with pytest.raises(ValueError, match=f"^{code}$"):
            gateway.call(_manifest(), context)
        assert len(requests) == 1
        if boundary.startswith("authorization"):
            audit = session.scalar(select(RuntimeAuditRecord).where(
                RuntimeAuditRecord.action == "tool_gateway_rejected",
            ))
            assert audit is not None
            assert audit.reason_code == (
                "AUTHORIZATION_REVOKED" if boundary == "authorization"
                else "AUTHORIZATION_VERSION_CHANGED"
            )
            assert audit.metadata_summary == {"run_id": run.run_id}


@pytest.mark.parametrize("mode", ["production", "harness", "private", "untrusted_verifier"])
def test_built_tool_gateway_preserves_transport_selection(builder_run, monkeypatch, mode):
    session, run, lease, configuration = builder_run
    requests = []
    resets = []
    peers = []

    class PeerTransport(httpx.MockTransport):
        def peer_ip(self):
            peers.append(True)
            return "8.8.8.8"

        def reset_peer_ip(self):
            resets.append(True)

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"output": {"revision": 1}})

    transport = PeerTransport(handler)
    monkeypatch.setattr(worker, "PeerTrackingHTTPTransport", lambda: transport)
    dependencies = None
    connector = _connector()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        if mode != "production":
            connector = _connector("http://127.0.0.1:9100")
            verifier = LoopbackTestTransport(RuntimeHarnessConfig(
                session_factory=None, trusted_clients={"caller": {}},
                runtime_id="runtime", mock_base_url=connector["base_url"],
            )) if mode == "harness" else object()
            configuration.RUNTIME_TOOL_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS = mode == "private"
            dependencies = _dependencies(configuration, client, verifier)
        if mode == "untrusted_verifier":
            with pytest.raises(ValueError, match="BUSINESS_CONNECTOR"):
                worker.build_tool_gateway(
                    session, run, connector, lease, is_draining=lambda: False,
                    dependencies=dependencies, runtime_settings=configuration,
                )
            assert not requests
            return
        gateway = worker.build_tool_gateway(
            session, run, connector, lease, is_draining=lambda: False,
            dependencies=dependencies, runtime_settings=configuration,
        )
        assert gateway.call(_manifest(), _context(run)) == {"revision": 1}
        assert len(requests) == 1
        assert resets == ([True] if mode == "production" else [])
        assert peers == ([True] if mode == "production" else [])
    transport.close()


def test_built_checkpoint_store_roundtrips_and_rejects_stale_lease(builder_run):
    session, run, lease, configuration = builder_run
    store = worker.build_checkpoint_store(session, configuration)
    state = {"completed_node_ids": ["read"]}
    store.save(run.run_id, "read", state, lease)
    assert store.load_latest(run.run_id, lease) == state
    run.fencing_token += 1
    session.flush()
    with pytest.raises(CheckpointError):
        store.load_latest(run.run_id, lease)


def test_built_checkpoint_store_rejects_invalid_key(builder_run):
    session, _, _, configuration = builder_run
    configuration.MEMORY_SNAPSHOT_FERNET_KEY = "invalid-key"
    with pytest.raises(ValueError):
        worker.build_checkpoint_store(session, configuration)
