# Agent 接入指南

新增 Agent 以 [QA Agent](qa_agent/) 为参考：业务后端持有会话和业务正文，每轮创建一个异步 Run，Runner 经受控工具读取已授权数据、调用模型并提交结果。公共 Runtime 负责调度、lease、预算、授权复核、checkpoint 和 callback。

本指南对应已实现的注册收口，设计理由和能力边界见 [08-多 Agent 扩展性与目录评审](../../头脑风暴/docs/hucci-agent设计思路/08-多Agent扩展性与目录评审.md)。历史版本包不可原地修改；新增业务不改现有 Memoir/QA 包、digest 或冻结契约。

## 1. 目录与职责

```text
app/agents/<agent_id>/
  registration.py       # 可信身份、工具权限、输出字段与延迟工厂加载器
  bootstrap.py          # 校验本 Agent 配置，装配每次派发的新执行器
  runner.py             # WorkflowNodeRunner.run_node 节点执行
  state.py              # 有私有工作态时定义，参考 QA；仅在内存使用
  tools.py              # 有业务工具时定义固定档案、可信引用提取与提交适配
  model_gateway.py      # 有模型节点时适配公共 ModelGateway
  <version>/            # 精确版本的不可变 AgentPackage
```

`registration.py` 导出中立协议 [AgentRegistration](../runtime/agent_registration.py) 的实例 `REGISTRATION`，包含：

- `agent_id`：与目录、包身份一致。
- `tool_profiles`：按 `business_type` 登记 `AgentToolProfile`；工具名称、前缀、connector、方法、路径、输入槽、副作用、取消语义、wire 版本均来自可信代码。
- `tool_output_fields`：工具允许写入的业务字段集合。
- `executor_factory_loader`：无参函数，在函数体内导入本 Agent 的 `bootstrap`，返回可调用工厂；不要在模块顶层加载 Worker 执行依赖。

[QA registration](qa_agent/registration.py) 登记 `qa_agent` / `business_qa`，工具前缀 `qa.`，wire 为 `1.1.0`；[QA tools](qa_agent/tools.py) 从可信运行上下文提取 `conversation_id`、`turn_id`、`run_id`，将业务身份绑定到 `turn_id`。

`bootstrap.py` 的工厂遵守 `ExecutorFactory` 调用协议，返回实现 `run` / `resume` 的 [RunExecutor](../runtime/interfaces.py)。参考 [QA bootstrap](qa_agent/bootstrap.py)，调用 [Worker 公共构建函数](../worker.py) `build_tool_gateway`、`build_model_gateway`、`build_checkpoint_store`，保留授权、lease、取消、draining、预算及发送前后的复核。节点执行参考 [QA runner](qa_agent/runner.py)，模型适配参考 [QA model_gateway](qa_agent/model_gateway.py)。

公共 `runtime/`、`schemas/`、`worker.py`、`api/`、`core/` 和 `services/`（`memoir/` 子目录除外）不能导入具体 Agent，包括函数体内的延迟导入。具体 Agent 的依赖留在自身目录和可信注册表。

## 2. 版本包必须提供什么

以 [QA 1.0.0](qa_agent/1.0.0/) 为最小参考；加载规则以 [AgentPackageService](../services/agent_package_service.py) 和 [包 schema](../schemas/agent_package.py) 为准，不复制加载逻辑。

| 文件 | 用途 |
| --- | --- |
| `agent.yaml` | 身份、精确版本、契约版本、生命周期、允许业务类型、有限预算与 Prompt 清单 |
| `input.schema.json` | Run 输入结构；QA 仅接收业务引用，不接收问题正文 |
| `output.schema.json` | Agent 输出结构 |
| `workflow.graph.py` | 静态字面量 `WORKFLOW_NODES`；加载时用 AST 解析，不执行任意 Python |
| `tools.manifest.json` | 工具声明，不能扩大代码档案权限 |
| `guardrails.yaml` | 护栏声明 |
| `callbacks.yaml` | callback 能力；人工等待节点必须启用对应 callback |
| `ui-trace.yaml` | 安全进度展示声明 |

以上 8 项之外，必须有可解析的 `evals/minimal.jsonl`，至少 5 条非空 JSON 用例。`agent.yaml` 声明的每个 Prompt 必须是 `prompts/` 中实际存在的版本化 `.md` 文件；模型/护栏节点的 `prompt_ref` 必须在该清单中。没有 Prompt 声明时不要求空目录。

