"""Runtime 访问业务 HTTP Tool 的固定 connector 网关。"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.tool_security import tool_signature
from app.schemas.agent_package import ToolManifest

# 工具注册表属于 Runtime 代码，而非可变 AgentPackage。即使 package 文件被错误更新，
# 也不能改变业务 host、接口路径或运行上下文的取值方式。
_FIXED_HTTP_TOOLS: dict[str, tuple[str, str, str, str, bool]] = {
    "memory.get_snapshot": (
        "couple_diary_backend",
        "POST",
        "/api/v1/internal/agent-tools/memory.get_snapshot",
        "input",
        False,
    ),
    "memory.publish_playback_document": (
        "couple_diary_backend",
        "POST",
        "/api/v1/internal/agent-tools/memory.publish_playback_document",
        "playback_document",
        True,
    ),
}
_TOOL_OUTPUT_SENSITIVE = re.compile(r"\b(?:1[3-9]\d{9}|\d{17}[\dXx])\b")


@dataclass(frozen=True)
class BusinessConnector:
    """仅由服务端配置构造，AgentPackage 不能提供 base_url 或密钥。"""

    base_url: str
    runtime_id: str
    key_id: str
    secret: str


class ToolGateway:
    """第一版只开放固定业务工具，禁止调用方拼接 URL 或读取 connector 密钥。"""

    def __init__(self, connectors: dict[str, BusinessConnector], client: httpx.Client) -> None:
        self._connectors = connectors
        self._connector_origins = {
            connector_id: self._fixed_origin(connector.base_url)
            for connector_id, connector in connectors.items()
        }
        self._client = client

    def call(
        self,
        manifest: ToolManifest,
        runtime_context: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """按 Runtime 内置 allowlist 调用 HTTP 工具。

        Package manifest 仅是声明，不能成为网络路由或身份来源；四个快照引用始终
        从 Worker 已验证的 ``runtime_context`` 提取。输出不合格或包含可识别敏感
        标识符时直接拒绝，不把响应正文写入日志或审计摘要。
        """
        registration = _FIXED_HTTP_TOOLS.get(manifest.name)
        if registration is None:
            raise ValueError("TOOL_MANIFEST_NOT_ALLOWED")
        connector_id, method, path, input_from, side_effect = registration
        if (
            manifest.connector_id != connector_id
            or manifest.method != method
            or manifest.relative_path != path
            or manifest.input_from != input_from
            or manifest.side_effect != side_effect
        ):
            raise ValueError("TOOL_MANIFEST_NOT_ALLOWED")
        if side_effect and not idempotency_key:
            raise ValueError("TOOL_IDEMPOTENCY_KEY_REQUIRED")

        references = self._trusted_references(runtime_context)
        if manifest.name == "memory.get_snapshot":
            input_data = references
        else:
            document = runtime_context.get(input_from)
            if not isinstance(document, dict):
                raise ValueError("TOOL_INPUT_SOURCE_INVALID")
            input_data = {**references, "document": document}
        return self._call(
            connector_id,
            path,
            input_data,
            manifest.name,
            idempotency_key,
            retry_transport=not side_effect,
        )

    def get_snapshot(
        self,
        connector_id: str,
        archive_id: str,
        snapshot_id: str,
        run_id: str,
        generation_epoch: int,
    ) -> dict[str, Any]:
        """读取冻结快照；读取无副作用，单次传输失败可安全重试。"""
        return self._call(
            connector_id,
            "/api/v1/internal/agent-tools/memory.get_snapshot",
            {
                "archive_id": archive_id,
                "snapshot_id": snapshot_id,
                "run_id": run_id,
                "generation_epoch": generation_epoch,
            },
            "memory.get_snapshot",
            retry_transport=True,
        )

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

    def _call(
        self,
        connector_id: str,
        path: str,
        input_data: dict[str, Any],
        tool_name: str,
        idempotency_key: str | None = None,
        *,
        retry_transport: bool = False,
    ) -> dict[str, Any]:
        """签名后调用固定工具；写操作超时结果交给上层幂等对账。"""
        connector = self._connectors.get(connector_id)
        if connector is None:
            raise ValueError("BUSINESS_CONNECTOR_UNAVAILABLE")
        connector_origin = self._connector_origins[connector_id]
        content = httpx.Request("POST", "http://tool.local", json={"input": input_data}).content
        timestamp = str(int(time.time()))
        headers = {"X-Agent-Runtime-Id": connector.runtime_id, "X-Agent-Key-Id": connector.key_id, "X-Agent-Timestamp": timestamp, "X-Agent-Signature": tool_signature("POST", path, timestamp, content, connector.secret)}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        attempts = 2 if retry_transport else 1
        for attempt in range(attempts):
            try:
                response = self._client.post(
                    f"{connector_origin}{path}",
                    content=content,
                    headers=headers,
                    timeout=10.0,
                    follow_redirects=False,
                )
                break
            except httpx.TransportError:
                if attempt + 1 == attempts:
                    raise
                # 不记录异常、快照或作品正文，避免业务私密信息进入日志。
                logging.warning(
                    "HTTP Business Tool 传输失败，将重试 tool=%s connector=%s code=TRANSPORT_ERROR",
                    tool_name,
                    connector_id,
                )
        if response.is_redirect:
            logging.warning(
                "HTTP Business Tool 请求被拒绝 tool=%s connector=%s code=HTTP_REDIRECT",
                tool_name,
                connector_id,
            )
        elif response.is_error:
            logging.warning(
                "HTTP Business Tool 请求失败 tool=%s connector=%s code=HTTP_STATUS_%s",
                tool_name,
                connector_id,
                response.status_code,
            )
        response.raise_for_status()
        output = response.json().get("output")
        if not isinstance(output, dict):
            raise ValueError("TOOL_OUTPUT_INVALID")
        logging.info("HTTP Business Tool 成功 tool=%s connector=%s", tool_name, connector_id)
        self._validate_output(output)
        return output

    @staticmethod
    def _fixed_origin(base_url: str) -> str:
        """connector 只能是无路径、查询或片段的 HTTP(S) origin。"""
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("BUSINESS_CONNECTOR_URL_INVALID")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("BUSINESS_CONNECTOR_URL_INVALID") from exc
        if port is not None and not 0 < port < 65536:
            raise ValueError("BUSINESS_CONNECTOR_URL_INVALID")
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _trusted_references(runtime_context: Mapping[str, Any]) -> dict[str, Any]:
        """提取运行时已绑定的归档引用，忽略 package 输入里的同名伪造字段。"""
        archive_id = runtime_context.get("archive_id")
        snapshot_id = runtime_context.get("snapshot_id")
        run_id = runtime_context.get("run_id")
        generation_epoch = runtime_context.get("generation_epoch")
        if (
            not isinstance(archive_id, str)
            or not isinstance(snapshot_id, str)
            or not isinstance(run_id, str)
            or isinstance(generation_epoch, bool)
            or not isinstance(generation_epoch, int)
        ):
            raise ValueError("TOOL_RUNTIME_CONTEXT_INVALID")
        return {
            "archive_id": archive_id,
            "snapshot_id": snapshot_id,
            "run_id": run_id,
            "generation_epoch": generation_epoch,
        }

    @staticmethod
    def _validate_output(output: dict[str, Any]) -> None:
        """执行最小 JSON 输出与敏感标识符校验，防止污染 AgentState。"""
        def walk(value: Any) -> None:
            if isinstance(value, str):
                if _TOOL_OUTPUT_SENSITIVE.search(value):
                    raise ValueError("TOOL_OUTPUT_SENSITIVE")
                return
            if value is None or isinstance(value, (bool, int, float)):
                return
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
                return
            raise ValueError("TOOL_OUTPUT_INVALID")

        walk(output)
