"""可信模型路由和跨 Worker 的 Redis 流量 permit。

此模块只记录 route/permit 等非内容标识，绝不能把 prompt 或 Provider 正文写入日志。
"""

from __future__ import annotations

import ipaddress
import logging
import math
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.runtime.interfaces import (
    LeaseContext,
    NullTrafficEventRecorder,
    TrafficEventRecorder,
)
from app.runtime.policy_engine import PolicyEngine
from app.runtime.prompt_registry import PromptDefinition

_CONTEXT_FACTORY = object()


class RedisEvaluator(Protocol):
    """兼容 redis-py 同步客户端的最小接口。"""

    def eval(self, script: str, numkeys: int, *args: object) -> object: ...


@dataclass(frozen=True)
class ModelRoute:
    """只可由服务端配置注册的模型路由。"""

    route_id: str
    provider: str
    model: str
    endpoint: str
    rate_limit_key: str
    max_concurrency: int
    rpm_limit: int
    tpm_limit: int
    timeout_seconds: float
    permit_ttl_seconds: float
    settle_margin_seconds: float
    price_unit: str
    input_price: float
    output_price: float
    circuit_failure_threshold: int = 0
    circuit_open_seconds: float = 0.0
    # 以下字段仅由管理员部署配置提供；不从 prompt、业务请求或模型输出读取。
    route_config_version: str = "v1"
    pricing_config_version: str = "v1"
    capabilities: frozenset[str] = frozenset({"structured_output"})
    data_residency: str = "public"
    max_context_tokens: int = 8192
    max_output_tokens: int = 4096
    enabled: bool = True
    # 空集合只用于直接构造的兼容测试；部署 JSON 必须显式提供租户与逻辑 policy。
    allowed_tenant_ids: frozenset[str] = frozenset()
    allowed_model_policies: frozenset[str] = frozenset()
    # 仅允许部署配置的候选 route；为空表示失败后直接交给节点模板 fallback。
    fallback_route_id: str | None = None

    def __post_init__(self) -> None:
        required = (self.route_id, self.provider, self.model, self.rate_limit_key)
        if any(not value or not value.strip() for value in required):
            raise ValueError("route_id/provider/model/rate_limit_key 不能为空")
        parsed_endpoint = urlsplit(self.endpoint)
        if (
            parsed_endpoint.scheme not in {"http", "https"}
            or not parsed_endpoint.netloc
            or parsed_endpoint.username is not None
            or parsed_endpoint.password is not None
            or parsed_endpoint.query
            or parsed_endpoint.fragment
        ):
            raise ValueError("endpoint 必须是不含凭据、查询参数或片段的 HTTP(S) 地址")
        _reject_unsafe_endpoint_host(parsed_endpoint.hostname, "MODEL_ENDPOINT_UNSAFE")
        if min(self.max_concurrency, self.rpm_limit, self.tpm_limit) <= 0:
            raise ValueError("并发、RPM 和 TPM 上限必须为正数")
        if self.timeout_seconds <= 0 or self.settle_margin_seconds < 0:
            raise ValueError("timeout_seconds 必须为正数且 settle_margin_seconds 不可为负")
        if self.permit_ttl_seconds < self.timeout_seconds + self.settle_margin_seconds:
            raise ValueError("permit_ttl_seconds 必须覆盖 timeout_seconds 与 settle_margin_seconds")
        if self.price_unit != "usd_per_1k_tokens":
            raise ValueError("price_unit 必须为 usd_per_1k_tokens")
        if any(not math.isfinite(price) or price < 0 for price in (self.input_price, self.output_price)):
            raise ValueError("价格必须是非负有限数")
        if isinstance(self.circuit_failure_threshold, bool) or not isinstance(
            self.circuit_failure_threshold, int
        ) or self.circuit_failure_threshold < 0:
            raise ValueError("circuit_failure_threshold 必须是非负整数")
        if isinstance(self.circuit_open_seconds, bool) or not isinstance(
            self.circuit_open_seconds, (int, float)
        ) or not math.isfinite(self.circuit_open_seconds) or self.circuit_open_seconds < 0:
            raise ValueError("circuit_open_seconds 必须是非负有限数")
        if self.circuit_failure_threshold == 0 and self.circuit_open_seconds != 0:
            raise ValueError("未启用熔断时 circuit_open_seconds 必须为 0")
        if self.circuit_failure_threshold > 0 and self.circuit_open_seconds <= 0:
            raise ValueError("启用熔断时 circuit_open_seconds 必须为正数")
        if not self.route_config_version or not self.pricing_config_version:
            raise ValueError("route_config_version 与 pricing_config_version 不能为空")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        for field_name, values in (
            ("allowed_tenant_ids", self.allowed_tenant_ids),
            ("allowed_model_policies", self.allowed_model_policies),
        ):
            if (
                not isinstance(values, frozenset)
                or any(not isinstance(value, str) or not value for value in values)
            ):
                raise ValueError(f"{field_name} 必须是字符串集合")
        if self.fallback_route_id is not None and (
            not isinstance(self.fallback_route_id, str)
            or not self.fallback_route_id
            or self.fallback_route_id == self.route_id
        ):
            raise ValueError("fallback_route_id 必须是不同的非空 route ID")
        if self.data_residency not in {"public", "private"}:
            raise ValueError("data_residency 仅允许 public 或 private")
        if (
            not isinstance(self.capabilities, frozenset)
            or not self.capabilities
            or any(not isinstance(capability, str) or not capability for capability in self.capabilities)
        ):
            raise ValueError("capabilities 必须是非空字符串集合")
        if "private_residency" in self.capabilities and self.data_residency != "private":
            raise ValueError("private_residency capability 必须使用 private data_residency")
        if (
            isinstance(self.max_context_tokens, bool)
            or isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_context_tokens, int)
            or not isinstance(self.max_output_tokens, int)
            or self.max_context_tokens <= 0
            or self.max_output_tokens <= 0
            or self.max_output_tokens > self.max_context_tokens
        ):
            raise ValueError("上下文与输出 token 上限必须为正整数且输出不可超过上下文")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ModelRoute:
        """从已解析的服务端 JSON 构造 route，不接受业务请求中的配置。"""
        route = dict(data)
        capabilities = route.get("capabilities")
        if not isinstance(capabilities, list):
            raise ValueError("部署 route 必须显式配置 capabilities 数组")
        route["capabilities"] = frozenset(capabilities)
        for field in ("allowed_tenant_ids", "allowed_model_policies"):
            values = route.get(field)
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) or not value for value in values)
            ):
                raise ValueError(f"部署 route 必须显式配置非空 {field} 数组")
            route[field] = frozenset(values)
        required = {
            "route_config_version", "pricing_config_version", "data_residency",
            "max_context_tokens", "max_output_tokens", "enabled",
        }
        if missing := sorted(required - set(route)):
            raise ValueError(f"部署 route 缺少治理字段: {','.join(missing)}")
        return cls(**route)


@dataclass(frozen=True)
class ModelPolicy:
    """逻辑模型策略的最小可审计定义，不包含 Provider 或任何凭据。"""

    name: str
    max_output_tokens: int
    required_capabilities: frozenset[str]
    fallback: str
    thinking_enabled: bool
    requires_vision: bool


