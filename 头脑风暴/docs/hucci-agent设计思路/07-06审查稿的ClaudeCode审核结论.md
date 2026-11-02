# 06 审查稿的 Claude Code 审核结论

审核日期：2026-10-27。代码基线：HEAD `a477390`，工作区仅有本目录文档变动。审核对象：[06-com下一阶段优化与业务问答Agent-ClaudeCode审查稿](06-com下一阶段优化与业务问答Agent-ClaudeCode审查稿.md)。

本文只是审核结论，没有修改代码、数据库、开发清单或 Git 状态，也没有运行测试。下文的结论依据只有两类：当前源码的阅读结果，以及调用方检索结果。

## 0. 结论

**修改后采用。** 06 中所列 7 个问题的现象基本属实，但严重程度没有分层：

- 真正阻塞 QA Agent 接入的只有两处：
  - **ToolGateway 拒绝所有非 Memoir 的 Run**；
  - **Worker 只装配 Memoir 执行器**。
  另外，工具结果的写入目标白名单是次一级阻塞。
- 结构化校验、partial、上下文预算、启动装配，对 QA 首版要么不构成阻塞，要么只需一行参数化即可解决，06 写重了。
- 06 漏掉了三处耦合：能力查询接口、发包时对可选节点的校验，以及 **QA 的交互时延与现有异步 Run 模型是否匹配**。
- 06 第 5 节建议的全局目录重组属于过度设计，不建议执行。

对用户三个问题的直接回答：

1. **com 下一步做什么：**用业务问答 Agent 作为真实的第二个 Agent，只解耦阻塞它接入的公共代码，不做全面重构。
2. **是否按职责调整目录：**只新增 `app/agents/qa_agent/`，再把 Memoir 专属文件就近归位；`app/runtime/`、`services/memoir`、`models/memoir` 原地保留。
3. **能否抽离高耦合业务：**可以，但只抽离阻塞 QA 的两三处，其余等到真实需求出现时再做。

## 1. 事实纠正

| 06 中的说法 | 源码实际情况 | 修正 |
| --- | --- | --- |
| 问题 3：QA 工具无法仅靠增加 manifest 接入 | [tool_gateway.py:328](../../../app/runtime/tool_gateway.py:328) 与 [tool_gateway.py:818](../../../app/runtime/tool_gateway.py:818) 硬性要求 `agent_id == "memoir_agent"` 且 `business_type == "couple_memory"`；[tool_gateway.py:713](../../../app/runtime/tool_gateway.py:713) 强制要求 `archive_id/snapshot_id`；[tool_gateway.py:229](../../../app/runtime/tool_gateway.py:229) 对 `memory.get_snapshot` 做了特判 | **06 低估了。**任何非 Memoir 的 Run 一旦调用工具，就会被直接拒绝。 |
| 问题 4：StructuredOutputParser 调用含回忆录规则的语义校验 | [structured_output.py:41](../../../app/runtime/structured_output.py:41) 确实无条件调用 `SemanticValidator`，但场景、动作、统计规则只在输出出现 `scenes/actions/stats/duration_ms` 键时才触发；控制字段拒绝和 `source_refs` 可信引用校验本身是通用的。`MemoirGuardrails` 与 `MemoirPlaybackEvaluator` 只由 [memoir_agent/runner.py](../../../app/agents/memoir_agent/runner.py) 使用 | **06 高估了。**这不阻塞 QA，只是文件位置不当。QA 输出不使用上述键名，也不输出 `url` 字段。 |
| 问题 5：partial 与循环合并带业务语义 | 只有包里声明了可选节点或 `bounded_loop`，才会进入 [executor.py:490](../../../app/runtime/executor.py:490)、[executor.py:777](../../../app/runtime/executor.py:777)、[agent_run_service.py:439](../../../app/services/agent_run_service.py:439) 这些路径 | QA 首版不用可选节点或循环，就不受影响，可以延后。 |
| 问题 6：上下文预算表识别 Memoir 节点 | `ContextManager` 只在 [memoir_agent/model_gateway.py:66](../../../app/agents/memoir_agent/model_gateway.py:66) 中实例化；[context_manager.py:10](../../../app/runtime/context_manager.py:10) 的节点上限表只影响 Memoir | 只有 QA 复用 ContextManager 时才需要处理，参数化即可。 |
| 问题 7：启动依赖未分离 | 生产环境已经不挂载回忆录路由（[api.py](../../../app/api/api.py) 中的 R1 路由门禁）；`main.py` 仍会装配回忆录依赖 | 现象属实，但不阻塞 QA。06 关于 `MEMORY_SNAPSHOT_FERNET_KEY` 的提醒正确：该密钥同时被 [worker.py:511](../../../app/worker.py:511) 和 [api_app_factory.py:24](../../../app/runtime/api_app_factory.py:24) 用于公共 checkpoint 加密，**不能随意改名或轮换**。 |

