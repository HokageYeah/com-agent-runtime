# AgentRuntime 总控 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统筹公共 AgentRuntime、`MemoirAgent`、情侣日记 FastAPI 后端工具 API、uni-app 回忆录播放器的开发顺序，按“契约冻结 -> 可靠运行底座 -> 原子业务发布 -> 前端 baseline/轮询 -> 故障与隐私验收”的路径完成第一版闭环。

**Architecture:** 总控计划负责执行顺序、跨模块契约、联调边界和验收标准；详细 Runtime 实现进入 [AgentRuntime 后端开发计划](../backend/2026-07-07-AgentRuntime-后端开发计划.md)。Runtime 使用权威数据库、持久 outbox、lease/fencing 执行异步任务；情侣日记后端以 baseline revision 0、`active_run_id + generation_epoch` 和原子发布工具维护业务作品权威状态；前端只调情侣日记后端。

**Tech Stack:** FastAPI + LangGraph Python + LangChain Python + LiteLLM / Provider Adapter + SQLAlchemy / Alembic + PostgreSQL（推荐）或同等锁语义 MySQL + Redis/Arq（可选通知）+ uni-app / Vue 3 / TypeScript。

## Global Constraints

- 当前 com-agent-runtime 根工程是唯一的 Runtime 工程；禁止创建嵌套 `services/agent-runtime`、第二份 `pyproject.toml`、Alembic 或同名 `app` 包。
- 当前工作区 15 份“回忆录技术探索”是权威基线。
- Runtime Contract、AgentPackage、Tool、Callback、Artifact 都必须版本化。
- 作品只能通过 `memory.publish_playback_document` 原子发布；播放器只读 `published_revision`。
- 调度采用 at-least-once + 幂等副作用，数据库 lease/fencing 保证单写者。
- Runtime 不保存第二套可播放回忆录正文；Checkpoint 私密状态必须加密、短 TTL、可 purge。
- 第一版小程序使用 HTTP 退避轮询；SSE 仅作可选适配。
- 第一版默认关闭 TTS/封面/视频，媒体 worker 失败不回写已结束 AgentRun。

---

## 1. 子计划入口

- 需求设计文档：[../需求设计文档.md](../需求设计文档.md)
- Runtime 后端详细计划：[../backend/2026-07-07-AgentRuntime-后端开发计划.md](../backend/2026-07-07-AgentRuntime-后端开发计划.md)
- 回忆录技术探索总入口：[../../回忆录技术探索/00-README.md](../../回忆录技术探索/00-README.md)
- 情侣日记后端集成参考：[../../回忆录技术探索/06-后端接口与AgentRuntime集成.md](../../回忆录技术探索/06-后端接口与AgentRuntime集成.md)
- 业务 Agent 接入规范：[../../回忆录技术探索/15-业务Agent接入规范.md](../../回忆录技术探索/15-业务Agent接入规范.md)

## 2. 核心原则

- AgentRuntime 是当前 com-agent-runtime 根工程内的公共运行时模块；它与情侣日记后端复用工程基础设施，但保持独立领域边界。
- `MemoirAgent` 是第一版验证公共 Runtime 的首个业务 Agent。
- Runtime 只负责执行过程和通用产物；回忆录业务表、权限、密码、删除、播放器数据归情侣日记后端。
- Runtime 不直连业务数据库，所有业务数据通过 HTTP Business Tool 读取或写回。
- 前端不直连 Runtime，只通过情侣日记后端查询回忆录详情、生成状态和安全 `public_trace`。
- 创建 AgentRun 使用 HTTP 短请求；需要业务绑定的 run 采用 held create，业务保存 `active_run_id` 后幂等 start。
- auto create/start/retry 通过事务 outbox产生 dispatch intent；Worker 使用数据库 lease/fencing 异步执行，Redis/Arq 只作通知。
- 小程序通过业务后端状态接口退避轮询；H5/已验证平台可选业务 SSE，Runtime 原生 SSE 放二期。
- archive 创建时发布 baseline revision 0；Agent 只能原子发布完整作品 revision，失败不影响当前播放器版本。
- `generation_epoch + active_run_id` 保护业务写回，execution attempt + fencing token 保护 Runtime 写入，两层不能互相替代。
- package digest 不可变，Registry 支持 active/deprecated/revoked；privacy purge 与 cancel 分离。
- 日记、赌局、RAG/Web Search 和工具结果都是 untrusted content，模型结果必须通过确定性语义校验。
- 契约先冻结，再分别开发 Runtime、业务后端和前端，避免字段、状态、工具名称漂移。
- 第一版必须保留完整 Runtime 骨架，但不做平台化后台、WebSocket、完整 MCP、RAG、长期记忆和多 Agent 协作。
- 所有日志、trace、callback、public trace 禁止包含日记原文、完整 prompt、模型原始输出、工具原始输入输出、openid、token、手机号、地址。

## 3. 契约冻结

### 3.1 Runtime API

| 接口 | 方法 | 责任 | 第一版 |
|---|---|---|---|
| `/health/live` | GET | 进程与事件循环存活 | 必做 |
| `/health/ready` | GET | DB schema、Registry、outbox/queue、签名配置和 draining | 必做 |
| `/api/v1/runtime-capabilities` | GET | 鉴权能力发现，不返回密钥与真实 endpoint | 必做 |
| `/api/v1/agent-runs` | POST | 创建 AgentRun | 必做 |
| `/api/v1/agent-runs/{run_id}/start` | POST | 幂等执行 held -> queued | 必做 |
| `/api/v1/agent-runs/{run_id}` | GET | 查询当前运行、调度、事件版本和 privacy 摘要 | 必做 |
| `/api/v1/agent-runs/{run_id}/steps` | GET | 查询步骤摘要 | 必做 |
| `/api/v1/agent-runs/{run_id}/retry` | POST | 从 checkpoint 重试 | 必做 |
| `/api/v1/agent-runs/{run_id}/cancel` | POST | 取消 Run | 必做 |
| `/api/v1/agent-runs/{run_id}/human-approval` | POST | 最小 approve/reject 状态迁移 | 第一版无复杂审核台 |
| `/api/v1/agent-runs/{run_id}/purge-private-data` | POST | privacy tombstone/version 与异步清理 | 必做 |

访问规则：`runtime-capabilities`、AgentRun 查询和 steps 查询必须校验服务身份与签名，但不要求 `Idempotency-Key`；create/start/retry/cancel/human-approval/purge 除验签外必须校验独立幂等键。`/health/live` 与 `/health/ready` 由部署探针访问并通过网络边界保护。

### 3.2 AgentRun 状态

```text
pending
planning
running
evaluating
waiting_human
succeeded
partial
failed
cancelled
```

情侣日记后端映射到回忆录生成状态：

| Runtime 状态 | 回忆录业务状态 |
|---|---|
| `pending` | `pending` |
| `planning/running/evaluating` | `running` |
| `waiting_human` | `waiting_human` |
| `succeeded` | `succeeded` |
| `partial` | `partial` |
| `failed` | `failed` |
| `cancelled` | `cancelled` |

### 3.3 创建 AgentRun 契约

情侣日记后端调用：

```text
POST {AGENT_RUNTIME_URL}/api/v1/agent-runs
```

请求头：

```text
X-Agent-Client-Id
X-Agent-Key-Id
X-Agent-Timestamp
X-Agent-Signature
Idempotency-Key
```

要求：

- `X-Agent-Client-Id` 是 Runtime 配置中的可信业务系统。
- `X-Agent-Key-Id` 标识服务账号当前签名密钥；只在轮换窗口接受新旧 key，过期 key 立即拒绝。
- `X-Agent-Signature` 使用 HMAC-SHA256，签名原文为 `{method}\n{path}\n{timestamp}\n{body_sha256}`。
- `X-Agent-Timestamp` 默认容忍窗口为 300 秒。
- `Idempotency-Key` 用于防止业务后端重试 Runtime 写操作时重复创建 run、迁移状态或触发副作用。
- Runtime 写接口按 `SHA256(upper(method) + "\n" + normalized_path + "\n" + body_sha256)` 计算 request hash，path 包含 `run_id` 等资源标识；相同 key/hash 重放首次结果，method、path 或 body hash 不同返回 HTTP `409 Conflict` + `IDEMPOTENCY_CONFLICT`。
- 需要绑定业务对象的 run 使用 `start_mode=held`；业务保存映射后调用 `/start`，Runtime 不在 HTTP 请求内执行 workflow。
- create/start 在事务接受前返回 429 或可重试 5xx 时不完成幂等记录，调用方按 Retry-After 使用同一 key 重试。

请求：

```json
{
  "agent_id": "memoir_agent",
  "agent_version": "1.0.0",
  "business_type": "couple_memory",
  "business_id": "archive_123",
  "start_mode": "held",
  "input": {
    "archive_id": "archive_123",
    "snapshot_id": "snapshot_456",
    "owner_user_id": "user_789",
    "space_id": "space_1",
    "relationship_segment_no": 2,
    "generation_epoch": 1,
    "locale": "zh-CN"
  },
  "callback_target_id": "couple_diary_memory_callback",
  "business_connector_id": "couple_diary_backend"
}
```

响应：

```json
{
  "run_id": "run_abc",
  "status": "pending",
  "dispatch_state": "held",
  "contract_version": "1.0.0",
  "package_digest": "sha256:...",
  "authorization_version": 12
}
```

### 3.4 HTTP Business Tool 契约

Runtime 调情侣日记后端：

```text
POST {BUSINESS_API}/api/v1/internal/agent-tools/{tool_name}
```

请求头：

```text
X-Agent-Runtime-Id
X-Agent-Key-Id
X-Agent-Run-Id
X-Agent-Tool-Name
X-Agent-Tool-Attempt
X-Agent-Timestamp
X-Agent-Signature
Idempotency-Key
```

请求体：

```json
{
  "input": {},
  "context": {
    "agent_id": "memoir_agent",
    "agent_version": "1.0.0",
    "run_id": "run_abc",
    "step_id": "step_load_snapshot",
    "business_type": "couple_memory",
    "business_id": "archive_123",
    "trace_id": "trace_xyz"
  }
}
```

