# 09-公共 Agent Runtime 架构

## 一、定位

公共 Agent Runtime 是所有业务 Agent 的基础设施。它不属于回忆录，也不属于客服；它负责“Agent 如何运行”，业务 Agent 负责“要完成什么业务目标”。

```text
公共 Agent Runtime
  -> 运行 MemoirAgent
  -> 运行 CustomerSupportAgent
  -> 运行 StudyAgent
  -> 运行 OperationAgent
```

## 二、技术组合

```text
LangGraph Python
  主编排器：workflow、checkpoint、resume、interrupt

LangChain Python
  组件库：prompt、tool、parser、retriever、middleware、createAgent

LiteLLM / Provider Adapter
  模型通道：provider 抽象、generate、structured output、fallback、成本统计

MCP SDK
  标准协议：外部 tools、resources、prompts

OpenTelemetry / LangSmith
  观测评测：trace、debug、eval、成本
```

选择原则：

- 和 OpenMAIC 保持 Stage / Scene / Action、provider 抽象、工具协议、降级链路等架构思想一致。
- 当前若 AgentRuntime 新启独立 Python 工程，以 Python 等价栈落地；TypeScript / AI SDK 经验作为设计参考，不要求同栈复刻。
- 不自研完整 Agent loop。
- 不把模型 provider 体系拆成两套。
- LangChain 用作组件生态，LangGraph 用作状态编排。

## 三、核心模块

### 3.0 Contract Layer

公共 Runtime 的最底层是无业务依赖的版本化契约层，建议维护 Pydantic 模型并导出 JSON Schema，覆盖：

- AgentRun 创建、start、查询、重试、取消请求与响应。
- RuntimeEvent / CallbackEvent。
- UnifiedToolDefinition、ToolRequest、ToolResult、ToolError。
- AgentArtifact envelope，而不是具体业务 payload。
- 标准错误码、状态枚举和 `schema_version`。

契约层不得依赖 LangGraph、LangChain、数据库 ORM 或情侣日记模型。每次变更必须做向后兼容测试；破坏性变更提升 major version。

### 3.1 API Layer

对业务系统提供：

```text
GET  /health/live
GET  /health/ready
GET  /api/v1/runtime-capabilities
POST /api/v1/agent-runs
POST /api/v1/agent-runs/{run_id}/start
GET /api/v1/agent-runs/{run_id}
GET /api/v1/agent-runs/{run_id}/steps
POST /api/v1/agent-runs/{run_id}/retry
POST /api/v1/agent-runs/{run_id}/cancel
POST /api/v1/agent-runs/{run_id}/human-approval
POST /api/v1/agent-runs/{run_id}/purge-private-data  # 仅受信任业务服务
```

第一版可暂不开放完整后台，但 API 结构要提前稳定。

`/health/live` 只表示进程和事件循环存活，不探测模型供应商；`/health/ready` 检查数据库 schema、Agent Registry、outbox/队列连接和签名配置，核心依赖不可用时返回 503。`/api/v1/runtime-capabilities` 需要服务身份认证，返回 Runtime/Contract 版本、已注册 Agent 版本、逻辑模型 policy、工具传输和可选媒体能力，不返回密钥、真实 provider base URL、connector 地址或租户配额。可选 provider 未配置只改变 capabilities，不应让 liveness 失败。

API Layer 必须认证业务调用方。第一版可选 mTLS 或服务账号签名/JWT，但必须得到稳定 `caller_id`，并校验该调用方可用的 agent、business_type、callback target、connector、配额和数据域。普通用户 token 不能直接创建 Runtime run。

授权记录必须有单调递增的 `authorization_version`。Runtime 创建 run 时保存版本；开始新 execution attempt、调用模型、调用工具和发送 callback 前重新确认 caller/tenant、connector、callback target 与数据域仍有效。版本变化但授权仍有效时，Runtime 以条件更新保存新版本并记录审计；权限撤销时终止动作。旧 run 不得依赖创建时快照继续产生副作用，也不能自动切换到另一个 connector。

