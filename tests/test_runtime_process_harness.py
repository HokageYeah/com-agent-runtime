import socket
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text

from app.models import AgentDefinition
from app.runtime.callback_gateway import CallbackGateway, CallbackTarget
from app.runtime.harness_entry import HarnessProcessConfig, build_dependencies
from app.runtime.process_harness import ProcessHarness
from app.runtime.test_harness import LoopbackTestTransport, RuntimeHarnessConfig


def test_harness_process_config_only_serializes_allowed_safe_fields(
    tmp_path: Path,
) -> None:
    config = HarnessProcessConfig(
        sqlite_path=tmp_path / "runtime.db",
        port=12345,
        mock_port=12346,
        role="worker",
        identity_id="test-client-123",
        timeout_seconds=1.0,
    )

    payload = config.to_payload()

    assert payload == {
        "identity_id": "test-client-123",
        "mock_port": 12346,
        "port": 12345,
        "role": "worker",
        "sqlite_path": str(tmp_path / "runtime.db"),
        "timeout_seconds": 1.0,
    }
    assert all("secret" not in key and "key" not in key for key in payload)


def test_harness_process_config_carries_explicit_postgres_schema_target(
    tmp_path: Path,
) -> None:
    """跨进程 API/Worker/Reconciler 必须连接同一个显式临时 schema，不能退回 SQLite。"""
    config = HarnessProcessConfig(
        sqlite_path=tmp_path / "runtime.db",
        port=12345,
        mock_port=12346,
        role="worker",
        identity_id="test-client-123",
        timeout_seconds=1.0,
        database_url="postgresql+psycopg://test_runtime@127.0.0.1/test_runtime",
        schema="agent_runtime_test_123",
    )

    payload = config.to_payload()

    assert payload["database_url"] == (
        "postgresql+psycopg://test_runtime@127.0.0.1/test_runtime"
    )
    assert "password" not in str(payload)
    assert payload["schema"] == "agent_runtime_test_123"