第一版工具清单：

| 工具 | side effect | 责任 |
|---|---|---|
| `memory.get_snapshot` | 否 | 读取脱敏素材快照 |
| `memory.publish_playback_document` | 是 | 单事务校验并发布完整 document/scenes/actions/`media_manifest`，切换 `published_revision` |

可选工具：

| 工具 | side effect | 责任 |
|---|---|---|
| `memory.enqueue_tts` | 是 | 二期启用；第一版只预留契约 |
| `memory.save_safety_report` | 是 | 可选保存安全审核摘要 |

side effect 幂等键固定为 `{run_id}:{logical_step_key}:{tool_name}:{operation_key}`。execution/step/tool attempt 只用于审计，不能进入幂等键。connector、path、identity、permission、generation token 和 operation key 只能来自 trusted run/manifest/deterministic state。

`X-Agent-Tool-Attempt` 只用于关联物理重试审计，业务后端不得据此授权、去重或判断当前 generation。

### 3.5 Callback 契约

Runtime 回调情侣日记后端：

```text
POST /api/v1/internal/agent-callbacks/memory
```

事件：

```text
run_started
step_changed
waiting_human
partial_succeeded
run_succeeded
run_failed
run_cancelled
```

callback payload：

```json
{
  "event": "step_changed",
  "event_id": "evt_123",
  "event_seq": 12,
  "run_id": "run_abc",
  "agent_id": "memoir_agent",
  "business_id": "archive_123",
  "status": "running",
  "status_version": 8,
  "current_step": "generate_scenes",
  "progress": 60,
  "public_trace": [
    {"step": "generate_scenes", "label": "生成回忆卡片", "status": "running"}
  ],
  "error": null
}
```

规则：

- callback 必须签名。
- callback 最多重试 5 次。
- `Idempotency-Key` 固定为 `callback:{event_id}`；业务后端以 `event_id` 为权威去重标识，同事件与相同 body hash 重放返回成功，同事件不同 body hash 返回 HTTP 409 + `IDEMPOTENCY_CONFLICT`。
- `event_seq` 在单个 run 内单调递增。
- 选择方案 A：`event_seq` 在同一个 `run_id` 的完整生命周期内全局单调递增，retry/resume 后继续累加，不引入 `(attempt_no, event_seq)` 复合版本号。
- 情侣日记后端分别保存 `last_event_seq` 和 `last_runtime_status_version`；任一倒退只记审计，不更新业务状态。
- 不返回 prompt、工具输入输出、模型原始输出、日记原文。
- Runtime 在状态事务中创建不可变 CallbackEvent 和 callback outbox；dispatcher 的首次投递、自动重试和 dead letter 重放复用原 `event_id/event_seq/status_version/Idempotency-Key`。

### 3.6 Public Trace 契约

等级：

```text
none
status_only
public_summary
debug_staff
full_internal
```

`MemoirAgent` 第一版使用 `public_summary`，面向前端只允许展示：

- 整理素材
- 寻找值得保留的片段
- 生成回忆卡片
- 检查隐私与表达
- 保存回忆作品

### 3.7 回忆录业务状态契约

| 状态 | 合法值 | 写入规则 |
|---|---|---|
| `MemoryAgentRunRef.status` | `pending_start/pending/running/waiting_human/succeeded/failed/partial/cancelled` | callback adapter/补偿任务写；Runtime `planning/evaluating` 折叠为 `running` |
| `content_status` | `baseline/pending/running/waiting_human/succeeded/failed/cancelled` | callback 仅在尚未发布时推进运行/失败状态；原子发布工具独占 `succeeded`，发布后 callback 不得降级 |
| `enhancement_status` | `disabled/pending/running/succeeded/partial/failed` | 媒体任务创建器和 media worker 独占 |
| `generation_status` | content/enhancement 派生 | 不单独持久写入 |
| `published_revision` | 已完整发布的 archive revision | baseline 事务或原子发布工具写入 |

callback、作品发布工具和媒体 worker 不得交叉写状态；成功 callback 只确认该 run 已发布 revision，不能自行提交 `content_status=succeeded`。

## 4. 推荐执行顺序

### Task 1: 契约冻结与文档确认

**Plan:** 本总控计划第 3 节 + 后端计划 Task 0。

- [✅] 确认 Runtime API、AgentRun 状态、HTTP Business Tool、callback、public trace 等契约。
- [✅] 冻结 API/Event/Tool/Artifact `contract_version/schema_version` 和兼容性规则。
- [✅] 冻结详细 RuntimeEvent 枚举及其到安全业务 callback 的确定性映射；Runtime 原生事件订阅仍放二期。
- [✅] 冻结 held create/start、dispatch_state、合法状态转换和独立时间预算。
- [✅] 冻结 package digest 与 active/deprecated/revoked 生命周期。
- [✅] 冻结未认领/人工等待 run 的同步取消、claimed run 的协作取消，以及 cancel/retry/approval 互斥规则。
- [✅] 冻结 worker lease/heartbeat/fencing、execution attempt 和稳定逻辑副作用键。
- [✅] 冻结 `ModelCallContext` 的可信来源、permit 后二次校验、物理 model attempt 预写/结算和 outcome unknown 保守成本语义。
- [✅] 冻结 privacy purge、authorization version、trusted/untrusted envelope 和语义校验。
- [✅] 冻结 RuntimeAuditEvent 字段、持久 audit sink、保留/访问策略和禁止记录的私密字段。
- [✅] 确认 HMAC-SHA256 签名原文、时间戳容忍窗口、`create/start/retry/cancel/human_approval/purge` 幂等作用域、TTL、过期原子换代和冲突响应；Runtime request hash 覆盖 method、包含资源 ID 的 normalized path 和 body hash，`AgentRun.create_idempotency_key` 只作审计索引，不承担永久唯一约束。
- [✅] 确认 callback HMAC-SHA256 签名请求头、业务侧验签规则和 header/body 一致性校验。
- [✅] 确认 callback `event_seq/status_version` 乱序保护。
- [✅] 确认 `event_seq` 采用 run 生命周期全局单调递增，retry/resume 后不重置。
- [✅] 确认手动重试计数与 Runtime 自动节点重试计数隔离。
- [✅] 确认第一版只支持 Workflow Agent。
- [✅] 确认 `memoir_agent@1.0.0` 是首个 AgentPackage。
- [✅] 确认 Runtime 服务目录为 ``，权威运行数据库与迁移方案已选定。

**Checkpoint:** Runtime、情侣日记后端、前端都以本契约为准。

### Task 2: Runtime Python 工程骨架

**Plan:** 后端计划 Task 1。

- [✅] 新建 ``。
- [✅] 建立 Contract 包、FastAPI app、配置、日志、`/health/live`、`/health/ready` 和鉴权 capabilities。
- [✅] 建立追加写 AuditService；生产缺少持久、访问受限的 audit sink，或部署声明启用的 outbox event type 缺少 handler 时 readiness 返回 503。
- [✅] 配置可信业务系统、签名容忍时间、Arq Redis 队列名和 Worker 启动命令。
- [✅] 配置模型流量控制 namespace 和 permit TTL；共享控制不可用时固定 fail closed，不提供进程内无限调用开关。
- [✅] 建立测试框架和 lint/type check 命令。
- [✅] 跑 `ruff check .` 和健康检查测试。

**Checkpoint:** AgentRuntime 服务可以单独启动。

### Task 3: Runtime 数据模型与迁移

**Plan:** 后端计划 Task 2。

- [✅] 创建 Runtime 核心表。
- [✅] 增加 RuntimeOutboxEvent、lease/fencing、dispatch、attempt、privacy、authorization 和独立时钟字段。
- [✅] AgentToolCall 保存稳定 logical operation key、业务幂等键和最终签名 body 的 request digest；同一逻辑操作允许保留多次物理 attempt 审计记录。
- [✅] AgentModelUsage 每行保存一个候选物理 model attempt 的 execution/model attempt、running/aborted/terminal/unknown 状态、permit、capability snapshot、`pricing_config_version/cost_unit`、`reserved_estimated_cost/estimated_cost` 与 token usage；未知 token 保持空值，不能伪装成 0，aborted_before_send 不表示已经请求 provider。
- [✅] 增加 `AdmissionBucket(scope_type, scope_key, held_count, queued_count, running_count, version)`，建立 scope 唯一约束与非负 check；`AgentRun.dispatch_state` 作为对账重建来源。
- [✅] Checkpoint 使用加密/TTL/classification，Artifact 使用摘要/digest/业务引用 envelope。
- [✅] 创建 Alembic 初始迁移。
- [✅] 覆盖 metadata 和唯一约束测试。

**Checkpoint:** Runtime 可以持久化 run、plan、step、tool、evaluation、checkpoint、artifact、model usage。

### Task 4: AgentPackage 与 MemoirAgent 包

**Plan:** 后端计划 Task 3、Task 12 的包定义部分。

- [✅] 定义 AgentPackage schema。
- [✅] 创建 `memoir_agent@1.0.0` 文件包。
- [✅] 固定 `agent.yaml`、`input.schema.json`、`output.schema.json`、受信任 `workflow.graph.py`、`prompts/`、`tools.manifest.json`、`guardrails.yaml`、`callbacks.yaml`、`ui-trace.yaml` 和 `evals/`。
- [✅] 加载器校验版本、schema、workflow、prompt 引用、工具清单、guardrails、callback、ui trace 和至少 5 条最小 eval 用例。
- [✅] 冻结 `policy.waiting_human_timeout_action=fallback|failed|cancelled`；只有启用 `waiting_human` callback 的 package 才允许进入人工等待，fallback 必须指向确定性的恢复节点。
- [✅] Tool manifest 预留 `mcp_server_id/mcp_tool_name/mcp_resource_uri`，并冻结 AI SDK 等价 tool schema fixture；第一版仅验证兼容，不连接 MCP。
- [✅] Tool manifest 冻结 `connector_id/method/relative path/input_from/output_to`；完整 URL、未声明状态路径和覆盖 trusted 控制字段的映射在注册期拒绝。
- [✅] 构建不可变 package digest，排除签名文件、构建时间和 digest 自身等生成元数据；同版本不同 digest 拒绝注册，revoked 支持在途安全停止。
- [✅] Package active/deprecated/revoked 变化记录操作者、原因、时间并写 RuntimeAuditEvent。

