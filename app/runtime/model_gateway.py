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

from app.runtime.interfaces import LeaseContext
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
        required = {
            "route_config_version", "pricing_config_version", "data_residency",
            "max_context_tokens", "max_output_tokens",
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
    def _as_utc(value: datetime) -> datetime:
        """兼容 SQLite 返回的无时区时间，统一用于可信 deadline 比较。"""
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ProviderAdapter(Protocol):
    """Provider 请求的唯一内容边界；Gateway 不记录传入或返回的正文。"""

    def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object: ...


class HttpProviderAdapter:
    """模型 Provider 的 HTTP 边界，发送后校验真实 TCP 对端以阻断 DNS rebinding。"""

    def __init__(
        self,
        client: httpx.Client,
        *,
        peer_ip_provider: Callable[[], str | None] | None = None,
        reset_peer_ip: Callable[[], None] | None = None,
    ) -> None:
        """注入受控 HTTP Client；生产调用必须同时提供对端 IP 读取器。"""
        self._client = client
        self._peer_ip_provider = peer_ip_provider
        self._reset_peer_ip = reset_peer_ip

    def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object:
        """物理发送前重做 DNS 预检，并在响应解析前核对 socket 实际对端。"""
        allowed_peer_ips = _ensure_public_endpoint(route.endpoint, "MODEL_PROVIDER_ENDPOINT")
        if self._peer_ip_provider is None:
            # Provider 响应属于不可信输入；没有真实 socket 地址时必须 fail-closed。
            logging.warning("模型 Provider 对端地址不可验证 route_id=%s code=MODEL_PROVIDER_PEER_UNVERIFIABLE", route.route_id)
            raise ValueError("MODEL_PROVIDER_PEER_UNVERIFIABLE")
        if self._reset_peer_ip is not None:
            self._reset_peer_ip()
        response = self._client.post(
            route.endpoint,
            json=request,
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        self._verify_connected_peer(allowed_peer_ips, route.route_id)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise ValueError("Provider JSON 响应格式无效")
        return payload

    def _verify_connected_peer(self, allowed_peer_ips: frozenset[str], route_id: str) -> None:
        """仅接受本轮 DNS 公网集合中的真实 TCP 对端，不记录请求或响应内容。"""
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
        if normalized_peer not in allowed_peer_ips:
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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._routes = routes
        self._traffic = traffic
        self._usage = usage_service
        self._lease = lease_service
        self._provider = provider
        self._policy = policy_engine
        self._call_guard = call_guard or _AllowModelCalls()
        self._model_policies = model_policies or ModelPolicyRegistry.default()
        self._sleep = sleep

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
                self._sleep(result.retry_after_seconds)
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
        if route_id not in context.allowed_route_ids:
            return ModelGatewayResult("route_not_allowed")
        if not self._route_supports_prompt(route, context, prompt):
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
            if not callable(attach_prompt_ref) or not attach_prompt_ref(
                usage_id, prompt.prompt_id, prompt.version
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
        # Acquire 后立即复核撤权、取消、draining 与 fencing；此处失败绝不能触网。
        if not self._can_send(context):
            self._usage.settle(usage_id, "aborted_before_send")
            self._traffic.settle(route, permit_id)
            return ModelGatewayResult("aborted_before_send")
        # 进入 started 前再次复核，避免状态变化跨越 permit 的发送权转换。
        if not self._can_send(context):
            self._usage.settle(usage_id, "aborted_before_send")
            self._traffic.settle(route, permit_id)
            return ModelGatewayResult("aborted_before_send")
        started = self._traffic.mark_started(route, permit_id)
        if started.status != "started" or not self._usage.mark_started(usage_id):
            self._usage.settle(usage_id, "aborted_before_send")
            self._traffic.settle(route, permit_id)
            return ModelGatewayResult("aborted_before_send")

        # 此检查必须紧贴实际 HTTP 调用；mark_started 与 Provider 之间不能有
        # 可被撤权/取消/失租穿透的窗口。
        if not self._can_send(context):
            self._usage.settle(usage_id, "aborted_before_send")
            self._traffic.settle(route, permit_id)
            return ModelGatewayResult("aborted_before_send")

        # 同步调用不得跨越可信 Run deadline 或当前 Worker lease；只把最短窗口交给 Provider。
        effective_timeout = self._effective_timeout(route, context)
        if effective_timeout is None:
            logging.info(
                "模型调用在发送前中止 run_id=%s step_id=%s route_id=%s reason=execution_window_expired",
                context.run_id,
                context.step_id,
                route_id,
            )
            self._usage.settle(usage_id, "aborted_before_send")
            self._traffic.settle(route, permit_id)
            return ModelGatewayResult("aborted_before_send")

        try:
            payload = self._provider.call(route, request, timeout_seconds=effective_timeout)
        except httpx.HTTPStatusError as exc:
            retry_after = self._retry_after(exc.response)
            self._usage.settle(usage_id, "rate_limited" if exc.response.status_code == 429 else "outcome_unknown")
            self._traffic.settle(route, permit_id, retry_after_seconds=retry_after if exc.response.status_code == 429 else 0)
            # 仅 5xx 是可确认的 Provider 故障；429 仍只使用原有共享冷却。
            if exc.response.status_code >= 500:
                self._traffic.record_circuit_failure(route)
            return ModelGatewayResult(
                "rate_limited" if exc.response.status_code == 429 else "outcome_unknown",
                retry_after_seconds=retry_after,
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            self._usage.settle(usage_id, "outcome_unknown")
            self._traffic.settle(route, permit_id)
            self._traffic.record_circuit_failure(route)
            return ModelGatewayResult("outcome_unknown")
        except Exception:
            self._usage.settle(usage_id, "outcome_unknown")
            self._traffic.settle(route, permit_id)
            return ModelGatewayResult("outcome_unknown")

        # 响应到达也不可绕过状态边界；撤权时丢弃 Provider 正文。
        if not self._can_send(context):
            self._usage.settle(usage_id, "outcome_unknown")
            self._traffic.settle(route, permit_id)
            return ModelGatewayResult("response_discarded")
        input_tokens, output_tokens = self._provider_usage(payload)
        self._usage.settle(
            usage_id, "succeeded", input_tokens=input_tokens,
            output_tokens=output_tokens, route=route,
        )
        self._traffic.settle(route, permit_id)
        # 仅可交付给调用方的成功响应才清除连续失败状态。
        self._traffic.record_circuit_success(route)
        return ModelGatewayResult("succeeded", data=payload)

    def _route_supports_prompt(
        self,
        route: ModelRoute,
        context: ModelCallContext,
        prompt: PromptDefinition | None,
    ) -> bool:
        """校验可信 Prompt 所需能力和 token 窗口；不匹配时不得尝试其它 Provider。"""
        if prompt is None:
            return True
        try:
            model_policy = self._model_policies.get(prompt.model_policy)
            guardrail_policy = self._model_policies.get(prompt.guardrail_policy)
        except ValueError:
            return False
        required_capabilities = (
            model_policy.required_capabilities | guardrail_policy.required_capabilities
        )
        if model_policy.thinking_enabled:
            required_capabilities = required_capabilities | {"thinking"}
        if model_policy.requires_vision:
            required_capabilities = required_capabilities | {"vision"}
        if not required_capabilities.issubset(route.capabilities):
            return False
        if model_policy.max_output_tokens > route.max_output_tokens:
            return False
        # estimated_input_tokens 仅来自权威 Step 摘要；prompt/业务输入无法抬高窗口。
        return (
            context.estimated_input_tokens + model_policy.max_output_tokens
            <= route.max_context_tokens
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
  redis.call('DEL', 'model_gateway:circuit_failures:'..route_id, 'model_gateway:circuit_open:'..route_id)
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

    def __init__(self, redis: RedisEvaluator, *, clock: Callable[[], float] = time.time) -> None:
        self._redis = redis
        self._clock = clock

    def acquire(self, route: ModelRoute, permit_id: str, *, estimated_tokens: int = 0) -> PermitResult:
        if not permit_id or estimated_tokens < 0:
            raise ValueError("permit_id 不能为空且 estimated_tokens 不可为负")
        return self._run(
            "acquire", route, permit_id, route.max_concurrency, route.permit_ttl_seconds,
            route.rpm_limit, route.tpm_limit, estimated_tokens, route.route_id,
            route.circuit_failure_threshold,
        )

    def record_circuit_failure(self, route: ModelRoute) -> PermitResult:
        """仅记录已确认的 Provider 失败；Redis 故障由调用方安全忽略结果。"""
        if route.circuit_failure_threshold == 0:
            return PermitResult("circuit_disabled")
        return self._run(
            "circuit_failure", route, "", route.circuit_failure_threshold,
            route.circuit_open_seconds, route.route_id,
        )

    def record_circuit_success(self, route: ModelRoute) -> PermitResult:
        """仅在可交付的成功响应后清除 route 的连续失败计数。"""
        if route.circuit_failure_threshold == 0:
            return PermitResult("circuit_disabled")
        return self._run("circuit_success", route, "", route.route_id)

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
        return self._run("settle", route, permit_id, retry_after_seconds, route.route_id)

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