def test_harness_process_config_rejects_unexpected_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "harness.json"
    config_path.write_text(
        '{"sqlite_path":"/tmp/runtime.db","port":12345,"mock_port":12346,"role":"worker",'
        '"identity_id":"test-client","timeout_seconds":1,"secret":"no"}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="TEST_HARNESS_CONFIG_INVALID"):
        HarnessProcessConfig.from_path(config_path)


def test_harness_dependencies_seed_only_fixed_test_agent_package(
    tmp_path: Path,
) -> None:
    dependencies = build_dependencies(
        HarnessProcessConfig(
            sqlite_path=tmp_path / "runtime.db",
            port=12345,
            mock_port=12346,
            role="worker",
            identity_id="test-client",
            timeout_seconds=1,
        )
    )
    session = dependencies.session_factory()
    try:
        definition = session.scalar(select(AgentDefinition))
        assert definition is not None
        assert (definition.agent_id, definition.package_digest) == (
            "memoir_agent",
            "sha256:harness-memoir",
        )
        assert "prompt" not in str(definition.definition_json).lower()
    finally:
        session.close()


def test_process_harness_reclaims_child_and_temporary_directory() -> None:
    with ProcessHarness(timeout_seconds=2) as harness:
        path = harness.path
        child = harness.start([sys.executable, "-c", "import time; time.sleep(30)"])
        assert child.poll() is None and path.exists()
    assert child.poll() is not None and not path.exists()


def test_process_harness_does_not_inherit_parent_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_TEST_SHOULD_NOT_ESCAPE", "private")
    with ProcessHarness() as harness:
        child = harness.start(
            [
                sys.executable,
                "-c",
                "import os, sys; sys.exit(int('RUNTIME_TEST_SHOULD_NOT_ESCAPE' in os.environ))",
            ]
        )

        assert harness.wait_for_exit(child) == 0


def test_process_harness_initializes_isolated_sqlite() -> None:
    with ProcessHarness() as harness:
        with harness.sqlite_session_factory()() as session:
            assert session.execute(text("SELECT 1")).scalar() == 1


def test_process_harness_starts_loopback_mock_service() -> None:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("当前受限环境禁止绑定 loopback 端口")
        port = probe.getsockname()[1]
    with ProcessHarness() as harness:
        harness.start_mock_business(port)
        assert httpx.get(f"http://127.0.0.1:{port}/health").json() == {
            "status": "mock_ready"
        }


def test_loopback_mock_projects_only_signed_safe_callback() -> None:
    try:
        port = _available_loopback_port()
    except PermissionError:
        pytest.skip("当前受限环境禁止绑定 loopback 端口")
    identity = "mock-client"
    with ProcessHarness() as harness:
        harness.start_mock_business(port, identity_id=identity)
        config = RuntimeHarnessConfig(
            session_factory=object(),
            trusted_clients={identity: {}},
            runtime_id="runtime",
            mock_base_url=f"http://127.0.0.1:{port}",
            timeout_seconds=1,
        )
        gateway = CallbackGateway(
            {
                "harness_callback": CallbackTarget(
                    f"http://127.0.0.1:{port}/callbacks",
                    "agent-runtime-harness",
                    "test",
                    f"harness-only-{identity}",
                )
            },
            httpx.Client(),
            test_transport=LoopbackTestTransport(config),
        )
        gateway.send(
            "harness_callback",
            {
                "event": "run_succeeded",
                "event_id": "event-1",
                "run_id": "run-1",
                "event_seq": 1,
                "status_version": 2,
                "agent_id": "memoir_agent",
                "business_id": "archive-1",
                "status": "succeeded",
                "error": None,
                "public_trace": [{"step": "publish_document", "status": "succeeded"}],
            },
        )

        assert httpx.get(f"http://127.0.0.1:{port}/state").json() == {
            "callback_count": 1,
            "last_status": "succeeded",
            "published_revision": 0,
            "snapshot_reads": 0,
            "publish_blocked": False,
            "publish_started": False,
        }


def test_loopback_mock_publish_delay_control_is_opaque_and_loopback_only() -> None:
    """延迟控制只暴露状态机，不读取或回显业务请求正文。"""
    try:
        port = _available_loopback_port()
    except PermissionError:
        pytest.skip("当前受限环境禁止绑定 loopback 端口")
    identity = "mock-control-client"
    with ProcessHarness() as harness:
        harness.start_mock_business(port, identity_id=identity)
        headers = {"X-Harness-Control": identity}
        assert httpx.post(
            f"http://127.0.0.1:{port}/__harness__/block-next-publish",
            headers=headers,
        ).json() == {"status": "armed"}
        state = httpx.get(f"http://127.0.0.1:{port}/state").json()
        assert state["publish_blocked"] is True
        assert state["publish_started"] is False
        assert httpx.post(
            f"http://127.0.0.1:{port}/__harness__/release-publish",
            headers=headers,
        ).json() == {"status": "released"}
        assert "input" not in str(state)


def _available_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_process_harness_starts_api_worker_and_reconciler_with_safe_readiness() -> None:
    try:
        mock_port = _available_loopback_port()
        api_port = _available_loopback_port()
    except PermissionError:
        pytest.skip("当前受限环境禁止绑定 loopback 端口")
    with ProcessHarness(timeout_seconds=5) as harness:
        harness.start_mock_business(mock_port)
        harness.start_api(api_port, mock_port=mock_port)
        response = httpx.get(
            f"http://127.0.0.1:{api_port}/api/v1/runtime/health/live", timeout=2
        )
        worker = harness.start_worker(mock_port=mock_port)

        assert response.json() == {
            "status": "live",
            "runtime_id": "agent-runtime-harness",
        }
        assert harness.wait_for_completed(worker, "worker") == "completed"
        assert harness.wait_for_exit(worker) == 0
        reconciler = harness.start_reconciler(mock_port=mock_port)
        assert harness.wait_for_completed(reconciler, "reconciler") == "completed"
        assert harness.wait_for_exit(reconciler) == 0
