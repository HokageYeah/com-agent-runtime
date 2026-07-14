# 15-业务 Agent 接入规范

## 一、目标

公共 Agent Runtime 要支持多个业务 Agent 接入，例如：

```text
MemoirAgent
CustomerSupportAgent
OrderAgent
StudyAgent
OperationAgent
```

接入规范需要回答四个问题：

1. 业务方如何调用 Agent。
2. Agent 如何调用业务工具。
3. Prompt、工具、参数、结果从哪里来。
4. 前端是否展示 Agent 思考与执行轨迹。

核心原则：

- 业务 Agent 以 `AgentPackage` 形式交付。
- 业务系统通过 HTTP Tool API 或 MCP Server 暴露能力。
- 所有业务统一调用 `POST /api/v1/agent-runs`。
- Runtime 不直接读取业务数据库。
- Runtime 调工具，业务系统执行工具。
- 业务结果通过工具写回业务系统，或通过 callback 通知业务系统拉取。

## 二、职责边界

| 角色 | 职责 |
|---|---|
| 业务系统 | 创建业务数据、提供工具 API、做权限和脱敏、展示结果 |
| Agent Runtime | 加载 AgentPackage、执行计划、调用工具、调用模型、记录过程 |
| 业务 Agent | 定义目标、workflow、prompt、工具清单、guardrails、evals |
| 前端 | 调业务系统接口，不直接调用 Runtime，按业务配置展示状态或轨迹 |

不要让 Runtime 直接连业务数据库，也不要让前端直接拿 Runtime 的完整 trace。

## 三、完整调用链

以回忆录为例：

```text
1. 情侣日记后端完成解绑归档
2. 情侣日记后端发布 baseline，并按解绑时冻结 manifest 生成 MemorySnapshot
3. 情侣日记后端调用 Runtime 创建 AgentRun
4. 情侣日记后端绑定 active_run_id 后调用 Runtime start
5. Runtime 加载 memoir_agent AgentPackage
6. Runtime 生成 AgentPlan
7. Runtime 执行 load_snapshot 节点
8. Runtime 通过 ToolGateway 调用 memory.get_snapshot
9. 情侣日记后端校验权限并返回脱敏 snapshot
10. Runtime 根据 prompt 调模型生成 highlights / chapters / scenes / actions
11. Runtime 执行 Evaluator 和 Guardrails
12. Runtime 调用 memory.publish_playback_document 原子发布完整作品 revision
13. Runtime callback 通知业务系统状态变更
14. 前端从情侣日记后端查询回忆录详情和生成状态
```

客服、订单等业务也是同一条主链，只是 AgentPackage 和工具不同。

## 四、AgentPackage 结构

推荐目录：

```text
agents/
  memoir_agent/
    agent.yaml
    input.schema.json
    output.schema.json
    workflow.graph.py
    prompts/
      highlight-extract.v1.md
      chapter-plan.v1.md
      scene-generate.v1.md
      safety-review.v1.md
    tools.manifest.json
    guardrails.yaml
    evals/
      minimal.jsonl
    callbacks.yaml
    ui-trace.yaml
```

说明：

| 文件 | 作用 |
|---|---|
| `agent.yaml` | Agent 元信息、运行类型、模型策略、运行限制 |
| `input.schema.json` | 创建 AgentRun 时的输入结构 |
| `output.schema.json` | AgentRun 最终输出结构 |
| `workflow.graph.py` | LangGraph 工作流定义 |
| `prompts/` | prompt 模板，由 Runtime 的 PromptRegistry 加载 |
| `tools.manifest.json` | 允许调用的工具清单 |
| `guardrails.yaml` | 公共和业务安全规则 |
| `evals/` | 最小评测集 |
| `callbacks.yaml` | Runtime 对业务系统的事件通知配置 |
| `ui-trace.yaml` | 前端是否展示执行轨迹、展示到什么粒度 |

`workflow.graph.py` 按受信任部署代码管理。第一版只加载经过 CI 构建、内容摘要校验和管理员注册的 AgentPackage，不提供普通业务方在线上传 Python workflow 的入口。

## 五、agent.yaml

示例：

