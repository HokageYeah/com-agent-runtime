# 10-Agent 工具协议与 MCP 适配

## 一、目标

工具协议要同时满足两类需求：

```text
业务内部工具
  例如 memory.get_snapshot、memory.publish_playback_document

外部生态工具
  例如知识库、搜索、文件、第三方系统、MCP Server
```

第一版先实现 HTTP Business Tool，接口设计向 MCP 对齐；二期再将可复用工具标准化为 MCP Server。

## 二、UnifiedToolDefinition

Runtime 内部统一工具定义：

```text
name
version
description
input_schema
output_schema
transport
permission
timeout_ms
retry_policy
side_effect
idempotency_required
owner
```

示例：

```json
{
  "name": "memory.get_snapshot",
  "version": "1.0.0",
  "description": "读取某段关系解绑时冻结的素材快照",
  "transport": "http_business_tool",
  "permission": "memory.snapshot.read",
  "timeout_ms": 3000,
  "side_effect": false,
  "idempotency_required": false
}
```

## 三、三类传输与两类框架适配

| 传输类型 | 用途 | 第一版 |
|---|---|---|
| Native Tool | Runtime 内部工具，如 JSON 修复、摘要 | 必做少量 |
| HTTP Business Tool | 业务系统内部工具 | 必做 |
| MCP Tool | 外部标准工具 | 二期完整接入 |

| 框架适配 | 用途 | 第一版 |
|---|---|---|
| LangChain Tool Adapter | 给 LangChain Agent 或 workflow 节点复用 | 必做 |
| AI SDK Tool Adapter | 供 TypeScript Runtime 或 OpenMAIC 集成复用 | 不做，仅保留协议兼容测试 |

框架适配器统一回到 ToolGateway 执行，不得自行请求业务服务。

## 四、HTTP Business Tool

### 4.1 调用责任

HTTP Business Tool 由公共 Agent Runtime 调用，业务系统负责执行。

```text
Runtime ToolGateway
  -> 根据 tools.manifest 组装参数
  -> 调用业务系统内部工具接口
  -> 业务系统校验签名、权限、幂等
  -> 业务系统返回结构化结果
  -> Runtime 写入 AgentState
```

业务系统不主动决定 Agent 下一步，也不把工具结果直接推给模型。

### 4.2 调用格式

业务工具统一调用：

```text
POST {BUSINESS_API}/api/v1/internal/agent-tools/{tool_name}
```

`BUSINESS_API` 由 Runtime 管理员注册的 `business_connector_id` 解析，AgentPackage 只能声明 connector 和相对 path，不能提供完整 URL、IP 或自定义 DNS。签名 HTTP Business Tool 禁止自动跟随重定向；连接前仍需执行出站 allowlist、DNS/IP 校验和私网阻断，避免工具定义形成 SSRF 通道。

请求头：

```text
X-Agent-Runtime-Id
X-Agent-Key-Id
X-Agent-Run-Id
X-Agent-Tool-Name
X-Agent-Timestamp
X-Agent-Signature
Idempotency-Key
```

签名规则第一版固定为 HMAC-SHA256：

```text
signature_payload = method + "\n" + path + "\n" + timestamp + "\n" + body_sha256
X-Agent-Signature = hex(hmac_sha256(shared_secret, signature_payload))
```

约束：

- `X-Agent-Timestamp` 默认容忍窗口为 300 秒。
- `shared_secret` 按 `X-Agent-Runtime-Id` 从环境变量或 Secret Manager 获取。
- `X-Agent-Key-Id` 支持密钥轮换；新旧 key 只在配置的轮换窗口内并存，共享密钥按 connector 隔离。
- 签名覆盖路径、请求体 hash 和时间戳，避免换路径、换工具或重放请求。
- 业务系统必须校验 `X-Agent-Tool-Name` 与 URL 中的 `{tool_name}` 一致。
- 业务系统使用恒定时间比较签名，并基于未经重新序列化的原始请求 body 计算 `body_sha256`。

请求体：

```json
{
  "input": {},
  "context": {
    "agent_id": "memoir_agent",
    "agent_version": "1.0.0",
    "business_id": "archive_123",
    "run_id": "run_abc"
  }
}
```

参数来源：

