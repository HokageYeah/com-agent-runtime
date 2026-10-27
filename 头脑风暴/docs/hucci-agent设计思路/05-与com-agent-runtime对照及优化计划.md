# 与 com-agent-runtime 对照及优化计划

核对日期：2026-10-27；代码基线：HEAD `11c748f`，本次研究开始时工作区干净。本篇是架构对照与建议，不是全仓审计、线上验收或已有总计划的重新排序。

## 1. 结论：方法兼容，运行形态和保证边界不同

Hucci 视频主要讨论“对话会话驱动、模型决定工具调用”的后端；com 目前主要承载“业务系统驱动、不可变 Package 定义、受控工作流执行”的公共 Runtime。前者适合讲清端口、适配与依赖注入，后者处理跨服务任务执行、恢复、发布和隐私治理。

com 已覆盖视频中的多数**架构职责**，并不具备视频涉及的每一种**具体功能**。例如 com 有模型网关与适配器，但不能据此说已经原生适配全部厂商；有运行状态持久化，也不能据此说已有完整对话分叉与历史压缩。

建议保留 com 的 Run/Plan/Step、Worker、Gateway、版本与授权模型。借鉴视频对依赖边界和组装根的表达方式，在第二个 Agent 或新协议接入时解决实际扩展瓶颈。把整个公共 Runtime 改成一个聊天循环，没有依据。

## 2. 核验范围与证据等级

本次读取了根 [README](../../../README.md)、[环境配置](../../../ENV_CONFIG.md)、[验证记录](../../../VERIFICATION.md)、[需求设计](../AgentRuntime/需求设计文档.md)、[总控计划](../AgentRuntime/plans/2026-07-07-AgentRuntime-总控开发计划.md)、[后端计划](../AgentRuntime/backend/2026-07-07-AgentRuntime-后端开发计划.md)，并沿下表列出的代码定位校准。

- **源码已见：**当前实现具有对应机制，不能自动等同于生产环境已启用。
- **本次测试：**四个指定测试文件实际通过，仅证明其覆盖范围。
- **历史记录：**文档记载的部署、真实服务或并发验证，未在本次重跑。
- **建议：**新增分析和候选计划，本次未实施，也未改已有 checklist。

仓库不同文档中保留了 M7、M8、`1.0.5` 等不同阶段横幅。例如部分旧段落仍说未实现/未提交，而更近期 README 和代码已有增量。本文不把历史横幅混成当前事实，也不据本地包存在判断线上版本。部署状态本次未查询。

## 3. 逐项映射：已经覆盖哪些职责