class ModelPolicyRegistry:
    """从部署内 YAML 加载固定策略；缺项即禁用对应模型增强能力。"""

    _FALLBACKS = {"template"}

    def __init__(self, policies: Mapping[str, object]) -> None:
        parsed: dict[str, ModelPolicy] = {}
        for name, raw_policy in policies.items():
            if not isinstance(name, str) or not isinstance(raw_policy, Mapping):
                raise ValueError("MODEL_POLICY_INVALID")
            max_output_tokens = raw_policy.get("max_output_tokens")
            capabilities = raw_policy.get("required_capabilities")
            fallback = raw_policy.get("fallback")
            thinking_enabled = raw_policy.get("thinking_enabled")
            requires_vision = raw_policy.get("requires_vision")
            if (
                isinstance(max_output_tokens, bool)
                or not isinstance(max_output_tokens, int)
                or max_output_tokens <= 0
                or not isinstance(capabilities, list)
                or not capabilities
                or any(not isinstance(item, str) or not item for item in capabilities)
                or fallback not in self._FALLBACKS
                or not isinstance(thinking_enabled, bool)
                or not isinstance(requires_vision, bool)
            ):
                raise ValueError("MODEL_POLICY_INVALID")
            parsed[name] = ModelPolicy(
                name=name,
                max_output_tokens=max_output_tokens,
                required_capabilities=frozenset(capabilities),
                fallback=fallback,
                thinking_enabled=thinking_enabled,
                requires_vision=requires_vision,
            )
        self._policies = parsed

    @classmethod
    def from_yaml(cls, path: Path) -> ModelPolicyRegistry:
        """读取受控 YAML；解析失败不回退到任意默认策略。"""
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError("MODEL_POLICY_INVALID") from exc
        if not isinstance(raw, Mapping):
            raise ValueError("MODEL_POLICY_INVALID")
        return cls(raw)

    @classmethod
    def default(cls) -> ModelPolicyRegistry:
        """加载仓库内版本化策略，供 Worker 在启动时冻结到 Gateway 实例。"""
        return cls.from_yaml(Path(__file__).parents[1] / "core" / "model_policy.yaml")

    def get(self, name: str) -> ModelPolicy:
        try:
            return self._policies[name]
        except KeyError as exc:
            raise ValueError("MODEL_POLICY_UNAVAILABLE") from exc

    def values(self) -> tuple[ModelPolicy, ...]:
        """返回冻结的逻辑策略，不暴露任何 Provider 配置。"""
        return tuple(self._policies.values())


class ModelRouteRegistry:
    """注册时拒绝重复 ID，避免请求通过 route 覆盖安全边界。"""

    def __init__(self, routes: list[ModelRoute]) -> None:
        self._routes = {route.route_id: route for route in routes}
        if len(self._routes) != len(routes):
            raise ValueError("MODEL_ROUTES_JSON 存在重复 route_id")
        for route in routes:
            if route.fallback_route_id and route.fallback_route_id not in self._routes:
                raise ValueError("fallback_route_id 必须引用已注册 route")

    def get(self, route_id: str) -> ModelRoute:
        try:
            return self._routes[route_id]
        except KeyError as exc:
            raise ValueError("MODEL_ROUTE_UNAVAILABLE") from exc

    @classmethod
    def from_config(cls, configured_routes: list[Mapping[str, Any]]) -> ModelRouteRegistry:
        return cls([ModelRoute.from_mapping(route) for route in configured_routes])


class ModelCapabilityEvaluator:
    """无副作用地计算可信 Prompt 能否使用受控 route。

    Redis 可用性由调用边界探测后以布尔值传入，避免能力计算本身触网或泄露 route 细节。
    """

    def __init__(self, policies: ModelPolicyRegistry) -> None:
        self._policies = policies

    def available(
        self,
        route: ModelRoute,
        prompt: PromptDefinition | None,
        *,
        estimated_input_tokens: int,
        redis_available: bool,
    ) -> bool:
        if not redis_available or prompt is None or estimated_input_tokens < 0:
            return False
        try:
            policies = (
                self._policies.get(prompt.model_policy),
                self._policies.get(prompt.guardrail_policy),
            )
        except ValueError:
            return False
        required_capabilities = frozenset().union(
            *(policy.required_capabilities for policy in policies),
        )
        model_policy = policies[0]
        if model_policy.thinking_enabled:
            required_capabilities |= {"thinking"}
        if model_policy.requires_vision:
            required_capabilities |= {"vision"}
        return (
            route.enabled
            and (
                not route.allowed_model_policies
                or model_policy.name in route.allowed_model_policies
            )
            and required_capabilities.issubset(route.capabilities)
            and ("private_residency" not in required_capabilities or route.data_residency == "private")
            and model_policy.max_output_tokens <= route.max_output_tokens
            and estimated_input_tokens + model_policy.max_output_tokens <= route.max_context_tokens
        )

    def available_policy_names(
        self, routes: tuple[ModelRoute, ...], *, redis_available: bool,
    ) -> list[str]:
        """生成可公开的逻辑策略名，不返回 route、Provider 或端点。"""
        if not redis_available:
            return []
        return sorted(
            policy.name
            for policy in self._policies.values()
            if any(
                route.enabled
                and (
                    not route.allowed_model_policies
                    or policy.name in route.allowed_model_policies
                )
                and policy.required_capabilities.issubset(route.capabilities)
                and ("private_residency" not in policy.required_capabilities or route.data_residency == "private")
                and policy.max_output_tokens <= route.max_output_tokens
                and policy.max_output_tokens <= route.max_context_tokens
                and (not policy.thinking_enabled or "thinking" in route.capabilities)
                and (not policy.requires_vision or "vision" in route.capabilities)
                for route in routes
            )
        )


@dataclass(frozen=True)
class PermitResult:
    status: str
    retry_after_seconds: float = 0.0

    @property
    def granted(self) -> bool:
        return self.status == "acquired"


