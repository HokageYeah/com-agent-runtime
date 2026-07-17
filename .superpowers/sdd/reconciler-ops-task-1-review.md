# Reconciler Ops Task 1 Review

## 结论：FAIL（需修复后复审）

### P1：租约在扫描仍执行时会过期，导致两个实例同时扫描

`ReconciliationLeaseService.acquire()` 只在扫描开始时写入固定 300 秒 `expires_at`，`ReconcilerRunner.run_once()` 在 `ReconciliationService.run_once()` 的整个执行期间没有续租、没有在每个扫描/修复写入处校验 fencing token，也不会在租约失效时中止旧扫描。因而一次扫描超过 TTL 时，另一实例可以按设计成功接管；旧实例仍会继续执行，于是出现两个实例并发扫描/修复。这不满足设计和验收中的“同一时刻仅一个实例执行”。

建议：为扫描过程续租并在续租失败时停止；或者将 fencing token 传入所有会产生副作用的修复条件更新，确保旧 owner 在被接管后不能继续写入。新增一个“首实例扫描阻塞超过 TTL、第二实例接管后首实例不得继续产生扫描副作用”的并发测试。

## 已核验项目

- 原子互斥与到期接管：首次建行使用主键唯一约束处理竞争，已有行使用 `expires_at <= now` 的条件更新；现有两实例与过期接管测试通过。但上述 P1 限制了该互斥保证的有效时间范围。
- 短 Session：`ReconcilerRunner` 每轮创建 Session，并在 `finally` 中关闭；未获租约时不会构造或调用对账服务。
- 周期入口：`--once` 调用单轮；默认循环和间隔均为 300 秒。`run_forever` 的间隔测试通过。
- 测试覆盖缺口：brief 要求 `--once` 与默认 300 秒的失败测试；当前只有 `ReconcilerRunner` 显式传入 `interval_seconds=300` 的循环测试，没有通过 `main()`/参数解析验证 `--once`，也没有断言默认值。
- 私密数据与范围：此任务新增日志只含固定操作状态；租约表只含 key、owner、过期时间和 token，未记录 prompt、模型正文、日记或快照。改动集中于租约模型、迁移、服务、入口和对应测试，未见不相关功能扩张。

## 复核命令

```text
poetry run pytest tests/test_reconciler_operations.py tests/test_reconciliation_service.py -q
# 17 passed

poetry run ruff check app/reconciler.py app/models/runtime.py app/models/__init__.py app/services/reconciliation_lease_service.py tests/test_reconciler_operations.py alembic/versions/20260717_1200_add_runtime_reconciliation_lease.py
# All checks passed

git diff --check
# passed
```

DONE

---

## 修复复审（结论：FAIL，原 P1 未关闭）

### P1：失租后的 `renew()` 会提交旧扫描写入，fencing / rollback 实际失效

修复增加了 `lease_guard`、`renew()` 与 token 条件，但 lease service 和 `ReconciliationService` 共用同一个 SQLAlchemy Session。`ReconciliationLeaseService.renew()` 在返回成功或失败前都会执行 `self._session.commit()`。因此存在如下时序：

1. 旧实例在一次 guard 成功后，对 Run 做修复写入；
2. 扫描在下一次 guard 前超过 TTL，其他实例接管；
3. 旧实例的下一次 `renew()` 条件更新得到 0 行，但仍执行 `commit()`；这会提交步骤 1 的旧实例写入；
4. `ReconciliationService._abort_scan()` 随后调用的 `rollback()` 已无法撤销该提交。

所以“失租中止/回滚”不成立，fencing token 也没有进入实际的 Run/Admission/Outbox 修复条件更新来拒绝失租旧 owner 的写入。现有 `test_taken_over_runner_stops_before_a_later_scan_side_effect` 只用内存列表验证 guard 的返回值，未在 guard 失败前写入数据库，无法暴露这个提交路径。

建议将租约续租放在独立 Session/事务中，且 guard 失败时先 rollback 扫描 Session；并为每个具副作用的修复写入加入可验证的 fencing 条件（或将可提交的修复限定在持锁事务内）。新增集成测试：首次修复已将 ORM/SQL 写入 pending，随后模拟租约被接管并让 guard 失败，断言扫描 Session 的 Run、Admission、Outbox 均未持久化旧 owner 的改动。

### 其他复核结果

- 已新增 `main()` 层面的 `--once` 与默认 300 秒测试，覆盖此前测试缺口。
- `ReconciliationService` 在扫描循环、dead-letter 循环和最终提交前调用 guard，并在正常的 guard 失败路径调用 rollback；问题在于 `renew()` 已提前提交同一 Session。
- `release()` 的实现仍只按 `owner_id` 过滤，未使用 fencing token，和注释“fencing 防止误释放”不符；若 owner ID 重用，旧执行可提前释放新租约。应将 token 传入并作为 release 条件。
- 新增日志仍只包含操作状态或 run ID，未见私密正文扩散；改动范围与本任务相关。

## 修复复审验证

```text
poetry run pytest tests/test_reconciler_operations.py tests/test_reconciliation_service.py -q
# 20 passed

poetry run ruff check app/reconciler.py app/models/runtime.py app/models/__init__.py app/services/reconciliation_lease_service.py app/services/reconciliation_service.py tests/test_reconciler_operations.py alembic/versions/20260717_1200_add_runtime_reconciliation_lease.py
# All checks passed

git diff --check
# passed
```

DONE

---

## 最终复审（结论：PASS）

此前 P1 已关闭。

- `ReconcilerRunner` 将 acquire、每次 renew、release 与扫描分别放在独立 Session；renew 的 commit 不再能够提交扫描事务。
- `lease_guard` 发现失租后立即 rollback `scan_session`；扫描服务也在所有循环边界和最终提交前检查 guard。`LeaseService.reap_expired(commit=False)` 确保这一扫描路径不再进行隐式 commit。
- `release(owner_id, fencing_token)` 已将 token 纳入条件，新增测试验证 owner ID 重用时旧 token 无法提前释放新租约。
- 新增文件型 SQLite 集成测试：旧实例先在扫描 Session 写入 Run、AdmissionBucket、RuntimeOutboxEvent，随后超时被另一实例接管；guard 失败后断言三类数据均未持久化，覆盖失租 rollback 的关键副作用。
- `--once` 与默认 300 秒入口测试仍在；日志内容仅为操作状态或对象 ID，未发现私密内容扩散。租约和事务边界调整均属于本任务必需范围。

## 最终验证

```text
poetry run pytest tests/test_reconciler_operations.py tests/test_reconciliation_service.py -q
# 22 passed

poetry run ruff check app/reconciler.py app/models/runtime.py app/models/__init__.py app/services/reconciliation_lease_service.py app/services/reconciliation_service.py app/services/lease_service.py tests/test_reconciler_operations.py alembic/versions/20260717_1200_add_runtime_reconciliation_lease.py
# All checks passed

git diff --check
# passed
```

DONE
