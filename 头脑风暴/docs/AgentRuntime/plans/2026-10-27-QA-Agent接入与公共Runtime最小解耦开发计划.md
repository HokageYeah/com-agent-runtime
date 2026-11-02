# QA Agent 接入与公共 Runtime 最小解耦开发计划

编写日期：2026-10-27。代码基线：分支 `hucci-agent`，HEAD `a477390`。
设计依据：[07-06审查稿的ClaudeCode审核结论](../../hucci-agent设计思路/07-06审查稿的ClaudeCode审核结论.md)（以下简称 07）。

本计划由 Claude Code 编写和审核，由 Codex 按任务执行。每个任务完成后先交 Claude Code 审核，审核通过才能开始下一个任务。

## 0. 已确认的需求决策（2026-10-27，用户确认）

| 决策项 | 结论 | 对计划的影响 |
| --- | --- | --- |
| 交互方式 | **异步 Run**：业务后端创建 Run，Worker 执行，结果通过 callback 或轮询通知 | 复用现有 Run/Worker/callback 链路；不做同步接口和 SSE，`native_sse` 保持 `False` |
| 数据范围 | **先使用 mock 业务服务** | com 侧完成解耦和 QA 全链路；业务工具由仓库内 mock 服务实现，不改动任何真实业务仓库或业务库 |
| 回答交付 | **提交工具写回业务端** | 最后一个节点调用带幂等键的“提交回答”工具，按 query-after-commit 语义执行；回答正文不进入 Run、Artifact、checkpoint、日志、callback |
| 代码管理 | **当前分支 `hucci-agent`，不提交** | 执行方不创建分支或 worktree，不 commit、不 push；何时提交由用户决定 |
| 能力发现 | 首版**不改** capabilities 接口 | `capabilities_api.py` 保持不变，按 07 列为按需事项 |

## 1. 目标与非目标

**目标**

1. 公共 Runtime 只解耦阻塞第二个 Agent 的地方：执行器选择、工具网关、工具输出目标、模型网关装配、上下文预算。Memoir 的行为、包内容和 digest 不变。
2. 新增 `qa_agent` 1.0.0。它用固定工作流回答一个业务问题：读取问题和已授权证据 → 模型生成带引用的回答 → 校验 → 幂等提交给业务端。
3. 用 mock 业务服务和 mock 模型 Provider，跑通 QA 的创建、执行、提交、callback、恢复、取消和撤销授权，并完成验收。

**非目标（本计划不做）**

- 全局目录重组；`app/runtime/`、`services/memoir/`、`models/memoir/` 原地保留。
- 动态工具循环、多步检索、对话压缩/分叉、长期记忆、MCP、多 Agent、代码沙箱。
- 同步接口、流式输出、capabilities 契约变更。
- partial 判定通用化、`bounded_loop` 改造、迁移 Memoir 守卫/评价器。
- 数据库迁移、新表；接入真实业务系统；部署。

## 2. 全局约束（每个任务都必须遵守）

1. **Memoir 零回归**：`app/agents/memoir_agent/1.0.0`–`1.0.8` 的包文件不改，digest 不变；对外 API/Event/Tool wire 不变。
2. **安全门禁只能收紧，不能放松**：lease/fencing、privacy/authorization 复核、取消、预算、发送前/返回后复核、控制字段与敏感键拒绝、checkpoint 白名单（`_SAFE_CHECKPOINT_KEYS`）全部保留。
3. **可信注册只来自代码**：Agent 身份、工具路由、connector、输出目标、预算表都由本仓库代码中的常量决定，不从 Package、请求或模型输出读取。
4. **隐私边界**：问题、证据、回答、prompt、模型原文、工具载荷、凭据、私有 URL 不进入 Store、日志、trace、callback、审计、artifact、checkpoint、错误文本、测试夹具或测试输出。
5. **数据库**：不新增 Alembic 迁移；业务库 `couple_diary_*` 不得改动；测试只用 Runtime 测试库或 SQLite。
6. **Git**：保留脏工作区；禁止 `git reset/checkout/stash/commit/push`，禁止删除文件以外的历史清理；不创建分支或 worktree。
7. **编码规范**：遵循 `AGENTS.md`、`.codex/rules/AI通用编码与协作规范.mdc`；新增核心逻辑、字段、状态转换要有中文注释；日志只记录安全元数据。
8. **TDD**：先写会失败的测试，再做最小实现。重构类任务先补齐能锁住现有行为的测试。
9. **不顺手改动**：与当前任务无关的代码、注释、格式一律不动；发现的问题写进回执的“遗留事项”。

