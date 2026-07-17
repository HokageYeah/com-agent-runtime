# Runtime P0 Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use Superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收口 Workflow 执行边界、人工等待、callback 状态机与最小对账，使回忆录 Run 在异常、撤销和投递失败后保持可恢复、可查询且不泄漏私密内容。

**Architecture:** Executor 负责在节点边界验证有效 Lease 并将等待人工的运行交给 LeaseService 原子收敛；业务 callback 只投影当前 archive generation 的安全状态。独立 Reconciler 扫描 Runtime 与业务的少量可判定异常，不读取快照正文、模型内容或播放文档。

**Tech Stack:** Python 3.13、FastAPI、SQLAlchemy、Alembic、Pydantic、httpx、pytest、ruff。

## Global Constraints

- 全部新增逻辑、模型字段与方法使用中文注释；日志仅记录 ID、状态、计数和错误码。
- 使用 TDD：每个行为先写失败测试，再写最小实现，再执行定向测试。
- checkpoint、Artifact、callback、ReconciliationReport 不得包含日记正文、快照、播放文档、prompt 或密钥。
- 保持现有脏工作区；不执行 reset、checkout 或提交。
- 不实现 ModelGateway、ProviderTrafficController、密钥轮换窗口或前端 UI，它们属于后续 Task。

---

### Task 1: Executor 节点前置边界

**Files:**
- Modify: `app/runtime/executor.py`
- Modify: `app/services/lease_service.py`
- Modify: `tests/runtime_test_workflow_executor.py`

**Interfaces:**
- Consumes: `LeaseService.can_write(run_id, LeaseContext)`、静态 `AgentPlan.steps_json`。
- Produces: 每个节点前重新校验 lease/privacy/authorization/cancel；节点失败保持安全失败，不猜测未在 Package schema 中声明的 fallback。

- [✅] **Step 1: Write the failing tests**

```python
def test_executor_stops_before_next_node_when_authorization_changes():
    run.authorization_version = 2
    assert executor.run("run", context).error_code == "LEASE_CONTEXT_INVALID"

```

- [✅] **Step 2: Verify RED**

Run: `poetry run pytest tests/runtime_test_workflow_executor.py -q`

Expected: 新增授权变化断言失败。

- [✅] **Step 3: Write minimal implementation**

```python
if not self._lease.can_write(run_id, lease_context):
    return self._fail(run, lease_context, "LEASE_CONTEXT_INVALID")
```

保留 `WORKFLOW_NODE_FAILED`；等待人工的 fallback 仅使用 Package 已冻结的 `waiting_human_fallback_node`。

- [✅] **Step 4: Verify GREEN**

Run: `poetry run pytest tests/runtime_test_workflow_executor.py -q`

Expected: PASS。

### Task 2: 人工等待、审批恢复与超时

**Files:**
- Modify: `app/runtime/executor.py`
- Modify: `app/services/lease_service.py`
- Modify: `app/services/agent_run_service.py`
- Modify: `tests/runtime_test_worker_lease_fencing.py`
- Modify: `tests/test_runtime_agent_run_service.py`

**Interfaces:**
- Consumes: 受信任节点结果 `{"waiting_human": True}`、Package `waiting_human_timeout_action`。
- Produces: `LeaseService.pause_for_human()` 原子写入 `waiting_human/finished/waiting_expires_at`、callback 与 Admission 释放；审批继续从 checkpoint 恢复。

- [✅] **Step 1: Write the failing tests**

```python
def test_waiting_human_releases_lease_and_emits_callback():
    assert (run.status, run.dispatch_state) == ("waiting_human", "finished")
    assert callback.event_type == "waiting_human"

def test_expired_waiting_human_run_fails_by_package_policy():
    assert reconciler.reconcile_waiting_human() == ["run-1"]
```

- [✅] **Step 2: Verify RED**

Run: `poetry run pytest tests/runtime_test_worker_lease_fencing.py tests/test_runtime_agent_run_service.py -q`

Expected: 等待人工状态或超时回收断言失败。

- [✅] **Step 3: Write minimal implementation**

```python
def pause_for_human(self, run_id: str, context: LeaseContext, timeout_seconds: int) -> bool:
    # 有效 fencing 条件下写 waiting_human、释放 Admission、追加 callback。
```

Executor 在 checkpoint 成功后调用该方法并返回非终态 `waiting_human`；超时失败先复用 Lease/Admission 状态迁移，不新建独立写路径。

- [✅] **Step 4: Verify GREEN**

Run: `poetry run pytest tests/runtime_test_worker_lease_fencing.py tests/test_runtime_agent_run_service.py -q`

Expected: PASS。

### Task 3: MemoryAgentRunRef 生命周期与 callback 乱序保护

**Files:**
- Modify: `app/models/memory_agent_run_ref.py`
- Create: `alembic/versions/20260717_*.py`
- Modify: `app/services/memory_agent_binding_service.py`
- Modify: `app/services/memory_agent_callback_service.py`
- Modify: `tests/test_memory_agent_callback_state.py`

