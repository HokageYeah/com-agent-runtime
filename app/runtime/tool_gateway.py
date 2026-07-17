"""Runtime 访问业务 HTTP Tool 的固定 connector 网关。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.tool_security import tool_signature


@dataclass(frozen=True)
class BusinessConnector:
    """仅由服务端配置构造，AgentPackage 不能提供 base_url 或密钥。"""

    base_url: str
    runtime_id: str
    key_id: str
    secret: str


class ToolGateway:
    """第一版只开放固定的只读快照工具，禁止调用方拼接 URL。"""

    def __init__(self, connectors: dict[str, BusinessConnector], client: httpx.Client) -> None:
        self._connectors, self._client = connectors, client

    def get_snapshot(self, connector_id: str, archive_id: str, snapshot_id: str) -> dict[str, Any]:
        connector = self._connectors.get(connector_id)
        if connector is None:
            raise ValueError("BUSINESS_CONNECTOR_UNAVAILABLE")
        path = "/api/v1/internal/agent-tools/memory.get_snapshot"
        body = {"input": {"archive_id": archive_id, "snapshot_id": snapshot_id}}
        content = httpx.Request("POST", "http://tool.local", json=body).content
        timestamp = str(int(time.time()))
        headers = {
            "X-Agent-Runtime-Id": connector.runtime_id,
            "X-Agent-Key-Id": connector.key_id,
            "X-Agent-Timestamp": timestamp,
            "X-Agent-Signature": tool_signature("POST", path, timestamp, content, connector.secret),
        }
        response = self._client.post(f"{connector.base_url.rstrip('/')}{path}", content=content, headers=headers, timeout=10.0)
        response.raise_for_status()
        output = response.json().get("output")
        if not isinstance(output, dict):
            raise ValueError("TOOL_OUTPUT_INVALID")
        logging.info("HTTP Business Tool 成功 tool=memory.get_snapshot connector=%s archive_id=%s", connector_id, archive_id)
        return output

    def publish_playback_document(
        self, connector_id: str, archive_id: str, run_id: str, snapshot_id: str, generation_epoch: int,
        document: dict[str, Any], idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """发布完整作品；调用方只能传受信任 run 上下文与已校验作品结构。"""
        return self._call(
            connector_id,
            "/api/v1/internal/agent-tools/memory.publish_playback_document",
            {"archive_id": archive_id, "run_id": run_id, "snapshot_id": snapshot_id, "generation_epoch": generation_epoch, "document": document},
            "memory.publish_playback_document", idempotency_key,
        )

    def get_publish_result(self, connector_id: str, archive_id: str, run_id: str, idempotency_key: str) -> dict[str, Any] | None:
        """对账未知写入；404 表示业务端尚未观察到该逻辑操作。"""
        try:
            return self._call(connector_id, "/api/v1/internal/agent-tools/memory.get_publish_result", {"archive_id": archive_id, "run_id": run_id}, "memory.get_publish_result", idempotency_key)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def _call(self, connector_id: str, path: str, input_data: dict[str, Any], tool_name: str, idempotency_key: str | None = None) -> dict[str, Any]:
        connector = self._connectors.get(connector_id)
        if connector is None:
            raise ValueError("BUSINESS_CONNECTOR_UNAVAILABLE")
        content = httpx.Request("POST", "http://tool.local", json={"input": input_data}).content
        timestamp = str(int(time.time()))
        headers = {"X-Agent-Runtime-Id": connector.runtime_id, "X-Agent-Key-Id": connector.key_id, "X-Agent-Timestamp": timestamp, "X-Agent-Signature": tool_signature("POST", path, timestamp, content, connector.secret)}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = self._client.post(f"{connector.base_url.rstrip('/')}{path}", content=content, headers=headers, timeout=10.0)
        response.raise_for_status()
        output = response.json().get("output")
        if not isinstance(output, dict):
            raise ValueError("TOOL_OUTPUT_INVALID")
        logging.info("HTTP Business Tool 成功 tool=%s connector=%s", tool_name, connector_id)
        return output
