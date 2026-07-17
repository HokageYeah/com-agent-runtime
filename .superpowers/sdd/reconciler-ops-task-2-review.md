# Reconciler Ops Task 2 Review

## 结论：FAIL（P1，需修复后复审）

### P1：Admission version 冲突后，后续 Run 修复仍可能用陈旧 bucket 覆盖并发迁移

`_repair_admission_bucket()` 的聚合修复本身正确使用了
`WHERE id = :id AND version = :observed_version`；直接的 version 冲突测试也证明该
UPDATE 会返回失败且不覆盖新版本。

但冲突失败时，`bucket` 仍保留在同一个 `scan_session` 的 identity map 中，既没有
`expire()` 也没有 rollback。随后本轮对某个其他 Run 的超时/死信修复会调用
`AdmissionService.transition_run()`。该服务以 ORM 实体读改写 bucket，并不携带 version
条件。它可能取得该陈旧实体，按旧计数修改并在最终 commit 时写回，从而覆盖刚才使
reconcile UPDATE 失败的并发 dispatch 迁移。这违背“更新失败不写副作用”以及
Admission 修复不得覆盖并发 bucket 写入的约束。

建议：version UPDATE 失败后立即 `expire(bucket)`（或将整个扫描 Session rollback 后重新
读取）；更可靠的方案是让 `AdmissionService.transition_run()` 也使用版本条件更新，并在
冲突时中止/重试本轮。新增一个集成测试：先使 bucket version 冲突，再在同一轮触发另一
Run 的 Admission 迁移，断言并发 writer 的对应计数与 version 不会被旧 Session 覆盖。

## 已核验项目

- Usage：`ModelUsageService.mark_expired_running_unknown()` 同时以 `status == "running"`、
  deadline 条件 UPDATE；已结算 (`succeeded`/其他非 running) 记录不会命中，且只将
  `estimated_cost` 回填为 `reserved_estimated_cost`。定向测试同时验证 expired `running`
  被收敛、expired `started` 不被改写；代码条件也覆盖与 settled 并发时 UPDATE 返回 0 的情况。
- Admission 聚合：预期 held/queued/running 计数确实由 `AgentRun.dispatch_state` 聚合而来，
  四个 scope 均被计算；直接 version 冲突路径不会覆盖新 bucket。上述 P1 是冲突后的同轮
  后续副作用漏洞，并非否定该单条 UPDATE 的 guard。
- 安全报告与日志：`ReconciliationReport` 仅含计数、动作名和告警数；新增日志字段只含
  计数、固定动作名与允许的对象 ID。未见 prompt、模型正文、日记、快照或 callback payload
  被读取进 report 或日志。
- 连续三次告警：Runner 持有跨轮 `failure_streaks`，第三次及以后输出
  `reconciler_warning action=<action> consecutive_failures=<n>`；该 warning 不含私密数据。
  现有单元测试验证从 2 到 3 的告警文本，但尚未覆盖真实 Runner 跨三轮失败的传递。

## 复核命令

```text
poetry run pytest tests/test_reconciler_operations.py tests/test_reconciliation_service.py tests/test_model_gateway.py -q
# 43 passed

poetry run ruff check app/reconciler.py app/services/reconciliation_service.py app/services/model_usage_service.py tests/test_reconciler_operations.py
# All checks passed

git diff --check
# passed
```

DONE

---

## P1 修复复审（结论：PASS）

此前 P1 已关闭。

- `_repair_admission_bucket()` 在 version 条件 UPDATE 成功或失败后都调用
  `self._session.expire(bucket)`。因此失败路径不再把冲突前的 bucket 留在 scan Session
  identity map；后续 `AdmissionService.transition_run()` 会重新从数据库读取并发 writer 的
  计数与 version，而非 flush 陈旧快照。
- 新增 `test_admission_conflict_does_not_flush_stale_bucket_during_later_run_repair` 覆盖所报
  时序：scan Session 先读 bucket，writer Session 写入 `queued_count=4`、`running_count=1`、
  `version=2`；对账条件修复失败后，同一 scan Session 终结 queued Run 并迁移 Admission。
  断言最终为 `(queued_count, running_count, version) == (3, 1, 3)`，保留并发 writer 的
  `running_count=1`，且只在其新值基础上执行本轮 queued 迁移。
- 定向验证通过：`poetry run pytest tests/test_reconciler_operations.py tests/test_reconciliation_service.py tests/test_model_gateway.py -q`（44 passed）；Ruff 与 `git diff --check` 均通过。

DONE
