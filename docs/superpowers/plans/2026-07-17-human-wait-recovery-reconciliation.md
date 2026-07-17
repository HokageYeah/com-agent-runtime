# 人工等待恢复与完整对账 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 AgentRun 提供可恢复的人工等待 fallback，以及可幂等修复运行异常的完整 Reconciler。

**Architecture:** 复用 `AgentRun`、`AgentPlan`、Checkpoint、Admission、Lease 与 Outbox 的现有权威状态。fallback 目标写入加密 checkpoint；Worker 新 attempt 通过 `WorkflowExecutor.resume` 从该目标节点继续。Reconciler 只针对数据库可确定的过期与撤销状态执行条件变更，成功后才迁移 Admission 与创建 Outbox。

**Tech Stack:** Python 3、FastAPI、SQLAlchemy、Pydantic、pytest、Ruff。

## Global Constraints

- 日志、Artifact、checkpoint 安全摘要、callback 均不得写入日记正文、快照或完整播放文档。
- 每个新增方法、模型字段和关键状态迁移添加中文注释与仅含 ID/状态/错误码的调试日志。
- 只扩展现有静态执行器，不引入新队列、图调度器或定时框架。
- 当前工作区包含用户未提交改动；本计划执行不执行 `git add`、`git commit`、`git reset` 或覆盖无关文件。

---

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

### Task 2: 为人工超时 fallback 添加原子对账恢复

**Files:**
- Modify: `app/services/reconciliation_service.py`
- Modify: `tests/test_reconciliation_service.py`

**Interfaces:**
- Consumes: `waiting_human + finished`、未过期 `AgentDefinition` 的 fallback policy、`AdmissionService.transition_run`、`OutboxService.append_run_dispatch`。
- Produces: `waiting_human + queued` 的恢复 Run，且只写一次 `run_dispatch(reason="waiting_human_timeout_fallback")`。

- [ ] **Step 1: 写失败测试，断言 fallback 超时重新入队而不是 failed。**

```python
report = ReconciliationService(session).run_once(now=now)
assert (run.status, run.dispatch_state) == ("waiting_human", "queued")
assert service.count_dispatch_events(run.run_id) == 1
assert report.repaired == 1
```

- [ ] **Step 2: 运行失败测试。**

Run: `poetry run pytest tests/test_reconciliation_service.py -q`

Expected: FAIL；当前 fallback 会安全终结为 failed。

- [ ] **Step 3: 最小实现条件恢复。**

将每个候选 Run 的 `status_version` 作为条件更新前提；成功后清空 `waiting_expires_at`、递增状态版本、迁移 `finished -> queued` Admission 并写 dispatch outbox。条件不命中时只记录安全的竞争日志，不增加 repaired 或 outbox。

- [ ] **Step 4: 写失败测试，断言过期与审批竞争不会产生双 outbox。**

```python
run.status_version += 1
report = ReconciliationService(session).run_once(now=now)
assert report.repaired == 0
assert service.count_dispatch_events(run.run_id) == 0
```

- [ ] **Step 5: 运行并修复 Task 2 测试。**

Run: `poetry run pytest tests/test_reconciliation_service.py -q`

Expected: PASS。

### Task 3: 扩展 Reconciler 的超时、死信与撤销修复

**Files:**
- Modify: `app/services/reconciliation_service.py`
- Modify: `app/reconciler.py`
- Test: `tests/test_reconciliation_service.py`

**Interfaces:**
- Consumes: `held_expires_at`、`queued_at + plan.stop_conditions_json["queue_ttl_seconds"]`、lease、`RuntimeOutboxEvent.status` 与 Definition 状态。
- Produces: 安全 `ReconciliationReport(scanned, repaired, dead_letter_callbacks, failures)` 和一次性终态 callback/dispatch outbox。

- [ ] **Step 1: 写失败测试，分别覆盖 held、queued、lease 到期和 run_dispatch 死信。**

```python
report = ReconciliationService(session).run_once(now=now)
assert held.status == "cancelled"
assert queued.error_code == "QUEUE_TIMEOUT"
assert reaped.dispatch_state == "queued"
assert dead_dispatch.error_code == "DISPATCH_FAILED"
```

- [ ] **Step 2: 运行失败测试。**

Run: `poetry run pytest tests/test_reconciliation_service.py -q`

Expected: FAIL；当前只扫描 waiting_human 且只统计 callback 死信。

- [ ] **Step 3: 最小实现四类权威修复。**

先调用 `LeaseService.reap_expired()`；再处理 held/queued 超时和 `run_dispatch` dead letter。每个终态路径统一设置 `finished_at`、`error_code`、`status_version`，调用对应 Admission 迁移并写 terminal callback。dead letter 仅能终结仍为 `pending + queued` 的同 aggregate Run。

- [ ] **Step 4: 写失败测试，覆盖 revoked Definition 的终结与取消请求。**

```python
report = ReconciliationService(session).run_once(now=now)
assert queued.status == "cancelled"
assert claimed.cancel_requested_at is not None
assert report.repaired == 2
```

- [ ] **Step 5: 最小实现撤销处理并运行 Task 3 测试。**

对 Definition `status=revoked` 的 held/queued/waiting_human 终结为 cancelled；claimed 仅写 `cancel_requested_at`。当前项目未提供授权撤销权威来源，因此授权版本主动扫描不在本计划实现范围，主计划保持 `[ ]`。

Run: `poetry run pytest tests/test_reconciliation_service.py -q`

Expected: PASS。

### Task 4: 更新总控计划并完成回归验证

**Files:**
- Modify: `头脑风暴/docs/AgentRuntime/plans/2026-07-07-AgentRuntime-总控开发计划.md`
- Modify: `docs/superpowers/plans/2026-07-17-human-wait-recovery-reconciliation.md`

**Interfaces:**
- Consumes: Task 1–3 的可验证结果。
- Produces: 完成项 `[✅]`，未实现的 generic graph fallback、实时授权来源、密钥轮换等保留 `[ ]`。

- [ ] **Step 1: 执行 P0 相关测试。**

Run: `poetry run pytest tests/runtime_test_workflow_executor.py tests/runtime_test_worker_entry.py tests/test_reconciliation_service.py tests/runtime_test_worker_lease_fencing.py -q`

Expected: PASS。

- [ ] **Step 2: 执行全量质量验证。**

Run: `poetry run pytest -q && poetry run ruff check app tests alembic && git diff --check`

Expected: pytest 全绿（允许既有第三方弃用警告）；Ruff 与 diff check 无错误。

- [ ] **Step 3: 更新文档复选框。**

将已交付的“fallback 节点”“waiting_human 超时恢复/终结”“完整 Reconciler 已覆盖的租约、held/queued、dispatch dead letter、Package 撤销”标记为 `[✅]`；未完成行保持 `[ ]`。

- [ ] **Step 4: 复核计划状态。**

Run: `rg -n '\- \[ \]' docs/superpowers/plans/2026-07-17-human-wait-recovery-reconciliation.md`

Expected: 仅保留尚未执行的步骤；完成后所有本计划步骤为 `[✅]`。
