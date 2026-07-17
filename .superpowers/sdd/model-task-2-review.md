# ModelGateway Task 2 审查

DONE

## 结论

**不通过（存在 P1 安全与记账正确性问题）。** Redis permit、429 共享冷却、超时保守记为 `outcome_unknown`，以及同一 ORM session 内的重复 settle 均有基本实现；且本次定向测试可通过。但 Task 2 要求的“只从有效 LeaseContext 构造”“发送前/响应后边界”“usage 状态、成本与 token 正确结算”“不保存 prompt”尚未被实现或保证，不能进入后续 Task。

## P1 发现

### 1. `ModelCallContext` 不是可信来源，且 capability/prompt 元数据可直接持久化私密正文

- 位置：`app/runtime/model_gateway.py:108-146`；`app/services/model_usage_service.py:32-49`。
- `@dataclass` 的公开构造器允许直接构造任意 `ModelCallContext`，`from_lease` 也只做空值/范围检查，接受调用方提供的任意 `LeaseContext`、`run_id`、`capability_snapshot`、`prompt_id` 和 `prompt_version`。它没有从持久化 Run/步骤或受信任的 Prompt/Capability registry 派生这些值。
- `create_running` 原样写入 `capability_snapshot_json` 和两个 prompt 字段。调用方只要将 prompt 放在 snapshot 或伪装成 ID/版本字符串，就会被写入 `agent_model_usages`；这与“不持久化 prompt/请求正文”的报告结论相矛盾。
- 建议：把上下文创建收口到持有 Run、Step、已验证 Lease 的服务；Gateway 不接受自由构造的 metadata。对 capability snapshot 使用固定的安全 schema/服务端派生值，并对 prompt ID/version 采用受限标识符或 registry 查验；补充数据库断言，验证含私密字符串的 request 和 metadata 均不会落库或进日志。

### 2. “发送前”校验不在实际 HTTP 调用前，撤权可在 `mark_started` 后穿透到 Provider

- 位置：`app/runtime/model_gateway.py:214-225`。
- 第二次 `can_write` 在 `mark_started` 和 `usage.mark_started` 之前。若取消、隐私/授权变更或 fencing 发生在 :214 返回成功之后、:225 调用 Provider 之前，代码仍会发送请求。设计要求“发送前失效则 settled 为 `aborted_before_send`”，此竞态正好违反该边界。
- 建议：在取得唯一发送权、完成 usage `started` 后、紧贴 Provider 调用前再执行一次权威边界检查；新增可编排的 lease fake，令前两次检查成功、发送前检查失败，并断言 `provider.calls == 0`、usage 为 `aborted_before_send`。

### 3. 失租与 request deadline 没有被执行期边界拒绝

- 位置：`app/services/lease_service.py:78-97`；`app/runtime/model_gateway.py:198-250`。
- `LeaseService.can_write` 对比 owner/fencing/privacy/authorization/cancel，但不检查 `run.lease_expires_at` 或 `context.lease_expires_at` 是否已过去。Gateway 也从不检查 `ModelCallContext.request_deadline_at`，尽管设计要求在路由后校验 deadline。因此过期 lease 或已过请求 deadline 的调用在其他字段仍匹配时仍可 acquire、mark_started 并触网。
- 建议：把当前时间与数据库 lease 到期、context lease 到期及 request deadline 的检查纳入权威边界，并在 acquire 后、紧贴发送前、响应后复用；补充“时间已过期但 owner/fencing 未改变”与“deadline 已过”均不调用 Provider 的测试。

### 4. 成功调用不结算实际 token/cost；`output_price` 完全未使用

- 位置：`app/runtime/model_gateway.py:224-250`；`app/services/model_usage_service.py:26, 62-89`。
- Provider 返回的 usage 从未解析或传给 `settle`；Gateway 的成功分支没有传 `input_tokens`/`output_tokens`。因此所有成功记录都保留根据调用方 `estimated_input_tokens` 计算的预留成本，两个 token 字段为 `NULL`，并且 `output_price` 从未参与任何计算。这不满足计划/设计要求的 token 与成本结算正确性，且可被不可信估算值低报保留成本。
- 建议：为 adapter/规范响应定义仅含计量字段的受验证 usage envelope；成功时按冻结 route 价格计算实际输入/输出成本，未知/超时保留明确的预留成本语义。若 Provider 不返回 usage，应明确标识估算而非作为已结算成本。增加输入/输出 token、零 token、缺失 usage 和输出价格参与计算的测试。

### 5. Usage 的“重复结算”不是跨 worker 原子的，冲突 outcome 可被最后写入者覆盖