```yaml
agent_id: memoir_agent
version: 1.0.0
contract_version: 1.0.0
name: 回忆录生成 Agent
runtime_type: workflow
engine: langgraph
owner: couple_memory

input_schema: ./input.schema.json
output_schema: ./output.schema.json
workflow: ./workflow.graph.py
tools_manifest: ./tools.manifest.json
guardrails: ./guardrails.yaml
callbacks: ./callbacks.yaml
ui_trace: ./ui-trace.yaml

model_policy:
  planning: reasoning
  highlight_extract: balanced
  writing: emotional_writing
  safety_review: strict

policy:
  max_steps: 16
  max_model_calls: 8
  max_tool_calls: 20
  max_run_seconds: 300
  held_ttl_seconds: 600
  queue_ttl_seconds: 900
  approval_ttl_seconds: 86400
  max_wall_clock_seconds: 172800
  max_auto_retry_per_step: 2
  max_manual_run_retry_count: 3
  max_estimated_cost: 2.0
  stop_when:
    - all_required_artifacts_created
    - safety_failed
    - cost_limit_reached
```

字段说明：

| 字段 | 来源 | 说明 |
|---|---|---|
| `agent_id` | AgentPackage 作者定义 | Runtime 注册和调用时使用 |
| `version` | AgentPackage 作者定义 | 必须显式传入，不自动用最新版 |
| `runtime_type` | AgentPackage 作者定义 | `workflow/autonomous/hybrid` |
| `owner` | 业务线 | 用于权限、成本归属、审计 |
| `model_policy` | AgentPackage 作者定义 | 只声明用途，不写密钥 |
| `policy` | Runtime 执行策略 | 防止循环、成本失控、工具滥用 |

`max_run_seconds` 只累计 worker 的活跃执行时间。`held_ttl_seconds`、`queue_ttl_seconds`、`approval_ttl_seconds` 和 `max_wall_clock_seconds` 分别约束握手、排队、人工等待和 run 最终存续时间；Runtime 可以收紧部署级上限，AgentPackage 不能放宽管理员策略。

注册约束：`package_digest` 是构建/注册元数据，不由作者写进 `agent.yaml`。构建器按排序后的 package 文件路径和内容计算摘要，排除签名文件等生成元数据；Runtime 注册表保证同一 `agent_id + version` 只对应一个 digest。Runtime 创建 run 时把 digest 固化，恢复执行不得重新解析“当前目录里的同版本文件”。

Registry 另行维护 `active/deprecated/revoked`，不允许 AgentPackage 作者在 `agent.yaml` 自行覆盖。deprecated 阻止新 create 但允许已绑定 digest 的 run 完成；revoked 代表安全撤销，阻止 create/start/retry/resume，并要求 worker 在每个 execution attempt 和安全边界重新校验。

## 六、input.schema.json

以 `MemoirAgent` 为例：

```json
{
  "type": "object",
  "required": [
    "archive_id",
    "snapshot_id",
    "owner_user_id",
    "space_id",
    "relationship_segment_no",
    "generation_epoch"
  ],
  "properties": {
    "archive_id": {"type": "string"},
    "snapshot_id": {"type": "string"},
    "owner_user_id": {"type": "string"},
    "space_id": {"type": "string"},
    "relationship_segment_no": {"type": "integer"},
    "generation_epoch": {"type": "integer", "minimum": 1},
    "locale": {"type": "string", "default": "zh-CN"}
  }
}
```

参数来源：

| 参数 | 谁生成 | 来源 |
|---|---|---|
| `archive_id` | 情侣日记后端 | 创建 `MemoryArchive` 后得到 |
| `snapshot_id` | 情侣日记后端 | 创建 `MemorySnapshot` 后得到 |
| `owner_user_id` | 情侣日记后端 | 当前回忆归属用户 |
| `space_id` | 情侣日记后端 | 当前双人空间 |
| `relationship_segment_no` | 情侣日记后端 | 当前绑定段号 |
| `generation_epoch` | 情侣日记后端 | archive 当前生成世代，用于阻止旧 run 写回 |
| `locale` | 业务后端或用户设置 | 文案语言 |

注意：这些参数只用于定位和授权，不直接把完整业务数据传给 Runtime。完整素材必须由 Runtime 调用 `memory.get_snapshot` 获取。

## 七、tools.manifest.json

示例：