### 06 遗漏的耦合与问题

1. [capabilities_api.py:74](../../../app/api/endpoints/capabilities_api.py:74) 固定只加载 `memoir_agent` 的 `1.0.7`、`1.0.8` 两个包。这是对外契约，QA 不会出现在能力列表中。
2. [agent_package_service.py:221](../../../app/services/agent_package_service.py:221) 在发包时校验：只要存在可选节点，就必须存在唯一的 `publish_document`，且可选节点必须位于它之后。它和问题 5 是同一类耦合，位置在发包阶段。
3. **交互时延与运行模型。**com 的 Run 链路是“入队 → Worker 执行 → callback”，而且能力声明中 `native_sse: False`。问答产品通常要求几秒内返回，或者流式输出。这一点不先确定，后续方案可能整体返工。

## 2. 问题清单（按严重程度排序）

| 级别 | 问题 | 证据 | 对 QA 的影响 |
| --- | --- | --- | --- |
| **P0** | ToolGateway 只信任 Memoir：工具上下文校验写死 Agent 与业务类型；`_FIXED_HTTP_TOOLS` 只有 `memory.*`；可信引用写死 archive/snapshot | `tool_gateway.py:46`、`:229`、`:328`、`:713`、`:818` | 阻塞：QA 一调用业务工具就失败。 |
| **P0** | Worker 执行器选择写死 Memoir，其他 Agent 一律退回占位执行器 | [worker.py:408](../../../app/worker.py:408) | 阻塞：QA 没有真实执行器。 |
| **P1** | HTTP 工具的 `output_to` 在包加载时限于 Memoir 的 11 个字段；Executor 固定创建/恢复 `AgentState`，且该类型设置了 `extra="forbid"` | [agent_package.py:110](../../../app/schemas/agent_package.py:110)、[state.py:12](../../../app/runtime/state.py:12)、[executor.py:337](../../../app/runtime/executor.py:337)、[executor.py:350](../../../app/runtime/executor.py:350) | 阻塞：只增加第二套字段白名单仍不能完成 QA 状态接入。 |
| **P1（待确认）** | QA 要求同步返回、流式输出还是异步回调，现有 Run 模型能否满足 | 能力声明 `native_sse: False` | 取决于产品要求，可能影响整体方案。 |
| P2 | 上下文预算表只含 Memoir 节点 | `context_manager.py:10` | 只有复用 ContextManager 时才阻塞。 |
| P2 | 能力查询接口固定为 Memoir 包 | `capabilities_api.py:74` | 只有业务方依赖能力发现时才需要处理。 |
| P3 | Memoir 的守卫、评价器放在公共 `app/runtime/` 目录 | `guardrails.py`、`evaluator.py` | 不阻塞，只影响可读性。 |
| P3 | partial 判定、场景合并、可选节点校验依赖 `publish_document` | `executor.py:490`、`agent_run_service.py:439`、`agent_package_service.py:221` | QA 首版不用可选节点时不阻塞。 |
| 暂缓 | `main.py` 装配回忆录依赖；回忆录历史实现位于 `services/memoir`、`models/memoir` | — | 不阻塞。 |

## 3. 修订后的解耦方案

总原则：公共层通过可信注册表获取各 Agent 的规则，但不导入具体 Agent。注册表在本仓库代码中写死，不从 Package 或请求中读取。Memoir 的现有规则原样迁入注册表。

### 补充核验：实际写入路径

此前把 `ToolGateway.apply_result` 当作 Memoir 实际工具结果写入边界，不准确。当前路径为：

- [runner.py:1033](../../../app/agents/memoir_agent/runner.py:1033) 调用专用 `get_snapshot(...)` 后直接赋值 `state.snapshot`；[runner.py:909](../../../app/agents/memoir_agent/runner.py:909) 同样通过专用发布方法写 `state.publish_result`。
- 网关在 [_call](../../../app/runtime/tool_gateway.py:602) 内调用 `_validate_output`，已有输出安全校验保持不变。
- 模型中间结果主要由 Runner 调用 `state.apply_tool_output(...)` 写入，例如 [runner.py:423](../../../app/agents/memoir_agent/runner.py:423) 起的相关节点。
- 通用 `ToolGateway.call(manifest)` 的应用调用方是 [langchain_tools.py:78](../../../app/runtime/langchain_tools.py:78)；测试中也有直接调用。

`apply_result` 在 `app/` 中没有发现业务调用方，但**不是没有测试调用方**：[test_agent_state_tool_output_safety.py](../../../tests/test_agent_state_tool_output_safety.py:127) 中有 4 处直接调用。保留该方法及既有测试，不删除、不修改，也不再把它当作主要写入路径。

