# soothe_agent 开发计划

> **供执行智能体使用：** 使用 `- [ ]` 逐项记录进度，完成后改为 `- [✅]`（任务标题同理为 `### [✅] Task`），**不要使用 `[x]`**；勾选必须有代码、测试或命令证据。先阅读情侣日记仓总控计划的“执行工作流选择”，按当前运行环境、任务规模和风险选择可用流程；插件不可用时使用项目原生工具与验证命令继续，不得模拟调用。

**目标：** 在 com-agent-runtime 中新增 `soothe_agent@1.0.0`，为情侣日记「AI 帮哄」提供帮我回与哄哄模拟器两种推理，通过 mock 业务服务与 mock 模型完成端到端验收。

**架构：** 完全照搬 `qa_agent@1.0.0` 的四节点异步 Run 模式；复用已有 `couple_diary_backend` connector；模型适配器按 `run.business_type` 选择两份 prompt；新增 `validation.py` 做确定性语义校验。不改动公共 Runtime 模块、memoir 与 QA 已发布包。

**技术栈：** Python、FastAPI、LangGraph、Pydantic v2、SQLAlchemy、pytest、ruff、mypy。

**分工：** 本计划由 Claude Code 编写并审阅，Codex 执行。总控计划在情侣日记仓：`/Users/yuye/YeahWork/Python项目/couple-diary-doc/头脑风暴/docs/superpowers/AI帮哄/plans/2026-11-02-AI帮哄-总控开发计划.md`。

---

## 0. 输入资料

实现前必须阅读：

- `AGENTS.md`
- `README.md`（“AgentPackage 与 MemoirAgent”“开发与验证”）
- `ENV_CONFIG.md`（QA Agent 配置段、`AGENT_MODEL_NODE_ROUTES_JSON`）
- `VERIFICATION.md`（QA Agent 接入记录）
- `app/agents/README.md`（新增 Agent 的唯一接入指南）
- `头脑风暴/docs/AgentRuntime/soothe_agent设计说明.md`（本计划的设计事实源）
- `头脑风暴/docs/AgentRuntime/plans/2026-10-27-QA-Agent接入与公共Runtime最小解耦开发计划.md`（结构样例）
- QA 参考实现：`app/agents/qa_agent/registration.py`、`bootstrap.py`、`runner.py`、`state.py`、`tools.py`、`model_gateway.py`、`app/agents/qa_agent/1.0.0/` 下全部文件
- 注册表：`app/agents/registry.py`
- 公共能力：`app/runtime/agent_registration.py`、`app/runtime/model_gateway.py`、`app/runtime/prompt_registry.py`、`app/runtime/context_manager.py`、`app/runtime/langchain_components.py`、`app/runtime/structured_output.py`、`app/runtime/tool_gateway.py`、`app/services/agent_package_service.py`、`app/schemas/agent_package.py`、`app/contracts/api.py`
- mock：`app/runtime/mock_business_server.py`、`app/runtime/mock_provider_server.py`
- 测试样例：`tests/test_qa_runner.py`、`tests/test_qa_model_gateway.py`、`tests/test_qa_package_tools_mock.py`、`tests/test_qa_end_to_end.py`、`tests/test_qa_factory.py`、`tests/test_agent_registry.py`、`tests/test_tool_gateway_profiles.py`、`tests/test_worker_agent_bootstrap.py`、`tests/test_register_agent_package.py`、`tests/test_config.py`
- env 模板：`docker/backend/test.env.example`、`docker/backend/production.env.example`
- 业务需求（情侣日记仓）：`头脑风暴/docs/superpowers/AI帮哄/需求设计文档.md` 第五、六、七、八、十一节

## 1. 成功标准

- `soothe_agent@1.0.0` 可被包服务加载并通过注册门禁（`--dry-run`）。
- 两种业务类型在 mock 业务服务 + mock 模型下各跑通一次完整 Run，结果经 `soothe.submit_turn_result` 提交。
- 设计说明第七节每条校验规则都有正反例测试。
- Run 输入、步骤摘要、日志中没有聊天正文、草稿或场景隐藏诉求（有测试断言）。
- memoir 与 QA 的全部测试保持通过，包 digest 不变。
- `poetry run pytest -q`、`poetry run ruff check .`、`poetry run mypy app`、`git diff --check` 全绿。

