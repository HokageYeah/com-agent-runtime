"""Runtime 访问业务 HTTP Tool 的固定 connector 网关。"""

from __future__ import annotations

import hashlib
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

from app.contracts.tools import (
    TOOL_ERROR_SPECS_BY_WIRE_VERSION,
    ToolError,
    ToolRequest,
    ToolResult,
)
from app.core.tool_security import tool_signature
from app.models import AgentRun, AgentToolCall
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
# 业务端工具幂等键冻结合同：Idempotency-Key 必须匹配 ^[A-Za-z0-9_-]{1,64}$
# （couple-diary-b Header pattern，违约由 FastAPI 在进 handler 前直接 422，
# 2026-08-19 线上故障：publish 逻辑键含冒号且超长 → BUSINESS_DATA_INVALID）。
# 逻辑键含冒号命名空间或超长时派生 sha256 hex（64 位小写十六进制，恒合规、
# 确定性可重放）；Runtime 内部审计坐标（logical_key）不受影响。
_WIRE_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _wire_idempotency_key(logical_key: str) -> str:
    """逻辑幂等键 → 业务 wire 合规键；合规即透传，违约派生 sha256 hex。"""

    if _WIRE_IDEMPOTENCY_KEY.fullmatch(logical_key):
        return logical_key
    return hashlib.sha256(logical_key.encode("utf-8")).hexdigest()


# agent_version → Tool wire 合同的显式允许表：1.0.0 走四字段/六码 v1，
# 1.0.1 起走 v1.1 完整错误合同；1.0.2 只改了 prompt manifest 的 guardrail
# 策略（redacted_only），Tool 合同与 1.0.1 完全一致，沿用 v1.1.0。
# 1.0.3（M6 媒体通道）只新增图节点与媒体生成，Tool 合同零变更，沿用 v1.1.0。
# 新版本包注册时若忘记在此登记，Worker 会在发包前以 TOOL_WIRE_VERSION_INVALID
# 瞬时失败（无日志、无 HTTP），表现为 load_snapshot 节点 WORKFLOW_NODE_FAILED。
_TOOL_WIRE_VERSION_BY_AGENT_VERSION = {
    "1.0.0": "1.0.0",
    "1.0.1": "1.1.0",
    "1.0.2": "1.1.0",
    "1.0.3": "1.1.0",
}
_DEFAULT_TOOL_WIRE_VERSION = "1.1.0"