## 3. 任务总览

| 任务 | 内容 | 依赖 | 是否改代码 |
| --- | --- | --- | --- |
| T0 | 基线核验 | — | 否 |
| T1 | 可执行 Agent 注册表 + active 启用门禁 | T0 | 是 |
| T2 | ToolGateway 按 Agent 分组注册 | T1 | 是 |
| T3 | 输出目标包级校验 + 安全写入函数抽取 + 上下文预算参数化 + 模型网关装配抽取 | T2 | 是 |
| T4 | QA Agent 1.0.0 实现（包、Runner、状态、工具、mock 服务、Worker 工厂） | T3 | 是 |
| T5 | QA 端到端与故障场景验收 + Memoir 全量回归 | T4 | 主要是测试 |
| T6 | 文档与验证记录收尾 | T5 | 仅文档 |

任务必须串行执行：T1–T3 都会修改公共边界，并行会互相冲突。单个任务内部可以用多智能体并行（例如一个写测试、一个查调用方），但同一文件同一时间只能有一个写入者。

## 4. 任务详情

### T0 基线核验（只读）

**目的**：在改动前记录当前测试与静态检查的真实状态，后续回归以此为准。

**步骤**

1. `git status --short`，记录当前未提交文件（应只有本目录及 hucci-agent设计思路 下的文档）。
2. 运行并记录结果（命令、通过/失败数量、失败用例名）：
   - `poetry run pytest -q`
   - `poetry run ruff check .`
   - `poetry run mypy app`
   - `poetry run alembic heads`
3. 若某些测试依赖 Docker/PostgreSQL/Redis 而当前环境不可用，记录被跳过或失败的用例清单及原因，不要尝试修复。
4. 计算并记录 Memoir 1.0.0–1.0.8 各包的 `package_digest`（用 `AgentPackageService.load`），作为后续“digest 不变”的对照基线。

**验收**：回执里有完整的基线表和 digest 列表；没有任何文件改动。

**T0 结果（2026-10-27，已审核通过）**

| 检查 | 基线 |
| --- | --- |
| `poetry run pytest -q` | 1267 passed，1 failed，28 skipped。唯一失败 `test_runtime_process_harness.py::test_api_listener_uses_kernel_port_and_is_listening_before_handoff` 由 Codex 沙箱禁止绑定回环端口导致；审核方在沙箱外重跑该文件 21 passed。另有 8 个因同一原因跳过，20 个因未配置 PostgreSQL/MySQL/Redis DSN 跳过 |
| `poetry run ruff check .` | 通过 |
| `poetry run mypy app` | 19 个错误、7 个文件（均为历史问题，本计划不修复；后续任务**不得新增**） |
| `poetry run alembic heads` | 单 head `20260907_1000` |

Memoir digest 基线：

