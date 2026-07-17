# Task 3 完成报告：Worker draining 模型调用守卫

## 实现结果

- `ModelGateway` 新增最小 `ModelCallGuard` 协议；未注入时默认允许，保持非 Worker 调用兼容。
- circuit preflight 之后、policy reservation/Redis permit 之前执行 guard；guard 拒绝统一返回 `aborted_before_send`，不创建 usage、permit 或 Provider 请求。
- permit 已获得后，在既有 HTTP 前 `_can_send` 复核中包含 guard；draining 变化时结算 usage 为 `aborted_before_send` 并释放 permit，Provider 不触网。
- `configured_model_gateway(session, *, is_draining=...)` 以实时 callable 创建 Worker draining guard；`configured_executor` 透传该 callable，`WorkerLoop` 对支持 `is_draining` 的 executor factory 传入同一个 `self._is_draining`。
- 日志仅包含 run_id、step_id、route_id 与固定安全原因 `worker_draining`，没有请求、响应、prompt、token 或正文。
- 未向 AgentRun、checkpoint 或 artifact 写入 draining 状态。

## TDD 记录

### RED

先在 `tests/test_model_gateway.py` 添加：

1. 始终 draining 时，禁止 policy/permit/usage/Provider；
2. permit 获取后 guard 切换 draining 时，usage/permit 安全收敛且 Provider 不触网。

并在 `tests/runtime_test_worker_entry.py` 添加 Worker 装配的实时 draining 回归测试。

执行：

```text
poetry run pytest tests/test_model_gateway.py tests/runtime_test_worker_entry.py -q
3 failed, 40 passed
```

预期失败原因：`ModelGateway.__init__()` 尚不接受 `call_guard`，`configured_model_gateway()` 尚不接受 `is_draining`。

### GREEN

最小实现 guard 注入、发送前复核与 Worker callable 透传后执行：

```text
poetry run pytest tests/test_model_gateway.py tests/test_memoir_model_gateway.py tests/runtime_test_worker_entry.py -q
47 passed in 3.32s
```

额外验证：

```text
git diff --check
poetry run ruff check app/runtime/model_gateway.py app/runtime/memoir_model_gateway.py app/worker.py
All checks passed!
```

## 关注点

- 全工作区的测试文件 ruff 检查仍会命中既有的 `tests/test_model_gateway.py:819` `B009`，与本任务新增 draining 测试无关；未为避免扩大范围而改动该既有断言。
- 工作区存在其他并行任务的脏文件；本任务未执行 git add/commit/reset/checkout，也未改总控计划。

## P2 补测：Worker executor factory draining callable 装配

在 `tests/runtime_test_worker_entry.py` 新增
`test_worker_passes_live_draining_callable_to_keyword_executor_factory`：使用接受
`is_draining` keyword 的 executor factory 记录所收 callable，断言其与传入
`WorkerLoop` 的 callable 为同一对象；切换外部 draining 状态后，factory 所保存的
callable 实时返回新状态。

本次仅允许补充测试且生产实现已存在，新增测试无法在未改动生产代码的当前工作区
获得有效 RED；首次执行即为 GREEN：

```text
poetry run pytest tests/runtime_test_worker_entry.py -q -k worker_passes_live_draining_callable_to_keyword_executor_factory
1 passed, 9 deselected in 0.76s
```
