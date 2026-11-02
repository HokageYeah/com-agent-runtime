# 多 Agent 扩展性与目录评审

评审日期：2026-10-27。基线：`hucci-agent` 分支工作区，即 [QA 接入计划](../AgentRuntime/plans/2026-10-27-QA-Agent接入与公共Runtime最小解耦开发计划.md) T0–T6 全部完成后的状态，代码尚未提交。

要回答的问题：如果以后接入客服、幼儿学习、英语教学这类智能体，现在的结构能不能支撑？目录是否臃肿，要不要按职责重新调整？

## 0. 结论

1. **运行底座已经通用。** Run、Worker、lease、预算、授权复核、幂等提交、checkpoint 白名单、模型网关、工具网关、callback 都与具体 Agent 无关，QA 已经用实际接入证明了这一点。
2. **“接入方式”还没有通用。** 现在每接入一个新 Agent，仍然要修改 **5 个公共文件**；其中公共内核 `tool_gateway.py` 甚至直接 import 了 `qa_agent`，依赖方向是反的。只有 2 个 Agent 时问题不明显，到第 5 个时公共文件会变成所有 Agent 的大杂烩。**这一项必须现在改。**
3. **目录不需要大规模重组。** 06 设想的 `runtime/runs|execution|gateways|...`、`adapters/`、`legacy/` 拆分会波及 1400 多个测试的 import 路径，收益只是“看起来整齐”。真正让人觉得臃肿的原因是第 2 点：Agent 专属代码散落在公共文件里。把它收回各 Agent 的包，目录自然就清楚了。
4. 对话型、工作流型 Agent 的**产品能力缺口**（流式输出、条件分支、共享 TTS 等）是真实存在的，但都应该等到具体 Agent 出现时再做，见第 3 节。

## 1. 新增一个 Agent 现在要改哪些公共文件

| 文件 | 现在的做法 | 问题 |
| --- | --- | --- |
| `app/agents/registry.py` | `EXECUTABLE_AGENT_IDS` 和工厂字典逐个列出；工厂从 `app.worker` 导入 | 每加一个 Agent 改两处 |
| `app/agents/output_targets.py` | 逐个导入各 Agent 的输出字段 | 每加一个 Agent 改一处 |
| `app/runtime/tool_gateway.py` | `_AGENT_TOOL_PROFILES` 在公共内核里写死；`import app.agents.qa_agent.tools` | **公共内核依赖具体 Agent**，违背“公共层不导入具体 Agent”的原则 |
| `app/worker.py` | `configured_memoir_executor`、`configured_qa_executor` 都写在 Worker 里，两者各自重复约 60 行 ToolGateway / checkpoint 装配代码；顶部 import Memoir Runner | Worker 会随 Agent 数量线性膨胀（现在已有 838 行） |
| `app/core/config.py` | 每个 Agent 一个 `XXX_MODEL_NODE_ROUTES_JSON` 字段 | 配置项随 Agent 数量增长 |

**目标：**新增 Agent 只需要两步，一是新建 `app/agents/<agent_id>/`，二是在注册表里加一行；公共文件一个都不用改。

## 2. 修订方案（最小且足够）

1. **每个 Agent 自带注册描述。**`app/agents/<agent_id>/registration.py` 导出一个 `AgentRegistration`，内容包括：身份、允许的业务类型、工具档案、工具输出字段、执行器工厂（延迟加载）。
2. **注册表只保留一份可信清单。**`app/agents/registry.py` 中每个 Agent 占一行；ToolGateway、包 schema、注册脚本、Worker 都从这里查询。清单仍然写在代码里，不从包或请求读取，安全模型不变。
3. **`AgentToolProfile` 等描述类型放到中立模块。**这样 `tool_gateway.py` 不再 import 任何具体 Agent，也避免循环导入。
4. **装配代码下沉到各 Agent。**各 Agent 的工厂放在 `app/agents/<agent_id>/bootstrap.py`。Worker 只保留公共构建函数（`build_tool_gateway`、`build_model_gateway`、`build_checkpoint_store`），不再 import 任何具体 Agent。Memoir 工厂原样迁移，行为不变。
5. **模型节点路由使用通用配置。**新 Agent 统一用 `AGENT_MODEL_NODE_ROUTES_JSON = {"<agent_id>": {"<node>": "<route_id>"}}`。`MEMOIR_MODEL_NODE_ROUTES_JSON` 已在部署环境使用，原样保留。`QA_MODEL_NODE_ROUTES_JSON` 是本批新增、尚未提交的配置，现在替换没有兼容成本。
6. **加两类护栏测试，防止回退。**
   - 架构测试：公共目录（`app/runtime`、`app/schemas`、`app/worker.py`、`app/api`、公共 `app/services`）不得 import 具体 Agent。
   - 注册一致性测试：遍历所有注册项，确认包能加载；manifest 里的工具都在档案中；`output_to` 都在允许字段内；工厂能被解析。
