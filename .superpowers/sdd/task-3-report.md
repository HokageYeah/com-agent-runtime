# Task 3 Reconciler 实施报告

## 完成范围

- 在每轮对账开始时调用 `LeaseService.reap_expired()`，过期 claimed lease 回到 queued 并保留 dispatch outbox。
- 修复已过期的 held Run：终结为 `cancelled`，错误码 `HELD_TIMEOUT`。
- 按冻结 `AgentPlan.stop_conditions_json.queue_ttl_seconds` 修复 queued Run：终结为 `failed`，错误码 `QUEUE_TIMEOUT`。
- 对 `run_dispatch` dead letter 仅在同 aggregate 的 Run 仍为 `pending + queued` 时终结，错误码 `DISPATCH_FAILED`。
- 对 `status=revoked` 的 Definition：held、queued、waiting_human 终结为 `cancelled`；claimed 仅写入 `cancel_requested_at`。
- 所有终态修复统一设置 `dispatch_state=finished`、`finished_at`、`error_code`、`status_version`，执行 Admission 迁移并写入一次终态 callback outbox。
- 保留既有 waiting_human timeout fallback 行为，未实现没有权威来源的授权版本主动扫描。

## TDD 证据

1. 先添加 held、queued TTL、过期 lease、run_dispatch dead letter 测试，执行测试得到 4 个预期失败。
2. 实现最小修复后测试转绿。
3. 添加 revoked Definition 测试并暂时移除该分支，执行测试得到 1 个预期失败；恢复最小分支后测试转绿。

## 验证

```text
poetry run pytest tests/test_reconciliation_service.py -q
10 passed

poetry run ruff check app/services/reconciliation_service.py tests/test_reconciliation_service.py
All checks passed!
```

日志仅记录 Run 标识与状态类别，不记录输入、callback 正文或其他私密载荷。

## P0 审查修复（并发安全）

- 终结、waiting_human fallback 回队及 revoked claimed 的取消请求均改为以 `run_id`、旧 `status`、`dispatch_state`、`status_version`（及取消条件）约束的条件更新；只有 `rowcount == 1` 才迁移 Admission 并写 callback/dispatch outbox。
- lease reaper 的 `claimed -> queued` 同样改为以未过期前状态与 lease 到期条件约束的条件更新，避免使用旧读取快照覆盖 Worker 的续租或终结。
- 保留 Task 1 的 frozen Plan waiting_human fallback 决策与行为，未新增授权版本扫描。

### 新增回归与验证

1. 两个 Session 的 Worker claim 竞争：对账器 loser 不写 terminal callback，且四级 Admission 均保持 `queued=0, running=1`。
2. 两个 Session 的人工审批竞争：对账器 loser 不写 timeout callback 或额外 outbox，且四级 Admission 均保持 `queued=1, running=0`。

```text
poetry run pytest tests/test_reconciliation_service.py tests/test_runtime_agent_run_service.py tests/runtime_test_worker_entry.py -q
24 passed

poetry run ruff check app/services/reconciliation_service.py app/services/lease_service.py tests/test_reconciliation_service.py
All checks passed!
```

DONE

## P0 复审补充（审批反向竞争）

- `AgentRunService.approve` 的 approve、reject fallback 与 reject terminal 分支统一改为带 `run_id + status=waiting_human + dispatch_state=finished + expected_version` 的条件更新。
- 仅条件更新成功后才刷新 Run、迁移 Admission、写 dispatch/callback outbox 和追加审批审计；零行更新统一报 `人工审批状态版本冲突`。
- 新增跨 Session 回归：审批先读取旧版本，对账先终结并提交后，审批失败且不复活 Run、不写审批 outbox/审计、不增加 Admission queued 配额。

```text
poetry run pytest tests/test_reconciliation_service.py -q
13 passed

poetry run pytest tests/test_runtime_agent_run_service.py tests/runtime_test_worker_entry.py -q
12 passed

poetry run ruff check app/services/agent_run_service.py app/services/reconciliation_service.py app/services/lease_service.py tests/test_reconciliation_service.py
All checks passed!
```

DONE
