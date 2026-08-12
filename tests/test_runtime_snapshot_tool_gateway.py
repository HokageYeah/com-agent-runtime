from __future__ import annotations

import json
import logging
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.core.tool_security import tool_signature
from app.db.sqlalchemy_db import Base
from app.models import AgentToolCall
from app.runtime.test_harness import LoopbackTestTransport, RuntimeHarnessConfig
from app.runtime.tool_gateway import BusinessConnector, ToolErrorRejected, ToolGateway
from app.schemas.agent_package import ToolManifest
from app.services.tool_call_audit_service import ToolCallAuditService


def _tool_context(archive_id: str, run_id: str) -> dict[str, str]:
    return {
        "agent_id": "memoir_agent", "agent_version": "1.0.1", "run_id": run_id,
        "step_id": "tool_step", "business_type": "couple_memory",
        "business_id": archive_id, "trace_id": "trace-1",
    }


@pytest.fixture(autouse=True)
def _resolve_mock_connector_to_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """MockTransport 的固定 connector 在测试中模拟为受控公网地址。"""
    original_getaddrinfo = socket.getaddrinfo

    def resolve(host: object, port: object, *args: object, **kwargs: object) -> object:
        if host == "business.local":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]
        return original_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1",
        "https://10.0.0.8",
        "https://169.254.169.254",
        "https://[fd00::8]",
        "https://localhost",
    ],
    ids=("loopback", "private", "link_local", "ipv6_private", "localhost"),
)
def test_gateway_rejects_unsafe_connector_endpoint_at_construction(base_url: str) -> None:
    """业务 connector 不得配置为本机、私网或链路本地 endpoint。"""
    with pytest.raises(ValueError, match="BUSINESS_CONNECTOR_ENDPOINT_UNSAFE"):
        ToolGateway(
            {"c": BusinessConnector(base_url, "agent-runtime", "dev", "secret")},
            httpx.Client(),
        )


def test_harness_explicitly_allows_loopback_mock_connector() -> None:
    """生产构造仍拒绝 loopback；只有测试 harness 可显式开启此例外。"""
    config = RuntimeHarnessConfig(
        session_factory=object(), trusted_clients={"test": {"keys": {"test": "random"}}},
        runtime_id="runtime-test", mock_base_url="http://127.0.0.1:8765",
    )
    gateway = ToolGateway(
        {"c": BusinessConnector("http://127.0.0.1:8765", "runtime-test", "test", "random")},
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))),
        test_transport=LoopbackTestTransport(config),
    )
    assert gateway is not None


def test_gateway_audits_connector_and_authorization_rejections_without_request_data() -> None:
    """拒绝审计只能收到 run ID 与固定码，不能携带 connector 配置或工具输入。"""
    events: list[tuple[str, str]] = []
    missing = ToolGateway(
        {}, httpx.Client(), audit_rejection=lambda run_id, code: events.append((run_id, code))
    )
    with pytest.raises(ValueError, match="BUSINESS_CONNECTOR_UNAVAILABLE"):
        missing.get_snapshot("missing", "archive-1", "snapshot-1", "run-1", 0)

    denied = ToolGateway(
        {"c": BusinessConnector("http://business.local", "runtime", "key", "secret")},
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"output": {}}))),
        authorization_permitted=lambda _run_id: False,
        audit_rejection=lambda run_id, code: events.append((run_id, code)),
    )
    with pytest.raises(ValueError, match="TOOL_AUTHORIZATION_REVOKED"):
        denied.get_snapshot("c", "archive-1", "snapshot-1", "run-2", 0)

    assert events == [
        ("run-1", "CONNECTOR_DISABLED"),
        ("run-2", "AUTHORIZATION_REVOKED"),
    ]


def test_gateway_audits_authorization_version_change_with_fixed_reason() -> None:
    events: list[tuple[str, str]] = []
    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "runtime", "key", "secret")},
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("must not send")
            )
        ),
        authorization_permitted=lambda _run_id: "AUTHORIZATION_VERSION_CHANGED",
        audit_rejection=lambda run_id, code: events.append((run_id, code)),
    )

    with pytest.raises(ValueError, match="TOOL_AUTHORIZATION_REVOKED"):
        gateway.get_snapshot("c", "archive-1", "snapshot-1", "run-1", 0)

    assert events == [("run-1", "AUTHORIZATION_VERSION_CHANGED")]


def test_gateway_rejects_connector_domain_resolved_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每次物理发送前必须拒绝重新解析到私网的 connector 域名。"""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))
        ],
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"output": {}})

    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValueError, match="BUSINESS_CONNECTOR_ENDPOINT_UNSAFE"):
        gateway.get_snapshot("c", "archive-1", "snapshot-1", "run-1", 0)
    assert calls == 0


def test_gateway_rejects_connected_peer_outside_preflight_dns_addresses() -> None:
    """连接后的实际对端不在本次 DNS 公网结果中时，响应不得进入 Runtime。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"output": {"snapshot_digest": "safe"}})

    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(handler)),
        peer_ip_provider=lambda: "8.8.4.4",
    )

    with pytest.raises(ValueError, match="BUSINESS_CONNECTOR_PEER_MISMATCH"):
        gateway.get_snapshot("c", "archive-1", "snapshot-1", "run-1", 0)
    assert calls == 1


