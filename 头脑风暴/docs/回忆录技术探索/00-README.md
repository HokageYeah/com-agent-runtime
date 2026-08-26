# 回忆录高阶情感作品与公共 Agent Runtime 技术探索

> **2026-08-26 阅读说明：** 本目录保留了项目初期的技术探索、候选方案和分期设想，不是当前部署手册或精确 wire 契约。新手应先阅读仓库根目录 `README.md` 、`ENV_CONFIG.md` 和 `VERIFICATION.md`，再阅读 `头脑风暴/docs/AgentRuntime/需求设计文档.md` 与 `契约冻结记录.md`。本目录中的 LiteLLM、Redis 队列、异步 MediaTask、3–16 张与 80 字上限等描述，如果与当前 AgentPackage 或测试冲突，只代表历史方案。
>
> **当前实现摘要：** Runtime 使用自研 `ModelGateway`/Provider Adapter，Run 调度以数据库 outbox/lease 为权威，Redis 只承担模型共享流控。`memoir_agent@1.0.4` 至少生成 3 个场景，不再设场景总数和 `body` 字数上限；发布前可在 Runtime 内部逐场景串行文生图，再把 `media_manifest` 与文档原子发布。媒体默认关闭，当前不读取或外发用户照片。

> **定位：** 将「情侣日记」回忆录从解绑后的静态归档升级为可生成、可播放、可降级、可扩展的高阶情感作品系统；同时从设计阶段开始抽离公共 AI Agent Runtime，避免回忆录、客服、学习助手等业务重复建设 Agent 基础能力。  
> **参考：** `OpenMAIC项目技术栈/PROJECT_ANALYSIS.md` 中对 OpenMAIC 的 Stage / Scene / Action、两阶段生成、媒体补全、播放引擎、AI SDK、LangChain / LangGraph 和降级策略的分析。  
> **已落地技术栈：** FastAPI + LangGraph Python + LangChain Core + 自研 ModelGateway/Provider Adapter + SQLAlchemy/Alembic + MySQL；Redis 用于模型共享流控。LiteLLM、Arq、OpenTelemetry Collector、LangSmith 和 MCP 还不是当前必需运行依赖。Runtime 通过授权 Business Tool 与情侣日记 FastAPI 后端集成。

## 一、核心结论

回忆录不应在情侣日记后端内部直接实现一套专属 AI 生成系统。正确方向是：

```text
公共 Agent Runtime
  -> 执行 MemoirAgent 回忆录业务 Agent
  -> 通过业务工具读取 MemorySnapshot
  -> 生成 MemoryScene / MemoryAction
  -> 可选在发布前生成图片并组装 media_manifest
  -> 通过唯一发布工具原子写回完整 PlaybackDocument
```

公共 Runtime 负责 Agent 的通用能力：

- LangGraph 工作流编排、状态持久化、失败恢复、人工介入。
- LangChain prompt、tool、parser、retriever、middleware 等组件生态。
- 模型调用、结构化输出和 provider 适配。当前由自研 ModelGateway 与版本化 Provider Adapter 承接 AI SDK 的 provider 抽象思想。
- MCP / HTTP Tool Gateway，统一接入业务工具和外部工具。
- 上下文管理、记忆、模型路由、安全护栏、观测、评测、成本记录。
- Public Trace 策略，决定前端是否展示脱敏后的 Agent 思考与执行轨迹。

回忆录业务只负责：

- 关系解绑边界。
- `MemoryArchive` / `MemorySnapshot` / `MemoryScene` / `MemoryAction` 等业务数据。
- 密码、权限、删除、置顶、播放器接口。
- 暴露给 Runtime 使用的业务工具。

## 二、OpenMAIC 启发

OpenMAIC 的经验不是“复制课堂功能”，而是复用架构思想：

```text
素材 / 需求
  -> 大纲
  -> 场景
  -> 动作
  -> 媒体
  -> 播放器
```

回忆录对应为：

```text
MemorySnapshot
  -> MemoirChapterPlan
  -> MemoryScene
  -> MemoryAction
  -> TTS / 封面 / 视频任务
  -> uni-app 回忆录播放器
```

这条链路由 `MemoirAgent` 执行，但 `MemoirAgent` 不自己实现 Agent 底层能力，而是运行在公共 Agent Runtime 上。

需要明确“对齐”与“复用”的边界：

