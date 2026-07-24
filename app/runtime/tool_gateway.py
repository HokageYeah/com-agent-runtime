"""Runtime 访问业务 HTTP Tool 的固定 connector 网关。"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.tool_security import tool_signature
from app.runtime.interfaces import LeaseContext
from app.runtime.state import AgentState
from app.schemas.agent_package import ToolManifest

# 工具注册表属于 Runtime 代码，而非可变 AgentPackage。即使 package 文件被错误更新，
# 也不能改变业务 host、接口路径或运行上下文的取值方式。
_FIXED_HTTP_TOOLS: dict[str, tuple[str, str, str, str, bool, str]] = {
    "memory.get_snapshot": (
        "couple_diary_backend",
        "POST",
        "/api/v1/internal/agent-tools/memory.get_snapshot",
        "input",
        False,
        "cancellable",
    ),
    "memory.publish_playback_document": (
        "couple_diary_backend",
        "POST",
        "/api/v1/internal/agent-tools/memory.publish_playback_document",
        "playback_document",
        True,
        "query_after_commit",
    ),
}
_TOOL_OUTPUT_SENSITIVE = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|\d{17}[\dXx])(?!\d)")


@dataclass(frozen=True)
class BusinessConnector:
    """仅由服务端配置构造，AgentPackage 不能提供 base_url 或密钥。"""

    base_url: str
    runtime_id: str
    key_id: str
    secret: str


class ToolGateway:
    """第一版只开放固定业务工具，禁止调用方拼接 URL 或读取 connector 密钥。"""

    def __init__(
        self,
        connectors: dict[str, BusinessConnector],
        client: httpx.Client,
        *,
        is_draining: Callable[[], bool] = lambda: False,
        deadline_at: Callable[[], datetime | None] = lambda: None,
        lease_expires_at: Callable[[], datetime | None] = lambda: None,
        authorization_permitted: Callable[[str], bool] = lambda run_id: True,
        peer_ip_provider: Callable[[], str | None] | None = None,
        reset_peer_ip: Callable[[], None] | None = None,
    ) -> None:
        self._connectors = connectors
        self._connector_origins = {
            connector_id: self._fixed_origin(connector.base_url)
            for connector_id, connector in connectors.items()
        }
        self._client = client
        # 回调必须在每次发送前读取 Worker 实时状态，不能固化到 Run 或 Gateway 构造时。
        self._is_draining = is_draining
        # 仅由 Worker 装配可信 Run/lease 窗口；Package 和工具输入不能影响 timeout。
        self._deadline_at = deadline_at
        self._lease_expires_at = lease_expires_at
        # 由 Worker 注入的权威 Run 版本复核；工具输入不能影响该决策。
        self._authorization_permitted = authorization_permitted
        # 生产 Worker 必须注入真实 socket 对端读取器；MockTransport 测试没有 TCP 连接，
        # 因而不强制该字段，避免测试伪造一套网络栈。
        self._peer_ip_provider = peer_ip_provider
        self._reset_peer_ip = reset_peer_ip
        self._is_mock_transport = isinstance(getattr(client, "_transport", None), httpx.MockTransport)

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
        connector_id, method, path, input_from, side_effect, cancellation_behavior = registration
        if (
            manifest.connector_id != connector_id
            or manifest.method != method
            or manifest.relative_path != path
            or manifest.input_from != input_from
            or manifest.side_effect != side_effect
            or manifest.cancellation_behavior != cancellation_behavior
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

    @staticmethod
    def apply_result(
        manifest: ToolManifest,
        output: object,
        state: AgentState,
        run_id: str,
        lease_context: LeaseContext,
        lease_service: Any,
    ) -> None:
        """在有效 lease 下校验并写入工具结果，拒绝任何失效结果污染状态。

        本方法是 HTTP Tool 输出进入 ``AgentState`` 的唯一边界：先复核
        fencing/privacy/authorization，再检查受限 object schema、敏感标识和
        控制面字段。失败时不修改状态，也不创建 Artifact 或 checkpoint。
        """
        if not lease_service.can_write(run_id, lease_context):
            logging.warning("工具结果写入被 lease 拒绝 run_id=%s code=%s", run_id, "TOOL_RESULT_LEASE_INVALID")
            raise ValueError("TOOL_RESULT_LEASE_INVALID")
        ToolGateway._validate_output_schema(manifest.output_schema, output)
        if not isinstance(output, dict):
            raise ValueError("TOOL_OUTPUT_SCHEMA_INVALID")
        ToolGateway._validate_output(output)
        state.apply_tool_output(manifest.output_to or "", output)
        logging.info("工具结果安全写入 run_id=%s tool=%s target=%s", run_id, manifest.name, manifest.output_to)

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
            return self._call(
                connector_id,
                "/api/v1/internal/agent-tools/memory.get_publish_result",
                {"archive_id": archive_id, "run_id": run_id},
                "memory.get_publish_result",
                idempotency_key,
                allow_during_draining=True,
            )
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
        allow_during_draining: bool = False,
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
            run_id = input_data.get("run_id")
            if not isinstance(run_id, str) or not self._authorization_permitted(run_id):
                logging.info(
                    "工具调用在发送前中止 tool=%s code=%s",
                    tool_name,
                    "TOOL_AUTHORIZATION_REVOKED",
                )
                raise ValueError("TOOL_AUTHORIZATION_REVOKED")
            if self._is_draining() and not allow_during_draining:
                # 每次物理发送前实时复核，读取重试也不能穿透 draining。
                logging.info(
                    "工具调用在发送前中止 tool=%s code=%s",
                    tool_name,
                    "TOOL_CALL_DRAINING",
                )
                raise ValueError("TOOL_CALL_DRAINING")
            # 不缓存 DNS；每一次物理发送前都重新解析，避免 connector 域名在
            # 注册后被重绑定到 loopback、私网或云元数据地址。
            allowed_peer_ips = self._ensure_public_endpoint(connector.base_url)
            if not self._is_mock_transport and self._peer_ip_provider is None:
                raise ValueError("BUSINESS_CONNECTOR_PEER_UNVERIFIABLE")
            if self._reset_peer_ip is not None:
                self._reset_peer_ip()
            try:
                response = self._client.post(
                    f"{connector_origin}{path}",
                    content=content,
                    headers=headers,
                    timeout=self._effective_timeout(10.0),
                    follow_redirects=False,
                )
                self._verify_connected_peer(allowed_peer_ips)
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

    def _effective_timeout(self, fixed_timeout: float) -> float:
        """取固定 Tool timeout 与可信 Run/lease 余量的最小值。"""
        timeout = fixed_timeout
        now = datetime.now(UTC)
        for window_end in (self._deadline_at(), self._lease_expires_at()):
            if window_end is None:
                continue
            normalized_end = (
                window_end.replace(tzinfo=UTC)
                if window_end.tzinfo is None
                else window_end.astimezone(UTC)
            )
            remaining = (normalized_end - now).total_seconds()
            if remaining <= 0:
                logging.info("工具调用在发送前中止 code=%s", "TOOL_CALL_DEADLINE_EXPIRED")
                raise ValueError("TOOL_CALL_DEADLINE_EXPIRED")
            timeout = min(timeout, remaining)
        return timeout

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
        ToolGateway._reject_unsafe_host(parsed.hostname)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _reject_unsafe_host(host: str | None) -> None:
        """拒绝 connector 配置中的 localhost 与非公网 IP 字面量。"""
        if not host:
            raise ValueError("BUSINESS_CONNECTOR_ENDPOINT_UNSAFE")
        if host.rstrip(".").lower() == "localhost":
            logging.warning("业务 connector endpoint 静态校验拒绝 code=BUSINESS_CONNECTOR_ENDPOINT_UNSAFE")
            raise ValueError("BUSINESS_CONNECTOR_ENDPOINT_UNSAFE")
        try:
            address = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            return
        if not address.is_global:
            logging.warning("业务 connector endpoint 静态校验拒绝 code=BUSINESS_CONNECTOR_ENDPOINT_UNSAFE")
            raise ValueError("BUSINESS_CONNECTOR_ENDPOINT_UNSAFE")

    @classmethod
    def _ensure_public_endpoint(cls, base_url: str) -> frozenset[str]:
        """发送前解析 connector 主机，返回本次允许匹配的公网地址集合。"""
        parsed = urlsplit(base_url)
        host = parsed.hostname
        cls._reject_unsafe_host(host)
        if not host:
            raise ValueError("BUSINESS_CONNECTOR_ENDPOINT_UNSAFE")
        try:
            default_port = 80 if parsed.scheme == "http" else 443
            addresses = socket.getaddrinfo(host, parsed.port or default_port, type=socket.SOCK_STREAM)
        except (OSError, ValueError) as exc:
            logging.warning("业务 connector endpoint DNS 解析失败 code=BUSINESS_CONNECTOR_ENDPOINT_DNS_UNRESOLVED")
            raise ValueError("BUSINESS_CONNECTOR_ENDPOINT_DNS_UNRESOLVED") from exc
        if not addresses:
            logging.warning("业务 connector endpoint DNS 解析为空 code=BUSINESS_CONNECTOR_ENDPOINT_DNS_UNRESOLVED")
            raise ValueError("BUSINESS_CONNECTOR_ENDPOINT_DNS_UNRESOLVED")
        peer_ips: set[str] = set()
        for _, _, _, _, sockaddr in addresses:
            try:
                address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
            except ValueError as exc:
                logging.warning("业务 connector endpoint DNS 地址无效 code=BUSINESS_CONNECTOR_ENDPOINT_UNSAFE")
                raise ValueError("BUSINESS_CONNECTOR_ENDPOINT_UNSAFE") from exc
            if not address.is_global:
                logging.warning("业务 connector endpoint DNS 地址被拒绝 code=BUSINESS_CONNECTOR_ENDPOINT_UNSAFE")
                raise ValueError("BUSINESS_CONNECTOR_ENDPOINT_UNSAFE")
            peer_ips.add(str(address))
        return frozenset(peer_ips)

    def _verify_connected_peer(self, allowed_peer_ips: frozenset[str]) -> None:
        """响应进入 Runtime 前核对真实 TCP 对端，拒绝预检后的 DNS rebinding。"""
        if self._peer_ip_provider is None:
            # MockTransport 不存在真实 socket，其他 Transport 已在发送前 fail-closed。
            return
        peer_ip = self._peer_ip_provider()
        try:
            normalized_peer = str(ipaddress.ip_address(peer_ip or ""))
        except ValueError as exc:
            logging.warning("业务 connector 对端地址不可验证 code=BUSINESS_CONNECTOR_PEER_UNVERIFIABLE")
            raise ValueError("BUSINESS_CONNECTOR_PEER_UNVERIFIABLE") from exc
        if normalized_peer not in allowed_peer_ips:
            logging.warning("业务 connector 对端地址不匹配 code=BUSINESS_CONNECTOR_PEER_MISMATCH")
            raise ValueError("BUSINESS_CONNECTOR_PEER_MISMATCH")

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

    @staticmethod
    def _validate_output_schema(schema: dict[str, Any], output: object) -> None:
        """校验受限 object schema，避免第三方 schema 功能隐式扩大写入能力。"""
        if not isinstance(output, dict) or schema.get("type") != "object":
            raise ValueError("TOOL_OUTPUT_SCHEMA_INVALID")
        if set(schema) - {"type", "required", "properties", "additionalProperties"}:
            raise ValueError("TOOL_OUTPUT_SCHEMA_INVALID")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        additional = schema.get("additionalProperties", True)
        if (
            not isinstance(properties, dict)
            or not isinstance(required, list)
            or not isinstance(additional, bool)
            or any(not isinstance(name, str) for name in required)
            or any(not isinstance(name, str) or not isinstance(rule, dict) for name, rule in properties.items())
            or any(name not in properties for name in required)
        ):
            raise ValueError("TOOL_OUTPUT_SCHEMA_INVALID")
        if any(name not in output for name in required):
            raise ValueError("TOOL_OUTPUT_SCHEMA_INVALID")
        if not additional and any(name not in properties for name in output):
            raise ValueError("TOOL_OUTPUT_SCHEMA_INVALID")
        for name, value in output.items():
            rule = properties.get(name)
            if rule is not None and not ToolGateway._matches_schema_type(value, rule):
                raise ValueError("TOOL_OUTPUT_SCHEMA_INVALID")

    @staticmethod
    def _matches_schema_type(value: object, rule: dict[str, Any]) -> bool:
        """仅识别当前 Tool manifest 所需的基础 JSON 类型，不执行动态 schema。"""
        if set(rule) != {"type"} or not isinstance(rule.get("type"), str):
            return False
        return {
            "string": isinstance(value, str),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "null": value is None,
        }.get(rule["type"], False)