def test_gateway_accepts_connected_peer_in_preflight_dns_addresses() -> None:
    """连接后的真实对端命中本次 DNS 公网结果时，允许安全响应进入 Runtime。"""
    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"output": {"snapshot_digest": "safe"}})
            )
        ),
        peer_ip_provider=lambda: "8.8.8.8",
    )

    assert gateway.get_snapshot("c", "archive-1", "snapshot-1", "run-1", 0) == {"snapshot_digest": "safe"}


def test_gateway_fails_closed_when_real_transport_has_no_peer_verifier() -> None:
    """真实 HTTP Transport 未注入 socket 对端读取器时，必须在发包前拒绝调用。"""
    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(),
    )

    with pytest.raises(ValueError, match="BUSINESS_CONNECTOR_PEER_UNVERIFIABLE"):
        gateway.get_snapshot("c", "archive-1", "snapshot-1", "run-1", 0, _tool_context("archive-1", "run-1"))


def test_gateway_rechecks_authorization_before_each_physical_send() -> None:
    """授权版本已撤销时不得向业务 connector 发送任何请求。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"output": {}})

    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(handler)),
        authorization_permitted=lambda run_id: run_id != "run-1",
    )

    with pytest.raises(ValueError, match="TOOL_AUTHORIZATION_REVOKED"):
        gateway.get_snapshot("c", "archive-1", "snapshot-1", "run-1", 0)
    assert calls == 0


def test_gateway_rechecks_execution_context_before_each_physical_send() -> None:
    """cancel、purge、失租或 Package 撤销后不得再触发任何工具请求。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"output": {}})

    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(handler)),
        execution_permitted=lambda _run_id: False,
    )

    with pytest.raises(ValueError, match="TOOL_EXECUTION_CONTEXT_INVALID"):
        gateway.get_snapshot("c", "archive-1", "snapshot-1", "run-1", 0)
    assert calls == 0