## 2. 文件结构与边界

新建：设计说明第三节列出的 `app/agents/soothe_agent/` 全部文件；`tests/test_soothe_*.py`（设计说明第十三节）。

修改：`app/agents/registry.py`（一处 import + 一行登记）、`app/runtime/mock_business_server.py`（三个 soothe 端点）、`docker/backend/test.env.example`、`docker/backend/production.env.example`（只加注释示例）、`ENV_CONFIG.md`、`README.md`、`VERIFICATION.md`；以及设计说明第十三节列出的既有测试补充断言。

边界：

- 公共模块（`app/runtime/`、`app/schemas/`、`app/worker.py`、`app/api/`、`app/core/`、`app/services/` 非 memoir 子目录）不得导入 `soothe_agent`，也不为它加分派分支。
- 不修改 memoir、QA 包内任何文件。
- 不新增数据库迁移。
- 不修改公共契约 `app/contracts/`。

## 3. 契约摘要

以设计说明第四、六节和情侣日记仓总控计划第 3.4 节为准：

| 项 | 值 |
|---|---|
| 身份 | `soothe_agent` / `1.0.0`；业务类型 `soothe_reply`、`soothe_simulator` |
| Run 输入 | `{session_id: str, turn_id: str}` |
| connector | `couple_diary_backend` |
| callback 目标 | `soothe_callback`（部署配置） |
| 数据域 | `couple_soothe` |
| 工具 | `soothe.get_turn_context`、`soothe.submit_turn_result`、`soothe.get_turn_result`，wire `1.1.0` |
| 节点 | `load_turn_context → compose_turn → validate_turn → submit_turn_result` |
| 模型路由 | `AGENT_MODEL_NODE_ROUTES_JSON={"soothe_agent":{"compose_turn":"<route_id>"}}` |

## 4. 任务拆分

### [✅] Task R1：技术验证（M0，先于一切实现）

**目标：** 验证设计说明中三处不确定点，决定是否需要拆包。

- [✅] Step 1：阅读 `agent_package_service.py` 与 `schemas/agent_package.py`，确认包加载是否要求模型节点 `prompt_ref` 与实际加载的 prompt 一致；`ModelGateway.call(..., prompt=prompt)` 是否用 node 的 `prompt_ref` 做校验。
- [✅] Step 2：确认 `guardrails.yaml` 的 `rules` 是否有白名单；有则只用白名单内的规则名。
- [✅] Step 3：确认受信调用方配置中 `callback_target_ids` 与 `data_domains` 的校验方式，确认新增 `soothe_callback` 与 `couple_soothe` 只需改部署配置。
- [✅] Step 4：确认 `AgentToolProfile.wire_versions` 之外是否还有按 Agent 版本登记 wire 的表（回忆录曾出现过 `_TOOL_WIRE_VERSION_BY_AGENT_VERSION`）；如有，列出需要登记的位置。

**输出：** 在本计划第 7 节写执行记录。若 Step 1 的结论是“会强制一致”，**停下**，提交 Claude 审阅（总控审阅关口 G0），由 Claude 修订为两个包后再继续。

### [ ] Task R2：版本包声明文件

- [ ] Step 1：按设计说明第五节创建 `app/agents/soothe_agent/1.0.0/` 下的 `agent.yaml`、`input.schema.json`、`output.schema.json`、`workflow.graph.py`、`tools.manifest.json`、`guardrails.yaml`、`callbacks.yaml`、`ui-trace.yaml`、`prompts/manifest.yaml`。
- [ ] Step 2：按设计说明第九节写入 `prompts/compose-reply.v1.md`、`prompts/compose-simulator.v1.md`；模板变量写法对齐 QA prompt 与 `render_model_messages` 的实际约定（R2 实施时确认 `{{var}}` 或其它语法）。
- [ ] Step 3：按设计说明第十二节写 `evals/minimal.jsonl`（≥8 条）。
- [ ] Step 4：`tests/test_soothe_package.py`：包可加载、两个 prompt 存在且在清单中、`allowed_business_types`、预算字段、evals 条数、digest 稳定（同内容两次加载一致）。