| 版本 | package_digest |
| --- | --- |
| 1.0.0 | `sha256:a6e2f53e223658fb648026335373d23f548232e5dd2c4c67a2c774df6e67833e` |
| 1.0.1 | `sha256:e92ae977220e02f3956d821b0c5ff6adc2320359970d9989863324ab11349c06` |
| 1.0.2 | `sha256:bd20469b0d205242b2639aaa039858495e42dd704036ec8be8b82bfb8ac2f379` |
| 1.0.3 | `sha256:c9b3936c9ca11e3e388a8cdba3f10e04a43bdaa998775c47da512c378c379f9c` |
| 1.0.4 | `sha256:c99d171acf1def1dc1eecbebe62e24643904e849d4d0544248ee3fcea48908bb` |
| 1.0.5 | `sha256:7cb0c8d7457e7ccc643108079a3fcc725ebee5e2557fa2b7f065454dc411dbe2` |
| 1.0.6 | `sha256:94456a80237bafd81b802a9eccf981ad9f0a46dd96083b6c916dbbab29c318bf` |
| 1.0.7 | `sha256:b5b06cbfae816372efcd567d2fe2182bccb7013cd0fc0d666c75c4b83b9502ba` |
| 1.0.8 | `sha256:da05c6c7f83f209f76f0ecbf5fc555fbeb7d90d4cd119f7d3be3b938b645848d` |

**环境说明**：Codex 在 workspace-write 沙箱内运行，不能绑定回环端口。需要回环端口的测试（process harness、delayed provider mock、loopback mock 服务）由审核方在沙箱外补跑并计入验收。

---

### T1 可执行 Agent 注册表 + active 启用门禁

**问题**：`app/worker.py:408` 只认 `memoir_agent`，其他 Agent 一律退回 `BootstrapExecutor`，返回 `pending + WORKFLOW_EXECUTOR_NOT_CONFIGURED`。注册脚本也不检查 Agent 有没有真实执行器。

**改动**

1. 新增 `app/agents/registry.py`（轻量模块，不导入 Worker 依赖）：
   - 常量 `EXECUTABLE_AGENT_IDS: frozenset[str]`，本任务只包含 `"memoir_agent"`（`qa_agent` 在 T4 加入）。
   - 函数 `is_executable_agent(agent_id: str) -> bool`。
   - 执行器工厂的查找：`agent_id → 工厂可调用对象`，延迟导入，映射只在代码里写死。
2. `app/worker.py::configured_executor`：把 Memoir 的装配逻辑原样搬成 Memoir 工厂函数，`_Executor` 通过注册表找工厂。
   - 找不到工厂时，保持现有行为（退回 `BootstrapExecutor`），以免影响 `tests/` 中 13 处使用 `agent_id="agent"` 的历史用例。
   - Memoir 工厂在 connector 配置不完整时返回 `None` 的现有回退逻辑不变。
   - 每次调用都新建 Runner 和执行器，不在注册表里缓存实例。
3. `app/scripts/register_agent_package.py`：以 `status=active` 注册时，如果 `agent_id` 不在 `EXECUTABLE_AGENT_IDS` 中，拒绝并给出明确错误。`agent_runtime_cli.py` 的 register 子命令通过子进程复用这个脚本，不需要单独改。

**测试（先写）**

- 注册表：`memoir_agent` 可执行；未知 Agent 不可执行。
- Worker：`memoir_agent` 仍拿到 WorkflowExecutor；未知 Agent 仍退回 BootstrapExecutor（锁住历史行为）。
- 注册脚本：未登记的 Agent 以 active 注册被拒绝；非 active 状态（`deprecated`/`revoked`，生命周期只支持这三种）不受影响；`memoir_agent` 注册行为不变。

**验收**

- 上述新测试通过；`tests/test_agent_runtime_cli.py`、与 worker/注册相关的现有测试通过。
- `poetry run ruff check .`、`poetry run mypy app` 不新增错误。
- Memoir 执行路径的行为无变化（以现有 Worker/Executor 测试为准）。

---

### T2 ToolGateway 按 Agent 分组注册

**问题**（`app/runtime/tool_gateway.py`）：

- `:46` `_FIXED_HTTP_TOOLS` 只有 `memory.*` 两个工具；
- `:229` 对 `memory.get_snapshot` 做了输入特判；
- `:328` `build_tool_context` 与 `:818` `_validated_tool_context` 硬性要求 `agent_id == "memoir_agent"`、`business_type == "couple_memory"`，`:816` 要求 `business_id == archive_id`，`:820` 要求工具名以 `memory.` 开头；
- `:713` `_trusted_references` 强制要求 `archive_id/snapshot_id/generation_epoch`；
- `:93` `_TOOL_WIRE_VERSION_BY_AGENT_VERSION` **只按版本号查表**。QA 的 1.0.0 会命中 Memoir 1.0.0 的旧 wire `1.0.0`，这是 07 没有列出的耦合，本任务一并解决。

