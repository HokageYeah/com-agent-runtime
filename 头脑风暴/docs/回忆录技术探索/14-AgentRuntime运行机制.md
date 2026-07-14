# 14-Agent Runtime 运行机制

## 一、目标

公共 Agent Runtime 的核心不是“调用一次大模型”，而是管理一个 Agent 任务从创建到产物落地的完整生命周期。

标准运行链路：

```text
Create Run
  -> Load AgentPackage
  -> Validate Input
  -> Build Context
  -> Plan
  -> Act
  -> Observe
  -> Evaluate
  -> Decide
  -> Persist Artifact
  -> Callback
```

这条链路适用于回忆录 Agent、客服 Agent、订单 Agent 等不同业务。

## 二、运行状态机

```text
pending
  -> planning
  -> running
  -> evaluating
  -> waiting_human
  -> succeeded
  -> partial
  -> failed
  -> cancelled
```

状态说明：

| 状态 | 说明 |
|---|---|
| `pending` | 已创建，等待执行 |
| `planning` | 正在生成或加载执行计划 |
| `running` | 正在执行节点、模型或工具 |
| `evaluating` | 正在评价当前步骤输出 |
| `waiting_human` | 等待人工确认或补充信息 |
| `succeeded` | 业务目标完成 |
| `partial` | 主业务产物已提交，但 Runtime 负责的可选步骤未完成；下游异步媒体失败不回写 AgentRun |
| `failed` | 不可恢复失败 |
| `cancelled` | 用户或系统取消 |

`planning/running/evaluating` 是持久状态，但不能作为 worker 所有权。所有权由独立 lease 控制：worker 原子取得 `lease_owner + lease_expires_at + fencing_token` 后才执行；heartbeat 续租失败后必须停止写入。这样单实例起步时也不会把“进程内 Map”误当成可恢复任务保证。

合法转换：

| 当前状态 | 允许的下一状态 | 触发者 |
|---|---|---|
| `pending` | `planning/failed/cancelled` | start/worker/system |
| `planning` | `running/failed/cancelled` | worker/system |
| `running` | `evaluating/waiting_human/failed/cancelled` | worker/policy/system |
| `evaluating` | `running/waiting_human/succeeded/partial/failed/cancelled` | evaluator/policy/system |
| `waiting_human` | `running/failed/cancelled` | approval API/timeout/system |
| `failed` | `pending` | 仅显式 retry，创建新 execution_attempt |
| `partial` | `pending` | 仅显式 retry 未完成可选步骤，创建新 execution_attempt |
| `succeeded/cancelled` | 无 | 重新执行业务目标必须创建新 run |

所有状态更新使用条件写入并递增 `status_version`。worker、callback dispatcher 和对账任务不能绕过该表直接覆盖终态；`dispatch_state` 与上述业务执行状态分开维护。

`dispatch_state` 属于 AgentRun，而不是 AgentDefinition：held run 为 `held`，start 事务提交后为 `queued`，worker 取得 lease 后为 `claimed`，run 结束后为 `finished`。lease 回收可将 `claimed -> queued`；显式 retry 通过条件写入把 `finished -> queued` 并递增 execution attempt。其他逆向转换一律拒绝。业务执行状态回答“任务做到哪一步”，dispatch 状态回答“任务由谁调度或持有”。

运行时间使用三类时钟：`max_run_seconds` 累计 `planning/running/evaluating` 的活跃执行时间；held、queued、`waiting_human` 使用独立 TTL；`run_deadline_at` 限制一次 run 的最终存续时间。系统不能用 `created_at + max_run_seconds` 判断执行超时，否则排队和人工等待会错误消耗模型执行预算。

## 三、标准运行步骤

### 3.1 Create Run

业务系统调用：

```text
POST /api/v1/agent-runs
```

Runtime 创建：

- `AgentRun`
- `IdempotencyRecord`
- `start_mode=auto` 时与 AgentRun 同事务写入 outbox；`start_mode=held` 时只保存 held run

Runtime 在通过 AgentPackage 和输入校验后再创建首个 Checkpoint；产生真实中间或最终结果后才创建 AgentArtifact，避免用空记录混淆“任务已创建”和“已经有产物”。

held 模式的业务握手：