**Interfaces:**
- Consumes: archive `active_run_id/generation_epoch` 与 callback `event_seq/status_version`。
- Produces: RunRef 保存 create/start/retry/purge 幂等键、版本摘要、对账与 purge 状态；过期 callback 只返回安全忽略结果。

- [✅] **Step 1: Write the failing tests**

```python
def test_callback_with_lower_status_version_is_ignored():
    assert service.apply(old_payload) is False

def test_run_ref_records_distinct_lifecycle_idempotency_keys():
    assert ref.start_idempotency_key == "start:key"
```

- [✅] **Step 2: Verify RED**

Run: `poetry run pytest tests/test_memory_agent_callback_state.py -q`

Expected: 缺少生命周期字段或乱序保护断言失败。

- [✅] **Step 3: Write minimal implementation**

```python
if event_seq <= ref.event_seq or status_version < ref.status_version:
    return False
```

为新增非空字段提供迁移默认值；callback 不更新 `published_revision/enhancement_status/succeeded`。

- [✅] **Step 4: Verify GREEN**

Run: `poetry run pytest tests/test_memory_agent_callback_state.py tests/test_alembic_metadata.py -q`

Expected: PASS。

### Task 4: Callback 安全收口

**Files:**
- Modify: `app/services/callback_delivery_service.py`
- Modify: `app/runtime/callback_gateway.py`
- Modify: `app/dispatcher.py`
- Modify: `tests/test_callback_delivery_service.py`
- Modify: `tests/test_runtime_callback_gateway.py`

**Interfaces:**
- Consumes: 不可变 `CallbackEvent`、当前 `AgentRun.privacy_state`、预注册 CallbackTarget。
- Produces: 投递前拒绝已开始 purge 的 Run；payload 只允许安全字段；dead letter 保留原 event 身份。

- [✅] **Step 1: Write the failing tests**

```python
def test_callback_sender_rejects_purged_run_before_http_send():
    with pytest.raises(ValueError, match="CALLBACK_RUN_NOT_DELIVERABLE"):
        service.send(outbox)
```

- [✅] **Step 2: Verify RED**

Run: `poetry run pytest tests/test_callback_delivery_service.py tests/test_runtime_callback_gateway.py -q`

Expected: callback 仍会投递或缺少错误码。

- [✅] **Step 3: Write minimal implementation**

```python
if run.privacy_state != "active":
    raise ValueError("CALLBACK_RUN_NOT_DELIVERABLE")
```

target 当前 authorization version 缺少独立权威注册表，保留总控 `[ ]`；不在此任务虚构字段或绕过业务授权。

- [✅] **Step 4: Verify GREEN**

Run: `poetry run pytest tests/test_callback_delivery_service.py tests/test_runtime_callback_gateway.py -q`

Expected: PASS。

### Task 5: 最小 Reconciler 与进程入口

**Files:**
- Create: `app/services/reconciliation_service.py`
- Create: `app/reconciler.py`
- Modify: `app/services/lease_service.py`
- Modify: `tests/test_reconciliation_service.py`

**Interfaces:**
- Consumes: `RuntimeOutboxEvent`、`AgentRun`、`AgentToolCall`、`MemoryAgentRunRef`。
- Produces: `ReconciliationReport` 安全计数；处理 callback dead letter、失效 lease、等待人工超时、`reconciliation_status=needed`。

- [✅] **Step 1: Write the failing tests**

```python
def test_reconciler_reports_callback_dead_letter_without_mutating_run_terminal_state():
    assert report.dead_letter_callbacks == 1
    assert run.status == "succeeded"

def test_reconciler_marks_missing_publish_result_for_followup():
    assert ref.reconciliation_status == "needed"
```

- [✅] **Step 2: Verify RED**

Run: `poetry run pytest tests/test_reconciliation_service.py -q`

Expected: 模块不存在或报告断言失败。

- [✅] **Step 3: Write minimal implementation**

```python
@dataclass(frozen=True)
class ReconciliationReport:
    scanned: int
    repaired: int
    dead_letter_callbacks: int
    failures: int
```

第一版只输出安全报告、回收已过期 lease、统计 callback dead letter 与待对账引用；不调用模型、不重放副作用、不读取正文。

- [✅] **Step 4: Verify GREEN**

Run: `poetry run pytest tests/test_reconciliation_service.py -q`

Expected: PASS。

### Task 6: 全量验证与总控更新

**Files:**
- Modify: `头脑风暴/docs/AgentRuntime/plans/2026-07-07-AgentRuntime-总控开发计划.md`

- [✅] **Step 1: Run complete verification**

Run: `poetry run pytest -q && poetry run ruff check app tests alembic && git diff --check`

Expected: 所有测试、lint 与 diff 检查通过。

- [✅] **Step 2: Update plan truthfully**

将已通过验收的细粒度项标记为 `[✅]`；密钥轮换、ModelGateway、完整 purge/素材删除补偿等未实现项保留 `[ ]`。
