# Model Execution Governance Task 2 报告

## 完成内容

- 为 `ModelRoute` 增加默认关闭的 route 级熔断配置，并校验类型、非负性、有限性及关闭状态组合。
- 在既有 Redis Lua permit 脚本中原子实现连续失败记录、成功清零和 acquire 前的熔断拒绝。
- 熔断 Redis key 严格使用 `route_id`：
  `model_gateway:circuit_failures:{route_id}` 与
  `model_gateway:circuit_open:{route_id}`。
- Gateway 对 timeout/network error 和 HTTP 5xx 记录可确认失败；HTTP 429 仅使用原有 Retry-After；可交付成功才清零。
- Redis 不可用维持 acquire fail-closed；记录熔断状态失败不会改变已得到的 Provider 调用结果。

## TDD 记录

### RED

1. `poetry run pytest tests/test_provider_traffic_controller.py -q`
   - 新增熔断测试失败：`ModelRoute.__init__()` 不接受
     `circuit_failure_threshold`，证明字段和控制器接口尚未实现。
   - 在测试 FakeRedis 已按目标协议扩展的前提下，既有 acquire 也因新参数尚未由生产代码提供而 fail-closed，属缺失实现导致。
2. `poetry run pytest tests/test_model_gateway.py -q`
   - 3 个新增 Gateway 测试失败：timeout/HTTP 503 后第二次调用仍为
     `outcome_unknown`，而不是 `circuit_open`，证明 Gateway 尚未记录熔断结果。

### GREEN

`poetry run pytest tests/test_provider_traffic_controller.py tests/test_model_gateway.py -q`

结果：`50 passed in 2.87s`

## 修改文件

- `app/runtime/model_gateway.py`
- `tests/test_provider_traffic_controller.py`
- `tests/test_model_gateway.py`
- `.superpowers/sdd/model-governance-task-2-report.md`

未修改 `app/core/config.py`：其 route 解析使用 `ModelRoute.from_mapping`，缺失的新字段会正确采用 dataclass 默认值，未知字段仍由构造函数拒绝。

## 自查

- circuit-open 在 Redis Lua acquire 中、permit 分配前检查；拒绝时不会创建 permit 或发起 Provider HTTP。
- 429 未调用失败记录；未知 Python 异常与 `response_discarded` 未清零也未累计。
- 测试覆盖 threshold=0、route 隔离、Redis eval 异常、连续失败、成功重置、429、timeout 与 5xx。
- `git diff --check -- app/runtime/model_gateway.py tests/test_provider_traffic_controller.py tests/test_model_gateway.py` 无输出。

## 关注点

- 熔断 open 窗口到期后会恢复 acquire；由于需求明确不做半开探测，保留连续失败计数，后续再次确认失败会立即重新打开熔断。
- 共享工作区存在其他任务的脏文件；本任务未暂存、提交、重置或修改总控计划。

## 审查修复（circuit preflight 与 disabled 保持状态）

### 根因与最小修复

- 原实现只在 `acquire` Lua 分支检查 circuit，因此 Gateway 已先调用
  `PolicyEngine.reserve`；虽然 acquire 拒绝后会删除 reservation，稳定打开的
  circuit 仍不满足“reserve/usage 为零”。新增同一 Lua 状态真相的只读
  `circuit_preflight`，并在 Gateway 的 policy reservation 前 fail-closed 调用。
- `threshold=0` 的 `record_circuit_success` 原本仍会执行 Lua `DEL`。两个
  controller 记录入口现在均本地短路为 `circuit_disabled`，不读取、创建、删除或
  修改任何既有 circuit key。
- preflight 与 acquire 间若 circuit 新打开，既有 acquire `circuit_open` 拒绝分支
  仍会 `cancel_reservation`；新增回归测试确认没有 usage artifact，也不会触网。

### RED

`poetry run pytest tests/test_provider_traffic_controller.py::test_disabled_circuit_does_not_mutate_existing_circuit_state tests/test_model_gateway.py::test_open_circuit_preflight_skips_policy_reservation_and_provider tests/test_model_gateway.py::test_circuit_opened_between_preflight_and_acquire_cancels_reservation -q`

- 结果：`2 failed, 1 passed`。
- stable open case 显示 `policy.reserve_calls == 1`，证实预检发生得太晚；disabled
  success case 返回 `circuit_reset`，证实其会改变既有 circuit 状态。

### GREEN

`poetry run pytest tests/test_provider_traffic_controller.py tests/test_model_gateway.py -q`

结果：`53 passed in 2.68s`。

同时运行：

`git diff --check -- app/runtime/model_gateway.py tests/test_provider_traffic_controller.py tests/test_model_gateway.py .superpowers/sdd/model-governance-task-2-report.md`

无输出。