```text
POST create(start_mode=held) -> run_id
  -> 业务系统事务绑定 active_run_id
  -> POST /agent-runs/{run_id}/start
  -> Runtime 条件更新 held -> queued，并在同事务写 outbox
```

`start` 必须幂等；已 queued/claimed 时返回当前状态，已取消或 held 超时则拒绝。worker 不扫描 held run。

创建 held run 时写入 `held_expires_at`；auto/start 入队时写入 `queued_at`，worker 认领时写入 `claimed_at`。这些字段用于定位业务握手、排队和执行阶段的耗时，不能从单个 `started_at` 反推。

如果 start 在状态事务前因 AdmissionController 过载返回 429，held 状态保持不变，Runtime 不完成该 IdempotencyRecord；业务补偿按 Retry-After 使用同一 key 重试。

创建任务必须是短请求。Runtime 不应要求业务后端保持长连接等待生成完成。

创建请求必须带 `Idempotency-Key`。Runtime 保存 request hash 和创建结果；相同 key 且 request hash 一致时返回原 `run_id`，request hash 不一致时返回 HTTP `409 Conflict` 和错误码 `IDEMPOTENCY_CONFLICT`。

### 3.2 Load AgentPackage

根据 `agent_id + agent_version` 加载：

- agent 元信息。
- 输入输出 schema。
- workflow 图。
- prompt。
- tool allowlist。
- guardrails。
- evals。
- callback 配置。

如果版本不存在，直接失败，不允许自动使用最新版。

Registry 状态也必须通过校验：`deprecated` package 不接受新 create，但已绑定 digest 的 run 可以继续；`revoked` package 禁止 start/retry/resume，已经 held/queued 的 run 取消，running worker 在下一个安全边界停止。Runtime 每次创建 execution attempt 都重新检查状态，安全撤销优先于 package digest 带来的可复现性。

### 3.3 Validate Input

校验：

- 输入 schema。
- 调用方身份。
- `caller_id/tenant_id` 是否有权使用指定 agent、business_type、callback target 和 business connector。
- business_type 是否允许调用该 Agent。
- callback_url 是否在 allowlist。
- cost / quota 是否允许创建新 run。
- AdmissionController 是否仍有 held/queued 配额；过载时返回 429 + Retry-After，不创建 run。

Runtime 从服务认证凭据推导 `caller_id/tenant_id`，不接受请求 body 自报身份作为授权依据。

校验成功后把当前 `authorization_version` 写入 AgentRun。该值用于检测运行期间的服务账号、connector、callback target 和数据域变更，不代表把权限永久冻结给 run。

### 3.4 Build Context

ContextBuilder 构建初始上下文：

```text
input
business metadata
agent rules
tool manifest
memory policy
runtime policy
```

注意：业务私密数据不应在这一步直接注入，必须通过工具按需读取。

ContextBuilder 将 AgentPackage/runtime policy 标为 trusted instructions，将业务输入、日记、RAG/Web Search 和工具返回标为 untrusted content。它们进入独立结构化数据槽；内容中的角色、工具调用和“忽略规则”等文字不改变 workflow、tool allowlist 或权限。

### 3.5 Plan

Planner 生成或加载 `AgentPlan`。

Workflow Agent：

```text
读取 AgentPackage 中的静态 workflow
  -> 转成 AgentPlan
```

Autonomous Agent：

```text
根据用户目标和工具清单生成计划
  -> PolicyEngine 校验
  -> 必要时人工确认
```

第一版只实现静态计划。

### 3.6 Act

Executor 执行下一步：

- deterministic node
- model node
- tool node
- guardrail node
- fallback node

每步开始和结束都写入 `AgentStep`。

执行前必须再次校验当前 fencing token、取消标记、package 状态、privacy version 和 authorization version。授权版本变化时按当前 caller/tenant/connector 重新授权；已撤销则终止后续模型和工具动作。模型调用尽量传播 cancellation；不可中止的副作用工具即使晚到，也只能凭稳定幂等键返回原结果，旧 fencing token 或失效 privacy version 不得推进 run 状态、保存私密 payload。

Runtime fencing token 只保护 Runtime 自己的库。业务副作用还必须携带业务系统签发的 generation/version token；例如 MemoirAgent 发布作品时携带 `generation_epoch`，由情侣日记后端拒绝删除或新一轮生成之前的旧 run。公共 Runtime 不解释该字段含义，只负责按 AgentPackage 映射并透传。