**命令：** `poetry run pytest -q tests/test_soothe_package.py`

### [ ] Task R3：状态模型与语义校验

- [ ] Step 1：`state.py` 按设计说明第六节实现上下文、草稿、提交回执、工作态模型，字段中文注释。
- [ ] Step 2：`validation.py` 实现 `validate_draft(business_type, context, draft)` 与禁用短语常量（设计说明第七节）。
- [ ] Step 3：`tests/test_soothe_validation.py`：第七节每条规则正反例；草稿模型的长度与枚举边界；`extra` 字段拒绝。

**命令：** `poetry run pytest -q tests/test_soothe_validation.py`

### [ ] Task R4：工具与注册

- [ ] Step 1：`tools.py`：`SOOTHE_HTTP_TOOLS`（connector `couple_diary_backend`）、`SOOTHE_TOOL_OUTPUT_FIELDS`、`soothe_trusted_references`、`submit_turn_result` 适配函数（照抄 QA `submit_answer` 的审计与幂等要求）。
- [ ] Step 2：`registration.py`：两个业务类型共用一个 `AgentToolProfile`，`business_id_field="turn_id"`，`wire_versions={"1.0.0":"1.1.0"}`；延迟工厂加载器。
- [ ] Step 3：`app/agents/registry.py` 加 import 与登记行；按 R1 Step 4 的结论登记其它 wire 表（如有）。
- [ ] Step 4：`app/runtime/mock_business_server.py` 增加三个 `soothe.*` 端点：根据 `turn_id` 前缀返回固定的帮我回或模拟器上下文；提交记录在内存；查询返回提交状态。
- [ ] Step 5：`tests/test_soothe_package_tools_mock.py`：请求路径、外壳、签名头、`Idempotency-Key`；`tests/test_agent_registry.py`、`tests/test_tool_gateway_profiles.py` 补 soothe 断言（未知业务类型不继承档案）。

**命令：** `poetry run pytest -q tests/test_soothe_package_tools_mock.py tests/test_agent_registry.py tests/test_tool_gateway_profiles.py`

### [ ] Task R5：模型适配器

- [ ] Step 1：`model_gateway.py` 按设计说明第八节实现 `SootheModelGatewayAdapter`。
- [ ] Step 2：核对消息角色渲染：用 mock Provider 抓取请求体，确认角色为 Provider 接受的 `system/user/assistant`（回忆录曾因 `human` 未映射导致 400）。
- [ ] Step 3：`tests/test_soothe_model_gateway.py`：路由集合精确匹配；业务类型 → prompt 选择；权威身份不符拒绝；素材槽与可信变量划分（聊天内容只出现在不可信槽）；`hidden_need` 进入请求但不进入任何日志；token 预估下限；异常收敛为受控错误码。

**命令：** `poetry run pytest -q tests/test_soothe_model_gateway.py`

### [ ] Task R6：Runner 与 bootstrap

- [ ] Step 1：`runner.py` 按设计说明第十节实现 `SootheNodeRunner`。
- [ ] Step 2：`bootstrap.py` 按设计说明第十一节实现配置校验与执行器装配，未配置时 `SOOTHE_AGENT_NOT_CONFIGURED`。
- [ ] Step 3：`tests/test_soothe_runner.py`：四节点顺序；`mode` 与业务类型不一致失败；未校验不得提交；先查后交（已提交直接收敛）；提交返回 409 记为 `TOOL_OUTCOME_UNKNOWN`；恢复时只查询不重发；模型返回非 JSON → `STRUCTURED_OUTPUT_INVALID`；校验失败 → `ANSWER_VALIDATION_FAILED`。
- [ ] Step 4：`tests/test_worker_agent_bootstrap.py`、`tests/test_config.py` 补 soothe 配置缺失/少键/多键/未知路由断言；`tests/test_register_agent_package.py` 补 soothe 注册 dry-run。