### 最小方案

1. **执行器注册表与启用边界（对应 P0-2）。**新增轻量的 `app/agents/registry.py`，登记可执行的 Agent 身份及延迟加载的工厂映射；API 和注册脚本导入注册表时不拉入 Worker 的依赖。把 `configured_executor` 中 Memoir 的装配逻辑原样搬成 Memoir 工厂，Memoir 的已有回退行为保持不变，不需要新建 `bootstrap/` 目录。

   新 Agent 的 `active` 注册必须先检查是否具有受信执行器登记；没有登记时明确拒绝。已核对 [agent_runtime_cli.py::_register](../../../app/scripts/agent_runtime_cli.py:738) 通过子进程复用 [register_agent_package.py](../../../app/scripts/register_agent_package.py)，优先在这条共享注册路径实现一次校验，不另建一套 CLI 注册逻辑。不能为了这项门禁，在 `AgentRunService.create` 中统一拒绝所有未登记工厂的历史 Agent；现有测试有 13 处使用 `agent_id="agent"` 创建 Run，其既有行为不因本次接入而改变。

   QA 工厂缺少 connector、模型等必需配置时，必须在派发前明确拒绝，或返回符合 `RunExecutor` 契约的终态失败；不能返回 `None` 后退回占位器。当前 [BootstrapExecutor](../../../app/worker.py:190) 返回 `pending + WORKFLOW_EXECUTOR_NOT_CONFIGURED`；[RunQueueService.consume](../../../app/services/run_queue_service.py:47) 已将 `failed` 等终态交给 `LeaseService.finish` 收敛，具体失败实现仍需覆盖该路径。Memoir 配置不完整时的原有回退不在本次修改范围。

2. **工具注册按 Agent 分组（对应 P0-1）。**把 `_FIXED_HTTP_TOOLS` 按 `(agent_id, business_type)` 分组。每组提供三项：工具注册信息、可信参数的提取方式、工具上下文校验规则。Memoir 条目与现有逻辑必须完全一致。HTTP 发送、签名、lease/fencing、授权复核和幂等仍留在公共层，manifest 仍然只是声明，不能借此授予权限。

3. **QA 私有强类型工作状态（对应 P1）。**保持 Executor 和 checkpoint 不变，不采用泛型状态协议，不给 `AgentState` 添加 QA 字段。QA Runner 自行维护强类型工作状态，贯通以下四个环节。

| 环节 | 做法 | 必要改动范围 |
| --- | --- | --- |
| 包加载 | 将业务输出目标的白名单校验移到能取得 `agent_id` 的 `AgentPackage` 层。按代码内固定映射选择允许字段，例如 `qa_agent → QA_OUTPUT_FIELDS`；未登记身份继续使用原 `SAFE_TOOL_OUTPUT_FIELDS`。HTTP 工具必填字段、相对路径等基础校验保留。 | `schemas/agent_package.py`；Memoir 和其他未登记 Agent 的包级字段校验结果保持不变，包文件不改，digest 不变。 |
| 状态创建 | Executor 仍创建 `AgentState`，QA 只使用其 `run_input`、`completed_node_ids`、`fallback_flags` 等公共执行信息。QA Runner 在构造时创建 `QaWorkingState(BaseModel, extra="forbid")`，保存问题、证据、草稿和提交结果。 | 新增 `app/agents/qa_agent/state.py`；不修改 Executor 的状态创建/恢复。 |
| 写入 | QA Runner 调用 QA 专用网关方法，保留 `_call` 的输出安全检查。把 `AgentState.apply_tool_output` 中的目标白名单、敏感键检查与 Pydantic 重新校验抽为模块级 `apply_safe_output(model, target, output, allowed)`；Memoir 原方法委托给它，QA 用它写入自己的状态。 | `runtime/state.py` 仅做行为不变的抽取；不修改 `ToolGateway.apply_result`，允许字段只来自可信代码。 |
| 恢复 | `QaWorkingState` 不落盘，checkpoint 仍只有 `_SAFE_CHECKPOINT_KEYS`。QA 读取和模型节点声明 `safe_to_rerun=True`，恢复时重新获取获授权数据并计算；回答提交节点使用稳定业务逻辑键，先查询已提交结果，按 query-after-commit 语义复用或提交。 | QA 节点定义及 Runner；不增加 checkpoint 内容，不保存问题、证据或草稿。 |

包级校验迁移还须覆盖 `ToolManifest` 单独构造的既有调用方：例如 [现有安全测试](../../../tests/test_agent_state_tool_output_safety.py:45) 要求 `output_to="authorization_version"` 在构造时即被拒绝。保留这种控制字段基础拒绝，不把“业务字段白名单迁到包级”误做成全面放松 `ToolManifest` 校验。