def test_gateway_signs_fixed_connector_request_without_logging_snapshot() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["runtime_id"] = request.headers["X-Agent-Client-Id"]
        captured["input"] = json.loads(request.content)["input"]
        captured["signature"] = request.headers["X-Agent-Signature"]
        captured["timestamp"] = request.headers["X-Agent-Timestamp"]
        captured["tool_attempt"] = request.headers.get("X-Agent-Tool-Attempt")
        captured["body"] = request.content
        return httpx.Response(200, json={"output": {"diaries": ["私密正文"]}})

    gateway = ToolGateway(
        {"couple_diary_backend": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert gateway.get_snapshot("couple_diary_backend", "archive-1", "snapshot-1", "run-1", 0) == {"diaries": ["私密正文"]}
    assert captured == {
        "url": "http://business.local/api/v1/internal/agent-tools/memory.get_snapshot",
        "runtime_id": "agent-runtime",
        "input": {
            "archive_id": "archive-1",
            "snapshot_id": "snapshot-1",
            "run_id": "run-1",
            "generation_epoch": 0,
        },
        "signature": tool_signature(
            "POST",
            "/api/v1/internal/agent-tools/memory.get_snapshot",
            str(captured["timestamp"]),
            captured["body"],
            "secret",
        ),
        "timestamp": captured["timestamp"],
        "tool_attempt": None,
        "body": captured["body"],
    }


def test_gateway_publishes_complete_document_with_run_snapshot_and_epoch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("memory.publish_playback_document")
        assert request.headers["X-Agent-Tool-Attempt"] == "7"
        # R3：请求体升级为冻结 ToolRequest 形状后必须同时包含 input 与 context 字段；
        # context 当前无冻结语义，置空，业务端 handler 仍只读 input 即向后兼容。
        assert json.loads(request.content) == {"input": {"archive_id": "a", "run_id": "r", "snapshot_id": "s", "generation_epoch": 2, "document": {"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []}}, "context": {}}
        return httpx.Response(200, json={"output": {"revision": 3, "content_digest": "digest"}})
    gateway = ToolGateway({"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")}, httpx.Client(transport=httpx.MockTransport(handler)))
    audit = AgentToolCall(tool_call_id="call-7", run_id="r", tool_attempt=7, side_effect=True)
    assert gateway.publish_playback_document("c", "a", "r", "s", 2, {"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []}, "write-1", audit) == {"revision": 3, "content_digest": "digest"}


def test_gateway_rejects_publish_without_authoritative_physical_attempt() -> None:
    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"output": {}}))),
    )

    with pytest.raises(ValueError, match="TOOL_ATTEMPT_REQUIRED"):
        gateway.publish_playback_document(
            "c", "a", "r", "s", 2,
            {"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []}, "write-1",
        )


def test_native_tool_uses_fixed_registry_and_persists_only_safe_summary() -> None:
    """Native 输入不进入审计，且不会被错误标记为业务副作用。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )

    result = gateway.call_native(
        "runtime.summarize_keys",
        {"value": {"private_body": "绝密正文"}, "max_items": 1},
        audit_service=ToolCallAuditService(session),
        run_id="run-1", execution_attempt=1, step_id="summarize",
        logical_key="run-1:summarize:1", request_digest="controlled-digest",
    )

    saved = session.scalar(select(AgentToolCall))
    assert result == {"keys": ["private_body"], "item_count": 1}
    assert saved is not None
    assert (saved.transport, saved.side_effect, saved.status) == ("native", False, "succeeded")
    assert saved.input_summary == {"operation": "runtime.summarize_keys"}
    assert saved.output_summary == {"keys": ["private_body"], "item_count": 1}
    assert "绝密正文" not in repr(saved.input_summary)
    assert "绝密正文" not in repr(saved.output_summary)


def test_native_tool_rejects_model_supplied_registration() -> None:
    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )

    with pytest.raises(ValueError, match="NATIVE_TOOL_NOT_ALLOWED"):
        gateway.call_native(
            "runtime.model_declared_tool", {}, audit_service=object(), run_id="run-1",
            execution_attempt=1, step_id="step", logical_key="key", request_digest="digest",
        )


def test_snapshot_gateway_retries_once_after_transport_failure_without_logging_body(caplog) -> None:
    """快照读取是幂等操作，可在连接失败后重试一次且日志不得包含私密正文。"""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.headers["X-Agent-Signature"]
        if attempts == 1:
            raise httpx.ConnectError("backend unavailable", request=request)
        return httpx.Response(200, json={"output": {"diaries": [{"text": "绝密日记正文"}]}})

    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with caplog.at_level(logging.INFO):
        assert gateway.get_snapshot("c", "archive-1", "snapshot-1", "run-1", 0) == {"diaries": [{"text": "绝密日记正文"}]}
    assert attempts == 2
    assert "绝密日记正文" not in caplog.text


def test_publish_gateway_does_not_retry_timeout_to_preserve_unknown_outcome() -> None:
    """写工具超时结果未知，必须交给已有幂等对账而非网关盲目重试。"""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timeout", request=request)

    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        gateway.publish_playback_document("c", "a", "r", "s", 2, {"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []}, "write-1", AgentToolCall(tool_call_id="call-timeout", run_id="r", tool_attempt=1, side_effect=True))
    except httpx.ReadTimeout:
        pass
    else:
        raise AssertionError("写工具超时必须透传给审计与对账流程")
    assert attempts == 1


def test_gateway_clamps_http_timeout_to_trusted_execution_window() -> None:
    """工具 HTTP timeout 不得超过 Run deadline 或有效 lease 的剩余窗口。"""
    observed_timeout: object | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {"snapshot_digest": "safe"}})

    class TimeoutRecordingClient(httpx.Client):
        def post(self, *args: object, **kwargs: object) -> httpx.Response:
            nonlocal observed_timeout
            observed_timeout = kwargs.get("timeout")
            return super().post(*args, **kwargs)

    now = datetime.now(UTC)
    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        TimeoutRecordingClient(transport=httpx.MockTransport(handler)),
        deadline_at=lambda: now + timedelta(seconds=2),
        lease_expires_at=lambda: now + timedelta(seconds=1),
    )

    assert gateway.get_snapshot("c", "a", "s", "r", 1) == {"snapshot_digest": "safe"}
    assert isinstance(observed_timeout, float)
    assert 0 < observed_timeout <= 1


def test_gateway_draining_rejects_new_write_without_sending_http() -> None:
    """draining 后不得发送新的发布副作用请求。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"output": {"revision": 1}})

    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(handler)),
        is_draining=lambda: True,
    )

    with pytest.raises(ValueError, match="TOOL_CALL_DRAINING"):
        gateway.publish_playback_document(
            "c", "a", "r", "s", 1,
            {"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []},
            "write-1", AgentToolCall(tool_call_id="call-drain", run_id="r", tool_attempt=1, side_effect=True),
        )
    assert calls == 0


def test_gateway_allows_query_after_commit_while_draining() -> None:
    """draining 时只允许按原幂等键查询已经提交写入的结果。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path.endswith("memory.get_publish_result")
        assert request.headers["Idempotency-Key"] == "write-1"
        return httpx.Response(200, json={"output": {"revision": 1}})

    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(handler)),
        is_draining=lambda: True,
    )

    assert gateway.get_publish_result("c", "a", "s", "r", 1, "write-1") == {"revision": 1}
    assert calls == 1


def test_generic_call_validates_fixed_cancellation_behavior() -> None:
    """Package 只能声明 Runtime 固定的工具取消语义。"""
    gateway = ToolGateway(
        {"couple_diary_backend": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(),
    )
    manifest = ToolManifest(
        name="memory.get_snapshot",
        version="1.0.0",
        connector_id="couple_diary_backend",
        method="POST",
        relative_path="/api/v1/internal/agent-tools/memory.get_snapshot",
        input_from="input",
        output_to="snapshot",
        cancellation_behavior="query_after_commit",
    )

    with pytest.raises(ValueError, match="TOOL_MANIFEST_NOT_ALLOWED"):
        gateway.call(
            manifest,
            {"archive_id": "a", "snapshot_id": "s", "run_id": "r", "generation_epoch": 1},
        )


def test_gateway_rejects_redirect_without_following_it() -> None:
    """固定工具不得跨重定向，以免签名请求被带往非受控地址。"""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(302, headers={"Location": "http://untrusted.local/steal"})

    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        gateway.get_snapshot("c", "archive-1", "snapshot-1", "run-1", 0)
    assert exc_info.value.response.status_code == 302
    assert attempts == 1


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://business.local",
        "http:///missing-host",
        "https://business.local?connector=forged",
        "https://business.local/#fragment",
    ],
)
def test_gateway_rejects_connector_url_outside_fixed_http_origin(base_url: str) -> None:
    """connector 必须是无查询和片段的 HTTP(S) 固定 origin。"""
    with pytest.raises(ValueError, match="BUSINESS_CONNECTOR_URL_INVALID"):
        ToolGateway(
            {"c": BusinessConnector(base_url, "agent-runtime", "dev", "secret")},
            httpx.Client(),
        )


def test_generic_call_only_accepts_fixed_manifest_and_trusted_context() -> None:
    """通用入口只能从可信运行上下文取引用，不能由 Package 改写 connector 或路径。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("memory.get_snapshot")
        assert json.loads(request.content) == {"input": {"archive_id": "a", "snapshot_id": "s", "run_id": "r", "generation_epoch": 2}, "context": _tool_context("a", "r")}
        return httpx.Response(200, json={"output": {"snapshot_digest": "safe-digest"}})

    gateway = ToolGateway({"couple_diary_backend": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")}, httpx.Client(transport=httpx.MockTransport(handler)))
    manifest = ToolManifest(name="memory.get_snapshot", version="1.0.0", connector_id="couple_diary_backend", method="POST", relative_path="/api/v1/internal/agent-tools/memory.get_snapshot", input_from="input", output_to="snapshot")

    assert gateway.call(manifest, {"archive_id": "a", "snapshot_id": "s", "run_id": "r", "generation_epoch": 2, "tool_context": _tool_context("a", "r"), "input": {"archive_id": "forged"}}) == {"snapshot_digest": "safe-digest"}

    forged = manifest.model_copy(update={"relative_path": "/internal/ssrf"})
    with pytest.raises(ValueError, match="TOOL_MANIFEST_NOT_ALLOWED"):
        gateway.call(forged, {"archive_id": "a", "snapshot_id": "s", "run_id": "r", "generation_epoch": 2})


def test_generic_call_rejects_sensitive_tool_output_without_logging_payload(caplog) -> None:
    """工具输出不得携带电话号码或身份证号等敏感标识符进入后续状态。"""
    gateway = ToolGateway(
        {"couple_diary_backend": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"output": {"phone": "13800138000"}}))),
    )
    manifest = ToolManifest(name="memory.get_snapshot", version="1.0.0", connector_id="couple_diary_backend", method="POST", relative_path="/api/v1/internal/agent-tools/memory.get_snapshot", input_from="input", output_to="snapshot")

    with caplog.at_level(logging.WARNING), pytest.raises(ValueError, match="TOOL_OUTPUT_SENSITIVE"):
        gateway.call(manifest, {"archive_id": "a", "snapshot_id": "s", "run_id": "r", "generation_epoch": 2, "tool_context": _tool_context("a", "r")})
    assert "13800138000" not in caplog.text