**改动**

1. 定义受信的 Agent 工具档案（例如 `AgentToolProfile`，名称由实现决定），以 `(agent_id, business_type)` 为键。每个档案包含：
   - 固定 HTTP 工具注册表（等价于现在 `_FIXED_HTTP_TOOLS` 的条目）；
   - 工具名前缀；
   - 可信参考字段的提取规则（等价于现在 `_trusted_references`）；
   - 工具上下文中 `business_id` 对应的可信输入字段（Memoir 为 `archive_id`）；
   - `agent_version → wire 版本` 表。
2. 本任务只登记 Memoir 档案，内容与现有常量逐项一致。`_TOOL_WIRE_VERSION_BY_AGENT_VERSION` 原表迁入 Memoir 档案。
3. `build_tool_context`、`_validated_tool_context`、`call`、`_trusted_references`、`_tool_wire_version` 改为按档案判断；没有档案的 Agent 一律拒绝，错误码沿用现有的 `TOOL_CONTEXT_TRUST_INVALID` / `TOOL_MANIFEST_NOT_ALLOWED` / `TOOL_WIRE_VERSION_INVALID`。
4. Memoir 专用方法（`get_snapshot`、`publish_playback_document`、`get_publish_result` 等）保持签名和行为不变。
5. HTTP 发送、签名、peer 校验、lease/fencing、授权复核、幂等、`_validate_output` 都留在公共层，不改。

**测试（先写）**

- 现有 ToolGateway、工具安全、tool context、wire 版本相关测试全部保持通过（这是本任务的主要回归面）。
- 新增：在测试中注入一个伪造的第二档案，验证：
  - 第二档案的 Run 能构造工具上下文，并且只能调用自己档案内的工具；
  - 第二档案调用 `memory.*` 工具被拒绝；Memoir 调用第二档案的工具被拒绝；
  - 第二档案 `1.0.0` 的 wire 版本取自自己的表，不会落到 Memoir 的 `1.0.0`；
  - 未登记的 `(agent_id, business_type)` 被拒绝。

**验收**

- 新增测试和全部现有 ToolGateway 相关测试通过。
- 因为 ToolGateway 是共享边界，本任务结束时必须跑**全量** `poetry run pytest -q`，结果与 T0 基线一致（不新增失败）。
- ruff、mypy 不新增错误。

---

### T3 输出目标包级校验 + 安全写入抽取 + 上下文预算参数化 + 模型网关装配抽取

**问题**

- `app/schemas/agent_package.py:110` 要求 HTTP 工具的 `output_to` 必须属于 Memoir 的 `SAFE_TOOL_OUTPUT_FIELDS`；
- `app/runtime/state.py` 的 `AgentState` 设置了 `extra="forbid"`，安全写入逻辑写死在 `apply_tool_output` 中；
- `app/runtime/context_manager.py:10` 的节点 token 上限表只有 Memoir 节点；
- `app/worker.py::configured_model_gateway` 把通用的 `ModelGateway` 构建和 Memoir 专属的节点路由校验、Adapter 包装混在一起。

**改动**

1. **包级 `output_to` 校验**
   - 在 `AgentPackage` 层面新增 validator，按 `agent_id` 从代码常量取允许字段集合；未登记的 Agent 使用原 `SAFE_TOOL_OUTPUT_FIELDS`。
   - `ToolManifest` 单独构造时必须**继续拒绝控制字段**：`tests/test_agent_state_tool_output_safety.py:45` 要求 `output_to="authorization_version"` 在构造时就被拒绝，这条行为必须保留。可行做法是 `ToolManifest` 只拒绝“所有已登记 Agent 允许字段的并集”之外的值，具体由包级再按 `agent_id` 收紧。
   - 本任务只登记 Memoir（字段集合等于现有 `SAFE_TOOL_OUTPUT_FIELDS`）；QA 字段在 T4 加入。
