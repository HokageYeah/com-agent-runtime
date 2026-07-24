"""情侣日记业务侧调用 AgentRuntime held Run API 的最小 HMAC 适配器。"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.services.memory_runtime_capability_cache import (
    MemoryRuntimeCapabilityCache,
    RuntimeCapabilityError,
    RuntimeCapabilitySnapshot,
)
from app.services.memory_runtime_launch_service import RuntimeHeldRun


class MemoryRuntimeAdapterError(ValueError):
    """HTTP、协议或兼容性失败的安全错误；不携带上游响应正文。"""


@dataclass(frozen=True)
class MemoryRuntimeClientConfig:
    """仅供服务端注入的 Runtime 连接配置，严禁写入日志或接口响应。"""

    base_url: str
    client_id: str
    key_id: str
    secret: str
    timeout_seconds: float = 5.0
    capability_ttl_seconds: int = 60


@dataclass(frozen=True)
class RuntimeRunState:
    """业务补偿可读取的按 Run ID 状态，不承载 Runtime 输入或执行数据。"""

    run_id: str
    status: str
    dispatch_state: str
    privacy_state: str
    privacy_version: int
    last_event_seq: int
    status_version: int


class MemoryAgentAdapter:
    """先验证 Runtime 能力，再以 held/create/start 两阶段启动回忆录 Run。"""

    def __init__(self, config: MemoryRuntimeClientConfig, client: httpx.Client) -> None:
        self._config = config
        self._client = client
        self._cache = MemoryRuntimeCapabilityCache(
            ttl_seconds=config.capability_ttl_seconds,
            required_policies={"emotional_writing", "strict"},
        )

    def create_held(
        self, *, archive_id: str, snapshot_id: str, generation_epoch: int,
        idempotency_key: str,
    ) -> RuntimeHeldRun:
        """创建 held Run；请求输入只包含冻结资源定位符。"""
        try:
            self._cache.get_or_refresh(
                self._fetch_capabilities, probe=self._fetch_capability_summary,
            )
        except RuntimeCapabilityError as exc:
            raise MemoryRuntimeAdapterError(str(exc)) from exc
        payload = {
            "agent_id": "memoir_agent", "agent_version": "1.0.0",
            "business_type": "couple_memory", "business_id": archive_id,
            "start_mode": "held",
            "input": {"archive_id": archive_id, "snapshot_id": snapshot_id,
                      "generation_epoch": generation_epoch},
            "callback_target_id": "memory_callback",
            "business_connector_id": "couple_diary_backend",
            "data_domain": "couple_memory",
        }
        data = self._request("POST", "/api/v1/runtime/agent-runs", payload, idempotency_key)
        if data is None:
            raise MemoryRuntimeAdapterError("MEMORY_RUNTIME_CREATE_RESPONSE_INVALID")
        required = ("run_id", "contract_version", "package_digest", "authorization_version")
        if not all(isinstance(data.get(key), (str, int)) for key in required):
            raise MemoryRuntimeAdapterError("MEMORY_RUNTIME_CREATE_RESPONSE_INVALID")
        if not all(isinstance(data[key], str) for key in required[:3]) or not isinstance(data["authorization_version"], int):
            raise MemoryRuntimeAdapterError("MEMORY_RUNTIME_CREATE_RESPONSE_INVALID")
        return RuntimeHeldRun(
            run_id=data["run_id"], contract_version=data["contract_version"],
            package_digest=data["package_digest"], authorization_version=data["authorization_version"],
        )

    def start_held(self, *, run_id: str, idempotency_key: str) -> None:
        """启动已由业务事务绑定的 held Run。"""
        self._request("POST", f"/api/v1/runtime/agent-runs/{run_id}/start", {}, idempotency_key)

    def get_run_summary(self, run_id: str) -> RuntimeHeldRun | None:
        """查询孤儿 held Run 的最小安全摘要；404 表示 Runtime 已不存在该 Run。

        查询结果只用于恢复既有绑定，禁止把 Runtime 的输入、步骤或错误正文带回业务库。
        """
        data = self._request(
            "GET", f"/api/v1/runtime/agent-runs/{run_id}", allow_not_found=True,
        )
        if data is None:
            return None
        required = ("run_id", "contract_version", "package_digest", "authorization_version")
        if not all(isinstance(data.get(key), (str, int)) for key in required):
            raise MemoryRuntimeAdapterError("MEMORY_RUNTIME_GET_RESPONSE_INVALID")
        if (
            not all(isinstance(data[key], str) for key in required[:3])
            or not isinstance(data["authorization_version"], int)
        ):
            raise MemoryRuntimeAdapterError("MEMORY_RUNTIME_GET_RESPONSE_INVALID")
        return RuntimeHeldRun(
            run_id=data["run_id"], contract_version=data["contract_version"],
            package_digest=data["package_digest"],
            authorization_version=data["authorization_version"],
        )

    def cancel_run(self, run_id: str, idempotency_key: str) -> None:
        """取消已被业务代次淘汰的 held Run；原因仅使用固定安全码。"""
        self._request(
            "POST", f"/api/v1/runtime/agent-runs/{run_id}/cancel",
            {"reason_code": "MEMORY_BINDING_SUPERSEDED"}, idempotency_key,
        )

    def retry_run(self, run_id: str, idempotency_key: str) -> None:
        """请求 Runtime 按原 Run 的 checkpoint 执行人工重试。

        Runtime 是 checkpoint、Package 撤销状态和三次人工重试额度的权威来源；
        业务侧只传递已持久化的稳定幂等键，绝不复制 checkpoint 或私密状态。
        """
        data = self._request(
            "POST", f"/api/v1/runtime/agent-runs/{run_id}/retry", {}, idempotency_key,
        )
        if data is None:
            raise MemoryRuntimeAdapterError("MEMORY_RUNTIME_RETRY_RESPONSE_INVALID")
        if data.get("run_id") != run_id:
            raise MemoryRuntimeAdapterError("MEMORY_RUNTIME_RETRY_RESPONSE_INVALID")

    def request_private_purge(self, run_id: str, idempotency_key: str) -> None:
        """请求 Runtime 建立 privacy tombstone；完成状态必须另行查询确认。"""
        data = self._request(
            "POST",
            f"/api/v1/runtime/agent-runs/{run_id}/purge-private-data",
            {},
            idempotency_key,
        )
        if data is None:
            raise MemoryRuntimeAdapterError("MEMORY_RUNTIME_PURGE_RESPONSE_INVALID")
        if (
            data.get("run_id") != run_id
            or data.get("privacy_state") not in {"purge_requested", "purged"}
        ):
            raise MemoryRuntimeAdapterError("MEMORY_RUNTIME_PURGE_RESPONSE_INVALID")

    def get_privacy_state(self, run_id: str) -> str | None:
        """读取 purge 对账所需的唯一状态字段，不带回 Runtime 输入或步骤内容。"""
        data = self._request(
            "GET", f"/api/v1/runtime/agent-runs/{run_id}", allow_not_found=True,
        )
        if data is None:
            return None
        privacy_state = data.get("privacy_state")
        if data.get("run_id") != run_id or privacy_state not in {
            "active", "purge_requested", "purged",
        }:
            raise MemoryRuntimeAdapterError("MEMORY_RUNTIME_PRIVACY_QUERY_INVALID")
        return privacy_state

    def get_run_state(self, run_id: str) -> RuntimeRunState | None:
        """为业务状态与 purge 补偿提供单次受限的 Run ID 兜底查询。"""
        data = self._request(
            "GET", f"/api/v1/runtime/agent-runs/{run_id}", allow_not_found=True,
        )
        if data is None:
            return None
        status = data.get("status")
        dispatch_state = data.get("dispatch_state")
        privacy_state = data.get("privacy_state")
        privacy_version = data.get("privacy_version")
        last_event_seq = data.get("last_event_seq")
        status_version = data.get("status_version")
        allowed_statuses = {
            "pending", "running", "waiting_human", "succeeded", "failed", "cancelled",
        }
        allowed_dispatch_states = {"held", "queued", "claimed", "finished"}
        allowed_privacy_states = {"active", "purge_requested", "purged"}
        if (
            data.get("run_id") != run_id
            or status not in allowed_statuses
            or dispatch_state not in allowed_dispatch_states
            or privacy_state not in allowed_privacy_states
            or isinstance(privacy_version, bool)
            or not isinstance(privacy_version, int)
            or privacy_version < 1
            or isinstance(last_event_seq, bool)
            or not isinstance(last_event_seq, int)
            or last_event_seq < 0
            or isinstance(status_version, bool)
            or not isinstance(status_version, int)
            or status_version < 1
        ):
            raise MemoryRuntimeAdapterError("MEMORY_RUNTIME_STATE_QUERY_INVALID")
        return RuntimeRunState(
            run_id=run_id,
            status=status,
            dispatch_state=dispatch_state,
            privacy_state=privacy_state,
            privacy_version=privacy_version,
            last_event_seq=last_event_seq,
            status_version=status_version,
        )

    def close(self) -> None:
        """释放注入的 HTTP 连接，供单次 launcher 结束时调用。"""
        self._client.close()

    def _fetch_capabilities(self) -> RuntimeCapabilitySnapshot:
        """readiness 与 capability 必须同时满足，draining 或协议漂移均 fail closed。"""
        ready = self._request("GET", "/api/v1/runtime/health/ready")
        if ready is None:
            raise RuntimeCapabilityError("MEMORY_RUNTIME_CAPABILITY_INCOMPATIBLE")
        if ready.get("status") != "ready":
            raise RuntimeCapabilityError("MEMORY_RUNTIME_CAPABILITY_INCOMPATIBLE")
        data = self._request("GET", "/api/v1/runtime/capabilities")
        if data is None:
            raise RuntimeCapabilityError("MEMORY_RUNTIME_CAPABILITY_INCOMPATIBLE")
        return self._capability_snapshot(data)

    def _fetch_capability_summary(self) -> RuntimeCapabilitySnapshot:
        """在 TTL 内探测无敏感版本摘要，变化时由缓存触发完整握手。"""
        data = self._request("GET", "/api/v1/runtime/capabilities")
        if data is None:
            raise RuntimeCapabilityError("MEMORY_RUNTIME_CAPABILITY_INCOMPATIBLE")
        return self._capability_snapshot(data)

    def _capability_snapshot(self, data: dict[str, Any]) -> RuntimeCapabilitySnapshot:
        """把 Runtime 安全能力响应规整为可比较的内存摘要。"""
        agents = data.get("agents")
        policies = data.get("model_policies")
        capabilities = data.get("capabilities")
        if not isinstance(agents, list) or not isinstance(policies, list) or not isinstance(capabilities, dict):
            raise RuntimeCapabilityError("MEMORY_RUNTIME_CAPABILITY_INCOMPATIBLE")
        parsed_agent_versions: set[tuple[str, str]] = set()
        for item in agents:
            if not isinstance(item, dict):
                continue
            agent_id, version = item.get("agent_id"), item.get("version")
            if isinstance(agent_id, str) and isinstance(version, str):
                parsed_agent_versions.add((agent_id, version))
        agent_versions = frozenset(parsed_agent_versions)
        digest = data.get("package_digest")
        if not isinstance(digest, str):
            raise RuntimeCapabilityError("MEMORY_RUNTIME_CAPABILITY_INCOMPATIBLE")
        return RuntimeCapabilitySnapshot(
            contract_version=data.get("contract_version", ""), package_digest=digest,
            agent_versions=agent_versions,
            model_policies=frozenset(item for item in policies if isinstance(item, str)),
            workflow_agent=capabilities.get("workflow_agent") is True,
            expires_at=self._cache._clock(),
        )

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None, allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        """按 Runtime 固定 canonical HMAC 发请求；错误不读取或记录响应正文。"""
        body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        canonical = f"{method}\n{path}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}".encode()
        headers = {"X-Agent-Client-Id": self._config.client_id,
                   "X-Agent-Key-Id": self._config.key_id,
                   "X-Agent-Timestamp": timestamp,
                   "X-Agent-Signature": hmac.new(self._config.secret.encode(), canonical, hashlib.sha256).hexdigest()}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._client.request(method, self._config.base_url.rstrip("/") + path, content=body, headers=headers, timeout=self._config.timeout_seconds)
            # 404 只对 GET 恢复查询表示“可安全忽略”；不得解析其错误正文。
            if allow_not_found and response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MemoryRuntimeAdapterError("MEMORY_RUNTIME_REQUEST_FAILED") from exc
        if not isinstance(data, dict):
            raise MemoryRuntimeAdapterError("MEMORY_RUNTIME_RESPONSE_INVALID")
        return data