**命令：** `poetry run pytest -q tests/test_soothe_runner.py tests/test_worker_agent_bootstrap.py tests/test_config.py tests/test_register_agent_package.py`

### [ ] Task R7：端到端与隐私

- [ ] Step 1：`tests/test_soothe_end_to_end.py`：mock 业务服务 + mock 模型 Provider，`soothe_reply` 与 `soothe_simulator` 各跑一次完整 Run，断言提交内容与 mock 模型输出一致、Run 成功。
- [ ] Step 2：同文件隐私断言：Run 输入、`AgentStep` 摘要、`caplog` 中不含 mock 聊天内容、草稿文本、`hidden_need`。
- [ ] Step 3：用 evals 中的 `reply_banned_phrase`、`simulator_control_field` 两条构造 mock 模型输出，断言 Run 失败且未提交。

**命令：** `poetry run pytest -q tests/test_soothe_end_to_end.py`

### [ ] Task R8：配置模板、文档与全量门禁

- [ ] Step 1：两个 env 模板加注释示例（设计说明第十一节），默认不启用 soothe 路由。
- [ ] Step 2：`ENV_CONFIG.md` 增加 soothe_agent 配置段；`README.md` 的 AgentPackage 段落加一行 soothe_agent 说明；`VERIFICATION.md` 增加本次验证记录（命令与结果）。
- [ ] Step 3：全量门禁：`poetry run pytest -q`、`poetry run ruff check .`、`poetry run mypy app`、`poetry run alembic heads`（应无变化）、`git diff --check`。
- [ ] Step 4：注册 dry-run：`./agent-runtime.sh register development --agent-id soothe_agent --version 1.0.0 --dry-run`。未经用户明确授权不执行真实注册与服务器部署。

**审阅关口 G1：** 提交 R2–R8 的 diff 摘要、全部命令输出、dry-run 输出。

## 5. 验证命令

```bash
cd /Users/yuye/YeahWork/AIAgent项目/com-agent-runtime
poetry run pytest -q tests/test_soothe_package.py tests/test_soothe_validation.py tests/test_soothe_package_tools_mock.py tests/test_soothe_model_gateway.py tests/test_soothe_runner.py tests/test_soothe_end_to_end.py
poetry run pytest -q
poetry run ruff check .
poetry run mypy app
poetry run alembic heads
git diff --check
./agent-runtime.sh register development --agent-id soothe_agent --version 1.0.0 --dry-run
```

`poetry run` 不可用时使用 `.venv/bin/pytest`（系统 Python 缺少 `pydantic_settings`）。

## 6. 非目标、风险与交付清单

非目标：多模态、长期记忆、模板降级、修改公共契约、真实环境注册与部署、与真实业务后端联调（在总控 M4 进行）。

| 风险 | 应对 |
|---|---|
| prompt_ref 一致性校验导致单包方案不可行 | R1 先验证，必要时拆包 |
| Provider 角色或 JSON 模式不兼容 | R5 Step 2 抓包核对 |
| 新增登记影响 memoir/QA | 全量测试 + digest 不变断言 |

交付清单：

- [ ] `app/agents/soothe_agent/` 全部文件
- [ ] 注册表、mock 业务服务、env 模板、ENV_CONFIG、README、VERIFICATION
- [ ] 新增测试与全量门禁输出
- [ ] 注册 dry-run 输出

## 7. 执行记录

### 2026-11-02｜R1 技术验证

关口：G0（R1 已完成；2026-11-02 Claude 审阅通过，见本节末“G0 审阅结论”）

完成任务：R1 Step 1–4。当前源码不强制节点 `prompt_ref` 与实际调用的 prompt 一致，未触发拆包停止条件；保留 `soothe_agent@1.0.0` 单包、两种业务类型的方案。本轮到此停止，R2–R8 未开始。