2. **安全写入函数抽取**
   - 把 `AgentState.apply_tool_output` 中的三项检查（目标字段白名单、`_contains_sensitive_tool_key`、Pydantic 重新校验）抽成模块级函数 `apply_safe_output(model, target, output, allowed)`。
   - `AgentState.apply_tool_output` 改为调用它，行为和错误码不变。
   - 校验失败时目标对象不得被部分修改。
   - `ToolGateway.apply_result` 不删除、不修改。
3. **上下文预算参数化**
   - `ContextManager` 允许构造时传入节点上限表，默认值为现有 `_NODE_TOKEN_CAPS`；未知节点仍然拒绝。
   - 依据：QA 在 T4 会复用 ContextManager 做可信指令和不可信证据的分槽，这是实际复用，符合 07 的“复用时再做”。
4. **模型网关装配抽取**
   - 从 `configured_model_gateway` 中抽出通用函数（例如 `build_model_gateway(session, runtime_settings, is_draining, dependencies)`），只负责构建 `ModelGateway`（Redis、限流、用量、策略、guard）。
   - Memoir 的节点路由键集合校验和 `MemoirModelGatewayAdapter` 包装保留在原函数中，行为不变（包括“配置不完整时退回模板”）。

**测试（先写）**

- `ToolManifest` 构造拒绝控制字段（现有测试保持通过）。
- 包级：Memoir 包声明非允许字段被拒绝；伪造第二个 Agent 的字段映射后，Memoir 字段与第二 Agent 字段互相声明被拒绝。
- Memoir 1.0.0–1.0.8 包加载成功，digest 与 T0 基线完全一致。
- `apply_safe_output`：合法写入成功；目标不在白名单、含敏感键、类型不符时抛出原错误码，且对象保持原样。
- `ContextManager`：默认表行为不变；传入自定义表后按自定义表计算，未知节点拒绝。
- 模型网关：Memoir 的模型装配相关测试保持通过。

**验收**：新增测试通过；全量 `poetry run pytest -q` 与 T0 基线一致；ruff、mypy 不新增错误；digest 对照表全部一致。

---

### T4 QA Agent 1.0.0 实现

**身份与常量**

| 项 | 值 |
| --- | --- |
| `agent_id` | `qa_agent` |
| `version` | `1.0.0` |
| `business_type` | `business_qa` |
| connector | `qa_business_backend`（测试和本地联调指向 mock 服务） |
| 工具 wire | `1.1.0` |

**Run 输入**（`input.schema.json`）：只包含业务引用，不包含问题正文。

```json
{ "conversation_id": "string", "turn_id": "string" }
```

工具上下文的 `business_id` 对应 `turn_id`。

**工作流**（静态线性，无可选节点，无 `bounded_loop`）：

| 节点 | 类型 | 说明 | `safe_to_rerun` |
| --- | --- | --- | --- |
| `load_question_context` | tool | 调用 `qa.get_question_context`，从业务端读取问题和**已授权**的证据列表（每条证据带 `source_ref`）。业务端负责授权和检索，com 不拿全量数据再过滤。 | true |
| `compose_answer` | model | 用 ContextManager 分槽：可信指令 + 不可信证据；结构化输出 `{answer, source_refs, no_evidence}`；经 `StructuredOutputParser` 校验，`source_refs` 必须来自本次证据。 | true |
| `validate_answer` | deterministic | 确定性校验：引用非空或 `no_evidence=true`；长度上限；不含控制字段。失败走受控错误，不提交。 | true |
| `submit_answer` | tool | 调用 `qa.submit_answer`（`side_effect=true`，`query_after_commit`），幂等键由 `run_id + turn_id` 等稳定业务字段派生；提交前先用 `qa.get_answer_result` 查询，已提交则复用结果。 | true |

**改动**

