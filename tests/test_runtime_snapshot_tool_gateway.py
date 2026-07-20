from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.core.tool_security import tool_signature
from app.runtime.tool_gateway import BusinessConnector, ToolGateway
from app.schemas.agent_package import ToolManifest


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