```json
{
  "tools": [
    {
      "name": "memory.get_snapshot",
      "transport": "http_business_tool",
      "connector_id": "couple_diary_backend",
      "method": "POST",
      "path": "/api/v1/internal/agent-tools/memory.get_snapshot",
      "side_effect": false,
      "permission": "memory.snapshot.read",
      "input_from": {
        "archive_id": "$run.input.archive_id",
        "snapshot_id": "$run.input.snapshot_id",
        "owner_user_id": "$run.input.owner_user_id"
      },
      "output_to": "$state.snapshot"
    },
    {
      "name": "memory.publish_playback_document",
      "transport": "http_business_tool",
      "connector_id": "couple_diary_backend",
      "method": "POST",
      "path": "/api/v1/internal/agent-tools/memory.publish_playback_document",
      "side_effect": true,
      "idempotency_required": true,
      "permission": "memory.document.publish",
      "input_from": {
        "archive_id": "$run.input.archive_id",
        "run_id": "$run.run_id",
        "generation_epoch": "$run.input.generation_epoch",
        "snapshot_id": "$run.input.snapshot_id",
        "playback_document": "$state.playback_document"
      },
      "output_to": "$state.publish_result"
    }
  ]
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `name` | Runtime 内部唯一工具名 |
| `transport` | `http_business_tool/mcp/native` |
| `connector_id` | Runtime 管理员注册的业务连接器；包含固定 base URL、服务身份和出站策略 |
| `method/path` | 允许的方法与相对路径，禁止完整 URL |
| `side_effect` | 是否写业务数据 |
| `idempotency_required` | 是否必须传幂等键 |
| `permission` | Runtime 和业务后端都要校验 |
| `input_from` | 参数从 run/state/tool output 哪里取 |
| `output_to` | 工具返回写入 Runtime state 的位置 |

manifest 还要区分参数信任级别：identity、tenant、business_id、connector、permission、callback target、generation/version token 和 side effect operation key 只能来自 trusted run/manifest/deterministic state。模型或 untrusted tool/RAG content 只能填充 schema 明确允许的候选业务字段，Runtime 必须重新校验引用 ID。

## 八、通信方式选择

### 8.1 业务后端调用 Runtime

第一版统一使用普通 HTTP 创建任务：

```text
POST /api/v1/agent-runs
```

要求：

- 业务 Adapter 在部署检查或能力缓存过期后调用鉴权的 `GET /api/v1/runtime-capabilities`，确认 Contract major、AgentPackage 版本和所需逻辑 policy；不能通过能力接口获取 provider 或 connector 密钥。
- 情侣日记后端使用 mTLS 或 Runtime 分配的服务账号签名/JWT 认证，Runtime 从凭据识别 `caller_id/tenant_id`。
- Runtime 校验调用方的 Agent allowlist、business_type、callback target、connector 和配额；请求 body 里的用户 ID 只用于业务定位，不能替代服务身份。
- Runtime 保存创建时的 `authorization_version`，并在 execution attempt、模型、工具和 callback 前按当前 caller/tenant/connector/target 授权复核；撤销后返回 `AUTHORIZATION_REVOKED`，不能自动更换身份或 connector。
- 请求必须快速返回 `run_id`。
- 不等待 Agent 执行完成。
- 需要业务绑定的 Agent 使用 `start_mode=held`；业务后端保存 `run_id/active_run_id` 后再幂等调用 `/start`。
- Runtime 通过 callback 推送关键状态。
- 业务后端可用 `GET /api/v1/agent-runs/{run_id}` 兜底查询。
- 创建、start、重试、取消和 privacy purge 请求必须带 `Idempotency-Key`。
- 相同 `Idempotency-Key` 且请求体 hash 一致时返回原结果；hash 不一致时返回 HTTP `409 Conflict`，错误码 `IDEMPOTENCY_CONFLICT`。
- create/start 在事务接受前返回 429 或可重试 5xx 时不固化幂等结果，业务后端按 Retry-After 使用同一 key 重试。

### 8.2 Runtime 到业务后端

使用 HTTP callback：

```text
POST {callback_url}
```

callback 必须签名、可重试、业务侧幂等。

创建 run 时传入的 `callback_url` 必须与调用方预注册的 callback target 完全匹配；更推荐传 `callback_target_id`，由 Runtime 解析固定 URL。Runtime 禁止请求任意 callback host，签名 callback 不跟随重定向。

callback 必须带 `event_id`、`event_seq`、`status_version`。Runtime 发 callback 时必须 HMAC-SHA256 签名，业务系统必须验签。业务系统用 `event_seq` 判断事件顺序，用 Runtime `status_version` 判断状态修订；任一值倒退都只记审计，不更新业务状态。

`event_seq` 采用方案 A：在同一个 `run_id` 的完整生命周期内全局单调递增，手动 retry、checkpoint resume 或自动恢复后继续从当前最大值累加，不引入 `(attempt_no, event_seq)` 复合版本号。

### 8.3 前端实时展示

小程序首期通过业务后端状态接口轮询生成进度。H5 或已验证流式能力的平台可以由业务后端提供 SSE：

```text
GET /api/v1/{business}/agent-runs/{business_id}/stream
```

前端不直接连接 Runtime。

SSE 采用业务后端读取本地状态推送，断线回退轮询；Runtime 原生 `/events` SSE 放二期。

### 8.4 WebSocket

WebSocket 放到二期，用于：

- 客服实时聊天。
- 用户中途打断 Agent。
- 人工介入。
- 多人协作。

回忆录第一版使用状态轮询足够；SSE 只作为具备流式适配平台的体验增强。

## 九、工具是谁来调用

工具调用方是 **公共 Agent Runtime**。

业务系统不主动推送工具结果，除非是 callback 或异步媒体任务。标准链路：

```text
LangGraph 节点需要数据
  -> Runtime ToolGateway 查 tools.manifest
  -> Runtime 组装 input
  -> Runtime 给业务系统发 HTTP 请求
  -> 业务系统校验签名、权限、幂等
  -> 业务系统执行工具
  -> 业务系统返回结构化结果
  -> Runtime 写入 AgentState