写入时继续保留 lease/fencing、privacy/authorization 的现有门禁。校验失败时先前状态不得被部分修改。QA 工厂须为每个 Run/执行 attempt 创建新 Runner；同次执行的各节点共用其私有状态，不能把 Runner 或 `QaWorkingState` 缓存在跨 Run 的注册表中。

4. **上下文预算表按需参数化（对应 P2）。**只有 QA 实际复用 `ContextManager` 时才允许传入节点上限表；默认值仍为现有表，未知节点仍拒绝。本项不作为 QA 接入前必须实施的独立优化。

### 目录建议

```text
app/agents/
  memoir_agent/     # 已有；可选：迁入 guardrails.py / evaluator.py，
                    # 原路径保留 re-export，避免批量改动测试和脚本的导入
  qa_agent/
    runner.py
    tools.py        # QA 的可信工具注册条目
    1.0.0/          # 新包，名称与版本在接入时确认
```

`app/runtime/`、`app/services/memoir/`、`app/models/memoir/` 原地保留，不建 `legacy/` 和 `adapters/` 目录。原因有三：

- 搬迁千行级公共文件会波及大量导入，`tests/` 下共 113 个测试文件；
- `models` 的路径牵涉 Alembic 迁移谱系；
- 回忆录历史实现已经各自位于 `memoir` 子目录，目录边界已经存在。

这些风险换来的只是目录整齐，当前不值得。

## 4. 最小实施顺序、兼容风险与验收

| 步骤 | 内容 | 验收标准 |
| --- | --- | --- |
| 0 | 冻结 QA 需求（见第 5 节） | 待决策项有书面结论 |
| 1 | 可执行注册表 + 新 Agent 的 active 启用边界；Memoir 装配行为保持兼容 | Memoir 回归通过；注册表没有 QA 时，QA 包不能通过受管入口以 active 状态注册，无法由该路径创建可执行 Run；现有使用 `agent_id="agent"` 的 13 处测试行为不变；不得把 QA 返回 pending 算作接入成功 |
| 2 | 工具注册分组 + 包级输出目标校验 + 安全写入函数抽取；预算参数化仅在实际复用时做 | Memoir 1.0.0–1.0.8 包加载结果与 digest 不变，原状态写入及 `apply_result` 安全测试通过；QA/Memoir 互相声明对方专属 `output_to` 被拒绝，工具调用不越权；控制字段或敏感键被拒绝且状态不变；伪造参数、授权撤销、迟到结果仍被阻断 |
| 3 | QA 1.0.0 最小接入：私有强类型工作状态 + 一个只读查询工具 + 一个回答提交工具 | 授权内查询成功、越权拒绝、引用仅来自工具结果；每个 Run/执行 attempt 新建 Runner；checkpoint 解密后只有安全键，恢复重跑读取节点；回答已提交但响应丢失时按原逻辑键查询复用、不重复提交；QA connector/模型必需配置缺失时明确拒绝或以错误码终止，不停在占位 pending；取消与费用上限生效 |
| 4（按需） | 迁移守卫与评价器；能力接口支持 QA；partial 判定通用化 | 每项各有回归测试 |

**兼容风险：**

- ToolGateway 是所有 Agent 共用的发送边界，步骤 2 一旦出错，Memoir 会被一起影响。因此步骤 2 必须跑全量测试和契约兼容测试，只跑局部测试不够。
- 只关闭 QA 入口，无法证明共享改动是安全的。公共层改动需要能单独回退的发布单元。

## 5. 实施前需要决策的事项

1. **交互方式：**QA 要求同步返回、流式输出，还是接受“提交后等待回调”的异步方式？这决定现有 Run 模型能否直接复用。
2. **数据范围：**QA 查询哪个业务系统、哪些业务对象？复用 `couple_diary_backend` connector，还是新增 connector？
3. **读写范围：**首版是否确认只读？注意，“保存回答”本身就是写操作，需要业务后端提供回答提交接口和结果查询接口。
4. **会话归属：**同意 06 的建议，由业务后端管理 Conversation、Turn 和历史；com 只负责一次 Run 的执行。
5. **能力发现：**业务方是否需要通过 capabilities 接口发现 QA？如需要，应评估对外契约是否升版。

## 6. 范围外观察（未核验线上环境）

[config.py:289](../../../app/core/config.py:289) 中 `MEMORY_SNAPSHOT_FERNET_KEY` 带有写在代码里的默认值。建议确认生产环境已通过环境变量覆盖它。该项与本次解耦无关，本文没有核验部署配置。
