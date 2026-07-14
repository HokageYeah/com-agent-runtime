# 06-后端接口与 Agent Runtime 集成

## 一、定位

情侣日记后端不直接实现通用 Agent loop，也不直接管理模型 provider。它负责回忆录业务数据和业务工具，公共 Agent Runtime 负责执行 `MemoirAgent`。

```text
情侣日记 FastAPI
  -> 创建 archive / snapshot
  -> 调用 Agent Runtime 创建 AgentRun
  -> 暴露 memory.* 内部工具
  -> 接收 Runtime callback
  -> 为 uni-app 提供回忆录播放器接口
```

## 二、后端模块建议

```text
app/api/endpoints/memory_api.py
app/api/endpoints/internal_agent_tools.py
app/api/endpoints/internal_agent_callbacks.py

app/services/memory_archive_service.py
app/services/memory_snapshot_service.py
app/services/memory_agent_adapter.py
app/services/memory_privacy_service.py
app/services/memory_player_service.py

app/schemas/memory.py
app/schemas/memory_agent_tools.py

app/models/memory_archive.py
app/models/memory_snapshot.py
app/models/memory_scene.py
app/models/memory_action.py
app/models/memory_media_asset.py
app/models/memory_agent_run_ref.py
```

避免新增回忆录专属生成服务承担通用 AI 能力。生成逻辑应沉到公共 Runtime 的 `MemoirAgent`。

## 三、解绑触发流程

### 3.1 和平解绑

```text
对方同意和平解绑
  -> 关系状态写为 UNBOUND_ARCHIVED
  -> 记录 unbound_at
  -> MemoryArchiveService.create_archives_for_relationship()
```

### 3.2 强制拉黑

```text
用户确认拉黑
  -> 当前关系关闭
  -> 待处理解绑申请关闭
  -> 记录冷却
  -> MemoryArchiveService.create_archives_for_relationship()
```

两种方式都触发归档，但 `unbound_reason` 只作为业务元信息，不允许 Agent 据此评价关系。

## 四、创建归档与 AgentRun

```text
create_archives_for_relationship(relationship_id)
  -> 在解绑事务中冻结 relationship segment + cutoff + source manifest
  -> 为双方各创建 MemoryArchive(generation_epoch=1)
  -> 为双方发布 baseline revision 0
  -> 写入 snapshot/run outbox
  -> 异步物化 MemorySnapshot
  -> outbox worker 调用 MemoryAgentAdapter.start_memoir_agent()
```

建议采用“先业务落库，再绑定 Runtime”的补偿友好流程：

```text
create MemoryArchive / baseline / frozen manifest / outbox
  -> 异步、幂等创建 MemorySnapshot
  -> 保存 MemoryAgentRunRef(status=pending_start, create/start idempotency keys)
  -> 调用 Runtime 创建 held AgentRun
  -> 业务事务写入 run_id、active_run_id 和 status=pending
  -> 幂等调用 Runtime /start，允许入队
  -> 失败时保留 pending_start，由业务补偿任务使用原始 `Idempotency-Key` 重试或标记 failed
```

`memory_agent_adapter` 在启动、能力缓存过期或 Runtime 版本变化时，使用服务身份调用 `/health/ready` 和 `/api/v1/runtime-capabilities`，校验 Runtime Contract major、`memoir_agent` 版本及所需逻辑 model policy。该检查不放进每次解绑事务的同步关键路径；检查失败时 archive 的 baseline 继续可播放，补偿任务等待 Runtime 恢复后创建 run。能力响应不得包含 provider/connector 密钥、真实 base URL 或租户配额。

`MemoryAgentAdapter.start_memoir_agent()`：

```text
POST {AGENT_RUNTIME_URL}/api/v1/agent-runs
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
    "generation_epoch": 1
  },
  "callback_url": "https://couple-api.example.com/api/v1/internal/agent-callbacks/memory"
}
```

返回：

```json
{
  "run_id": "run_abc",
  "status": "pending",
  "contract_version": "1.0.0",
  "package_digest": "sha256:...",
  "authorization_version": 12
}
```

通信要求：