### 3.7 Observe

Runtime 收集步骤结果：

- 模型输出。
- 工具返回。
- 节点状态。
- token 和成本。
- 错误。
- 产物。

观察结果进入 `AgentCheckpoint` 和 `AgentArtifact`，并只保存脱敏摘要。

为了恢复执行，Checkpoint 可以暂存必要状态，但必须使用加密 payload、data classification、TTL 和删除联动；AgentArtifact 默认只保存摘要、hash 与业务写回引用。trace、日志和 callback 始终不能保存或传输完整回忆录正文。

Checkpoint、Artifact 和模型/工具临时结果写入时必须带读取上下文时取得的 `privacy_version`，数据库条件更新要求 run 仍为 `privacy_state=active`。业务服务请求 purge 后，即使旧 worker 的模型调用无法立即中止，迟到结果也只能丢弃并记录无内容审计，不能重新创建已清理的私密状态。

### 3.8 Evaluate

Evaluator 评价当前结果。

评价维度：

| 维度 | 说明 |
|---|---|
| schema | 输出是否符合结构 |
| semantics | ID、引用、数量、时长和工具参数是否来自 trusted state 与 allowlist |
| goal | 是否推进业务目标 |
| grounding | 是否有真实数据依据 |
| safety | 是否触发安全风险 |
| completeness | 是否完整 |
| cost | 是否超预算 |
| retryability | 是否值得重试 |

决策：

```text
pass
retry
fallback
human_review
fail
```

### 3.9 Decide

PolicyEngine 根据评价结果决定下一步：

| 决策 | 行为 |
|---|---|
| `pass` | 进入下一节点 |
| `retry` | 按 retry policy 重试当前节点 |
| `fallback` | 进入降级节点 |
| `human_review` | 状态变为 `waiting_human` |
| `fail` | 状态变为 `failed` |

进入 `waiting_human` 时必须写 `waiting_expires_at` 和恢复所需 checkpoint。审批接口使用幂等键和当前 `status_version` 做条件更新；超时策略由 AgentPackage 指定为 fallback、failed 或 cancelled，不能无限等待。

### 3.10 Persist Artifact

Runtime 保存通用产物：

- plan。
- step output。
- evaluation。
- final output。
- safety report。

业务产物通过工具写回业务系统。例如回忆录的 scenes/actions 写回情侣日记后端。

### 3.11 Callback

Runtime 回调业务系统：

```text
run_started
step_changed
partial_succeeded
run_succeeded
run_failed
run_cancelled
```

Runtime 在提交 run/step 状态变化的同一数据库事务中创建不可变 CallbackEvent，并写入 `RuntimeOutboxEvent(event_type=callback)`，再由独立 dispatcher 投递。不能先更新状态、再以非持久的进程内任务发送 callback，否则进程退出会永久丢失事件。

dispatcher 对 outbox 行使用独立 lease，遵守 `Retry-After` 并按指数退避。超过投递次数或保留窗口后把原事件标为 `dead_letter` 并告警；人工或对账重放继续使用原 `event_id/event_seq/status_version`，不分配新序号。业务系统依靠幂等消费和主动查询 Runtime 修复本地状态。

业务系统不依赖 callback 作为唯一状态来源，仍可查询 Runtime。

callback payload 必须包含：

```json
{
  "event_id": "evt_001",
  "event_seq": 12,
  "run_id": "run_abc",
  "business_id": "archive_123",
  "event": "step_changed",
  "status": "running",
  "status_version": 8,
  "current_step": "generate_scenes",
  "progress": 60
}
```

`event_seq` 在单个 `run_id` 的完整生命周期内全局单调递增。手动 retry、checkpoint resume 或自动恢复后，Runtime 必须从当前最大 `event_seq` 继续累加，不重置为 1。`status_version` 只在 Runtime 持久状态修订时递增。业务系统同时校验两者：事件序号负责去重和排序，状态版本负责阻止旧状态覆盖；两者不能与业务表自己的 `row_version` 混用。

## 四、通信模式

### 4.1 推荐模式

第一版采用：

