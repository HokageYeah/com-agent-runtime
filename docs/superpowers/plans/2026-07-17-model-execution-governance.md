# Model Execution Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变业务请求契约的前提下，为现有 ModelGateway 增加模型调用预算、Provider 熔断和 Worker draining 调用边界。

**Architecture:** `PolicyEngine` 从可信 `ModelCallContext`、Run 冻结 capability snapshot 与持久 `AgentModelUsage` 得出确定性准入结果。`ProviderTrafficController` 使用现有 Redis Lua 原子地保存 route 级连续失败与熔断窗口。`ModelGateway` 在 permit 前执行策略和熔断检查，并通过注入的安全调用守卫在 permit 后和 HTTP 前阻断 draining Worker。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy、Redis Lua、httpx、pytest、ruff。

## Global Constraints

- 只允许服务端配置 route、预算与熔断参数；业务请求不得影响这些安全边界。
- 不新增数据库表；预算只读取既有 `AgentRun.capability_snapshot_json` 和 `AgentModelUsage`。
- 日志、usage、checkpoint、artifact 不得保存或打印 prompt、Provider 正文、日记正文、播放文档、token、用户标识或完整 capability snapshot。
- Redis 故障保持 fail-closed；429 仅复用 Retry-After，不计入熔断失败。
- 本共享且脏的工作区禁止 `git add`、`git commit`、重置或覆盖既有改动。
- 每个行为先写失败测试，再写最小实现；完成任务后运行其全部目标测试。

---

## File Structure

- Create: `app/runtime/policy_engine.py` — 只读可信账本的模型调用准入与安全错误码。
- Modify: `app/runtime/model_gateway.py` — route 熔断配置、Redis Lua 原子状态、PolicyEngine 和调用守卫装配。
- Modify: `app/core/config.py` — 严格解析 route 熔断字段，默认关闭熔断。
- Modify: `app/services/agent_run_service.py`、`app/api/endpoints/agent_runs_api.py` — 创建 Run 时冻结允许路由以及 package policy 的安全预算子集。
- Modify: `app/worker.py`、`app/runtime/memoir_model_gateway.py` — 仅将 Worker draining 状态作为运行时守卫注入，不写入业务输入。
- Modify: `tests/test_model_gateway.py`、`tests/test_provider_traffic_controller.py`、`tests/test_memoir_model_gateway.py`、`tests/runtime_test_worker_entry.py` — 覆盖策略、熔断、draining 和隐私安全边界。
- Modify: `头脑风暴/docs/AgentRuntime/plans/2026-07-07-AgentRuntime-总控开发计划.md` — 仅在所有测试通过后标记已完成的细粒度项 `[✅]`。

## Task 1: Freeze policy limits and implement PolicyEngine

**Files:**

- Create: `app/runtime/policy_engine.py`
- Modify: `app/services/agent_run_service.py`
- Modify: `app/api/endpoints/agent_runs_api.py`
- Modify: `tests/test_model_gateway.py`

**Consumes:** `ModelCallContext.from_authoritative(session, run_id, step_id, lease_context)`、`AgentRun.capability_snapshot_json`、`AgentModelUsage`。

**Produces:**

```python
@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    code: str | None = None

class PolicyEngine:
    def evaluate(self, context: ModelCallContext, route: ModelRoute) -> PolicyDecision:
        """返回当前可信模型调用是否能在发送前获准。"""
```

`capability_snapshot_json["model_policy"]` 仅允许冻结下列非负字段：`max_model_calls` 与 `max_model_cost`。缺失字段表示该维度不设额外上限；bool、负数、NaN、字符串和嵌套对象在 Run 创建期拒绝。当前调用以 `route.input_price * estimated_input_tokens / 1000` 作为最小保守预留；已存在 usage 的成本按 `estimated_cost`，为空时按 `reserved_estimated_cost`，仍为空时按 0 聚合。

- [✅] **Step 1: 写失败测试：调用次数达到冻结上限时不触网**

在 `tests/test_model_gateway.py` 加入：

