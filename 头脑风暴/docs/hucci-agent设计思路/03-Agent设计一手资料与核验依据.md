# Agent 设计一手资料与核验依据

访问与整理日期：2026-10-27（Asia/Shanghai）。

本文提供评估 Agent 设计的一般基线。这里只陈述已阅读的一手资料及工程归纳，不把这些内容归因于视频，也不据此声称视频已经讲到或遗漏某项能力。视频论述与项目实现的判定，应分别回到转写时间点、项目代码和实际测试。

资料的权威性有不同范围：协议规范约束协议实现；SDK 文档解释该 SDK 的行为；厂商工程文章提供其场景下的经验。它们都不构成“所有 Agent 必须采用同一架构”的证明。

## 一、已核验的一手资料

### S1：Agent 与 workflow，以及何时增加自主性

来源：[Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)，2024-12-19。

该文按控制权区分：workflow 通过预定义代码路径编排模型与工具，Agent 由模型动态选择过程与工具。建议先采用能解决问题的简单方案；开放任务可通过工具反馈循环推进，但需要停止条件与测试。

适用范围：选型与编排模式。该分类是 Anthropic 的架构用语；不能把它当成唯一术语标准。原页已提示其部分工具信息随时间变化，应避免照搬旧产品清单。

### S2：上下文工程，以及长期任务的记忆

来源：[Anthropic — Effective context engineering for AI agents](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)，2025-09-29。

上下文工程管理每次推理所见的信息，包括指令、工具、外部数据与历史。资料建议按需检索、精简工具返回，并通过压缩、外置笔记或子任务隔离维持长任务连续性；过度压缩会丢失关键事实。

适用范围：控制信息相关性与上下文开销。不能推导出“上下文越短越好”，也不能推导出“所有项目必须使用向量数据库”。持久化后仍需选择哪些信息进入模型窗口。

### S3：最小组成、单 Agent 起步与多 Agent 拆分

来源：[OpenAI — A practical guide to building agents（PDF）](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)，重点为 PDF 第 7–17、25–31 页。

该指南用模型、工具、指令描述最小组成，建议先建立评测基线并发挥单 Agent 能力，再按复杂逻辑或工具选择失误决定拆分；管理者调用子 Agent 与执行权移交是两类不同模式。

适用范围：入门设计与演进路线。三项最小组成用于解释核心机制，不能据此判断生产系统已经具备恢复、隔离或运维能力；指南也没有要求所有应用一开始就多 Agent 化。

### S4：持久化状态与长期记忆的范围