def test_disabled_tts_contract_is_rejected_before_network() -> None:
    """预留 TTS 契约即使被误调用，也必须在任何 DNS/HTTP 前拒绝。"""

    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("disabled tool must not send")

    gateway = ToolGateway(
        {
            "couple_diary_backend": BusinessConnector(
                "http://business.local", "agent-runtime", "dev", "secret"
            )
        },
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    manifest = ToolManifest(
        name="memory.enqueue_tts",
        version="1.0.0",
        connector_id="couple_diary_backend",
        method="POST",
        relative_path="/api/v1/internal/agent-tools/memory.enqueue_tts",
        input_from="media_tasks",
        output_to="media_tasks",
        side_effect=True,
        cancellation_behavior="query_after_commit",
        enabled=False,
    )

    with pytest.raises(ValueError, match="TOOL_CAPABILITY_DISABLED"):
        gateway.call(
            manifest,
            {
                "archive_id": "archive",
                "snapshot_id": "snapshot",
                "run_id": "run",
                "generation_epoch": 1,
                "media_tasks": [],
            },
            idempotency_key="stable-key",
        )


# ---------------------------------------------------------------------------
# R3：ToolRequest/ToolResult/ToolError 契约升级测试
# ---------------------------------------------------------------------------


def _make_default_gateway(handler: Callable[[httpx.Request], httpx.Response]) -> ToolGateway:
    """构造一个最小可用的 ToolGateway，供契约升级测试复用。"""
    return ToolGateway(
        {"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_request_body_carries_frozen_tool_request_shape_with_empty_context() -> None:
    """R3：请求体必须升级为冻结 ToolRequest 形状，含 input 与 context 两个字段。

    context 当前无冻结语义，统一置空；后续冻结语义后再扩展，禁止业务端猜测字段。
    """
    captured_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(200, json={"output": {"snapshot_digest": "safe"}})

    gateway = _make_default_gateway(handler)
    assert gateway.get_snapshot("c", "archive-1", "snapshot-1", "run-1", 0) == {"snapshot_digest": "safe"}

    # 请求体形状必须严格是 {input, context}；context 必须存在且为空 dict。
    assert set(captured_body) == {"input", "context"}
    assert captured_body["context"] == {}
    assert captured_body["input"] == {
        "archive_id": "archive-1",
        "snapshot_id": "snapshot-1",
        "run_id": "run-1",
        "generation_epoch": 0,
    }


def test_response_with_current_schema_version_is_accepted() -> None:
    """R3：响应显式声明 schema_version='1.0.0' 视为匹配当前协议版本，正常返回 output。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"output": {"snapshot_digest": "safe"}, "schema_version": "1.0.0"},
        )

    gateway = _make_default_gateway(handler)
    assert gateway.get_snapshot("c", "a", "s", "r", 0) == {"snapshot_digest": "safe"}


def test_response_with_unsupported_schema_version_is_rejected() -> None:
    """R3：响应声明非 '1.0.0' 的 schema_version 必须按受控错误码拒绝，避免业务端单方升级协议。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"output": {"snapshot_digest": "safe"}, "schema_version": "2.0.0"},
        )

    gateway = _make_default_gateway(handler)
    with pytest.raises(ValueError, match="TOOL_OUTPUT_SCHEMA_VERSION_INVALID"):
        gateway.get_snapshot("c", "a", "s", "r", 0)


def test_response_with_missing_schema_version_falls_back_to_default_and_is_accepted() -> None:
    """R3：缺失 schema_version 时走 ToolResult 默认值 '1.0.0'，视为匹配，避免过度拒绝历史业务响应。

    向后兼容要点：本地 handler 历史响应已含 schema_version='1.0.0'；若未来某个新 handler
    误删该字段，默认值兜底防止 Runtime 把合法响应误判为契约破坏。
    """

    def handler(_: httpx.Request) -> httpx.Response:
        # 故意不返回 schema_version；ToolResult 默认 '1.0.0' 应当兜底。
        return httpx.Response(200, json={"output": {"snapshot_digest": "safe"}})

    gateway = _make_default_gateway(handler)
    assert gateway.get_snapshot("c", "a", "s", "r", 0) == {"snapshot_digest": "safe"}


def test_local_handler_shape_with_schema_version_is_backward_compatible() -> None:
    """R3：本地 memory_tools_api 响应形状 {output, schema_version='1.0.0'} 必须仍可解析。

    保护现有资产：runtime 仓内 memory_tools_api 已固定该形状，升级响应解析后必须
    继续兼容，不应触发契约错误。
    """

    def handler(_: httpx.Request) -> httpx.Response:
        # 与 app/api/endpoints/memory_tools_api.py 现有响应形状一致。
        return httpx.Response(
            200,
            json={"output": {"revision": 9, "content_digest": "abc"}, "schema_version": "1.0.0"},
        )

    gateway = _make_default_gateway(handler)
    assert gateway.get_publish_result("c", "a", "s", "r", 1, "write-1") == {
        "revision": 9,
        "content_digest": "abc",
    }


# ---------------------------------------------------------------------------
# R3 补充：业务上下文 header / 内层 schema_version / ToolError fail closed
# ---------------------------------------------------------------------------


def test_request_adds_business_context_headers_without_entering_signature_base() -> None:
    """R3 补充：请求新增 X-Agent-Run-Id / X-Agent-Tool-Name 两个业务上下文 header。

    这两个 header 辅助业务端定位 Run/Tool，但不参与 HMAC 签名原文；签名仍是
    METHOD\\npath\\ntimestamp\\nbody_sha256。已有签名 header 集合保持不变。
    """
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # httpx 大小写不敏感读取 header，按混合大小写名取值。
        captured["run_id"] = request.headers["X-Agent-Run-Id"]
        captured["tool_name"] = request.headers["X-Agent-Tool-Name"]
        captured["timestamp"] = request.headers["X-Agent-Timestamp"]
        captured["signature"] = request.headers["X-Agent-Signature"]
        captured["body"] = request.content
        return httpx.Response(200, json={"output": {"snapshot_digest": "safe"}})

    gateway = _make_default_gateway(handler)
    assert gateway.get_snapshot("c", "archive-1", "snapshot-1", "run-ctx-9", 0) == {"snapshot_digest": "safe"}

    # 业务上下文 header 必须存在并取自 runtime context / 当前 tool 名。
    assert captured["run_id"] == "run-ctx-9"
    assert captured["tool_name"] == "memory.get_snapshot"
    # 签名原文不变：仍由 method/path/timestamp/body 派生，与未加新 header 时一致。
    expected_sig = tool_signature(
        "POST",
        "/api/v1/internal/agent-tools/memory.get_snapshot",
        captured["timestamp"],
        captured["body"],
        "secret",
    )
    assert captured["signature"] == expected_sig


def test_response_output_with_inner_schema_version_equal_to_current_is_accepted() -> None:
    """R3 补充：output 自带内层 schema_version='1.0.0' 时（如 Snapshot）必须独立校验通过。

    Snapshot/Archive 由业务端序列化时自带 schema_version 字段，与外层 ToolResult 信封
    的 schema_version 是两层不同语义，必须各自对齐当前协议版本。
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                # 外层 ToolResult.schema_version
                "schema_version": "1.0.0",
                # 内层 output 自带 schema_version（Snapshot 形态）
                "output": {
                    "schema_version": "1.0.0",
                    "source_range": {"relationship_id": "r-1"},
                    "diary_items": [],
                },
            },
        )

    gateway = _make_default_gateway(handler)
    result = gateway.get_snapshot("c", "a", "s", "r", 0)
    assert result["schema_version"] == "1.0.0"


def test_response_output_with_inner_schema_version_mismatch_is_rejected() -> None:
    """R3 补充：output 内层 schema_version 与当前协议不一致时必须按受控码拒绝。

    防止业务端单方升级 Snapshot 内层 schema 但 Runtime 仍当 1.0.0 消费导致状态污染。
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0.0",
                "output": {"schema_version": "2.0.0", "diary_items": []},
            },
        )

    gateway = _make_default_gateway(handler)
    with pytest.raises(ValueError, match="TOOL_OUTPUT_SCHEMA_VERSION_INVALID"):
        gateway.get_snapshot("c", "a", "s", "r", 0)


