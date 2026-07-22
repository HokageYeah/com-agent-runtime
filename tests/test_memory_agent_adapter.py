"""回忆录 Runtime HTTP adapter 仅传输资源标识与安全摘要。"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from app.services.memory_agent_adapter import (
    MemoryAgentAdapter,
    MemoryRuntimeAdapterError,
    MemoryRuntimeClientConfig,
)


def test_adapter_checks_capabilities_then_creates_and_starts_held_run() -> None:
    """create/start 复用调用方给定幂等键，输入中不包含回忆录正文。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        body = bytes(request.content)
        timestamp = request.headers["X-Agent-Timestamp"]
        canonical = f"{request.method}\n{request.url.path}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}"
        assert hmac.compare_digest(
            request.headers["X-Agent-Signature"],
            hmac.new(b"secret", canonical.encode(), hashlib.sha256).hexdigest(),
        )
        if request.url.path.endswith("/health/ready"):
            return httpx.Response(200, json={"status": "ready"}, request=request)
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json={
                "contract_version": "1.0.0", "package_digest": "sha256:memoir",
                "agents": [{"agent_id": "memoir_agent", "version": "1.0.0"}],
                "model_policies": ["emotional_writing", "strict"],
                "capabilities": {"workflow_agent": True},
            }, request=request)
        if request.url.path.endswith("/agent-runs"):
            assert request.headers["Idempotency-Key"] == "create:a:2"
            assert json.loads(body) == {"agent_id": "memoir_agent", "agent_version": "1.0.0", "business_type": "couple_memory", "business_id": "a", "start_mode": "held", "input": {"archive_id": "a", "snapshot_id": "s", "generation_epoch": 2}, "callback_target_id": "memory_callback", "business_connector_id": "couple_diary_backend", "data_domain": "couple_memory"}
            return httpx.Response(201, json={"run_id": "run-1", "contract_version": "1.0.0", "package_digest": "sha256:memoir", "authorization_version": 1}, request=request)
        assert request.headers["Idempotency-Key"] == "start:a:2"
        return httpx.Response(200, json={"run_id": "run-1"}, request=request)

    adapter = MemoryAgentAdapter(
        MemoryRuntimeClientConfig("http://runtime.local", "couple-diary", "dev", "secret"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    held = adapter.create_held(archive_id="a", snapshot_id="s", generation_epoch=2, idempotency_key="create:a:2")
    adapter.start_held(run_id=held.run_id, idempotency_key="start:a:2")

    assert held.run_id == "run-1"
    assert calls == ["/api/v1/runtime/health/ready", "/api/v1/runtime/capabilities", "/api/v1/runtime/agent-runs", "/api/v1/runtime/agent-runs/run-1/start"]


def test_adapter_refreshes_handshake_when_capability_digest_changes_before_ttl() -> None:
    """运行中部署新包时，第二次 create 不能继续信任旧 TTL 缓存。"""
    calls: list[str] = []
    digests = iter(["sha256:old", "sha256:new", "sha256:new"])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/health/ready"):
            return httpx.Response(200, json={"status": "ready"}, request=request)
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json={
                "contract_version": "1.0.0", "package_digest": next(digests),
                "agents": [{"agent_id": "memoir_agent", "version": "1.0.0"}],
                "model_policies": ["emotional_writing", "strict"],
                "capabilities": {"workflow_agent": True},
            }, request=request)
        return httpx.Response(201, json={
            "run_id": "run-1", "contract_version": "1.0.0",
            "package_digest": "sha256:new", "authorization_version": 1,
        }, request=request)

    adapter = MemoryAgentAdapter(
        MemoryRuntimeClientConfig("http://runtime.local", "couple-diary", "dev", "secret"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    adapter.create_held(archive_id="a", snapshot_id="s", generation_epoch=1, idempotency_key="create:a:1")
    adapter.create_held(archive_id="b", snapshot_id="s", generation_epoch=1, idempotency_key="create:b:1")

    assert calls == [
        "/api/v1/runtime/health/ready", "/api/v1/runtime/capabilities", "/api/v1/runtime/agent-runs",
        "/api/v1/runtime/capabilities", "/api/v1/runtime/health/ready", "/api/v1/runtime/capabilities", "/api/v1/runtime/agent-runs",
    ]


def test_adapter_rejects_capabilities_without_package_digest() -> None:
    """缺少可比较包摘要时不能伪造固定值继续创建 held Run。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health/ready"):
            return httpx.Response(200, json={"status": "ready"}, request=request)
        return httpx.Response(200, json={
            "contract_version": "1.0.0",
            "agents": [{"agent_id": "memoir_agent", "version": "1.0.0"}],
            "model_policies": ["emotional_writing", "strict"],
            "capabilities": {"workflow_agent": True},
        }, request=request)

    adapter = MemoryAgentAdapter(
        MemoryRuntimeClientConfig("http://runtime.local", "couple-diary", "dev", "secret"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(MemoryRuntimeAdapterError, match="MEMORY_RUNTIME_CAPABILITY_INCOMPATIBLE"):
        adapter.create_held(archive_id="a", snapshot_id="s", generation_epoch=1, idempotency_key="create:a:1")


def test_adapter_queries_or_cancels_held_run_without_reading_error_body() -> None:
    """孤儿补偿只能读取 Run 安全摘要，并以独立稳定键请求取消。"""
    calls: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = bytes(request.content)
        timestamp = request.headers["X-Agent-Timestamp"]
        canonical = f"{request.method}\n{request.url.path}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}"
        assert hmac.compare_digest(
            request.headers["X-Agent-Signature"],
            hmac.new(b"secret", canonical.encode(), hashlib.sha256).hexdigest(),
        )
        calls.append((request.method, request.url.path, request.headers.get("Idempotency-Key")))
        if request.method == "GET" and request.url.path.endswith("/run-1"):
            return httpx.Response(200, json={
                "run_id": "run-1", "status": "running", "dispatch_state": "held",
                "contract_version": "1.0.0", "package_digest": "sha256:memoir",
                "authorization_version": 1,
            }, request=request)
        if request.method == "GET":
            # 错误正文模拟私密或不可信内容；adapter 不得读取或持久化它。
            return httpx.Response(404, content=b"private upstream error", request=request)
        assert json.loads(body) == {"reason_code": "MEMORY_BINDING_SUPERSEDED"}
        return httpx.Response(200, json={"run_id": "run-1"}, request=request)

    adapter = MemoryAgentAdapter(
        MemoryRuntimeClientConfig("http://runtime.local", "couple-diary", "dev", "secret"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    summary = adapter.get_run_summary("run-1")
    assert summary is not None and summary.run_id == "run-1"
    assert adapter.get_run_summary("missing") is None
    adapter.cancel_run("run-1", "cancel:a:2")
    assert calls == [
        ("GET", "/api/v1/runtime/agent-runs/run-1", None),
        ("GET", "/api/v1/runtime/agent-runs/missing", None),
        ("POST", "/api/v1/runtime/agent-runs/run-1/cancel", "cancel:a:2"),
    ]


def test_adapter_requests_private_purge_and_reads_only_privacy_state() -> None:
    """隐私删除使用调用方给定稳定键，查询只接收 Runtime 的安全状态摘要。"""
    calls: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("Idempotency-Key")))
        if request.method == "POST":
            assert json.loads(bytes(request.content)) == {}
            return httpx.Response(
                202,
                json={"run_id": "run-1", "privacy_state": "purge_requested"},
                request=request,
            )
        return httpx.Response(
            200,
            json={"run_id": "run-1", "privacy_state": "purged"},
            request=request,
        )

    adapter = MemoryAgentAdapter(
        MemoryRuntimeClientConfig("http://runtime.local", "couple-diary", "dev", "secret"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    adapter.request_private_purge("run-1", "memory:purge:a:run-1:3")
    assert adapter.get_privacy_state("run-1") == "purged"
    assert calls == [
        ("POST", "/api/v1/runtime/agent-runs/run-1/purge-private-data", "memory:purge:a:run-1:3"),
        ("GET", "/api/v1/runtime/agent-runs/run-1", None),
    ]


def test_adapter_retries_run_with_caller_stable_key() -> None:
    """用户侧 retry 只转交既有 Run 与稳定键，不传 checkpoint 或业务正文。"""
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["key"] = request.headers.get("Idempotency-Key")
        observed["body"] = bytes(request.content)
        return httpx.Response(200, json={"run_id": "run-1"}, request=request)

    adapter = MemoryAgentAdapter(
        MemoryRuntimeClientConfig("http://runtime.local", "couple-diary", "dev", "secret"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    adapter.retry_run("run-1", "memory-retry:archive-1:run-1")

    assert observed == {
        "path": "/api/v1/runtime/agent-runs/run-1/retry",
        "key": "memory-retry:archive-1:run-1",
        "body": b"{}",
    }