```

业务系统只暴露工具，不决定 Agent 下一步怎么跑。

## 十、工具请求参数

HTTP Business Tool 标准请求：

```json
{
  "input": {
    "archive_id": "archive_123",
    "snapshot_id": "snapshot_456",
    "owner_user_id": "user_789"
  },
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

字段说明：

| 字段 | 由谁生成 | 含义 |
|---|---|---|
| `input` | Runtime 根据 `input_from` 组装 | 工具业务参数 |
| `context.agent_id` | Runtime | 当前 Agent |
| `context.agent_version` | Runtime | Agent 版本 |
| `context.run_id` | Runtime | 当前 AgentRun |
| `context.step_id` | Runtime | 当前步骤 |
| `context.business_type` | 业务方创建 run 时传入 | 业务类型 |
| `context.business_id` | 业务方创建 run 时传入 | 业务对象 ID |
| `context.trace_id` | Runtime | 串联日志和链路追踪 |

请求头必须包含：

```text
X-Agent-Runtime-Id
X-Agent-Key-Id
X-Agent-Run-Id
X-Agent-Tool-Name
X-Agent-Timestamp
X-Agent-Signature
Idempotency-Key
```

签名规则：

```text
signature_payload = method + "\n" + path + "\n" + timestamp + "\n" + body_sha256
X-Agent-Signature = hex(hmac_sha256(shared_secret, signature_payload))
```

`X-Agent-Timestamp` 默认容忍窗口为 300 秒。业务系统按 `X-Agent-Runtime-Id` 获取密钥，并校验 tool name、run id、权限和 allowlist。

生产环境必须发送 `X-Agent-Key-Id`。业务系统可在轮换窗口同时接受新旧 key id，使用恒定时间比较验签；过期密钥退出窗口后立即拒绝。共享密钥按 connector 隔离，不能在回忆录、客服和订单系统间共用。第一版签名算法固定为 HMAC-SHA256；未来新增算法时再冻结签名版本 header 和 canonical payload，不能由客户端自行声明未注册算法。

`Idempotency-Key` 建议：

```text
{run_id}:{logical_step_key}:{tool_name}:{operation_key}
```

side effect 工具必须校验幂等。

网络重试、checkpoint resume 和 worker 接管复用同一逻辑操作 key；`attempt_no` 仅用于 `X-Agent-Tool-Attempt` 和审计，不参与幂等键计算。

幂等记录要求：

- side effect 工具由业务系统保存幂等记录，建议 TTL 30 天。
- Runtime 创建、start、重试、取消 run 的幂等记录由 Runtime 保存，建议 TTL 7 天。
- 相同 key + 相同 request hash 返回原结果。
- 相同 key + 不同 request hash 返回 HTTP `409 Conflict`，错误码 `IDEMPOTENCY_CONFLICT`，不重复执行。

## 十一、Prompt 如何获取

Prompt 由 Runtime 的 PromptRegistry 加载，不由业务接口动态传入。

加载顺序：

```text
AgentPackage prompts/
  -> PromptRegistry 注册 manifest
  -> Runtime 根据 prompt_id + version 读取模板
  -> ContextManager 注入变量
  -> ModelGateway 调模型
```

Prompt 输入变量来源：

| 变量 | 来源 |
|---|---|
| `system_rules` | AgentPackage / guardrails |
| `business_rules` | guardrails.yaml |
| `sanitized_material` | `sanitize_materials` 节点输出 |
| `stats` | `compute_stats` 节点输出 |
| `highlights` | `extract_highlights` 节点输出 |
| `expected_schema` | output schema 或节点 schema |

业务系统不直接拼 prompt，避免 prompt 版本不可追踪。

## 十二、callbacks.yaml

示例：

```yaml
events:
  run_started:
    enabled: true
    include: [run_id, status, business_id, started_at]
  step_changed:
    enabled: true
    include: [run_id, current_step, progress, public_trace]
    throttle_ms: 1000
  partial_succeeded:
    enabled: true
    include: [run_id, status, artifacts_summary]
  run_succeeded:
    enabled: true
    include: [run_id, status, output_summary, artifacts_summary]
  run_failed:
    enabled: true
    include: [run_id, status, error_code, safe_message]
  waiting_human:
    enabled: false

delivery:
  mode: webhook
  default_callback_url_field: callback_url
  signing_required: true
  retry:
    max_attempts: 5
    backoff: exponential
```

事件说明：

| 事件 | 含义 | 业务处理 |
|---|---|---|
| `run_started` | Runtime 已开始执行 | 更新状态为生成中 |
| `step_changed` | 当前步骤变化 | 更新进度，可用于前端轨迹 |
| `partial_succeeded` | 主产物已发布，但 Runtime 负责的可选步骤失败 | 校验该 run 的 published revision 后派生 partial |
| `run_succeeded` | Runtime 请求范围内完成 | 校验 published revision；异步媒体仍看 enhancement_status |
| `run_failed` | 不可恢复失败 | 展示兜底和重试 |
| `waiting_human` | 需要人工确认 | 二期接审核台 |

callback 不应包含：

- 原始日记正文。
- 完整 prompt。
- 模型原始输出。
- 隐私图片 URL。
- 内部错误堆栈。

callback 乱序处理：

- Runtime 在单个 run 内生成单调递增 `event_seq`。
- retry 后 `event_seq` 不重置，继续从该 run 的最大 `event_seq` 累加。
- 业务系统分别保存 `last_event_seq` 和 `last_runtime_status_version`。
- 旧事件只记审计，不更新业务状态。
- `step_changed(running)` 不能覆盖已经处理的 `run_succeeded`、`run_failed`、`run_cancelled` 或 `partial_succeeded`。

callback dispatcher 达到重试上限后将原 outbox 行标记为 `dead_letter`。运营重放继续发送原 `event_id/event_seq/status_version`；业务系统不得因为重放时间更晚就把它当成新状态。业务 Adapter 还需保留按 run_id 主动查询和对账能力。

## 十三、ui-trace.yaml

不同 Agent 对前端轨迹展示要求不同。建议由 AgentPackage 配置：

```yaml
ui_trace:
  mode: public_summary
  expose_events:
    - run_started
    - step_changed
    - fallback_used
    - run_succeeded
    - run_failed
  hide:
    - prompt
    - raw_model_output
    - tool_input
    - tool_output
    - private_state
  labels:
    load_snapshot: "整理素材"
    extract_highlights: "寻找值得保留的片段"
    plan_chapters: "规划回忆章节"
    generate_scenes: "生成回忆卡片"
    safety_review: "检查隐私与表达"
```

模式：

| 模式 | 说明 | 适用 |
|---|---|---|
| `none` | 前端不展示轨迹 | 隐私敏感、后台任务 |
| `status_only` | 只展示生成中、成功、失败 | 回忆录 MVP |
| `public_summary` | 展示脱敏步骤文案 | 回忆录增强版、学习助手 |
| `debug_staff` | 仅内部人员可看详细轨迹 | 运营、客服质检 |
| `full_internal` | 内部审计完整 trace | 管理后台，不给普通用户 |

前端只调用业务系统接口：

```text
GET /api/v1/memory/archives/{archive_id}/generation
```

业务系统根据 `ui-trace.yaml` 和用户权限，从 Runtime 拉取或接收 callback 后返回安全轨迹。

## 十四、业务系统如何拿到结果

有两种方式。

### 14.1 工具写回

适合结构化业务产物：

```text
Runtime -> memory.publish_playback_document
  -> 业务后端校验 active_run_id + generation_epoch
  -> 单事务保存 document/scenes/actions
  -> 原子切换 published_revision
```

这是回忆录首选方式。业务后端不暴露“分别发布 Scene 和 Action”的工具，避免播放器读取半成品。

### 14.2 callback 通知后业务方拉取

适合较大的通用产物：

```text
Runtime -> callback run_succeeded
业务系统 -> GET /api/v1/agent-runs/{run_id}/artifacts
业务系统 -> 自己落业务表
```

第一版回忆录不建议用这种方式保存 scenes/actions，因为业务权限和幂等更容易在业务工具里控制。

## 十五、MemoirAgent 详细接入示例

### 15.1 业务方创建 run

```text
解绑归档完成
  -> 创建 MemoryArchive
  -> 发布 baseline revision 0
  -> 创建 MemorySnapshot
  -> 调用 Runtime
```

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
  "callback_url": "https://couple-api.example.com/api/v1/internal/agent-callbacks/memory"
}
```

### 15.2 Runtime 调工具

`load_snapshot` 节点调用：

```text
POST /api/v1/internal/agent-tools/memory.get_snapshot
```

业务后端返回：

```json
{
  "success": true,
  "data": {
    "snapshot": {
      "snapshot_id": "snapshot_456",
      "relationship": {
        "bound_at": "2025-03-14T00:00:00+08:00",
        "unbound_at": "2025-12-25T00:00:00+08:00"
      },
      "diary_items": [],
      "bet_items": [],
      "stats": {}
    }
  }
}
```

### 15.3 Runtime 获取 prompt

```text
generate_scenes 节点
  -> PromptRegistry 读取 memoir-scene-generate.v1.md
  -> 注入 sanitized_material / chapter_plan / expected_schema
  -> ModelGateway 调用模型
  -> OutputParser 校验 MemoryScene[]
