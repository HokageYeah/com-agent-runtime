# Task 1 report

## Completed changes

- `app/runtime/planner.py`: freezes a definition policy's `waiting_human_fallback_node` into the static plan only when it identifies a declared workflow node.
- `app/runtime/executor.py`: writes `resume_from_node_id` only to the encrypted checkpoint for a human-wait fallback, validates it on resume, rejects absent/mismatched targets with `FALLBACK_NODE_INVALID`, and starts the iteration at that node.
- `app/services/run_queue_service.py`: records the Run status before claim and dispatches a reclaimed `waiting_human` Run through `resume`; normal queued Runs still use `run`.
- `app/worker.py`: adds safe `resume` entry points to the bootstrap and configured executors.
- Tests added:
  - fallback checkpoint executes only the fallback node;
  - a queued, reconciled `waiting_human` Run uses `resume`, not `run`.

No log statement or public output includes checkpoint contents, fallback checkpoint values, workflow payloads, or business document text.

## TDD evidence

RED command:

```text
poetry run pytest tests/runtime_test_workflow_executor.py tests/runtime_test_worker_entry.py -q
```

Result: `3 failed, 9 passed`.

- New executor test failed because `resume_from_node_id` was passed into `AgentState`, which rejects unknown fields.
- New Worker test failed because `RunQueueService.consume` called `run` and never `resume`.
- One pre-existing callback-delivery test also failed; see risks below.

GREEN commands:

```text
poetry run pytest tests/runtime_test_workflow_executor.py -q
```

Result: `8 passed`.

```text
poetry run pytest tests/runtime_test_worker_entry.py -q
```

Result: `3 passed, 1 failed`.

The two Task 1 regression tests pass. The remaining failure is unrelated baseline/test-fixture setup:
`test_worker_dispatches_callback_outbox_when_callback_gateway_is_configured` inserts a callback event/outbox entry but no corresponding `AgentRun`; `CallbackDeliveryService` correctly rejects it with `CALLBACK_RUN_NOT_ACTIVE`.

## Risks / follow-up

- The brief requests adding `resume` to `RunExecutor` protocol, but `app/runtime/interfaces.py` is not in the explicit permitted file list. I intentionally did not modify it. Runtime implementations now expose `resume`; the protocol declaration should be updated by the owner if the file restriction is lifted.
- The current workspace contains substantial existing uncommitted Task 6-era changes in all target files. This task's edits were kept limited to the Task 1 behavior above; no reset, staging, or commit was performed.

## Review remediation (2026-07-17)

- `app/runtime/planner.py`: freezes the validated
  `waiting_human_timeout_action` together with the already-frozen fallback
  node in `AgentPlan.fallback_policy_json`.
- `app/services/reconciliation_service.py`: timeout fallback now reads only
  the Run's `AgentPlan.fallback_policy_json`; it keeps `status=waiting_human`,
  atomically moves `dispatch_state` from `finished` to `queued`, migrates
  Admission, clears the expired wait deadline, and writes one
  `run_dispatch` outbox event. The existing state guard makes the repair
  idempotent: after the transition the Run is no longer eligible for the
  timeout query. Definition changes cannot alter this outcome.
- `app/runtime/interfaces.py`: `RunExecutor` now declares `resume`; existing
  Worker fakes and concrete executors implement it.
- `tests/runtime_test_worker_entry.py`: callback delivery now includes the
  corresponding terminal `AgentRun`, matching the callback service's active
  run ownership check.
- Added regressions for frozen timeout policy, Reconciler fallback requeue,
  Worker `resume` after timeout fallback, a real human-wait encrypted
  fallback checkpoint, and invalid fallback safe failure.

## Remediation TDD and verification evidence

RED command:

```text
poetry run pytest tests/test_reconciliation_service.py tests/runtime_test_workflow_executor.py tests/runtime_test_worker_entry.py -q
```

Result: `3 failed, 14 passed`.

- The frozen Plan timeout-fallback test failed because Reconciler changed the
  Run to `failed/finished` instead of requeuing it.
- The Worker timeout-fallback test consequently found no dispatchable Run.
- The human-wait checkpoint assertion initially attempted to read after the
  lease had correctly been released; the test was corrected to decrypt the
  persisted checkpoint directly, without bypassing the production access
  control boundary.

GREEN command:

```text
poetry run pytest tests/runtime_test_workflow_executor.py tests/runtime_test_worker_entry.py -q
```

Result: `14 passed`.

Final verification commands:

```text
poetry run pytest tests/test_reconciliation_service.py tests/runtime_test_workflow_executor.py tests/runtime_test_worker_entry.py -q
# 18 passed

poetry run ruff check app/runtime/interfaces.py app/runtime/planner.py app/services/reconciliation_service.py tests/test_reconciliation_service.py tests/runtime_test_workflow_executor.py tests/runtime_test_worker_entry.py
# All checks passed
```

No log, callback, outbox payload, or report entry contains checkpoint state,
fallback checkpoint values, workflow payloads, or business document text.

## Follow-up review remediation (2026-07-17)

- A private checkpoint may retain its encrypted fallback target, but it is now
  consumed only when a safe, non-private Run marker
  `WAITING_HUMAN_FALLBACK` is set. Normal `approve` clears that marker;
  `WorkflowExecutor.resume` then ignores the target and resumes after the
  checkpoint's completed nodes. Timeout and reject fallback set the marker,
  so only those paths perform the deterministic jump.
- Reject fallback now uses the frozen Plan policy rather than rereading a
  mutable Definition. `StaticPlanner` freezes its validated `reject_action`.
- Reconciler validates that a timeout-fallback target is a string declared in
  the frozen Plan steps before requeueing. A missing or out-of-plan target
  reaches the normal terminal failure transaction with
  `FALLBACK_NODE_INVALID`, callback, and no dispatch event. Executor applies
  the same validation when a fallback-marked Run is resumed.

RED command:

```text
poetry run pytest tests/test_reconciliation_service.py tests/runtime_test_worker_entry.py -q
```

Result: `2 failed, 9 passed`.

- Normal approve resumed directly at the fallback node, skipping the ordinary
  continuation node.
- Reconciler requeued a timeout fallback whose frozen target was absent from
  the Plan steps.

Final verification:

```text
poetry run pytest tests/test_reconciliation_service.py tests/runtime_test_workflow_executor.py tests/runtime_test_worker_entry.py tests/test_runtime_agent_run_service.py -q
# 26 passed

poetry run ruff check app/runtime/executor.py app/runtime/planner.py app/services/agent_run_service.py app/services/reconciliation_service.py tests/test_reconciliation_service.py tests/runtime_test_workflow_executor.py tests/runtime_test_worker_entry.py tests/test_runtime_agent_run_service.py
# All checks passed
```