`agent.yaml` 的 `agent_id` / `version` 必须与目录精确一致，`contract_version` 必须兼容。加载必须指定精确版本，禁止 `latest`。同一身份和版本的 digest 不可变，内容变更应新增版本。QA 的静态四节点为读取上下文、生成回答、校验回答、提交回答，均声明安全重跑。

## 3. 可信注册步骤

1. 在新 Agent 目录完成上述实现和版本包，导出 `REGISTRATION`。
2. 编辑 [registry.py](registry.py)：新增固定 import，再在 `REGISTRATIONS` 元组加一行。以已有 QA 为例：

   ```python
   from app.agents.qa_agent.registration import REGISTRATION as QA_REGISTRATION
   ```

   ```python
   REGISTRATIONS: tuple[AgentRegistration, ...] = (
       MEMOIR_REGISTRATION,
       QA_REGISTRATION,
   )
   ```

   “注册表加一行”指清单中的一行登记；当前实现还需上述固定 import，因此同一文件实际有两处编辑。身份集合、工具档案、输出字段和工厂查询均从该清单派生，不另建登记表。重复 `agent_id` 在导入阶段报错。
3. 通过包加载、注册门禁和本 Agent 测试后，按项目既有包注册流程注册精确版本；代码中的可执行身份登记不等于数据库里已注册包，也不授予调用方权限。

不要从 Package、请求、模型输出或环境变量读取 Python 工厂路径、导入路径或注册描述。新 Agent 无需在 `worker.py` 或其他公共模块追加分派分支。

## 4. 部署配置

完整说明见 [ENV_CONFIG.md](../../ENV_CONFIG.md)。新增配置合并进现有对象，保留其他 Agent 的配置；真实地址和凭据只放部署环境或本地私有环境文件。

- `RUNTIME_BUSINESS_CONNECTORS_JSON` 登记可信档案指定的 connector。QA 要求 `qa_business_backend` 启用，且 `base_url`、`runtime_id`、`key_id`、`secret` 为非空字符串；`base_url` 只填服务 origin，工具路径由代码固定。QA 当前以 mock 业务服务验证，不能据此声称已接真实业务。
- `RUNTIME_TRUSTED_CLIENTS_JSON` 为调用方显式授权 `agent_ids`、`business_types`、`connector_ids`、`data_domains` 和 `callback_target_ids`；callback 目标还须在 `RUNTIME_CALLBACK_TARGETS_JSON` 独立登记并启用。
- 新 Agent 使用 `AGENT_MODEL_NODE_ROUTES_JSON`，格式为 `{"<agent_id>":{"<node_id>":"<route_id>"}}`；工厂调用 `Settings.agent_model_node_routes(agent_id)`。公共解析器校验完整嵌套字符串对象，缺 Agent 键返回 `{}`；本 Agent 的工厂负责精确校验自身节点集合及路由可用性。非法配置只返回受控错误，不回显配置值。

QA 的映射示例（`qa-mock` 须先在 `MODEL_ROUTES_JSON` 登记并启用）：

```dotenv
AGENT_MODEL_NODE_ROUTES_JSON={"qa_agent":{"compose_answer":"qa-mock"}}
```

QA 的节点键集合必须恰好为 `compose_answer`。模型网关还依赖 Redis、模型能力/策略和路由要求；Provider Key 经 `MODEL_PROVIDER_API_KEYS_JSON` 注入。`MEMOIR_MODEL_NODE_ROUTES_JSON` 维持 Memoir 既有约定。改配置后重启 Worker。

新 Agent 应像 QA 一样，在必需 connector、模型路由或公共网关缺失时返回明确终态失败执行器，交由队列和 lease 正常收敛，不能返回 `None` 退回占位执行器。QA 返回 `failed + QA_AGENT_NOT_CONFIGURED`，无模板 fallback；运行时模型不可用也明确失败，不提交占位回答。

## 5. 状态、隐私与副作用

