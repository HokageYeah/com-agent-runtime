### Task 1: 冻结 fallback 恢复目标并让 Worker 调用 resume

**Files:**
- Modify: `app/runtime/planner.py`
- Modify: `app/runtime/executor.py`
- Modify: `app/services/run_queue_service.py`
- Modify: `app/worker.py`
- Test: `tests/runtime_test_workflow_executor.py`
- Test: `tests/runtime_test_worker_entry.py`

**Interfaces:**
- Consumes: `AgentDefinition.definition_json["policy"]["waiting_human_fallback_node"]`。
- Produces: checkpoint 私密状态字段 `resume_from_node_id: str | None`；`RunQueueService` 对 `waiting_human -> queued` 的 resumed run 调用 `RunExecutor.resume(run_id, context)`。

- [ ] **Step 1: 写失败测试，断言 fallback checkpoint 恢复时跳过目标节点之前的节点。**

```python
result = executor.resume("fallback-run", context)
assert runner.node_ids == ["fallback"]
assert result.status == "succeeded"
```

- [ ] **Step 2: 运行失败测试。**

Run: `poetry run pytest tests/runtime_test_workflow_executor.py -q`

Expected: FAIL；现有 executor 会按静态顺序执行，而不是从 fallback 节点开始。

- [ ] **Step 3: 最小实现 checkpoint 恢复起点。**

```python
def _execute(..., resume_from_node_id: str | None = None) -> AgentRunResult:
    skipping = resume_from_node_id is not None
    for raw_node in plan.steps_json:
        node = self._validated_node(raw_node)
        if skipping:
            skipping = node["node_id"] != resume_from_node_id
            if skipping:
                continue
```

在人审节点返回 `waiting_human` 前，把经过 Plan/Definition 校验的 fallback 节点写入加密 checkpoint；正常 approve 不写恢复目标。`resume` 读取并校验该字段；目标不在 `steps_json` 时返回 `FALLBACK_NODE_INVALID`。

- [ ] **Step 4: 写失败测试，断言被 Reconciler 恢复的 Run 在新 lease 下执行 resume。**

```python
queue.consume("fallback-run")
assert executor.resume_calls == ["fallback-run"]
assert executor.run_calls == []
```

- [ ] **Step 5: 运行失败测试。**

Run: `poetry run pytest tests/runtime_test_worker_entry.py -q`

Expected: FAIL；当前 `RunQueueService.consume` 始终调用 `run`。

- [ ] **Step 6: 最小实现 Worker 选择恢复入口。**

在 claim 前读取 Run 的 `status`：状态为 `waiting_human` 时调用 `resume`，其他 queued Run 仍调用 `run`。因为 claim 只变更 dispatch_state、不覆盖 status，这同时修复 approve 与 fallback 的恢复路径；为 `RunExecutor` protocol 增加 `resume`，`BootstrapExecutor` 保持安全失败实现，不新增数据库字段或迁移。

- [ ] **Step 7: 运行 Task 1 测试。**

Run: `poetry run pytest tests/runtime_test_workflow_executor.py tests/runtime_test_worker_entry.py -q`

Expected: PASS。

