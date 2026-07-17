# ModelGateway Task 1 审查

DONE

## 结论

**不通过（需修复以下 P1 问题后再进入 Task 2）。** 基础 fail-closed、正常路径的并发/RPM/TPM 限制、重复 settle 与共享 Retry-After 均有实现和定向测试；但 permit 的一次性状态转换和路由绑定尚不安全，可能使同一个 permit 触发多次 Provider 请求或绕过目标 route 的计数。此外，较短的 Retry-After 会缩短既有共享冷却窗口。

## 发现

### P1：`mark_started` 不是一次性发送权，重复调用会获得同样的 `started` 成功结果

- 位置：`app/runtime/model_gateway.py:128-132`、`app/runtime/model_gateway.py:165-166`
- 影响：第一个调用将 `acquired` 置为 `started` 并返回 `started`；随后任意重复/并发调用也因 `return {state, 0}` 返回 `started`。若 Gateway 的发送判断是 `status == "started"`，两个 worker 都会发送同一请求，而 Redis 只记录一个 permit，导致实际 Provider 并发、RPM/TPM 超出限制。
- 同样地，`acquire` 未先检查 permit 状态，重复 acquire 会在 `:124` 把已 `started`/`settled` 的 permit 重置为 `acquired`。
- 建议：保留可查询的幂等状态，但为“本次调用取得发送权”返回独立结果（如 `started_now` / `already_started`）；acquire 对已有 permit 只返回确定的既有状态，绝不可重置状态或重新计数。补充两个 controller 共享同一 `permit_id` 的并发/replay 测试，断言仅一个调用可进入发送分支。

### P1：permit 未绑定到 acquire 时的 route，可用一个 route 的 permit 发送/结算另一个 route

- 位置：`app/runtime/model_gateway.py:124` 写入了 `route`，但 `:128-143` 从未校验它；Python 侧 `:165-176` 也未校验。
- 影响：先以 route A acquire 后，可用同一 `permit_id` 调用 `mark_started(route B, ...)` 并得到 `started`，却完全没有计入 B 的 active/RPM/TPM；`settle(route B, ...)` 也只从 B 的 active 集合删除，遗留 A 的计数直至 TTL。该行为破坏冻结路由和共享限流边界。
- 建议：Lua 在 `started`、`settle`（以及重复 acquire）中比较 permit hash 的 `route` 与传入 route，不匹配即拒绝且不修改任何计数；增加跨 route permit 重用测试。

### P1：较短的 Retry-After 能覆盖并缩短已存在的共享冷却

- 位置：`app/runtime/model_gateway.py:140-142`
- 影响：worker A 先写入 60 秒冷却，worker B（已获 permit 的在途请求）随后以 1 秒 `retry_after_seconds` settle，会无条件 `SET` 为较短时间。其他 worker 随后可过早 acquire，违反 429 共享退避。
- 建议：写入 `max(当前 blocked_until, now + retry_after)`，并让 Redis key 的 TTL 覆盖这个最大时间；补充“长冷却后写短冷却仍保持长冷却”的跨 worker 测试。

### P1：部署配置入口绕过了已实现的 route registry，重复 route ID 未被拒绝

- 位置：`app/core/config.py:374-387`；对照 `app/runtime/model_gateway.py:73-89`
- 影响：`Settings.model_routes` 直接返回 list，未使用 `ModelRouteRegistry`，因此 `MODEL_ROUTES_JSON` 中相同 `route_id` 且不同 endpoint/价格/限流的配置会通过校验。后续消费者若按顺序或 dict 转换选择 route，会出现配置覆盖/歧义，无法满足 route 注册的不可覆盖边界。
- 建议：配置加载时构造并暴露 `ModelRouteRegistry`（或至少调用其重复 ID 校验），并补充重复 ID 配置失败测试。

## 已确认项