class ToolErrorRejected(ValueError):
    """已验证的业务 ToolError 的安全控制流信号。

    仅携带冻结 code/retryable，不携带 Provider response、detail 或异常文本；调用者
    因此可决定终止、受控重试或 generation superseded 停止，而不会把私密响应带出
    HTTP 边界。
    """

    def __init__(self, error: ToolError) -> None:
        self.error_code = error.error_code
        self.retryable = error.retryable
        super().__init__(f"TOOL_ERROR_{error.error_code}")


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
        allow_private_endpoints: bool = False,
    ) -> None:
        # 仅测试 harness 显式注入的对象可绕过公网 DNS/peer 校验；生产默认 None。
        self._test_transport = test_transport
        # 开发联调逃生门：本机双进程部署时 connector 指向 127.0.0.1。仅当运维
        # 通过 RUNTIME_TOOL_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS 显式开启（config
        # 侧仅 development/test 允许置真）才跳过公网 DNS/对端复核；其余 SSRF
        # 防线（scheme 白名单/端口/无路径凭据）保持不变，生产默认 False。
        self._allow_private_endpoints = allow_private_endpoints
        self._connectors = connectors
        self._connector_origins = {
            connector_id: self._fixed_origin(
                connector.base_url,
                allow_loopback=allow_private_endpoints or test_transport is not None,
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
        tool_context = runtime_context.get("tool_context")
        if not isinstance(tool_context, Mapping):
            raise ValueError("TOOL_CONTEXT_INVALID")
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
            tool_context=tool_context,
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
    def build_tool_context(run: AgentRun, step_id: str) -> dict[str, str]:
        """从可信 Run/Step 构造冻结 envelope context 7 字段。

        形状由 fixture tool_contract.context_required 冻结：agent_id、
        agent_version、run_id、step_id、business_type、business_id、trace_id。
        所有字段都必须来自真实 AgentRun/当前 Step，缺失不允许以空串兜底；否则
        在 HTTP 请求构造前 fail closed，避免调用方自报任意身份或业务归属。
        """
        context = {
            "agent_id": run.agent_id,
            "agent_version": run.agent_version,
            "run_id": run.run_id,
            "step_id": step_id,
            "business_type": run.business_type,
            "business_id": run.business_id,
            "trace_id": run.trace_id,
        }
        if any(not isinstance(value, str) or not value for value in context.values()):
            raise ValueError("TOOL_CONTEXT_INVALID")
        if context["agent_id"] != "memoir_agent" or context["business_type"] != "couple_memory":
            raise ValueError("TOOL_CONTEXT_TRUST_INVALID")
        if not re.fullmatch(r"\d+\.\d+\.\d+", context["agent_version"]):
            raise ValueError("TOOL_CONTEXT_TRUST_INVALID")
        return context

    def get_snapshot(
        self,
        connector_id: str,
        archive_id: str,
        snapshot_id: str,
        run_id: str,
        generation_epoch: int,
        # tool_context 是 envelope 元数据而非业务入参；位置参数让 *args 测试替身
        # 自动兼容（生产 Runner 总是显式透传，None 仅用于路径1/替身的退化场景）。
        tool_context: Mapping[str, str] | None = None,
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
            tool_context=tool_context,
        )

    def publish_playback_document(
        self, connector_id: str, archive_id: str, run_id: str, snapshot_id: str, generation_epoch: int,
        document: dict[str, Any], idempotency_key: str, tool_call: AgentToolCall | None = None,
        tool_context: Mapping[str, str] | None = None,
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
            tool_context=tool_context,
        )

    def get_publish_result(
        self,
        connector_id: str,
        archive_id: str,
        *scope_and_key: object,
        tool_context: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """对账未知写入，并兼容 v1.0.0 的两字段 query wire。

        新调用传 ``snapshot_id, run_id, generation_epoch, idempotency_key``；历史
        调用传 ``run_id, idempotency_key``，由 Provider 以 Archive/RunRef 的可信
        关联复核缺失 scope。其它 arity/type 一律在发包前拒绝。
        """
        if len(scope_and_key) == 2:
            run_id, idempotency_key = scope_and_key
            if not isinstance(run_id, str) or not isinstance(idempotency_key, str):
                raise ValueError("TOOL_PUBLISH_RESULT_INPUT_INVALID")
            input_data: dict[str, Any] = {"archive_id": archive_id, "run_id": run_id}
        elif len(scope_and_key) == 4:
            snapshot_id, run_id, generation_epoch, idempotency_key = scope_and_key
            if (
                not isinstance(snapshot_id, str)
                or not isinstance(run_id, str)
                or isinstance(generation_epoch, bool)
                or not isinstance(generation_epoch, int)
                or not isinstance(idempotency_key, str)
            ):
                raise ValueError("TOOL_PUBLISH_RESULT_INPUT_INVALID")
            input_data = {
                "archive_id": archive_id,
                "snapshot_id": snapshot_id,
                "run_id": run_id,
                "generation_epoch": generation_epoch,
            }
        else:
            raise ValueError("TOOL_PUBLISH_RESULT_INPUT_INVALID")
        try:
            return self._call(
                connector_id,
                "/api/v1/internal/agent-tools/memory.get_publish_result",
                input_data,
                "memory.get_publish_result",
                idempotency_key,
                allow_during_draining=True,
                tool_context=tool_context,
            )
        except ToolErrorRejected as exc:
            if exc.error_code == "PUBLISH_NOT_YET_OBSERVED":
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
        tool_context: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """签名后调用固定工具；写操作超时结果交给上层幂等对账。"""
        connector = self._connectors.get(connector_id)
        if connector is None:
            run_id = input_data.get("run_id")
            if isinstance(run_id, str):
                self._audit_rejection(run_id, CONNECTOR_DISABLED)
            raise ValueError("BUSINESS_CONNECTOR_UNAVAILABLE")
        connector_origin = self._connector_origins[connector_id]
        # 工具 context 是可信 Run/Step envelope，不是可选业务输入。MockTransport 的
        # 旧单元测试可继续覆盖网络细节；任何真实 HTTP transport 都必须在发包前具有
        # 完整 context，不能以 ``None``、``{}`` 或空字符串降级。
        context = self._validated_tool_context(tool_context, input_data, tool_name)
        tool_request = ToolRequest(
            input=input_data,
            context=context,
        )
        content = httpx.Request("POST", "http://tool.local", json=tool_request.model_dump()).content
        timestamp = str(int(time.time()))
        wire_version = self._tool_wire_version(context)
        # 身份头方向是跨项目冻结边界：业务后端调用 Runtime 才使用
        # X-Agent-Client-Id；Runtime 调用业务 Tool（以及 callback）必须使用
        # X-Agent-Runtime-Id。不能因两端当前实现恰好一致而混用。
        headers = {"X-Agent-Runtime-Id": connector.runtime_id, "X-Agent-Key-Id": connector.key_id, "X-Agent-Timestamp": timestamp, "X-Agent-Signature": tool_signature("POST", path, timestamp, content, connector.secret), "X-Agent-Tool-Contract-Version": wire_version}
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
            headers["Idempotency-Key"] = _wire_idempotency_key(idempotency_key)
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
            elif self._allow_private_endpoints:
                # 开发联调显式放行私网 connector：跳过公网 DNS 解析与对端复核，
                # 仅信任运维装配的 base_url（业务请求/Package 无法提供 endpoint）。
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
                # 开发联调放行时同样跳过 TCP 对端复核（对端就是本机 loopback）。
                if self._test_transport is None and not self._allow_private_endpoints:
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
        # 所有非 2xx 都必须是完整、精确的 ToolError。合法错误也不能退回到裸 HTTP
        # 分支：它们通过 ToolErrorRejected 驱动上层审计/重试/旧 generation 终止。
        if response.is_error:
            raise ToolErrorRejected(self._parse_tool_error(response, tool_name, wire_version))
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

    def _parse_tool_error(
        self, response: httpx.Response, tool_name: str, wire_version: str
    ) -> ToolError:
        """严格解析 non-2xx ToolError，且绝不记录 response body。"""
        try:
            parsed = response.json()
        except ValueError:
            raise ValueError("TOOL_ERROR_SHAPE_INVALID") from None
        if not isinstance(parsed, dict):
            raise ValueError("TOOL_ERROR_SHAPE_INVALID")
        required_keys = {"error_code", "error_type", "retryable", "safe_message"}
        keys = set(parsed)
        # v1.0.0 的历史 wire 允许省略 visibility，也允许显式声明 false；这两个
        # 形状等价。显式 true 以及任何其它额外字段均不能被旧 consumer 接受。
        # v1.1.0 则固定要求完整五字段，不能由 response 自行协商。
        allowed_key_sets = (
            (required_keys, required_keys | {"details_visible_to_model"})
            if wire_version == "1.0.0"
            else (required_keys | {"details_visible_to_model"},)
        )
        if keys not in allowed_key_sets:
            raise ValueError("TOOL_ERROR_SHAPE_INVALID")
        try:
            candidate = ToolError.model_validate(parsed)
        except ValidationError:
            raise ValueError("TOOL_ERROR_SHAPE_INVALID") from None
        if candidate.details_visible_to_model:
            raise ValueError("TOOL_ERROR_SHAPE_INVALID")
        specs = TOOL_ERROR_SPECS_BY_WIRE_VERSION.get(wire_version)
        if specs is None:
            raise ValueError("TOOL_WIRE_VERSION_INVALID")
        spec = specs.get(candidate.error_code)
        if spec is None:
            raise ValueError("TOOL_ERROR_CODE_UNKNOWN")
        if (
            response.status_code != spec["http_status"]
            or candidate.retryable is not spec["retryable"]
            or candidate.error_type != spec["error_type"]
            or candidate.safe_message != spec["safe_message"]
        ):
            raise ValueError("TOOL_ERROR_CONTRADICTION")
        logging.info("HTTP Business Tool 返回受控错误 tool=%s code=%s", tool_name, candidate.error_code)
        return candidate

    @staticmethod
    def _tool_wire_version(context: Mapping[str, str]) -> str:
        """从可信 AgentRun identity 选择 Tool wire；Mock 单测默认最新 wire。

        该选择不能来自 Tool input 或 Provider response。历史 1.0.0 Run 明确请求
        四字段/六码 v1，1.0.1 起新 Run 请求 v1.1 的完整错误合同。
        """
        if not context:
            return _DEFAULT_TOOL_WIRE_VERSION
        version = _TOOL_WIRE_VERSION_BY_AGENT_VERSION.get(context.get("agent_version", ""))
        if version is None:
            # agent_version 是经过格式校验的受控标识（非正文），允许入日志。
            # 该 raise 发生在发送前且历史上两次造成"瞬时 WORKFLOW_NODE_FAILED
            # 且无任何工具日志"的静默故障（升包未登记 / 旧 Worker 残留进程），
            # 这行日志是排查该症状的唯一线索，不能省。
            logging.warning(
                "Tool wire 版本未登记，发送前中止 agent_version=%s",
                context.get("agent_version", ""),
            )
            raise ValueError("TOOL_WIRE_VERSION_INVALID")
        return version

    def _validated_tool_context(
        self,
        tool_context: Mapping[str, str] | None,
        input_data: Mapping[str, Any],
        tool_name: str,
    ) -> dict[str, str]:
        """在真实发送前验证冻结 7 字段，阻断 context 伪造和 run 漂移。"""
        # 网络隔离单元测试使用 MockTransport；生产 HTTP transport 不存在此分支。
        if tool_context is None and self._is_mock_transport:
            return {}
        required = {"agent_id", "agent_version", "run_id", "step_id", "business_type", "business_id", "trace_id"}
        if not isinstance(tool_context, Mapping) or set(tool_context) != required:
            raise ValueError("TOOL_CONTEXT_INVALID")
        context = dict(tool_context)
        if any(not isinstance(value, str) or not value for value in context.values()):
            raise ValueError("TOOL_CONTEXT_INVALID")
        if context["run_id"] != input_data.get("run_id"):
            raise ValueError("TOOL_CONTEXT_RUN_ID_MISMATCH")
        if context["business_id"] != input_data.get("archive_id"):
            raise ValueError("TOOL_CONTEXT_BUSINESS_ID_MISMATCH")
        if context["agent_id"] != "memoir_agent" or context["business_type"] != "couple_memory":
            raise ValueError("TOOL_CONTEXT_TRUST_INVALID")
        if not re.fullmatch(r"\d+\.\d+\.\d+", context["agent_version"]):
            raise ValueError("TOOL_CONTEXT_TRUST_INVALID")
        if not tool_name.startswith("memory."):
            raise ValueError("TOOL_CONTEXT_TRUST_INVALID")
        return context

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