- 私有业务状态定义在本 Agent 的内存模型中，参考 [QaWorkingState](qa_agent/state.py) 的 `extra="forbid"` 和严格校验。问题、证据、回答、Prompt、模型原文、工具载荷、凭据和私有 URL 不进入 Store、日志、trace、callback、审计、artifact、checkpoint、错误文本或测试输出。
- 工具结果通过 [apply_safe_output](../runtime/state.py) 写入指定内存模型，显式传入该 Agent 白名单；递归拒绝控制字段和敏感键，候选模型校验成功后才修改状态。`tool_output_fields` 只声明允许的工具输出目标，**注册不会给公共 `AgentState` 自动添加字段，也不会把私有工作态纳入 checkpoint**。有新业务字段时由自身 Runner 持有并安全写入，参考 QA，不调用公共状态去接收未知字段。
- [执行器 checkpoint 白名单](../runtime/executor.py) 仅允许 `completed_steps`、`completed_node_ids`、`fallback_flags`、`resume_from_node_id` 这些路由元数据。加密不构成保存正文的理由。恢复需要重新读取/计算私有内容，重跑安全性由 Agent 自己证明；QA 恢复时重跑整链，再以稳定键对账。
- 启用的 HTTP 工具必须命中本 Agent / 业务类型的可信档案，不能跨前缀、connector 或 wire 兜底。保留 ToolGateway 的统一输出校验和发送前复核，不能直接发 HTTP 绕过网关。禁用的工具声明只是包内预留，不会被执行，也不需要登记到档案，但仍须符合本 Agent 的工具前缀和 `output_to` 允许字段。
- 副作用使用不含物理 attempt 的稳定逻辑幂等键，提交前登记审计，并按原键查询已提交结果。QA 用 `run_id + turn_id` 等稳定字段派生键；结果未知时先查询，已提交复用结果；有未知历史 attempt 且查询未见提交时保守失败，不盲目重发。相同键不能通过改变正文绕过摘要一致性检查。
- 模型输入经 ContextManager 区分可信指令与不可信数据，限制上下文/token/成本；输出经结构化解析和业务引用校验后才能提交。异常只传播固定错误码和安全身份，不记录上游异常原文。

## 6. 接入验证

每个新 Agent 必须补自己的包、状态、Runner、工具、模型适配、工厂和端到端测试。QA 参考：[包与工具](../../tests/test_qa_package_tools_mock.py)、[Runner 与隐私](../../tests/test_qa_runner.py)、[模型适配](../../tests/test_qa_model_gateway.py)、[缺配置工厂](../../tests/test_qa_factory.py)、[E1–E10 端到端](../../tests/test_qa_end_to_end.py)。至少验证正常/无证据、非法引用不提交、已提交响应丢失后的幂等恢复、撤权、取消、预算耗尽、配置缺失终态，以及解密 checkpoint 与所有输出中无正文。

注册收口护栏从仓库根目录执行：

```bash
poetry run pytest -q tests/test_agent_architecture_boundaries.py tests/test_agent_registration_consistency.py
```

架构测试扫描所有指定公共目录的 AST，禁止导入任何具体 Agent。注册一致性测试遍历可信清单：至少一个包版本且全部可加载；启用的 HTTP 工具在本 Agent 档案内，所有 HTTP 工具均符合前缀；所有 `output_to` 在允许字段内；工厂可解析为可调用对象；声明工具时每个允许业务类型都有对应档案。包加载不能替代这些跨注册项检查。

交付前还需全量回归、Ruff、Mypy 不新增错误、diff 和文档本地链接检查，并核对所有既有版本包 digest 不变：

```bash
poetry run pytest -q
poetry run ruff check .
poetry run mypy app
git diff --check
```

这些是接入要求，不是本指南宣称已通过的结果；实际记录与沙箱/DSN 限制见 [VERIFICATION.md](../../VERIFICATION.md)。

## 7. 当前能力边界

当前可复用 QA 模式做异步问答，也支持静态线性工作流、受限 `bounded_loop` 和人工等待。没有面向实时对话的流式输出或低延迟执行通道，没有条件边或动态工具选择循环。TTS/音频服务仍属 Memoir，尚无可供任意 Agent 直接启用的公共 TTS。注册新 Agent 不会增加这些执行能力；需真实业务需求和单独设计后再扩展，见 [08 文档](../../头脑风暴/docs/hucci-agent设计思路/08-多Agent扩展性与目录评审.md)。