- Redis `eval` 异常或脚本响应异常被转换为 `redis_unavailable`，没有进程内放行回退（`app/runtime/model_gateway.py:178-185`）。
- 对不同 permit 的正常路径，Lua 原子维护 active、RPM 与 TPM 滑动窗口；现有测试覆盖并发、RPM、TPM、settle 一次释放和基础跨 worker 冷却。
- 当前模块日志只含 operation 与 `route_id`，未记录 prompt、Provider 正文或密钥。
- 已复跑：`poetry run pytest tests/test_provider_traffic_controller.py -q`（7 passed）与 `poetry run ruff check app/runtime/model_gateway.py app/core/config.py tests/test_provider_traffic_controller.py`（passed）。

---

# Task 1 P1 修复复审

DONE

## 结论

**仍不通过：route binding 只绑定到 `rate_limit_key`，未绑定冻结的 `route_id`。** single-start、共享 Retry-After 只延长、配置重复 route ID 拒绝均已修复并由新增测试覆盖。

## 仍存 P1

### permit 可跨共享配额的不同 route 使用

- 位置：`app/runtime/model_gateway.py:104` 将脚本参数 `route` 设为 `route_key`，Python 侧 `:183-185` 的 `route_key` 仅由 `route.rate_limit_key` 构成；`acquire/started/settle` 的匹配因此都是对该 key（`:108-111`、`:136`、`:144`）。
- 影响：两个不同 `route_id`（可以有不同 provider/model/endpoint）只要共享同一 `rate_limit_key`，permit A 就能对 route B `mark_started` / `settle` 而不返回 `route_mismatch`。这仍允许用 A 的授权调用 B 的 endpoint，违反冻结路由边界；当前测试只覆盖不同 `rate_limit_key` 的 B，无法检出。
- 建议：在 permit hash 中另存 `route.route_id`（或不可变 route fingerprint），脚本的 `started`、`settle` 与重复 `acquire` 必须比较该标识；同时继续以 `rate_limit_key` 作为 active/RPM/TPM 的共享计数键。新增“不同 route_id、相同 rate_limit_key 必须 route_mismatch”的测试。

## 已确认的修复

- `mark_started` 仅首个 `acquired -> started` 返回 `started`，后续返回 `already_started`；重复 acquire 只返回既有状态，不再重置 permit（`app/runtime/model_gateway.py:108-111, 133-139`）。
- `settle` 以现有 `blocked_until` 和新冷却截止时间的最大值写入，并以该差值设置 TTL，短冷却不会覆盖长冷却（`:148-152`）。
- `MODEL_ROUTES_JSON` 的字段验证和 `model_routes` 属性均调用 `ModelRouteRegistry`，重复 `route_id` 在 Settings 创建时被拒绝（`app/core/config.py:264-277, 390-401`）。
- 已复跑：`poetry run pytest tests/test_provider_traffic_controller.py -q`（12 passed）及对应 Ruff 检查（passed）。

---

# Task 1 最终复审：route_id 绑定

DONE

## 结论

**可验收。** permit 现以冻结的 `route_id` 绑定，而并发/RPM/TPM 与 blocked 仍按 `rate_limit_key` 共享，满足“路由身份不可跨用、同配额可共享限流”的边界。

## 核验结果

- acquire 将 `route_id` 写入 permit hash，并对重放 acquire 比较该值（`app/runtime/model_gateway.py:108-112, 130`）。
- `mark_started` 与 `settle` 分别比较传入的 `route_id`；不匹配即返回 `route_mismatch`，不转换状态或释放计数（`:134-146`）。
- Python 调用为 acquire、start、settle 分别按 Lua 参数位置传入 `route.route_id`（`:170-181`），而 `route_key` 仍只用于共享流控 Redis key（`:183-186`）。
- 测试中的 `other_route` 仅改变 `route_id`、保留相同 `rate_limit_key`，已断言 acquire/start/settle 均为 `route_mismatch`，直接覆盖此前遗漏的场景。
- 已复跑：`poetry run pytest tests/test_provider_traffic_controller.py -q`（12 passed）；`poetry run ruff check app/runtime/model_gateway.py app/core/config.py tests/test_provider_traffic_controller.py`（passed）。