**Checkpoint:** Runtime 可以加载指定版本 AgentPackage，不能自动使用最新版。

### Task 5: AgentRun API 与静态 Planner

**Plan:** 后端计划 Task 4、Task 5。

- [✅] 实现创建、查询、取消、重试和最小人工确认 API；复杂审核台保持二期范围。
- [✅] 所有 Runtime 写接口校验调用方身份、时间戳、HMAC 签名和幂等键。
- [✅] 创建 run 时校验 input schema、caller/tenant、business_type、callback target、business connector、数据域和 Admission 配额。
- [✅] 实现 held create、幂等 start、held 超时和 auto 模式；首次 start 在同一事务完成 `held -> queued`、Admission 迁移、`queued_at`、run_dispatch outbox 和幂等响应。同 key/hash 重放首次响应，新 key 请求已 queued/claimed 的 run 只返回当前安全摘要，不重复迁移配额或写 outbox。
- [✅] 在 AgentRun API 阶段完成签名、授权、connector registry、AdmissionService 和事务 OutboxService；create held/auto、start、retry、human approval/fallback 恢复的配额预留、状态迁移和 dispatch outbox 同事务，429 不改变状态或固化幂等结果，cancel/purge 永不被 Admission 阻塞。
- [✅] AdmissionService 以 global 规范键 `*` 和认证上下文中的 caller/tenant/agent ID 幂等 upsert bucket，再按 `(scope_type, scope_key)` 固定顺序锁定，并以 `dispatch_state` 完成 `none/held/queued/claimed/finished` 占用迁移；幂等命中、条件写失败和回滚不改变计数。
- [✅] 实现 `IdempotencyRecord`，按 `client_id + scope + key` 隔离 `create/start/retry/cancel/human_approval/purge`；request hash 固定覆盖 method、normalized path 和 body hash，未过期且 hash 相同返回原结果，不同返回 HTTP `409 Conflict` + `IDEMPOTENCY_CONFLICT`，过期记录在数据库锁保护下原子换代。
- [✅] AgentRun 查询返回当前 `status/dispatch_state/status_version/last_event_seq/execution_attempt/privacy_state/privacy_version`、purge 时间、更新时间和安全 public trace；业务对账不得依赖 create/start/purge 的缓存响应推断当前状态。
- [✅] 创建时冻结不含密钥/endpoint 的 capability snapshot；查询额外返回由持久 `AgentStep` 推导的安全 `progress/current_step` 摘要。
- [✅] `AgentRun` 仅保存 `create_idempotency_key` 作为普通审计索引，不建立跨 TTL 的永久唯一约束；维护任务清理过期记录，purge 记录至少保留至清理完成并满足审计保留期。
- [✅] 取消 `pending + held/queued` 或 `waiting_human + finished` 时在 API 事务内直接终止并创建 callback outbox；claimed run 只写取消请求，由有效 fencing worker 在安全边界终止，所有路径与 retry/approval 互斥。
- [✅] approval/cancel/retry/purge 写入脱敏且追加的 `RuntimeAuditEvent`，与 Run 状态变更在同一事务持久化。
- [✅] 人工确认只接受 `status=waiting_human AND dispatch_state=finished` 的 run，要求独立幂等键、`decision=approve|reject` 和 `expected_status_version`；approve 在同一事务只推进 `dispatch_state: finished -> queued`、递增 `status_version` 并写 dispatch outbox，run 状态由新 worker 取得 lease 后从 `waiting_human` 迁移为 `running`；reject 按 package 策略重新入队执行 fallback，或终结为 failed/cancelled，过期版本返回冲突且不推进状态。
- [✅] Retry 只允许原 caller 或内部审计身份调用，手动 run 级重试默认最多 3 次。
- [ ] `partial` 只重试 Runtime 未完成的可选步骤，不重新发布主作品；依赖 Task 6 写入节点完成状态、可选节点声明与发布结果。
- [✅] Runtime 自动节点重试使用 step attempt / `auto_retry_count`，不消耗手动重试次数。
- [✅] 为 `memoir_agent` 生成静态 `AgentPlan`；产生真实恢复状态后再保存首个 checkpoint，不创建空 Artifact。

**Checkpoint:** 情侣日记后端可以创建 run 并拿到 `run_id`。

### Task 5.5: Runtime 队列与 Worker

**Plan:** 后端计划 Task 4.5。

- [✅] 实现 `RunQueueService`。
- [✅] 实现 `python -m app.worker` 或等价 Arq Worker 启动入口。
- [✅] dispatcher lease 认领 run_dispatch outbox，可用 Arq 通知 Worker。
- [✅] dispatcher 使用按 `event_type` 的显式 handler registry；本阶段只启用 `run_dispatch`，认领查询不选择未启用或缺 handler 的事件，后者保持 pending 且不增加 attempt/dead-letter；两类事件分开轮询以避免头阻塞。
- [✅] Worker 使用数据库条件写原子认领 `dispatch_state=queued` 且 status 为 `pending/waiting_human` 的可执行 run，取得 execution attempt、lease 和 fencing token；cancelled 或已请求取消的 run 不可认领。
- [✅] Worker claim 同事务执行 Admission `queued -> running`，进入 waiting_human/终态执行 `running -> none`；reaper 回收 lease 时执行 `running -> queued` 并写新的 dispatch outbox。
- [✅] 队列阶段先冻结可注入的 `RunExecutor.run(run_id, lease_context)` 协议并用 fake executor 验证；后端 Task 6 的 WorkflowExecutor 再实现该协议，避免 Worker 任务前向依赖未完成执行器。
- [✅] heartbeat 失效由 reaper 回收；旧 worker 迟到写入被 fencing 拒绝。
- [✅] draining 在执行器返回的非终态安全边界停止续租，由 reaper 以原子 `claimed -> queued` 迁移接管，Admission 占用不会提前释放。
- [✅] cancel/retry 互斥；实例进入 draining 后停止新认领、readiness 503、liveness 成功；安全返回边界会让 lease 到期，Admission 占用由 reaper 的 `claimed -> queued` 事务迁移，接管时只创建一个新 execution attempt。
- [ ] 在途 Worker 宽限期内 heartbeat、checkpoint 落库后停止写入，以及宽限期耗尽时停止新的模型/工具调用；依赖 Task 6 的真实执行器、CheckpointStore、ModelGateway 与 ToolGateway。

**Checkpoint:** 长任务脱离 HTTP 请求执行，Redis 丢失/重复不影响正确性，同一 run 只有有效 fencing token 的 Worker 可以写入。

### Task 6: Runtime Executor 与 Step 观测

**Plan:** 后端计划 Task 6、Task 10。

> 阶段完成基础（不新增计划任务）：已实现可注入 mock `WorkflowExecutor`，可按静态 AgentPlan 写 `AgentStep.running/succeeded` 安全摘要；同时提供独立 `CheckpointStore`，以 Fernet 认证加密完整恢复状态，写入仅含节点进度的安全摘要、TTL、privacy/fencing 校验及脱敏持久审计。执行器与加密恢复状态的正式注入仍待后续节点/密钥配置一并完成。原有 Task 6 条目仍按完整验收语义逐项标记。
- [ ] 加载受信任 `workflow.graph.py` 并从导出 manifest 生成 AgentPlan。
- [ ] 执行每个节点时写 `AgentStep`。
- [ ] 每个节点完成后写 checkpoint。
- [ ] 通过 ArtifactStore 保存摘要、digest 和业务资源引用；临时私密 payload 受 fencing/privacy version、TTL 和 purge 联动约束。
- [ ] 支持从 checkpoint resume。
- [ ] 支持 fallback 节点。
- [ ] `human_review` 决策先持久化恢复 checkpoint；成功后 Worker 原子设置 `waiting_expires_at`、收敛 `status=waiting_human/dispatch_state=finished`、释放 lease 和 Admission，并创建 callback outbox。超时按 package 的 `waiting_human_timeout_action` 条件恢复或终止，迟到审批因 `status_version` 不匹配而失败。
- [ ] 每个节点前校验 fencing、cancel、package、privacy 和 authorization version。

**Checkpoint:** mock workflow 可以完整执行，并能在失败后恢复。

### Task 6.5: 情侣日记归档、快照与播放文档底座

**Files:**
- Create: `app/models/memory_archive.py`
- Create: `app/models/memory_snapshot.py`
- Create: `app/models/memory_playback_document.py`
- Create: `app/models/memory_scene.py`
- Create: `app/models/memory_action.py`
- Create: `app/models/memory_media_asset.py`
- Create: `app/models/memory_agent_run_ref.py`
- Create: `app/services/memory_archive_service.py`
- Create: `app/services/memory_snapshot_service.py`
- Create: `app/services/memory_player_service.py`
- Create: `alembic/versions/<revision>_memory_runtime_foundation.py`
- Test: `tests/test_memory_archive_snapshot.py`
- Test: `tests/test_memory_playback_publication.py`

**Interfaces:**
- Produces: `MemoryArchiveService.create_archives_for_relationship(relationship_id)`、加密 `MemorySnapshot`、revision 0 baseline、`published_revision` 读取边界和 `MemoryAgentRunRef`。

