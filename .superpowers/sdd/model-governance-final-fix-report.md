# Model Governance 最终审查修复报告

## 修复内容

- `PolicyEngine.reserve()` 现在每次回读权威 `AgentRun` 与 `AgentStep`；仅当 Step 仍为
  `running`、其 `execution_attempt` 等于 lease attempt、`step_attempt` 等于
  `ModelCallContext.model_attempt` 时，才创建预算预留。条件更新也包含同一 Step 条件，
  防止检查到写入之间的陈旧 context 穿透。
- Gateway 的 `_can_send()` 在每一个发送前边界（包括紧邻 Provider HTTP 的最后一次）回读
  同一权威事实，因此 Step 变为终态或 attempts 改变后，已取得 permit 的调用也会在触网前中止。
- 熔断启用时强制 `circuit_open_seconds > 0`；Lua 对每次确认 failure 的计数键设置并刷新
  `EXPIRE`（open seconds），故久远 failure 不会永久累计。
- circuit preflight 拒绝新增安全摘要日志；policy、circuit 与 draining 拒绝均有 caplog 回归，
  验证日志不含请求正文或 token。

## TDD 记录

### RED

1. 新增陈旧 Step 的 status/execution attempt/model attempt 三个用例，以及在 reservation 后
   撤销 Step 的 HTTP 前拦截用例：

```text
poetry run pytest tests/test_model_gateway.py::test_stale_step_context_cannot_reserve_or_acquire tests/test_model_gateway.py::test_step_revoked_after_reservation_cannot_reach_provider -q
4 failed
```

失败表现为旧实现仍返回 `succeeded`，证明 reserve 与 HTTP 前检查都漏检了 Step 权威状态。

2. 新增 `(circuit_failure_threshold=1, circuit_open_seconds=0)` 配置用例：

```text
poetry run pytest tests/test_provider_traffic_controller.py::test_route_validates_circuit_configuration -q
1 failed
```

3. 新增时间推进的 failure TTL 用例：

```text
poetry run pytest tests/test_provider_traffic_controller.py::test_old_circuit_failures_expire_before_a_later_failure -q
1 failed
```

第二次久远 failure 原来错误返回 `circuit_opened`。

4. 新增 policy/circuit/draining caplog 安全正文用例：

```text
poetry run pytest tests/test_model_gateway.py::test_model_governance_denial_logs_exclude_request_body -q
1 failed
```

失败原因是 circuit 拒绝尚未产生安全摘要日志。

### GREEN

- Step authoritative recheck 后：`4 passed`。
- 熔断配置校验后：`6 passed`。
- failure TTL 刷新后：`7 passed`（含相关配置用例）。
- circuit 安全日志后：caplog 用例 `1 passed`。

## 最终验证

```text
poetry run pytest tests/test_provider_traffic_controller.py tests/test_model_gateway.py tests/test_memoir_model_gateway.py tests/runtime_test_worker_entry.py -q
76 passed in 3.95s

poetry run ruff check app/runtime/model_gateway.py app/runtime/policy_engine.py tests/test_model_gateway.py tests/test_provider_traffic_controller.py
All checks passed!
```

## 修改文件

- `app/runtime/policy_engine.py`
- `app/runtime/model_gateway.py`
- `tests/test_model_gateway.py`
- `tests/test_provider_traffic_controller.py`
- `.superpowers/sdd/model-governance-final-fix-report.md`

未执行任何 Git 操作。