def test_response_output_without_inner_schema_version_still_accepted() -> None:
    """R3 补充：output 不含内层 schema_version 字段（如 publish 结果）时不触发内层校验。"""

    def handler(_: httpx.Request) -> httpx.Response:
        # publish 结果形状：revision + content_digest，不含 schema_version 字段。
        return httpx.Response(
            200,
            json={"output": {"revision": 4, "content_digest": "abc"}, "schema_version": "1.0.0"},
        )

    gateway = _make_default_gateway(handler)
    assert gateway.get_publish_result("c", "a", "s", "r", 1, "write-1") == {"revision": 4, "content_digest": "abc"}


def test_legacy_publish_result_call_preserves_head_two_field_wire_shape() -> None:
    """HEAD v1.0.0 caller form continues to send only archive_id + run_id."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["input"] == {"archive_id": "a", "run_id": "r"}
        return httpx.Response(200, json={"output": {"revision": 4}})

    gateway = _make_default_gateway(handler)
    assert gateway.get_publish_result("c", "a", "r", "write-1") == {"revision": 4}


def test_head_four_field_tool_error_defaults_visibility_to_false() -> None:
    """Consumer accepts the valid ToolError form emitted by HEAD v1.0.0."""

    gateway = _make_default_gateway(
        lambda _: httpx.Response(
            404,
            json={
                "error_code": "PUBLISH_NOT_YET_OBSERVED",
                "error_type": "publish_not_observed",
                "retryable": False,
                "safe_message": "尚未观察到发布结果",
            },
        )
    )
    legacy_context = {**_tool_context("a", "r"), "agent_version": "1.0.0"}
    assert gateway.get_publish_result(
        "c", "a", "r", "write-1", tool_context=legacy_context
    ) is None


def test_tool_wire_version_is_selected_from_trusted_agent_run_identity() -> None:
    """历史 1.0.0 Run 继续请求 v1，1.0.1 新 Run 显式请求 v1.1。"""

    versions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        version = request.headers["X-Agent-Tool-Contract-Version"]
        versions.append(version)
        error: dict[str, object] = {
            "error_code": "PUBLISH_NOT_YET_OBSERVED",
            "error_type": "publish_not_observed",
            "retryable": False,
            "safe_message": "尚未观察到发布结果",
        }
        if version == "1.1.0":
            error["details_visible_to_model"] = False
        return httpx.Response(404, json=error)

    gateway = _make_default_gateway(handler)
    legacy_context = {**_tool_context("a", "r"), "agent_version": "1.0.0"}
    assert gateway.get_publish_result(
        "c", "a", "r", "write-1", tool_context=legacy_context
    ) is None
    assert gateway.get_publish_result(
        "c", "a", "r", "write-2", tool_context=_tool_context("a", "r")
    ) is None
    assert versions == ["1.0.0", "1.1.0"]


def test_non_2xx_response_with_tool_error_claiming_model_visibility_fails_closed() -> None:
    """R3 补充：非 2xx 响应 body 自称 ToolError 但声明 details_visible_to_model=true 必须 fail closed。

    冻结 ToolError 铁律：details_visible_to_model 默认且必须为 False；任何反向声明都是
    试图把业务错误详情灌入模型上下文的攻击面，一律按 TOOL_ERROR_SHAPE_INVALID 拒绝。
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "error_code": "BUSINESS_INTERNAL",
                "error_type": "ServerError",
                "retryable": True,
                "safe_message": "ok",
                # 攻击面：试图让 Runtime 把错误详情送给模型。
                "details_visible_to_model": True,
            },
        )

    gateway = _make_default_gateway(handler)
    with pytest.raises(ValueError, match="TOOL_ERROR_SHAPE_INVALID"):
        gateway.get_snapshot("c", "a", "s", "r", 0)