1. 包目录 `app/agents/qa_agent/1.0.0/`：按 Memoir 包的结构提供 `agent.yaml`、`input.schema.json`、`output.schema.json`、`tools.manifest.json`、`workflow.graph.py`、`prompts/`、`evals/`、`callbacks.yaml`、`guardrails.yaml`、`ui-trace.yaml`。缺哪个文件会导致加载失败，以 `AgentPackageService` 的校验为准，不多建。预算要有限且偏小（例如 `max_model_calls` 2、`max_steps` 8、`max_run_seconds` 120）。
2. `app/agents/qa_agent/state.py`：`QaWorkingState(BaseModel, extra="forbid")`，字段为 `question_context`、`answer_draft`、`submit_result`，每个字段有中文注释；只在内存中存在，从不落盘。
3. `app/agents/qa_agent/tools.py`：QA 的工具档案（T2 的结构），工具为 `qa.get_question_context`、`qa.submit_answer`、`qa.get_answer_result`；QA 的允许 `output_to` 字段集合（T3 的结构）。
4. `app/agents/qa_agent/runner.py`：实现 `WorkflowNodeRunner.run_node`，按节点分派；用 `apply_safe_output` 写 `QaWorkingState`；通过 ToolGateway 的通用路径或 QA 专用方法调用工具（不得绕过 `_call` 的输出校验和发送前复核）。
5. `app/agents/qa_agent/model_gateway.py`（或在 runner 内）：QA 的模型适配，复用 T3 抽出的 `build_model_gateway`；节点路由来自新配置 `QA_MODEL_NODE_ROUTES_JSON`（`app/core/config.py` 新增，键集合必须恰好为 `{"compose_answer"}`）。
6. 注册：`EXECUTABLE_AGENT_IDS` 加入 `qa_agent`；Worker 工厂登记 QA 工厂。
7. **配置缺失的处理**：QA 工厂在 connector 或模型路由缺失/不完整时，**不得返回 `None` 退回 BootstrapExecutor**。应返回符合 `RunExecutor` 契约的终态失败（`status="failed"`，明确错误码，例如 `QA_AGENT_NOT_CONFIGURED`），由 `RunQueueService.consume` 交给 `LeaseService.finish` 收敛。QA **没有模板 fallback**：模型不可用时同样以明确错误码失败。
8. `app/runtime/mock_business_server.py`：新增 QA 三个工具路由。只保存不可逆摘要（与现有 publish 的做法一致），`/state` 只返回聚合计数；支持“提交后阻塞/释放”的 harness 控制，用于 T5 的“已提交但响应丢失”场景。
9. 配置样例：`ENV_CONFIG.md` 及 `.env` 样例（如有）补 `qa_business_backend` connector 和 `QA_MODEL_NODE_ROUTES_JSON` 的说明，不写真实值。

**测试（先写）**

- 包：`qa_agent` 1.0.0 加载成功，digest 稳定；QA 工具 `output_to` 使用 Memoir 字段被拒绝。
- 状态：`QaWorkingState` 拒绝额外字段；`apply_safe_output` 写入控制字段/敏感键被拒绝且状态不变。
- Runner 单元：每个节点的正常路径；引用不在证据中被拒；`no_evidence` 路径；`validate_answer` 失败不调用提交工具。
- 工具：QA 只能调用 `qa.*`；伪造 `turn_id` 或 `run_id` 的上下文被拒；幂等键在重试时保持不变。
- 工厂：connector 缺失、模型路由缺失时 Run 以 `failed` + 明确错误码结束，不停留在 `pending`。
- 注册：`qa_agent` 现在可以 active 注册。

**验收**：新增测试通过；全量测试与 T0 基线一致；ruff、mypy 不新增错误；Memoir digest 一致；日志与测试输出里检索不到问题、证据、回答正文。

---

### T5 端到端与故障场景验收

**目的**：用 mock 业务服务和 mock 模型 Provider 证明 QA 全链路可用，同时确认 Memoir 没有回归。

**场景（每个都要有自动化测试）**