| 字段 | 来源 |
|---|---|
| `input` | Runtime 根据 `tools.manifest.json` 的 `input_from` 从 `run.input`、`state` 或上一步工具结果中组装 |
| `context.agent_id` | Runtime 当前 Agent |
| `context.agent_version` | Runtime 当前 Agent 版本 |
| `context.business_id` | 创建 AgentRun 时业务方传入 |
| `context.run_id` | Runtime 创建 |
| `context.step_id` | Runtime 当前步骤 |
| `context.trace_id` | Runtime 链路追踪 ID |

参数组装区分 trusted state 与 untrusted model/content：business_id、user/tenant scope、connector、path、permission、generation/version token 和 side effect operation key 只能来自已校验的 run、manifest 或确定性节点。模型可以生成受 schema 约束的业务候选字段，但 Runtime 必须按 allowlist 重新解析 ID；模型或工具返回中的完整 URL、工具名、权限声明和 connector_id 一律不能覆盖 manifest。

side effect 工具必须传 `Idempotency-Key`，建议格式：

```text
{run_id}:{logical_step_key}:{tool_name}:{operation_key}
```

这里的 key 标识“同一个逻辑副作用”，在 HTTP 重试、checkpoint resume、worker 接管后必须保持不变。`attempt_no` 只放在审计 header 或 ToolCall 记录中，不能进入幂等键，否则每次重试都会被业务系统视为新写入。

幂等键生命周期：

| 场景 | 存储者 | TTL | 重复请求行为 |
|---|---|---:|---|
| side effect HTTP Business Tool | 业务系统 | 30 天 | 请求体 hash 一致返回原结果，不一致返回 HTTP 409 + `IDEMPOTENCY_CONFLICT` |
| AgentRun 创建/start/重试/取消 | Runtime | 7 天 | 请求体 hash 一致返回原结果，不一致返回 HTTP 409 + `IDEMPOTENCY_CONFLICT` |
| Runtime callback | 业务系统 | 30 天 | `event_id` 或 `event_seq` 已处理则直接返回成功 |

幂等冲突统一返回 HTTP `409 Conflict`，错误码为 `IDEMPOTENCY_CONFLICT`，响应体沿用业务系统或 Runtime 的标准错误结构。

幂等响应缓存只覆盖已接受或已产生副作用的操作。执行前 429、连接失败和可重试 5xx 不完成该 key，网络恢复后继续用同一 key；否则瞬时过载会被错误固化到整个 TTL。

响应：

```json
{
  "success": true,
  "data": {},
  "error": null,
  "meta": {
    "tool_name": "memory.get_snapshot",
    "duration_ms": 120
  }
}
```

### 4.3 参数映射

工具定义必须写清参数来源：

```json
{
  "name": "memory.get_snapshot",
  "input_from": {
    "archive_id": "$run.input.archive_id",
    "snapshot_id": "$run.input.snapshot_id",
    "owner_user_id": "$run.input.owner_user_id"
  },
  "output_to": "$state.snapshot"
}
```

这能避免工具参数靠 prompt 猜测，也方便 Runtime 做 schema 校验和审计。

## 五、LangChain Tool 适配

内部工具可包装成 LangChain `tool()`：

```text
UnifiedToolDefinition
  -> LangChain tool()
  -> createAgent 或 workflow 节点使用
```

约束：

- schema 使用 Pydantic / JSON Schema。若后续有 TypeScript Agent 适配层，可再映射为 Zod。
- description 不允许包含隐藏指令。
- side effect 工具必须经过 allowlist。
- 工具返回结果进入 ContextManager 压缩后再给模型。

## 六、AI SDK Tool 适配

为兼容 OpenMAIC 和 AI SDK：

```text
UnifiedToolDefinition
  -> AI SDK tool()
  -> ToolLoopAgent / streamText tools
```

适用：

- 动态客服 Agent。
- 需要流式工具调用事件的对话场景。

回忆录第一版主要使用 LangGraph 节点工具，不依赖 AI SDK tool loop。

## 七、MCP 适配

MCP 用于外部工具、资源和 prompt 的标准接入。

### 7.1 第一版策略

第一版不强制把情侣日记业务工具做成 MCP Server，原因：

