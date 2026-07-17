# Task 1: Freeze policy limits and implement PolicyEngine

## Goal

为现有 `ModelGateway` 添加只读、确定性的模型调用准入：冻结 package 的安全预算子集，按可信 Run 与持久 usage 限制调用次数和保守成本。拒绝必须发生在 permit、usage 和 Provider HTTP 调用之前。

## Files

- Create: `app/runtime/policy_engine.py`
- Modify: `app/schemas/agent_package.py`
- Modify: `app/services/agent_run_service.py`
- Modify: `app/runtime/model_gateway.py`
- Modify: `tests/test_model_gateway.py`
- Modify: `tests/test_runtime_agent_run_api.py` and/or `tests/test_runtime_agent_run_service.py`

## Binding requirements

- `PackagePolicy` 增加可选 `max_model_calls: int | None`、`max_model_cost: float | None`，均只允许非 bool、非负有限值；缺失不代表零额度。
- 由于 Run 只保存 `AgentDefinition.definition_json`，`AgentRunService.create()` 必须从权威 definition 的 `policy` 冻结唯一允许的 `model_policy` 子集；请求 body/input 的同名字段不能影响该快照。
- 新建 `PolicyDecision(allowed: bool, code: str | None)` 与 `PolicyEngine(session).evaluate(context, route)`。
- PolicyEngine 只读取权威 `AgentRun` 与同一 Run 的 `AgentModelUsage`，不能相信调用者提供的预算或成本。
- `max_model_calls`：已有 usage 行数达到上限立即拒绝，code `MODEL_CALL_LIMIT_EXCEEDED`。
- `max_model_cost`：每个 usage 优先取非负有限 `estimated_cost`，为空再取 `reserved_estimated_cost`；当前调用额外预留 `route.input_price * context.estimated_input_tokens / 1000`；达到或超过上限拒绝，code `MODEL_COST_LIMIT_EXCEEDED`。
- 运行状态不可执行时拒绝，code `MODEL_RUN_NOT_EXECUTABLE`；不得创建 usage、permit 或 Provider HTTP。
- `ModelGatewayResult` 增加安全的可选 `error_code`。policy 拒绝 status 固定 `policy_denied`。
- ModelGateway 仅在 route allowlist 与 deadline 检查之后、`traffic.acquire()` 之前执行 policy。日志只允许 run_id/step_id/route_id/安全 code，不含 request/response/prompt/token/正文。
- 不新增数据库表，不修改请求 API 契约，不引入自动重试。

## Required tests (TDD)

1. 先新增失败测试：已有一个同 Run `AgentModelUsage`、冻结 `max_model_calls=1` 时，Gateway 返回 `policy_denied/MODEL_CALL_LIMIT_EXCEEDED`；Provider 调用数为 0，usage 行数不增加。
2. 先新增失败测试：`outcome_unknown` usage 的 reserved/estimated 成本占用预算；当前调用的输入 token 预留也会使剩余额度不足时在 Provider 前拒绝。
3. 创建 Run 的 API/service 测试：请求携带伪造 `model_policy` 不会写入；definition 的合法 policy 被冻结；非法 bool/负数/NaN package policy 被 loader/schema 拒绝。
4. 运行：
   `poetry run pytest tests/test_model_gateway.py tests/test_runtime_agent_run_api.py tests/test_runtime_agent_run_service.py -q`

## TDD and workspace requirements

- 严格先测试、确认红，再写最小生产实现，报告中保存 RED/GREEN 命令和结果。
- 代码、属性、方法尽可能补充简洁中文注释；日志只用安全摘要。
- 工作区有其他人的未提交改动：只修改本任务文件，不重置、覆盖、暂存、提交或使用破坏性 Git 命令。
- 报告写入 `.superpowers/sdd/model-governance-task-1-report.md`，包含实现、RED/GREEN、测试、文件清单和自查。