- 创建 AgentRun 使用普通 HTTP 短请求。
- 不在创建请求中等待生成完成；held run 在显式 start 前不得执行。
- 后端先在本地事务保存 `run_id` 到 `memory_agent_run_refs` 并绑定 archive.active_run_id，再调用 `POST /api/v1/agent-runs/{run_id}/start`。
- Runtime 后续通过 callback 通知状态。
- 业务后端保留查询 Runtime 状态的兜底能力。
- 创建请求必须带 `Idempotency-Key`。相同 key 且请求体 hash 一致时复用原 run；相同 key 但请求体 hash 不一致时返回 HTTP `409 Conflict` 和错误码 `IDEMPOTENCY_CONFLICT`。
- 幂等冲突统一返回 HTTP `409 Conflict`，错误码为 `IDEMPOTENCY_CONFLICT`，错误响应仍使用业务统一响应结构。
- 重试创建不重建 snapshot，除非用户或后台明确触发重新归档。
- 业务后端保存 Runtime 返回的 `contract_version`、AgentPackage `package_digest` 和 `authorization_version` 摘要，便于问题追溯；不支持的契约 major version 必须拒绝，不做静默兼容。
- `callback_url` 必须预注册或由 `callback_target_id` 解析，Runtime 不接受任意 host；业务工具也通过预注册 `business_connector_id` 访问情侣日记后端。
- archive 保存 `active_run_id + generation_epoch`。Runtime 创建成功后以条件更新绑定；如果 archive 已删除、epoch 已变化或已有更新 run，当前 run 立即取消并只保留审计。
- `/start` 失败时补偿任务用独立稳定 Idempotency-Key 重试 start，不重新创建 run。held run 超时由 Runtime 取消，业务对账后决定重新创建。
- Runtime queued、held 或 waiting_human 的等待时间不消耗 `max_run_seconds` 活跃执行预算；业务侧根据各自 TTL 展示等待状态并决定补偿，不能用 archive 创建时间替代 Runtime 的时钟语义。

## 五、业务工具 API

公共 Runtime 通过内部工具访问业务能力。建议统一路由：

```text
POST /api/v1/internal/agent-tools/{tool_name}
```

所有内部工具必须校验：

- Runtime 服务身份。
- tool allowlist。
- archive 与 owner 权限。
- 幂等键。
- 请求签名和过期时间。
- 当前 Runtime caller/tenant、run 与业务 connector 授权；不能只信创建 run 时的授权快照。

服务间签名第一版固定为 HMAC-SHA256：

```text
signature_payload = method + "\n" + path + "\n" + timestamp + "\n" + body_sha256
X-Agent-Signature = hex(hmac_sha256(shared_secret, signature_payload))
```

要求：

- `X-Agent-Timestamp` 默认容忍窗口为 300 秒。
- 密钥通过环境变量或 Secret Manager 下发，不写入 AgentPackage。
- 请求携带 `X-Agent-Key-Id`，业务后端按轮换窗口选择密钥并使用恒定时间比较；不同 connector 不共用 shared secret。
- 业务后端按 `X-Agent-Runtime-Id` 找到对应密钥和 allowlist。
- 签名失败、过期、tool name 不匹配或 callback host 不在 allowlist 时拒绝请求。
- 业务后端按请求当下状态重新校验 archive owner、generation epoch 和工具权限。Runtime 传来的 authorization version 只用于审计，不能替代业务权限查询。

### 5.1 memory.get_snapshot

```text
POST /api/v1/internal/agent-tools/memory.get_snapshot
```

输入：

```json
{
  "archive_id": "archive_123",
  "snapshot_id": "snapshot_456",
  "owner_user_id": "user_789"
}
```

输出：

```json
{
  "snapshot": {
    "snapshot_id": "snapshot_456",
    "relationship": {},
    "diary_items": [],
    "bet_items": [],
    "media_refs": [],
    "permissions": {}
  }
}
```

要求：

- 不返回已删除日记。
- 不返回当前用户无权查看素材。
- 不返回跨 `relationship_segment_no` 数据。
- 对敏感字段做业务侧脱敏。

### 5.2 memory.publish_playback_document

```text
POST /api/v1/internal/agent-tools/memory.publish_playback_document
```

要求：

- 请求携带完整 `MemoryPlaybackDocument + scenes + actions + media_manifest`，使用逻辑发布键幂等。
- 校验 `run_id == active_run_id`、`generation_epoch`、snapshot、document schema、scene/action 引用、素材引用、数量和内容 hash。
- 在单个业务数据库事务中写入新 revision 并更新 `published_revision`；任一步失败都不改变播放器当前版本。
- 每个 scene 的素材引用必须属于当前 snapshot。
- archive 已删除或 epoch/run 不匹配时返回不可重试的 `GENERATION_SUPERSEDED`。
- 相同幂等键和 request hash 返回首次 revision；hash 不同返回 `IDEMPOTENCY_CONFLICT`。

