# Model Task 3 审查

## 结论：不通过（存在 Critical）

### Critical — 生产执行路径没有接入实际 `ModelGateway`

`MemoirNodeRunner` 定义并接收的是本任务新增的 `MemoirModelGateway.call(node_id, request)`
协议；但已实现的受信任 Gateway 是
`ModelGateway.call(context, route_id, request)`，需要由权威的 Run/Step/Lease 构造
`ModelCallContext`，并需要受信任的 route ID。两者不能直接互换。

更关键的是 Worker 在 `app/worker.py` 的 `run()` 与 `resume()` 路径仍只构造
`MemoirNodeRunner(gateway, ToolCallAuditService(session))`，没有创建或注入任何模型
Gateway。因此真实 Memoir 工作流永远走模板 fallback；此次测试只注入了手写 fake，
没有证明模型节点经 permit、lease、usage 和受信任 route 边界调用 Provider。

修复应在既有执行边界提供一个适配器：从当前运行中的权威 Step 和 Lease 构造
`ModelCallContext`，将每个静态节点映射到受信任 route ID，再调用实际
`ModelGateway`；同时把它注入 Worker 的 Memoir Runner。不得将 context/route 交给
业务输入或模型请求决定。

### Important — 场景唯一 ID 未校验，报告与实现不符

报告称章节和场景都会校验“唯一 ID”，但 `_valid_scenes()` 仅检查数量、字段类型、
`scene_type` 和 source-ref allowlist；它没有拒绝重复 `scene_id`。重复 scene ID 可继续
进入 `generate_actions` 和 `safety_review`，后者也未要求 scene/action ID 唯一，导致
非法结构化模型输出没有安全 fallback。

应在 `_valid_scenes()`（并建议在 `_is_safe_playback()` 做纵深校验）拒绝重复
`scene_id`，并增加该非法输出回退到模板的回归测试。

## 已核验的正向项

- 仅触及既有的 `extract_highlights`、`plan_chapters`、`generate_scenes` 三个可替换节点；
  没有在本任务加入 PromptRegistry 或 ContextManager。
- `extract_highlights` 的模型请求从 snapshot 提取稳定 ID 组成 `source_refs`；现有测试也
  证明日记正文不进入该请求。章节/场景请求仅传递 refs 或已裁剪的结构。
- Gateway 非 `succeeded`、调用抛异常、缺字段/越界引用等输入会回退模板；相关指定回归
  通过，且日志只记录 node ID 与计数，不记录请求或 Provider 正文。

## 本地验证

`poetry run pytest tests/test_memoir_model_gateway.py tests/test_memoir_snapshot_runner.py tests/test_memoir_publish_audit.py -q`

结果：`13 passed`。

`poetry run ruff check app/agents/memoir_agent/runner.py tests/test_memoir_model_gateway.py tests/test_memoir_snapshot_runner.py tests/test_memoir_publish_audit.py`

结果：通过。`git diff --check` 也通过。

DONE

---

## 修复复审（Worker Gateway 装配与重复 scene ID）

### 结论：通过

此前 Critical 已关闭。Worker 的 Memoir `run()` 和 `resume()` 现在均通过同一
`_memoir_executor()` 装配路径，将 `configured_model_gateway(session)` 注入
`MemoirNodeRunner`。该装配只读取 Settings 中的预注册 route、固定的三节点 route 映射
和 Redis URL；映射必须完整且 route ID 必须存在，不能由 Run 或模型请求指定。

新 `MemoirModelGatewayAdapter` 在每次节点调用时查询当前 `running` 的权威 Step，使用
已绑定的 Worker lease 构造 `ModelCallContext`，再调用实际 `ModelGateway`。因此调用会
经过既有 permit、usage、deadline、lease/fencing 和 capability route allowlist 边界。

Redis 仅用 `Redis.from_url()` 创建客户端，未在 Worker 装配时探测连接；实际调用阶段
Redis `eval` 的异常仍由 `ProviderTrafficController` 返回 `redis_unavailable`，Gateway
不会发送 Provider 请求，Runner 随即模板 fallback，符合运行期 fail-closed 的要求。

此前 Important 已关闭。`_valid_scenes()` 已拒绝重复 `scene_id`，
`_is_safe_playback()` 亦加上纵深唯一性检查；新增回归覆盖重复模型场景 ID 回退模板。

本次未发现 PromptRegistry/ContextManager 引入，也未发现向模型、日志或 fallback
状态加入快照正文的路径。

### 复验

`poetry run pytest tests/test_config.py tests/test_provider_traffic_controller.py tests/test_model_gateway.py tests/runtime_test_worker_entry.py tests/test_memoir_model_gateway.py tests/test_memoir_snapshot_runner.py tests/test_memoir_publish_audit.py -q`

结果：`66 passed`。

此前同次检查中的 Ruff 与 `git diff --check` 通过。

DONE