需要先把 `run_id` 绑定到业务对象的系统使用 `start_mode=held` 创建 pending run。业务系统保存 `active_run_id` 后再幂等调用 `/start`；Runtime 只有 start 事务写入 outbox 后才允许 worker 认领。held run 设置过期时间，由对账任务取消长期未启动记录。

### 3.2 Agent Registry

记录：

- `agent_id`
- `agent_version`
- `runtime_type`
- `input_schema`
- `output_schema`
- `workflow_id`
- `tool_allowlist`
- `model_policy`
- `guardrail_policy`
- `package_digest`
- `contract_version`

第一版可以用配置文件或数据库注册，不需要管理后台。

`AgentDefinition.status` 固定为 `active/deprecated/revoked`：

- `active`：允许 create/start/retry/resume。
- `deprecated`：拒绝新 create；已经绑定该 digest 的 held/queued/run 可按原契约执行和恢复，用于正常版本迁移。
- `revoked`：用于安全撤销。拒绝 create/start/retry/resume，held/queued run 转 cancelled，running run 在下一个模型、工具或 checkpoint 边界取消。worker 每次 Load AgentPackage 和开始新 execution attempt 时都重新检查状态，不能因为 run 已固定 digest 就绕过撤销。

状态变化必须记录操作者、原因和时间。撤销 package 不自动删除业务或 Runtime 私密数据；需要删除时仍走独立的 privacy purge 流程。

### 3.3 Runtime Executor

支持三类运行模式：

| 模式 | 技术 | 用途 |
|---|---|---|
| Workflow | LangGraph StateGraph | 回忆录、报告、审核 |
| Autonomous | LangChain createAgent / Runtime ToolLoopAgent | 客服、数据问答 |
| Hybrid | LangGraph 外层 + Agent 子节点 | 复杂工单、多步骤助手 |

第一版只必须支持 Workflow。

### 3.4 Planner

Planner 负责把业务目标转换成可执行计划。它不是每次都必须调用模型：Workflow Agent 可加载静态计划，Autonomous Agent 可动态生成计划，Hybrid Agent 可先静态分阶段，再在局部节点里动态规划。

计划输出建议记录为 `AgentPlan`：

```text
plan_id
run_id
strategy: static | dynamic | hybrid
steps
dependencies
stop_conditions
fallback_policy
created_at
```

第一版要求：

- MemoirAgent 使用静态计划。
- Runtime 仍然保存 `AgentPlan`，便于后续客服、订单等 Agent 复用同一套运行机制。
- 计划必须包含最大 step、最大模型调用次数、最大工具调用次数和失败策略。

二期再做：

- 动态计划生成。
- 计划修订。
- 多候选计划评估。
- 计划级人工确认。

### 3.5 Evaluator / Critic

Evaluator 负责评价每个关键步骤的输出是否可以进入下一步。

评价维度：

- schema 是否通过。
- 是否满足业务目标。
- 是否引用真实素材。
- 是否触发安全风险。
- 是否需要重试、降级或人工介入。

第一版要求：

- 每个 LLM 节点后执行轻量评价。
- MemoirAgent 至少评价高光、章节、场景、安全复核结果。
- 评价结果保存为 `AgentEvaluation`。

二期再做：

- LLM-as-judge。
- 多模型交叉评价。
- 在线抽样评价。
- 自动回归评测。

### 3.6 PolicyEngine

PolicyEngine 统一执行运行策略：

- step limit。
- token limit。
- cost budget。
- tool allowlist。
- retry policy。
- timeout。
- side effect 工具审批。
- human-in-the-loop 规则。

第一版必须支持硬限制，防止 Agent 无限循环、成本失控和重复副作用。

第一版默认值：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `max_steps` | 16 | 单个 run 最大步骤数 |
| `max_model_calls` | 8 | 单个 run 最大模型调用数 |
| `max_tool_calls` | 20 | 单个 run 最大工具调用数 |
| `max_run_seconds` | 300 | planning/running/evaluating 的活跃执行预算 |
| `max_auto_retry_per_step` | 2 | Runtime 自动执行时单步最大重试次数 |
| `max_manual_run_retry_count` | 3 | 用户或后台手动重试 run 的上限 |
| `max_estimated_cost` | 2.0 | 预估成本上限 |