### 5.3 memory.enqueue_tts

创建 TTS 任务。请求必须带 `document_id/revision + generation_epoch + run_id`，业务后端按稳定幂等键创建 MemoryMediaTask。旧 document/epoch 的任务可以被取消或完成后清理，但不得更新当前 archive 的 `enhancement_status`。TTS 失败不影响已发布 document/scenes/actions。

Runtime 不通过业务工具写运行状态。Runtime callback 是 `MemoryAgentRunRef` 状态的唯一外部来源；发布工具只拥有作品 revision，媒体 worker 只拥有 `enhancement_status`。

## 六、Runtime Callback

```text
POST /api/v1/internal/agent-callbacks/memory
```

Runtime 在关键状态变化时回调，callback payload 以 6.1 的完整事件结构为准。

情侣日记后端更新对应 `memory_agent_run_refs`。只有 callback 的 `run_id/epoch` 仍匹配 archive 当前 `active_run_id/generation_epoch` 时，才更新 archive 的 `content_status/public_trace`；`generation_status` 由内容和增强状态派生。

### 6.1 Callback 事件

建议支持：

| 事件 | 业务处理 |
|---|---|
| `run_started` | active run/epoch 匹配时把 content_status 标记为 running |
| `step_changed` | 更新当前步骤和公开进度 |
| `partial_succeeded` | 更新 run ref；确认该 run 已发布 revision 后派生 partial |
| `run_succeeded` | 更新 run ref；确认该 run 已发布 revision 后显示 succeeded |
| `run_failed` | active 且未发布新 revision 时标记 content failed，继续展示旧 revision/baseline |
| `run_cancelled` | active run/epoch 匹配时标记 cancelled |

callback 内容只允许包含安全摘要：

```json
{
  "event_id": "evt_001",
  "event_seq": 12,
  "event": "step_changed",
  "run_id": "run_abc",
  "business_id": "archive_123",
  "status": "running",
  "status_version": 8,
  "current_step": "generate_scenes",
  "progress": 60,
  "public_trace": [
    {"label": "生成回忆卡片", "status": "running"}
  ],
  "error": null
}
```

禁止在 callback 中包含日记原文、完整 prompt、模型原始输出、工具原始输入输出。

### 6.1.1 Callback 签名校验

Runtime 发往业务后端的 callback 必须签名。请求头：

```text
X-Agent-Runtime-Id
X-Agent-Key-Id
X-Agent-Run-Id
X-Agent-Business-Id
X-Agent-Event-Id
X-Agent-Event-Seq
X-Agent-Timestamp
X-Agent-Signature
Idempotency-Key
```

签名算法复用内部工具调用：

```text
signature_payload = method + "\n" + path + "\n" + timestamp + "\n" + body_sha256
X-Agent-Signature = hex(hmac_sha256(shared_secret, signature_payload))
```

业务后端必须校验：

- `X-Agent-Timestamp` 默认 300 秒窗口。
- `X-Agent-Runtime-Id` 对应的密钥、允许的 callback path 和业务类型。
- `X-Agent-Run-Id` 与 body 中 `run_id` 一致。
- `X-Agent-Business-Id` 与 body 中 `business_id` 一致，并且与本地 `MemoryAgentRunRef.archive_id` 一致。
- `X-Agent-Event-Id` 与 body 中 `event_id` 一致。
- `X-Agent-Event-Seq` 与 body 中 `event_seq` 一致。

验签失败返回 401，时间戳过期返回 401，事件幂等冲突返回 409。

### 6.2 Callback 失败处理

Runtime callback 必须可重试：

```text
max_attempts: 5
backoff: exponential
```

达到重试次数后，Runtime 将原 CallbackEvent 标为 `dead_letter` 并告警。对账或人工重放继续使用原 `event_id/event_seq/status_version`，不能生成新事件伪装成更晚状态；业务后端还要通过 Runtime 查询接口修复本地摘要状态。

业务后端 callback 接口必须幂等。重复收到同一 `run_id + event_id` 或 `run_id + event_seq` 不应重复写入业务数据。

乱序保护：