@dataclass(frozen=True, init=False)
class ModelCallContext:
    """仅由权威 Run/Step 与有效 lease 派生的调用身份。"""

    run_id: str
    step_id: str
    model_attempt: int
    lease_context: LeaseContext
    estimated_input_tokens: int = 0
    request_deadline_at: datetime | None = None
    allowed_route_ids: frozenset[str] = frozenset()
    tenant_id: str = ""
    required_data_residency: str | None = None

    def __init__(
        self,
        *,
        _factory: object,
        run_id: str,
        step_id: str,
        model_attempt: int,
        lease_context: LeaseContext,
        estimated_input_tokens: int,
        request_deadline_at: datetime | None,
        allowed_route_ids: frozenset[str],
        tenant_id: str,
        required_data_residency: str | None,
    ) -> None:
        if _factory is not _CONTEXT_FACTORY:
            raise TypeError("ModelCallContext 必须由 from_authoritative 构造")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "step_id", step_id)
        object.__setattr__(self, "model_attempt", model_attempt)
        object.__setattr__(self, "lease_context", lease_context)
        object.__setattr__(self, "estimated_input_tokens", estimated_input_tokens)
        object.__setattr__(self, "request_deadline_at", request_deadline_at)
        object.__setattr__(self, "allowed_route_ids", allowed_route_ids)
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(self, "required_data_residency", required_data_residency)

    @classmethod
    def from_authoritative(
        cls,
        session: Session,
        run_id: str,
        step_id: str,
        lease_context: LeaseContext,
    ) -> ModelCallContext:
        """从数据库中运行中的 Step 派生，不接受调用方提供 attempt/deadline/预算。"""
        # 延迟导入避免 Settings 校验 server-side route 时与 ORM Base 初始化循环。
        from app.models import AgentRun, AgentStep

        run = session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        step = session.scalar(
            select(AgentStep).where(AgentStep.step_id == step_id, AgentStep.run_id == run_id)
        )
        if (
            run is None
            or step is None
            or step.status != "running"
            or step.execution_attempt != lease_context.execution_attempt
            or run.execution_attempt != lease_context.execution_attempt
            or run.lease_owner != lease_context.lease_owner
            or run.fencing_token != lease_context.fencing_token
        ):
            raise ValueError("MODEL_CALL_CONTEXT_UNTRUSTED")
        summary = step.input_summary if isinstance(step.input_summary, Mapping) else {}
        estimated_input_tokens = summary.get("estimated_input_tokens", 0)
        if isinstance(estimated_input_tokens, bool) or not isinstance(estimated_input_tokens, int):
            estimated_input_tokens = 0
        if estimated_input_tokens < 0:
            estimated_input_tokens = 0
        request_deadline_at = run.run_deadline_at
        snapshot = run.capability_snapshot_json
        execution_policy = (
            snapshot.get("execution_policy") if isinstance(snapshot, Mapping) else None
        )
        max_run_seconds = (
            execution_policy.get("max_run_seconds")
            if isinstance(execution_policy, Mapping)
            else None
        )
        active_elapsed_ms = run.active_elapsed_ms
        if (
            isinstance(max_run_seconds, int)
            and not isinstance(max_run_seconds, bool)
            and max_run_seconds >= 0
            and isinstance(active_elapsed_ms, int)
            and not isinstance(active_elapsed_ms, bool)
        ):
            # 活跃时间只由 Executor 在节点边界累计；这里将剩余额度折算为本次
            # Provider permit、等待和 HTTP 可共同使用的最短 deadline。
            remaining_ms = max_run_seconds * 1000 - active_elapsed_ms
            active_deadline = datetime.now(UTC) + timedelta(milliseconds=max(0, remaining_ms))
            if request_deadline_at is None or cls._as_utc(request_deadline_at) > active_deadline:
                request_deadline_at = active_deadline
        return cls(
            _factory=_CONTEXT_FACTORY,
            run_id=run_id,
            step_id=step_id,
            model_attempt=step.step_attempt,
            lease_context=lease_context,
            estimated_input_tokens=estimated_input_tokens,
            request_deadline_at=request_deadline_at,
            allowed_route_ids=cls.allowed_routes_from_snapshot(run.capability_snapshot_json),
            tenant_id=run.tenant_id,
            required_data_residency=cls.required_residency_from_snapshot(
                run.capability_snapshot_json
            ),
        )

    @classmethod
    def with_minimum_estimated_input_tokens(
        cls,
        context: ModelCallContext,
        minimum_tokens: int,
    ) -> ModelCallContext:
        """从既有权威上下文派生更保守的输入预留，禁止调用方降低原估算。

        repair 请求包含首次候选的有界 untrusted data；它必须按实际短生命周期
        Provider request 提高 TPM/成本预留，但不能改写 lease、路由或授权身份。
        """
        if (
            isinstance(minimum_tokens, bool)
            or not isinstance(minimum_tokens, int)
            or minimum_tokens < 0
        ):
            raise ValueError("MODEL_INPUT_TOKEN_ESTIMATE_INVALID")
        return cls(
            _factory=_CONTEXT_FACTORY,
            run_id=context.run_id,
            step_id=context.step_id,
            model_attempt=context.model_attempt,
            lease_context=context.lease_context,
            estimated_input_tokens=max(
                context.estimated_input_tokens,
                minimum_tokens,
            ),
            request_deadline_at=context.request_deadline_at,
            allowed_route_ids=context.allowed_route_ids,
            tenant_id=context.tenant_id,
            required_data_residency=context.required_data_residency,
        )

    @staticmethod
    def allowed_routes_from_snapshot(snapshot: object) -> frozenset[str]:
        """只接受 Run 冻结快照中的安全 route ID 列表；缺失/畸形一律拒绝。"""
        if not isinstance(snapshot, Mapping):
            return frozenset()
        route_ids = snapshot.get("allowed_model_route_ids")
        if not isinstance(route_ids, list) or any(
            not isinstance(route_id, str) or not route_id for route_id in route_ids
        ):
            return frozenset()
        return frozenset(route_ids)

    @staticmethod
    def required_residency_from_snapshot(snapshot: object) -> str | None:
        """驻留约束只能来自创建 Run 时冻结的服务端能力快照。"""
        if not isinstance(snapshot, Mapping):
            return None
        value = snapshot.get("required_model_data_residency")
        return value if value in {"public", "private"} else None

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """兼容 SQLite 返回的无时区时间，统一用于可信 deadline 比较。"""
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ProviderAdapter(Protocol):
    """Provider 请求的唯一内容边界；Gateway 不记录传入或返回的正文。"""

    def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object: ...


# OpenAI 兼容 Provider 标识：请求自动补 model 字段并解包 choices[0].message.content。
# 其余 provider（如内部 harness）保持原有"响应体即结构化 JSON"契约，零行为变化。
OPENAI_COMPATIBLE_PROVIDER = "openai_compatible"


