# AgentRuntime 执行器恢复闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让静态 WorkflowExecutor 在有效 lease 下为每个完成节点保存加密恢复状态与最小 Artifact，并能从加密 checkpoint 安全恢复。

**Architecture:** WorkflowExecutor 保持只编排受信任静态计划；CheckpointStore 是唯一的私密恢复状态入口，ArtifactStore 只保存安全摘要、内容摘要和业务资源引用。未接入真实 Tool/Model 节点 Runner 前，Worker 继续使用安全失败的 BootstrapExecutor，避免假成功。

**Tech Stack:** Python 3.13、SQLAlchemy、Pydantic、cryptography Fernet、pytest。

## Global Constraints

- Runtime 不保存第二套可播放回忆录正文。
- 所有 checkpoint 和 artifact 写入前必须复核 lease、fencing、privacy、authorization 与取消状态。
- 日志、摘要和 Artifact 不得包含日记原文、prompt、模型原始输出或工具原始 payload。
- 以测试先行；每步先观察失败再补最小实现。

---

### Task 1: 加密 checkpoint 与 resume 接通

**Files:**
- Modify: `app/runtime/executor.py`
- Modify: `tests/runtime_test_workflow_executor.py`

**Interfaces:**
- Consumes: `CheckpointStore.save(run_id, checkpoint_key, state, context)`。
- Produces: `WorkflowExecutor.resume()` 从 `CheckpointStore.load_latest()` 恢复完成节点。

- [ ] **Step 1: 写入失败测试**

```python
assert checkpoint.encrypted_state_blob is not None
assert checkpoint.state_summary == {"completed_node_ids": ["load_snapshot"]}
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `poetry run pytest tests/runtime_test_workflow_executor.py -q`
Expected: FAIL，因为当前执行器直接写入未加密 checkpoint。

- [ ] **Step 3: 最小实现**

```python
checkpoint_store.save(run_id, checkpoint_key, state, lease_context)
state = checkpoint_store.load_latest(run_id, lease_context)
```

- [ ] **Step 4: 运行测试并确认通过**

Run: `poetry run pytest tests/runtime_test_workflow_executor.py tests/runtime_test_checkpoint_resume.py -q`
Expected: PASS。

### Task 2: 最小 ArtifactStore 与节点产物审计

**Files:**
- Create: `app/runtime/artifact.py`
- Modify: `app/runtime/executor.py`
- Modify: `tests/runtime_test_workflow_executor.py`

**Interfaces:**
- Produces: `ArtifactStore.save_node_result(run, node_id, result, context) -> str`。
- Contract: 只持久化结果键名和固定状态摘要，业务资源引用为 `business://{business_type}/{business_id}`。

- [ ] **Step 1: 写入失败测试**

```python
artifact = session.scalar(select(AgentArtifact))
assert artifact.summary_json == {"node_id": "load_snapshot", "result_keys": ["node_id", "result"]}
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `poetry run pytest tests/runtime_test_workflow_executor.py -q`
Expected: FAIL，因为尚未写入 AgentArtifact。

- [ ] **Step 3: 最小实现**

```python
artifact_store.save_node_result(run, node_id, node_result, lease_context)
```

- [ ] **Step 4: 运行测试并确认通过**

Run: `poetry run pytest tests/runtime_test_workflow_executor.py -q`
Expected: PASS。

### Task 3: 回归验证与计划同步

**Files:**
- Modify: `头脑风暴/docs/AgentRuntime/plans/2026-07-07-AgentRuntime-总控开发计划.md`

- [ ] **Step 1: 运行 Runtime 相关回归测试**

Run: `poetry run pytest tests/runtime_test_*.py tests/test_runtime_*.py -q`
Expected: PASS。

- [ ] **Step 2: 运行静态检查**

Run: `poetry run ruff check app tests`
Expected: PASS。

- [ ] **Step 3: 更新总控计划**

将已满足的 Task 6 条目改为 `[✅]`，保留 workflow.graph 装配、fallback、human review 和真实生产 Runner 为未完成状态。