```python
def test_model_call_limit_denies_before_permit_or_provider() -> None:
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.capability_snapshot_json = {
        "allowed_model_route_ids": ["summary"],
        "model_policy": {"max_model_calls": 1},
    }
    existing = AgentModelUsage(
        usage_id="usage-limit-1", run_id="run-1", step_id="step-1",
        execution_attempt=1, model_attempt=1, status="succeeded",
        provider="provider", model="model", route_id="summary",
        permit_id="settled-permit", estimated_cost=0.1,
        reserved_estimated_cost=0.1,
    )
    session.add(existing)
    session.commit()
    provider = RecordingProvider()
    result = _gateway(session, provider).call(_context(session, lease), "summary", {"safe": "ref"})
    assert result.status == "policy_denied"
    assert result.error_code == "MODEL_CALL_LIMIT_EXCEEDED"
    assert provider.calls == 0
    assert session.scalars(select(AgentModelUsage)).all().__len__() == 1
```

- [✅] **Step 2: 运行失败测试确认当前缺少策略准入**

Run: `poetry run pytest tests/test_model_gateway.py::test_model_call_limit_denies_before_permit_or_provider -q`

Expected: FAIL，因为 `ModelGatewayResult` 尚无 `error_code` 或 Gateway 仍会调用 Provider。

- [✅] **Step 3: 写失败测试：未知成本也占用预算**

新增测试将已有 usage 设置为 `status="outcome_unknown"`、`estimated_cost=reserved_estimated_cost=0.2`，冻结 `max_model_cost=0.2`，断言结果为 `policy_denied/MODEL_COST_LIMIT_EXCEEDED`、无 Provider 调用。再新增一个调用，验证当前调用的最小预留成本会使 `0.19 + 0.02` 在发送前被拒绝。

- [✅] **Step 4: 实现最小 PolicyEngine**

创建 `app/runtime/policy_engine.py`，保持只读与无日志正文：

```python
@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    code: str | None = None

class PolicyEngine:
    def __init__(self, session: Session) -> None:
        self._session = session

    def evaluate(self, context: ModelCallContext, route: ModelRoute) -> PolicyDecision:
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == context.run_id))
        if run is None or run.status not in {"pending", "running", "waiting_human"}:
            return PolicyDecision(False, "MODEL_RUN_NOT_EXECUTABLE")
        policy = self._policy(run.capability_snapshot_json)
        usages = self._session.scalars(
            select(AgentModelUsage).where(AgentModelUsage.run_id == context.run_id)
        ).all()
        if policy.max_model_calls is not None and len(usages) >= policy.max_model_calls:
            return PolicyDecision(False, "MODEL_CALL_LIMIT_EXCEEDED")
        reserved = route.input_price * context.estimated_input_tokens / 1000
        used = sum(self._conservative_cost(usage) for usage in usages)
        if policy.max_model_cost is not None and used + reserved > policy.max_model_cost:
            return PolicyDecision(False, "MODEL_COST_LIMIT_EXCEEDED")
        return PolicyDecision(True)
```

实现 `_policy` 时只读取 `model_policy` mapping 的两个字段；实现 `_conservative_cost` 时优先 `estimated_cost`、其次 `reserved_estimated_cost`，并仅接收非负有限 float。为 `ModelGatewayResult` 增加可选 `error_code: str | None = None`，但不携带详情消息。

- [✅] **Step 5: 在创建 Run 时冻结 package policy 的安全子集**

在 `AgentRunService.create()` 构造 capability snapshot 时，从已加载 package policy 的 `max_model_calls`、`max_model_cost` 复制为 `model_policy`；API 传入的任意同名字段都忽略。缺失字段不写入。为 API 测试增加断言：创建请求即使携带 `model_policy`，持久 snapshot 只等于服务端 package 定义。

- [✅] **Step 6: 运行 Task 1 测试**

Run: `poetry run pytest tests/test_model_gateway.py tests/test_runtime_agent_run_api.py tests/test_runtime_agent_run_service.py -q`

Expected: PASS。

## Task 2: Add atomic route circuit breaker

**Files:**

