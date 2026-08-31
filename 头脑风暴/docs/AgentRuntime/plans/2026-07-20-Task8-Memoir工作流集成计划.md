# Task 8 Memoir 工作流集成 Implementation Plan

> **2026-08-31 增量校准：** Task 1～4 记录的是既有单次模型节点接入完成事实；
> 它不代表五类素材动态生成已经完成。当前 `1.0.4` 前置链仍存在最多 8 refs、
> 1～3 章和单次 token 窗口等隐性裁剪。后续 `bounded_loop` 与
> `memoir_agent@1.0.5` 设计见
> [通用受控循环与 Memoir 动态生成设计说明](./2026-08-31-通用受控循环与Memoir动态生成设计说明.md)，
> 下列新增任务均未实现。

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已完成的 PromptRegistry、ContextManager、Structured Output、ModelGateway 与语义校验接入 MemoirAgent 的实际节点，且不让正文进入日志、Artifact 或 checkpoint。

**Architecture:** 每个模型节点从冻结 PromptRegistry 精确解析 prompt id/version 和 schema；节点将快照素材转为 ContextManager 的受限上下文，再调用 ModelGateway，最后以 Structured Output/语义校验得到可写入 AgentState 的安全摘要。失败由既有 baseline/fallback 边界处理。

**Tech Stack:** Python、Pydantic、SQLAlchemy、pytest、现有 ModelGateway。

## Global Constraints

- 不新增模型 SDK、动态工具选择或第二套 prompt/JSON parser。
- 输入、输出、日志、Artifact、checkpoint 禁止保存日记正文、Prompt 模板、完整播放文档。
- 节点只使用冻结 package/prompt/model policy，不接受业务请求覆盖。

---

### Task 1: 节点 Prompt 与上下文装配

**Files:** `app/agents/memoir_agent/runner.py`、`tests/test_memoir_model_gateway.py`

- [✅] 写失败测试：模型节点调用必须携带精确 prompt id/version 与 ContextManager 生成的无正文摘要。
- [✅] 实现：为 highlight/chapter/scene 节点集中调用既有 PromptRegistry 与 ContextManager；缺 prompt、预算或 policy 时返回标准安全错误。safety 仍为无模型规则节点。
- [✅] 验证：`poetry run pytest -q tests/test_memoir_model_gateway.py`。

### Task 2: 结构化输出与语义校验

**Files:** `app/agents/memoir_agent/runner.py`、`tests/test_prompt_context_structured_output.py`、新增节点回归测试。

- [✅] 写失败测试：无效 JSON、schema 不匹配、未知 source ref、危险控制字段均不能写入 AgentState。
- [✅] 实现：复用 `StructuredOutputParser`、一次无执行 JSON repair 和 `SemanticValidator`；仅通过结果进入 `apply_tool_output` 白名单。
- [✅] 验证：节点成功/失败均不泄漏正文的断言。

### Task 3: ModelGateway 审计与失败降级

**Files:** `app/agents/memoir_agent/runner.py`、`tests/test_model_gateway.py`、`tests/test_memoir_model_gateway.py`

- [✅] 写失败测试：route/policy 不可用、预算耗尽、provider 超时均保留 baseline，ModelUsage 仅记录 prompt id/version 与安全摘要。
- [✅] 实现：节点使用权威 Run/Step/Lease context 调 ModelGateway；部署内 PromptDefinition 覆盖请求自报元数据，异常映射为标准码并交给既有 workflow fallback。
- [✅] 验证：`poetry run pytest -q && poetry run ruff check . && git diff --check`。

### Task 4: Ponytail 与总控

- [✅] Ponytail 最小实现审查：复用既有 PromptRegistry、StructuredOutputParser、ModelGateway 与 AgentState 白名单，未新增 parser、模型 client 或正文日志。
- [✅] 仅将实际接入并具备回归测试的 Task 8 项标记 `[✅]`；未实现的模型能力、视觉/媒体与动态工具选择保留 `[ ]`。

### Task 5: 通用 `bounded_loop` 静态节点

**Files:** AgentPackage schema/loader、Executor、PolicyEngine、Checkpoint/Audit、相关测试。

- [ ] Package 加载时校验 `node_type=bounded_loop` 与完整 `loop_policy`；循环体只允许 model/deterministic，无 Business Tool、媒体上传或发布副作用。
- [ ] Executor 在单一静态 DAG 节点内执行有限循环，逐轮复核全局与节点预算、deadline、lease/fencing、privacy/authorization 和 Package 状态；每次模型调用独立计量。
- [ ] 循环中途不写 Checkpoint，只写无内容 audit 计数与 `continue|complete|partial|failed`；整个循环节点完成边界才写安全路由 Checkpoint。节点中途 crash/resume 从 Snapshot、循环节点起点全量重算，发布继续 query-after-commit。
- [ ] 审计仅记录 iteration、批次大小、计数、usage id、耗时和受控原因码，不记录 source ref、摘要、prompt、候选或 Scene。

### Task 6: `memoir_agent@1.0.5` 五类动态生成

**Files:** 新不可变 AgentPackage、Memoir Runner、ContextManager/model policy、版本登记与相关测试。

- [ ] 五类合格素材按类型交错循环扫描；单轮切片由模型 context window 与既有通用预算计算，移除固定 refs 与 1～3 章对总素材的裁剪。
- [ ] 模型按批次决定 Scene 数、主题和混合叙事，产品只保留至少 3 Scene，不设总 Scene/图片数量上限；冻结 Scene/Action/PlaybackDocument wire 不变。
- [ ] `finalize_scenes` 确定性保证每个实际存在的合格素材类型至少被一个 Scene 的真实 source_ref 引用；Business 必须为其提供安全 digest，缺失时契约 fail closed。预算耗尽但类型覆盖完整时循环结果为 `partial`；缺类先单次修复，仍缺失则 `failed`，不得用无来源 fallback 冒充覆盖。
- [ ] 补旧包冻结、五类覆盖、超过 8 条、多 Scene、预算/取消/撤权/fencing、resume 重算、发布对账和每场景媒体降级测试；未有代码与验证证据前不得勾选。