- [✅] 建立 `MemoryArchive/MemorySnapshot/MemoryPlaybackDocument/MemoryScene/MemoryAction/MemoryMediaAsset/MemoryAgentRunRef`、Alembic 迁移和 `MemoryArchiveService` 基础实现；以显式 `FrozenMemoryInput` 接收冻结素材，使用 Fernet 认证加密快照，并在同一事务为双方创建隔离 archive 与 revision 0 baseline。
- [✅] 建立 `MemoryPlayerService` 的 `published_revision` 读取边界，以及完整 PlaybackDocument 的原子发布：先校验并落库完整 `scenes/actions/media_manifest`，再在同一事务切换唯一发布指针。
- [ ] 和平解绑或强制拉黑事务冻结 `space_id + relationship_segment_no + snapshot_cutoff_at + source manifest/version`，为双方创建独立 archive，并写 snapshot/run outbox。
- [ ] `MemoryArchive` 保存 owner/partner 快照、关系时间、summary、content/enhancement 状态、generation epoch、active run、published revision、pin/delete 字段；`MemoryAgentRunRef` 保存运行摘要、retry、event/status version、row version、各操作幂等键和 purge 对账字段。
- [ ] 快照 materializer 只按冻结 manifest 读取素材，保存 `source_manifest_hash/privacy_filter_version`，不读取任务执行时新增或变更的数据。
- [ ] `MemorySnapshot` 保存 `snapshot_version/source_range_json/diary_items_json/bet_items_json/stats_json/privacy_filter_version/snapshot_cutoff_at/source_manifest_hash`；正文使用数据库或应用层静态加密，只允许 memory service 和内部 tool 身份读取，通用后台、debug 日志和导出任务不得展开。
- [ ] `MemorySnapshot/MemoryPlaybackDocument/MemoryScene/MemoryAction` 分别维护版本；数据库保留原始 snapshot version，服务层单向迁移到当前领域模型，未知未来 major 禁止旧服务写回覆盖。
- [ ] Scene schema 冻结 `cover/stats/diary_highlight/bet_highlight/image/milestone/summary` 与 `normal/sensitive/fallback`；Action schema 冻结 `show_card/focus_image/type_text/hold/play_tts/transition`，并校验 order、duration、scene/media 引用。
- [ ] 每个 AI Scene 持久化经当前 snapshot allowlist 校验的 `source_refs_json`；业务后端建立按 source ref 反查 archive/revision 的 JSON 索引或等价引用映射，并保证映射与 Scene 在同一发布事务提交、随 revision 清理，避免素材删除补偿依赖全表扫描或模型原文。
- [ ] 建立 `unique(space_id, relationship_segment_no, owner_user_id)`、`unique(run_id)`、`unique(archive_id, generation_epoch)`、`unique(archive_id, revision)`，并为 Scene/Action/Media 建立指向同一 document/archive 的外键或等价约束。
- [ ] archive 创建时发布 revision 0 baseline，包含封面、基础统计、总结和默认 Action；Runtime 不可用时仍可播放。
- [ ] 建立 `content_status/enhancement_status` 唯一写入者，`generation_status` 只由两者派生。
- [ ] 详情服务只读取 `published_revision` 指向的完整 document/scenes/actions/media，不拼接草稿或不同 revision。
- [ ] `MemoryMediaAsset` 持久化 `storage_key` 和 `prompt_hash`，不持久化带签名的 `access_url` 或敏感 prompt；短期访问地址只在鉴权 API 响应时生成。
- [ ] MediaAsset 冻结 `image/audio/video`、`diary_original/ai_generated/tts/default_asset`、`ready/deleting/deleted`；生成中和失败状态只属于二期 MediaTask，不混入 Asset 状态。
- [ ] 普通 superseded revision 按 `retain_until` 宽限后幂等 GC；隐私删除立即撤权，不使用普通宽限。
- [ ] 测试重复归档幂等、双方 archive 隔离、跨段号过滤、冻结 manifest、baseline 可播放和 revision 指针一致性。

**Checkpoint:** Runtime 开始接入前，情侣日记后端已经具备稳定快照、baseline 和原子作品版本容器。

### Task 6.75: 情侣日记后端 Runtime 能力握手与适配器

**Files:**
- Create: `app/services/memory_agent_adapter.py`
- Create: `app/services/memory_runtime_capability_cache.py`
- Test: `tests/test_memory_agent_adapter.py`
- Test: `tests/test_memory_runtime_capabilities.py`

**Interfaces:**
- Consumes: Runtime `/health/ready`、`/api/v1/runtime-capabilities` 和 held AgentRun API。
- Produces: `MemoryAgentAdapter.start_memoir_agent(archive_id: str, snapshot_id: str, generation_epoch: int) -> MemoryAgentRunRef` 及可失效的兼容能力缓存。

- [ ] adapter 在启动、缓存过期或 Runtime 版本变化时使用服务身份检查 readiness/capabilities，校验 Contract major、`memoir_agent@1.0.0`、所需逻辑 model policy 和能力开关。
- [ ] 能力检查不进入解绑事务同步关键路径；Runtime 不可用、draining、版本不兼容或缺少 policy 时保留 baseline，由业务 outbox/补偿任务等待恢复后重试。
- [ ] capability 缓存记录 Runtime/contract/package 摘要和过期时间；版本变化立即失效，不缓存密钥、真实 provider/connector endpoint 或租户配额。
- [ ] 创建 held run 使用稳定 create 幂等键；返回后保存 `contract_version/package_digest/authorization_version`，不支持的 major 拒绝绑定和 start。
- [ ] archive 在绑定前已删除、epoch 已变化或已有更新 active run 时取消新 run，只保留审计；start 使用独立稳定幂等键。
- [ ] `pending_start_timeout_seconds=600`；超过 10 分钟仍未绑定 run_id 时由业务补偿任务复用原 create 幂等键修复或明确标记失败，不无限等待。

**Checkpoint:** Runtime 故障或版本漂移不会阻断解绑归档，也不会让不兼容 AgentRun 越过 baseline 降级边界。

### Task 7: ToolGateway 与情侣日记后端工具 API 并行开发

**Plan:** Runtime 后端计划 Task 7 + 回忆录技术探索 `06-后端接口与AgentRuntime集成.md`。

Runtime 侧：

- [ ] 实现 `ToolGateway.call()`。
- [ ] 实现 JSON repair、摘要压缩、敏感字段扫描等少量 Native Tool，以及 LangChain Tool 基础包装；所有 adapter 最终回到 ToolGateway，不自行请求业务 connector。
- [ ] 实现 HTTP Business Tool 签名、幂等、超时、重试。
- [ ] 通过 connector registry 解析固定 endpoint，阻止 SSRF 和重定向。
- [ ] 使用不含 attempt 的稳定逻辑幂等键，并校验 trusted 参数来源。
- [ ] side effect 工具先持久化 `AgentToolCall.running + logical_operation_key + idempotency_key + request_digest`，事务提交后再发送请求；每次物理 attempt 独立记录，重试/接管复用同一逻辑键和 digest。
- [ ] 工具结果经 output schema、敏感扫描和 `output_to` allowlist 写入 AgentState，不能覆盖 identity、authorization、connector 或 generation/version token。
- [ ] 按工具声明的 `cancellation_behavior=cancellable/non_cancellable/query_after_commit` 处理中断；已提交副作用只允许用原逻辑幂等键查询结果，不能因 attempt 变化重放。
- [ ] ToolCall/AgentState 结果写入继续校验 fencing、privacy 和 authorization；迟到或不确定结果留给对账按原 key 查询。相同 key 不同 digest 的 409 按不可重试冲突终止，禁止更换 key 重放。

情侣日记后端侧：

- [ ] 暴露 `POST /api/v1/internal/agent-tools/memory.get_snapshot`。
- [ ] 暴露 `POST /api/v1/internal/agent-tools/memory.publish_playback_document`，单事务发布完整作品。
- [ ] 发布请求接收完整 document/scenes/actions/`media_manifest`、`run_id/snapshot_id/generation_epoch` 和稳定幂等键；媒体能力关闭时仍要求空 `media_manifest`，并按 scenes/actions/`media_manifest` 规范化内容计算或复核 digest；成功返回 `revision/content_digest`，供 Runtime 固化 `publish_result`。
- [ ] 第一版只预留 `memory.enqueue_tts` 契约，不启用媒体任务。
- [ ] 校验 Runtime 服务身份/key、archive/owner、active_run_id、generation_epoch、素材引用和幂等键。

**Checkpoint:** Runtime 能通过 mock 或真实情侣日记后端调用 `memory.*` 工具。

### Task 8: ModelGateway、PromptRegistry、ContextManager、评价与护栏

**Plan:** 后端计划 Task 8、Task 9。

