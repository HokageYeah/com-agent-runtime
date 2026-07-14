# 11-LangGraph 与 LangChain 工作流设计

## 一、分工

```text
LangGraph
  负责有状态编排和长任务可靠性

LangChain
  负责 Agent 组件、prompt、tool、parser、retriever、middleware

Provider Adapter / LiteLLM
  负责模型 provider、fallback、成本记录和流式 / 结构化调用
```

三者不是替代关系，而是分层关系。

## 二、Agent 类型

### 2.1 Workflow Agent

技术：

```text
LangGraph StateGraph
  + LangChain prompt/parser/tool
  + Provider Adapter / LiteLLM ModelGateway
```

适合：

- MemoirAgent。
- 报告生成。
- 内容审核。
- 课程生成。
- 批处理任务。

特点：

- 流程明确。
- 易测试。
- 易恢复。
- 易做降级。

### 2.2 Autonomous Agent

技术：

```text
LangChain createAgent
  或 Runtime 自定义 ToolLoopAgent
```

适合：

- 客服 Agent。
- 数据问答。
- 知识库助手。
- 运维排障。

特点：

- 动态选择工具。
- 更像对话助手。
- 必须限制 step、tool allowlist 和 cost。

### 2.3 Hybrid Agent

技术：

```text
LangGraph 外层 workflow
  -> 某些节点调用 LangChain createAgent
```

适合：

- 复杂客服工单。
- 多阶段学习辅导。
- 运营分析。

示例：

```text
classify_ticket
  -> autonomous_support_agent
  -> safety_review
  -> persist_ticket
```

## 三、MemoirAgent 图结构

第一版图：

```text
START
  -> load_snapshot
  -> sanitize_materials
  -> compute_stats
  -> extract_highlights
  -> plan_chapters
  -> generate_scenes
  -> generate_actions
  -> safety_review
  -> publish_playback_document
  -> END
```

二期增加：

```text
publish_playback_document
  -> enqueue_tts
  -> enqueue_cover
  -> generate_share_preview
  -> END
```

## 四、节点类型

| 类型 | 示例 | 说明 |
|---|---|---|
| deterministic | compute_stats | 纯代码，稳定可测 |
| llm | extract_highlights | 调用模型 |
| tool | load_snapshot | 调业务工具 |
| guardrail | safety_review | 规则 + 模型复核 |
| fallback | template_scenes | 降级产物 |

第一版应尽量把能确定的事情写成 deterministic 节点，减少 Agent 不稳定性。

## 五、节点输入输出

每个节点都要声明：

```text
name
type
input_fields
input_trust_policy
output_fields
prompt_id
tool_names
retry_policy
fallback_node
timeout_ms
```

这样 Runtime 才能记录 step、恢复执行、生成评测样本。

## 六、LangChain 在节点内的用法

### 6.1 PromptTemplate

用于统一 prompt 变量：

```text
system_rules
business_rules
sanitized_material
stats
expected_schema
```

`sanitized_material`、Retriever 文档和工具结果仍是 untrusted content。PromptTemplate 必须把它们放在独立数据变量中，不能与 `system_rules/business_rules` 合并成同一自由文本片段。

### 6.2 OutputParser

每个 LLM 节点必须使用 parser：

- JSON parser。
- Pydantic / JSON Schema parser。
- 修复 parser。
- 长度和引用校验。
- material/scene/action ID、数值边界和工具参数的确定性语义校验。

### 6.3 Tool

业务工具包装成 LangChain Tool，供 Autonomous Agent 或部分节点复用。

### 6.4 Retriever

第一版回忆录不需要复杂 RAG。二期客服 Agent 接入时使用 LangChain Retriever 连接知识库。

### 6.5 Middleware

用于：

- 注入业务安全规则。
- 截断上下文。
- 记录模型调用。
- 拦截敏感输出。
- 为业务输入、Retriever 文档和工具结果附加 trust label。
- 阻止 untrusted content 修改 tool allowlist、connector、权限和 side effect 参数来源。
- 在模型、工具和 callback 边界复核 authorization version。

## 七、Checkpoint 与恢复

每个节点完成后保存 checkpoint：

```text
run_id
node_name
state_schema_version
encrypted_state_blob 或 storage_ref
data_classification
expires_at
created_at
```

Checkpoint 只保存恢复必需的状态，必须加密、设置 TTL 并响应业务删除事件。日志和 trace 只能记录 checkpoint key 与安全摘要。

Checkpoint saver 写入时携带 AgentRun 的 `privacy_version`，并以 `privacy_state=active AND privacy_version=expected` 作为条件。purge tombstone 生效后，LangGraph 的迟到节点即使返回成功也不能重建 checkpoint；该 run 同时失去 resume/retry 资格，只保留无内容执行审计。

失败恢复策略：

| 失败节点 | 恢复方式 |
|---|---|
| load_snapshot | 重新调用工具 |
| extract_highlights | 重试后模板高光 |
| plan_chapters | 重试后模板章节 |
| generate_scenes | 重试后模板场景 |
| generate_actions | 默认动作 |
| publish_playback_document | 使用稳定幂等键重试；后端按 generation_epoch 原子发布 |

## 八、Human-in-the-loop

第一版不做复杂人工审核界面，但 Runtime 状态应预留：

```text
waiting_human
```

二期用于：

- 高风险回忆录文案人工确认。
- 客服退款或封号操作确认。
- Prompt 质量人工抽检。

## 九、事件流

Runtime 应能输出事件：

```text
run_started
step_started
model_call_started
model_call_finished
tool_call_started
tool_call_finished
step_failed
fallback_used
run_succeeded
run_failed
```

第一版 Runtime 只需落库、callback 和查询；小程序前端轮询业务后端本地状态，只暴露 `public_trace`。业务后端 SSE 是 H5/已验证流式端的可选适配，Runtime 原生 `/api/v1/agent-runs/{run_id}/events` 放到二期。

## 十、第一版与二期边界

第一版：

- LangGraph Workflow Agent。
- MemoirAgent。
- LangChain prompt/parser/tool 基础使用。
- 节点级 retry / fallback。
- checkpoint。

二期：

- LangChain createAgent 动态 Agent。
- Hybrid Agent。
- LangGraph subgraph。
- human-in-the-loop UI。
- RAG / retriever。
- 多 Agent 协作。