- Runtime 为单个 run 生成单调递增的 `event_seq`。
- `event_seq` 在同一个 `run_id` 的完整生命周期内全局单调递增；retry 后继续从当前最大值累加，不重置为 1。
- 业务后端分别保存 `last_event_seq` 和 `last_runtime_status_version`，本地 `row_version` 只用于乐观锁。
- callback 到达时，如果 `event_seq <= last_event_seq`，或 Runtime `status_version < last_runtime_status_version`，只记录审计，不更新业务状态。
- `succeeded/partial/failed/cancelled` 是终态或准终态，旧的 `step_changed(running)` 不能覆盖它们。

## 七、用户侧 API

所有 `/api/v1/...` 接口在路由层显式使用统一响应构造，遵循后端 README 中的 `build_api_response_from_request()` 规范。

### 7.1 密码

```text
POST /api/v1/memory/password/setup
POST /api/v1/memory/password/verify
```

### 7.2 列表

```text
GET /api/v1/memory/archives
```

排序：

```text
is_pinned desc, unbound_at desc
```

### 7.3 详情

```text
GET /api/v1/memory/archives/{archive_id}
```

返回：

- archive
- `published_revision` 对应的完整 playback document
- generation_status
- content_status / enhancement_status
- agent_run_summary

无权、已删除、未解锁时拒绝访问。

详情和生成状态响应设置 `Cache-Control: private, no-store`。媒体访问校验 owner、archive、document revision 和删除状态；普通 superseded revision 只在配置的播放宽限期内允许已授权访问，隐私删除立即撤权。

### 7.4 生成状态

```text
GET /api/v1/memory/archives/{archive_id}/generation
```

可返回 Runtime 的简要状态和可公开轨迹，不暴露 prompt 和敏感中间数据。

示例：

```json
{
  "archive_id": "archive_123",
  "generation_status": "running",
  "progress": 60,
  "current_step": "generate_scenes",
  "public_trace": [
    {"step": "load_snapshot", "label": "整理素材", "status": "succeeded"},
    {"step": "generate_scenes", "label": "生成回忆卡片", "status": "running"}
  ]
}
```

### 7.4.1 状态轮询与可选 SSE

小程序 MVP 轮询 7.4 状态接口。响应增加 `status_version/updated_at/retry_after_ms`，前端按建议间隔和退避策略查询；终态或页面进入后台后停止。

H5 或已经验证流式请求能力的平台可由情侣日记后端提供 SSE，前端仍不直连 Runtime：

```text
GET /api/v1/memory/archives/{archive_id}/generation/stream
```

SSE 后端实现方式可选：

```text
第一版方式 A：读取本地 memory_agent_run_refs / generation_status，向前端推送
二期方式 B：后端订阅 Runtime events，再过滤为 public_trace 后推送
```

SSE 事件示例：

```text
event: step_changed
data: {"progress":60,"current_step":"generate_scenes","label":"生成回忆卡片","status":"running"}
```

SSE 只返回 `public_trace` 级别内容，不返回 prompt、工具输入输出、模型原始输出。实现必须支持鉴权、heartbeat、事件序号恢复、终态关闭和断线回退轮询。

### 7.5 重试

```text
POST /api/v1/memory/archives/{archive_id}/retry
```

后端调用：

```text
POST {AGENT_RUNTIME_URL}/api/v1/agent-runs/{run_id}/retry
```

默认从失败节点恢复，不重建 snapshot。

重试要求：

- 仅 archive 所属用户、具备内部审计权限的后台任务可以触发。
- 只允许 Runtime run 为 `failed/partial` 且存在 checkpoint 时调用 Runtime retry。
- Runtime package 已 revoked，或 run 的 `privacy_state` 为 `purge_requested/purged` 时禁止 retry；需要重新生成时创建新 run 和新业务 generation_epoch。
- 用户手动重试默认最多 3 次；Runtime 自动节点重试使用 `max_auto_retry_per_step`，两套计数器互不消耗。
- 重试接口必须使用新的 `Idempotency-Key`，但继续复用原 snapshot。
- `failed` 默认从失败 checkpoint 恢复；Runtime `partial` 只重试未成功入队的增强节点，不重新发布已成功作品。媒体 worker 后续生成失败走独立媒体重试，不调用 AgentRun retry。

### 7.6 置顶与删除

```text
POST /api/v1/memory/archives/{archive_id}/pin
POST /api/v1/memory/archives/{archive_id}/unpin
DELETE /api/v1/memory/archives/{archive_id}
```

