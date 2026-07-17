# Model Execution Governance — Task 1 报告

## 实现

- `PackagePolicy` 增加 `max_model_calls` 与 `max_model_cost`；拒绝 bool、负数和非有限成本，缺失额度保持为不限额。
- `AgentRunService.create()` 仅从权威 `AgentDefinition.definition_json.policy` 校验并冻结 `model_policy`，请求 `input` 中的同名字段不会写入快照。
- 新增只读 `PolicyEngine(session)` 与 `PolicyDecision`：只读取权威 `AgentRun` 和同 Run `AgentModelUsage`，在调用次数、保守成本或不可执行状态时返回受控拒绝码。
- `ModelGateway` 在 route allowlist、deadline 检查之后、流量 permit 之前执行策略；拒绝返回 `policy_denied` 和安全的 `error_code`，日志只记录 run/step/route/code。
- Worker 装配 `PolicyEngine(session)`，使实际模型网关不可跳过策略。

## TDD 记录

### RED

先新增调用次数上限、未知结果的预留成本，以及 Run 创建快照/非法策略测试，然后运行：

```text
poetry run pytest tests/test_model_gateway.py tests/test_runtime_agent_run_service.py -q
```

结果：`3 failed, 26 passed`。

- 两个网关用例因 `ModelGatewayResult` 尚无 `error_code` 且调用未被策略拒绝失败。
- Run 创建用例因快照中没有 `model_policy` 失败。

### GREEN

最小实现后，首次运行发现配置加载到 ORM 的导入环；按堆栈定位后将 `PolicyEngine` 的 ORM 导入延迟到 `evaluate()`，避免影响 Settings 初始化。随后运行：

```text
poetry run pytest tests/test_model_gateway.py tests/test_runtime_agent_run_service.py -q
```

结果：`29 passed in 1.72s`。

## 最终验证

按任务简报指定命令运行：

```text
poetry run pytest tests/test_model_gateway.py tests/test_runtime_agent_run_api.py tests/test_runtime_agent_run_service.py -q
```

结果：`30 passed, 1 warning in 2.21s`，警告为 Starlette/TestClient 对当前 httpx 的既有弃用提示。

另执行 `git diff --check`，无空白错误。

## 文件清单

- 新增：`app/runtime/policy_engine.py`
- 修改：`app/schemas/agent_package.py`
- 修改：`app/services/agent_run_service.py`
- 修改：`app/runtime/model_gateway.py`
- 修改：`app/worker.py`（仅注入 PolicyEngine）
- 修改：`tests/test_model_gateway.py`
- 修改：`tests/test_runtime_agent_run_service.py`

## 自查

- policy 拒绝发生在 permit、usage 和 Provider HTTP 之前。
- 成本累计优先使用有效 `estimated_cost`，否则使用有效 `reserved_estimated_cost`；当前调用预留输入成本后以“达到或超过”上限拒绝。
- 未增加数据库表、未改动请求 API 契约、未引入重试。
- 工作区已有的其他改动未重置、暂存或提交；未修改总控计划。

## 审查修复（不可执行状态与并发预算）

### RED

先新增 Run 在构造 Context 后发生取消、隐私状态变化、授权版本变化或 fencing
变化时的拒绝用例，并逐一断言 `traffic.acquire == 0`、usage 为 0、Provider 为 0；
再新增两个独立 Session 在首个 Provider 调用尚未返回时竞争同一 Run 的次数/成本预算用例。

```text
poetry run pytest tests/test_model_gateway.py tests/test_runtime_agent_run_service.py -q
```

结果：`4 failed, 32 passed`。不可执行 Run 仍可能返回成功或在 permit 后中止。

```text
poetry run pytest tests/test_model_gateway.py::test_concurrent_sessions_only_one_call_acquires_traffic_for_one_call_budget -q
```

结果：`1 failed`；第二个 Session 已进入 `traffic.acquire`，证明原先仅查询 usage
不能阻止并发预算穿透。

### GREEN

- `PolicyEngine` 现在在 permit 前回读并验证 Run 的取消、隐私、授权版本、execution
  attempt、lease owner、fencing token 与未过期 lease；任一条件不满足统一返回
  `policy_denied/MODEL_RUN_NOT_EXECUTABLE`。
- 对允许调用，以同一 Run 的 `status_version` 条件更新串行化“读取账本 + 写入预留”，并
  将 `reserved` usage 先提交。这样并发 Session 会在自己的 policy 阶段看到既有预留；次数和
  成本额度都不会让第二次调用进入 `traffic.acquire`。未取得 permit 的预留会删除并释放预算。
- 扩展 `PackagePolicy` 非法成本额度覆盖：负数与 `NaN`。

### 最终验证

```text
poetry run pytest tests/test_model_gateway.py tests/test_runtime_agent_run_api.py tests/test_runtime_agent_run_service.py -q
```

结果：`39 passed, 1 warning in 3.48s`。唯一警告仍为既有 Starlette/TestClient 的
httpx 弃用提示。随后执行 `git diff --check`，无空白错误。
