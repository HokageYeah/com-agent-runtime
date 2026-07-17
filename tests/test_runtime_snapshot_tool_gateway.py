from __future__ import annotations

import json

import httpx

from app.runtime.tool_gateway import BusinessConnector, ToolGateway


def test_gateway_signs_fixed_connector_request_without_logging_snapshot() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["runtime_id"] = request.headers["X-Agent-Runtime-Id"]
        return httpx.Response(200, json={"output": {"diaries": ["私密正文"]}})

    gateway = ToolGateway(
        {"couple_diary_backend": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")},
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert gateway.get_snapshot("couple_diary_backend", "archive-1", "snapshot-1") == {"diaries": ["私密正文"]}
    assert captured == {"url": "http://business.local/api/v1/internal/agent-tools/memory.get_snapshot", "runtime_id": "agent-runtime"}


def test_gateway_publishes_complete_document_with_run_snapshot_and_epoch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("memory.publish_playback_document")
        assert json.loads(request.content) == {"input": {"archive_id": "a", "run_id": "r", "snapshot_id": "s", "generation_epoch": 2, "document": {"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []}}}
        return httpx.Response(200, json={"output": {"revision": 3, "content_digest": "digest"}})
    gateway = ToolGateway({"c": BusinessConnector("http://business.local", "agent-runtime", "dev", "secret")}, httpx.Client(transport=httpx.MockTransport(handler)))
    assert gateway.publish_playback_document("c", "a", "r", "s", 2, {"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []}) == {"revision": 3, "content_digest": "digest"}