### 3.7 AgentPackage

AgentPackage 是业务 Agent 接入 Runtime 的交付单元。

建议结构：

```text
agent.yaml
input.schema.json
output.schema.json
workflow.graph.py
prompts/
tools.manifest.json
guardrails.yaml
evals/
callbacks.yaml
ui-trace.yaml
```

Runtime 通过 AgentPackage 加载 Agent，而不是让业务代码散落在 Runtime 内部。

同一 `agent_id + version` 必须不可变，并绑定内容摘要 `package_digest`。已产生 run 的 package 不能原地覆盖；修订必须发布新版本。

`workflow.graph.py` 属于受信任的部署时代码，不是用户上传脚本。第一版只允许 CI/CD 构建并由 Runtime 管理员注册的签名 package；普通业务调用方不能上传、覆盖或指定任意 Python 文件。若未来开放第三方 package，必须先引入代码签名、隔离执行和供应链扫描，不能依赖当前进程内加载方式。

`ui-trace.yaml` 决定前端是否能展示 Agent 执行轨迹，以及只能展示哪一层脱敏摘要。

### 3.8 ModelGateway

职责：

- provider 路由。
- 结构化输出。
- JSON 修复。
- token 和成本记录。
- 失败重试。
- fallback 模型。
- thinking / reasoning 参数适配。

第一版默认使用 LiteLLM / Provider Adapter 承接 provider 路由。LangChain model adapter 作为补充，不作为主通道。

### 3.9 PromptRegistry

职责：

- prompt id。
- prompt version。
- prompt variables。
- input schema。
- output schema。
- owner agent。
- 灰度和回滚。

第一版可以用文件系统 + manifest，二期再做后台管理。

### 3.10 ToolGateway

ToolGateway 执行三类传输：

- Native Tool：Runtime 内部工具。
- HTTP Business Tool：业务系统工具。
- MCP Tool：外部标准工具。

LangChain Tool 与 AI SDK Tool 只是上述工具的框架适配器，不是新的传输类型。适配器不得绕过 ToolGateway 的 allowlist、签名、预算和审计。

工具调用必须有权限、超时、重试、幂等和审计。ToolGateway 每次调用都按 AgentRun 的 caller/tenant 和当前 `authorization_version` 重新授权；业务后端仍独立校验 Runtime 服务身份、run、业务对象和工具权限，形成双层边界。

### 3.11 ContextManager

职责：

- token 预算。
- 上下文拼装。
- 素材分块。
- 摘要压缩。
- 敏感字段脱敏。
- 工具结果压缩。
- 上下文快照。

ContextManager 必须区分 trusted instructions 与 untrusted content。日记正文、赌局标题、图片说明、RAG 文档、Web Search 和工具返回默认标记为 `trusted=false`，只能进入模板的数据槽，不能拼接进 system/developer 指令或工具描述。检测到“忽略规则、调用工具、泄露提示”等文本时可以标记或隔离，但 prompt injection 分类器不能作为唯一防线。

第一版必须支持任务内上下文，二期再扩展长期记忆检索。

### 3.12 MemoryManager

记忆分层：

| 类型 | 说明 | 第一版 |
|---|---|---|
| Run Memory | 当前 run 的步骤状态和中间结果 | 必做 |
| Session Memory | 会话级短期记忆 | 可选 |
| Long-term Memory | 跨任务长期记忆 | 二期或三期 |

回忆录素材默认不进入长期记忆。

### 3.13 Guardrails

公共护栏：

- 输入 schema 校验。
- 输出 schema 校验。
- 工具权限。
- 高风险工具拦截。
- 敏感字段检测。
- token / cost / step 限制。
- 不可信内容与指令隔离。
- 模型引用、ID、数值和工具参数的确定性语义校验。

业务护栏由 Agent 定义补充，例如回忆录的情绪安全规则。

### 3.14 Observability

记录：