来源：[LangGraph — Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。

官方文档区分 checkpointer 保存线程内图状态，与 store 保存跨线程应用数据。前者支撑会话延续、暂停与恢复；后者可用于偏好和知识。内存版 saver 在进程重启后丢失数据。

适用范围：LangGraph 的存储接口与生命周期。不能用“已配置 MemorySaver”证明具备跨进程耐久性，也不能把业务事实库、执行快照和模型当前上下文视为同一数据对象。

### S5：durable execution 的真实保证边界

来源：[LangGraph — Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)，重点为 Checkpoints、Replay、Durability modes。

检查点保存图执行边界；replay 会重跑所选检查点之后的节点，包括模型与 API 调用。`exit`、`async`、`sync` 的落盘时机与崩溃恢复保证不同，不能只凭“使用图框架”判断恢复可靠性。

适用范围：该框架的恢复语义。需结合实际 saver、耐久模式、节点边界和外部系统副作用验收；同步保存检查点也不等于外部业务操作天然只发生一次。

### S6：人工介入、暂停状态与外部副作用

来源：[OpenAI Agents SDK — Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/)，重点为 Long-running approvals、Recovering after a crash、Versioning pending tasks。

SDK 支持敏感工具调用暂停、批准/拒绝与恢复。官方明确：批准不保证 exactly-once；外部动作成功而状态未保存时仍可能重复。建议稳定幂等键，并由服务端保管状态、认证与授权审批者，防止重放。

适用范围：该 SDK 的审批与恢复机制，同时提示通用分布式副作用问题。人工批准、业务成功和运行状态提交是不同事件；长时间挂起还需要兼容定义版本。

### S7：可观测性与敏感信息

来源：[OpenAI Agents SDK — Tracing](https://openai.github.io/openai-agents-python/tracing/)。

文档记录模型生成、工具调用、handoff 与 guardrail 等事件，可用于追踪一次完整运行。模型和函数的输入输出可能包含敏感信息，SDK 提供关闭相关采集的配置。

适用范围：该 SDK 的追踪实现。Trace 解释过程，不自动证明结果正确；业务需要另行选择保留内容、脱敏与访问范围，不能把完整输入输出无条件当作普通日志。

### S8：评测要验证环境结果

来源：[Anthropic — Demystifying evals for AI agents](https://www.anthropic.com/research/demystifying-evals-for-ai-agents)，2026-01-09。

该文区分任务、重复试验、轨迹与最终环境结果；Agent 声称完成，不等于数据库或产物真的达到目标。代码、模型与人工评分各有适用场景，需要组合并校准。

适用范围：Agent 质量评估方法。评测应包含模型与执行框架的整体表现；离线评测与生产监控互补，单次成功演示不足以证明稳定性。

### S9：MCP 的协议职责

来源：[Model Context Protocol — Specification，2025-11-25 固定版本](https://modelcontextprotocol.io/specification/2025-11-25)。

MCP 规范 host/client/server 之间的上下文与能力交互，包括 resources、prompts、tools 和能力协商。规范同时说明，协议本身不能强制实现其安全原则，权限、用户控制与数据保护仍需宿主落实。

适用范围：采用此版本的协议互操作。本文选取固定版本便于复核，不宣称它是访问日最新版本。接入 MCP 不等于获得完整 Agent 调度、业务事务、安全沙箱或持久化能力。

### S10：提示注入与权限边界

来源：[OWASP Gen AI Security Project — LLM01:2025 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)。

外部网页、文件等内容可形成间接提示注入；RAG 与微调不能完全解决该问题。资料建议分隔不可信内容、最小权限、代码约束、高风险操作人工介入与对抗测试。

适用范围：威胁建模与缓解思路。不能声称一段 system prompt 已彻底防注入；也不能把所有工具调用一律设为同等风险，应依据能力与业务影响设计执行边界。

### S11：多 Agent 的适用边界

来源：[Anthropic — How we built our multi-agent research system](https://www.anthropic.com/research/multi-agent-research-system)，2025-06-13。

该研究系统受益于并行探索、独立上下文和结果压缩；共享上下文过多、任务依赖密集时协调更困难，额外 token 与协调成本也显著增加。

适用范围：该团队研究产品的工程经验。文中性能与用量数字来自特定模型和内部任务，不宜外推为所有项目的收益比例；拆 Agent 的价值应由本项目实测决定。

## 二、用于核验分层设计的职责表

以下是结合上述资料形成的工程归纳，**并非某一家官方规定的统一七层架构**。这些职责可合并在小项目的同一进程中；是否拆模块、服务或仓库，应由变化频率、部署方式和隔离要求决定。

| 职责边界 | 应回答的问题 | 依据与适用范围 |
| --- | --- | --- |
| 业务目标与领域规则 | 用户要得到什么，什么算成功，哪些规则必须由业务代码保证？ | 本报告归纳；验收应落到真实结果，参考 [S8](https://www.anthropic.com/research/demystifying-evals-for-ai-agents)。 |
| Agent / workflow 编排 | 哪些步骤固定，哪些由模型选择，何时结束、失败或转人工？ | 按控制权划分职责，参考 [S1](https://www.anthropic.com/research/building-effective-agents)、[S3](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)。 |
| 运行与恢复 | 一次 run 如何挂起、保存、恢复，崩溃后从哪里继续？ | 恢复边界取决于实现，参考 [S4](https://docs.langchain.com/oss/python/langgraph/persistence)、[S5](https://docs.langchain.com/oss/python/langgraph/checkpointers)。 |
| 模型与上下文 | 如何构造本轮模型输入，模型切换如何验证？ | 信息预算与模型选择属于不同决策，参考 [S2](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)、[S3](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)。 |
| 工具与外部适配 | 输入输出是什么，由谁鉴权，重试是否造成重复业务操作？ | 协议互通与副作用控制分别核验，参考 [S9](https://modelcontextprotocol.io/specification/2025-11-25)、[S6](https://openai.github.io/openai-agents-python/human_in_the_loop/)。 |
| 状态、记忆与产物 | 哪些是执行进度、跨会话知识、用户可交付产物，各保留多久？ | 本报告归纳；线程状态与长期数据应有明确范围，参考 [S4](https://docs.langchain.com/oss/python/langgraph/persistence)。 |
| 质量、安全与治理 | 如何评测、观测、限制能力和升级人工处理？ | 贯穿各层，参考 [S7](https://openai.github.io/openai-agents-python/tracing/)、[S8](https://www.anthropic.com/research/demystifying-evals-for-ai-agents)、[S10](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)。 |

具体目录名、依赖注入方式、是否使用 DDD、是否把每个角色实现为独立 Agent，都属于后续工程选择。上表只提供可检查的职责，不以目录数量判定架构质量。

## 三、如何区分“正确”与“完整”

**正确性**核验某个主张是否成立、前提是否清楚、代码是否支持；**完整性**核验在声明的目标场景下是否覆盖必要职责。一个介绍核心循环的演示可以正确，但不足以单独充当生产实施方案；也不能因它没有讨论部署就判定核心循环错误。

以下为本报告提出的核验问题，应按场景裁剪，不作为要求所有项目一次性实现的清单。

| 核验维度 | 对设计说明的检查问题 | 需要的证据 |
| --- | --- | --- |
| 目标与验收 | 能否明确成功产物、可接受失败与交还用户的条件？ | 可执行验收样例；参照 [S8](https://www.anthropic.com/research/demystifying-evals-for-ai-agents)。 |
| 自主性 | 固定流程已能完成时，增加模型决策是否有收益？ | 与简单方案比较；参照 [S1](https://www.anthropic.com/research/building-effective-agents)。 |
| 工具契约 | 参数、结果与错误是否明确，权限在什么位置执行？ | 工具定义与执行端实现；参照 [S3](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)、[S10](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)。 |
| 信息生命周期 | 检索、记忆、执行状态与模型输入分别是什么？ | 读写范围、上下文样例；参照 [S2](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)、[S4](https://docs.langchain.com/oss/python/langgraph/persistence)。 |
| 长任务恢复 | 哪些步骤可重跑，哪类动作必须防重复？ | 中断与重启测试；参照 [S5](https://docs.langchain.com/oss/python/langgraph/checkpointers)、[S6](https://openai.github.io/openai-agents-python/human_in_the_loop/)。 |
| 外部内容与权限 | 不可信资料能否诱导越权调用，关键动作由谁放行？ | 隔离与权限实现、注入测试；参照 [S10](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)。 |
| 可诊断与质量 | 能否解释失败，并检查最终环境而非只看回答？ | 运行轨迹与独立验收；参照 [S7](https://openai.github.io/openai-agents-python/tracing/)、[S8](https://www.anthropic.com/research/demystifying-evals-for-ai-agents)。 |
| 多 Agent | 子任务能否独立执行、交付与汇总，收益是否抵消开销？ | 同任务的单/多 Agent 对照；参照 [S11](https://www.anthropic.com/research/multi-agent-research-system)。 |
| 可演进性 | 模型、提示、工具变化后，旧挂起任务能否安全恢复？ | 版本兼容与恢复测试；参照 [S6](https://openai.github.io/openai-agents-python/human_in_the_loop/)。 |

对短暂、只读的问答任务，可以不建设跨进程执行恢复；跨天运行、外部写入或等待审批的任务，就必须说明相应状态与副作用处理。长期记忆、多 Agent、MCP 都是按需能力，不能作为判断“真正 Agent”的固定门槛。

## 四、访问记录与限制

- 上述 11 个来源均于 2026-10-27 实际打开阅读；未使用二手教程替代其内容。
- Anthropic 的 `/engineering/...` 地址当前跳转到相应 `/research/...` 原文，本文链接使用最终地址。
- LangGraph 的 [durable-execution 原地址](https://docs.langchain.com/oss/python/langgraph/durable-execution) 当次访问跳转至 Persistence；耐久模式与 replay 语义在 Checkpointers 页面核验。本文没有据旧地址标题推测旧版页面内容。
- OpenAI 实践指南以官方 CDN PDF 为核验对象；PDF 页码按阅读器从 1 起计，网页发布信息与 PDF 版本时间不作等同判断。
- 本文未对这些 SDK 进行安装或运行测试，也未对 com-agent-runtime 进行代码审计。API 细节是文档证据，不能替代项目当前依赖版本和故障测试。
- 未在本文分析视频发言；本目录中的视频提取、正确性分析与项目对照文档负责完成证据关联。