- 位置：`app/services/model_usage_service.py:54-90`。
- 当前实现是“先 SELECT，再检查 Python 对象状态，再 UPDATE/flush”。两个 session 同时读到 `running`/`started` 时，都会通过 :71，并可分别写入不同 status/cost；没有 `WHERE status IN (...)` 的条件更新、行锁或 version column。现有测试只在同一 session 顺序调用，未覆盖重试/多 worker 结算的实际边界。
- 影响：超时、429、成功等竞态结果可以覆盖首个结算，usage 审计与成本不再幂等；Redis permit 的幂等 settle 不能修复数据库账本。
- 建议：使用带前置状态条件的单条 UPDATE（并根据 rowcount 返回 `already_settled`），或使用数据库乐观锁；以两个独立 session 覆盖同时/重放结算，并断言首个最终状态与成本不可被修改。

## 已确认项

- 已复跑：`poetry run pytest tests/test_model_gateway.py tests/test_provider_traffic_controller.py -q`，**17 passed**。
- `HttpProviderAdapter` 只向已注册 `ModelRoute.endpoint` 发送 request，模块内日志只写 operation 与 route ID；在正常 request 参数路径中未发现直接记录 prompt 或 Provider response body。
- 429 分支读取 `Retry-After`，把 `rate_limited` 结算到 usage，并通过 Redis settle 写入共享 blocked window；相关跨 controller 测试存在。
- `httpx.TimeoutException`/`NetworkError` 与其他未预期异常均结算为 `outcome_unknown`，保留预留成本，符合保守未知结果语义。
- Redis permit 的 `settle` 本身具有 Lua 原子幂等性，重复释放不会重复移除 active 计数；这不覆盖上述 SQL usage 的并发结算问题。

---

# Task 2 P1 修复复审

DONE

## 结论

**仍不通过。** 下列修复已有效：usage 不再落库 capability/prompt/pricing 的调用方元数据；`mark_started` 后增加了紧贴 HTTP 的最终边界；LeaseService 和 Gateway 会拒绝过期 lease/deadline；成功响应可按 Provider usage 与冻结 route 的 input/output price 结算；普通 `settle` 改为条件 UPDATE。定向测试现为 **25 passed**，Ruff 通过。

但仍有两个 P1：reconciler 路径绕开条件结算，且 Gateway 在已 acquire permit 后遇到上下文不可信/usage 建账异常时不释放 permit。后者也说明 Context 仍非“只能来自有效 Lease”的封闭构造边界。

## 剩余 P1

### 1. Reconciler 的过期结算绕开条件 UPDATE，仍可覆盖另一个 worker 的最终 usage

- 位置：`app/services/model_usage_service.py:121-135`。
- `mark_expired_running_unknown` 先查出 `running`/`started` ORM 对象，再逐行赋值并 flush；它没有复用 `settle` 的 `WHERE status IN (...)` 条件更新。若 worker A 已查询到记录，worker B 随后成功结算，A 的 flush 仍可能把成功结果改回 `outcome_unknown`。
- 这正是“跨 worker 条件 settle”的旁路，影响最终状态与成本审计。应改为按 deadline 与 in-flight status 的单条条件 UPDATE（或逐条调用带条件的 settle），并以两个 session 模拟“reconciler 读取后、正常成功结算后、reconciler flush”验证不覆盖。

### 2. `create_running` 拒绝不可信 Context 时，已获得的 permit 不会 settle；Context 也仍可由任意调用方自由构造

- 位置：`app/runtime/model_gateway.py:205-216`；`app/services/model_usage_service.py:21-64`。
- Gateway 在 :206 已 acquire permit；若 :216 的 `create_running` 因 run ownership/fencing 在两次检查间变化而抛出 `MODEL_CALL_CONTEXT_UNTRUSTED`，异常直接冒出，:213 之后没有 `try/finally` 释放 permit。该 permit 将占用 active 并持续消耗并发容量至 TTL，形成可重复触发的共享配额拒绝服务。
- `ModelCallContext` 仍是可直接调用的公开 dataclass，且 `create_running` 只匹配 execution attempt/owner/fencing；任意拥有这些值的调用方可伪造 `step_id`、`model_attempt`、估算 tokens、deadline 并建立 usage。虽然私密 metadata 已不落库，但这不等于 Context 仅能从有效 lease/权威步骤来源创建。
- 建议：在 acquire 后把建账、开始、发送置于保证 settle 的异常安全结构中；把 Context 构造收口为根据权威 Run/Step/Lease 服务端派生，或让 usage 服务同时验证可写状态、step 归属和 deadline。新增“先通过 `_can_send`、再令 `create_running` 失败”测试，断言异常/安全结果后另一个 permit 仍可 acquire；并覆盖伪造 step/model attempt 被拒绝。

## 已确认的修复