```text
业务后端 -> Runtime
  POST /api/v1/agent-runs 创建任务
  GET /api/v1/agent-runs/{run_id} 查询状态

Runtime -> 业务后端
  HTTP callback 推送关键事件

前端 -> 业务后端
  普通 HTTP 查询详情
  小程序按 retry_after_ms 退避轮询；已验证流式能力的平台可选 SSE
```

### 4.2 后端到 Runtime 不使用长连接

业务后端调用 Runtime 的目标是创建可靠长任务，不是拿动画级实时输出。创建任务后立即返回 `run_id` 更稳：

- AgentRun 可脱离单次连接继续执行。
- callback 可重试。
- 业务后端可轮询兜底。
- 签名、幂等、审计更简单。
- 不会因为网关超时中断长任务。

### 4.3 SSE 用于事件订阅

SSE 用于事件订阅，不用于创建任务。

可选接口：

```text
GET /api/v1/agent-runs/{run_id}/events
```

该接口主要给业务后端订阅 Runtime 事件，或给内部调试后台使用。普通业务前端不直接连接 Runtime。

回忆录第一版不实现 Runtime 原生 SSE。小程序前端轮询业务后端本地 `memory_agent_run_refs/generation_status/public_trace`；业务后端 SSE 只作为 H5/已验证流式端的可选适配。Runtime 原生 `/events` 放二期。

### 4.4 WebSocket 放二期

WebSocket 适合：

- 客服 Agent 多轮实时对话。
- 用户中途打断 Agent。
- 人工介入。
- 多人协作。
- 高频双向交互。

回忆录第一版不需要 WebSocket。

## 五、Public Trace

Runtime 内部 trace 不等于前端可见轨迹。每个 AgentPackage 通过 `ui-trace.yaml` 决定哪些事件可以暴露给业务前端。

可见等级：

| 等级 | 说明 |
|---|---|
| `none` | 不展示轨迹 |
| `status_only` | 只展示生成中、成功、失败 |
| `public_summary` | 展示脱敏步骤文案 |
| `debug_staff` | 仅内部人员可看详细轨迹 |
| `full_internal` | 仅审计后台可看完整 trace |

业务系统面向前端返回的是 `public_trace`，不能返回 prompt、工具原始输入输出、模型原始输出和私密状态。

## 六、Plan / Act / Observe / Evaluate 模式

公共 Runtime 应显式采用：

```text
Plan
  决定要做什么

Act
  执行模型、工具或确定性节点

Observe
  收集执行结果

Evaluate
  判断结果质量和下一步
```

这比简单的 ReAct loop 更适合生产环境，因为每一步都有状态、评价和恢复点。

## 七、停止条件

必须有硬停止条件：

- 最大 step 数。
- 最大模型调用数。
- 最大工具调用数。
- 最大 token。
- 最大成本。
- 最大运行时间。
- 最大重试次数。
- side effect 工具最大调用次数。

Autonomous Agent 尤其必须限制循环，避免无限工具调用、重复写入和成本失控。

默认硬限制：

| 参数 | 默认值 |
|---|---:|
| `max_steps` | 16 |
| `max_model_calls` | 8 |
| `max_tool_calls` | 20 |
| `max_run_seconds` | 300 |
| `max_auto_retry_per_step` | 2 |
| `max_manual_run_retry_count` | 3 |
| `max_estimated_cost` | 2.0 |

`max_auto_retry_per_step` 只统计 Runtime 自动节点重试；`max_manual_run_retry_count` 只统计用户或后台触发的 run 级重试，两者互不消耗。

## 八、失败恢复

恢复策略：

```text
读取最新 checkpoint
  -> 找到失败 step
  -> 校验 package 未 revoked、privacy_state 仍为 active
  -> 创建新的 execution_attempt 并取得新 fencing token
  -> 检查工具幂等
  -> 根据 retry policy 继续
```

不同失败：

| 失败 | 策略 |
|---|---|
| 模型超时 | 重试或切 fallback 模型 |
| JSON 解析失败 | repair 后重试 |
| 工具超时 | 工具级重试 |
| 权限失败 | 立即失败 |
| `GENERATION_SUPERSEDED` | 旧业务世代，停止副作用并取消/结束旧 run，不重试 |
| `PACKAGE_REVOKED` | 安全撤销，取消 run，不允许 retry/resume |
| `PRIVATE_DATA_PURGED` | 私密状态已请求清理或已清理，丢弃迟到结果，不允许 retry/resume |
| `AUTHORIZATION_REVOKED` | caller/tenant/connector 或数据域权限已撤销，终止副作用，不自动切换身份 |
| `DISPATCH_FAILED` | run_dispatch 多次 dead letter 且对账重放失败，条件写为 failed 并通知业务系统 |
| 安全失败 | fallback 或 human_review |
| 超预算 | 停止增强节点 |