- run
- step
- model call
- tool call
- prompt version
- token
- cost
- error
- safety result

第一版至少落库，二期接 OpenTelemetry / LangSmith 深度分析。

### 3.15 AdmissionController / Backpressure

单 run 的 step/token/cost 限制不能替代系统级背压。第一版至少配置：

- 全局、`caller_id/tenant_id`、agent 的 queued/held/running 上限。
- 每个 provider/model 的并发与速率预算。
- held run 数量和最长 held 时间，防止业务创建后不 start 占满数据库。
- 队列已满时返回 `429 RUNTIME_OVERLOADED + Retry-After`，不先创建一个永远等待的 run。
- worker 原子认领时再次校验执行槽位；进程数增加不能绕过数据库或集中限流器中的配额。

第一版可用简单 FIFO + 分调用方并发上限。优先级、加权公平队列和跨区域调度放二期，但队列不能无界增长。回忆录在 Runtime 过载时继续展示业务 baseline，由补偿任务按 Retry-After 延迟创建或 start。

### 3.16 Runtime Operations / Deployment

第一版把 API、dispatcher/reconciler 和 workflow worker 视为可独立扩缩的进程角色，但它们共享同一个权威运行数据库：

```text
短请求 API
  -> PostgreSQL/MySQL：run、checkpoint、outbox、lease、幂等记录
  -> Redis/消息队列：可选通知与调度加速

后台 worker
  -> 原子认领 run
  -> 执行 LangGraph
  -> checkpoint / callback outbox
```

Runtime 不得把 run、队列、checkpoint 或 AgentArtifact 权威 payload 只放在本地文件、进程内存或单实例临时磁盘。Serverless 可以承载无状态短 API，但不能依赖一次函数请求持续执行完整 AgentRun；生产 worker 必须运行在支持后台长任务、优雅停机和共享持久化的计算环境。

实例进入 draining 后停止认领新 run，继续为当前 lease heartbeat，并在安全 checkpoint 后释放或完成；超过停机宽限期时主动释放 lease，让 reaper 生成新 execution attempt。readiness 在 draining 期间返回 503，liveness 保持成功，供负载均衡先摘流再终止进程。

## 四、核心数据模型

### AgentDefinition

```text
id
agent_id
version
runtime_type
definition_json
status
revoked_at
revocation_reason
created_at
updated_at
```

### AgentRun

```text
id
run_id
agent_id
agent_version
package_digest
contract_version
caller_id
tenant_id
business_type
business_id
status
dispatch_state: held | queued | claimed | finished
input_json（只保存业务定位参数和安全元数据）
capability_snapshot_json（只保存逻辑 policy/能力和版本，不含密钥或 endpoint）
authorization_version
output_summary_json
error_code
error_message
manual_retry_count
auto_retry_count
status_version
last_event_seq
execution_attempt
lease_owner
lease_expires_at
fencing_token
cancel_requested_at
privacy_state: active | purge_requested | purged
privacy_version
privacy_purge_requested_at
private_data_purged_at
held_expires_at
queued_at
claimed_at
active_elapsed_ms
run_deadline_at
waiting_expires_at
started_at
finished_at
created_at
updated_at
```

Runtime 状态集合固定为 `pending/planning/running/evaluating/waiting_human/succeeded/partial/failed/cancelled`。`status_version` 在持久状态发生变化时递增；`last_event_seq` 表示 Runtime 已生成的最大事件序号。callback 按单个 run 单调递增 `event_seq`，业务系统分别保存最近事件序号和最近 Runtime 状态版本。

`execution_attempt + fencing_token` 用于阻止失联 worker 恢复后写入旧结果。worker 必须原子认领 run、周期续租；续租失败即停止模型/工具/落库动作。取消采用协作式传播：先写 `cancel_requested_at`，再中止可中止调用，并在安全边界落为 `cancelled`。

`max_run_seconds` 只累计 worker 实际占用的 `planning/running/evaluating` 活跃执行时间。held、queued 和 `waiting_human` 分别受 `held_expires_at`、队列超时和 `waiting_expires_at` 控制；`run_deadline_at` 是跨状态的最终保留/业务截止时间。排队拥塞不能消耗模型执行预算，人工等待也不能让 run 无限保留。