- [ ] 实现文件化 PromptRegistry。
- [ ] PromptRegistry 校验 `prompt_id/version/owner_agent/input_schema/output_schema/model_policy/guardrail_policy/status`，节点精确引用版本且不自动回退 latest，模型调用记录 prompt id/version。
- [ ] 使用 LangChain `PromptTemplate/ChatPromptTemplate`、Pydantic structured parser 和 ContextManager/usage/安全 middleware hook；第一版不启用 createAgent 动态工具选择。
- [ ] 实现 LiteLLM / Provider Adapter。
- [ ] 固化第一版 `model_policy.yaml`，包含 `reasoning/balanced/emotional_writing/cheap_structured/strict/private_first` 映射。
- [ ] 每个可信 route 配置 `rate_limit_key/max_concurrency/rpm/tpm/request_timeout_seconds/permit_ttl_seconds/settle_margin_seconds/circuit_failure_threshold/circuit_open_seconds/pricing_config_version/cost_unit/input_unit_cost/output_unit_cost`；rate_limit_key 由 provider account、model 和部署分区生成，不含 key，价格与流量字段均不接受业务输入覆盖。
- [ ] route 注册强制 `permit_ttl_seconds >= request_timeout_seconds + settle_margin_seconds`，单次 HTTP timeout 不超过 acquire deadline；配置非法时禁用 route，避免 permit 已释放而请求仍占用上游并发。
- [ ] Runtime 预算统一使用一种 cost unit；预留和实际估算成本按该物理 attempt 固化的 pricing version 计算。内部/免费模型显式零价，缺失价格、单位不一致或数值非法时禁用 route/capability。
- [ ] 每个 policy 固定 `max_output_tokens`、能力要求和显式 fallback；ModelGateway 校验 structured output、vision、上下文长度、数据驻留与 thinking 参数，不满足时禁止任意选用默认模型。
- [ ] provider endpoint 只允许管理员在 registry 配置；校验协议、host、port、DNS/IP、内网地址和每次重定向，AgentPackage、业务请求和 prompt 均不能覆盖 endpoint/key。
- [ ] `private_first` 没有合规私有 provider 时显式返回 capability disabled 或执行 policy 声明的 fallback，禁止静默改用任意云模型。
- [ ] 实现 ContextManager 的 token 预算、素材分块、工具结果摘要压缩和敏感字段二次扫描。
- [ ] 隔离 trusted instructions/untrusted content，并对 material/source ID、数字、Action 和工具参数做确定性语义校验。
- [ ] 实现结构化输出、JSON repair、schema 校验。
- [ ] 实现 `ProviderTrafficController.acquire/mark_started/settle`；Redis 原子维护共享并发、RPM/TPM、blocked_until、熔断和 `acquired -> started -> settled` permit 状态，mark/settle 采用 CAS 且重复调用不重复增减计数。
- [ ] 每次候选请求单独 acquire/finally settle；只有 acquired permit 可按 aborted_before_send 原子释放并发槽、回滚 RPM/TPM 预留，started permit 无 usage 或结果未知时保留预留到窗口过期。acquired TTL 回收时回滚未发送预留，started TTL 回收只释放并发槽；重试等待不持有 permit，上游 429 的 Retry-After 写入共享冷却，fallback route 单独取 permit。
- [ ] permit 等待受节点 timeout、剩余 active budget 和 run deadline 的最小值约束并计入 active elapsed；共享控制不可用时进入显式 provider fallback、模板 fallback 或安全失败。
- [ ] Executor 只从有效 LeaseContext 构造 `ModelCallContext(run/step/execution attempt/lease owner/fencing/privacy/authorization/deadline)`；prompt、业务 input、AgentState 和模型输出不能覆盖这些字段。
- [ ] acquire 后再次校验 lease/fencing、cancel、package、privacy、authorization、route/capability 和 deadline；等待期间失效时释放 permit，不写 usage、不请求 provider。
- [ ] 实现 `ModelUsageService` 与 `AgentModelUsage` 生命周期：二次校验后先提交 running usage 和 token/成本预留，提交后、真正发送 HTTP 前再次执行发送边界校验；失效时把既有 usage/acquired permit 结算为 aborted_before_send。复核通过后也要先成功 mark_started 再请求 provider；每次 retry/fallback 独立记录候选 model attempt，返回后分别 settle permit 和 usage。
- [ ] 响应后执行同一安全边界校验；上下文已失效时丢弃模型输出，只允许幂等结算原 usage 行的无内容 token、成本、provider request ID 和状态，不能推进 run/step/checkpoint/artifact。
- [ ] 过期 running usage 转为 `outcome_unknown` 并继续按预留成本计量；PolicyEngine 计算 `max_model_calls/max_estimated_cost` 时，aborted_before_send 不计，已观察 usage 用实际估算成本，未决/未知记录用预留成本，同一行不重复相加。
- [ ] `thinking_summary` 只记录能力开关、预算和归一化参数，不保存隐藏推理文本。
- [ ] 实现 Evaluator、Guardrails、PolicyEngine。
- [ ] 实现 AdmissionController：AdmissionBucket 管理 global/caller/tenant/agent 的 held/queued/running；实际路由确定后由 ProviderTrafficController 管理 provider/model 流量。PolicyEngine 负责 active/held/queue/approval/wall clock 预算，不重复实现限流状态。
- [ ] capabilities 在共享流量控制异常时把模型增强标为不可用，禁止继续宣告可用后由各 Worker 自行放行。
- [ ] 禁止完整 prompt 和私密素材入日志。

**Checkpoint:** 模型节点输出可控、可评价、可降级，成本可记录。

### Task 9: MemoirAgent MVP 工作流

**Plan:** 后端计划 Task 12。

- [ ] 实现 `load_snapshot`。
- [ ] 实现 `sanitize_materials`。
- [ ] 实现 `compute_stats`。
- [ ] 实现 `extract_highlights` 和模板高光。
- [ ] 实现 `plan_chapters` 和模板章节。
- [ ] 实现 `generate_scenes` 和模板场景。
- [ ] 实现规则版 `generate_actions`。
- [ ] MemoirAgent MVP 正常生成 3～8 张场景卡，单卡主体文案不超过 80 字；发布契约硬上限 16，越界由 evaluator 裁剪、fallback 或拒绝。
- [ ] 实现 `safety_review`。
- [ ] 构建包含 scenes/actions/`media_manifest` 的完整 playback document，并实现 `publish_playback_document` 原子发布；媒体能力关闭时提交必填空清单。
- [ ] 发布请求带 `run_id/snapshot_id/generation_epoch` 和稳定逻辑幂等键，业务后端复核包含 `media_manifest` 的 `content_digest`，只有成功后才允许 run 终止为 succeeded/partial。
- [ ] 第一版媒体节点在 capability 关闭时 skipped；媒体生成放二期。

**Checkpoint:** `revision 0 baseline -> MemorySnapshot -> 完整 PlaybackDocument 原子发布 -> published_revision` 闭环可跑通。

### Task 10: Callback 与业务生成状态

**Plan:** 后端计划 Task 11 + 情侣日记后端 `MemoryAgentRunRef`。

Runtime 侧：

- [ ] 生成 `run_started`、`step_changed`、可选 `waiting_human`、`run_succeeded`、`run_failed`、`partial_succeeded`、`run_cancelled` 事件；内部 `human_review_requested` 确定性映射为 `waiting_human`。
- [ ] 每个 callback 事件带 `event_id/event_seq/status_version`。
- [ ] 为 dispatcher 注册 callback OutboxDeliveryHandler 后才启用 `callback` 类型；此前 pending 事件不算失败，启用后继续使用原事件身份投递，callback 堆积不阻塞 run_dispatch。
- [ ] callback payload 只包含安全摘要。
- [ ] callback 请求带 `X-Agent-Runtime-Id`、`X-Agent-Key-Id`、`X-Agent-Run-Id`、`X-Agent-Business-Id`、`X-Agent-Event-Id`、`X-Agent-Event-Seq`、`X-Agent-Timestamp`、`X-Agent-Signature`、`Idempotency-Key` 并由业务后端验签；幂等键固定为 `callback:{event_id}`。
- [ ] 业务后端按原始 body bytes 计算 hash、使用恒定时间比较，并只在密钥轮换窗口接受新旧 key；签名 callback 禁止重定向。
- [ ] retry/resume 后 callback `event_seq` 继续从当前 run 最大值累加。
- [ ] callback 失败重试复用原 `event_id/event_seq/status_version/Idempotency-Key`；业务端对同事件同 body 返回成功且不重复写，对同事件不同 body 返回 409 幂等冲突。
- [ ] 状态变化、CallbackEvent 与 callback outbox 同事务提交；dispatcher 使用 lease、Retry-After、dead letter 和原事件重放。
- [ ] callback 前复核 target 当前 authorization version，撤销后停止发送并告警。

情侣日记后端侧：

- [ ] 新增或更新 `memory_agent_run_refs`。
- [ ] 每次 create/start/retry/purge 保存独立幂等键、`run_id/active_run_id/generation_epoch/row_version`、contract/package/authorization 摘要、last_event_seq、last_runtime_status_version，以及 `privacy_purge_status=not_requested/requested/purged/failed`、`privacy_purge_idempotency_key` 和请求/完成时间。
- [ ] 接收 callback 后始终幂等更新对应 `MemoryAgentRunRef`；只有 active run/epoch 匹配且内容尚未发布时，callback 才可推进 `content_status` 的 pending/running/waiting_human/failed/cancelled 与 `public_trace`，不得写 `published_revision/enhancement_status/succeeded`。
- [ ] `run_succeeded/partial_succeeded` 必须确认业务库已存在该 run 原子发布的 revision 且 `content_status=succeeded`；callback 只接受终态摘要，不重复写成功。缺少发布结果时保留 baseline/上一版本，记录 `RECONCILIATION_NEEDED` 并告警对账。
- [ ] 按 `event_seq/status_version` 拒绝 callback 乱序导致的状态倒退。
- [ ] 重复 callback 不重复写入。
- [ ] 为前端生成状态接口返回 `public_trace`。

**Checkpoint:** 前端通过情侣日记后端能看到生成状态变化，不需要直连 Runtime。

### Task 10.5: 补偿、对账与 SSE 定案

**Plan:** 后端计划 Task 11.5 + 回忆录技术探索 `06-后端接口与AgentRuntime集成.md`。

- [ ] Runtime 对账扫描 dispatch/callback dead letter、lease/heartbeat、active elapsed、held/queued/waiting_human、wall clock、tool call、purge、package revoked 和 authorization version 状态；没有 worker 的挂起 run 必须由对账任务直接条件终止或按 package 策略重新入队。
- [ ] Runtime 对账扫描超过请求 deadline 的 `AgentModelUsage.running` 并条件标记 `outcome_unknown`；不猜测零成本、不用旧 fencing 推进执行，迟到可信计量只结算原 usage 行。
- [ ] Runtime 对账比较 AdmissionBucket 与 AgentRun.dispatch_state 的 global/caller/tenant/agent 聚合占用；漂移时按固定锁序和 bucket version 条件修复，保证计数非负并记录安全指标。
- [ ] held/queued/waiting_human 超时、package/authorization 撤销和其他对账终止路径统一复用 AdmissionService；claimed run 只在有效 worker/reaper 的最终状态事务释放 running，避免提前复用仍在执行的槽位。
- [ ] run_dispatch 原事件重放仍失败时置 `failed(DISPATCH_FAILED)`，同事务释放 held/queued Admission 占用；callback dead letter 由原事件重放和业务主动查询恢复。
- [ ] 对账任务默认每 5 分钟执行一次；同一对象连续 3 次修复失败后升级告警。
- [ ] 多实例对账使用数据库/分布式 lease 或按 `run_id` 分片，同一对象同一时间只允许一个修复者。
- [ ] 提供独立 reconciler 进程入口；调度/lease、纯规则判定、事务修复和报告聚合分别归入口、runtime rule、service、schema 负责。
- [ ] 每个扫描批次输出安全 `ReconciliationReport` 结构化日志和指标，固定包含扫描、修复、失败、告警计数、动作类型与标准错误码，不携带业务正文或 Runtime 私密 payload。
- [ ] 情侣日记后端保留按 `run_id` 查询 Runtime 的兜底能力，使用 `status_version/last_event_seq` 修复 callback 摘要，并以 `privacy_state/privacy_version` 确认 purge 进度。
- [ ] 业务使用 held create；create 失败只重试 create，绑定后 start 失败只重试 start。
- [ ] 小程序第一版轮询业务状态接口；业务 SSE 只作为已验证平台的可选适配，断线回退轮询。
- [ ] 删除 archive 先撤权、递增 generation_epoch、清空 active_run_id，并在调用 Runtime 前把业务 `privacy_purge_status` 写为 `requested`、保存稳定幂等键。Runtime purge 返回 `202/purge_requested` 只表示写屏障已接受；超时或可重试失败复用原键，相同 key 与 request hash 的重复 POST 重放首次接受响应，不把后来查询到的终态写回幂等响应。业务对账通过 AgentRun 查询到 `privacy_state=purged` 后才写本地 `purged/completed_at`，cancel 成功不能替代清理完成。
- [ ] 原日记或赌局正式删除时，通过 `source_refs_json` 的索引或等价引用映射定位受影响 archive/revision，递增 generation_epoch、取消 active run，并发布移除素材的新 revision；无法安全重写时切回 baseline。
- [ ] 新指针提交后清理旧 snapshot/revision/media；隐私删除立即撤销详情与媒体授权，不等待普通 retain window。
- [ ] 维护任务清理已过期的 `IdempotencyRecord`；purge scope 仅在 run 已 `purged` 且满足审计保留期后清理，避免删除重放重新触发副作用。