- 对齐 OpenMAIC 的是契约分层、两阶段生成、结构化 Action、provider 路由、按场景降级和可恢复长任务思想。
- 当前独立 Python Runtime 不直接依赖 OpenMAIC 的 Next.js、Zustand、Dexie、`@openmaic/dsl` 或编辑态 `pi-agent-core` 实现。
- OpenMAIC 的编辑态 Agent Runtime 是面向课堂编辑、SSE 工具执行和客户端应用结果的专用运行时；本方案是面向多业务异步任务的公共 Runtime，二者不能描述成同一套现成代码。
- 可复用的第一优先级应是稳定协议和测试向量，而不是复制 OpenMAIC 主应用内部类型。
- OpenMAIC 客户端只在 content/actions/必要 TTS 完整后提交 Scene，并用 `generationEpoch` 丢弃迟到结果。回忆录对应采用 `MemoryPlaybackDocument + published_revision` 原子发布完整作品，并用业务 `generation_epoch` 拒绝旧 run 写回。
- OpenMAIC 的 IndexedDB 与服务端文件不是自动双向同步。回忆录因此只把情侣日记数据库视为业务作品权威源，Runtime Artifact 不形成第二套可播放副本。
- OpenMAIC 对页面生成使用有界并发，对媒体队列按 provider 能力限流。公共 Runtime 对应增加 AdmissionController 和 provider 共享背压，不能只依赖单 run 的 `max_steps/max_cost`。
- OpenMAIC 用 `/api/health` 暴露服务端能力，并明确 serverless 临时磁盘不能承担跨实例权威状态。公共 Runtime 对应冻结 liveness/readiness、鉴权能力发现和共享持久化边界，长 AgentRun 由后台 worker 执行。
- OpenMAIC 删除编辑 Agent 会话时使用 tombstone 阻止迟到保存把会话复活。回忆录删除对应采用 Runtime privacy tombstone/version；cancel 停止执行，purge 写屏障阻止迟到模型结果重建私密 Checkpoint 或 Artifact。
- OpenMAIC 让模型只引用 `img_N` 等稳定 ID，再由代码完成替换、归属校验和结构修复。回忆录对应把日记/RAG/工具结果视为 untrusted content，模型只返回候选 material/source ID，Runtime 与业务后端负责确定性语义校验。

## 三、文档导航

| 文档 | 内容 |
|---|---|
| [01-产品体验蓝图](./01-产品体验蓝图.md) | 回忆作品形态、页面入口、播放节奏、卡片样例 |
| [02-总体技术架构](./02-总体技术架构.md) | 公共 Agent Runtime + MemoirAgent + 情侣日记后端的整体边界 |
| [03-数据模型与素材快照](./03-数据模型与素材快照.md) | 归档边界、核心实体、素材快照、权限与删除约束 |
| [04-MemoirAgent业务工作流](./04-MemoirAgent业务工作流.md) | 基于 LangGraph 的回忆录业务 Agent 工作流 |
| [05-播放器与前端页面](./05-播放器与前端页面.md) | uni-app 页面、播放状态机、低配/高配展示 |
| [06-后端接口与AgentRuntime集成](./06-后端接口与AgentRuntime集成.md) | FastAPI 如何创建快照、调用 Runtime、暴露业务工具 |
| [07-安全隐私与情绪安全](./07-安全隐私与情绪安全.md) | 密码、脱敏、AI provider、内容审核、温和表达 |
| [08-实施路线图](./08-实施路线图.md) | 第一版、二期、三期任务拆分、验收场景、风险优先级 |
| [09-公共AgentRuntime架构](./09-公共AgentRuntime架构.md) | Runtime 分层、核心模块、第一版必做与二期能力 |
| [10-Agent工具协议与MCP适配](./10-Agent工具协议与MCP适配.md) | UnifiedTool、LangChain Tool、AI SDK Tool、MCP Tool、业务 HTTP Tool |
| [11-LangGraph与LangChain工作流设计](./11-LangGraph与LangChain工作流设计.md) | Workflow / Autonomous / Hybrid Agent 的使用边界 |
| [12-模型网关与Prompt工程](./12-模型网关与Prompt工程.md) | Provider Adapter、LangChain prompt/parser、模型路由、结构化输出 |
| [13-观测评测与运行治理](./13-观测评测与运行治理.md) | OpenTelemetry、LangSmith、评测集、成本、失败复盘 |
| [14-AgentRuntime运行机制](./14-AgentRuntime运行机制.md) | Plan、Act、Observe、Evaluate、Retry、Artifact 的完整运行链路 |
| [15-业务Agent接入规范](./15-业务Agent接入规范.md) | 回忆录、客服、订单等业务 Agent 如何注册、调用和迁入 |

## 四、设计原则

### 4.1 公共能力先抽离

项目在最初设计阶段就选择了公共 Runtime 路线，当前实现仍保持这一边界：不在情侣日记业务后端内另建一套回忆录专属 Agent 运行时。

### 4.2 第一版不是巨型平台

第一版必须有完整骨架：

- AgentDefinition
- AgentPackage
- AgentRun / AgentPlan / AgentStep / AgentToolCall / AgentEvaluation / AgentArtifact / AgentCheckpoint
- Planner / Evaluator / PolicyEngine
- LangGraph Workflow Executor
- LangChain prompt / tool / parser 组件
- 自研 ModelGateway / 版本化 Provider Adapter
- ToolGateway
- ContextManager
- Guardrails
- Retry / Resume
- Observability
- MemoirAgent