- Modify: `app/runtime/model_gateway.py`
- Modify: `app/core/config.py`
- Modify: `tests/test_provider_traffic_controller.py`
- Modify: `tests/test_model_gateway.py`

**Consumes:** `ModelRoute`、`ProviderTrafficController.acquire()`、现有 `_PERMIT_SCRIPT`、Provider 的 httpx 异常类型。

**Produces:**

```python
class ModelRoute:
    circuit_failure_threshold: int = 0
    circuit_open_seconds: float = 0.0

class ProviderTrafficController:
    def record_success(self, route: ModelRoute) -> None:
        """清除成功 route 的连续可确认失败计数。"""

    def record_confirmed_failure(self, route: ModelRoute) -> PermitResult:
        """记录 timeout/network/5xx，并在达到阈值后打开熔断。"""
```

Redis 新键只允许 `model_gateway:circuit_failures:{route_id}` 和 `model_gateway:circuit_open:{route_id}`；所有读改写在 Lua 内完成。`acquire` 在现有 blocked 检查前检查 open key；返回 `circuit_open` 与剩余秒数。

- [✅] **Step 1: 写失败测试：连续 timeout 打开 route 熔断**

在 `tests/test_provider_traffic_controller.py` 的 FakeRedis/Lua 行为夹具中增加 circuit 键支持，并写：

```python
def test_confirmed_failures_open_route_circuit_and_acquire_returns_retry_after() -> None:
    route = _route(circuit_failure_threshold=2, circuit_open_seconds=30)
    traffic = ProviderTrafficController(FakeRedis(), clock=lambda: 100.0)
    assert traffic.record_confirmed_failure(route).status == "failure_recorded"
    assert traffic.record_confirmed_failure(route).status == "circuit_opened"
    result = traffic.acquire(route, "permit-after-open")
    assert result.status == "circuit_open"
    assert result.retry_after_seconds == 30.0
```

- [✅] **Step 2: 运行失败测试**

Run: `poetry run pytest tests/test_provider_traffic_controller.py::test_confirmed_failures_open_route_circuit_and_acquire_returns_retry_after -q`

Expected: FAIL，因为 route 与 controller 尚无熔断接口。

- [✅] **Step 3: 扩展 ModelRoute 与配置解析**

在 `ModelRoute.__post_init__()` 验证：threshold 为非负 int（bool 拒绝）；open seconds 为非负有限数；threshold 为 0 时 open seconds 必须为 0。`ModelRoute.from_mapping()` 通过 dataclass 默认值兼容现有 route JSON。`Settings.model_routes` 只接受这两个显式字段，不允许未知配置字段静默穿透。

- [✅] **Step 4: 扩展 Lua 原子熔断逻辑**

在 `_PERMIT_SCRIPT` 增加 `circuit_failure` 与 `circuit_success` 操作：

```lua
if op == 'circuit_failure' then
  local threshold, open_seconds = tonumber(ARGV[6]), tonumber(ARGV[7])
  if threshold <= 0 then return {'circuit_disabled', 0} end
  local failures = redis.call('INCR', circuit_failures)
  redis.call('EXPIRE', circuit_failures, math.ceil(open_seconds))
  if failures >= threshold then
    local until_at = now + open_seconds
    redis.call('SET', circuit_open, until_at, 'EX', math.ceil(open_seconds))
    return {'circuit_opened', until_at}
  end
  return {'failure_recorded', failures}
end
if op == 'circuit_success' then
  redis.call('DEL', circuit_failures, circuit_open)
  return {'circuit_reset', 0}
end
```

在 `acquire` 分支的第一个状态检查中读取 `circuit_open`；仍保持 `blocked`（429）与 `circuit_open` 两类状态独立。`_run()` 只对 `blocked` 或 `circuit_open` 计算 retry-after。

- [✅] **Step 5: 将 Provider 结果映射到熔断事件**

