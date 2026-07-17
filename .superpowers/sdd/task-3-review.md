# Task 3 审查结论：需修改（阻断）

## 阻断问题

### P0：修复前置条件不是原子条件更新，竞争 Worker 可被错误终结并泄漏 Admission 运行配额

`ReconciliationService` 先将所有 `AgentRun` 读入内存，再依据对象的旧
`status` / `dispatch_state` 修改并在最后提交（`app/services/reconciliation_service.py:39-74, 186-198`）。这些查询没有行锁，也没有将旧状态、`status_version` 或 lease/fencing 条件放到 `UPDATE ... WHERE` 中。

例如：对账器读取到 `pending + queued` 的超时 Run 或 dead `run_dispatch`，Worker 随后成功 `claim`，将其及 Admission 从 `queued -> claimed`；对账器仍会把过期对象写成 `finished`，并按旧状态调用 `AdmissionService.transition_run(..., "queued", "finished")`。结果是：

- 已被 Worker 接管的 Run 被违反要求地终结；
- queued 计数被减，而实际已迁移出的 running 计数未被减，造成 Admission bucket 泄漏；
- 仍会新增 terminal callback，违反多实例下“只有成功状态变更才写 Outbox”的一次性语义。

这直接违反设计文档中“每一类修复以状态、dispatch_state、版本或 lease 条件限制”以及 dead dispatch “仅处理仍 queued”的要求。`waiting_human` fallback 也有相同的审批竞争窗口。

建议将每类修复收敛为带前置条件和 `status_version` 的条件更新（必要时 `SELECT ... FOR UPDATE`），仅在 `rowcount == 1` 时更新 Admission 并追加 callback/dispatch outbox；并增加两个 Session 的竞争测试，断言 loser 不写任何 outbox 且四级 Admission bucket 不漂移。

## 已核验通过的部分

- `held`、冻结 Plan 的 `queued` TTL、lease reaper、dead `run_dispatch` 与 revoked Definition 均有覆盖；终态统一设置 `finished_at`、`error_code`、`status_version`，并调用 Admission 迁移和 callback outbox。
- 正常重复执行时，状态守卫会阻止再次写 terminal callback；dead dispatch 只在内存检查为 `pending + queued` 时尝试终结。
- revoked Definition 对 held/queued/waiting_human 终结，对 claimed 只写 `cancel_requested_at`；没有猜测或扫描不存在的授权版本权威来源。
- 新增日志只记录 `run_id`、状态类别和安全计数，未记录输入、Plan 内容或 callback payload。
- CLI 入口只建立连接、执行一次并关闭 Session/数据库，范围合理。

## 测试/静态检查

- `poetry run pytest tests/test_reconciliation_service.py -q`：10 passed。
- `poetry run ruff check app/services/reconciliation_service.py app/reconciler.py tests/test_reconciliation_service.py`：passed。

现有测试未覆盖多实例/审批或 Worker claim 竞争、重复运行的 Outbox 数量，以及 Admission bucket 的迁移一致性，因此无法捕获上述 P0。

## 并发修复复审（仍需修改）

### 已修复：Reconciler 作为竞争 loser 时不再产生副作用

本次将 `_terminate`、waiting_human fallback 和 revoked-claimed 取消请求改为带
`run_id + status + dispatch_state + status_version` 的条件更新，且只在
`rowcount == 1` 后迁移 Admission、追加 Outbox。lease reaper 也已用
`claimed + lease_expires_at < now` 条件更新保护。Worker claim 竞争测试覆盖了
Worker 先成功的方向；对账条件更新返回零行，所以不会写 terminal callback，四级
Admission 维持 `queued=0/running=1`。这一方向通过审查。

### P0：审批路径仍不是条件更新，反向竞争可复活已由 Reconciler 终结的 Run

设计要求审批与对账竞争时“只有一个条件更新成功，另一个路径不写任何状态或
Outbox”。当前 `AgentRunService.approve` 只在内存中检查
`expected_version`（`app/services/agent_run_service.py:287-293`），随后对 ORM
对象直接赋值并提交（:297-313）；它没有把 `status=waiting_human`、
`dispatch_state=finished`、`status_version=expected_version` 写入数据库 UPDATE 的
WHERE 条件。

因此新增测试只验证“审批先提交、Reconciler 后失败”的单向顺序
（`tests/test_reconciliation_service.py:317-343`）。若审批事务先读到旧快照而
Reconciler 先条件更新并提交，审批方随后无条件 flush/commit 仍可把 Run 重写为
`waiting_human + queued`，并写入 `human_approve` dispatch。此时会出现：

- 已有 Reconciler terminal callback，又新增 approval dispatch outbox；
- Admission 先按 `finished -> finished` 不变，后又按旧快照 `finished -> queued`
  增加 queued 计数，Run 被错误复活；
- 违反该竞争中 winner-only 的状态、Outbox 与 Admission 一致性。

应把 `approve`（包含 reject 分支）改为同样带 `expected_version` 的条件更新，
只有成功后才迁移 Admission/写 Outbox；并添加“审批读取旧版本后，对账先终结并
提交”的反向双 Session 测试。完成前，Task 3 的并发安全验收不能通过。

复审执行：`poetry run pytest tests/test_reconciliation_service.py tests/test_runtime_agent_run_service.py tests/runtime_test_worker_entry.py -q`（24 passed）；`ruff` 目标检查通过。测试绿不覆盖上述反向竞争。

## 最终复审：通过

先前的审批反向竞争已关闭。`AgentRunService.approve` 现在以
`run_id + status=waiting_human + dispatch_state=finished + status_version=expected_version`
执行条件更新；零行更新立刻抛出版本冲突。Admission 迁移、dispatch/callback
outbox 和审批审计均严格位于 `rowcount == 1` 之后，因此失败分支没有状态或副作用。

新增的反向双 Session 回归先让审批会话持有旧对象，再由 Reconciler 终结并提交；
审批条件更新失败，断言 Run 保持 `failed/finished`，只有一条 Reconciler callback，
没有 approval dispatch 或审批审计，四级 Admission bucket 均未增加 queued/running。
这与 Worker claim 竞争测试共同覆盖双方获胜的方向；Reconciler 和审批的条件更新
均保证 loser 不追加 Outbox、不漂移 Admission。

最终验证：`poetry run pytest tests/test_reconciliation_service.py tests/test_runtime_agent_run_service.py tests/runtime_test_worker_entry.py -q`（25 passed）；目标 Ruff 检查通过。Task 3 并发修复审查通过。