| 视频概念 / 能力 | com 对应实现 | 对照判断 |
| --- | --- | --- |
| 自己的领域语言 | [运行实体](../../../app/models/runtime.py)、[公共契约](../../../app/contracts/api.py)、[包定义](../../../app/schemas/agent_package.py) | 已有 Definition、Run、Plan、Step、ToolCall、Usage 等；核心语言比聊天消息更适合任务平台。 |
| AgentService 编排 | [AgentRunService](../../../app/services/agent_run_service.py)、[WorkflowExecutor](../../../app/runtime/executor.py)、[Worker](../../../app/worker.py) | 职责已拆分为生命周期事务、执行及调度；不需要合并成一个大服务。 |
| Session 与 SessionStore | Run、Step、Checkpoint、Artifact；[CheckpointStore](../../../app/runtime/checkpoint.py) | 覆盖运行状态保存，**不等价于**用户对话历史、分支与会话存储。 |
| ModelClient 与模型设置 | [ModelGateway / ProviderAdapter](../../../app/runtime/model_gateway.py)、ModelRoute、ModelPolicy | 已有统一边界、路由能力、限流、费用与 fallback；协议支持范围仍需逐个确认。 |
| Context 与角色上下文 | [ContextManager](../../../app/runtime/context_manager.py)、[PromptRegistry](../../../app/runtime/prompt_registry.py)、[消息渲染](../../../app/runtime/langchain_components.py)、[Memoir 模型适配](../../../app/agents/memoir_agent/model_gateway.py) | 已有上下文、可信/不可信分槽及精确 Prompt 版本；不是全历史直接拼接。 |
| Tool / ToolResult | [ToolManifest](../../../app/schemas/agent_package.py)、[ToolGateway](../../../app/runtime/tool_gateway.py)、[Tool 契约](../../../app/contracts/tools.py) | 已有声明、输入输出、错误、connector、副作用和取消语义，执行边界更严格。 |
| 替换外部依赖做测试 | ProviderAdapter、RunExecutor Protocol、注入式 httpx Client、[LangChain Tool 包装](../../../app/runtime/langchain_tools.py) | 已有端口与测试替身，不必为形式再次建立同名抽象。 |
| Reviewer | [Guardrails](../../../app/runtime/guardrails.py)、[Evaluator](../../../app/runtime/evaluator.py)、[语义校验](../../../app/runtime/semantic_validation.py)、确定性授权 | 已覆盖一部分审核职责；没有证据表明每个工具前都有独立模型 Reviewer，也没有必要强制增加。 |
| 循环次数限制 | [PolicyEngine](../../../app/runtime/policy_engine.py)、[bounded_loop](../../../app/runtime/bounded_loop.py) | 有更丰富预算；当前 bounded_loop 不等于自由工具循环。 |
| HTTP / CLI 与组装根 | [路由聚合](../../../app/api/api.py)、[Worker 装配](../../../app/worker.py)、[应用工厂](../../../app/runtime/api_app_factory.py)、[CLI](../../../app/scripts/agent_runtime_cli.py) | 已有相应职责；生产 API 和 Worker 是不同进程，有各自组装入口合理。 |
| 存储适配与依赖倒置 | SQLAlchemy 服务、CheckpointStore、部分 Protocol | 功能分层存在，但并非完全隔离 ORM 的纯端口架构；执行器直接使用 Session 和运行模型。不能声称已严格实现视频的依赖方向。 |
| 分叉 / 对话压缩 | 未找到可据以确认的公共会话分叉与历史摘要服务 | 素材分批、摘要和节点 token cap 不等于通用对话压缩；当前业务不需要就不用补。 |

此处是职责映射，不宣称类之间一一对应。例如视频中的 Assistant 和 Reviewer 是上下文角色；com 的节点、护栏与业务校验由多个模块共同承担。

## 4. com 已有、视频未展开的生产机制

| 机制 | 源码入口 | 保留它的原因 |
| --- | --- | --- |
| 精确版本与不可变包 | [AgentPackageService](../../../app/services/agent_package_service.py)、[PromptRegistry](../../../app/runtime/prompt_registry.py) | 精确版本、路径约束、digest、生命周期及最小 eval 文件约束，使变更和恢复有可核验基线。 |
| 业务绑定后启动 | [AgentRunService](../../../app/services/agent_run_service.py)、[API](../../../app/api/endpoints/agent_runs_api.py) | held/create/start 避免业务映射建立前执行；不能被“收到消息立即循环”替代。 |
| 单写者与接管 | [RunQueueService](../../../app/services/run_queue_service.py)、[LeaseService](../../../app/services/lease_service.py)、[执行接口](../../../app/runtime/interfaces.py) | lease、attempt、fencing 防止过期 Worker 推进状态。 |
| 可靠交付与对账 | [OutboxService](../../../app/services/outbox_service.py)、[Dispatcher](../../../app/dispatcher.py)、[ReconciliationService](../../../app/services/reconciliation_service.py) | 处理重复、丢通知和投递失败；不能用内存事件代替权威记录。 |
| 工具幂等与结果查询 | [ToolGateway](../../../app/runtime/tool_gateway.py) 的发布/查询路径 | 发布响应丢失时需辨别已提交与未提交，避免重复业务副作用。 |
| 容量与费用门禁 | [AdmissionService](../../../app/services/admission_service.py)、[PolicyEngine](../../../app/runtime/policy_engine.py)、[ModelUsageService](../../../app/services/model_usage_service.py) | 次数上限之外还需要容量、费用、时长和物理调用记账。 |
| 授权撤销及隐私清理 | [AuthorizationService](../../../app/core/authorization.py)、[AgentRunService 清理路径](../../../app/services/agent_run_service.py)、Executor/Gateway 复核 | 任务运行期间权限会变化；创建时一次通过不够。 |
| 安全产物和观测 | [ArtifactStore](../../../app/runtime/artifact.py)、[AuditService](../../../app/services/audit_service.py)、[Observability](../../../app/runtime/observability.py) | 既保留诊断信息，又避免正文、prompt、密钥和私有 URL 被扩散。 |

