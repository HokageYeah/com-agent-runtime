# AgentRuntime 总控 Implementation Plan

> **2026-08-13 当前跨仓门禁：M3 COMPLETE / M4 GO。** 业务 bootstrap 已把 CREATE USER 密码从 mysql argv 移至 stdin；离线 bootstrap/guard `28 passed` 证明密码不进入 fake mysql argv、调用日志或 stdout/stderr，且失败补偿未回退。Runtime v1.1 fixture 跨仓门禁 `9 passed`，本仓只读合同回归 `81 passed`、Ruff/Mypy 通过。凭据边界改动后，隔离 Docker MySQL `127.0.0.1:33306` 已重跑权限负测、same fingerprint、conflicting fingerprint 三项，结果 `3 passed, 47 deselected`。M4 仅获准开始 B11、F5–F7，尚未标记完成。

> **2026-08-31 后续开发块（未实现）：** 新增公共 `bounded_loop` 静态 DAG
> 节点与 `memoir_agent@1.0.5` 五类动态生成。当前代码最新仍为 `1.0.4`；
> 不得把本计划新增 `[ ]`、新 Package、版本登记或生产切流表述为已完成。
> 设计入口：
> [通用受控循环与 Memoir 动态生成设计说明](./2026-08-31-通用受控循环与Memoir动态生成设计说明.md)。

> **2026-08-06 跨项目校准：** 本计划的 Runtime 公共能力任务仍有效；Task 6.5、Task 10.75 及本仓库现存 Archive/Snapshot/密码/播放态代码标记为“已实现的迁移证据”，目标归属改为 `couple-diary-b`，不得继续在公共 Runtime 扩展业务接口。生产闭环只保留 Runtime 公共 Run/Worker/Tool/Callback 能力，公共路径以 `/api/v1/runtime/capabilities` 与 `/api/v1/runtime/agent-runs` 为准。历史勾选项中的“revision 0 封面/基础统计”不得原样成为目标 baseline；迁移后 revision 0 按情侣日记计划收敛为无来源派生信息的通用安全版本。详细迁移与联调顺序见情侣日记仓库 `头脑风暴/docs/superpowers/回忆录/plans/2026-08-06-回忆录-总控开发计划.md`。

> **2026-08-13 历史代码闭合记录：** MySQL 运行时观测曾待显式隔离 DSN，且旧的 `2 passed, 47 deselected` 是凭据边界改动前的历史证据。修改后已重跑完整三项 `3 passed, 47 deselected`；以页首 **M3 COMPLETE / M4 GO** 为准。fixture SHA：v1.0 `04a0c12594e0ee1ca062b40842d1d4140aaad52d7f63b9a6c8dc03f9cba1b929`、v1.1 `7500539a671d13e58d688c95b78eaf8d74c06c80bc146142b64dda40907553c4`。

> **本复核对历史记录的优先级说明：** 下文保留的“M3 COMPLETE/M4 GO”“v1.0 字节级不变且同时承载九码五字段扩展”及“所有 v1 都要求显式 `details_visible_to_model`”均是历史记录，现已被本段取代，不得作为完成证据。v1.0 固定四字段 wire，`details_visible_to_model` 可省略且默认 `false`；只有经 `X-Agent-Tool-Contract-Version` 协商的 v1.1 固定五字段并要求该值为 `false`。内部 `memory.*` Tool 错误直接使用协商 ToolError JSON，不能回退为普通业务 `ret/data` 或 FastAPI `detail`；普通业务 API 响应合同不受影响。可信 context 的 `business_id` 现须匹配真实 Archive 业务 ID，旧的“只验证非空”记录已经失效。

> **2026-08-07 R1 路由门禁边界（迁移源盘点，非重做）：** Task 6.5（情侣日记归档/快照/播放文档底座）、Task 10.75（回忆录密码/列表/用户侧业务 API）以及本仓内旧前端联调相关条目对应的实现代码均判定为“仓内历史实现已完成、目标架构待迁移”——代码保留在仓库内作为迁移证据与审计回放来源，不删除、不重写。本仓 Runtime 已在 R1 落地生产配置路由门禁：`production` 环境下 FastAPI 仅注册 `/api/v1/runtime/*` provider（health / capabilities / agent-runs），不再挂载 `/api/v1/memory/*` 用户业务、`/api/v1/internal/agent-tools/memory.*` 本地工具 handler、`/api/v1/internal/agent-callbacks/memory` 业务回调 consumer，也不启用 `app.memory_runtime_launcher` legacy 启动器；`development` / `test` 仍按现状注册以便审计与跨仓联调。下方原 checkbox 状态保持不变，仅作为历史勾选证据；目标 baseline 以情侣日记仓库计划为准。

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
- 通用受控循环与 Memoir 动态生成设计：[./2026-08-31-通用受控循环与Memoir动态生成设计说明.md](./2026-08-31-通用受控循环与Memoir动态生成设计说明.md)

## 2. 核心原则

- AgentRuntime 是当前 `com-agent-runtime` 根工程内的公共运行时服务；它与 `couple-diary-b` 分属独立工程、部署与数据库，只通过版本化 HTTP Run/Tool/callback 契约协作，不复用业务 ORM、事务或迁移。
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
| `/api/v1/runtime/health/live` | GET | 进程与事件循环存活 | 必做 |
| `/api/v1/runtime/health/ready` | GET | DB schema、Registry、outbox/queue、签名配置和 draining | 必做 |
| `/api/v1/runtime/capabilities` | GET | 鉴权能力发现，不返回密钥与真实 endpoint | 必做 |
| `/api/v1/runtime/agent-runs` | POST | 创建 AgentRun | 必做 |
| `/api/v1/runtime/agent-runs/{run_id}/start` | POST | 幂等执行 held -> queued | 必做 |
| `/api/v1/runtime/agent-runs/{run_id}` | GET | 查询当前运行、调度、事件版本和 privacy 摘要 | 必做 |
| `/api/v1/runtime/agent-runs/{run_id}/steps` | GET | 查询步骤摘要 | 必做 |
| `/api/v1/runtime/agent-runs/{run_id}/retry` | POST | 从 checkpoint 重试 | 必做 |
| `/api/v1/runtime/agent-runs/{run_id}/cancel` | POST | 取消 Run | 必做 |
| `/api/v1/runtime/agent-runs/{run_id}/human-approval` | POST | 最小 approve/reject 状态迁移 | 第一版无复杂审核台 |
| `/api/v1/runtime/agent-runs/{run_id}/purge-private-data` | POST | privacy tombstone/version 与异步清理 | 必做 |

访问规则：capabilities、AgentRun 查询和 steps 查询必须校验服务身份与签名，但不要求 `Idempotency-Key`；create/start/retry/cancel/human-approval/purge 除验签外必须校验独立幂等键。`/api/v1/runtime/health/live` 与 `/api/v1/runtime/health/ready` 由部署探针访问并通过网络边界保护。

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
POST {AGENT_RUNTIME_URL}/api/v1/runtime/agent-runs
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
    "generation_epoch": 1,
    "locale": "zh-CN"
  },
  "callback_target_id": "memory_callback",
  "business_connector_id": "couple_diary_backend",
  "data_domain": "couple_memory"
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

成功响应遵循冻结 `ToolResult`：顶层固定 `{"output":{...},"schema_version":"1.0.0"}`。对 `memory.get_snapshot`，`output.schema_version` 是 Snapshot 业务 payload 的独立版本；不得用外层 Tool 合同版本替代 Snapshot schema 检查。