def test_non_2xx_with_fastapi_detail_shape_fails_closed() -> None:
    """任何 FastAPI detail 形状都不是 Internal Tool 的合法错误合同。"""
    # 形如 FastAPI HTTPException 默认响应：{"detail": "..."}，不是 ToolError 形状。
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "IDEMPOTENCY_CONFLICT"})

    gateway = _make_default_gateway(handler)
    with pytest.raises(ValueError, match="TOOL_ERROR_SHAPE_INVALID"):
        gateway.get_publish_result("c", "a", "s", "r", 1, "write-1")


def test_non_2xx_response_with_tool_error_unknown_code_fails_closed() -> None:
    """P3：非 2xx 响应自称 ToolError 但 error_code 不在冻结 allowlist 中必须 fail closed。

    冻结铁律：业务端只能返回 Runtime 冻结的 error_code；未知码意味着契约单方漂移或
    注入攻击面，一律按 TOOL_ERROR_CODE_UNKNOWN 拒绝，绝不透传 body 内容给模型上下文。
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                # 不在冻结 error_code 矩阵中的未知码。
                "error_code": "BUSINESS_TOTALLY_UNKNOWN",
                "error_type": "Forbidden",
                "retryable": False,
                "safe_message": "ok",
                "details_visible_to_model": False,
            },
        )

    gateway = _make_default_gateway(handler)
    with pytest.raises(ValueError, match="TOOL_ERROR_CODE_UNKNOWN"):
        gateway.get_snapshot("c", "a", "s", "r", 0)


def test_non_2xx_response_with_tool_error_http_status_contradiction_fails_closed() -> None:
    """P3：error_code 已知但 HTTP 状态码与冻结矩阵不符必须 fail closed。

    IDEMPOTENCY_CONFLICT 冻结为 409；若以 403 返回，状态码与码语义矛盾，意味着业务端
    误用或中间件篡改，一律按 TOOL_ERROR_CONTRADICTION 拒绝。
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,  # 矩阵冻结 IDEMPOTENCY_CONFLICT=409，409≠403 → 矛盾。
            json={
                "error_code": "IDEMPOTENCY_CONFLICT",
                "error_type": "Conflict",
                "retryable": False,
                "safe_message": "ok",
                "details_visible_to_model": False,
            },
        )

    gateway = _make_default_gateway(handler)
    with pytest.raises(ValueError, match="TOOL_ERROR_CONTRADICTION"):
        gateway.get_snapshot("c", "a", "s", "r", 0)