- `create_running` 将 `capability_snapshot_json`、`prompt_id`、`prompt_version` 和 pricing metadata 固定为 `None`；新增私密字符串测试确认不入 usage 表（`app/services/model_usage_service.py:47-60`）。
- Gateway 在 `mark_started`/usage started 后再次 `_can_send`，测试覆盖该窗口发生撤权时 `provider.calls == 0`（`app/runtime/model_gateway.py:227-232`）。
- `LeaseService.can_write` 校验数据库 lease、context lease 与 run deadline；Gateway 也在 acquire 前和每次发送边界检查 request deadline/lease（`app/services/lease_service.py:87-104`；`app/runtime/model_gateway.py:202-204, 266-278`）。
- 成功响应只解析 `usage` 的 token 数值，不持久化正文；输入、输出价格均参与成功成本计算，缺失 usage 时保留预留成本（`app/runtime/model_gateway.py:258-296`；`app/services/model_usage_service.py:92-119`）。
- 普通 `mark_started` 与 `settle` 采用状态条件 UPDATE；独立 session 的顺序重放不能覆盖已经 settled 的记录。
- 已复跑：`poetry run pytest tests/test_model_gateway.py tests/test_provider_traffic_controller.py -q`（**25 passed**）；`poetry run ruff check app/runtime/model_gateway.py app/services/model_usage_service.py app/services/lease_service.py tests/test_model_gateway.py`（passed）。

---

# Task 2 新增 P1 修复复审

DONE

## 结论

**仍不通过：已修复的三项 P1 可验收，但 route capability 授权仍缺失。**

`mark_expired_running_unknown` 已成为包含 in-flight status 与 deadline 条件的单条 UPDATE；建账异常路径会 settle 已 acquire 的 permit；`ModelCallContext` 改为从权威 Run/运行中 Step 派生，并在 usage 建账时复核 Run/Step/attempt/预算/deadline。定向测试 **27 passed**，Ruff 通过。

但 Gateway 仍仅把调用方的 `route_id` 交给全局 `ModelRouteRegistry.get`，没有将该 route 与权威 Step/Agent capability 绑定或验证。这不满足设计明确的“解析 route 后校验 capability”边界：任一持有有效 Run/Step/Lease 的内部调用方可选择任何部署中已注册（包括更高权限、更高成本）的模型 route。

## 剩余 P1：没有 capability → route 的权威授权校验

- 位置：`app/runtime/model_gateway.py:197-213`；`ModelCallContext.from_authoritative` 的字段/查询位于 `:112-183`。
- 证据：Context 只派生 `run_id`、`step_id`、attempt、token estimate 与 deadline，未携带或查询允许的 `route_id` 集合；`ModelGateway.call` 在任何 lease/capability 检查前执行 `route = self._routes.get(route_id)`，之后也没有 route authorization 判断。`AgentStep.input_summary` 仅用于 `estimated_input_tokens`，无 route allowlist 约束。
- 建议：从冻结 AgentPackage/Run capability snapshot 或 Step 的受信任定义中派生允许的 route ID（最好为单一不可变 route/fingerprint），将其加入不可伪造的 Context，并在 acquire 前拒绝不匹配的 `route_id`，不创建 usage、不发送 Provider。补充同一有效 Run/Step 请求“允许 route 成功、另一个已注册 route 被拒绝”的测试。

## 已确认修复

- `ModelUsageService.mark_expired_running_unknown` 使用 `UPDATE ... WHERE status IN ('running','started') AND request_deadline_at < now`，已完成状态不会被 reconciler 覆盖（`app/services/model_usage_service.py:121-139`）。
- `create_running` 异常由 Gateway 捕获并立即 `traffic.settle`；新增测试确认随后 permit 可重新 acquire（`app/runtime/model_gateway.py:216-222`）。
- `ModelCallContext` 关闭默认 dataclass 构造，仅 `from_authoritative` 能以私有 factory token 实例化；它要求 Run/Step 归属、运行中 Step、execution attempt、owner/fencing 匹配。usage 服务额外复核 step attempt、估算 token 和 deadline，伪造 Step/attempt/预算不能落库（`app/runtime/model_gateway.py:112-183`；`app/services/model_usage_service.py:21-60`）。
- 已复跑：`poetry run pytest tests/test_model_gateway.py tests/test_provider_traffic_controller.py -q`（**27 passed**）；`poetry run ruff check app/runtime/model_gateway.py app/services/model_usage_service.py app/services/lease_service.py tests/test_model_gateway.py`（passed）。

---

# Task 2 最终复审：route capability 授权

DONE

## 结论

**不通过：拒绝逻辑可验收，但 capability 的权威生产来源未接通，导致所有正常创建的 Run 都无法调用模型。**

