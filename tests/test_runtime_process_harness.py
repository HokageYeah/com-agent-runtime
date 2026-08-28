import json
import socket
import sys
from os import dup
from pathlib import Path
from time import monotonic

import httpx
import pytest
from sqlalchemy import select, text

from app.models import AgentDefinition
from app.runtime import harness_bootstrap, harness_entry
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


def test_harness_process_config_carries_only_api_listener_fd(tmp_path: Path) -> None:
    """API 复用父进程预绑定 socket；文件描述符只允许进入 API 子进程。"""
    config = HarnessProcessConfig(
        sqlite_path=tmp_path / "runtime.db",
        port=12345,
        mock_port=12346,
        role="api",
        identity_id="test-client-123",
        timeout_seconds=1.0,
        socket_fd=9,
    )

    assert config.to_payload()["socket_fd"] == 9
    with pytest.raises(ValueError, match="TEST_HARNESS_CONFIG_INVALID"):
        HarnessProcessConfig(
            sqlite_path=tmp_path / "runtime.db",
            port=12345,
            mock_port=12346,
            role="worker",
            identity_id="test-client-123",
            timeout_seconds=1.0,
            socket_fd=9,
        )


def test_api_listener_uses_kernel_port_and_is_listening_before_handoff() -> None:
    """API 最终 socket 直接绑定系统端口，交接前已可接受连接。"""
    with ProcessHarness() as harness:
        listener = harness._create_api_listener()
        try:
            host, port = listener.getsockname()
            assert listener.family == socket.AF_INET
            assert host == "127.0.0.1"
            assert 1 <= port <= 65535
            assert listener.get_inheritable() is True
            with socket.create_connection((host, port), timeout=1):
                accepted, _ = listener.accept()
                accepted.close()
        finally:
            listener.close()


def test_bootstrap_import_failure_emits_only_safe_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """harness_entry 未导入时也必须输出可诊断且无敏感值的摘要。"""

    def _fail_import(_module_name: str) -> object:
        raise ImportError("private-value-must-not-escape")

    monkeypatch.setattr(harness_bootstrap.importlib, "import_module", _fail_import)

    with pytest.raises(SystemExit) as caught:
        harness_bootstrap.bootstrap_entry("api", tmp_path / "api.json")

    assert caught.value.code == 1
    assert json.loads(capsys.readouterr().err) == {
        "event": "harness_failed",
        "role": "api",
        "stage": "bootstrap",
        "error_type": "ImportError",
    }