改动文件：仅本计划文件，勾选 R1 四步并补充本节记录。没有新增或修改仓库内的实现代码、测试、版本包、配置模板、公共契约或迁移；情侣日记仓只读。

验证命令与结果：

- `PYTHONPATH=. poetry run python /private/tmp/soothe-r1-probe.py` → 退出码 0，依次输出 `STEP1 PASS`、`STEP2 PASS`、`STEP3 PASS`、`STEP4 PASS`；具体断言见下文。该脚本仅为本轮临时验证材料，不是 Agent 实现：使用 `/private/tmp` 中的 QA 包副本、SQLite 内存库、FakeRedis 与内存 Provider/HTTP 替身，不访问真实业务、模型或数据库。唯一 warning 是刻意设置的禁用 callback 目标未被装配，符合预期。
- 下列既有定向测试 → **88 passed in 6.35s**，退出码 0，无失败、无跳过；其中冻结 digest 用例核对 Memoir 1.0.0–1.0.8 与 QA 1.0.0 共 10 个包，与既有审核基线一致。

  ```bash
  poetry run pytest -q tests/test_runtime_agent_package_loader.py tests/test_prompt_context_structured_output.py tests/test_model_gateway.py::test_gateway_records_registered_prompt_reference_without_template_body tests/runtime_test_runtime_admission_authorization.py tests/test_runtime_callback_configuration.py tests/test_agent_registry.py tests/test_tool_gateway_profiles.py tests/test_qa_model_gateway.py tests/test_qa_package_tools_mock.py tests/test_qa_end_to_end.py::test_t5_package_digests_match_reviewed_t0_and_t4_records
  ```

- `git diff --check` → 退出码 0，无空白错误。`git diff --no-index --check -- /private/tmp/soothe-r1-plan-before.md 头脑风暴/docs/AgentRuntime/plans/2026-11-02-soothe_agent-开发计划.md` → 输出为空，无空白错误；退出码 1 表示两份文档有差异，后者同时覆盖尚未跟踪的本计划。`git diff --exit-code` → 退出码 0，无已跟踪文件改动。
- `python3 -`（内联文档检查）→ 15 个本地 Markdown 链接的目标路径全部存在；与开始前副本比较，R2–R8 和 R1 之前的章节未变，未创建 `app/agents/soothe_agent/`。仅校验本地路径，不将其表述为网页或锚点验证。

偏离计划之处：无。执行环境为 Codex，按 M0 单智能体验证；开始前已按第 0 节读取列出的输入资料（README、ENV_CONFIG、VERIFICATION 与业务需求按指定章节，QA 版本目录全部文件），并只读核对情侣日记总控计划第 3.4、5、7、9.1 节。源码基线为 `b5d5203`；开始时本计划与设计说明均为未跟踪文件，保留该状态，不提交、不建分支。

未完成或待确认：Claude 的 G0 审阅；R2–R8、soothe 包注册 dry-run、两种业务的真实执行与部署均未进行。本轮没有实现改动，未运行全量 pytest、Ruff、Mypy、Alembic 或真实 Provider/数据库/harness 验收，不将历史门禁数字作为本轮结果。

#### Step 1：不强制实际 prompt 与节点声明一致，无需据此拆包