实现正确地从 `run.capability_snapshot_json.allowed_model_route_ids` 派生不可变 allowlist；缺失或畸形值会变成空集合；未授权 route 在 acquire、usage、HTTP 之前返回 `route_not_allowed`。定向测试 **28 passed**，Ruff 通过。

但全仓库检索显示只有模型测试手工写入 `allowed_model_route_ids`。实际 `AgentRunService.create` 冻结 capability snapshot 时仅写入 agent/package/connector 身份，并未写入该字段，也不存在从 AgentPackage 定义提取 route allowlist 的代码。因而所有通过正常 API 创建的 Run 都会得到空 allowlist，并永久 `route_not_allowed`；这不是可上线的 capability 授权实现。

## P1：生产 Run 从不获得模型 route capability

- 证据：`app/runtime/model_gateway.py:190-199` 对缺失值返回空 `frozenset()`，`ModelGateway.call` 在 `:254-256` 拒绝空集合；但 `app/services/agent_run_service.py:75-82` 写入的 `capability_snapshot_json` 没有 `allowed_model_route_ids`。`rg` 结果显示该字段只在 gateway/usage 与 `tests/test_model_gateway.py` 中出现，没有定义到 Run 的生产创建/冻结路径。
- 影响：fail-closed 行为本身是安全的，但全部实际 Run 被拒绝，模型 Gateway 没有可用的授权成功路径；当前测试以手工构造 `AgentRun` 掩盖了集成缺口。
- 修复建议：在受信任 AgentPackage 定义中引入并校验允许的 route ID，创建 Run 时将经过验证的 allowlist 冻结进 capability snapshot；同时拒绝未在部署 route registry 中注册的定义 route。增加端到端测试：通过 `AgentRunService` 创建的允许模型 Run 能调用被授权 route，而缺失/字符串/混合类型/未授权 route 均在 permit、usage、HTTP 前拒绝。

## 已确认项

- `allowed_routes_from_snapshot` 仅接受非空字符串组成的 `list`；缺失、非 Mapping、非 list 或任一畸形元素均返回空 allowlist（`app/runtime/model_gateway.py:190-199`）。
- Gateway 在 `traffic.acquire` 之前检查 `route_id in context.allowed_route_ids`；未授权测试断言 `redis.permits == {}`、usage 不存在、Provider 调用次数为零（`app/runtime/model_gateway.py:254-262`；`tests/test_model_gateway.py:367-383`）。
- Usage 服务再次把 Context allowlist 与当前 Run 冻结 snapshot 对比，并确认 route 属于该集合，避免由 Context/usage 直接调用绕过（`app/services/model_usage_service.py:50-57`）。
- 已复跑：`poetry run pytest tests/test_model_gateway.py tests/test_provider_traffic_controller.py -q`（**28 passed**）；`poetry run ruff check app/runtime/model_gateway.py app/services/model_usage_service.py app/services/lease_service.py tests/test_model_gateway.py`（passed）。

---

# Task 2 最终复审：真实 Run route 冻结与 Settings 注入

DONE

## 结论

**可验收。** 真实 API 创建 Run 时，`AgentRunService` 仅接收从应用 Settings 解析的、已验证的服务端 `ModelRoute` ID，并把该集合冻结到 `capability_snapshot_json.allowed_model_route_ids`。业务 `command.input` 的同名字段只会保留在业务 input，不能覆盖或扩大 capability snapshot；Gateway/usage 继续以冻结 snapshot 执行 allowlist 校验。

## 核验结果

- `create_run` 从 `request.app.state.settings.model_routes` 取得路由 ID，再以关键字参数传给 `AgentRunService`；没有从 HTTP command/input 读取路由、endpoint、价格或限流配置（`app/api/endpoints/agent_runs_api.py:134-142`）。
- `Settings.MODEL_ROUTES_JSON` 在加载与属性解析时都经 `ModelRouteRegistry` 验证，route ID 列表因此来自服务端部署配置（`app/core/config.py:261-278, 385-407`）。
- `AgentRunService` 对注入 ID 去重、排序并冻结；snapshot 的 `allowed_model_route_ids` 不引用 `command.input`（`app/services/agent_run_service.py:32-50, 84-97`）。
- `test_created_run_freezes_server_routes_and_rejects_command_route_override` 令 input 伪造 `allowed_model_route_ids=["other"]`，断言 snapshot 仍是服务端 `["summary"]`；随后实际 Gateway 只成功调用 `summary`，拒绝 `other`（`tests/test_model_gateway.py:388-435`）。
- 已复跑：`poetry run pytest tests/test_model_gateway.py tests/test_provider_traffic_controller.py tests/runtime_test_runtime_dispatch_semantics.py tests/test_runtime_agent_run_api.py -q`（**38 passed**，仅既有 Starlette/httpx deprecation warning）；对应 Ruff 检查通过。