**Checkpoint:** Runtime 和业务库状态不一致时有补偿路径，前端生成进度不依赖 Runtime 原生 SSE。

### Task 10.75: 回忆录密码、列表与用户侧业务 API

**Files:**
- Create: `app/api/endpoints/memory_api.py`
- Create: `app/models/memory_password.py`
- Create: `app/services/memory_password_service.py`
- Modify: `app/services/memory_player_service.py`
- Create: `app/schemas/memory.py`
- Test: `tests/test_memory_password_access.py`
- Test: `tests/test_memory_user_api.py`

**Plan:** 回忆录技术探索 `01-产品体验蓝图.md`、`03-数据模型与素材快照.md`、`05-播放器与前端页面.md`、`06-后端接口与AgentRuntime集成.md`。

- [ ] 提供 `POST /api/v1/memory/password/setup` 和 `POST /api/v1/memory/password/verify`；密码限定 4～6 位数字，仅保存强哈希，第一版不提供找回或重置。
- [ ] 连续输错 5 次后冷却 10 分钟；验证成功签发约 15 分钟、绑定当前用户会话的短期解锁凭证，重新进入入口、凭证过期或切后台超过阈值后重新验证。
- [ ] 提供 `GET /api/v1/memory/archives`、`GET /api/v1/memory/archives/{archive_id}` 和 `GET /api/v1/memory/archives/{archive_id}/generation`。
- [ ] 提供 `POST /api/v1/memory/archives/{archive_id}/retry`、`POST /api/v1/memory/archives/{archive_id}/pin`、`POST /api/v1/memory/archives/{archive_id}/unpin` 和 `DELETE /api/v1/memory/archives/{archive_id}`。
- [ ] 所有接口复用现有认证与 `build_api_response_from_request`；密码验证、详情、生成状态和私有媒体响应设置 `Cache-Control: private, no-store`。
- [ ] 私有媒体优先使用鉴权代理或短 TTL 地址，每次访问校验 owner、archive、published document revision 和删除状态；签名 URL 不进入访问日志、持久 Store、错误上报或分享 payload。
- [ ] 列表按 `is_pinned DESC, unbound_at DESC` 排序，每位用户最多置顶一条；解锁前只返回归档 ID、生成状态、解绑日期等最小字段，不返回昵称、头像、摘要或场景内容。
- [ ] 详情、重试、置顶、取消置顶和删除均校验 archive owner；删除先撤权并执行 Task 10.5 的 cancel/purge 流程。
- [ ] 用户重试只允许 `failed/partial` 且存在 checkpoint 的 run，默认最多 3 次；package revoked 或 privacy purge 已开始时拒绝，partial 只恢复未完成的可选增强步骤。

**Checkpoint:** 用户能够通过受密码保护的业务 API 管理自己的回忆录，Runtime 不直接承担用户鉴权或归档 CRUD。

### Task 11: uni-app 回忆录播放器接入

**Plan:** 回忆录技术探索 `05-播放器与前端页面.md`。

- [ ] 回忆录列表展示 `generation_status`。
- [ ] 增加首次设置密码、短期解锁、错误次数/冷却提示；解锁前列表只渲染后端最小字段。
- [ ] 列表支持单条置顶、取消置顶和删除确认，成功后以服务端排序和状态为准刷新。
- [ ] 回忆录详情读取 `archive/scenes/actions/media/agent_run_summary`。
- [ ] AI 尚未发布时读取 revision 0 baseline，只消费 `published_revision` 指向的完整作品。
- [ ] 加载详情后校验 document/scene/action schema major、scene/action/media 引用和 duration 上限；未知 major 停止动态 Action 并降级服务端基础静态卡，同 major 未知可选 Action 记录告警后跳过。
- [ ] 详情与生成状态请求使用 `request<T>()` 且显式 `custom.auth: true`；响应使用 `Cache-Control: private, no-store`，私有媒体 URL 不进入持久 Store、日志或分享 payload。
- [ ] 生成状态响应包含 `status_version/updated_at/retry_after_ms`；连续无变化时退避，页面隐藏、离开或终态立即停止。可选 SSE 使用约 15 秒 heartbeat、`Last-Event-ID`/事件序号恢复、唯一终态事件和断线回退轮询。
- [ ] 生成中展示安全 `public_trace`。
- [ ] 实现 `MemoirActionRunner` 状态机：`idle/loading/ready/playing/paused/ended/replay/error/low_power_ready`。
- [ ] 第一版只执行 `show_card/type_text/hold/transition`，其中 transition 仅允许 `fade/slide`；暂停、切后台、离页和手动切卡时取消当前定时器，恢复时依据状态重新调度，避免并发 Action。
- [ ] `actions` 为空时按 scenes 默认轮播；用户未发生明确手势前不自动播放音频，低性能模式进入 `low_power_ready` 并使用静态卡。
- [ ] `scenes` 为空时展示服务端 baseline 封面与总结空态，不执行 Action，不出现空白播放器。
- [ ] 详情请求失败展示重试；回忆已删除返回列表并清理本地播放状态；图片失败使用默认封面，音频失败静音跳过，列表无数据展示空态。
- [ ] AgentRun 失败时展示基础统计卡和重试入口。
- [ ] 不展示 prompt、工具输入输出、模型原始输出。

**Checkpoint:** 用户只通过业务后端看到回忆作品和生成进度。

### Task 12: 端到端联调

**Plan:** 后端计划 Task 13。

- [ ] 启动 API、dispatcher、worker、reconciler。
- [ ] 启动情侣日记后端。
- [ ] 创建 archive、baseline 和 frozen snapshot manifest，held 创建 AgentRun，绑定 active_run_id 后 start。
- [ ] Runtime 调 `memory.get_snapshot` 获取脱敏快照。
- [ ] Runtime 生成 scenes/actions 和 `media_manifest`；第一版媒体能力关闭时清单为空但字段不省略。
- [ ] Runtime 调 `memory.publish_playback_document` 原子发布完整作品并切换 `published_revision`。
- [ ] 第一版媒体节点为 skipped，播放器静音使用文本作品。
- [ ] Runtime callback 更新 `memory_agent_run_refs`。
- [ ] 前端查询详情并播放。

**Checkpoint:** 完成 `baseline -> held/start -> Runtime 执行 -> 原子发布 -> callback/轮询 -> 前端播放器` 第一版闭环。

### Task 13: 评测、观测与失败复盘

**Plan:** 后端计划 Task 13 + 回忆录技术探索 `13-观测评测与运行治理.md`。

- [ ] 建立最小评测集。
- [ ] 覆盖无日记无赌局、只有日记、只有赌局、双方同日记录、强制拉黑、模型脏 JSON、工具超时。
- [ ] 覆盖冻结 manifest、`source_refs_json` 索引定位原素材后续删除、未知 schema major、旧 revision 媒体迟到、私密缓存、轮询退避和页面后台停止。
- [ ] 覆盖 snapshot 旧版本单向迁移、未知未来 major 拒绝写回、Runtime capability 缓存失效和不兼容 major 保持 baseline。
- [ ] 覆盖 CallbackEvent 不可变、outbox 投递状态分离、密钥轮换、原始 body 验签和 partial retry 不重复发布。
- [ ] 覆盖旧 run callback、generation epoch 变化、成功 callback 缺少 published revision、发布后失败/取消 callback 不降级内容、密码错误冷却、解锁过期、唯一置顶和非 owner 操作。
- [ ] 统计 schema 通过率、素材引用正确率、幻觉率、情绪安全通过率、fallback 触发率、平均成本、平均耗时，以及每次 execution/model attempt 的 aborted_before_send、实际成本、预留成本和 outcome unknown 数量。
- [ ] 统计 admission/队列、provider 限流、outbox/dead letter、隐私 purge、授权撤销、提示注入和语义校验失败指标。
- [ ] 检查日志不含敏感字段。
- [ ] 外部 OTel/LangSmith/调试样本 exporter 默认关闭；启用前配置数据分级、采样字段、区域/跨境、保留期、审计权限和 privacy purge 删除能力，脱敏失败时拒绝导出。
- [ ] 输出第一版失败复盘模板。

**Checkpoint:** Runtime 不只是能跑，还能被排查、评测和持续优化。

## 5. 跨模块职责表

