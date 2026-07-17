# Model Task 3 报告

## 完成内容

- `MemoirNodeRunner` 新增可注入的 `MemoirModelGateway` 边界，并只接入既有的
  `extract_highlights`、`plan_chapters`、`generate_scenes` 三个可替换模型节点。
- Runner 传递给模型的仅为稳定 `source_refs`、已验证章节和场景结构；快照日记/赌局正文
  不会进入模型请求、日志、fallback 状态或节点结果。
- Gateway 非 `succeeded`、调用异常和任意结构化结果不合法时，均继续使用已有模板产物。
  高光、章节和场景分别检查素材引用 allowlist、对象结构、数量和唯一 ID；非法场景不会
  进入安全审核或发布文档。
- 未引入 PromptRegistry、ContextManager，亦未修改现有模型路由、usage、lease 或流控边界。

## TDD 证据

先新增 `tests/test_memoir_model_gateway.py`，运行：

```text
poetry run pytest tests/test_memoir_model_gateway.py -q
2 failed
TypeError: MemoirNodeRunner.__init__() got an unexpected keyword argument 'model_gateway'
```

在最小注入与降级实现后，补充“成功响应但缺失 chapters 字段”回归：

```text
poetry run pytest tests/test_memoir_model_gateway.py -q
1 failed
{'fallback': False} != {'fallback': True}
```

收紧结构校验后转绿。

## 验证

```text
poetry run pytest tests/test_memoir_model_gateway.py tests/test_memoir_snapshot_runner.py tests/test_memoir_publish_audit.py -q
13 passed

poetry run ruff check app/agents/memoir_agent/runner.py tests/test_memoir_model_gateway.py tests/test_memoir_snapshot_runner.py tests/test_memoir_publish_audit.py
All checks passed!
```

未执行 git 操作。

## Review 修复（Worker 受信任模型装配与场景 ID）

- Worker 新增 `configured_model_gateway(session)`：仅从 `Settings` 的
  `MODEL_ROUTES_JSON`、`RUNTIME_REDIS_URL` 与固定的
  `MEMOIR_MODEL_NODE_ROUTES_JSON` 组装 `ModelRouteRegistry`、Redis
  `ProviderTrafficController`、`ModelUsageService`、`LeaseService`、
  `HttpProviderAdapter` 和实际 `ModelGateway`。任一配置缺失、映射不完整、路由不受信任或
  Redis client 初始化失败时，返回 `None`，Runner 显式使用既有模板 fallback。
- 新增 `MemoirModelGatewayAdapter`。它从当前运行中的权威 `AgentStep` 与已认领的
  `LeaseContext` 构造 `ModelCallContext`，并仅以部署映射指定 route ID；Runner/业务输入
  均不能指定 context 或 route。
- Redis 装配只执行 `Redis.from_url()`，不会连接或探测 Redis；实际流控调用时 Redis 故障由
  `ProviderTrafficController` fail-closed，模型节点随即模板 fallback。
- `_valid_scenes()` 与 `_is_safe_playback()` 都拒绝重复 `scene_id`；新增回归证明模型输出
  重复场景 ID 时不会进入播放结构，而会回退模板。

### 修复后自检

```text
poetry run pytest tests/test_memoir_model_gateway.py tests/test_memoir_snapshot_runner.py tests/test_memoir_publish_audit.py tests/runtime_test_worker_entry.py -q
22 passed in 1.18s

poetry run ruff check app/worker.py app/core/config.py app/runtime/memoir_model_gateway.py app/agents/memoir_agent/runner.py tests/test_memoir_model_gateway.py tests/runtime_test_worker_entry.py
All checks passed!
```

`pyproject.toml` 已声明 `redis>=5.0.0`。本地 `poetry lock` 因无法访问 PyPI 未完成，随后
网络授权请求被中断；未执行 git 操作。