这里列的是已见机制，不对所有竞态和完整生产部署作本次通过声明。原有并发、进程和跨仓测试记录应按[验证文档](../../../VERIFICATION.md)单独验收。

### 当前图执行方式需要准确描述

[StaticPlanner](../../../app/runtime/planner.py) 从受信定义生成静态计划；[StaticWorkflowGraph](../../../app/runtime/graph_builder.py) 只接受受限的**线性 DAG**，不允许 Package 任意声明 fork/join 或条件边。LangGraph 用于验证、编译与固定顺序，真实节点副作用由 WorkflowExecutor 在安全门禁下执行。

`bounded_loop` 是这个静态计划中的一种受控节点：继承 Run 剩余预算，循环体约束为 model/deterministic，发布等 Business Tool 留在循环外。它不能证明 com 已有视频中“由模型自由挑选工具并循环”的通用 Agent，也不能因没有该模式就说当前 Memoir 设计错误。

## 5. 哪些做法直接移植会冲突

| 直接移植的做法 | 与当前项目的冲突 | 兼容方向 |
| --- | --- | --- |
| 让 Session.messages 成为全部运行事实 | 无法表达 Run 生命周期、Worker 所有权和已发布产物的一致性 | 如需聊天，在业务侧或独立会话服务管理 Session，通过受控接口关联 Run。 |
| 把完整消息、模型结果、工具载荷永久写进 JSONL/Store | 违反当前项目的私密内容持久化与观测边界；还缺共享调度与并发语义 | 运行存储继续只保留允许内容；新增会话产品须另定义存储所有权、授权、保留和删除。 |
| 把全部内容存入加密 checkpoint 以续聊 | 加密本身不使正文落库符合当前恢复规则 | 当前 Executor 只接受安全路由元数据，恢复重新取获授权素材并重算。 |
| 让模型任意生成工具、connector、发布参数 | 破坏静态 Package、可信参数映射和业务二次鉴权 | 模型只给候选内容；注册、路由、身份、目标和权限由可信代码控制。 |
| 用 Reviewer 的允许结果取代业务授权 | 审核结果没有 caller/tenant/resource 的权威性 | 规则与业务端授权为硬门禁，Reviewer 按需提供辅助信号。 |
| 直接增加通用 BashTool | 扩大能力范围，当前没有对应代码沙箱/权限/生命周期方案 | Memoir 继续使用窄业务工具；独立需求成立后设计隔离执行服务。 |
| 用 max_iterations 替换 PolicyEngine | 丢失模型/工具次数、费用、时间与取消约束 | 新循环继续继承公共预算和执行门禁。 |
| 新协议接入时仅改模型名或 base URL | 请求、消息、usage、停止原因和工具语义未必兼容 | 增加原生适配与契约测试；仅真正兼容的实现复用已有协议。 |

特别是 checkpoint：需求文档部分旧段落提到短 TTL 加密私密状态，但 [Executor.resume](../../../app/runtime/executor.py) 当前代码明确拒绝非安全键的旧完整状态，并尝试清除；成功节点写 checkpoint 时也只投影安全元数据。不能凭“已经加密”把完整作品、素材或聊天历史重新放进去。

