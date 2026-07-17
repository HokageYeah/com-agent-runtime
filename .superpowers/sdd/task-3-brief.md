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