| 能力 | AgentRuntime | 情侣日记后端 | uni-app 前端 |
|---|---|---|---|
| 创建回忆归档 | 不负责 | 负责 | 不负责 |
| 创建素材快照 | 不负责 | 负责 | 不负责 |
| 冻结 source manifest / baseline | 不负责 | 解绑事务冻结并发布 revision 0 | 只消费 baseline |
| 创建 AgentRun | 提供 API | 调 Runtime API | 不负责 |
| Runtime 能力握手 | 提供鉴权 readiness/capabilities | 缓存并校验 Contract/Agent/policy，失败时保留 baseline | 不直连 Runtime |
| Agent 执行 | 负责 | 不负责 | 不负责 |
| 运行调度 / Worker | outbox、lease、heartbeat、fencing、attempt | 不负责 | 不负责 |
| 模型调用 | 负责 | 不负责 | 不负责 |
| 业务权限 | 不绕过 | 负责校验 | 不负责 |
| 工具调用 | 发起调用 | 执行工具 | 不负责 |
| 完整作品发布 | 通过单一原子工具请求 | 事务保存 document/scenes/actions/`media_manifest` 并切换 revision | 只消费 published revision |
| media_tasks / TTS | 第一版只预留契约 | 二期负责媒体任务与 enhancement 状态 | 第一版静音播放 |
| 生成状态 | 保存完整运行状态 | 保存业务摘要 | 只展示摘要 |
| public_trace | 生成安全摘要 | 过滤后返回 | 展示 |
| 完整 trace | 内部审计 | 不面向用户 | 不可见 |
| 密码与短期解锁 | 不负责 | 哈希、限错、签发解锁凭证 | 设置、验证、处理过期 |
| 归档列表/详情/重试 | 不负责用户接口 | 权限校验并提供统一业务响应 | 只调用业务 API |
| 删除、置顶 | 不负责 | owner 校验、唯一置顶、删除撤权 | 发起操作并刷新 |
| 播放状态机 | 不负责 | 提供完整 published document | ActionRunner 执行与降级 |

## 6. 第一版不做

- Runtime 管理后台。
- Runtime 原生用户侧 SSE。
- WebSocket。
- 完整 MCP Client / Server。
- Autonomous Agent。
- Hybrid Agent。
- RAG / Retriever。
- 长期记忆。
- 多 Agent handoff / A2A。
- 代码沙箱。
- 商业计费。
- 分享 H5 生成。
- 视频分镜和 AI 视频。

## 7. 验收场景总表

| 场景 | 预期 |
|---|---|
| 和平解绑创建 snapshot 后启动 AgentRun | Runtime 返回 run_id，业务后端保存映射 |
| held create 后、业务绑定前 | 不执行工具，不发送 run_started；start 重试不创建第二个 run |
| Runtime 创建成功但业务侧 600 秒内未绑定 run_id | `pending_start` 补偿任务复用原 create `Idempotency-Key` 恢复绑定或明确标记失败，不创建第二个 run |
| 强制拉黑创建 snapshot 后启动 AgentRun | 文案不提拉黑，不评价关系 |
| `memoir_agent@1.0.0` 不存在 | 创建失败，不自动使用最新版 |
| callback target / connector 未注册或越权 | 创建失败 |
| 创建 AgentRun 重复请求 | 相同 `client_id + Idempotency-Key + request hash` 重放首次创建响应；run 当前状态通过 GET 查询 |
| start 重复或并发请求 | 同 key/hash 重放首次响应；新 key 命中 queued/claimed 时返回当前摘要且不重复迁移 Admission 或写 dispatch outbox |
| 相同幂等键指向不同资源或请求体 | method、含 run_id 的 path 或 body hash 不同即返回 HTTP 409 + `IDEMPOTENCY_CONFLICT`，不推进资源 |
| 创建幂等记录已过期 | 在数据库锁保护下换代并允许创建新 run；历史 `AgentRun.create_idempotency_key` 不永久阻塞复用 |
| 相同 key 用于不同幂等 scope | create/start/retry/cancel/human_approval/purge 相互独立，不误返回其他操作结果 |
| 受保护读接口的签名与幂等边界 | 合法签名的 capabilities、AgentRun 和 steps GET 不带 `Idempotency-Key` 仍成功；签名缺失、越权读取被拒绝，写接口缺少幂等键被拒绝 |
| start/retry/approval 并发抢占最后一个 queued 配额 | 只有同事务 AdmissionBucket 预留成功的请求推进状态并写 outbox，其余返回 429 且保持原状态和未完成幂等记录 |
| claim/等待/终止/reaper 并发迁移 | AdmissionBucket 与 AgentRun.dispatch_state 聚合一致，重复请求或旧 fencing 不重复增减且计数不为负 |
| 人工确认重复或携带旧 `expected_status_version` | 重复请求返回原结果；旧版本返回冲突，不重复入队或覆盖新状态 |
| 人工确认 approve/reject | approve 只把 dispatch_state 重新入队并与 outbox 同事务提交，worker 认领后才将 run 迁移为 running；reject 按 package 策略进入确定终态或重新入队 fallback |
| 取消 held/queued 或 waiting_human run | API 同事务直接写 cancelled、finished、status_version 和 callback outbox，不等待不存在的 worker |
| 取消 claimed run | 先写 cancel_requested_at；有效 fencing worker 在安全边界终止，迟到 worker 和新副作用被拒绝 |
| workflow 进入人工等待 | 先保存恢复 checkpoint，再原子写 waiting_expires_at、释放 lease/Admission 并发送带版本的 waiting_human；未启用 callback 时 package 校验拒绝人工等待分支 |
| waiting_human 超时与审批并发 | 只有 status_version 条件成功的一方推进；超时按 package 策略 fallback、failed 或 cancelled，迟到审批不覆盖结果 |
| Worker 重复消费同一 run | 数据库条件认领只允许一个有效 fencing token |
| Worker draining 时仍有在途 run | readiness 503、liveness 成功且 lease 在宽限期内持续 heartbeat；完成则正常终止，宽限期耗尽则主动让 lease 到期并停止写入，只由 reaper 创建一个新 attempt |
| Native/LangChain Tool adapter 调用业务工具 | 必须回到 ToolGateway；直接 connector 请求在测试中失败 |
| `memory.get_snapshot` 返回无素材 | 生成基础卡或模板卡 |
| Runtime readiness 失败、draining 或 capabilities 不兼容 | 不创建/启动不兼容 run，baseline 保持可播放，补偿任务等待恢复 |
| 只有日记 | 生成日记统计和高光 |
| 只有赌局 | 生成赌局统计和高光 |
| 模型输出脏 JSON | repair 成功或 fallback |
| 多 Worker 并发请求同一 provider/model | Redis 原子 permit 保证共享并发、RPM/TPM 不超限；各进程不能用本地计数绕过 |
| permit 持有 Worker 崩溃 | acquired permit 的 TTL 回收并回滚未发送 RPM/TPM 预留；started permit 只回收并发槽并保留速率预留到窗口过期 |
| permit TTL 小于请求 timeout + settle margin | route 注册失败且 capability disabled，不发起可能越过 permit 生命周期的请求 |
| route 缺少价格版本、价格非法或 cost unit 不一致 | route/capability 禁用，未知价格不按零成本放行 |
| provider 返回 429 + Retry-After | 写入共享 blocked_until，其他 Worker 同步退避；当前节点在 deadline 内等待或走显式 fallback |
| 共享模型流量控制不可用 | 不请求上游，不退化到本地无限调用；capability 降级并走显式 fallback 或安全失败 |
| permit 等待期间或 usage 预写后、发送前发生取消、撤权、purge、route 禁用或 Worker 失租 | 发送边界校验失败，释放 permit、回滚该 permit 的 RPM/TPM 预留且不请求上游；已预写 usage 结算为 aborted_before_send，HTTP 开始后禁止使用该状态 |
| 模型请求发出后 Worker 崩溃或失租 | 迟到输出不推进工作流；原 usage 过期后为 outcome_unknown 并按预留成本计量，可信迟到计量只结算原记录 |
| `generate_scenes` 失败 | 使用模板场景 |
| `generate_actions` 失败 | 使用默认动作 |
| 媒体能力关闭 / `media_manifest` 缺失、非法或 digest 不一致 | 关闭时提交必填空清单并可正常发布；缺失、非法或摘要不一致时整笔拒绝，`published_revision` 不变化 |
| 原子发布任一校验/落库失败 | 回滚，published_revision 保持原值 |
| 原子发布成功响应 | 返回同一 revision 和规范化 content_digest，Runtime `publish_result` 与业务已发布版本一致 |
| 快照异步物化期间源数据变化 | 仍按解绑事务冻结 manifest 生成，不混入新数据 |
| 已发布后原素材正式删除 | 通过 `source_refs_json` 索引或等价映射定位所有仍可访问 revision，发布移除素材的新 revision 或切回 baseline，旧内容撤权并清理 |
| publish 超时或 Worker 接管 | 同一逻辑幂等键返回原 revision，不重复写 |
| side effect ToolCall 已写 running 后进程崩溃 | 对账使用原幂等键和 request digest 查询或重试，业务只保留首次结果 |
| 相同 side effect 幂等键对应不同请求 digest | 业务返回 409，Runtime 按不可重试契约错误终止并审计，不生成新 key 绕过 |
| 取消发生在副作用工具提交前后 | 可取消工具中止；已提交或不可取消工具按原幂等键查结果，旧 fencing/privacy version 不推进 run |
| provider endpoint 指向内网、DNS 重绑定或重定向到未授权地址 | ModelGateway 拒绝请求并记录安全指标，不向目标发起后续调用 |
| `private_first` 未配置私有 provider | 显式 capability disabled 或使用已声明 fallback，不静默切换云模型 |
| 媒体 capability 关闭 | 节点 skipped，文本卡片静音播放 |
| 未知播放契约 major | 前端停止动态 Action，降级基础静态卡 |
| snapshot 为旧版本或未知未来 major | 旧版本由服务层单向迁移；未知未来 major 拒绝旧服务写回覆盖 |
| MemoirAgent 场景数或单卡长度越界 | MVP 裁剪/fallback 到 3～8 张且单卡不超过 80 字，绝不发布超过 16 张的作品 |
| 小程序页面进入后台 | 停止状态轮询、Action 计时和音频；回到前台按当前状态恢复 |
| `actions` 为空或设备进入低性能模式 | 默认轮播 scenes 或展示静态卡，不出现空白页和并发计时器 |
| `scenes` 为空 | 展示 baseline 封面与总结空态，不执行 Action |
| 详情/图片/音频失败或回忆已删除 | 分别提供重试、默认封面、静音跳过或返回列表，不残留旧播放状态 |
| callback 重复或 event/body 冲突 | 同一 `event_id + body hash` 与 `callback:{event_id}` 重放成功且不重复更新；同 event 不同 body 返回 409，不生成新事件或新键绕过 |
| callback 乱序 | 业务后端按 event_seq/status_version 拒绝状态倒退 |
| 旧 run callback 晚于新一轮生成到达 | 更新旧 `MemoryAgentRunRef`，但不改变当前 archive generation status |
| 成功 callback 没有该 run 的 published revision | RunRef 保留终态审计，archive 内容继续 baseline/上一版本并记录 `RECONCILIATION_NEEDED`，不暴露不存在的 AI 作品 |
| 作品发布后迟到 `run_failed/run_cancelled` callback | 不把 `content_status=succeeded` 或 `published_revision` 降级，仅更新 RunRef 审计和安全摘要 |
| callback 重试与 dead letter | CallbackEvent 不变，只有 outbox delivery state 变化，重放复用原事件身份和 `Idempotency-Key` |
| callback handler 尚未启用 | callback outbox 保持 pending、attempt 不变且不阻塞 run_dispatch；启用后用原 event_id/event_seq/status_version 投递 |
| Runtime 与业务库状态不一致 | outbox、对账任务或业务补偿恢复 |
| run_dispatch dead letter | 原事件重放；超限后 `failed(DISPATCH_FAILED)` |
| 删除/重生成与旧发布并发 | generation_epoch + active_run_id 拒绝旧写回 |
| purge 请求被接受、重复或物理清理失败 | 首次返回 202/requested；重复 POST 重放原接受响应且不创建第二个清理，GET 才返回当前状态；业务保持撤权并对账，Runtime 保持写屏障、重试清理并告警，直到查询为 purged |
| purge 与迟到结果并发 | privacy version 条件写拒绝私密内容复活 |
| package/authorization 撤销 | held/queued/waiting_human 直接条件终止；claimed run 写取消请求并在安全边界停止，后续动作不得继续 |
| 日记包含提示注入 | 不改变 workflow、工具、connector、统计和发布参数 |
| Runtime 失败 | 前端展示基础统计卡和重试入口 |
| 用户删除自己的回忆 | 业务后端拒绝详情访问，Runtime 不绕过 |
| 密码连续输错 5 次 | 冷却 10 分钟，服务端拒绝继续尝试；解锁凭证约 15 分钟后失效 |
| 同一用户置顶第二条回忆 | 原置顶被取消或同事务替换，列表始终最多一条置顶 |
| 非 owner 请求详情、重试、置顶或删除 | 业务后端拒绝，Runtime 和前端均不能绕过 |
| 查询 public_trace | 不含 prompt、工具原始输入输出、模型原始输出 |
| 外部观测 exporter 未配置治理策略或脱敏失败 | 保持关闭或拒绝导出，不把 Runtime 数据发送到第三方 |

