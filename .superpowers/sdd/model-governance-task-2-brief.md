# Task 2: Add atomic route circuit breaker

## Goal

在现有 Redis Lua `ProviderTrafficController` 中增加 route 级连续失败熔断。熔断是服务端 route 的安全属性，与 429 Retry-After 限流完全独立；Redis 故障必须 fail-closed。

## Files

- Modify: `app/runtime/model_gateway.py`
- Modify: `app/core/config.py`（仅在现有 route 解析确有显式未知字段拒绝逻辑时调整；不得为此重构 Settings）
- Modify: `tests/test_provider_traffic_controller.py`
- Modify: `tests/test_model_gateway.py`

## Binding requirements

- `ModelRoute` 新增默认关闭的 `circuit_failure_threshold: int = 0` 与 `circuit_open_seconds: float = 0.0`。
- threshold 必须是非 bool、非负 int；open seconds 必须是非 bool、非负有限数；threshold 为 0 时 open seconds 必须是 0。route JSON 缺失字段保持现有行为。
- Redis 键严格按 `route_id` 隔离：`model_gateway:circuit_failures:{route_id}`、`model_gateway:circuit_open:{route_id}`；不得按 rate_limit_key、用户、业务输入或正文建熔断键。
- 在同一 Lua 脚本中原子实现：`circuit_failure` 增加连续失败，在阈值达到时写 open-until；`circuit_success` 删除失败计数和 open 状态；`acquire` 在分配 permit 之前读取 open 状态并返回 `circuit_open` 和 retry-after。
- 网络错误、timeout、HTTP 5xx 为可确认失败；HTTP 429 仅走既有 Retry-After，不累计熔断；成功且响应未被状态边界丢弃时清零失败；任意未知 Python 异常和 `outcome_unknown` 不清零也不累计。
- circuit 被拒绝时没有 permit、usage 或 Provider HTTP。record circuit 时 Redis 失败仅返回/记录安全失败，不得把已有 Provider 成功伪装为失败；`acquire` Redis 不可用维持 `redis_unavailable` fail-closed。
- 日志只允许 route_id、状态、计数或 retry-after；不得记录 request/response/prompt/模型正文/token/用户标识。
- 不新增表，不修改业务请求 API，不实现自动重试或半开探测。

## Required tests (TDD)

1. 连续两次可确认失败（threshold=2）后 open，新的 acquire 返回 `circuit_open` 和准确 retry-after。
2. 成功清零失败计数；429 只设置 blocked/Retry-After，之后仍可记录为 first failure 而非第二次；HTTP 503 计入失败。
3. route A 打开不影响 route B；threshold=0 完全不启用熔断；Redis eval 异常 fail-closed。
4. ModelGateway 真实路径：timeout/5xx 后后续调用在 permit 前返回 circuit_open，429 不开熔断，成功响应清零；测试 Provider 不被熔断拒绝路径调用。
5. 先新增测试并记录 RED，再写最小实现，最后运行：
   `poetry run pytest tests/test_provider_traffic_controller.py tests/test_model_gateway.py -q`

## Workspace requirements

- 工作区为共享脏树：仅修改本任务相关文件；不得 git add/commit/reset/checkout，不得改总控计划。
- 代码/方法尽可能有简洁中文注释，日志使用安全摘要。
- 完整报告写到 `.superpowers/sdd/model-governance-task-2-report.md`，包括 RED/GREEN 命令、测试、文件清单、自查与疑虑。