| 编号 | 场景 | 预期 |
| --- | --- | --- |
| E1 | 正常问答 | Run `succeeded`；mock 服务收到 1 次提交；callback 只含安全状态 |
| E2 | 无证据 | 按 `no_evidence` 提交受控回答或按设计以明确错误码结束（实现时二选一，并在回执中说明） |
| E3 | 模型输出引用了不存在的证据 | 不提交；Run 以明确错误码结束 |
| E4 | 提交成功但响应丢失（mock 阻塞后 Worker 中断，重新执行） | 恢复后按原幂等键查询到已提交结果并复用，mock 提交计数仍为 1 |
| E5 | 执行中撤销授权 | 发送前复核拒绝，工具不再发包 |
| E6 | 执行中取消 | Run `cancelled`，不再提交 |
| E7 | 模型额度耗尽 | 以预算错误码结束 |
| E8 | QA 配置缺失 | Run `failed` + 明确错误码，不停在 `pending` |
| E9 | checkpoint 内容 | 解密后只包含 `_SAFE_CHECKPOINT_KEYS` |
| E10 | 隐私 | 日志、审计、callback、artifact、测试输出中检索不到问题/证据/回答的测试哨兵字符串 |

可复用的设施：`app/runtime/test_harness.py`、`process_harness.py`、`postgres_harness.py`、`mock_provider_server.py`、`mock_business_server.py`。需要 Docker 的 harness，每轮结束后必须 `down -v` 清理。

**回归**

- 全量 `poetry run pytest -q`，与 T0 基线一致。
- `poetry run ruff check .`、`poetry run mypy app`、`poetry run alembic heads`（仍只有一个 head）、`git diff --check`。
- Memoir 1.0.0–1.0.8 digest 与 T0 基线一致。

**验收**：E1–E10 全部有测试且通过；回归全部满足；Docker harness 若环境不可用，回执中明确写出未执行的命令和原因。

---

### T6 文档与验证收尾（仅文档）

1. `VERIFICATION.md`：只写本计划中**真实执行过**的命令、结果和仍需人工验证的项目。
2. `ENV_CONFIG.md`：确认 T4 的配置说明完整。
3. `README.md`：在 Agent 列表或能力说明处补一句 QA Agent（如果 README 有这类列表；没有就不加）。
4. 本计划文件：在第 6 节勾选完成状态，并写明每个任务的验证证据。

**验收**：文档里的命令都能运行；没有写入未验证的结论；`git diff --check` 通过。

## 5. 执行与审核协议

**执行方（Codex）每个任务的回执必须包含：**

1. 任务编号与结论（完成 / 部分完成 / 受阻）。
2. 改动文件清单（新增 / 修改），每个文件一句话说明改了什么。
3. 新增或修改的测试清单。
4. 实际执行的验证命令和结果（通过/失败数量；失败列出用例名）。
5. 与 T0 基线的对比（是否新增失败）。
6. 没有执行的验证及原因。
7. 遗留事项、发现的计划外问题、需要用户决策的问题。

**审核方（Claude Code）**：

- 阅读 `git diff` 和新增测试，必要时重跑关键命令。
- 结论只有“通过”或“需修改”。需修改时给出具体问题和修改要求，执行方在同一任务内修复后再次回执。
- 审核通过后才能开始下一个任务。

**遇到以下情况，执行方必须停下来回执，不能自行决定**：

- 需要修改 Memoir 包文件、冻结契约、数据库迁移；
- 需要放松任何安全检查；
- 计划与源码冲突，或按计划实现会破坏现有测试；
- 全量测试出现与 T0 基线不一致、且无法确认是否由本任务引起的失败。

### 5.1 本次执行方式（仅适用于本计划）