- 包加载仅检查 `agent.yaml.prompts` 中的文件存在，并要求每个模型/护栏节点的 `prompt_ref` 属于该声明清单；没有把“节点声明的文件”与“适配器实际加载的 PromptDefinition”绑定。证据：[agent_package_service.py](../../../../app/services/agent_package_service.py) 的 `_validate_prompts`（第 248 行）、`_validate_node_prompt_refs`（第 260 行）；[agent_package.py](../../../../app/schemas/agent_package.py) 的 `WorkflowNodeDefinition.prompt_ref`（第 41 行），没有额外的一致性 validator。
- [PromptRegistry.load](../../../../app/runtime/prompt_registry.py)（第 41 行）按 `agent_id / agent_version / prompt_id / version` 精确查询 manifest，检查 owner、active 状态和文件路径；它不接收节点，也不比较节点 `prompt_ref`。两份 prompt 仍须各自声明在包和 manifest 中，由可信适配器选择。
- [ModelGateway.call](../../../../app/runtime/model_gateway.py)（第 743 行）及 `_call_route` 使用实际传入的 prompt 做模型/护栏策略、能力与 token 窗口检查，不读取节点 `prompt_ref`；第 892–904 行将实际 prompt 的 id/version 传给 [ModelUsageService.attach_prompt_ref](../../../../app/services/model_usage_service.py)（第 127 行），后者只给 reserved usage 写引用，不与工作流作比较。
- **实测：** 临时 QA 包同时声明原 prompt 与第二份 prompt，模型节点及保存的 AgentPlan 仍引用原文件；经 PromptRegistry 加载第二份并调用真实 ModelGateway，结果 `succeeded`、内存 Provider 调用 1 次，usage 记录第二份 prompt 的 id/version。反例：节点改指未声明文件、PromptRegistry 请求不存在的 manifest 条目，均被拒绝。
- **结论：** 本项停止条件未触发，不拆包；这只证明当前公共能力允许该选择方式，soothe 适配器按业务类型选择的实现与测试仍属 R5。

#### Step 2：rules 无规则名白名单，声明不会自动执行

- [AgentPackageService.load](../../../../app/services/agent_package_service.py) 第 102 行仅用 `_read_yaml` 读取护栏对象；第 307 行的读取器要求 YAML 根为对象。[AgentPackage.guardrails](../../../../app/schemas/agent_package.py) 第 204 行为 `dict[str, Any]`，没有 `rules` 的枚举、规则名白名单或执行分派。
- **实测：** 临时包加入 `reject_unsafe_phrases` 和任意验证名 `r1_arbitrary_rule` 均能加载并原样保留；将整个 YAML 根改成数组则被拒绝。
- **结论：** R2 可使用设计中的 `reject_control_fields`、`enforce_answer_length`、`reject_unsafe_phrases`。这些是声明；禁用短语等实际规则仍必须在 R3 的 `validation.py` 实现，再由 Runner 调用，不能因 YAML 加载成功就认为护栏已生效。

#### Step 3：回调目标与数据域使用部署 allowlist，无需增加公共枚举

- [AuthorizationService.authorize_create](../../../../app/core/authorization.py)（第 27 行）逐项检查 `agent_ids`、`business_types`、`callback_target_ids`、`connector_ids`、`data_domains`；第 85 行 `_require_allowed` 对已配置字段要求为 list 且包含请求值，空列表或非 list 都拒绝。**兼容例外：** 字段缺失或为 `None` 时不限制，因此部署必须显式维护完整授权列表，不能用缺省表示拒绝。
- [CreateAgentRunRequest](../../../../app/contracts/api.py) 第 51、55 行分别将 callback 目标定义为必填非空字符串、数据域定义为有长度限制的字符串，无固定名称枚举。`data_domain` 默认仍为 `couple_memory`，soothe 创建 Run 时必须显式传 `couple_soothe`。
- [Settings](../../../../app/core/config.py) 第 518、526 行从部署 JSON 读取调用方与 callback 目标；[configured_callback_gateway](../../../../app/worker.py)（第 464 行）遍历目标表，只装配启用且具备字符串字段 `url / runtime_id / key_id / secret` 的项。投递前第 161 行 `_callback_target_authorized` 还检查目标存在、调用方 callback allowlist 和授权版本；授权列表不是目标定义本身。
- **所需部署合并：** 在现有 `couple-diary` 调用方的 `agent_ids` 加 `soothe_agent`、`business_types` 加两种 soothe 类型、`data_domains` 加 `couple_soothe`、`callback_target_ids` 加 `soothe_callback`；`connector_ids` 保留 `couple_diary_backend`。另在 `RUNTIME_CALLBACK_TARGETS_JSON` 独立登记并启用 `soothe_callback`，URL 指向业务后端 `/api/v1/internal/soothe-callbacks`，复用现有 callback 签名凭据，保留原有配置。配置更新后重启读取它的 API/Worker 进程；本轮未改部署配置。
- **实测：** 两种业务类型均通过显式授权；callback/data-domain 空列表与非 list 均拒绝，缺省列表保留上述兼容行为；未授权 callback 被拒绝；新目标可由 Settings 配置装配，禁用目标不装配。
- **结论：** 这两个名称的登记与授权只需部署配置，不需修改公共契约、schema、枚举或数据库。业务端 callback handler 与 soothe Agent 本身仍须由后续任务提供，本结论不表示它们已可运行。