def test_api_harness_preserves_inherited_ipv4_socket_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API 必须按 IPv4 传入预绑定 socket，不得被 Uvicorn 重包装为 Unix socket。"""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    expected_address = listener.getsockname()
    inherited_fd = dup(listener.fileno())
    observed: dict[str, object] = {}

    class _Server:
        def __init__(self, config: object) -> None:
            observed["config"] = config

        def run(self, *, sockets: list[socket.socket]) -> None:
            observed["family"] = sockets[0].family
            observed["address"] = sockets[0].getsockname()

    monkeypatch.setattr(harness_entry, "_HarnessApiServer", _Server)
    try:
        config = HarnessProcessConfig(
            sqlite_path=tmp_path / "runtime.db",
            port=12345,
            mock_port=12346,
            role="api",
            identity_id="test-client-123",
            timeout_seconds=1.0,
            socket_fd=inherited_fd,
        )
        harness_entry.serve_api(object(), config)
    finally:
        listener.close()

    assert observed["family"] == socket.AF_INET
    assert observed["address"] == expected_address


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


def test_health_timeout_reclaims_child_and_temporary_directory() -> None:
    """健康探针超时也必须走同一有限回收路径。"""
    try:
        port = _available_loopback_port()
    except PermissionError:
        pytest.skip("当前受限环境禁止绑定 loopback 端口")

    with pytest.raises(TimeoutError, match="TEST_HARNESS_HEALTH_TIMEOUT"):
        with ProcessHarness(timeout_seconds=1) as harness:
            path = harness.path
            child = harness.start(
                [sys.executable, "-c", "import time; time.sleep(30)"]
            )
            harness.wait_for_port(
                "127.0.0.1", port, timeout_seconds=0.05, process=child
            )

    assert child.poll() is not None
    assert not path.exists()


def test_process_harness_does_not_inherit_parent_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_TEST_SHOULD_NOT_ESCAPE", "private")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/test-python-runtime/lib")
    with ProcessHarness() as harness:
        child = harness.start(
            [
                sys.executable,
                "-c",
                "import os, sys; "
                "sys.exit(int('RUNTIME_TEST_SHOULD_NOT_ESCAPE' in os.environ or "
                "os.environ.get('LD_LIBRARY_PATH') != '/test-python-runtime/lib'))",
            ]
        )

        assert harness.wait_for_exit(child) == 0


def test_wait_for_port_reports_child_exit_without_waiting_for_health_timeout() -> None:
    """服务进程已退出时必须立即失败，不能继续误报为完整健康探针超时。"""
    try:
        port = _available_loopback_port()
    except PermissionError:
        pytest.skip("当前受限环境禁止绑定 loopback 端口")
    with ProcessHarness(timeout_seconds=5) as harness:
        child = harness.start([sys.executable, "-c", "raise SystemExit(17)"])
        started_at = monotonic()

        with pytest.raises(RuntimeError) as caught:
            harness.wait_for_port(
                "127.0.0.1", port, process=child, fallback_role="api"
            )

        assert monotonic() - started_at < 1
        assert str(caught.value) == (
            "TEST_HARNESS_PROCESS_EXITED:api:bootstrap:ProcessExit:17"
        )


def test_bootstrap_subprocess_import_failure_reaches_safe_parent_message() -> None:
    """真实 bootstrap 子进程导入失败时，父进程只收到固定摘要。"""
    with ProcessHarness(timeout_seconds=5) as harness:
        child = harness.start(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; "
                "from app.runtime import harness_bootstrap as bootstrap; "
                "bootstrap._ENTRY_MODULE='app.runtime.missing_harness_entry'; "
                "bootstrap.bootstrap_entry('api', Path('/unused'))",
            ],
            capture_stderr=True,
        )

        with pytest.raises(RuntimeError) as caught:
            harness.wait_for_port(
                "127.0.0.1", 12345, process=child, fallback_role="api"
            )

    assert str(caught.value) == (
        "TEST_HARNESS_PROCESS_EXITED:api:bootstrap:ModuleNotFoundError:1"
    )


def test_wait_for_port_reports_only_safe_structured_child_failure() -> None:
    """CI 只显示固定启动阶段和异常类型，不得透传子进程 stderr。"""
    with ProcessHarness(timeout_seconds=5) as harness:
        child = harness.start(
            [
                sys.executable,
                "-c",
                "import sys; "
                "sys.stderr.write('{\"event\":\"harness_failed\",\"role\":\"api\",'"
                "'\"stage\":\"api_server\",\"error_type\":\"SystemExit\"}\\n'); "
                "sys.stderr.write('private-value-must-not-escape\\n'); "
                "raise SystemExit(17)",
            ],
            capture_stderr=True,
        )

        with pytest.raises(RuntimeError) as caught:
            harness.wait_for_port("127.0.0.1", 12345, process=child)

    assert str(caught.value) == (
        "TEST_HARNESS_PROCESS_EXITED:api:api_server:SystemExit:17"
    )
    assert "private-value-must-not-escape" not in str(caught.value)


def test_wait_for_ready_treats_stdout_eof_as_safe_process_exit() -> None:
    """stdout EOF 代表子进程已关闭输出，必须转入安全退出诊断而非解析空 JSON。"""
    with ProcessHarness(timeout_seconds=5) as harness:
        child = harness.start(
            [
                sys.executable,
                "-c",
                "import sys; "
                "sys.stderr.write('{\"event\":\"harness_failed\",\"role\":\"api\",'"
                "'\"stage\":\"api_server\",\"error_type\":\"OSError\"}\\n'); "
                "sys.stderr.write('private-value-must-not-escape\\n'); "
                "raise SystemExit(1)",
            ],
            capture_stdout=True,
            capture_stderr=True,
        )

        with pytest.raises(RuntimeError) as caught:
            harness._wait_for_ready(child, "api")

    assert str(caught.value) == (
        "TEST_HARNESS_PROCESS_EXITED:api:api_server:OSError:1"
    )
    assert "private-value-must-not-escape" not in str(caught.value)


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
    except PermissionError:
        pytest.skip("当前受限环境禁止绑定 loopback 端口")
    with ProcessHarness(timeout_seconds=5) as harness:
        harness.start_mock_business(mock_port)
        # API 端口由最终 listening socket 用 bind(0) 原子分配。
        api_process, api_port = harness.start_api(mock_port=mock_port)
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
    assert api_process.stderr is not None
    api_stderr = api_process.stderr.read()
    assert harness.identity_id not in api_stderr
    assert "harness-only-" not in api_stderr
