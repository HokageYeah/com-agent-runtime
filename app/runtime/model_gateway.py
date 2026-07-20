"""可信模型路由和跨 Worker 的 Redis 流量 permit。

此模块只记录 route/permit 等非内容标识，绝不能把 prompt 或 Provider 正文写入日志。
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
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

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ModelRoute:
        """从已解析的服务端 JSON 构造 route，不接受业务请求中的配置。"""
        return cls(**dict(data))


class ModelRouteRegistry:
    """注册时拒绝重复 ID，避免请求通过 route 覆盖安全边界。"""

    def __init__(self, routes: list[ModelRoute]) -> None:
        self._routes = {route.route_id: route for route in routes}
        if len(self._routes) != len(routes):
            raise ValueError("MODEL_ROUTES_JSON 存在重复 route_id")

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
        return cls(
            _factory=_CONTEXT_FACTORY,
            run_id=run_id,
            step_id=step_id,
            model_attempt=step.step_attempt,
            lease_context=lease_context,
            estimated_input_tokens=estimated_input_tokens,
            request_deadline_at=run.run_deadline_at,
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


class ProviderAdapter(Protocol):
    """Provider 请求的唯一内容边界；Gateway 不记录传入或返回的正文。"""

    def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object: ...


class HttpProviderAdapter:
    """注入 httpx.Client 的最小 JSON Provider adapter。"""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object:
        response = self._client.post(route.endpoint, json=request, timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise ValueError("Provider JSON 响应格式无效")
        return payload


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
    ) -> None:
        self._routes = routes
        self._traffic = traffic
        self._usage = usage_service
        self._lease = lease_service
        self._provider = provider
        self._policy = policy_engine
        self._call_guard = call_guard or _AllowModelCalls()

    def call(
        self,
        context: ModelCallContext,
        route_id: str,
        request: object,
        *,
        prompt: PromptDefinition | None = None,
    ) -> ModelGatewayResult:
        route = self._routes.get(route_id)
        if route_id not in context.allowed_route_ids:
            return ModelGatewayResult("route_not_allowed")
        # deadline 在 permit/usage 前拒绝，避免过期请求占用共享配额或触网。
        if self._deadline_expired(context):
            return ModelGatewayResult("aborted_before_send")
        # 熔断预检同样必须在 policy reservation 前执行，稳定打开时不留下 usage。
        circuit = self._traffic.preflight_circuit(route)
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

        try:
            payload = self._provider.call(route, request, timeout_seconds=route.timeout_seconds)
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

    @staticmethod
    def _deadline_expired(context: ModelCallContext) -> bool:
        return bool(
            context.request_deadline_at is not None
            and ModelGateway._as_utc(context.request_deadline_at) <= datetime.now(UTC)
        )

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


# 计数和状态转换必须在同一个 Lua 脚本内完成；不得退化到本地计数器。
_PERMIT_SCRIPT = """
local op, route, permit, blocked, now = ARGV[1], ARGV[2], ARGV[3], ARGV[4], tonumber(ARGV[5])
local active, rpm, tpm, token_amounts = route..':active', route..':rpm', route..':tpm', route..':tpm_amounts'
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
  redis.call('ZREMRANGEBYSCORE', active, '-inf', now)
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
  redis.call('EXPIRE', permit, math.ceil(ttl))
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
  local state = redis.call('HGET', permit, 'state')
  if not state then return {'expired', 0} end
  if redis.call('HGET', permit, 'route_id') ~= ARGV[6] then return {'route_mismatch', 0} end
  if state == 'acquired' then redis.call('HSET', permit, 'state', 'started'); return {'started', 0} end
  if state == 'started' then return {'already_started', 0} end
  return {state, 0}
end
if op == 'settle' then
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