def test_non_2xx_response_with_tool_error_retryable_contradiction_fails_closed() -> None:
    """P3：error_code 已知、HTTP 状态码一致但 retryable 与状态码语义矛盾必须 fail closed。

    冻结规则：retryable = (http_status >= 500)，与 runner.py HTTPStatusError 处理一致。
    MEMORY_SNAPSHOT_UNAVAILABLE 冻结为 403（<500 → retryable=False）；若 body 声明
    retryable=True 则语义矛盾，按 TOOL_ERROR_CONTRADICTION 拒绝，防止业务端单方放宽重试。
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,  # 矩阵冻结 MEMORY_SNAPSHOT_UNAVAILABLE=403，<500。
            json={
                "error_code": "MEMORY_SNAPSHOT_UNAVAILABLE",
                "error_type": "Forbidden",
                "retryable": True,  # 403<500 应为 False，True → 矛盾。
                "safe_message": "ok",
                "details_visible_to_model": False,
            },
        )

    gateway = _make_default_gateway(handler)
    with pytest.raises(ValueError, match="TOOL_ERROR_CONTRADICTION"):
        gateway.get_snapshot("c", "a", "s", "r", 0)


def test_non_2xx_response_with_known_consistent_tool_error_drives_typed_control_flow() -> None:
    """合法 ToolError 也必须进入受控 consumer 行为，不能退回裸 HTTP 分支。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,  # 矩阵冻结 MEMORY_SNAPSHOT_UNAVAILABLE=403。
            json={
                "error_code": "MEMORY_SNAPSHOT_UNAVAILABLE",
                "error_type": "snapshot_unavailable",
                "retryable": False,  # 403<500 → 一致。
                "safe_message": "回忆快照当前不可读取",
                "details_visible_to_model": False,
            },
        )

    gateway = _make_default_gateway(handler)
    with pytest.raises(ToolErrorRejected) as exc_info:
        gateway.get_snapshot("c", "a", "s", "r", 0)
    assert (exc_info.value.error_code, exc_info.value.retryable) == ("MEMORY_SNAPSHOT_UNAVAILABLE", False)