7. **接入指南。**新增 `app/agents/README.md`，写清新 Agent 的目录约定、必须提供的文件、注册步骤和需要通过的测试。

**这次不做的事：**不搬动 `app/runtime/` 中的公共文件；不移动 Memoir 的守卫、评价器、媒体和音频服务（它们只有 Memoir 使用，不会阻碍新 Agent）；不移动测试设施文件（`mock_*`、`*_harness`）；不改 `services/memoir`、`models/memoir`。

### 目标目录（只增不搬）

```text
app/agents/
  README.md                 # 新增：Agent 接入指南
  registry.py               # 可信注册清单，每个 Agent 一行
  memoir_agent/
    registration.py         # 新增
    bootstrap.py            # 新增：从 worker.py 原样迁入的工厂
    runner.py / model_gateway.py / 1.0.0–1.0.8/   # 原有
  qa_agent/
    registration.py         # 新增
    bootstrap.py            # 新增：从 worker.py 迁入
    runner.py / state.py / tools.py / model_gateway.py / 1.0.0/
app/runtime/                # 公共内核，不再 import 具体 Agent
app/worker.py               # 只做调度和公共构建，不认识具体 Agent
```

## 3. 不同类型 Agent 的支撑度

| Agent 类型 | 现在能直接支撑的 | 出现真实需求时再补的 |
| --- | --- | --- |
| 问答 / 知识类（客服知识库问答、英语语法答疑） | QA 模式：由业务后端持有会话，每轮一个 Run，回答通过工具提交 | — |
| 实时对话类（在线客服、英语口语陪练） | 异步 Run 可以用，但时延是“排队 + 执行” | **流式输出（SSE）或低延迟执行通道**；要先明确时延指标 |
| 需要人工介入（客服转人工） | 已有 `waiting_human` 节点与 callback | 具体审批与超时策略按业务定义 |
| 固定流程类（学习报告生成、作业批改） | 静态线性工作流 + `bounded_loop` | — |
| 带分支的流程（幼儿学习：答对进下一关，答错重讲） | 现在只支持**线性 DAG**，没有条件边 | **条件分支 / 受控工具循环**，需要单独设计版本兼容 |
| 需要语音（英语发音、幼儿故事朗读） | TTS 和音频服务目前是 Memoir 专属 | 把音频能力**提升为公共能力**；有第二个使用方时再做 |
| 多步检索 / 动态工具选择 | 不支持（属于设计取舍） | 受控工具循环，必须继承预算、授权和幂等 |

这些能力都会改动公共执行模型，需要以具体业务的验收标准驱动，**不建议提前建设**。第 2 节的注册收口能保证：到那时新增的能力只落在公共层的一个位置，各 Agent 按需启用。

## 4. 风险与兼容

- **测试替身路径会变：**有 3 个现有测试通过 `monkeypatch.setattr(worker, "MemoirNodeRunner", ...)` 替换 Runner。工厂迁移后，这些测试需要改为替换新模块里的符号。这只是测试目标的变化，不是行为变化。
- **对外兼容：**Memoir 包内容和 digest、对外 API、Tool wire、`MEMOIR_*` 配置一律不变。
- **安全模型不变：**注册仍是代码中的可信清单；新 Agent 能拿到的权限不会多于今天手写的版本。