在 `ModelGateway.call()` 中：成功且 response 未被丢弃时调用 `record_success(route)`；`httpx.TimeoutException`、`httpx.NetworkError` 和 `HTTPStatusError` 的 5xx 调用 `record_confirmed_failure(route)`；429 不调用熔断方法；未知 Python 异常只保守结算 usage，不自动复位或累计熔断。任何 Redis 熔断记录失败都只记录安全 warning，不能把已获得的 Provider 响应伪装成失败。

- [✅] **Step 6: 写并运行回归测试**

新增以下断言并运行：

```python
def test_429_sets_retry_after_without_opening_circuit() -> None:
    """429 只写 Retry-After，不增加连续熔断失败。"""

def test_success_resets_confirmed_failure_count() -> None:
    """成功 Provider 响应清除同一 route 的连续失败。"""

def test_http_503_counts_as_confirmed_circuit_failure() -> None:
    """HTTP 5xx 属于可确认 Provider 失败。"""
```

Run: `poetry run pytest tests/test_provider_traffic_controller.py tests/test_model_gateway.py -q`

Expected: PASS。

## Task 3: Inject Worker draining guard into the model call boundary

**Files:**

- Modify: `app/runtime/model_gateway.py`
- Modify: `app/runtime/memoir_model_gateway.py`
- Modify: `app/worker.py`
- Modify: `tests/test_memoir_model_gateway.py`
- Modify: `tests/runtime_test_worker_entry.py`

**Consumes:** `WorkerLoop.is_draining`、`configured_executor()`、`MemoirModelGatewayAdapter.bind_lease()`、`ModelGateway.call()`。

**Produces:**

```python
class ModelCallGuard(Protocol):
    def permits_new_call(self, context: ModelCallContext) -> bool:
        """返回该 Worker 此刻是否仍允许开始新的模型调用。"""

class ModelGateway:
    def __init__(
        self, routes: ModelRouteRegistry, traffic: ProviderTrafficController,
        usage_service: ModelUsageService, lease_service: LeaseBoundary,
        provider: ProviderAdapter, call_guard: ModelCallGuard | None = None,
    ) -> None:
        """注入可选运行时调用守卫；未注入时保持既有允许语义。"""
```

默认 guard 永远允许，以保持单元测试和非 Worker 使用者兼容。Worker 仅传入封装 `is_draining()` 的 guard，guard 不读取/写入业务正文，也不修改 lease。

- [✅] **Step 1: 写失败测试：draining 时 permit 前拒绝**

在 `tests/test_model_gateway.py` 写入：

```python
def test_draining_guard_denies_before_permit_and_provider() -> None:
    session, lease = _run_session()
    provider = RecordingProvider()
    gateway = _gateway(session, provider, call_guard=AlwaysDrainingGuard())
    result = gateway.call(_context(session, lease), "summary", {"safe": "ref"})
    assert result.status == "aborted_before_send"
    assert provider.calls == 0
    assert session.scalar(select(AgentModelUsage)) is None
```

- [✅] **Step 2: 运行失败测试**

Run: `poetry run pytest tests/test_model_gateway.py::test_draining_guard_denies_before_permit_and_provider -q`

Expected: FAIL，因为 Gateway 构造函数尚不接受 guard。

- [✅] **Step 3: 实现 Guard 并在 Gateway 三个安全点调用**

在 `model_gateway.py` 定义 protocol 与默认实现；将 `_can_send()` 扩展为先判 deadline、再判 `guard.permits_new_call(context)`、最后判 lease。`call()` 在 `route_not_allowed/deadline` 后、创建 permit 前显式调用一次 `_can_send()`；现有 permit 后及 HTTP 前检查继续复用 `_can_send()`。拒绝日志格式固定为：

```python
logging.info(
    "模型调用在安全边界拒绝 run_id=%s step_id=%s route_id=%s reason=%s",
    context.run_id, context.step_id, route.route_id, "worker_draining",
)
```

不得加入 request 或 response 参数。

- [✅] **Step 4: 从 Worker 注入实时 draining guard**

让 `configured_model_gateway(session, *, is_draining: Callable[[], bool] = lambda: False)` 构造只持有该 callable 的 guard，并将 guard 交给 `ModelGateway`。`configured_executor()` 接收同一 callable，并传递到 `configured_model_gateway()`；`WorkerLoop` 构造 executor 时将其 `self._is_draining` 传入 callable executor。不要把 draining 存入 `AgentRun`、checkpoint 或 artifact。