## 6. 源码已经显示的扩展机会

这些是**扩展时的工作量与耦合线索**，不是当前 Memoir 业务必然失效的缺陷。

### 6.1 组装根中仍直接识别 Memoir

[app/worker.py](../../../app/worker.py) 的 `configured_executor`（387 行起）内部通过 `agent_id == "memoir_agent"` 选择执行器，并直接创建 MemoirNodeRunner 及媒体/音频服务；其他情况走 BootstrapExecutor。

这说明“能注册 Package”与“新增业务 Agent 自动具备真实执行器”是两件事。接入第二个 Agent 时适合建立**部署内可信的执行器工厂注册表**，复用 RunExecutor/WorkflowExecutor。注册表应由代码和部署配置控制，不接收调用者提供的 Python 路径，也不在请求中加载任意插件。

### 6.2 工具与上下文策略仍包含业务固定项

[ToolGateway.call](../../../app/runtime/tool_gateway.py)（194 行起）按 `_FIXED_HTTP_TOOLS` 校验工具、connector、路径和参数来源，正确地防止 manifest 自行扩大权限；当前映射集中在 memory 工具。

[ContextManager](../../../app/runtime/context_manager.py)（9–18、44 行起）使用 `extract_highlights`、`plan_chapters`、`generate_scenes` 等节点名固定预算上限，未知节点拒绝。这是安全默认，但新 Agent 需要扩展可信策略。

可借鉴视频的“应用定义端口、适配器实现”：公共层执行统一校验，业务注册提供受信工具与预算策略。必须保留未知项拒绝、主体隔离与全局预算上限，不能为了可插拔改成“Package 声明什么就信什么”。

### 6.3 执行器还知道业务发布节点名

[WorkflowExecutor](../../../app/runtime/executor.py) 约 490 行通过 `"publish_document" in completed_node_ids` 判断某个可选节点失败能否降级为 partial。该规则体现当前“已发布主作品后增强失败”的业务语义。

第二个 Agent 的主产物可能没有这个节点名。届时可把“核心产物已提交”的可信里程碑或判定接口显式化，继续由确定性规则控制 partial，而不是让所有 Agent 复用同一个业务节点名。新定义需要版本兼容，不能改写旧 Package。

### 6.4 模型网关有抽象，但原生协议适配仍有限

[ProviderAdapter / HttpProviderAdapter](../../../app/runtime/model_gateway.py)（530 行起）已经是良好扩展点。目前看到 OpenAI-compatible 分支补 `model` 并解包 `choices[0].message.content`；其余分支主要要求 JSON 响应。未见这里实现完整 Anthropic Messages 或 Gemini GenerateContent 的原生协议翻译。

该兼容分支注释也明确：只返回 content 时 usage 不可得，按未知用量处理。因此后续可以评估统一结果信封，在不落原文的前提下保留 usage、stop reason 和能力摘要；这不等于现有预算/账本不存在，也不在本次判定未知用量路径有漏洞。

视频的启发是把协议变化留在适配器，但验收必须覆盖能力、鉴权、usage、拒绝、截断、工具关联和安全网络边界，不能只测一条文本回答。

## 7. 候选优化计划：按触发条件实施

下表是本次研究给出的建议顺序，**不是对 AgentRuntime 总计划未完成项的审计或重排**。本次只产出设计建议，没有创建新开发任务或改 checklist。