`purge-private-data` 是写屏障，不是一次普通 delete。Runtime 在同一事务中设置 `cancel_requested_at`、将 `privacy_state` 改为 `purge_requested` 并递增 `privacy_version`；事务提交后接口返回 202 和当前 privacy version。Checkpoint、Artifact 私密 payload 和模型/工具临时结果只能在 `privacy_state=active AND privacy_version=expected` 时写入。清理任务删除既有私密 payload 后标记 `purged`，调用方通过 AgentRun 查询确认完成。purge 后该 run 禁止 retry/resume，只允许写入不含内容的状态、成本和安全审计，避免迟到模型结果重新生成已删除内容。

### AgentPlan

```text
id
plan_id
run_id
strategy
steps_json
stop_conditions_json
fallback_policy_json
status
created_at
updated_at
```

### AgentStep

```text
id
run_id
step_name
step_type
execution_attempt
step_attempt
status
input_summary
output_summary
error_code
error_message
started_at
finished_at
```

### AgentToolCall

```text
id
run_id
step_id
tool_name
tool_version
execution_attempt
tool_attempt
logical_operation_key
input_summary
output_summary
status
duration_ms
error_code
created_at
```

### AgentEvaluation

```text
id
run_id
step_id
target_type
target_id
evaluator_type
score_json
decision
reason_summary
created_at
```

### AgentCheckpoint

```text
id
run_id
checkpoint_key
state_schema_version
data_classification
privacy_version
encrypted_state_blob 或 storage_ref
content_digest
expires_at
created_at
```

Checkpoint 为恢复执行可以包含脱敏后的任务状态，但必须加密、设置 TTL 并按业务删除事件清理。日志、trace 和管理列表只显示摘要，不能展开回忆录正文。生产环境不得用明文 `state_json` 充当长期存档。

### AgentArtifact

```text
id
run_id
artifact_type
artifact_schema_version
data_classification
privacy_version
summary_json
content_digest
payload_ref
retention_until
created_at
```

`AgentArtifact` 是通用 envelope。回忆录默认只保存摘要、hash 和业务侧资源引用；需要暂存 payload 时必须使用加密存储、最短保留期和删除联动。Runtime 不把 AgentArtifact 当作业务数据的权威副本。

### AgentModelUsage

```text
id
run_id
step_id
execution_attempt
model_policy
route_config_version
provider
model
thinking_summary
prompt_tokens
completion_tokens
total_tokens
estimated_cost
created_at
```

### RuntimeOutboxEvent

```text
id
event_id
event_type: run_dispatch | callback
run_id
event_seq（callback 必填）
status_version（callback 必填）
target_id
payload_json 或 payload_ref（只允许安全事件摘要）
delivery_state: pending | delivering | delivered | dead_letter
attempt_count
next_attempt_at
lease_owner
lease_expires_at
last_error_code
created_at
delivered_at
retention_until
```

`CallbackEvent` 是不可变的契约 payload，`RuntimeOutboxEvent` 保存它的投递状态；`event_type=callback` 时 payload/ref 指向对应 CallbackEvent。run 状态变化、callback 序号分配和对应 outbox 行必须在同一事务提交。dispatcher 使用 lease 原子认领，遵守对端 `Retry-After` 并按指数退避重试；达到次数或时间窗口后进入 `dead_letter`、告警并交给对账任务。人工重放复用原 `event_id`；callback 还必须复用原 `event_seq/status_version`。

dead letter 按事件类型处理：callback dead letter 不改变 run 终态，业务系统通过主动查询修复本地摘要；`run_dispatch` dead letter 不能让 run 永久停在 pending/queued，对账先重放同一 dispatch event，超过运维策略上限后以条件更新把 run 标为 `failed(DISPATCH_FAILED)`，并创建对应 callback 事件。worker 对重复 dispatch 仍通过原子认领保证单写者。

### IdempotencyRecord