```

### 15.4 Runtime 写回结果

```text
POST /api/v1/internal/agent-tools/memory.publish_playback_document
```

发布请求带完整 document/scenes/actions、`run_id`、`snapshot_id` 和 `generation_epoch`。业务后端只在 run/epoch 仍为当前值时发布，返回 `revision + content_digest`；callback 负责运行状态，不再由工具重复修改状态。

### 15.5 前端展示

前端查询情侣日记后端：

```text
GET /api/v1/memory/archives/{archive_id}
GET /api/v1/memory/archives/{archive_id}/generation
```

业务后端返回：

- `generation_status`
- `content_status / enhancement_status`
- `published_revision`
- `public_trace`，可选。
- `scenes`
- `actions`
- `media`

## 十六、CustomerSupportAgent 详细接入示例

Agent 类型：`hybrid`。

前端或客服系统调用业务后端：

```text
POST /api/v1/support/ai-assist
```

客服业务后端创建 AgentRun：

```json
{
  "agent_id": "customer_support_agent",
  "agent_version": "1.0.0",
  "business_type": "customer_support",
  "business_id": "ticket_123",
  "input": {
    "ticket_id": "ticket_123",
    "user_id": "user_789",
    "message": "我想查询订单什么时候到",
    "session_id": "support_session_456"
  },
  "callback_url": "https://support-api.example.com/api/v1/internal/agent-callbacks/support"
}
```

工具：

- `support.search_kb`
- `support.get_user_profile`
- `support.get_order_status`
- `support.create_ticket_note`
- `support.escalate_human`

轨迹展示建议：

```yaml
ui_trace:
  mode: public_summary
  labels:
    classify_intent: "识别问题类型"
    search_knowledge_base: "查找客服知识库"
    check_order_status: "查询订单状态"
    draft_answer: "整理回复"
