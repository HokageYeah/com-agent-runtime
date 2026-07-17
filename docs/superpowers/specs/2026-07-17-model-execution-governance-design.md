# Model Execution Governance Design

## Goal

在既有 `ModelGateway`、Redis permit 与 Worker 执行链路上补齐最小模型调用治理：运行策略准入、Provider 熔断和 draining 安全边界。实现必须拒绝越权、超预算或不可安全执行的调用，且不能记录 prompt、模型原始输出或日记正文。

## Scope

本设计仅覆盖模型调用前的确定性治理，包含：

- `PolicyEngine`：依据冻结 package policy、Run 状态和已持久化 `AgentModelUsage` 聚合，判断单次模型调用是否可发起。
- Redis route 级熔断：在现有 `ProviderTrafficController` 内维护连续可确认 Provider 失败状态。
- `ModelGateway` 调用守卫：在 permit 获取前和真实 HTTP 调用前拒绝不安全调用。
- Worker draining 守卫：draining 或宽限期耗尽后禁止新的模型调用。

本轮不实现 PromptRegistry、结构化输出解析、ContextManager、真实 `workflow.graph.py` 装配，也不引入新进程、新数据库表或外部策略服务。

## Architecture

### PolicyEngine

新增纯服务 `PolicyEngine`，输入为可信 `ModelCallContext`、目标 `ModelRoute` 与当前数据库 session。它只读取：

- `AgentRun` 的 deadline、取消、隐私和授权状态；
- Run 冻结 capability/package policy 中声明的模型调用次数与成本上限；
- 同一 Run 已持久化 `AgentModelUsage` 的尝试次数、实际成本和保守预留成本。

`PolicyEngine` 返回不带业务内容的 `PolicyDecision(allowed, code)`。缺少或畸形 policy 字段采用保守默认值：不额外放宽已有 route 与 deadline 边界；未声明的可选预算上限不限制调用。达到明确上限时返回稳定错误码，例如 `MODEL_CALL_LIMIT_EXCEEDED` 或 `MODEL_COST_LIMIT_EXCEEDED`。

成本聚合采用已结算实际成本；对于 `running`、`outcome_unknown` 等无法确认真实成本的 usage，使用其保守预留/估算成本，绝不按零成本处理。当前尝试的最小预留成本在准入时一并计入，避免并发请求同时越过预算。

### Provider Circuit Breaker

熔断状态存放在既有 Redis namespace，按 `route_id` 隔离，不按业务输入或用户维度建键。每条 route 增加以下服务端配置：

- `circuit_failure_threshold`：连续可确认失败阈值；
- `circuit_open_seconds`：熔断开启时长。

默认配置为关闭熔断（阈值为 0），确保现有部署行为不被意外改变。网络错误、超时和 HTTP 5xx 计入连续失败；HTTP 429 仅写入现有 Retry-After 阻塞，不增加失败计数；成功响应清零失败计数；`outcome_unknown` 不清零，也不作为可确认失败计数。熔断开启时在 permit 前返回 `circuit_open` 与剩余重试时间。Redis 不可用时保持现有 fail-closed 语义。

### ModelGateway Integration

`ModelGateway.call()` 依次执行：

1. 验证 route 是否在 Run 冻结允许列表中，以及 request deadline；
2. 调用 `PolicyEngine`；
3. 检查/获取 ProviderTrafficController permit（其中检查 route 熔断）；
4. permit 后复核 lease、取消、隐私、授权、deadline 与 Worker 调用守卫；
5. 建立 usage、标记开始并紧贴 Provider HTTP 调用再次复核；
6. 按 Provider 结果结算 usage、permit、Retry-After 与熔断失败计数。

Policy 或 circuit 拒绝均不创建 `AgentModelUsage`、不占用 permit，也不发送 HTTP 请求。实际发送前任一二次校验失败统一返回 `aborted_before_send`，并释放已获得的 permit/usage。

### Worker Draining Boundary

Worker 将当前 draining 状态作为不可伪造的调用守卫注入 ModelGateway（或其 Memoir adapter）。一旦 Worker 开始 draining，尚未发起的新模型调用必须在 permit 前拒绝；已发送的 Provider 请求不强行中断，由既有 timeout 和 lease/fencing 逻辑收敛。宽限期耗尽时使用同一守卫，避免新增第二套状态机。

### Logging and Privacy

新增日志只允许 `run_id`、`step_id`、`route_id`、安全错误码、状态和非内容计数。禁止记录 request、response、prompt、完整 policy 快照、token、用户标识和日记/播放文档正文。

## Error Semantics

| Condition | Result status/code | HTTP/Provider side effect |
| --- | --- | --- |
| 模型调用次数到达上限 | `policy_denied` / `MODEL_CALL_LIMIT_EXCEEDED` | 无 usage、无 permit、无 HTTP |
| 保守成本到达上限 | `policy_denied` / `MODEL_COST_LIMIT_EXCEEDED` | 无 usage、无 permit、无 HTTP |
| Route 熔断仍开启 | `circuit_open` | 无 usage、无 permit、无 HTTP |
| Worker draining | `aborted_before_send` | 无 usage、无 permit、无 HTTP |
| permit 后租约/授权失效 | `aborted_before_send` | 释放 permit，若已建 usage 则结算为 aborted |
| Provider 429 | `rate_limited` | 写 Retry-After，不计入熔断失败 |
| Provider timeout/network/5xx | `outcome_unknown` | 保守 usage 结算；timeout/network/5xx 计入熔断 |

## Tests

- 调用次数、成本预算、未知结果保守成本均能拒绝后续请求，且 Provider 从未被调用。
- 并发/重复 usage 聚合按持久化数据计算，不以调用方参数为准。
- 连续 timeout 或 5xx 打开 route 级熔断；成功响应恢复失败计数；429 不污染熔断。
- 熔断剩余时间返回安全 retry-after，Redis 不可用时 fail closed。
- Worker draining 在 permit 前拒绝；draining 在 permit 获取后发生时，发送前二次校验仍拒绝并释放资源。
- 新日志、usage 摘要与 checkpoint/artifact 测试均不包含业务正文。

## Non-goals

- 不实现跨 route 的全局 Provider 健康平台。
- 不自动重试 Provider 请求；重试仍由冻结 workflow/既有运行语义决定。
- 不修改业务输入或允许业务方传入模型路由、预算、熔断参数。