## 8. 风险优先级

| 风险 | 优先级 | 处理 |
|---|---|---|
| Runtime 越权读取业务数据 | P0 | 只通过业务工具，业务后端校验权限 |
| 工具重复写入 | P0 | side effect 工具强制幂等键 |
| Scene/Action 分步写入半成品 | P0 | 单一原子发布工具 + published_revision |
| Worker 重复执行或迟到写入 | P0 | 数据库 lease/heartbeat/fencing + 稳定逻辑幂等键 |
| Runtime 早于业务映射执行 | P0 | held create，绑定 active_run_id 后显式 start |
| 调度事件丢失 | P0 | 持久 outbox、dead letter 和 DISPATCH_FAILED 终止 |
| callback 乱序导致状态倒退 | P0 | event_seq/status_version 版本比对 |
| callback 被伪造 | P0 | Runtime callback HMAC-SHA256 签名，业务后端验签 |
| Runtime 与业务库跨库不一致 | P0 | 对账任务、幂等补偿、业务兜底查询 |
| 隐私泄露 | P0 | 日志脱敏、工具摘要、public trace 分级 |
| Agent 无限循环 | P0 | PolicyEngine 硬限制 |
| 多业务形成无界队列 | P0 | AdmissionBucket 的 held/queued/running 上限与 429/Retry-After |
| 多 Worker 绕过 provider 配额或重试放大 | P0 | Redis 原子 ProviderTrafficController、permit TTL、共享 Retry-After/熔断、fail closed |
| permit 等待后的授权/租约/隐私竞态导致越权模型调用 | P0 | 可信 ModelCallContext、acquire 后二次校验、失效即释放 permit |
| 长任务阻塞 HTTP 请求 | P0 | create/start 只落库和写 outbox，Worker 异步执行 |
| 重复创建 AgentRun | P0 | 入站签名与 `Idempotency-Key` |
| 情绪伤害 | P0 | MemoirAgent guardrails 和 safety_review |
| purge 后私密内容复活 | P0 | tombstone/version、条件写、purge 后禁止恢复 |
| 运行中权限撤销不生效 | P0 | authorization version、动作前复核和业务二次鉴权 |
| 间接提示注入 | P0 | trust label、数据槽隔离、语义校验和静态 allowlist |
| AgentPackage 任意代码/同版漂移 | P0 | 受信任 CI package、不可变 digest、revoked 停止开关 |
| checkpoint 不可恢复 | P1 | 节点级 checkpoint 和 resume 测试 |
| 模型输出不稳定 | P1 | JSON repair、schema 校验、fallback |
| 上下文过大或带敏感字段 | P1 | ContextManager 做 token 预算、摘要压缩和二次脱敏 |
| 模型策略配置发散 | P1 | 第一版固定 model_policy.yaml 最小映射 |
| callback 丢失 | P1 | 重试、业务兜底查询 Runtime |
| 排队/人工等待误耗预算 | P1 | active/held/queue/approval/wall clock 独立时钟 |
| 成本失控或崩溃请求成本漏记 | P1 | 物理 model attempt 先落 running usage，实际/预留成本互斥计量，未知结果保守占用并对账 |
| 前后端契约漂移 | P1 | 本计划第 3 节契约冻结 |
| Python Runtime 偏离原 TS/AI SDK 推荐路线 | P2 | 保留 Provider Adapter、Tool 适配和 OpenMAIC 概念映射，二期复盘是否需要跨语言 SDK |
| 第一版只有 MemoirAgent 验证抽象不足 | P2 | 二期接入 CustomerSupportAgent 前预留破坏性重构窗口 |
| 解锁前列表隐私展示过多 | P2 | 回忆录业务后端确认未解锁态字段，只展示最小摘要 |

## 9. 第一版完成定义

- Runtime 服务可独立启动。
- Runtime live/ready/capabilities 契约稳定且不泄露配置。
- Runtime API 能校验业务系统签名、key、target/connector 与授权，并完成 held/start 握手；创建、启动、重试、取消、人工确认和清理操作具有分 scope、可过期换代的幂等语义。
- Runtime outbox/dispatcher/Worker 能异步调度，数据库 lease/fencing 保证单写者。
- Runtime Contract 和 package digest 有兼容性/不可变测试。
- 情侣日记后端按解绑时冻结 manifest 生成加密 Snapshot，并在 Runtime 启动前提供 revision 0 baseline。
- 情侣日记后端在 Runtime 启动、能力缓存过期或版本变化时校验 readiness、Contract major、AgentPackage 与逻辑 model policy；不兼容时保持 baseline 并等待补偿。
- Snapshot、PlaybackDocument、Scene、Action 独立版本可单向迁移，未知未来 major 不被旧服务覆盖。
- Runtime callback 具备 event_seq/status_version 乱序保护。
- Runtime dispatch/callback dead letter、跨库状态、lease 超时和独立时间预算有对账路径。
- Runtime 能安全处理 `waiting_human` 的 approve/reject，并校验 `expected_status_version` 防止迟到确认覆盖新状态。
- Runtime 能直接取消没有有效 worker 的 held/queued/waiting_human run，并通过可选 `waiting_human` callback 让业务摘要状态可达。
- `memoir_agent@1.0.0` AgentPackage 可加载。
- `POST /api/v1/agent-runs` 能创建 run。
- Runtime 能执行 MemoirAgent workflow。
- Runtime 能调用 HTTP Business Tool。
- 回忆录只通过 `memory.publish_playback_document` 原子发布完整 revision，旧 run 被 generation epoch 拒绝。
- 原素材删除能发布脱敏新 revision 或切回 baseline，并清理旧 Snapshot、作品和媒体授权。
- Runtime 能保存 run、plan、step、tool、evaluation、checkpoint、artifact、model usage。
- Runtime 对 package 生命周期、Checkpoint 解密读取、授权变化、人工确认、取消/retry、purge 和敏感调试访问生成不含正文的持久 RuntimeAuditEvent。
- Runtime 能通过 ContextManager 管理上下文预算和脱敏摘要。
- Runtime 能隔离 untrusted content 并执行确定性语义校验。
- Runtime 支持 Admission、package/authorization 撤销和 privacy purge 写屏障。
- Runtime 能 callback 情侣日记后端。
- 情侣日记后端能把 run 状态映射为回忆录生成状态。
- 情侣日记后端只有在 active run、generation epoch 和该 run 已发布 revision 同时匹配时才接受成功 callback 摘要；`content_status=succeeded` 仍只由原子发布工具提交。
- 回忆录密码设置/验证、短期解锁、列表、详情、重试、单条置顶和删除 API 可用并完成 owner 校验。
- 前端在 AI 未发布或失败时播放 baseline，在成功后播放 published revision。
- 前端校验播放契约与引用，使用私密 no-store 请求，并按 retry_after_ms 退避轮询。
- 前端 `MemoirActionRunner` 能处理播放、暂停、后台、重播、低性能与空 actions 降级，且不会残留并发定时器或自动播放音频。
- 无素材、模型失败、工具失败、callback 重复都有兜底或测试。
- 日志和 public trace 不泄露敏感内容。