- [✅] **Step 5: 写集成测试并运行**

在 `tests/runtime_test_worker_entry.py` 断言 configured executor 在 `is_draining=lambda: True` 时，Memoir 模型节点拿到 `aborted_before_send` 且 FakeProvider 从未调用；同时保留 `is_draining=False` 的既有成功路径。

Run: `poetry run pytest tests/test_model_gateway.py tests/test_memoir_model_gateway.py tests/runtime_test_worker_entry.py -q`

Expected: PASS。

## Task 4: Verification, plan status and security regression

**Files:**

- Modify: `头脑风暴/docs/AgentRuntime/plans/2026-07-07-AgentRuntime-总控开发计划.md`
- Test: `tests/test_model_gateway.py`
- Test: `tests/test_provider_traffic_controller.py`
- Test: `tests/test_memoir_model_gateway.py`
- Test: `tests/runtime_test_worker_entry.py`

**Consumes:** Tasks 1–3 completed implementation and test results.

**Produces:** 总控计划中已真实完成的 Provider 熔断、PolicyEngine、Worker 模型安全边界条目使用 `[✅]`；PromptRegistry、ContextManager、真实 workflow graph 等本轮范围之外的条目保持 `[ ]`。

- [✅] **Step 1: 安全日志回归测试**

为 policy deny、circuit open、draining deny 三条路径捕获日志，断言其中只含 `run_id`、`step_id`、`route_id`、错误码/状态；测试 request 内的 `"diary_body": "private text"` 和响应内的 `"content": "private result"` 均不出现在日志文本中。

- [✅] **Step 2: 运行模型治理目标测试**

Run: `poetry run pytest tests/test_provider_traffic_controller.py tests/test_model_gateway.py tests/test_memoir_model_gateway.py tests/runtime_test_worker_entry.py -q`

Expected: PASS。

- [✅] **Step 3: 运行全量静态与测试验证**

Run: `poetry run pytest -q && poetry run ruff check app tests alembic && git diff --check`

Expected: pytest 全绿、ruff 无错误、diff check 无输出。

- [✅] **Step 4: 更新总控计划的真实完成状态**

仅在 Step 3 成功后，把总控计划中以下已完成语义标为 `[✅]`，并在同一行注明当前范围：

```markdown
- [✅] ProviderTrafficController：Redis 原子 permit、Retry-After 与 route 级连续失败熔断；Redis 不可用 fail closed。
- [✅] PolicyEngine：冻结模型调用次数/保守成本上限，调用前拒绝且不触网；工具预算和跨节点 wall-clock 保持后续任务。
- [✅] Worker 宽限期模型边界：draining 时停止新的模型调用；checkpoint 后停止写入与工具调用边界保持后续真实执行器任务。
```

不得把 PromptRegistry、ContextManager、结构化解析、完整 workflow graph、工具调用预算或外部观测标记为已完成。

- [✅] **Step 5: 对已修改文档执行格式检查**

Run: `git diff --check -- '头脑风暴/docs/AgentRuntime/plans/2026-07-07-AgentRuntime-总控开发计划.md' docs/superpowers`

Expected: 无输出。

## Plan Self-Review

- 规格覆盖：Task 1 覆盖预算和保守成本；Task 2 覆盖 route 熔断、429 语义、Redis fail-closed；Task 3 覆盖 Worker draining 三个检查点；Task 4 覆盖隐私日志、全量验证和总控计划状态。
- 范围控制：没有新增数据库表、外部服务、自动重试或业务侧可控策略参数。
- 接口一致性：Task 1 产出的 `PolicyDecision` 由 Task 2/3 的 `ModelGateway.call()` 消费；Task 3 的 guard 为可选构造参数，不破坏既有调用点。
- 占位符检查：每项实现和验证步骤都给出明确文件、接口或命令；明确非本轮范围均保留在总控计划的 `[ ]` 条目中。
