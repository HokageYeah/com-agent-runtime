# Task 8 Memoir 工作流集成 Implementation Plan

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