| 建议顺序 | 工作项与触发条件 | 建议落点 | 可验收结果 |
| --- | --- | --- | --- |
| 1 | 下一次新业务评审时，明确 Session / Run、workflow / 动态 loop、产物所有权与原文存储范围 | 新业务接入说明、契约与架构文档 | 示例能说明目标、成功判据、权限、数据删除和重试语义；不重复建设现有底座。 |
| 2 | 接入第二个业务 Agent 时，验证最小端到端执行；抽离执行器工厂及业务里程碑 | Worker、RunExecutor、WorkflowExecutor 的必要接口 | 两个 Agent 都能创建/执行/取消/恢复；未知 Agent 安全拒绝；Memoir 旧包行为与 digest 不变；partial 不依赖另一业务的节点名。 |
| 3 | 同一新业务需要工具与模型节点时，受信注册工具绑定与上下文预算 | ToolGateway 注册边界、ContextManager 策略 | 新业务不用修改公共逻辑中的业务名分支；越权 connector、未知工具/节点和超预算仍被拒绝；撤销与迟到结果门禁不退化。 |
| 4 | 业务确需原生新模型协议时，新增协议适配器与能力契约，评估结果信封 | ProviderAdapter / ModelGateway、模型配置和协议测试 | 能力不支持时明确失败；保留安全用量/停止原因；通过结构化输出、超时、撤销、限流和费用未知的契约回归；再做受控真实样本。 |
| 5 | 新产品确需多轮聊天时，另设会话边界，并试点分叉/压缩 | 业务会话服务、可选执行策略 | 对话与 Run 可关联；历史与摘要生命周期明确；分支互不污染；旧审批/副作用不随分叉重放；当前私密内容禁存规则不被旁路。 |
| 6 | 出现无法由静态流程覆盖的任务后，再试受控工具循环 | 新执行策略与版本化 Package 能力 | 继承预算、权限、审计、幂等、取消和恢复；同任务对比静态方案，质量收益足以抵消成本与风险。 |

第 2–3 项通常应由同一个真实第二 Agent 验证，而非先造通用插件平台再找需求。第 4 项可因供应商需求独立推进；第 5–6 项是条件性产品扩展，没有当前需求就保留在候选列表。

评测能力优先复用现有 eval、隔离 mock/harness 与观测机制。每项新增能力附带相应协议或故障测试，不再新造一套与现有工具重复的测试平台。MCP、长期记忆、多 Agent 和代码沙箱各自另立需求与验收，不把它们作为“架构完整”的装饰项。

## 8. 双向可借鉴的结果

**视频对 com 的启发：**让第二业务的变化尽量落在新 Runner、可信注册和适配器中；让应用依赖清楚的小接口；组装处明确实际选中的依赖。最直接的验证是“接入第二个业务是否总要修改公共执行器和 Gateway 里的业务名称”。

**com 对视频骨架的补全：**把“保存消息”扩展成明确运行状态与安全恢复，把“审核工具”扩展成确定性授权和副作用控制，把“循环上限”扩展成完整预算，把“回复完成”扩展成独立业务验收和可靠交付。这些职责可按业务规模裁剪，不要求小项目照搬全部 Runtime。

因此，双方主要是表达方式与工程深度的互补。当前最有价值的演进方向，是用新业务检验公共边界，同时保留 com 已建立的可靠性、隐私和业务所有权约束。

## 9. 本次实际验证与限制

执行命令：

```bash
.venv/bin/python -m pytest -q \
  tests/test_prompt_context_structured_output.py \
  tests/test_runtime_native_tools.py \
  tests/test_runtime_langchain_components.py \
  tests/test_runtime_langchain_tools.py
```

结果：**16 passed in 0.54s**。这些用例覆盖 Prompt 精确版本、上下文预算和隔离、结构化解析、Native Tool 及 LangChain 适配等；测试进程通过已有 conftest 隔离本机 dotenv。没有新增测试或修改实现。

没有运行全量回归、真实模型、真实业务发布、数据库并发 harness、镜像构建或部署。对 Worker、授权、恢复等的结论来自当前源码与历史验证入口，不能把上述 16 个测试扩张成“全系统已验证”。文档本身另检查了本地链接、源码路径、关键符号、媒体摘要和格式差异。