class HttpProviderAdapter:
    """模型 Provider 的 HTTP 边界，发送后校验真实 TCP 对端以阻断 DNS rebinding。"""

    def __init__(
        self,
        client: httpx.Client,
        *,
        peer_ip_provider: Callable[[], str | None] | None = None,
        reset_peer_ip: Callable[[], None] | None = None,
        api_keys: Mapping[str, str] | None = None,
    ) -> None:
        """注入受控 HTTP Client；生产调用必须同时提供对端 IP 读取器。

        api_keys 是部署 env 提供的 route_id -> API Key 映射，仅进入请求头，
        绝不进入日志、异常消息或响应处理。
        """
        self._client = client
        self._peer_ip_provider = peer_ip_provider
        self._reset_peer_ip = reset_peer_ip
        self._api_keys = dict(api_keys or {})

    def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object:
        """物理发送前重做 DNS 预检，并在响应解析前核对 socket 实际对端。"""
        allowed_peer_ips = _ensure_public_endpoint(route.endpoint, "MODEL_PROVIDER_ENDPOINT")
        if self._peer_ip_provider is None:
            # Provider 响应属于不可信输入；没有真实 socket 地址时必须 fail-closed。
            logging.warning("模型 Provider 对端地址不可验证 route_id=%s code=MODEL_PROVIDER_PEER_UNVERIFIABLE", route.route_id)
            raise ValueError("MODEL_PROVIDER_PEER_UNVERIFIABLE")
        if self._reset_peer_ip is not None:
            self._reset_peer_ip()
        headers: dict[str, str] = {}
        api_key = self._api_keys.get(route.route_id)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        outbound: object = request
        if route.provider == OPENAI_COMPATIBLE_PROVIDER and isinstance(request, Mapping):
            # OpenAI 兼容供应商要求 body 携带 model；model 固定取部署 route 配置，
            # 请求本身没有任何覆盖 provider/model 的入口。
            body = dict(request)
            body.setdefault("model", route.model)
            outbound = body
        response = self._client.post(
            route.endpoint,
            json=outbound,
            headers=headers,
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        self._verify_connected_peer(allowed_peer_ips, route.endpoint, route.route_id)
        response.raise_for_status()
        payload = response.json()
        if route.provider == OPENAI_COMPATIBLE_PROVIDER:
            return self._extract_openai_content(payload)
        if not isinstance(payload, (dict, list)):
            raise ValueError("Provider JSON 响应格式无效")
        return payload

    @staticmethod
    def _extract_openai_content(payload: object) -> str:
        """从 OpenAI 兼容响应解包 choices[0].message.content 作为模型输出。

        只取 content 字符串交给结构化解析器；envelope 其余字段不进入 Runtime。
        注意：该路径下 token 计量不可得，账本按既有"未知 token"口径结算
        （与 harness 无 usage 响应同路径）；需要精确计量时再扩展 payload 契约。
        """
        if not isinstance(payload, Mapping):
            raise ValueError("Provider JSON 响应格式无效")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ValueError("Provider JSON 响应格式无效")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str):
            raise ValueError("Provider JSON 响应格式无效")
        return content

    def _verify_connected_peer(
        self, allowed_peer_ips: frozenset[str], endpoint: str, route_id: str
    ) -> None:
        """连接后校验真实 TCP 对端：公网 IP 且命中发送前/重解析 DNS 集合并集。

        不记录请求或响应内容；DNS/CDN 合法轮换通过一次连接后重解析容忍，
        其余情况（私网对端、集合外对端、重解析失败）全部 fail-closed。
        """
        assert self._peer_ip_provider is not None
        peer_ip = self._peer_ip_provider()
        try:
            normalized_peer = str(ipaddress.ip_address(peer_ip or ""))
        except ValueError as exc:
            logging.warning(
                "模型 Provider 对端地址不可验证 route_id=%s code=MODEL_PROVIDER_PEER_UNVERIFIABLE",
                route_id,
            )
            raise ValueError("MODEL_PROVIDER_PEER_UNVERIFIABLE") from exc
        # 对端必须本身是公网地址：私网/回环/保留地址不进入任何集合比对，
        # 立即拒绝，保持 DNS rebinding/SSRF 防线不变。
        if not ipaddress.ip_address(normalized_peer).is_global:
            logging.warning(
                "模型 Provider 对端不是公网地址 route_id=%s code=MODEL_PROVIDER_PEER_MISMATCH",
                route_id,
            )
            raise ValueError("MODEL_PROVIDER_PEER_MISMATCH")
        if normalized_peer in allowed_peer_ips:
            return
        # peer 未命中发送前快照：合法 Provider 可能在两次 DNS 查询之间轮换公网
        # 节点，对同一受信任 endpoint 立即补一次解析；仅当 peer 命中
        # “发送前集合 ∪ 重解析集合”才放行，不做自动重发（重试交给上层预算循环）。
        try:
            reresolved_peer_ips = _ensure_public_endpoint(endpoint, "MODEL_PROVIDER_ENDPOINT")
        except ValueError as exc:
            # 重解析失败（DNS 瞬时故障/新地址非公网）继续 fail-closed，
            # 统一按对端不匹配处理，不放宽安全边界。
            logging.warning(
                "模型 Provider 对端重解析失败 route_id=%s code=MODEL_PROVIDER_PEER_MISMATCH",
                route_id,
            )
            raise ValueError("MODEL_PROVIDER_PEER_MISMATCH") from exc
        if normalized_peer in reresolved_peer_ips:
            logging.info(
                "模型 Provider 对端命中连接后重解析集合 route_id=%s reason=dns_rotation",
                route_id,
            )
            return
        logging.warning(
            "模型 Provider 对端地址不匹配 route_id=%s code=MODEL_PROVIDER_PEER_MISMATCH",
            route_id,
        )
        raise ValueError("MODEL_PROVIDER_PEER_MISMATCH")


class LeaseBoundary(Protocol):
    def can_write(self, run_id: str, context: LeaseContext) -> bool: ...


class ModelCallGuard(Protocol):
    """Worker 生命周期边界：禁止 draining 后开启新的模型调用。"""

    def permits_new_call(self, context: ModelCallContext) -> bool: ...


class _AllowModelCalls:
    """非 Worker 调用保持现有默认行为。"""

    def permits_new_call(self, context: ModelCallContext) -> bool:
        return True


@dataclass(frozen=True)
class ModelGatewayResult:
    status: str
    data: object | None = None
    retry_after_seconds: float = 0.0
    error_code: str | None = None