| 项 | 约定 |
| --- | --- |
| 执行方 | Codex，程序路径 `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`，工作目录为本仓库根目录 |
| 模型与思考强度 | `-m gpt-6.1-sol -c model_reasoning_effort='"high"'`，每次 `exec` 和 `exec resume` 都显式传入。**只对本计划的 T0–T6 生效**；本计划结束后，该项目使用什么模型和强度由用户自行决定 |
| 沙箱 | `-s workspace-write`，`approval_policy="never"`；不使用 `danger-full-access` |
| 会话粒度 | 每个任务新开一个 Codex 会话；审核要求修改时，用 `codex exec resume <会话ID>` 在同一会话内继续 |
| 多智能体 | 允许 Codex 在单个任务内部使用多智能体，但同一文件同一时间只能有一个写入者 |
| 回执 | Codex 的最终消息按第 5 节格式输出；审核方读取回执、`git diff` 和测试结果 |
| 沙箱外补测 | Codex 沙箱不能绑定回环端口。依赖回环端口的测试由审核方在沙箱外补跑，结果计入该任务的验收 |
| Git | 全程不提交、不推送、不建分支；何时提交由用户决定 |

**单个任务的循环**：审核方下发任务 → Codex 执行并回执 → 审核方检查回执、diff 和测试（必要时沙箱外补测）→ 需修改则 resume 同一会话继续，通过则勾选第 6 节并下发下一个任务。遇到需要用户决策的问题，审核方暂停循环并询问用户。

## 6. 进度

- [x] T0 基线核验（2026-10-27 审核通过）
- [x] T1 可执行 Agent 注册表 + active 启用门禁（2026-10-27 审核通过：全量 1293 passed / 1 沙箱失败 / 28 skipped，mypy 仍 19 个历史错误；审核方沙箱外补跑相关与回环测试 114 passed）
- [x] T2 ToolGateway 按 Agent 分组注册（2026-10-27 审核通过：全量 1308 passed / 1 沙箱失败 / 28 skipped，mypy 无新增，Memoir digest 9/9 一致；审核方沙箱外补跑 193 passed。遗留：`call()` 在“工具名合法但缺 tool_context”时错误码由 `TOOL_CONTEXT_INVALID` 变为 `TOOL_MANIFEST_NOT_ALLOWED`，仅内部诊断码、无外部消费者，不影响行为）
- [x] T3 输出目标包级校验 + 安全写入抽取 + 上下文预算参数化 + 模型网关装配抽取（2026-10-27 审核通过：全量 1331 passed / 1 沙箱失败 / 28 skipped，mypy 错误集合不变，Memoir digest 9/9 一致；审核方沙箱外补跑 112 passed）
- [x] T4 QA Agent 1.0.0 实现（2026-10-27 审核通过：沙箱内全量 1388 passed / 1 沙箱失败 / 28 skipped；审核方沙箱外全量 **1397 passed / 0 failed / 20 skipped（仅缺 DSN）**；mypy 无新增；Memoir digest 9/9 一致；qa_agent 1.0.0 digest `sha256:9513dc262c64832ad2d4c6fa564459be79d47bdeeb6bf42797efe48f51ef6466`。计划外但必要的公共改动：`executor.py` 新增只接受固定枚举码的 `WorkflowNodeError`，Memoir 原异常路径不变；`error_code` 在契约中为自由字符串，无兼容风险。实现选择：无证据时提交 `no_evidence=true` 的受控回答；提交结果未知且查询为未提交时保守失败、不重发）
- [x] T5 端到端与故障场景验收（2026-10-27 审核通过：新增 `tests/test_qa_end_to_end.py` 20 项覆盖 E1–E10；审核方沙箱外全量 **1417 passed / 0 failed / 20 skipped（仅缺 DSN）**，ruff、`git diff --check` 通过；Memoir 与 QA digest 不变。修复缺陷 1 处：QA 模型调用输入 token 估算为 0 导致 token 预算失效，已按 Memoir 同样方式设置估算下限）
- [x] T6 文档与验证收尾（2026-10-27 审核通过：VERIFICATION、ENV_CONFIG、README 已补齐；QA 五文件 80 passed，ruff 通过，mypy 仍 19 个历史错误，Alembic 单 head，`git diff --check` 与新增本地链接检查通过）