@pytest.mark.parametrize("body", [
    b"not-json",
    {"error_code": "MEMORY_SNAPSHOT_UNAVAILABLE"},
    {"error_code": "MEMORY_SNAPSHOT_UNAVAILABLE", "error_type": "snapshot_unavailable", "retryable": False, "safe_message": "回忆快照当前不可读取", "details_visible_to_model": False, "extra": "no"},
    {"error_code": "MEMORY_SNAPSHOT_UNAVAILABLE", "error_type": "snapshot_unavailable", "retryable": "false", "safe_message": "回忆快照当前不可读取", "details_visible_to_model": False},
    {"error_code": "MEMORY_SNAPSHOT_UNAVAILABLE", "error_type": "snapshot_unavailable", "retryable": 0, "safe_message": "回忆快照当前不可读取", "details_visible_to_model": False},
])
def test_non_2xx_tool_error_rejects_non_json_missing_extra_and_non_boolean_primitives(body: object) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        if isinstance(body, bytes):
            return httpx.Response(403, content=body)
        return httpx.Response(403, json=body)

    with pytest.raises(ValueError, match="TOOL_ERROR_SHAPE_INVALID"):
        _make_default_gateway(handler).get_snapshot("c", "a", "s", "r", 1)


@pytest.mark.parametrize(
    ("code", "status", "error_type", "retryable", "safe_message"),
    [
        ("IDEMPOTENCY_CONFLICT", 409, "idempotency_conflict", False, "请求与既有幂等操作冲突"),
        ("GENERATION_SUPERSEDED", 409, "generation_superseded", False, "当前生成已被更新版本取代"),
        ("AUTHORIZATION_REVOKED", 403, "authorization_revoked", False, "该运行授权已失效"),
        ("BUSINESS_DATA_INVALID", 422, "business_data_invalid", False, "业务数据不满足工具要求"),
        ("MEMORY_SNAPSHOT_UNAVAILABLE", 403, "snapshot_unavailable", False, "回忆快照当前不可读取"),
        ("MEMORY_RUN_NOT_ACTIVE", 409, "run_not_active", False, "该回忆录运行当前不可执行"),
        ("MEMORY_DOCUMENT_INVALID", 422, "document_invalid", False, "播放文档不满足发布要求"),
        ("PUBLISH_NOT_YET_OBSERVED", 404, "publish_not_observed", False, "尚未观察到发布结果"),
        ("RUNTIME_SERVICE_UNAVAILABLE", 503, "service_unavailable", True, "业务工具服务暂时不可用"),
    ],
)
def test_tool_error_matrix_is_strict_and_preserves_only_safe_control_data(
    code: str, status: int, error_type: str, retryable: bool, safe_message: str,
) -> None:
    gateway = _make_default_gateway(lambda _: httpx.Response(status, json={
        "error_code": code, "error_type": error_type, "retryable": retryable,
        "safe_message": safe_message, "details_visible_to_model": False,
    }))
    if code == "PUBLISH_NOT_YET_OBSERVED":
        assert gateway.get_publish_result("c", "a", "s", "r", 1, "write-1") is None
    else:
        with pytest.raises(ToolErrorRejected) as exc_info:
            gateway.get_snapshot("c", "a", "s", "r", 0)
        assert (exc_info.value.error_code, exc_info.value.retryable) == (code, retryable)


def test_real_http_tool_rejects_missing_context_before_any_send() -> None:
    calls = 0

    class CountingTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"output": {}})

    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "runtime", "key", "secret")},
        httpx.Client(transport=CountingTransport()),
    )
    with pytest.raises(ValueError, match="TOOL_CONTEXT_INVALID"):
        gateway.get_snapshot("c", "a", "s", "r", 1)
    assert calls == 0


@pytest.mark.parametrize("context", [
    {},
    {"agent_id": "memoir_agent"},
    {**_tool_context("a", "r"), "extra": "no"},
    {**_tool_context("a", "r"), "step_id": ""},
    {**_tool_context("a", "r"), "trace_id": 1},  # type: ignore[dict-item]
    {**_tool_context("a", "r"), "run_id": "other"},
    {**_tool_context("a", "r"), "business_id": "other"},
    {**_tool_context("a", "r"), "agent_id": "other_agent"},
    {**_tool_context("a", "r"), "agent_version": "untrusted"},
    {**_tool_context("a", "r"), "business_type": "other"},
])
def test_real_http_tool_rejects_untrusted_context_before_any_send(context: object) -> None:
    gateway = ToolGateway(
        {"c": BusinessConnector("http://business.local", "runtime", "key", "secret")},
        httpx.Client(),
    )
    with pytest.raises(ValueError, match="TOOL_CONTEXT"):
        gateway.get_snapshot("c", "a", "s", "r", 1, context)  # type: ignore[arg-type]
