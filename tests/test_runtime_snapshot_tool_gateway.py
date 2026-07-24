from __future__ import annotations

import json
import logging
import socket
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.tool_security import tool_signature
from app.runtime.tool_gateway import BusinessConnector, ToolGateway
from app.schemas.agent_package import ToolManifest


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
        gateway.get_snapshot("c", "archive-1", "snapshot-1", "run-1", 0)


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


def test_gateway_signs_fixed_connector_request_without_logging_snapshot() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["runtime_id"] = request.headers["X-Agent-Runtime-Id"]
        captured["input"] = json.loads(request.content)["input"]
        captured["signature"] = request.headers["X-Agent-Signature"]
        captured["timestamp"] = request.headers["X-Agent-Timestamp"]
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
        "body": captured["body"],
    }


def test_gateway_publishes_complete_document_with_run_snapshot_and_epoch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("memory.publish_playback_document")
        assert json.loads(request.content) == {"input": {"archive_id": "a", "run_id": "r", "snapshot_id": "s", "generation_epoch": 2, "document": {"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []}}}
        return httpx.Response(200, json={"output": {"revision": 3, "content_digest": "digest"}})
    gateway = ToolGateway({"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")}, httpx.Client(transport=httpx.MockTransport(handler)))
    assert gateway.publish_playback_document("c", "a", "r", "s", 2, {"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []}) == {"revision": 3, "content_digest": "digest"}


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
        gateway.publish_playback_document("c", "a", "r", "s", 2, {"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []}, "write-1")
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
            "write-1",
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

    assert gateway.get_publish_result("c", "a", "r", "write-1") == {"revision": 1}
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
        assert json.loads(request.content) == {"input": {"archive_id": "a", "snapshot_id": "s", "run_id": "r", "generation_epoch": 2}}
        return httpx.Response(200, json={"output": {"snapshot_digest": "safe-digest"}})

    gateway = ToolGateway({"couple_diary_backend": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")}, httpx.Client(transport=httpx.MockTransport(handler)))
    manifest = ToolManifest(name="memory.get_snapshot", version="1.0.0", connector_id="couple_diary_backend", method="POST", relative_path="/api/v1/internal/agent-tools/memory.get_snapshot", input_from="input", output_to="snapshot")

    assert gateway.call(manifest, {"archive_id": "a", "snapshot_id": "s", "run_id": "r", "generation_epoch": 2, "input": {"archive_id": "forged"}}) == {"snapshot_digest": "safe-digest"}

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
        gateway.call(manifest, {"archive_id": "a", "snapshot_id": "s", "run_id": "r", "generation_epoch": 2})
    assert "13800138000" not in caplog.text