第一版不做完整平台化后台、不做复杂多 Agent 市场、不做商业计费、不做代码沙箱。

### 4.3 业务数据归业务系统掌控

公共 Runtime 不直接连接情侣日记业务库。情侣日记后端通过内部 Tool API 暴露可控能力，例如：

- `memory.get_snapshot`
- `memory.publish_playback_document`
- `memory.enqueue_tts`

权限、脱敏、删除过滤和数据边界由情侣日记后端负责。

### 4.4 结构可播放，媒体可降级

回忆录详情页消费的是结构化作品，不是原始日记和赌局：

- `MemoryScene` 决定展示内容。
- `MemoryAction` 决定播放动作。
- `MemoryMediaAsset` 决定 TTS、封面、视频等增强能力。

AI、TTS、图片或视频失败时，仍必须展示基础统计卡和模板动作，不能出现空白页。

情侣日记后端在 archive 创建时发布 revision 0 baseline；MemoirAgent 只能用单个原子发布工具切换到新作品 revision。播放器永远读取 `published_revision`，不会读取生成中的 Scene/Action 半成品。

### 4.5 通信边界清晰

业务后端创建 AgentRun 使用 HTTP 短请求；Runtime 通过 callback 推送关键状态；业务前端不直连 Runtime。小程序 MVP 通过业务后端状态接口轮询安全摘要，H5 或具备流式适配的端再启用 SSE。WebSocket 留给二期客服、人工介入、多轮实时交互等场景。

第一版必做 HTTP 状态查询和退避轮询；SSE 是业务后端的可选传输适配，不是小程序正确性的依赖。Runtime 原生 `/events` 事件流放到二期，避免前端进度依赖 Runtime 内部 trace。

### 4.6 实施前置清单

进入开发前必须冻结以下运行契约：

- callback 事件带 `event_seq` 和 `status_version`，业务后端按版本拒绝旧事件覆盖新状态。
- Runtime 发往业务后端的 callback 必须使用 HMAC-SHA256 签名，业务后端必须验签、校验时间戳和去重。
- `Idempotency-Key` 有明确存储者、TTL、请求体 hash 和冲突响应规则。
- Runtime 库与情侣日记业务库不做分布式事务，采用 pending 本地记录、幂等重试、对账任务和补偿恢复。
- 服务间签名使用 HMAC-SHA256，并明确签名原文、密钥分发和时间戳容忍窗口。
- PolicyEngine 默认硬限制、模型策略映射、双重脱敏边界、Retry owner 校验、手动/自动重试计数隔离必须在第一版实现前确定。
- 冻结 Runtime API、AgentPackage、Tool、Callback、Artifact 五类契约的 `schema_version`，并提供兼容性与迁移规则。
- `agent_id + agent_version` 还必须绑定不可变 `package_digest`；同一版本内容变化必须拒绝注册。
- 副作用工具的幂等键按“逻辑操作”稳定，网络重试和节点恢复不得因为 `attempt_no` 变化而生成新键。
- 明确 worker 对 run 的原子认领、lease/heartbeat、失联回收、取消传播和同一 run 单写者约束。
- `dispatch_state` 归 AgentRun 所有；Step、ToolCall、ModelUsage 记录 execution attempt，副作用逻辑幂等键不随 attempt 变化。
- 区分活跃执行预算、held/queued TTL、人工等待 TTL 和最终 run deadline，避免排队或审批消耗模型执行时间。
- 冻结 `/health/live`、`/health/ready` 和鉴权能力发现响应；API、worker、dispatcher 只依赖共享权威存储恢复，不能把临时磁盘或进程内存当作长任务状态源。
- AgentPackage Registry 定义 `active/deprecated/revoked`，安全撤销优先于已固定的 package digest，并传播到 create/start/retry/resume 和在途 worker。
- 私密数据清理先写 privacy purge tombstone 并递增 version，再执行物理删除；所有私密 payload 使用条件写，purge 后 run 禁止 retry/resume。
- dispatch/callback 使用持久 outbox；达到重试窗口进入 dead letter，重放必须复用原事件身份。
- dead letter 按事件类型恢复：callback 依赖原事件重放和主动查询，run_dispatch 重放失败后明确置为 `DISPATCH_FAILED`。
- 冻结 trusted instructions/untrusted content envelope、模型引用/工具参数语义校验和间接提示注入反例集。
- AgentRun 保存 authorization version；Runtime 在 execution attempt、模型、工具和 callback 前按当前权限复核，业务工具继续二次鉴权。

## 五、推荐落地顺序

1. 先建设公共 Agent Runtime 第一版骨架。
2. 在 Runtime 上实现 `MemoirAgent`。
3. 情侣日记后端完成 archive / snapshot / tool API / result callback。
4. 前端完成基础播放器和生成状态展示。
5. 二期再扩展 TTS、封面、视频、分享页、客服 Agent、长期记忆和管理后台。
