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
from pydantic import ValidationError

from app.contracts.tools import ToolError, ToolRequest, ToolResult
from app.core.tool_security import tool_signature
from app.models import AgentToolCall
from app.runtime.interfaces import LeaseContext
from app.runtime.native_tools import (
    contains_sensitive_identifier,
    repair_json_once,
    summarize_keys,
)
from app.runtime.state import AgentState
from app.runtime.test_harness import LoopbackTestTransport
from app.schemas.agent_package import ToolManifest
from app.schemas.audit import (
    AUTHORIZATION_REVOKED,
    CONNECTOR_DISABLED,
    RUNTIME_REJECTION_REASON_CODES,
)
from app.services.tool_call_audit_service import ToolCallAuditService

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
# Native Tool 同样只能由 Runtime 代码注册；Package、模型或业务请求不能声明函数名。
_FIXED_NATIVE_TOOLS: dict[str, tuple[tuple[str, ...], Callable[..., object]]] = {
    "runtime.repair_json_once": (("value",), repair_json_once),
    "runtime.summarize_keys": (("value", "max_items"), summarize_keys),
    "runtime.contains_sensitive_identifier": (("value",), contains_sensitive_identifier),
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
        authorization_permitted: Callable[[str], str | bool | None] = lambda run_id: None,
        execution_permitted: Callable[[str], bool] = lambda run_id: True,
        audit_rejection: Callable[[str, str], None] = lambda run_id, code: None,
        peer_ip_provider: Callable[[], str | None] | None = None,
        reset_peer_ip: Callable[[], None] | None = None,
        test_transport: LoopbackTestTransport | None = None,
    ) -> None:
        # 仅测试 harness 显式注入的对象可绕过公网 DNS/peer 校验；生产默认 None。
        self._test_transport = test_transport
        self._connectors = connectors
        self._connector_origins = {
            connector_id: self._fixed_origin(
                connector.base_url, allow_loopback=test_transport is not None
            )
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
        # 外部副作用前必须实时复核 lease/fencing、cancel、privacy 与 Package 状态；
        # Gateway 只接受布尔结论，不能把 Run 或私密输入写入日志。
        self._execution_permitted = execution_permitted
        # 拒绝审计只接收 Run ID 与固定错误码，绝不允许 connector、URL 或请求内容穿透。
        self._audit_rejection = audit_rejection
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
        if not manifest.enabled:
            raise ValueError("TOOL_CAPABILITY_DISABLED")
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

    def call_native(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        audit_service: ToolCallAuditService,
        run_id: str,
        execution_attempt: int,
        step_id: str,
        logical_key: str,
        request_digest: str,
    ) -> object:
        """执行固定 Native Tool，并只把安全摘要结算到权威物理 attempt。

        Native Tool 不能成为绕过 policy、预算或审计的旁路。输入和完整输出只留在
        当前调用栈；审计记录仅保留操作名、受控 digest 与无正文结果摘要。
        """
        registration = _FIXED_NATIVE_TOOLS.get(tool_name)
        if registration is None:
            raise ValueError("NATIVE_TOOL_NOT_ALLOWED")
        names, handler = registration
        if set(arguments) != set(names):
            raise ValueError("NATIVE_TOOL_INPUT_INVALID")
        record = audit_service.begin_native(
            run_id=run_id, execution_attempt=execution_attempt, step_id=step_id,
            tool_name=tool_name, logical_key=logical_key, request_digest=request_digest,
        )
        try:
            result = handler(**dict(arguments))
        except (TypeError, ValueError):
            audit_service.fail(record, "NATIVE_TOOL_INPUT_INVALID", retryable=False,
                               error_type="native_tool_rejected")
            raise ValueError("NATIVE_TOOL_INPUT_INVALID") from None
        audit_service.succeed(record, self._native_output_summary(tool_name, result))
        return result

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

    @staticmethod
    def to_tool_error(exc: Exception) -> ToolError:
        """把 Gateway 抛出的异常映射为冻结 ToolError；统一未来失败语义转换入口。

        决策（R3 路径 b）：worker/runner 当前 catch Exception 后写受控 audit code，
        不直接消费 ToolError；因此 Gateway 仍抛 ValueError 以保持现有异常处理契约，
        仅提供该助手作为未来统一的 ToolError 转换入口。

        铁律：``details_visible_to_model`` 永远 False，业务错误详情不进入模型上下文。
        ValueError 的字符串内容（如 'TOOL_OUTPUT_INVALID'）原样映射到 error_code；
        非 ValueError 给出 'TOOL_UNKNOWN' 兜底。``retryable`` 默认 False，调用方
        按受控码自行决定是否覆盖，避免在助手内臆造重试策略。
        """
        if isinstance(exc, ValueError) and str(exc):
            error_code = str(exc)
        else:
            error_code = "TOOL_UNKNOWN"
        return ToolError(
            error_code=error_code,
            error_type=type(exc).__name__,
            retryable=False,
            safe_message="工具调用失败，详情见审计日志",
            details_visible_to_model=False,
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
        document: dict[str, Any], idempotency_key: str, tool_call: AgentToolCall | None = None,
    ) -> dict[str, Any]:
        """发布完整作品；调用方只能传受信任 run 上下文与已校验作品结构。"""
        if tool_call is None:
            raise ValueError("TOOL_ATTEMPT_REQUIRED")
        if tool_call.run_id != run_id or tool_call.side_effect is not True:
            raise ValueError("TOOL_ATTEMPT_INVALID")
        return self._call(
            connector_id,
            "/api/v1/internal/agent-tools/memory.publish_playback_document",
            {"archive_id": archive_id, "run_id": run_id, "snapshot_id": snapshot_id, "generation_epoch": generation_epoch, "document": document},
            "memory.publish_playback_document", idempotency_key, tool_call=tool_call,
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
        tool_call: AgentToolCall | None = None,
    ) -> dict[str, Any]:
        """签名后调用固定工具；写操作超时结果交给上层幂等对账。"""
        connector = self._connectors.get(connector_id)
        if connector is None:
            run_id = input_data.get("run_id")
            if isinstance(run_id, str):
                self._audit_rejection(run_id, CONNECTOR_DISABLED)
            raise ValueError("BUSINESS_CONNECTOR_UNAVAILABLE")
        connector_origin = self._connector_origins[connector_id]
        # R3：构造冻结 ToolRequest 形状后序列化请求体，确保含 input 与 context 两个字段。
        # context 字段当前无冻结语义（契约冻结记录与 tools.py 均未规定其内容），
        # 置空是最保守不臆造做法；未来冻结后由调用方填充。本地 handler 只读 input，
        # 加 context={} 向后兼容。序列化结果仍为 {"input":..., "context":{}}。
        tool_request = ToolRequest(input=input_data, context={})
        content = httpx.Request("POST", "http://tool.local", json=tool_request.model_dump()).content
        timestamp = str(int(time.time()))
        headers = {"X-Agent-Runtime-Id": connector.runtime_id, "X-Agent-Key-Id": connector.key_id, "X-Agent-Timestamp": timestamp, "X-Agent-Signature": tool_signature("POST", path, timestamp, content, connector.secret)}
        if tool_call is not None:
            attempt = tool_call.tool_attempt
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
                raise ValueError("TOOL_ATTEMPT_INVALID")
            headers["X-Agent-Tool-Attempt"] = str(attempt)
        # R3 补充：业务上下文 header 辅助业务端定位 Run/Tool，不参与 HMAC 签名原文。
        # 签名原文仍是 METHOD\npath\ntimestamp\nbody_sha256，新 header 仅随请求发送。
        # run_id 缺失或非字符串时不写该 header，后续授权校验会按既定路径拒绝。
        header_run_id = input_data.get("run_id")
        if isinstance(header_run_id, str):
            headers["X-Agent-Run-Id"] = header_run_id
        headers["X-Agent-Tool-Name"] = tool_name
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        attempts = 2 if retry_transport else 1
        for attempt in range(attempts):
            run_id = input_data.get("run_id")
            authorization_rejection = (
                self._authorization_permitted(run_id)
                if isinstance(run_id, str)
                else AUTHORIZATION_REVOKED
            )
            if authorization_rejection is None or authorization_rejection is True:
                reason_code = None
            elif authorization_rejection in RUNTIME_REJECTION_REASON_CODES:
                reason_code = str(authorization_rejection)
            else:
                reason_code = AUTHORIZATION_REVOKED
            if not isinstance(run_id, str) or reason_code is not None:
                if isinstance(run_id, str):
                    self._audit_rejection(run_id, reason_code or AUTHORIZATION_REVOKED)
                logging.info(
                    "工具调用在发送前中止 tool=%s code=%s",
                    tool_name,
                    "TOOL_AUTHORIZATION_REVOKED",
                )
                raise ValueError("TOOL_AUTHORIZATION_REVOKED")
            if not self._execution_permitted(run_id):
                logging.info(
                    "工具调用在发送前中止 tool=%s code=%s",
                    tool_name,
                    "TOOL_EXECUTION_CONTEXT_INVALID",
                )
                raise ValueError("TOOL_EXECUTION_CONTEXT_INVALID")
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
            allowed_peer_ips: frozenset[str]
            if self._test_transport is not None:
                if not self._test_transport.allows(connector.base_url):
                    raise ValueError("TEST_HARNESS_LOOPBACK_REQUIRED")
                allowed_peer_ips = frozenset()
            else:
                allowed_peer_ips = self._ensure_public_endpoint(connector.base_url)
            if self._test_transport is None and not self._is_mock_transport and self._peer_ip_provider is None:
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
                if self._test_transport is None:
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
        # R3 补充：非 2xx 响应 body 自称 ToolError 形状时按冻结铁律 fail closed。
        # 仅当 body 能解析为 ToolError 且显式声明 details_visible_to_model=true 时
        # 提前抛受控错误码，阻止业务错误详情通过模型可见通道进入 AgentState/日志。
        # 其他非 2xx 响应（FastAPI 默认 detail / 空 body 等）继续交给 raise_for_status，
        # 保持 runner 对 409/404 的 HTTPStatusError 捕获契约不变。
        ToolGateway._fail_closed_if_response_claims_unsafe_tool_error(response, tool_name)
        response.raise_for_status()
        # R3：响应解析升级为冻结 ToolResult，强制 schema_version 与当前协议版本对齐。
        # ToolResult.schema_version 默认 '1.0.0'，缺失字段走默认值视为匹配；显式声明
        # 其他版本按不匹配拒绝，避免业务端单方面升级协议绕过 Runtime 校验。
        try:
            tool_result = ToolResult.model_validate(response.json())
        except ValidationError as exc:
            logging.info("HTTP Business Tool 响应违反 ToolResult 契约 tool=%s code=%s", tool_name, "TOOL_OUTPUT_INVALID")
            raise ValueError("TOOL_OUTPUT_INVALID") from exc
        if tool_result.schema_version != "1.0.0":
            logging.info(
                "HTTP Business Tool 响应 schema_version 不匹配 tool=%s expected=1.0.0 got=%s",
                tool_name,
                tool_result.schema_version,
            )
            raise ValueError("TOOL_OUTPUT_SCHEMA_VERSION_INVALID")
        output = tool_result.output
        # R3 补充：Snapshot 形态的 output 自带内层 schema_version 字段，与外层信封独立。
        # 业务端 MemorySnapshotService 序列化时写入该字段，Runtime 必须独立校验，
        # 防止单方升级内层 schema 但外层信封仍伪装成 1.0.0。
        # 字段缺失（如 publish 结果）不触发该校验，保持各工具自有 output 形状。
        inner_version = output.get("schema_version")
        if isinstance(inner_version, str) and inner_version != "1.0.0":
            logging.info(
                "HTTP Business Tool 输出内层 schema_version 不匹配 tool=%s expected=1.0.0 got=%s",
                tool_name,
                inner_version,
            )
            raise ValueError("TOOL_OUTPUT_SCHEMA_VERSION_INVALID")
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
    def _fixed_origin(base_url: str, *, allow_loopback: bool = False) -> str:
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
        if not allow_loopback:
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
    def _fail_closed_if_response_claims_unsafe_tool_error(
        response: httpx.Response, tool_name: str
    ) -> None:
        """非 2xx 响应若 body 自称 ToolError 且违反冻结铁律，一律 fail closed。

        R3 补充（运行手册 L558）：仅当 body 能完整解析为 ToolError 形状时介入；
        介入条件目前只覆盖契约明确禁止的 ``details_visible_to_model=True``——这是
        冻结合约里唯一的硬性不安全声明，必须无条件拒绝，防止业务错误详情灌入
        模型上下文。

        ponytail: 跳过 coordinator 提到的「未知 error_code」「HTTP 状态码 vs
        (error_code+retryable) 语义矛盾」两类检查——目前仓库没有冻结的
        error_code 枚举或状态码↔可重试映射表，引入即臆造策略。等 R4+ 冻结枚举
        与映射表后再扩展本方法，已在 VERIFICATION.md 风险项登记。

        body 不构成 ToolError（如 FastAPI 默认 ``{"detail": ...}``、空 body）时
        不介入，让 ``raise_for_status`` 按 409/404 现有契约传播 HTTPStatusError。
        """
        if not response.is_error:
            return
        try:
            parsed = response.json()
        except ValueError:
            return
        if not isinstance(parsed, dict):
            return
        # 仅在 body 自称 ToolError（含全部 4 个必填字段）时介入；非 ToolError 形状
        # 由 raise_for_status 按状态码处理，保留 runner 既有捕获契约。
        required_keys = {"error_code", "error_type", "retryable", "safe_message"}
        if not required_keys.issubset(parsed):
            return
        try:
            candidate = ToolError.model_validate(parsed)
        except ValidationError:
            # 自称 ToolError 但 shape 不合法，按受控码拒绝，不透传 body 内容。
            logging.info(
                "HTTP Business Tool 非 2xx 响应自称 ToolError 但 shape 非法 tool=%s code=%s",
                tool_name,
                "TOOL_ERROR_SHAPE_INVALID",
            )
            raise ValueError("TOOL_ERROR_SHAPE_INVALID") from None
        if candidate.details_visible_to_model:
            logging.info(
                "HTTP Business Tool 非 2xx 响应非法声明 details_visible_to_model=True tool=%s code=%s",
                tool_name,
                "TOOL_ERROR_SHAPE_INVALID",
            )
            raise ValueError("TOOL_ERROR_SHAPE_INVALID")

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
    def _native_output_summary(tool_name: str, result: object) -> dict[str, object]:
        """Native 结果摘要禁止包含待修复 JSON 或待扫描正文。"""
        if tool_name == "runtime.contains_sensitive_identifier":
            return {"matched": bool(result)}
        if tool_name == "runtime.summarize_keys" and isinstance(result, dict):
            keys = result.get("keys")
            count = result.get("item_count")
            if isinstance(keys, list) and isinstance(count, int):
                return {"keys": list(keys), "item_count": count}
        if tool_name == "runtime.repair_json_once":
            if result is None:
                return {"result": "none"}
            summary = summarize_keys(result, max_items=32)
            keys, count = summary["keys"], summary["item_count"]
            if not isinstance(keys, list) or not isinstance(count, int):
                raise ValueError("NATIVE_TOOL_OUTPUT_INVALID")
            return {"keys": list(keys), "item_count": count}
        raise ValueError("NATIVE_TOOL_OUTPUT_INVALID")

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