#### Step 4：无第二份按 Agent 版本登记的公共 wire 表

- [AgentToolProfile.wire_versions](../../../../app/runtime/agent_registration.py) 第 43 行是当前 Agent 的版本允许表；[ToolGateway._tool_wire_version](../../../../app/runtime/tool_gateway.py)（第 732 行）先按 `(agent_id, business_type)` 查可信档案，再按 `agent_version` 查该档案的表。未登记版本抛 `TOOL_WIRE_VERSION_INVALID`，不借用其他 Agent 的同版本。
- 回忆录的版本映射现位于 [memoir_agent/registration.py](../../../../app/agents/memoir_agent/registration.py) 第 43 行 `_MEMOIR_WIRE_VERSIONS`，通过第 102 行传入自己的 profile；它不是需要为 soothe 再登记的第二份表。[QA registration](../../../../app/agents/qa_agent/registration.py) 第 30 行独立声明 `1.0.0 → 1.1.0`。`app/` 下已没有 `_TOOL_WIRE_VERSION_BY_AGENT_VERSION`；测试中的同名局部变量只是 Memoir profile 的别名，不是额外登记源。
- `_DEFAULT_TOOL_WIRE_VERSION` 仅服务空 context 的 MockTransport 兼容路径；`TOOL_ERROR_SPECS_BY_WIRE_VERSION` 按 wire 定义冻结错误契约，二者都不是新 Agent 的版本登记表。
- **实测：** QA 1.0.0 取 `1.1.0`，Memoir 1.0.0 仍取 `1.0.0`；既有第二档案隔离/未知版本拒绝测试通过。全 `app/` 检索未发现旧全局表。
- **后续登记位置：** 只需在 R4 新建的 `app/agents/soothe_agent/registration.py` 中给共享 profile 设置 `wire_versions={"1.0.0":"1.1.0"}`，让两个业务类型都指向它，再在 `app/agents/registry.py` 加固定 import 与清单登记。没有 R4 Step 3 需要额外修改的 wire 表，不改 Memoir、QA 或公共 ToolGateway。

### 2026-11-02｜G0 审阅结论（Claude）

- **结论：通过。** 抽查 `agent_package_service._validate_node_prompt_refs`、`authorization._require_allowed`、`ToolGateway._tool_wire_version` 与全 `app/` 检索，四条结论与源码一致；保留 `soothe_agent@1.0.0` 单包、两种业务类型方案，设计说明与共享契约无需修订。
- **执行要求补充：**
  1. 授权字段缺省即不限制（兼容旧本地配置），因此 env 模板与部署说明中必须给出包含 `soothe_agent`、两种业务类型、`couple_soothe`、`soothe_callback` 的**完整显式列表**，不得省略字段。
  2. `reject_unsafe_phrases` 只是声明；R3 的 `validation.py` 与 R6 Runner 调用才是实际生效点，R7 端到端必须覆盖禁用短语导致 Run 失败。
  3. 业务端创建 Run 必须显式传 `data_domain=couple_soothe`（契约默认值是 `couple_memory`）。
- **规范修正：** R1 勾选原为 `[x]`，已按项目规范改为 `[✅]`；后续统一用 `[✅]`。
- **下一步：** 可以开始 R2–R8（M1），并与业务端 M2、M3 并行。