每次恢复都在 AgentStep、AgentToolCall 和 AgentModelUsage 写入新的 `execution_attempt`，节点或工具自身重试再递增 `step_attempt/tool_attempt`。副作用工具的 `logical_operation_key` 和业务幂等键保持不变，attempt 字段只服务审计、成本和故障分析。

## 九、跨库一致性与补偿

Runtime 库和业务库不是同一事务边界，第一版不做分布式事务。

要求：

- Runtime 对 auto create 或显式 start，在同一本地事务中更新 dispatch_state 并写 outbox/queue intent；dispatcher 投递失败时 run 保持 `pending`，由 outbox 重试和对账任务恢复。
- `run_dispatch` 进入 dead letter 后，对账先重放同一 event；超过恢复上限时把仍未 claimed 的 run 标记 `failed(DISPATCH_FAILED)` 并生成 callback，不能永久保留 queued/pending。
- 业务后端先保存 `MemoryAgentRunRef(status=pending_start)` 并创建 held run；create 失败重试 create，绑定后 start 失败只重试 start，不重复创建 run。
- side effect 工具调用前先记录 `AgentToolCall` 和幂等键，业务系统按幂等键返回首次写入结果。
- callback 失败时 Runtime 保存 `callback_events` 并按退避策略重试。
- callback 达到重试上限后进入 dead letter 并告警；重放复用原事件身份，业务后端主动查询作为恢复兜底。
- 业务后端可以按 `run_id` 查询 Runtime，修复本地状态。
- 对账任务默认每 5 分钟扫描 pending 未入队、lease/heartbeat 失效、活跃执行预算耗尽、人工等待超时、callback 多次失败、tool call running 超时；同一对象连续 3 次修复失败后升级告警。

worker 调度还必须满足：

- 消费者对 run 做条件更新实现原子认领；Redis 锁只能作为优化，数据库 fencing token 才是最终写保护。
- heartbeat 超时后由 reaper 回收，生成新的 execution attempt；旧 worker 的迟到结果被 fencing 拒绝。
- cancel/retry 互斥并受状态转换表约束；终态 run 不接受普通状态写入，显式 retry 创建新的 attempt 并保留审计。

## 十、第一版必须实现

- `AgentRun` 生命周期。
- `AgentPlan` 静态计划。
- `AgentStep` 执行记录。
- `AgentEvaluation` 轻量评价。
- `AgentCheckpoint` 节点级恢复。
- `PolicyEngine` 硬限制。
- callback，包含 HMAC-SHA256 签名和 event_seq/status_version 乱序保护。
- public trace 策略。
- 后端创建任务 HTTP、状态 callback、小程序轮询基线和可选业务 SSE 的通信边界。
- retry / fallback。
- IdempotencyRecord。
- 对账和补偿任务。
- Runtime Contract 版本校验与兼容性测试。
- outbox、worker lease/heartbeat、fencing token、execution attempt 和取消传播。
- liveness/readiness、鉴权能力发现、共享持久化和 worker draining。
- 活跃执行、held/queued、人工等待和最终 deadline 的独立时钟。
- Step/ToolCall/ModelUsage 的 execution attempt 审计维度。
- package active/deprecated/revoked 生命周期和安全撤销传播。
- privacy purge tombstone/version 与所有私密 payload 的条件写屏障。
- dispatch/callback outbox 的 lease、dead letter 和原事件重放。
- trusted instructions/untrusted content 隔离、确定性语义校验和 injection 反例测试。
- authorization version、每次模型/工具/callback 前复核和撤销传播。

## 十一、二期再做

- 动态计划生成。
- 计划修订。
- LLM-as-judge。
- 多模型评价。
- 人工审核界面。
- Runtime 原生 `/events` SSE 事件流。
- WebSocket 双向交互。
- 子图和多 Agent 协作。
- A2A / handoff。