- 回忆录只需要少量内部工具。
- 业务权限和脱敏需要后端强控制。
- HTTP Tool API 更快落地。

但工具定义要预留 MCP 字段：

```text
transport: http_business_tool | mcp | native
mcp_server_id
mcp_tool_name
mcp_resource_uri
```

### 7.2 二期策略

二期可做：

```text
couple-memory-mcp-server
  tools:
    memory.get_snapshot
    memory.publish_playback_document
  resources:
    memory://archives/{archive_id}/snapshot
  prompts:
    memoir_safety_rules
```

注意：即使做成 MCP，也必须保留服务身份、用户权限、工具 allowlist 和敏感字段过滤。

## 八、工具安全

工具 request/result envelope 必须带 `schema_version`；未知 major version 直接拒绝。工具定义还应声明 `cancellation_behavior`（可中止、不可中止、已提交后仅查询结果），便于 Runtime 取消 run 时正确处理副作用。

工具安全是 P0。

必须做：

- 工具 allowlist。
- 服务间签名。
- 输入 schema 校验。
- 输出 schema 校验。
- side effect 工具幂等。
- 超时和重试限制。
- 输出敏感字段扫描。
- prompt injection 检查。
- 工具描述签名或版本锁定。
- 幂等冲突审计。
- side effect 工具重复请求返回原结果，不重复写入。
- 出站 connector 注册表、协议/host/port allowlist、DNS 解析结果校验；签名工具请求禁止跟随重定向。

prompt injection 检查只产生风险信号。真正的执行边界来自静态 tool allowlist、trusted 参数来源、每次调用的当前授权校验、业务后端二次鉴权和 side effect 幂等。即使分类器漏报，日记、RAG、Web Search 或工具结果中的文字也不能新增工具、改变 connector、提升权限或跳过审批。

Runtime 在每次工具调用前比较 AgentRun 保存的 `authorization_version` 与当前 caller/tenant/connector 授权版本。版本变化后重新授权；权限被撤销时返回 `AUTHORIZATION_REVOKED`，停止该 run 后续副作用，不自动换 connector 或凭据。业务后端仍按请求时状态判断用户和业务对象权限。

禁止：

- Runtime 直接连接业务数据库。
- Agent 自由拼接业务 SQL。
- 未授权工具进入模型上下文。
- 把工具错误堆栈原样返回给模型。
- 允许 AgentPackage 或模型拼接完整工具 URL、覆盖 connector 密钥或跟随到未授权 host。

## 九、回忆录工具清单

第一版必做：

| 工具 | side effect | 说明 |
|---|---|---|
| `memory.get_snapshot` | 否 | 读取脱敏素材快照 |
| `memory.publish_playback_document` | 是 | 原子校验并发布 document/scenes/actions，切换 published_revision |

第一版可选：

| 工具 | side effect | 说明 |
|---|---|---|
| `memory.enqueue_tts` | 是 | 创建 TTS 任务 |
| `memory.save_safety_report` | 是 | 保存安全审核报告 |

二期：

| 工具 | 说明 |
|---|---|
| `memory.enqueue_cover_generation` | AI 封面 |
| `memory.enqueue_video_storyboard` | 视频或 H5 分镜 |
| `memory.regenerate_chapter` | 章节重生成 |
| `memory.create_share_page` | 分享页 |

## 十、工具错误语义

工具错误要结构化：

```json
{
  "error_code": "SNAPSHOT_NOT_FOUND",
  "error_type": "business_not_found",
  "retryable": false,
  "safe_message": "素材快照不存在",
  "details_visible_to_model": false
}
```

Runtime 根据 `retryable` 决定重试或降级。

业务写工具还可以返回 `GENERATION_SUPERSEDED`：archive 已删除、`generation_epoch` 已变化或 run 不再 active。该错误不可重试，Runtime 应取消剩余副作用并结束旧 run。公共 Runtime 的 worker fencing 防止旧 worker 写 Runtime 库，业务 `generation_epoch` 防止仍在运行的旧任务写业务库，两层不可互相替代。

Runtime 自身可以在调用前返回 `AUTHORIZATION_REVOKED`：caller、tenant、connector、callback target 或数据域授权版本已经失效。该错误不可重试；管理员恢复授权后也必须显式创建新 run，避免旧上下文继续执行。