```

高风险工具，例如退款、封号、关闭账户，必须 `human_review`。

客服 Agent 与回忆录使用同一套接入契约：创建 run 必须带 `Idempotency-Key`，callback 必须签名并使用全生命周期递增的 `event_seq`，side effect 工具必须幂等，客服侧脱敏规则不得把手机号、openid、token、地址等字段写入 trace 或 prompt。

知识库、工单正文、用户消息和搜索结果全部按 untrusted content 进入独立数据槽。内容中的工具命令、角色指令和权限声明不能改变客服 Agent 的 tool allowlist；订单号、用户 ID 和 side effect 参数必须从业务后端返回的可信结构重新解析。

## 十七、OrderAgent 详细接入示例

Agent 类型：`workflow`。

调用：

```json
{
  "agent_id": "order_agent",
  "agent_version": "1.0.0",
  "business_type": "order",
  "business_id": "order_123",
  "input": {
    "order_id": "order_123",
    "user_id": "user_789",
    "intent": "query_logistics"
  },
  "callback_url": "https://order-api.example.com/api/v1/internal/agent-callbacks/order"
}
```

工具：

- `order.get_detail`
- `order.query_logistics`
- `order.check_after_sale_policy`
- `order.create_after_sale_request`

设计要求：

- 查询类工具可自动执行。
- 售后、退款、取消订单等副作用工具必须人工确认或业务规则确认。
- 订单业务后端必须校验 `user_id` 是否有权访问 `order_id`。

## 十八、接入验收

每个 AgentPackage 接入前必须通过：

| 项目 | 要求 |
|---|---|
| schema 校验 | 输入输出 schema 完整 |
| 工具 allowlist | 只能使用 manifest 内工具 |
| 参数来源 | 每个工具参数都有 `input_from` 或明确来源 |
| 最小评测集 | 至少 5 条成功 / 失败 / 边界用例 |
| 安全规则 | 有公共和业务 guardrails |
| 成本策略 | 有 max cost / token / step |
| 回调测试 | callback 幂等、签名、重试、乱序保护 |
| 权限测试 | 越权工具调用失败 |
| 轨迹策略 | 明确 `none/status_only/public_summary/debug_staff/full_internal` |
| 降级测试 | 模型或工具失败有处理 |
| 幂等测试 | 相同 key 返回原结果，不同 request hash 返回 HTTP 409 + `IDEMPOTENCY_CONFLICT` |
| 补偿测试 | Runtime 与业务状态不一致时可恢复 |
| 契约兼容 | package、tool、callback、artifact 的 schema version 有兼容性测试 |
| 并发恢复 | worker 失联接管后旧 worker 不能写入，取消不会重复执行副作用 |
| 调度实体 | dispatch_state 只在 AgentRun；AgentDefinition 不携带单次运行状态 |
| attempt 审计 | Step、ToolCall、ModelUsage 可按 execution attempt 对账，逻辑副作用幂等键不变 |
| 时间预算 | 活跃执行、held、queued、waiting_human 和最终 deadline 独立测试 |
| 运行运维 | liveness/readiness、鉴权能力发现、共享持久化和 worker draining 通过故障测试 |
| Package 撤销 | deprecated/revoked 行为、在途取消和审计记录符合规范 |
| Privacy purge | tombstone/version 先于删除，迟到私密写入失败，purge 后无法 retry/resume |
| Outbox 恢复 | dead letter 可告警并用原事件身份重放，业务主动查询可修复摘要状态 |
| Dispatch 恢复 | run_dispatch dead letter 重放失败后明确 DISPATCH_FAILED，不永久 pending/queued |
| 不可信内容 | 日记/RAG/工具结果的 injection 反例不能改变 workflow、工具、connector 和权限 |
| 语义校验 | 未知引用、越界数值、模型生成 URL/工具参数不会进入业务副作用 |
| 授权撤销 | authorization version 变化后模型、工具、callback 动作按当前权限停止 |

## 十九、第一版与二期边界

第一版支持：

- AgentPackage 文件化注册。
- MemoirAgent。
- HTTP Business Tool。
- 静态 Workflow Agent。
- callback。
- `status_only` 或 `public_summary` 轨迹。
- 最小评测集。

二期支持：

- AgentPackage 管理后台。
- 客服 Agent。
- 订单 Agent。
- MCP Server 化。
- 动态 Agent。
- RAG。
- 人工审核界面。
- debug_staff / full_internal 轨迹后台。