```text
client_id
idempotency_key
scope
request_hash
response_json
resource_type
resource_id
expires_at
created_at
updated_at
```

规则：

- 创建、start、重试、取消和 privacy purge 的幂等记录默认保留 7 天；purge 记录至少保留到私密 payload 清理完成。
- HTTP Business Tool 和 callback 的幂等记录建议保留 30 天。
- 相同 `client_id + scope + idempotency_key` 且 request hash 一致时返回原结果。
- request hash 不一致时返回 HTTP `409 Conflict`，错误码 `IDEMPOTENCY_CONFLICT`，不创建新 run，也不重复执行 side effect。
- Runtime/业务系统只在操作已被事务接受或已经产生副作用时固化可重放响应。schema/权限失败、执行前 `429 RUNTIME_OVERLOADED`、连接失败和可重试 5xx 不写 completed IdempotencyRecord；调用方可以使用同一 key 重试。实现可另记 attempt audit，但不能把瞬时拒绝缓存成 7/30 天固定结果。

## 五、第一版必须做

| 能力 | 要求 |
|---|---|
| Workflow Executor | 支持 LangGraph 固定流程 |
| Agent Registry | 支持配置 `memoir_agent` |
| Package Lifecycle | active/deprecated/revoked、审计和安全撤销传播 |
| AgentPackage | 支持以包形式注册业务 Agent |
| AgentRun 生命周期 | pending/planning/running/evaluating/waiting_human/succeeded/partial/failed/cancelled |
| AgentPlan | 支持静态计划落库 |
| Step 记录 | 每个节点落库 |
| ToolCall 记录 | 每次业务工具调用落库 |
| Evaluation 记录 | 每个关键 LLM 节点评价结果落库 |
| Checkpoint | 至少支持节点级恢复 |
| PolicyEngine | 支持 step/token/cost/tool 硬限制 |
| ToolGateway | 支持 HTTP Business Tool |
| ModelGateway | 支持 LiteLLM / Provider Adapter 模型调用；AI SDK 仅作为跨栈适配扩展 |
| Runtime Contract | 版本化 API/Event/Tool/Artifact schema 与兼容性测试 |
| Worker Lease | 原子认领、heartbeat、fencing、超时回收与取消传播 |
| Admission/Backpressure | queued/held/running 与 provider 并发上限、429/Retry-After |
| Runtime Operations | liveness/readiness、鉴权能力发现、共享持久化、draining 与优雅停机 |
| Privacy Purge | purge tombstone/version、条件写屏障、清理后禁止 retry/resume |
| PromptRegistry | 支持版本化 prompt |
| Guardrails | 支持 schema、权限、情绪安全 |
| Untrusted Content | 数据/指令隔离、引用与工具参数语义校验、prompt injection 测试 |
| Observability | 支持日志、token、成本 |
| Callback | 支持 HMAC-SHA256 签名、event_seq/status_version 乱序保护 |
| Idempotency | 支持 request hash、TTL、冲突响应 |
| Reconciliation | 支持 pending 未入队、callback 失败、运行超时的对账恢复 |
| Outbox Delivery | dispatch/callback 持久事件、lease、退避、dead letter 和原事件重放 |
| Runtime Authorization | 授权版本、运行中复核和撤销传播；业务工具继续做二次权限校验 |

## 六、二期再做

- Agent 管理后台。
- MCP Server / Client 完整注册和发现。
- LangChain createAgent 动态 Agent。
- 动态规划和计划修订。
- LLM-as-judge 和多模型评价。
- RAG 检索和向量库。
- 长期记忆。
- 人工审核界面。
- Prompt 在线灰度。
- LangSmith 深度评测。
- 多 Agent handoff / A2A。
- 代码沙箱。
- 自助租户管理、租户管理后台和商业计费；首期已有的 `tenant_id` 数据隔离、授权与限流不能延后。

## 七、边界提醒

公共 Runtime 不保存情侣日记原始业务数据，不绕过业务后端权限，不直接写业务表。它保存的是 Agent 执行过程、状态和通用产物；业务结果必须通过业务工具写回业务系统。
