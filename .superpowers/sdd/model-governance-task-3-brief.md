# Task 3: Inject Worker draining guard

## Goal

Worker 开始 draining 后，任何尚未开始的模型调用必须在 Redis permit、usage 和 Provider HTTP 之前停止；已发送请求由现有 timeout/lease 收敛。

## Files

- Modify `app/runtime/model_gateway.py`, `app/runtime/memoir_model_gateway.py`, `app/worker.py`
- Modify `tests/test_model_gateway.py`, `tests/test_memoir_model_gateway.py`, `tests/runtime_test_worker_entry.py`

## Binding requirements

- 新增最小 `ModelCallGuard` protocol，`permits_new_call(context) -> bool`；默认允许，保持现有非 Worker 用法。
- Gateway 在 circuit preflight 之后、policy reserve 和 permit 前检查 guard；并在既有 permit 后及 HTTP 前的 `_can_send` 复核中检查 guard。拒绝统一 `aborted_before_send`，不创建 usage/permit/HTTP。
- 日志只可含 run_id、step_id、route_id 和安全 reason，不含 request/response/prompt/token/正文。
- `configured_model_gateway(session, *, is_draining=...)` 以实时 callable 构造 guard；`configured_executor` 与 `WorkerLoop` 将同一个 `self._is_draining` 传到 executor factory。不得把 draining 写入 AgentRun/checkpoint/artifact。
- 只实现 draining 守卫，不新增宽限期状态机、数据库字段、重试。

## Required TDD tests

1. Always-draining guard：Gateway 返回 aborted_before_send，Provider=0、usage=0、traffic acquire=0。
2. guard 在 permit 获取后改变为 draining：HTTP 前拒绝，已获得 permit/usage 被安全释放或 aborted。
3. Worker 配置装配测试：`is_draining=True` 的 Memoir 模型节点不会触网，false 时既有成功路径不变。
4. 先 RED 再 GREEN；执行 `poetry run pytest tests/test_model_gateway.py tests/test_memoir_model_gateway.py tests/runtime_test_worker_entry.py -q`。

## Workspace

共享脏树，仅改本任务文件；禁止 git add/commit/reset/checkout，不改总控计划。中文注释与安全日志。完整报告写 `.superpowers/sdd/model-governance-task-3-report.md`，含 RED/GREEN 与测试结果。