删除事务先设置 `deleted_at`、递增 `generation_epoch`、清空 `active_run_id` 并撤销详情访问，再 best-effort 取消旧 Runtime run。即使取消失败，旧 run 的发布工具也会因 epoch 不匹配被业务后端拒绝。

删除当前用户的 archive 后，业务后端先在 `MemoryAgentRunRef` 保存 `privacy_purge_status=requested` 和稳定幂等键，再调用 Runtime 的受限私密数据清理接口。Runtime 先在事务中写入 privacy purge tombstone、递增 `privacy_version` 并请求取消，再异步删除该 run 的 Checkpoint 私密 payload、临时 Artifact payload 和模型/工具临时结果。所有后续私密写入必须校验旧 worker 持有的 privacy version，失配即丢弃；清理完成后标记 `purged`。业务对账查询到 Runtime `privacy_state=purged` 后才更新本地 completed 状态，不能把 cancel 成功当作清理成功。Runtime 可按审计策略保留 run_id、状态、hash、成本等非内容元数据。清理请求必须幂等，并进入对账任务。

## 八、第一版必须实现

情侣日记后端第一版必须做：

- archive / snapshot / playback_document / scene / action / media 基础表。
- `memory_agent_run_refs` 映射表。
- 解绑后创建 snapshot。
- 调用 Agent Runtime 创建 `memoir_agent` run。
- Runtime readiness/capabilities 检查、Contract/AgentPackage 版本校验和能力缓存失效策略。
- 暴露 `memory.get_snapshot`、`memory.publish_playback_document`，并为二期预留 `memory.enqueue_tts`。
- 支持生成状态查询、重试、删除、置顶、密码解锁。
- 所有工具 API 有服务身份校验和幂等控制。

## 九、二期再做

- 工具 API 改造成标准 MCP Server。
- AgentRun 详情后台查看。
- 人工审核 callback。
- 媒体任务独立重试。
- 分享页生成与撤销。
- 用户选择重生成某一章。

## 十、测试建议

- 同一关系重复触发归档只生成双方各一条 archive。
- Runtime 重复 callback 不造成重复保存。
- Runtime callback 乱序到达不造成状态倒退。
- `memory.publish_playback_document` 重试返回同一 revision，失败时不切换 `published_revision`。
- worker 接管或节点恢复仍复用相同逻辑副作用幂等键，不因 attempt 变化重复插入。
- 相同 `Idempotency-Key` 请求体一致时返回原结果，请求体不一致时返回 HTTP `409 Conflict` 和错误码 `IDEMPOTENCY_CONFLICT`。
- Runtime 创建成功但业务绑定失败、或业务 pending_start 未绑定 run_id 时，可通过补偿任务使用原始 `Idempotency-Key` 恢复。
- Runtime readiness 失败或进入 draining 时不创建新 run，恢复后补偿创建且不重复扣费。
- Runtime capability 缺少所需 policy 时使用 baseline/明确降级，不把 provider 配置详情暴露给业务前端。
- 已删除日记不会被 `memory.get_snapshot` 返回。
- 已发布作品引用的素材后来正式删除时，新 revision 移除引用，旧 revision 和媒体完成清理后不可访问。
- 不同 `relationship_segment_no` 数据不混入。
- Runtime 失败时详情页展示基础统计卡。
- 用户删除自己的 archive 不影响对方。
- 删除与发布并发时，旧 run 因 generation_epoch 不匹配无法恢复已删除作品。
- callback 与发布工具无状态双写，异步媒体失败不修改已结束 AgentRun。
- purge 与迟到模型/工具结果并发时，privacy version 写屏障阻止 Checkpoint/Artifact 私密内容复活。
- package 从 deprecated 变为 revoked 后，新 create/start/retry/resume 被拒绝，queued/running run 按安全边界取消。
- callback 进入 dead letter 后重放仍使用原事件身份，业务状态不倒退且可通过主动查询修复。
- run_dispatch 进入 dead letter 并超过恢复上限后，run 明确变为 failed(DISPATCH_FAILED)，业务补偿不会无限等待 pending。
- 日记/赌局文本包含提示注入时，模型不能改变工具、connector、统计值、generation epoch 或发布参数。
- Runtime caller/connector 在途撤销后，业务工具二次鉴权拒绝旧 run，Runtime 终止剩余副作用。
- create held run 后、业务绑定前不会出现工具调用或 callback；start 重试不创建第二个 run。
- 所有用户侧成功响应符合统一结构。