class ModelGateway:
    """以 permit、usage、lease 三道边界保护实际 Provider HTTP 调用。"""

    def __init__(
        self,
        routes: ModelRouteRegistry,
        traffic: ProviderTrafficController,
        usage_service: Any,
        lease_service: LeaseBoundary,
        provider: ProviderAdapter,
        policy_engine: PolicyEngine,
        *,
        call_guard: ModelCallGuard | None = None,
        model_policies: ModelPolicyRegistry | None = None,
        capability_evaluator: ModelCapabilityEvaluator | None = None,
        traffic_event_recorder: TrafficEventRecorder | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._routes = routes
        self._traffic = traffic
        self._usage = usage_service
        self._lease = lease_service
        self._provider = provider
        self._policy = policy_engine
        self._call_guard = call_guard or _AllowModelCalls()
        self._model_policies = model_policies or ModelPolicyRegistry.default()
        self._capability_evaluator = capability_evaluator or ModelCapabilityEvaluator(self._model_policies)
        self._traffic_event_recorder = traffic_event_recorder or NullTrafficEventRecorder()
        self._sleep = sleep
        self._monotonic = monotonic

    def record_validation_rejection(self, route_id: str, error_codes: tuple[str, ...]) -> None:
        """记录模型结果的受控拒绝码；错误明细和模型输出均不进入流量账本。"""
        if not error_codes:
            return
        try:
            self._traffic_event_recorder.record(
                "semantic_validation_rejected", route_id, "SEMANTIC_VALIDATION_FAILED",
            )
            if {"FORBIDDEN_CONTROL_FIELD", "TOOL_PARAMETERS_FORBIDDEN"}.intersection(error_codes):
                self._traffic_event_recorder.record(
                    "prompt_injection_rejected", route_id, "INDIRECT_PROMPT_INJECTION",
                )
        except Exception:
            logging.warning("Runtime 语义拒绝流量账本写入失败 route_id=%s", route_id)

    def call(
        self,
        context: ModelCallContext,
        route_id: str,
        request: object,
        *,
        prompt: PromptDefinition | None = None,
    ) -> ModelGatewayResult:
        route = self._routes.get(route_id)
        attempted_route_ids: set[str] = set()
        while True:
            attempted_route_ids.add(route.route_id)
            result = self._call_route(context, route, request, prompt=prompt)
            if result.status == "rate_limited" and self._wait_within_execution_window(
                route, context, result.retry_after_seconds
            ):
                # 等待期间不持有 permit；重新执行会创建新的 usage/permit 并重新校验 lease。
                wait_started_at = self._monotonic()
                self._sleep(result.retry_after_seconds)
                waited_ms = max(0, int((self._monotonic() - wait_started_at) * 1000))
                if waited_ms:
                    self._policy.record_active_elapsed(context, waited_ms)
                result = self._call_route(context, route, request, prompt=prompt)
                if result.status != "rate_limited":
                    return result
            # 429 已写入主 route 的共享冷却；只有部署固定且 Run 快照显式允许的
            # fallback 才可新取 permit，绝不从业务输入推导另一个 Provider。
            fallback_id = route.fallback_route_id
            if (
                result.status != "rate_limited"
                or not fallback_id
                or fallback_id in attempted_route_ids
                or fallback_id not in context.allowed_route_ids
            ):
                return result
            logging.info(
                "模型调用使用部署 fallback primary_route_id=%s fallback_route_id=%s",
                route.route_id,
                fallback_id,
            )
            route = self._routes.get(fallback_id)

    def context_token_budget(self, route_id: str, prompt: PromptDefinition) -> int:
        """返回策略冻结后的输入窗口；不满足时在 Provider 前 fail-closed。"""
        route = self._routes.get(route_id)
        policy = self._model_policies.get(prompt.model_policy)
        if policy.max_output_tokens > route.max_output_tokens:
            raise ValueError("MODEL_CONTEXT_BUDGET_UNAVAILABLE")
        budget = route.max_context_tokens - policy.max_output_tokens
        if budget <= 0:
            raise ValueError("MODEL_CONTEXT_BUDGET_UNAVAILABLE")
        return budget

    def capability_available(
        self, route_id: str, prompt: PromptDefinition, estimated_input_tokens: int
    ) -> bool:
        """暴露不含 Provider 细节的 Prompt/route 能力判定，供调用前安全降级。"""
        route = self._routes.get(route_id)
        traffic_available = self._traffic.preflight_circuit(route).status == "circuit_available"
        return self._capability_evaluator.available(
            route, prompt, estimated_input_tokens=estimated_input_tokens,
            redis_available=traffic_available,
        )

    @classmethod
    def _wait_within_execution_window(
        cls,
        route: ModelRoute,
        context: ModelCallContext,
        retry_after_seconds: float,
    ) -> bool:
        """仅在共享冷却为正且不越过可信 deadline/lease 时允许一次同步等待。"""
        if retry_after_seconds <= 0:
            return False
        remaining = cls._effective_timeout(route, context)
        return remaining is not None and retry_after_seconds <= remaining

    def _call_route(
        self,
        context: ModelCallContext,
        route: ModelRoute,
        request: object,
        *,
        prompt: PromptDefinition | None,
    ) -> ModelGatewayResult:
        """执行单一受信任候选 route；每个候选独立创建 usage 与 Redis permit。"""
        route_id = route.route_id
        governance_error = self._route_governance_error(context, route, prompt)
        if governance_error == "MODEL_ROUTE_NOT_DEPLOYED":
            return ModelGatewayResult("route_not_allowed")
        if governance_error is not None:
            logging.info(
                "模型 route 治理拒绝 route_id=%s code=%s",
                route_id,
                governance_error,
            )
            return ModelGatewayResult("governance_denied", error_code=governance_error)
        if not self._route_supports_prompt(route, context.estimated_input_tokens, prompt):
            logging.info(
                "模型 route 能力不足 route_id=%s code=MODEL_CAPABILITY_UNAVAILABLE",
                route_id,
            )
            # Memoir Runner 只会对这个明确状态执行 policy 声明的模板 fallback。
            return ModelGatewayResult("capability_disabled", error_code="MODEL_CAPABILITY_UNAVAILABLE")
        # 每次实际模型调用前重新解析 endpoint。这里不缓存 DNS 结果，以免注册后
        # 域名被重新绑定至内网地址时绕过构造期的静态校验。
        try:
            _ensure_public_endpoint(route.endpoint, "MODEL_ENDPOINT")
        except ValueError as exc:
            logging.warning(
                "模型路由 endpoint 预检拒绝 route_id=%s code=%s",
                route_id,
                str(exc),
            )
            return ModelGatewayResult("endpoint_rejected", error_code=str(exc))
        # deadline 在 permit/usage 前拒绝，避免过期请求占用共享配额或触网。
        if self._deadline_expired(context):
            return ModelGatewayResult("aborted_before_send")
        # 熔断预检同样必须在 policy reservation 前执行，稳定打开时不留下 usage。
        circuit = self._traffic.preflight_circuit(route)
        if circuit.status == "redis_unavailable":
            logging.warning(
                "模型共享流控不可用 route_id=%s code=MODEL_TRAFFIC_UNAVAILABLE",
                route_id,
            )
            return ModelGatewayResult(
                "capability_disabled", error_code="MODEL_TRAFFIC_UNAVAILABLE"
            )
        if circuit.status != "circuit_available":
            logging.info(
                "模型熔断拒绝 run_id=%s step_id=%s route_id=%s status=%s",
                context.run_id, context.step_id, route_id, circuit.status,
            )
            return ModelGatewayResult(
                circuit.status, retry_after_seconds=circuit.retry_after_seconds,
            )
        # draining 后不再创建预算预留、Redis permit 或外部请求。
        if not self._permits_new_call(context, route_id):
            return ModelGatewayResult("aborted_before_send")
        # 策略必须在 permit、usage 与 Provider HTTP 前执行，拒绝不产生副作用。
        decision, usage_id = self._policy.reserve(context, route)
        if not decision.allowed:
            logging.info(
                "模型策略拒绝 run_id=%s step_id=%s route_id=%s code=%s",
                context.run_id, context.step_id, route_id, decision.code,
            )
            return ModelGatewayResult("policy_denied", error_code=decision.code)
        # 仅把受注册表校验过的 id/version 关联到预留 usage；禁止写入模板正文。
        if prompt is not None:
            attach_prompt_ref = getattr(self._usage, "attach_prompt_ref", None)
            attach_thinking_summary = getattr(self._usage, "attach_thinking_summary", None)
            model_policy = self._model_policies.get(prompt.model_policy)
            thinking_summary = {
                "thinking_enabled": model_policy.thinking_enabled,
                "max_output_tokens": model_policy.max_output_tokens,
                "input_token_budget": route.max_context_tokens - model_policy.max_output_tokens,
                "normalization_version": "v1",
            }
            if (
                not callable(attach_prompt_ref)
                or not callable(attach_thinking_summary)
                or not attach_prompt_ref(usage_id, prompt.prompt_id, prompt.version)
                or not attach_thinking_summary(usage_id, thinking_summary)
            ):
                self._usage.cancel_reservation(usage_id)
                return ModelGatewayResult("aborted_before_send")
        permit_id = str(uuid4())
        acquired = self._traffic.acquire(
            route, permit_id, estimated_tokens=context.estimated_input_tokens
        )
        if not acquired.granted:
            if usage_id is not None:
                self._usage.cancel_reservation(usage_id)
            if acquired.status == "redis_unavailable":
                logging.warning(
                    "模型共享流控不可用 route_id=%s code=MODEL_TRAFFIC_UNAVAILABLE",
                    route_id,
                )
                return ModelGatewayResult(
                    "capability_disabled", error_code="MODEL_TRAFFIC_UNAVAILABLE"
                )
            return ModelGatewayResult(acquired.status, retry_after_seconds=acquired.retry_after_seconds)
        if usage_id is None or not self._usage.activate_reservation(usage_id, permit_id):
            self._traffic.settle(route, permit_id)
            return ModelGatewayResult("aborted_before_send")
        return self._send_activated_attempt(
            context, route, request, usage_id, permit_id,
        )

    @staticmethod
    def _route_governance_error(
        context: ModelCallContext,
        route: ModelRoute,
        prompt: PromptDefinition | None,
    ) -> str | None:
        """按固定可信顺序判定 route；fallback 候选也必须重新走完整链。"""
        if not route.enabled:
            return "MODEL_ROUTE_EMERGENCY_DISABLED"
        if (
            route.allowed_tenant_ids
            and "*" not in route.allowed_tenant_ids
            and context.tenant_id not in route.allowed_tenant_ids
        ) or (
            context.required_data_residency is not None
            and route.data_residency != context.required_data_residency
        ):
            return "MODEL_ROUTE_TENANT_DENIED"
        if route.allowed_model_policies and (
            prompt is None or prompt.model_policy not in route.allowed_model_policies
        ):
            return "MODEL_ROUTE_POLICY_DENIED"
        if route.route_id not in context.allowed_route_ids:
            return "MODEL_ROUTE_NOT_DEPLOYED"
        return None

    def _send_activated_attempt(
        self,
        context: ModelCallContext,
        route: ModelRoute,
        request: object,
        usage_id: str,
        permit_id: str,
    ) -> ModelGatewayResult:
        """在单一 finally 中收敛已激活 usage 与 permit，禁止留下发送中孤儿记录。"""
        outcome = "aborted_before_send"
        retry_after = 0.0
        tokens: tuple[int | None, int | None] = (None, None)
        provider_request_id: str | None = None
        result = ModelGatewayResult("aborted_before_send")
        try:
            # acquire 后、started 前和 HTTP 紧邻处都回读权威状态，任一失败不触网。
            if not self._can_send(context) or not self._can_send(context):
                return result
            if self._traffic.mark_started(route, permit_id).status != "started":
                return result
            if not self._usage.mark_started(usage_id) or not self._can_send(context):
                return result
            effective_timeout = self._effective_timeout(route, context)
            if effective_timeout is None:
                logging.info(
                    "模型调用在发送前中止 run_id=%s step_id=%s route_id=%s reason=execution_window_expired",
                    context.run_id, context.step_id, route.route_id,
                )
                return result
            try:
                payload = self._provider.call(route, request, timeout_seconds=effective_timeout)
            except httpx.HTTPStatusError as exc:
                retry_after = self._retry_after(exc.response)
                outcome = "rate_limited" if exc.response.status_code == 429 else "outcome_unknown"
                if exc.response.status_code >= 500:
                    self._traffic.record_circuit_failure(route)
                result = ModelGatewayResult(outcome, retry_after_seconds=retry_after)
            except (httpx.TimeoutException, httpx.NetworkError):
                outcome = "outcome_unknown"
                self._traffic.record_circuit_failure(route)
                result = ModelGatewayResult(outcome)
            except Exception:
                outcome = "outcome_unknown"
                result = ModelGatewayResult(outcome)
            else:
                # 仅提取 Provider 的无内容请求身份；正文仍只在当前调用栈可见。
                provider_request_id = self._provider_request_id(payload)
                # 响应到达后再撤权时只结算无内容计量，绝不返回 Provider 正文。
                if not self._can_send(context):
                    outcome = "outcome_unknown"
                    result = ModelGatewayResult("response_discarded")
                else:
                    outcome = "succeeded"
                    tokens = self._provider_usage(payload)
                    result = ModelGatewayResult("succeeded", data=payload)
        finally:
            settled = self._settle_activated_attempt(
                route, usage_id, permit_id, outcome, tokens, retry_after, provider_request_id,
            )
        if not settled and result.status == "succeeded":
            # 已调用 Provider 但账本无法确认，不能把不可对账结果当作成功交付。
            return ModelGatewayResult("outcome_unknown")
        if result.status == "succeeded":
            self._traffic.record_circuit_success(route)
        return result

    def _settle_activated_attempt(
        self,
        route: ModelRoute,
        usage_id: str,
        permit_id: str,
        outcome: str,
        tokens: tuple[int | None, int | None],
        retry_after: float,
        provider_request_id: str | None,
    ) -> bool:
        """两个账本分别尽力收敛；一侧故障不能阻断另一侧释放共享资源。"""
        usage_settled = permit_settled = True
        try:
            usage_result = self._usage.settle(
                usage_id, outcome, input_tokens=tokens[0], output_tokens=tokens[1],
                route=route if outcome == "succeeded" else None,
                provider_request_id=provider_request_id,
            )
            usage_settled = usage_result in {"settled", "already_settled"}
            if not usage_settled:
                logging.warning(
                    "模型 usage 结算未确认 usage_id=%s code=MODEL_USAGE_SETTLE_UNCONFIRMED",
                    usage_id,
                )
        except Exception:
            usage_settled = False
            logging.warning("模型 usage 结算失败 usage_id=%s code=MODEL_USAGE_SETTLE_FAILED", usage_id)
        try:
            permit_result = self._traffic.settle(
                route, permit_id,
                retry_after_seconds=retry_after if outcome == "rate_limited" else 0,
            )
            permit_settled = permit_result.status in {"settled", "already_settled"}
            if not permit_settled:
                logging.warning(
                    "模型 permit 结算未确认 route_id=%s code=MODEL_PERMIT_SETTLE_UNCONFIRMED",
                    route.route_id,
                )
        except Exception:
            permit_settled = False
            logging.warning("模型 permit 结算失败 route_id=%s code=MODEL_PERMIT_SETTLE_FAILED", route.route_id)
        return usage_settled and permit_settled

    def _route_supports_prompt(
        self,
        route: ModelRoute,
        estimated_input_tokens: int,
        prompt: PromptDefinition | None,
    ) -> bool:
        """内部调用已完成流控预检；统一复用无副作用 capability 判定。"""
        if prompt is None:
            return True
        return self._capability_evaluator.available(
            route, prompt, estimated_input_tokens=estimated_input_tokens, redis_available=True,
        )

    @staticmethod
    def _deadline_expired(context: ModelCallContext) -> bool:
        return bool(
            context.request_deadline_at is not None
            and ModelGateway._as_utc(context.request_deadline_at) <= datetime.now(UTC)
        )

    @classmethod
    def _effective_timeout(cls, route: ModelRoute, context: ModelCallContext) -> float | None:
        """返回可信同步窗口的最短 timeout；任一窗口已过期则禁止发送。"""
        now = datetime.now(UTC)
        timeout_seconds = route.timeout_seconds
        # Run deadline 可为空；lease 始终来自已认领 Worker 的 fencing 上下文。
        windows = (context.request_deadline_at, context.lease_context.lease_expires_at)
        for expires_at in windows:
            if expires_at is None:
                continue
            remaining_seconds = (cls._as_utc(expires_at) - now).total_seconds()
            if remaining_seconds <= 0:
                return None
            timeout_seconds = min(timeout_seconds, remaining_seconds)
        return timeout_seconds

    def _can_send(self, context: ModelCallContext) -> bool:
        if not self._call_guard.permits_new_call(context):
            return False
        # 紧邻 HTTP 的权威 Step 回读，防止构造后被重试/终态变更的 context 穿透。
        if not self._policy.context_is_authoritative(context):
            return False
        if self._deadline_expired(context):
            return False
        if self._as_utc(context.lease_context.lease_expires_at) <= datetime.now(UTC):
            return False
        return self._lease.can_write(context.run_id, context.lease_context)

    def _permits_new_call(self, context: ModelCallContext, route_id: str) -> bool:
        if self._call_guard.permits_new_call(context):
            return True
        logging.info(
            "模型调用在发送前中止 run_id=%s step_id=%s route_id=%s reason=worker_draining",
            context.run_id,
            context.step_id,
            route_id,
        )
        return False

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _provider_usage(payload: object) -> tuple[int | None, int | None]:
        """仅从 Provider 的计量 envelope 读取 token；正文不进入账本。"""
        if not isinstance(payload, Mapping):
            return None, None
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            return None, None
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
        if (
            isinstance(input_tokens, bool) or isinstance(output_tokens, bool)
            or not isinstance(input_tokens, int) or not isinstance(output_tokens, int)
            or input_tokens < 0 or output_tokens < 0
        ):
            return None, None
        return input_tokens, output_tokens

    @staticmethod
    def _provider_request_id(payload: object) -> str | None:
        """只接受 Provider 约定的请求身份，供未知结果的迟到计量核验。"""
        if not isinstance(payload, Mapping):
            return None
        value = payload.get("provider_request_id", payload.get("request_id"))
        if not isinstance(value, str) or not value or len(value) > 120:
            return None
        return value

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        value = response.headers.get("Retry-After")
        try:
            return max(0.0, float(value)) if value is not None else 0.0
        except ValueError:
            return 0.0


def _reject_unsafe_endpoint_host(host: str | None, error_code: str) -> None:
    """拒绝 endpoint 中可在构造期确定的 localhost 与非公网 IP 字面量。"""
    if not host:
        raise ValueError(error_code)
    if host.rstrip(".").lower() == "localhost":
        logging.warning("模型路由 endpoint 静态校验拒绝 code=%s", error_code)
        raise ValueError(error_code)
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        # 域名必须在发送前再解析；构造期不把测试/部署 DNS 瞬时故障固化为路由状态。
        return
    if not address.is_global:
        logging.warning("模型路由 endpoint 静态校验拒绝 code=%s", error_code)
        raise ValueError(error_code)


def _ensure_public_endpoint(endpoint: str, error_prefix: str) -> frozenset[str]:
    """解析 endpoint 并返回本次允许的公网 IP，用于与实际 TCP 对端比对。"""
    parsed = urlsplit(endpoint)
    host = parsed.hostname
    _reject_unsafe_endpoint_host(host, f"{error_prefix}_UNSAFE")
    if not host:
        raise ValueError(f"{error_prefix}_UNSAFE")
    try:
        default_port = 80 if parsed.scheme == "http" else 443
        addresses = socket.getaddrinfo(host, parsed.port or default_port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        logging.warning("模型路由 endpoint DNS 解析失败 code=%s", f"{error_prefix}_DNS_UNRESOLVED")
        raise ValueError(f"{error_prefix}_DNS_UNRESOLVED") from exc
    if not addresses:
        logging.warning("模型路由 endpoint DNS 解析为空 code=%s", f"{error_prefix}_DNS_UNRESOLVED")
        raise ValueError(f"{error_prefix}_DNS_UNRESOLVED")
    peer_ips: set[str] = set()
    for _, _, _, _, sockaddr in addresses:
        try:
            address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        except ValueError as exc:
            logging.warning("模型路由 endpoint DNS 地址无效 code=%s", f"{error_prefix}_UNSAFE")
            raise ValueError(f"{error_prefix}_UNSAFE") from exc
        if not address.is_global:
            logging.warning("模型路由 endpoint DNS 地址被拒绝 code=%s", f"{error_prefix}_UNSAFE")
            raise ValueError(f"{error_prefix}_UNSAFE")
        peer_ips.add(str(address))
    return frozenset(peer_ips)


# 计数和状态转换必须在同一个 Lua 脚本内完成；不得退化到本地计数器。
_PERMIT_SCRIPT = """
local op, route, permit, blocked, now = ARGV[1], ARGV[2], ARGV[3], ARGV[4], tonumber(ARGV[5])
local active, rpm, tpm, token_amounts = route..':active', route..':rpm', route..':tpm', route..':tpm_amounts'
local function reap_expired_permits()
  -- permit 状态比 active 槽多保留一小段时间，才能在 TTL 回收时区分
  -- “尚未发送”与“已发送但结果未知”：前者回滚 RPM/TPM，后者保守保留。
  local expired = redis.call('ZRANGEBYSCORE', active, '-inf', now)
  for _, expired_permit in ipairs(expired) do
    if redis.call('HGET', expired_permit, 'state') == 'acquired' then
      redis.call('ZREM', rpm, expired_permit)
      redis.call('ZREM', tpm, expired_permit)
      redis.call('HDEL', token_amounts, expired_permit)
    end
    redis.call('ZREM', active, expired_permit)
    redis.call('HSET', expired_permit, 'state', 'expired')
    redis.call('EXPIRE', expired_permit, 60)
  end
end
if op == 'acquire' then
  local concurrency, ttl, rpm_limit, tpm_limit, tokens = tonumber(ARGV[6]), tonumber(ARGV[7]), tonumber(ARGV[8]), tonumber(ARGV[9]), tonumber(ARGV[10])
  local route_id = ARGV[11]
  local circuit_threshold = tonumber(ARGV[12])
  if circuit_threshold > 0 then
    local circuit_open = 'model_gateway:circuit_open:'..route_id
    local circuit_open_until = tonumber(redis.call('GET', circuit_open) or '0')
    if circuit_open_until > now then return {'circuit_open', circuit_open_until} end
  end
  local existing_route_id = redis.call('HGET', permit, 'route_id')
  if existing_route_id then
    if existing_route_id ~= route_id then return {'route_mismatch', 0} end
    return {redis.call('HGET', permit, 'state'), 0}
  end
  local blocked_until = tonumber(redis.call('GET', blocked) or '0')
  if blocked_until > now then return {'blocked', blocked_until} end
  reap_expired_permits()
  if redis.call('ZCARD', active) >= concurrency then return {'concurrency_exceeded', 0} end
  local threshold = now - 60
  redis.call('ZREMRANGEBYSCORE', rpm, '-inf', threshold)
  local stale = redis.call('ZRANGEBYSCORE', tpm, '-inf', threshold)
  for _, item in ipairs(stale) do redis.call('ZREM', tpm, item); redis.call('HDEL', token_amounts, item) end
  if redis.call('ZCARD', rpm) >= rpm_limit then return {'rpm_exceeded', 0} end
  local values = redis.call('HVALS', token_amounts); local used = 0
  for _, value in ipairs(values) do used = used + tonumber(value) end
  if used + tokens > tpm_limit then return {'tpm_exceeded', 0} end
  redis.call('ZADD', active, now + ttl, permit)
  redis.call('ZADD', rpm, now, permit)
  redis.call('ZADD', tpm, now, permit)
  redis.call('HSET', token_amounts, permit, tokens)
  redis.call('HSET', permit, 'state', 'acquired', 'route', route, 'route_id', route_id)
  -- 留存状态至 active TTL 后 60 秒，供失联 Worker 的 TTL 回收安全结算。
  redis.call('EXPIRE', permit, math.ceil(ttl) + 60)
  return {'acquired', ttl}
end
if op == 'circuit_preflight' then
  local threshold, route_id = tonumber(ARGV[6]), ARGV[7]
  if threshold == 0 then return {'circuit_available', 0} end
  local circuit_open = 'model_gateway:circuit_open:'..route_id
  local circuit_open_until = tonumber(redis.call('GET', circuit_open) or '0')
  if circuit_open_until > now then return {'circuit_open', circuit_open_until} end
  return {'circuit_available', 0}
end
if op == 'circuit_failure' then
  local threshold, open_seconds, route_id = tonumber(ARGV[6]), tonumber(ARGV[7]), ARGV[8]
  if threshold == 0 then return {'circuit_disabled', 0} end
  local failures = 'model_gateway:circuit_failures:'..route_id
  local circuit_open = 'model_gateway:circuit_open:'..route_id
  local count = redis.call('INCR', failures)
  -- 每次确认失败刷新计数窗口，避免很久以前的故障永久累计。
  redis.call('EXPIRE', failures, math.max(1, math.ceil(open_seconds)))
  if count >= threshold then
    local open_until = now + open_seconds
    redis.call('SET', circuit_open, open_until, 'EX', math.max(1, math.ceil(open_seconds)))
    return {'circuit_opened', open_until}
  end
  return {'circuit_failure', count}
end
if op == 'circuit_success' then
  local route_id = ARGV[6]
  local failures = 'model_gateway:circuit_failures:'..route_id
  local circuit_open = 'model_gateway:circuit_open:'..route_id
  local recovered = redis.call('EXISTS', failures) == 1 or redis.call('EXISTS', circuit_open) == 1
  redis.call('DEL', failures, circuit_open)
  if recovered then return {'circuit_recovered', 0} end
  return {'circuit_reset', 0}
end
if op == 'started' then
  reap_expired_permits()
  local state = redis.call('HGET', permit, 'state')
  if not state then return {'expired', 0} end
  if redis.call('HGET', permit, 'route_id') ~= ARGV[6] then return {'route_mismatch', 0} end
  if state == 'acquired' then redis.call('HSET', permit, 'state', 'started'); return {'started', 0} end
  if state == 'started' then return {'already_started', 0} end
  return {state, 0}
end
if op == 'settle' then
  reap_expired_permits()
  local state = redis.call('HGET', permit, 'state')
  if not state then return {'already_settled', 0} end
  if redis.call('HGET', permit, 'route_id') ~= ARGV[7] then return {'route_mismatch', 0} end
  if state == 'settled' then return {'already_settled', 0} end
  redis.call('HSET', permit, 'state', 'settled')
  redis.call('ZREM', active, permit)
  local retry_after = tonumber(ARGV[6])
  if retry_after > 0 then
    local blocked_until = math.max(tonumber(redis.call('GET', blocked) or '0'), now + retry_after)
    redis.call('SET', blocked, blocked_until, 'EX', math.ceil(blocked_until - now))
  end
  return {'settled', 0}
end
return {'invalid_operation', 0}
"""


class ProviderTrafficController:
    """Redis 是唯一共享流控真相；Redis 故障时一律拒绝。"""

    def __init__(
        self, redis: RedisEvaluator, *, clock: Callable[[], float] = time.time,
        recorder: TrafficEventRecorder | None = None,
    ) -> None:
        self._redis = redis
        self._clock = clock
        self._recorder = recorder or NullTrafficEventRecorder()

    def acquire(self, route: ModelRoute, permit_id: str, *, estimated_tokens: int = 0) -> PermitResult:
        if not permit_id or estimated_tokens < 0:
            raise ValueError("permit_id 不能为空且 estimated_tokens 不可为负")
        result = self._run(
            "acquire", route, permit_id, route.max_concurrency, route.permit_ttl_seconds,
            route.rpm_limit, route.tpm_limit, estimated_tokens, route.route_id,
            route.circuit_failure_threshold,
        )
        if result.status == "redis_unavailable":
            self._record("redis_fail_closed", route, result.status)
        elif result.status != "acquired":
            self._record("permit_rejected", route, result.status)
        return result

    def record_circuit_failure(self, route: ModelRoute) -> PermitResult:
        """仅记录已确认的 Provider 失败；Redis 故障由调用方安全忽略结果。"""
        if route.circuit_failure_threshold == 0:
            return PermitResult("circuit_disabled")
        result = self._run(
            "circuit_failure", route, "", route.circuit_failure_threshold,
            route.circuit_open_seconds, route.route_id,
        )
        if result.status == "circuit_opened":
            self._record("circuit_opened", route, result.status)
        elif result.status == "redis_unavailable":
            self._record("redis_fail_closed", route, result.status)
        return result

    def record_circuit_success(self, route: ModelRoute) -> PermitResult:
        """仅在可交付的成功响应后清除 route 的连续失败计数。"""
        if route.circuit_failure_threshold == 0:
            return PermitResult("circuit_disabled")
        result = self._run("circuit_success", route, "", route.route_id)
        if result.status == "circuit_recovered":
            self._record("circuit_recovered", route, result.status)
        elif result.status == "redis_unavailable":
            self._record("redis_fail_closed", route, result.status)
        return result

    def preflight_circuit(self, route: ModelRoute) -> PermitResult:
        """在 usage 预留前只读检查 route 熔断；Redis 故障一律拒绝。"""
        if route.circuit_failure_threshold == 0:
            return PermitResult("circuit_available")
        return self._run(
            "circuit_preflight", route, "", route.circuit_failure_threshold, route.route_id,
        )

    def mark_started(self, route: ModelRoute, permit_id: str) -> PermitResult:
        return self._run("started", route, permit_id, route.route_id)

    def settle(self, route: ModelRoute, permit_id: str, *, retry_after_seconds: float = 0) -> PermitResult:
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds 不可为负")
        result = self._run("settle", route, permit_id, retry_after_seconds, route.route_id)
        if retry_after_seconds > 0 and result.status in {"settled", "already_settled"}:
            self._record("retry_after_applied", route, "rate_limited")
        elif result.status == "redis_unavailable":
            self._record("redis_fail_closed", route, result.status)
        return result

    def _record(self, event_type: str, route: ModelRoute, result_code: str) -> None:
        try:
            self._recorder.record(event_type, route.route_id, result_code)
        except Exception:
            # 流量观测不能携带内容，也不能让记录器异常把 Redis 的 fail-closed 语义改为放行。
            logging.warning("Runtime 流量账本写入失败 event_type=%s route_id=%s", event_type, route.route_id)

    def _run(self, operation: str, route: ModelRoute, permit_id: str, *values: object) -> PermitResult:
        route_key = f"model_gateway:route:{route.rate_limit_key}"
        permit_key = f"model_gateway:permit:{permit_id}"
        blocked_key = f"model_gateway:blocked:{route.rate_limit_key}"
        now = self._clock()
        try:
            response = self._redis.eval(
                _PERMIT_SCRIPT, 0, operation, route_key, permit_key, blocked_key, now, *values
            )
            status, detail = self._response_values(response)
        except Exception:  # Redis 连通性/脚本故障均不可放行请求。
            logging.warning("模型流控 Redis 不可用 operation=%s route_id=%s", operation, route.route_id)
            return PermitResult("redis_unavailable")
        retry_after = max(0.0, detail - now) if status in {"blocked", "circuit_open"} else 0.0
        return PermitResult(status=status, retry_after_seconds=retry_after)

    @staticmethod
    def _response_values(response: object) -> tuple[str, float]:
        if not isinstance(response, (list, tuple)) or len(response) != 2:
            raise ValueError("Redis permit 脚本响应无效")
        raw_status, raw_detail = response
        status = raw_status.decode() if isinstance(raw_status, bytes) else str(raw_status)
        return status, float(raw_detail)