非 2xx 响应直接遵循协商版本的冻结 `ToolError`：v1.0 固定四字段 wire（`details_visible_to_model` 可省略并默认 `false`）；v1.1 固定五字段并要求其显式为 `false`。Runtime 按该版本 allowlist 校验 HTTP 状态/code/retryable 一致性；未知或非法错误 fail closed，不把 FastAPI `detail` 或响应原文写入状态、审计、日志或模型上下文。

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
  "public_trace": [
    {"step": "generate_scenes", "label": "生成回忆卡片", "status": "running"}
  ],
  "error": null
}
```

callback body 严格遵循 `CallbackPayload(extra=forbid)`，不包含 `current_step/progress`；这两个安全摘要只由 AgentRun query 返回，业务后端通过主动查询补偿 callback 丢失或修复进度。

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
- [✅] 确认 Runtime 模块目录为根工程 `app/`，并复用根 `alembic/` 与权威数据库方案。

**Checkpoint:** Runtime、情侣日记后端、前端都以本契约为准。

### Task 2: Runtime Python 工程骨架

**Plan:** 后端计划 Task 1。

- [✅] 在根工程 `app/` 中建立 Runtime 模块。
- [✅] 建立 Contract 包、FastAPI app、配置、日志、`/api/v1/runtime/health/live`、`/api/v1/runtime/health/ready` 和鉴权 capabilities。
- [✅] 建立追加写 AuditService；生产缺少持久、访问受限的 audit sink，或部署声明启用的 outbox event type 缺少 handler 时 readiness 返回 503。
- [✅] 配置可信业务系统、签名容忍时间、Arq Redis 队列名和 Worker 启动命令。
- [✅] 配置模型流量控制 namespace 和 permit TTL；共享控制不可用时固定 fail closed，不提供进程内无限调用开关。
- [✅] 建立测试框架和 lint/type check 命令。
- [✅] 跑 `ruff check .` 和健康检查测试。

**Checkpoint:** 根 FastAPI 应用与根 Worker 可以加载 AgentRuntime 模块。

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
- [✅] 创建 `memoir_agent@1.0.0` 文件包。**（2026-08-11 第六次最小收口 P1：1.0.0 恢复 `enqueue_media_tasks` 缺 `safe_to_rerun` 的冻结原貌——620f44a 曾给该节点补 `safe_to_rerun=False` 违反同版本 digest 不可变铁律；另发 `memoir_agent@1.0.1` 承载显式 `safe_to_rerun=False`，新 Run 路由 1.0.1，旧 Run 绑 1.0.0。两版本 digest 独立、`contract_version` 都 1.0.0）**（2026-08-19 M5 收尾追注：另发 `memoir_agent@1.0.2`——各模型节点 `guardrail_policy` 由 `private_first` 改 `redacted_only`，修复与公有 DeepSeek route（仅 `structured_output` 能力）互斥导致的静默 `capability_disabled` 全模板兜底；新 Run 路由 1.0.2，1.0.0/1.0.1 旧 Run 绑原版本，`contract_version` 仍 1.0.0。注册入口统一为 `./agent-runtime.sh register <env> --agent-id memoir_agent --version 1.0.2`）**
- [✅] 固定 `agent.yaml`、`input.schema.json`、`output.schema.json`、受信任 `workflow.graph.py`、`prompts/`、`tools.manifest.json`、`guardrails.yaml`、`callbacks.yaml`、`ui-trace.yaml` 和 `evals/`。
- [✅] 加载器校验版本、schema、workflow、prompt 引用、工具清单、guardrails、callback、ui trace 和至少 5 条最小 eval 用例。
- [✅] 冻结 `policy.waiting_human_timeout_action=fallback|failed|cancelled`；只有启用 `waiting_human` callback 的 package 才允许进入人工等待，fallback 必须指向确定性的恢复节点。
- [✅] Tool manifest 预留 `mcp_server_id/mcp_tool_name/mcp_resource_uri`，并冻结 AI SDK 等价 tool schema fixture；第一版仅验证兼容，不连接 MCP。
- [✅] Tool manifest 冻结 `connector_id/method/relative path/input_from/output_to`；完整 URL、未声明状态路径和覆盖 trusted 控制字段的映射在注册期拒绝。
- [✅] 构建不可变 package digest，排除签名文件、构建时间和 digest 自身等生成元数据；同版本不同 digest 拒绝注册，revoked 支持在途安全停止。**（2026-08-11 第六次收口 P1 实证：1.0.0 在 620f44a 被改动加 `safe_to_rerun` 违反此铁律——同版本改内容属非法；故恢复 1.0.0 缺键原貌 + 另发 1.0.1。`test_memoir_agent_1_0_0_and_1_0_1_are_independent_immutable_packages` 证明两版本 digest 不同且各自合法 load，`contract_version` 都 1.0.0；同版本改内容→必须升版本的规则被真实触发并按规则处置）**
- [✅] Package active/deprecated/revoked 变化记录操作者、原因、时间并写 RuntimeAuditEvent。
- [ ] 扩展 AgentPackage schema/loader 支持 `node_type=bounded_loop` 与冻结
  `loop_policy`；首版 `budget_profile=inherit_run_limits_v1`，按剩余
  `max_model_calls/max_tokens/max_model_cost/max_run_seconds` 和 ContextManager 公式导出
  循环/批次上限，不允许 Package 或业务请求自选数值；拒绝必要预算缺失/耗尽、未知 merge/error 策略、含 Business Tool/
  媒体/发布副作用循环体或企图放宽 Runtime 全局预算的 Package。
- [ ] 新建不可变 `memoir_agent@1.0.5` 并冻结 digest；不得覆盖
  `1.0.0`～`1.0.4`，未完成 provider/consumer 与部署登记前不得用于新 Run。

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
- [ ] 收敛写命令导出合同与 HTTP 路由：start/retry 实际解析可选 `expected_status_version` 并在提供时做条件校验，cancel/purge 实际解析必填稳定 `reason_code` 并写受控审计；human approval 继续使用必填 `decision + expected_status_version`。endpoint、Pydantic schema、OpenAPI/JSON fixture 和 provider/consumer tests 必须同源，禁止只用原始 body 做幂等 hash 后忽略合同字段。
- [✅] AgentRun 查询返回当前 `status/dispatch_state/status_version/last_event_seq/execution_attempt/privacy_state/privacy_version`、purge 时间、更新时间和安全 public trace；业务对账不得依赖 create/start/purge 的缓存响应推断当前状态。
- [✅] 创建时冻结不含密钥/endpoint 的 capability snapshot；查询额外返回由持久 `AgentStep` 推导的安全 `progress/current_step` 摘要。
- [ ] 收敛当前导出 `AgentRunQuery` 与 HTTP `RunDetail` 的重复/漂移：query 使用单一版本化 schema，`progress` 为 0..100，`current_step` 为只含 `step_id,step_name,step_type,status,execution_attempt,step_attempt,error_code` 的对象或 null；顶层与 Step 不返回自由 `error_message`。同步 endpoint、schema export、fixture 和情侣日记 consumer contract。
- [✅] `AgentRun` 仅保存 `create_idempotency_key` 作为普通审计索引，不建立跨 TTL 的永久唯一约束；维护任务清理过期记录，purge 记录至少保留至清理完成并满足审计保留期。
- [✅] 取消 `pending + held/queued` 或 `waiting_human + finished` 时在 API 事务内直接终止并创建 callback outbox；claimed run 只写取消请求，由有效 fencing worker 在安全边界终止，所有路径与 retry/approval 互斥。
- [✅] approval/cancel/retry/purge 写入脱敏且追加的 `RuntimeAuditEvent`，与 Run 状态变更在同一事务持久化。
- [✅] 人工确认只接受 `status=waiting_human AND dispatch_state=finished` 的 run，要求独立幂等键、`decision=approve|reject` 和 `expected_status_version`；approve 在同一事务只推进 `dispatch_state: finished -> queued`、递增 `status_version` 并写 dispatch outbox，run 状态由新 worker 取得 lease 后从 `waiting_human` 迁移为 `running`；reject 按 package 策略重新入队执行 fallback，或终结为 failed/cancelled，过期版本返回冲突且不推进状态。
- [✅] Retry 只允许原 caller 或内部审计身份调用，手动 run 级重试默认最多 3 次。
- [✅] `partial` 只重试 Runtime 未完成的可选步骤，不重新发布主作品；控制面仅接受“主发布已成功 + 失败节点全为声明 optional”的 checkpoint 恢复，执行器跳过已完成节点。
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
- [✅] `SIGTERM/SIGINT` 只把 Worker 切换为 draining；当前同步节点在受 lease/deadline 限制的调用窗口内完成，执行器于节点边界 heartbeat 后先落安全 Artifact/checkpoint，随即停止新模型/工具及后续节点写入、让 lease 到期，由 reaper 以新 fencing token 唯一接管。
- [✅] Worker draining 模型调用边界：运行期 guard 在 permit、usage 与 Provider HTTP 前/后复核，draining 时拒绝新的模型调用并安全释放已获资源；工具调用与 checkpoint 停写边界仍按上项推进。
- [✅] WorkflowExecutor draining 安全返回：节点开始前拒绝新工作；已完成节点先写摘要 Artifact 与 checkpoint，再以非终态结果交给 reaper 接管，不新建后台续约线程。
- [✅] 同步调用窗口：ToolGateway 与 ModelGateway 均将 HTTP timeout 限制为固定 route/tool timeout、可信 Run deadline、当前 Lease 到期时间三者的最小值；任一窗口已过期时不触网，draining 不会开启新模型或工具调用。

**Checkpoint:** 长任务脱离 HTTP 请求执行，Redis 丢失/重复不影响正确性，同一 run 只有有效 fencing token 的 Worker 可以写入。

### Task 6: Runtime Executor 与 Step 观测

**Plan:** 后端计划 Task 6、Task 10。

> 完成说明：`WorkflowExecutor` 已按静态 AgentPlan 写入安全 Step 摘要、加密 checkpoint 和最小 Artifact，并已由后续 Task 7/8/9 接入真实 Tool/Model Runner、生产密钥注入边界与 Worker 装配；恢复、迟到写入和 draining 均有 SQLite/PostgreSQL/真实 Worker 回归。**2026-08-06 复核更正：** 现有 checkpoint 密文内是完整 `AgentState`，会包含 Snapshot/tool payload 和内容中间态；加密只是保密控制，未满足不持久化边界，因此 Task 6/10 的内容最小化仍为待修正。
- [✅] Package loader 以 AST 字面量读取受信任 `workflow.graph.py` 的导出节点；StaticPlanner 复用 `WorkflowNodeDefinition` 校验后冻结到 `AgentPlan`，畸形定义拒绝创建可执行计划，不执行 Package Python。
- [✅] 执行每个节点时写 `AgentStep`。
- [✅] 每个节点完成后写加密 checkpoint。
- [✅] 通过 ArtifactStore 保存摘要、digest 和业务资源引用；临时私密 payload 受 fencing/privacy version、TTL 和 purge 联动约束。
- [✅] 支持从 checkpoint resume。
- [✅] 支持冻结 Package 的确定性 fallback 节点：正常 approve 从 checkpoint 线性继续，reject/timeout fallback 才跳转；无效目标安全失败，私密恢复目标不进入日志、Artifact 或 callback。
- [✅] `human_review` 先持久化恢复 checkpoint，再原子设置等待状态、释放 lease/Admission 并创建 callback；超时按冻结策略条件恢复或终止，审批与对账竞争均以 status/version 条件写拒绝迟到覆盖。
- [✅] 每个节点前校验 fencing、cancel、package、privacy 和 authorization version。
- [✅] 每个节点返回后在写 Artifact/checkpoint 前，复用同一 LeaseContext heartbeat 与 `LeaseService.can_write` 再次校验；fencing/privacy/authorization/cancel 失效时不写入、不启动下一节点。
- [✅] checkpoint 改为明确的安全恢复投影，禁止 `state.model_dump()` 整体落库；Snapshot/tool payload、脱敏素材、模型中间文本和播放文档即使加密也不持久化。**（2026-08-11 第四次最小收口 ✅：`executor.py` `_SAFE_CHECKPOINT_KEYS` 白名单只落路由/fallback/进度元数据，旧全量 `model_dump` 由 purge 路径清除；`runtime_test_workflow_executor.py` `test_executor_checkpoint_decrypted_blob_excludes_all_five_content_sentinels_and_playback` + legacy 拒绝 purge 测试为证）**
- [✅] resume 按当前 privacy/authorization 重取 Snapshot 并重算内容节点；已提交副作用只按稳定逻辑键 query-after-commit，旧完整状态 checkpoint 撤销/purge 后不得恢复。**（2026-08-11 第三次最终收口 ✅：`runtime_test_workflow_executor.py` 28 passed 覆盖 query-after-commit + safe_to_rerun 分类恢复 + legacy purge 跨 Session 持久化 + authorization/privacy 防复活；二次收口裁定第(2)条「维持历史状态不重审」因本次具备真实验证证据而解除）**

**Checkpoint:** mock workflow 可以完整执行，并能在失败后恢复。

### Task 6.5: 情侣日记归档、快照与播放文档底座

> **归属校准（2026-08-06）：** 下列勾选项记录已经验证过的领域模型、事务与测试能力，但这些能力需要迁移到 `couple-diary-b`；迁移完成并通过双写禁止/数据归属验收后，公共 Runtime 中对应业务路由、服务、表和迁移退出生产装配。不得把“本仓库已有实现”等同于跨项目闭环已经完成。

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
- [✅] 归档补偿使用 `(space_id, relationship_segment_no, owner_user_id)` 幂等复用首次冻结的双方 archive；manifest、payload digest、脱敏版本或冻结时间不一致时拒绝覆盖。revision 0 生成封面/统计卡和默认 `show_card` 动作，Runtime 不可用时仍可播放。
- [✅] 建立 `MemoryPlayerService` 的 `published_revision` 读取边界，以及完整 PlaybackDocument 的原子发布：先校验并落库完整 `scenes/actions/media_manifest`，再在同一事务切换唯一发布指针。
- [✅] `RelationshipArchiveService` 在和平解绑或强制拉黑事务中冻结 `space_id + relationship_segment_no + snapshot_cutoff_at + source manifest/version`，为双方创建独立 archive，并写入 Runtime 启动 outbox；Runtime 故障不回滚 baseline。
- [✅] `MemorySnapshotMaterializer` 已按真实 `couple_relationships/diary_entries/bets` 的空间、关系、段号、解绑截止时间与删除状态冻结最小素材 manifest；晚到或跨段素材不进入加密快照。Runtime readiness/capabilities 与实际 HTTP adapter 已由 Task 6.75 接入。
- [✅] `RelationshipArchiveService` 在同一业务 Session 锁定 `BOUND` 关系、写入 `UNBOUND_ARCHIVED` 与解绑操作者/原因后，立即按冻结输入创建双方隔离 archive；服务自身不提交事务，调用方可统一 commit 或 rollback。`snapshot/run outbox` 与 Runtime held-run 已由本 Task 后续条目和 Task 6.75 接入。
- [✅] 解绑归档事务为每个 owner archive 写入 Runtime 自有的 `MemoryRuntimeLaunchEvent(create_held)`；投递消费者成功后才绑定 `MemoryAgentRunRef` 并追加唯一 `start_held` 事件。create/start 均使用 `(archive_id, generation_epoch)` 派生的独立稳定幂等键；Runtime 网络失败只保留标准错误码与 pending 事件，revision 0 baseline 不回滚。
- [✅] `MemoryArchive` 保存 owner/partner 快照、关系时间、summary、content/enhancement 状态、generation epoch、active run、published revision、pin/delete 字段；`MemoryAgentRunRef` 保存运行摘要、retry、event/status version、row version、各操作幂等键和 purge 对账字段。
- [✅] 快照 materializer 只按解绑时冻结的关系段、截止时间和删除状态读取素材；`source_manifest_hash/privacy_filter_version` 与加密快照一起持久化，不读取任务执行时新增或变更的数据。
- [✅] `MemorySnapshot` 保存 snapshot/schema 版本、冻结 manifest/hash、privacy/cutoff 与认证加密的版本化 envelope；envelope 仅白名单化 `source_range/user_snapshots/diary_items/bet_items/stats`，只允许 memory service 和已绑定内部 tool 身份解密，通用后台、debug 日志和导出任务不得展开。
- [✅] `MemorySnapshot/MemoryPlaybackDocument/MemoryScene/MemoryAction` 分别持久化 schema major；数据库保留原始 snapshot version，旧无版本 Snapshot 只读单向投影为 v1，发布服务严格拒绝未知 future major。后续新增 major 时继续按同一单向迁移规则扩展。
- [✅] Scene schema 已冻结 `cover/stats/diary_highlight/bet_highlight/image/milestone/summary` 与 `normal/sensitive/fallback`；Action schema 已冻结 `show_card/focus_image/type_text/hold/play_tts/transition`，服务层和数据库均校验 order、duration、Scene 引用。MVP 媒体关闭时只接受空 `media_manifest`。
- [✅] 发布前已冻结 document schema major、Scene/Action 类型、正时长与 Scene 引用；未知 major、未知类型或无效 Action 不切换 `published_revision`。`safety_level`/media 的完整领域校验仍随该大条目后续推进。
- [✅] 每个 AI Scene 的 `source_refs_json` 已按当前已授权 Snapshot manifest 校验；`MemorySourceReference` 以 `(archive_id, document_id, revision, source_type, source_id)` 建最小反查映射，与 document/Scene/Action 和发布指针在同一事务提交，并随 superseded revision GC 清理，不保存素材正文。
- [✅] 建立 `unique(space_id, relationship_segment_no, owner_user_id)`、`unique(run_id)`、`unique(archive_id, generation_epoch)`、`unique(archive_id, revision)`，并为 Scene/Action/Media 建立指向同一 document/archive 的外键或等价约束；SQLite 与 PostgreSQL 迁移回归均覆盖代际唯一性。
- [✅] archive 创建时发布 revision 0 baseline，包含封面、基础统计和默认 Action；Runtime 不可用时仍可播放。
- [✅] 建立 `content_status/enhancement_status` 写入边界：callback 只在未发布时推进内容运行/失败态，原子发布独占内容成功与 revision，媒体创建器/worker 独占 enhancement；第一版媒体关闭统一为 `disabled`，`generation_status` 只在读取层派生。
- [✅] `MemoryPlayerService.get_published_playback()` 只读取 `published_revision` 指向的完整 document/scenes/actions/media，不拼接草稿或不同 revision；用户详情 API 已由 Task 10.75 接入。
- [✅] `MemoryMediaAsset` 持久化 `storage_key` 和 `prompt_hash`，不持久化带签名的 `access_url` 或敏感 prompt；短期访问地址只在鉴权 API 响应时生成。
- [✅] MediaAsset 已冻结 `image/audio/video`、`diary_original/ai_generated/tts/default_asset`、`ready/deleting/deleted`，并以数据库约束拒绝未知枚举；生成中和失败状态仍只属于二期 MediaTask。
- [✅] 普通 superseded revision 在新版本发布时写固定七天 `retain_until`，`MemoryRevisionGcService` 幂等清理到期非发布 document 及其 Scene/Action/Media/source-ref；日志仅记录数量。
- [✅] 隐私删除已实现立即撤权、generation epoch 递增与 Runtime purge 查询确认；该路径不使用普通 superseded revision 宽限。
- [✅] 已测试重复归档幂等、双方 archive 隔离、跨段号过滤、冻结 manifest、baseline 可播放和 revision 指针一致性。

**Checkpoint:** Runtime 开始接入前，情侣日记后端已经具备稳定快照、baseline 和原子作品版本容器。

### Task 6.75: 情侣日记后端 Runtime 能力握手与适配器

**Files:**
- Create: `app/services/memory_agent_adapter.py`
- Create: `app/services/memory_runtime_capability_cache.py`
- Test: `tests/test_memory_agent_adapter.py`
- Test: `tests/test_memory_runtime_capabilities.py`

**Interfaces:**
- Consumes: Runtime `/api/v1/runtime/health/ready`、`/api/v1/runtime/capabilities` 和 held AgentRun API。
- Produces: `MemoryAgentAdapter.start_memoir_agent(archive_id: str, snapshot_id: str, generation_epoch: int) -> MemoryAgentRunRef` 及可失效的兼容能力缓存。

- [✅] adapter 在启动、缓存过期或 Runtime 版本变化时使用服务身份检查 readiness/capabilities，校验 Contract major、`memoir_agent@1.0.0`、所需逻辑 model policy 和能力开关。
- [✅] capabilities 已改为现有 HMAC 服务身份校验；`MemoryAgentAdapter` 使用短 TTL、仅进程内的安全摘要缓存校验 readiness、Contract major、`memoir_agent@1.0.0`、`emotional_writing/strict` policy 和 `workflow_agent`，并以最小 archive/snapshot/epoch 输入创建 held Run、独立键 start。
- [✅] 能力检查不进入解绑事务同步关键路径；Runtime 不可用、draining、版本不兼容或缺少 policy 时保留 baseline，由业务 outbox/补偿任务等待恢复后重试。
- [✅] capability 缓存记录 Runtime/contract/package 摘要和过期时间；版本变化立即失效，不缓存密钥、真实 provider/connector endpoint 或租户配额。
- [✅] 创建 held run 使用稳定 create 幂等键；返回后保存 `contract_version/package_digest/authorization_version`，不支持的 major 拒绝绑定和 start。
- [✅] archive 在绑定前已删除、epoch 已变化或已有更新 active run 时取消新 run，只保留审计；start 使用独立稳定幂等键。
- [✅] `pending_start_timeout_seconds=600`；超过 10 分钟仍未绑定 run_id 时由业务补偿任务复用原 create 幂等键修复或明确标记失败，不无限等待。
- [✅] `MemoryRuntimeLaunchService.deliver_pending()` 可消费既有 pending create/start 意图；`reconcile_pending_start(now)` 对超过 600 秒的已绑定 pending-start 只重放原 start event/key，缺事件时标记对账 needed，绝不新建 Run。
- [✅] 部署配置已提供 Runtime URL、服务身份、超时与 capability TTL；`python -m app.memory_runtime_launcher` 复用根 Session 工厂单次消费 outbox/补偿，适合由 cron 或独立 worker 调度，不在 Web lifespan 内启动竞争线程。

**Checkpoint:** Runtime 故障或版本漂移不会阻断解绑归档，也不会让不兼容 AgentRun 越过 baseline 降级边界。

### Task 7: ToolGateway 与情侣日记后端工具 API 并行开发

**Plan:** Runtime 后端计划 Task 7 + 回忆录技术探索 `06-后端接口与AgentRuntime集成.md`。

Runtime 侧：

- [✅] 实现 `ToolGateway.call()`。
- [✅] 实现固定注册表驱动的 `ToolGateway.call()`：manifest 只能匹配 Runtime 内置 connector/method/path/input/side-effect 声明，运行上下文的 archive/snapshot/run/epoch 引用不可由 package 输入覆盖；输出执行 JSON 与敏感标识符扫描。
- [✅] 实现 JSON repair、摘要压缩、敏感字段扫描等少量 Native Tool，以及 LangChain Tool 基础包装；所有 adapter 最终回到 ToolGateway，不自行请求业务 connector。
- [✅] 已提供无网络 Native Tool：一次 fenced/raw JSON repair、仅键名与数量的摘要、递归敏感标识布尔扫描；不复制正文、不写日志；LangChain `StructuredTool/BaseTool` 最小包装只转换受限 schema/结果并回流 `ToolGateway`，不启用动态工具选择。
- [✅] Runtime 固定 `memory.*` HTTP Tool 已使用既有 HMAC、固定 path、10 秒超时、读取单次传输重试与写入零盲重试；写工具使用稳定幂等键并由审计/查询恢复。
- [✅] 固定 connector registry 在构造期拒绝非 HTTP(S) origin、userinfo/path/query/fragment、localhost 与非公网 IP；每次物理发送前重新解析 DNS，任一解析结果为私网/链路本地/loopback 或解析失败即 fail-closed，并拒绝所有重定向；日志不记录工具正文。
- [✅] 连接建立后校验实际对端 IP 与本次预检解析结果一致，覆盖连接期间 DNS rebinding；Worker 使用无代理、无 keep-alive 的对端跟踪 Transport，未注入真实 socket 对端读取器时 ToolGateway 在发包前 fail-closed。
- [✅] 使用不含 attempt 的稳定逻辑幂等键，并校验 trusted 参数来源；archive/snapshot/run/epoch 只从 Runtime context 读取。
- [✅] `ToolCallAuditService.begin_side_effect()` 在发送副作用前落 `running + logical_operation_key + idempotency_key + request_digest`；同一逻辑键请求摘要或幂等键漂移返回 `TOOL_CALL_OPERATION_CONFLICT`，重试物理 attempt 复用原键。
- [✅] side effect 工具先持久化并提交 `AgentToolCall.running + logical_operation_key + idempotency_key + request_digest`，再发送请求；`running/outcome_unknown/succeeded` 接管只按原逻辑键、幂等键和 digest 查询，避免重新发送写请求。
- [✅] `ToolGateway.apply_result()` 在 `LeaseService.can_write` 通过后，按受限 output schema、敏感标识扫描和 `output_to` allowlist 写入 AgentState；schema 不匹配、敏感字段或失效 fencing/privacy/authorization 均不改状态。
- [✅] `AgentState.apply_tool_output()` 仅接受冻结 `output_to` 白名单并递归拒绝 identity/authorization/connector/generation/version/fencing/run/credential 等控制字段；Package manifest 与语义校验器同步拒绝危险 target，日志不写输出正文。
- [✅] 已注册固定工具的取消语义由 Runtime registry 强制校验：`memory.get_snapshot=cancellable`、`memory.publish_playback_document=query_after_commit`；draining 时每次物理发送前拒绝新调用，后者仅允许按原幂等键查询已提交结果。
- [✅] ToolCall/AgentState 结果写入继续校验 fencing、privacy 和 authorization；迟到或不确定结果留给对账按原 key 查询。相同 key 不同 digest 的 409 按不可重试冲突终止，禁止更换 key 重放。
- [✅] 对齐冻结 `ToolRequest/ToolResult`：可信 Run/Step 构造 `input+context`，必发关联 headers，响应双层版本校验；provider/consumer fixture 与跨仓测试已同步。
- [✅] 结构化处理非 2xx `ToolError`：严格类型/形状/矩阵 fail-closed，合法码驱动安全审计、受控重试、不可重试终止、旧 generation 停止或 publish-result 对账；无正文日志测试通过。

情侣日记后端侧：

> 完成说明：`memory.publish_playback_document` 已具备 Runtime HMAC、`active_run_id + generation_epoch`、完整 document 容器、snapshot/source-ref allowlist、稳定幂等重放、`AgentToolCall` 审计和写工具接管/对账；以下完整验收条目均有 SQLite/PostgreSQL/真实 Worker 证据。

- [✅] 暴露 `POST /api/v1/internal/agent-tools/memory.get_snapshot`：校验 Runtime HMAC、archive/snapshot/run/generation 归属与 active Run 后才解密冻结快照；正文仅返回给内部 Runtime，不写日志、checkpoint 摘要或 Artifact。
- [✅] 暴露 `POST /api/v1/internal/agent-tools/memory.publish_playback_document`，单事务发布完整作品。
- [✅] 发布请求接收完整 document/scenes/actions/`media_manifest`、`run_id/snapshot_id/generation_epoch` 和稳定幂等键；MVP 媒体关闭时强制空 `media_manifest`，按冻结 manifest 校验 source refs，并返回 `revision/content_digest` 供 Runtime 固化 `publish_result`。
- [✅] 第一版只预留 `enabled=false` 的 `memory.enqueue_tts` 契约；`enqueue_media_tasks` 确定性返回 `skipped(CAPABILITY_DISABLED)`，不创建媒体任务且不触网。
- [✅] 校验 Runtime 服务身份/key、archive/owner、active_run_id、generation_epoch、冻结快照素材引用和幂等键。
- [✅] Runtime `ToolGateway` 的快照读取使用固定 connector endpoint 与 HMAC，并强制传播 `archive_id/snapshot_id/run_id/generation_epoch`；传输失败只对只读读取重试一次，写发布不盲目重试以保留原逻辑键对账语义。

**Checkpoint:** Runtime 能通过 mock 或真实情侣日记后端调用 `memory.*` 工具。

### Task 8: ModelGateway、PromptRegistry、ContextManager、评价与护栏

**Plan:** 后端计划 Task 8、Task 9。

- [✅] 实现文件化 PromptRegistry。
- [✅] PromptRegistry 校验 `prompt_id/version/owner_agent/input_schema/output_schema/model_policy/guardrail_policy/status`，不自动回退 latest；ModelGateway 可将已注册 prompt id/version 写入对应 usage attempt，且不保存模板正文。
- [✅] 使用 LangChain `ChatPromptTemplate` 将部署内可信模板与 ContextManager 脱敏数据槽渲染为短生命周期消息请求；Pydantic structured parser 和语义校验仍在结果写入前执行，usage 仅关联 prompt id/version；第一版不启用 createAgent 动态工具选择。
- [✅] 实现受信任 HTTP Provider Adapter 与 ModelGateway：Redis 共享 permit、route allowlist、fail-closed、429 共享冷却、usage 安全结算；LiteLLM 适配仍按后续 Provider 扩展处理。
- [✅] 固化第一版 `model_policy.yaml`，包含 `reasoning/balanced/emotional_writing/cheap_structured/strict/private_first` 映射。
- [✅] 每个可信 route 的部署 JSON 强制声明流控/permit/circuit、`route_config_version/pricing_config_version`、统一 cost unit/价格、能力、数据驻留及上下文/输出上限；业务输入不能覆盖这些字段。
- [✅] route 注册强制 `permit_ttl_seconds >= timeout_seconds + settle_margin_seconds`，单次 HTTP timeout 取 route、Run deadline、lease 的最小值；非法配置拒绝加载。
- [✅] Runtime 使用 `usd_per_1k_tokens`，并将 route 的 `pricing_config_version`、价格和 cost unit 冻结至每个 usage attempt；缺少部署治理字段的 route 不可用。
- [✅] 每个 policy 固定 `max_output_tokens`、能力要求和显式 fallback；ModelGateway 校验 structured output、vision、上下文长度、数据驻留与 thinking 参数，不满足时禁止任意选用默认模型。
- [✅] provider endpoint 只允许管理员在 registry 配置；校验协议、host、port、DNS/IP、内网地址和每次重定向，AgentPackage、业务请求和 prompt 均不能覆盖 endpoint/key。
- [✅] 已对 `ModelRoute` endpoint 做构造期 origin/localhost/非公网 IP 拒绝，并在每次 Provider 发送前重新解析 DNS；解析失败或任一 IP 非公网时返回安全错误、不创建 usage/permit、不发送请求，且不记录 prompt/响应正文。
- [✅] Provider HTTP 连接建立后校验实际对端 IP：每次发送前重新 DNS 预检并清空旧 socket peer，响应解析前要求真实 TCP 对端属于本轮公网解析集合；缺少 peer、地址非法或不匹配均 fail-closed，禁止重定向，且日志不记录 prompt/响应正文。
- [✅] `ModelCapabilityEvaluator` 无副作用地统一判定 Prompt policy、route capability/驻留、上下文窗口与 Redis 前置可用性；`/runtime/capabilities` 只公开 `model_enhancement_available` 与可用逻辑 policy，不泄露 provider、endpoint、route 或凭据。`private_first` 没有合规私有 provider 时在 usage/permit/HTTP 前返回 `capability_disabled`，Memoir 节点只走 YAML 显式 `template` fallback，禁止静默改用云模型。
- [✅] ContextManager 按 `extract_highlights/plan_chapters/generate_scenes` 的受控 cap 与 Prompt policy/route 输入窗口取最小预算，在节点内对素材和工具键名/数量摘要共享严格总窗口；未知节点或无效预算 fail-closed，工具 payload 不进入上下文。
- [✅] MemoirAgent 的高光、章节、场景模型节点现精确加载内置 `prompt_id@v1`，向模型只传 prompt id/version、model policy、ContextManager 的 source-ref/脱敏计数摘要与安全输入；日记正文和模板正文不进入模型请求 DTO、日志或 checkpoint。
- [✅] MemoirAgent 模型结果的字符串与 Mapping 统一经 StructuredOutputParser、一次无执行 JSON repair、Pydantic schema、SemanticValidator 和 `AgentState.apply_tool_output` 白名单；无效 JSON、未知来源引用或控制字段只触发模板 fallback。
- [✅] MemoirModelGatewayAdapter 只从权威运行中 Step、有效 Lease、Run 冻结 agent version 与部署 PromptRegistry 构造调用；请求伪造的 prompt 元数据会被覆盖，usage 仅关联 prompt id/version。
- [✅] StructuredOutputParser 统一调用 SemanticValidator：来源引用必须属于冻结 owner scope，scene/action 引用与覆盖、数量、时长、受控统计字段和 action enum 均 fail-closed；`owner_id` 等控制字段及任意 `tool_params` 一律拒绝，语义越权只触发模板 fallback。
- [✅] 实现结构化输出、一次无执行能力的 JSON repair、schema 校验。
- [✅] 实现 `ProviderTrafficController.acquire/mark_started/settle` 与 route 级连续失败熔断；Redis 原子维护共享并发、RPM/TPM、blocked_until、circuit open 与 `acquired -> started -> settled` permit，重复调用不重复增减计数；半开探测属于第一版完成定义之外的后续策略扩展。
- [✅] permit TTL 回收区分发送边界：`acquired` 过期回滚其 RPM/TPM 预留并释放并发槽，`started` 过期仅释放并发槽且保留速率预留至一分钟窗口结束；状态短暂保留以支持安全结算与重复调用。
- [✅] 主 route 429 后，Gateway 最多在可信 Run/lease 窗口内等待一次；每次重试重新创建 usage/permit 并复核发送边界。等待不可行或仍被限流时，仅可使用部署声明且 Run 快照允许的 fallback route，其拥有独立 rate-limit key/permit。
- [✅] 每次候选请求单独 acquire/finally settle；只有 acquired permit 可按 aborted_before_send 原子释放并发槽、回滚 RPM/TPM 预留，started permit 无 usage 或结果未知时保留预留到窗口过期。acquired TTL 回收时回滚未发送预留，started TTL 回收只释放并发槽；重试等待不持有 permit，上游 429 的 Retry-After 写入共享冷却，fallback route 单独取 permit。
- [✅] permit 等待受节点 timeout、剩余 active budget 和 run deadline 的最小值约束并计入 active elapsed；共享控制不可用时进入显式 provider fallback、模板 fallback 或安全失败。
- [✅] `MemoirModelGatewayAdapter` 仅从运行中的唯一 `AgentStep`、有效 `LeaseContext` 与冻结 Run 快照构造 `ModelCallContext(run/step/execution attempt/lease owner/fencing/privacy/authorization/deadline)`；prompt、业务 input、AgentState 和模型输出均不能覆盖这些字段。
- [✅] acquire 后再次校验 lease/fencing、cancel、package、privacy、authorization、route/capability 和 deadline；等待期间失效时释放 permit，不写 usage、不请求 provider。
- [✅] 实现 `ModelUsageService` 与 `AgentModelUsage` 生命周期：权威 Context、发送前/后边界校验、running/started/aborted/unknown 条件结算、实际 token 成本与 permit 回收；429/fallback 与结构化输出 one-shot repair 均创建独立物理 attempt、permit 和 usage。
- [✅] Provider 响应后复用同一 lease/privacy/authorization/deadline 边界；上下文失效时丢弃模型输出并将原 usage 保守结算为 `outcome_unknown`，不推进 run/step/checkpoint/artifact。
- [✅] 对账将过期 running/started usage 条件转为 `outcome_unknown` 并保留预留成本；PolicyEngine 按冻结 package policy 聚合模型调用次数与保守成本，在 permit 前以条件预留拒绝超限，并已覆盖 active/held/queue/approval/wall clock 与工具预算。
- [✅] `AgentModelUsage.thinking_summary_json` 仅在受控 Prompt 关联阶段写入 `thinking_enabled`、输入/输出预算和固定归一化版本；service 层严格 allowlist 拒绝 reasoning、模型原文和任意自由字段，且不进入日志、trace、callback、checkpoint 或审计。
- [✅] 实现 Evaluator、Guardrails、PolicyEngine。
- [✅] 实现 AdmissionController：AdmissionBucket 管理 global/caller/tenant/agent 的 held/queued/running；实际路由确定后由 ProviderTrafficController 管理 provider/model 流量。PolicyEngine 负责 active/held/queue/approval/wall clock 预算，不重复实现限流状态。
- [✅] 共享流量控制 Redis 在 preflight/acquire 异常时统一返回 `capability_disabled(MODEL_TRAFFIC_UNAVAILABLE)`，不触网、不保留 usage/permit，并由节点走显式模板 fallback。
- [✅] AgentModelUsage、日志、public trace、callback、checkpoint、artifact 与审计仅输出或持久化受控 ID、状态、错误码、计数、预算与版本摘要；完整 prompt、模型原文/隐藏推理、工具 payload、签名 URL、checkpoint 正文和密钥一律拒绝、投影剥离或在 privacy purge 中清除。
- [✅] RuntimeTrafficEvent 将 permit 拒绝、Retry-After、熔断开闭、Redis fail-closed 与提示/语义拒绝原子聚合到唯一分钟窗口；阈值安全告警只在首次跨越时写入无内容审计。
- [ ] 实现 `bounded_loop` Runner：静态 DAG 内按冻结输入顺序/policy 有限循环，
  每轮与每次模型发送前复核全局/节点预算、deadline、取消、Package、privacy/
  authorization、lease/fencing；物理模型调用逐次计量，不能借单一 Step 绕过治理。
- [ ] 循环中途不写 Checkpoint；每轮审计只保存 iteration、计数、usage id、耗时、
  `continue|complete|partial|failed` 和 reason code，不保存 source ref、摘要、prompt、
  候选、Scene 或播放文档。仅在整个循环节点完成边界写安全路由 Checkpoint；节点中途
  crash/resume 从同一 Snapshot、循环节点起点全量重算，发布仍 query-after-commit。

**Checkpoint:** 模型节点输出可控、可评价、可降级，成本可记录。

### Task 9: MemoirAgent MVP 工作流

**Plan:** 后端计划 Task 12。

- [✅] 实现 `load_snapshot`：仅经签名的内部工具按 archive/snapshot 读取冻结快照，Runner 不记录素材正文。
- [ ] 将 Snapshot reader 扩展到跨工程 Snapshot Tool v1：读取 `schema_version + materials[]`，material 仅接收 `material_type/source_ref/sanitized_payload`，目标类型限定为 `diary/completed_bet/handbook_note/matured_wish/bucket_list_completion`；现有 `bet_items/bets` 与 `bet:<id>` 只由显式 legacy reader 接收，并在建立可信 allowlist 前单向归一化为 `completed_bet:<id>`。同一 Snapshot 混用新旧赌约引用必须 fail-closed，新 provider 和发布文档不得输出旧格式。
- [✅] 实现 `sanitize_materials`：冻结快照只在该节点读取；下游仅获得带 `source_ref/type/sensitive` 的最小素材，普通项最多 80 字脱敏摘要，敏感项不复制文本。
- [✅] 实现 `compute_stats`：仅计算已加载快照的日记/赌局数量与是否有素材；空快照返回零值 fallback，不保存素材正文。
- [✅] 实现 `extract_highlights` 的模板高光 fallback：只保留最多 8 个稳定素材 ID，不复制正文；空素材返回空引用。
- [✅] 接入 ModelGateway 后实现模型版 `extract_highlights`，复用可信 Run/Step/Lease、冻结 prompt 引用、结构化输出与素材引用 allowlist；失败时保留模板 fallback。
- [✅] 实现 `plan_chapters` 模板章节：生成 1 个仅含章节 ID、类型和安全素材引用的基础回顾章节。
- [✅] 接入 ModelGateway 后实现模型版 `plan_chapters`，仅接受已校验的章节 ID、类型和可信素材引用；失败时保留模板 fallback。
- [✅] 实现 `generate_scenes` 模板场景：按章节生成最多 3 个 summary Scene，空输入仍保留基础场景。
- [✅] 接入 ModelGateway 后实现模型版 `generate_scenes`，仅接受已校验的 summary Scene 与可信素材引用；失败时保留模板 fallback。
- [✅] 实现规则版 `generate_actions`：每个 Scene 生成 `show_card` 与固定 3000ms 时长，不包含正文。
- [✅] MemoirAgent MVP 正常生成 3～8 张场景卡，单卡主体文案不超过 80 字；发布审核允许最多 16 张，越界、禁用情绪文案或不合法 Action 时回退三张无引用基础卡。
- [✅] 上一条仅是 `1.0.0`～`1.0.3` 的历史冻结完成事实；`1.0.4` 已取消
  Scene 总数、16 张发布门和 80 字上限，只保留至少 3 Scene，并按最终每个
  Scene 尝试配图。不得再把历史限制写成当前全局规则。
- [✅] 实现 `safety_review`：校验模板 Scene/Action 的结构、引用和时长；不合法时回退为无素材引用的基础卡片。
- [✅] 构建包含 scenes/actions/`media_manifest` 的完整 playback document，并实现 `publish_playback_document` 原子发布；媒体能力关闭时提交必填空清单。
- [✅] legacy 基线已证明发布请求以 `{"input":{...}}` 携带 `run_id/snapshot_id/generation_epoch`、snake_case 完整 `document` 和稳定逻辑幂等键，业务后端复核快照归属、active Run、epoch，并对补默认值前的原始 `document` 以 UTF-8/不 ASCII 转义/键排序/紧凑分隔符规范 JSON 复核 `content_digest`；只有成功后才允许 run 终止为 succeeded/partial。本勾选只证明发布与 digest 行为，不代表冻结 `ToolRequest/ToolResult` envelope 已完成；后者仍以 Task 7 未完成项为准。
- [✅] 模板工作流端到端回归：Worker 经 run_dispatch、lease、8 个静态节点与发布审计后将 Run 终结为 `succeeded`，且 Artifact/审计不保存日记正文。
- [✅] 第一版媒体能力保持关闭：冻结 fallback 为 `skipped(capability_disabled)`，发布文档固定携带空 `media_manifest`，不创建 MediaTask。

**Checkpoint:** `revision 0 baseline -> MemorySnapshot -> 完整 PlaybackDocument 原子发布 -> published_revision` 闭环可跑通。

### Task 10: Callback 与业务生成状态

**Plan:** 后端计划 Task 11 + 情侣日记后端 `MemoryAgentRunRef`。

Runtime 侧：

- [✅] 终态 callback 信封：`succeeded/partial/failed/cancelled` 映射为冻结事件名，事件与 callback outbox 同事务写入，并只包含 Run、业务标识、版本、状态和空的安全轨迹。
- [✅] WorkflowExecutor 在开始执行时写入 `run_started`，每个成功静态节点写入 `step_changed`；轨迹仅含节点名、状态，且与 checkpoint/artifact 同一事务提交。
- [✅] 生成 `run_started`、`step_changed`、可选 `waiting_human`、`run_succeeded`、`run_failed`、`partial_succeeded`、`run_cancelled` 事件；内部 `human_review_requested` 确定性映射为 `waiting_human`。
- [✅] 每个 callback 事件带 `event_id/event_seq/status_version`。
- [✅] 为 dispatcher 注册 callback OutboxDeliveryHandler 后才启用 `callback` 类型；此前 pending 事件不算失败，启用后继续使用原事件身份投递，callback 堆积不阻塞 run_dispatch。
- [✅] callback payload 只包含安全摘要。
- [✅] callback 请求带 `X-Agent-Runtime-Id`、`X-Agent-Key-Id`、`X-Agent-Run-Id`、`X-Agent-Business-Id`、`X-Agent-Event-Id`、`X-Agent-Event-Seq`、`X-Agent-Timestamp`、`X-Agent-Signature`、`Idempotency-Key` 并由业务后端验签；幂等键固定为 `callback:{event_id}`。
- [✅] 业务后端按原始 body bytes 计算 hash、使用恒定时间比较，并只在密钥轮换窗口接受新旧 key；签名 callback 禁止重定向。
- [✅] retry/resume 后 callback `event_seq` 继续从当前 run 最大值累加。
- [✅] callback 失败重试复用原 `event_id/event_seq/status_version/Idempotency-Key`；业务端对同事件同 body 返回成功且不重复写，对同事件不同 body 返回 409 幂等冲突。
- [✅] 状态变化、CallbackEvent 与 callback outbox 同事务提交；dispatcher 使用 lease、Retry-After、五次失败 dead letter 和原事件重放。
- [✅] callback 前复核 target 当前 authorization version，撤销后停止发送并告警。

情侣日记后端侧：

- [✅] 提供签名 `memory` callback 内部接口：校验 Runtime 身份、`run_id/business_id/event_id/event_seq` 头体一致和 `callback:{event_id}` 幂等键；同事件同 body 重放返回原结果。
- [✅] callback 投影仅接受 archive 当前 `active_run_id` 且 RunRef generation 与 archive 一致的事件；`run_succeeded/partial_succeeded` 缺少已发布 revision 时写 `reconciliation_status=needed`，不伪造成功状态。
- [✅] `MemoryAgentRunRef` 持久化 callback 的白名单 `public_trace`，只接受最多 8 条 `step/status/label` 展示字段，拒绝模型、工具和素材内容。
- [✅] 提供已验签的回忆录生成状态查询：返回 archive 内容状态、发布 revision、当前 RunRef 状态、对账标记和 `public_trace`，不返回快照、日记或播放文档。
- [✅] 新增或更新 `memory_agent_run_refs`。
- [✅] 每次 create/start/retry/purge 保存独立幂等键、`run_id/active_run_id/generation_epoch/row_version`、contract/package/authorization 摘要、event/status version，以及 purge 状态、稳定幂等键和请求/完成时间；Runtime 查询使用 `last_event_seq/status_version` 对账。
- [✅] 接收 callback 后幂等更新对应 `MemoryAgentRunRef`；只有 active run/epoch 匹配且内容尚未发布时，callback 才可推进 `content_status` 的 pending/running/waiting_human/failed/cancelled 与 `public_trace`，不得写 `published_revision/enhancement_status/succeeded`。
- [✅] `run_succeeded/partial_succeeded` 必须确认业务库已存在该 run 原子发布的 revision 且 `content_status=succeeded`；callback 只接受终态摘要，不重复写成功。缺少发布结果时保留 baseline/上一版本，记录 `RECONCILIATION_NEEDED` 并告警对账。
- [✅] 按 `event_seq/status_version` 拒绝 callback 乱序导致的状态倒退。
- [✅] 重复 callback 不重复写入。
- [✅] 为前端生成状态接口返回 `public_trace`。

**Checkpoint:** 前端通过情侣日记后端能看到生成状态变化，不需要直连 Runtime。

### Task 10.5: 补偿、对账与 SSE 定案

**Plan:** 后端计划 Task 11.5 + 回忆录技术探索 `06-后端接口与AgentRuntime集成.md`。

- [✅] Runtime 对账扫描 callback dead letter、active elapsed、wall clock、tool call、purge 与 authorization version；已完成 dispatch dead letter、lease、held/queued/waiting_human 与 package revoked 的 P0 条目。
- [✅] P0 对账器具备独立进程入口：条件修复 waiting_human fallback/终态、lease、held/queued 超时、run_dispatch 死信及 package revoked；仅成功条件写后迁移 Admission/写 Outbox，不读取私密 payload、不重放副作用。
- [✅] Runtime 对账扫描超过请求 deadline 的 `AgentModelUsage.running` 并条件标记 `outcome_unknown`；不猜测零成本、不用旧 fencing 推进执行，迟到可信计量只结算原 usage 行。
- [✅] Runtime 对账比较 AdmissionBucket 与 AgentRun.dispatch_state 的 global/caller/tenant/agent 聚合占用；漂移时按固定锁序和 bucket version 条件修复，保证计数非负并记录安全指标。
- [✅] held/queued/waiting_human 超时、package 撤销与 authorization version 变化路径复用 AdmissionService；claimed run 只写取消请求或由 lease reaper/Worker 安全接管，条件写失败不产生 Admission/Outbox 副作用。
- [✅] run_dispatch dead letter 仅对仍可终结的 queued Run 条件置 `failed(DISPATCH_FAILED)`，同事务释放 Admission；callback dead letter 保持原事件重放和业务主动查询恢复。
- [✅] 对账任务默认每 5 分钟执行，数据库 fencing lease 保证多实例互斥；连续 3 次修复失败输出安全告警计数。外部告警平台属于部署侧可选集成，不是第一版代码缺口。
- [✅] 多实例对账使用数据库 fencing lease；失租实例回滚未提交扫描副作用，接管者安全继续。
- [✅] 提供独立 reconciler 进程入口；P0 的纯规则判定、事务修复和安全报告聚合已归入 service，完整调度/lease 分层仍随原扫描范围推进。
- [✅] 每个扫描批次输出安全 `ReconciliationReport` 结构化日志和指标，固定包含扫描、修复、失败、告警计数、动作类型与标准错误码，不携带业务正文或 Runtime 私密 payload。
- [✅] 情侣日记后端保留按 `run_id` 查询 Runtime 的兜底能力，使用 `status_version/last_event_seq` 修复 callback 摘要，并以 `privacy_state/privacy_version` 确认 purge 进度。
- [✅] `MemoryAgentAdapter.get_run_state(run_id)` 已提供仅含 `status/dispatch_state/privacy_state/privacy_version/last_event_seq/status_version` 的按 Run ID 状态与 purge 兜底查询；不读取 Runtime 输入、步骤或私密正文。
- [✅] 业务使用 held create；create 失败只重试 create，绑定后 start 失败只重试 start。
- [✅] 小程序第一版轮询业务状态接口；页面隐藏、离开或终态停止，连续版本不变时退避。业务 SSE 保持可选且第一版未启用。
- [✅] 删除 archive 先撤权、递增 generation_epoch、清空 active_run_id，并在调用 Runtime 前保存 requested 状态和稳定 purge 幂等键；重复请求复用首次接受结果，只有 AgentRun 查询确认 `privacy_state=purged` 后才写本地完成时间，cancel 不能替代 purge。
- [✅] 已提供 `MemoryDeletionCompensationService`：删除事务先撤权、递增 generation、清空 active Run，并持久化每个 Run 的 `privacy_purge` 补偿意图和稳定键；重复投递只复用原键，只有 Runtime 安全查询明确为 `purged` 才写本地完成时间。
- [✅] 原日记或赌局正式删除时，通过 `source_refs_json` 的索引或等价引用映射定位受影响 archive/revision，递增 generation_epoch、取消 active run，并发布移除素材的新 revision；无法安全重写时切回 baseline。
- [✅] 已通过 `MemorySourceReference` 反查受影响当前 revision；第一版无安全重写器时原子切回 baseline、登记稳定 cancel 键，并立即清理旧 snapshot/revision/source-ref/media 记录。
- [✅] 新指针提交后按 retain window 清理旧 revision/scenes/actions/media/source refs；素材或 archive 隐私删除立即撤销详情与媒体授权、切回安全 baseline 并清理受影响私密版本，不等待普通 retain window。
- [✅] 素材删除的 baseline 指针切换后在同一事务调用 revision GC，不等待普通 retain window；archive 隐私删除后播放器因 tombstone 拒绝详情读取。
- [✅] 维护任务清理已过期的 `IdempotencyRecord`；purge scope 仅在 run 已 `purged` 且满足审计保留期后清理，避免删除重放重新触发副作用。
- [✅] `IdempotencyService` 已在 replay 与过期清理时保留未确认 `purged` 的 purge 原键；`MemoryDeletionCompensationService.run_maintenance()` 已接入 `ReconcilerRunner` 的数据库 lease/fencing 窗口，输出并汇入无内容的投递、purge 确认、旧版本 GC 与中止计数。

**Checkpoint:** Runtime 和业务库状态不一致时有补偿路径，前端生成进度不依赖 Runtime 原生 SSE。

### Task 10.75: 回忆录密码、列表与用户侧业务 API

> 进度：[✅] 用户鉴权、密码解锁、归档管理与 S3 兼容私有媒体短期签名访问已完成；实际桶凭证仅由部署环境注入，不写入仓库。

**Files:**
- Create: `app/api/endpoints/memory_api.py`
- Create: `app/models/memory_password.py`
- Create: `app/services/memory_password_service.py`
- Modify: `app/services/memory_player_service.py`
- Create: `app/schemas/memory.py`
- Test: `tests/test_memory_password_access.py`
- Test: `tests/test_memory_user_api.py`

**Plan:** 回忆录技术探索 `01-产品体验蓝图.md`、`03-数据模型与素材快照.md`、`05-播放器与前端页面.md`、`06-后端接口与AgentRuntime集成.md`。

- [✅] 已提供 `POST /api/v1/memory/password/setup` 和 `POST /api/v1/memory/password/verify`；密码限定 4～6 位数字，仅保存 scrypt 强哈希，第一版不提供找回或重置。
- [✅] 连续输错 5 次后冷却 10 分钟；验证成功签发约 15 分钟、绑定当前 JWT `jti` 的短期解锁凭证，过期后必须重新验证。
- [✅] 已提供 owner 隔离的 `GET /api/v1/memory/archives`、`GET /api/v1/memory/archives/{archive_id}` 和 `GET /api/v1/memory/archives/{archive_id}/generation`；详情仅按 `published_revision` 返回完整作品。
- [✅] 已提供 owner+解锁校验的 `POST /api/v1/memory/archives/{archive_id}/retry`、`pin`、`unpin` 和 `DELETE`；删除只登记 Task 10.5 补偿意图并立即 tombstone，网络投递仍由对账调度器处理。
- [✅] 用户 API 复用现有认证与 `build_api_response_from_request`；密码验证、详情、生成状态、写操作和媒体响应均设置 `Cache-Control: private, no-store`。
- [✅] 私有媒体访问 API 已逐次校验 owner、archive、当前 `published document`、资产 ready 状态和删除 tombstone；已接入 `boto3` 的 S3/MinIO/COS 兼容私有桶短期 `get_object` 签名 SDK，默认 60 秒、最长 300 秒，不记录 `storage_key`/URL。五项桶配置全空时 API fail-closed，半配置或生产 HTTP endpoint 拒绝启动。
- [✅] 列表按 `is_pinned DESC, unbound_at DESC` 排序，置顶操作在同一 owner 的未删除归档中先清空再置顶；解锁前只返回归档 ID、派生生成状态、解绑日期等最小字段，不返回昵称、头像、摘要或场景内容。
- [✅] 详情、生成状态、重试、置顶、取消置顶、删除和媒体访问均校验 archive owner 与同会话短期解锁凭证；跨 owner archive 统一返回不可用。
- [✅] 用户重试仅选择 `failed/partial` 且未进入 purge 的 Run，并复用持久化的 retry 幂等键；checkpoint、三次额度、Package revoked 与 partial 恢复范围仍由 Runtime 原子校验，业务侧不复制私密 checkpoint。

**Checkpoint:** 用户能够通过受密码保护的业务 API 管理自己的回忆录，Runtime 不直接承担用户鉴权或归档 CRUD。

### Task 11: uni-app 回忆录播放器接入

**Plan:** 回忆录技术探索 `05-播放器与前端页面.md`。

- [✅] 回忆录列表展示 `generation_status`。
- [✅] 增加首次设置密码、短期解锁、错误次数/冷却提示；解锁前列表只渲染后端最小字段。
- [✅] 列表支持单条置顶、取消置顶和删除确认，成功后以服务端排序和状态为准刷新。
- [✅] 回忆录详情读取 `archive/scenes/actions/media/agent_run_summary`。
- [✅] AI 尚未发布时读取 revision 0 baseline，只消费 `published_revision` 指向的完整作品。
- [✅] 加载详情后校验 document/scene/action schema major、scene/action/media 引用和 duration 上限；未知 major 停止动态 Action 并降级服务端基础静态卡，同 major 未知可选 Action 仅记录固定无内容告警码后跳过。
- [✅] 详情与生成状态请求使用 `request<T>()` 且显式 `custom.auth: true`；响应使用 `Cache-Control: private, no-store`，私有媒体 URL 不进入持久 Store、日志或分享 payload。
- [✅] 生成状态响应包含 `status_version/updated_at/retry_after_ms`；连续无变化时退避，页面隐藏、离开或终态立即停止。第一版不启用可选 SSE。
- [✅] 生成中展示安全 `public_trace`。
- [✅] 实现 `MemoirActionRunner` 状态机：`idle/loading/ready/playing/paused/ended/replay/error/low_power_ready`。
- [✅] 第一版只执行 `show_card/type_text/hold/transition`，其中 transition 仅允许 `fade/slide`；暂停、切后台、离页和手动切卡时取消当前定时器，恢复时依据状态重新调度，避免并发 Action。
- [✅] `actions` 为空时按 scenes 默认轮播；第一版媒体关闭不自动播放音频，低性能模式进入 `low_power_ready` 并使用静态卡。
- [✅] `scenes` 为空时展示安全静态空态，不执行 Action，不出现空白播放器。
- [✅] 详情请求失败展示显式重试；回忆已删除返回列表并清理 runner、轮询、场景和短期媒体 URL；图片失败使用默认占位图，音频保持静音且不请求媒体，列表无数据展示空态。
- [✅] AgentRun 失败时保留 baseline/基础作品并提供重试入口。
- [✅] 不展示 prompt、工具输入输出、模型原始输出。

**Checkpoint:** 用户只通过业务后端看到回忆作品和生成进度。

### Task 12: 端到端联调

**Plan:** 后端计划 Task 13。

- [✅] `tests/test_runtime_postgres_harness.py` 在同一临时 PostgreSQL schema 启动 API、Worker、Reconciler；dispatch 由 Worker 单轮投递，闭环不依赖宿主机服务。
- [✅] 同一测试启动仅回环监听的情侣日记业务 mock，验证签名业务工具、原子发布和 callback 投递。
- [✅] 创建 archive、baseline 和 frozen snapshot manifest，held 创建 AgentRun，绑定 active_run_id 后 start。
- [✅] Runtime 调 `memory.get_snapshot` 获取脱敏快照。
- [✅] Runtime 生成 scenes/actions 和 `media_manifest`；第一版媒体能力关闭时清单为空但字段不省略。
- [✅] Runtime 调 `memory.publish_playback_document` 原子发布完整作品并切换 `published_revision`。
- [✅] 第一版媒体节点为 skipped，播放器静音使用文本作品。
- [✅] Runtime callback 更新 `memory_agent_run_refs`。
- [✅] 前端业务 client 回环集成测试覆盖 baseline、状态轮询、`published_revision` 详情和安全场景播放；真机交互仍按 `VERIFICATION.md` 手动验收。

**Checkpoint:** 完成 `baseline -> held/start -> Runtime 执行 -> 原子发布 -> callback/轮询 -> 前端播放器` 第一版闭环。

### Task 13: 评测、观测与失败复盘

**Plan:** 后端计划 Task 13 + 回忆录技术探索 `13-观测评测与运行治理.md`。

- [✅] 建立最小评测集。
- [✅] 覆盖无日记无赌局、只有日记、只有赌局、双方同日记录、强制拉黑、模型脏 JSON、工具超时。
- [✅] 覆盖冻结 manifest、`source_refs_json` 索引定位原素材后续删除、未知 schema major、旧 revision 媒体迟到、私密缓存、轮询退避和页面后台停止；旧媒体隔离同时通过 SQLite 与真实 PostgreSQL。
- [✅] 覆盖 snapshot 旧版本只读单向迁移、未知未来 major 拒绝读取/写回、Runtime capability 缓存失效和不兼容 major 保持 baseline；快照兼容同时通过 SQLite 与真实 PostgreSQL。
- [✅] 覆盖 CallbackEvent 不可变、outbox 投递状态分离、密钥轮换、原始 body 验签和 partial retry 不重复发布。
- [✅] 覆盖旧 run callback、generation epoch 变化、成功 callback 缺少 published revision、发布后失败/取消 callback 不降级内容、密码错误冷却、解锁过期、唯一置顶和非 owner 操作。
- [✅] 统计 schema 通过率、素材引用正确率、幻觉率、情绪安全通过率、fallback 触发率、平均成本、平均耗时，以及每次 execution/model attempt 的 aborted_before_send、实际成本、预留成本和 outcome unknown 数量。
- [✅] 统计 admission/队列、outbox/dead letter、隐私 purge、授权撤销和语义失败安全聚合；`RuntimeTrafficEvent` 以分钟窗口聚合 provider 限流、Redis fail-closed、提示注入与语义拒绝，不保存业务内容。
- [✅] 检查日志不含敏感字段。
- [✅] 外部 OTel/LangSmith/调试样本 exporter 默认关闭；启用前配置数据分级、采样字段、区域/跨境、保留期、审计权限和 privacy purge 删除能力，脱敏失败时拒绝导出。
- [✅] 输出第一版失败复盘模板：仅包含 Run ID、状态、受控错误码、状态/执行/隐私版本及安全聚合指标；不读取或导出输入输出、错误原文、prompt、模型/工具载荷、Checkpoint 或密钥。

**Checkpoint:** Runtime 不只是能跑，还能被排查、评测和持续优化。

### Task 14 / R5: `bounded_loop` 与 `memoir_agent@1.0.5` 动态生成

**Plan:** [2026-08-31 专题设计](./2026-08-31-通用受控循环与Memoir动态生成设计说明.md) + 后端计划 Task 14。

- [ ] 先实现并验证公共 `bounded_loop` Package/Executor/Policy/Audit/Resume 能力；
  循环体只允许 model/deterministic，所有副作用留在循环外。
- [ ] `1.0.5` 以 `prepare_scene_batches -> generate_scene_batches(bounded_loop)
  -> finalize_scenes` 替代全局固定 refs/1～3 章裁剪；五类合格素材按类型交错扫描，
  单轮切片由模型上下文与既有通用预算计算，不作为总素材上限。
- [ ] 模型决定每批 Scene 数、主题和跨类型叙事；产品只保留至少 3 Scene，不设
  Scene/图片总数上限。确定性收尾保证每个存在的安全素材类型至少被引用一次。
- [ ] 循环结果统一为 `continue|complete|partial|failed`：全量处理并覆盖为
  `complete`；预算/批次失败但类型覆盖完整为 `partial`；缺类只允许一次经相同
  ModelGateway/预算/guardrail 的 repair，输入使用该类型安全 digest 与真实 source_ref；
  无剩余许可/预算或仍缺失则 `failed`，Runtime 不用 deterministic 模板补写 Scene。
  实际存在类型缺安全 digest 时契约 fail closed，不以无来源 fallback 冒充覆盖。
- [ ] 媒体继续在循环外逐场景串行执行；单图失败或 900 秒预算耗尽只降级文字卡，
  不改变文本内容 succeeded/partial 判定。
- [ ] 完成 Package digest、Tool wire/capabilities、部署模板、Business 显式版本及
  双仓 fixture/behavior 测试后，按“Business 接收 → Runtime 注册 → Business 切流”上线；
  旧 Run 继续使用冻结版本。

**Checkpoint:** 五类动态生成在资源、恢复、隐私和发布边界内可控；当前尚未完成。

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
| `memoir_agent@1.0.0`～`1.0.3` 场景数或单卡长度越界 | 按冻结合同回退到 3～8 张且单卡不超过 80 字 |
| `memoir_agent@1.0.4` 多场景或长正文 | 只校验至少 3 Scene、结构、引用与安全内容，不因超过 8/16 Scene 或 80 字整批回退 |
| `memoir_agent@1.0.5` 循环预算耗尽（待实现） | 类型覆盖完整则循环结果为 partial 并原子发布；缺类仅允许一次受同一 ModelGateway/预算/guardrail 治理、携带安全 digest 与真实 source_ref 的 repair；无许可/预算或仍缺失则 failed，不以 deterministic/无来源卡冒充覆盖 |
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
- `POST /api/v1/runtime/agent-runs` 能创建 run。
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
