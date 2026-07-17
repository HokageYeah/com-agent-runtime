# Task 1 审查结果

## Critical

- **人工等待超时的 fallback 仍不能恢复执行。** `app/services/reconciliation_service.py` 的 `_timeout_terminal_status()` 在策略为 `waiting_human_timeout_action == "fallback"` 时仍记录“尚未接入”并把 Run 终结为 `failed`，既不重入队也不会让 Worker 调用 `resume`。这与 brief 的“被 Reconciler 恢复的 Run 在新 lease 下执行 resume”及“`waiting_human` approve/fallback 都能 resume”不符。修复时应由 Reconciler 使用该 Run 已冻结的 `AgentPlan.fallback_policy_json` 决定 fallback，保持 `status="waiting_human"`、设为 `dispatch_state="queued"` 并写 dispatch outbox；不得重新读取可变的 `AgentDefinition` 策略。随后 `RunQueueService` 才会按 waiting_human 状态走 `resume`。

## Important

- **`RunExecutor` protocol 未增加 `resume`。** `app/runtime/interfaces.py` 仍只声明 `run`，而 `RunQueueService` 已无条件假定执行器拥有 `resume`。这不满足 brief 的接口要求，破坏类型契约，也会让其他符合 protocol 的实现运行时失败。报告以“文件不在显式列表”为由跳过，但需求明确要求改动该接口；应补上该方法并让相关 fake/实现遵循它。

- **指定的 Task 1 测试命令不绿，现有 callback 测试应在本轮修复。** 实测 `poetry run pytest tests/runtime_test_workflow_executor.py tests/runtime_test_worker_entry.py -q` 为 `11 passed, 1 failed`。失败用例在 `tests/runtime_test_worker_entry.py::test_worker_dispatches_callback_outbox_when_callback_gateway_is_configured` 只插入 CallbackEvent/outbox、未插入对应 active `AgentRun`，因此被 `CallbackDeliveryService` 的最终隐私屏障以 `CALLBACK_RUN_NOT_ACTIVE` 正确拒绝。由于该用例就在 brief 指定的验收文件内且这轮 worker diff 新增了它，不能作为遗留失败接受；应补齐最小 active Run fixture（或按真实安全语义调整断言），直到整条指定命令通过。

- **fallback 行为的测试覆盖不足。** 新测试手工写入 `resume_from_node_id`，只证明循环可从目标开始，未验证真实 `waiting_human` 节点会从已冻结 Plan 策略写入加密 checkpoint；也没有覆盖不存在/不匹配目标返回 `FALLBACK_NODE_INVALID`，以及 approve、reject-fallback、timeout-fallback 三条路径都由 Worker 选择 `resume`。应补足这些回归测试，特别是 timeout 路径以防 Critical 问题回归。

- **范围控制偏宽。** task diff 在 `executor.py`、`worker.py` 和两个测试文件中同时混入了 Artifact、callback、Memoir 真实执行器、HTTP 配置及端到端发布改造；这些并非完成本 Task 1 所必需，且带来了上述 callback 失败。即便工作区已有并行变更，也应从本任务提交/diff 中剥离无关内容，或明确其独立验收状态，保持本任务最小且可验证。

## Minor

- **恢复起点校验实现本身安全。** `resume_from_node_id` 从加密 checkpoint 读取后会与已落库 `AgentPlan.fallback_policy_json` 及 `steps_json` 双重比对；缺失或篡改目标返回 `FALLBACK_NODE_INVALID`，不会静默从错误节点执行。Planner 也只在定义策略指向声明节点时把目标冻结到 Plan。checkpoint 摘要不会包含该私密恢复字段，日志和 callback 仅含 ID/节点名/状态，符合数据最小化约束。

- **approve 与 reject-fallback 的当前 Worker 选择正确。** 两条路径均保持 Run 的 `status="waiting_human"` 并把 `dispatch_state` 置为 `queued`；`RunQueueService` 在 claim 前读取该状态，claim 不覆盖 status，因此会调用 `resume`。但这不覆盖上述超时 fallback。

## 结论

Task 1 的 checkpoint 跳转和人工审批后的 resume 已有核心实现，但超时 fallback 未接入、接口契约缺失且指定测试集不通过，当前不能判定为完整完成。

---

# Task 1 修复复审（2026-07-17）

## Critical

- **approve 路径错误地走了 fallback 跳转。** `WorkflowExecutor._execute()` 只要人审节点返回 `waiting_human` 且 Plan 配有 `waiting_human_fallback_node`，就无条件把 `resume_from_node_id` 写入 checkpoint；`AgentRunService.approve(..., decision="approve")` 仅重新入队，不会清除或覆盖该私密字段。因此 approve 后 Worker 虽正确选择 `resume`，但 `resume()` 会从 fallback 节点开始，跳过正常审批后应继续的节点。这直接违背 brief 的“正常 approve 不写恢复目标”。现有新增测试只断言 Worker 调用了 `resume`，未覆盖 approve 后实际执行的节点序列。应将“普通恢复”和“fallback 恢复”显式区分，并新增端到端回归：approve 继续审批节点后的正常流程，timeout/reject fallback 才从冻结目标开始。

- **冻结策略为 fallback 但没有有效冻结目标时会静默线性恢复，不是安全失败。** Planner 会冻结 `waiting_human_timeout_action="fallback"`，却在目标缺失或不在 `steps_json` 时省略 `waiting_human_fallback_node`；人审 checkpoint 随之不含 `resume_from_node_id`。Reconciler 只看 action 就重入队，Executor 因恢复字段为 `None` 不触发 `FALLBACK_NODE_INVALID`，会从 completed 节点之后按普通顺序执行。现有“invalid fallback”测试手工构造了 checkpoint 中的非法字段，未覆盖这个真实生成路径。应在 Plan 生成、人审暂停或 Reconciler 重入队前验证 action= fallback 必须同时有声明于 Plan steps 的冻结目标；否则明确失败为 `FALLBACK_NODE_INVALID`，绝不能普通 resume。

## Important

- **此前 findings 已修复。** timeout fallback 现仅依据 `AgentPlan.fallback_policy_json`，保持 `waiting_human` 后重入队并写 dispatch outbox；`RunExecutor` 已声明 `resume`；callback 测试补齐了对应 active Run；真实人审 checkpoint 加密字段和缺失目标的直接安全失败也已有测试。实测 `poetry run pytest tests/test_reconciliation_service.py tests/runtime_test_workflow_executor.py tests/runtime_test_worker_entry.py -q` 为 `18 passed`，指定 ruff 检查通过。

## Minor

- 日志、callback/outbox 及 checkpoint 摘要仍未暴露日记正文、快照或完整播放文档；fallback 字段仅存在加密 checkpoint，数据最小化边界保持正确。

## 复审结论

修复解决了原先的超时重入队、协议和测试阻断，但 approve/fallback 的恢复语义尚未区分，且无有效目标的 fallback 可错误线性执行；Task 1 仍不能验收。

---

# Task 1 最终复审（2026-07-17）

## Critical

- 无遗留 Critical。普通 approve 会清除 `WAITING_HUMAN_FALLBACK` 标记，`WorkflowExecutor` 因而忽略 checkpoint 中预置的私密目标并按已完成节点线性恢复；timeout 与 reject fallback 则仅在设置该标记后才校验并消费 `resume_from_node_id` 跳转。approve 的真实 Worker 闭环测试已断言节点序列为 `review -> continue -> fallback`，证明不会直接跳至 fallback。

- 无遗留 Critical。Reconciler 仅依据冻结的 `AgentPlan.fallback_policy_json` 判断 timeout fallback，并通过 `_has_valid_fallback_target()` 要求目标为字符串且出现在冻结步骤中；缺失或越界目标会以 `FALLBACK_NODE_INVALID` 终结、写终态 callback、且不创建 dispatch。Executor 对带 fallback 标记的恢复再次做相同校验，形成纵深安全失败。

## Important

- 无遗留 Important。reject fallback 也已改为读取冻结 Plan 的 `reject_action`，并设置同一恢复标记；普通 queued Run 仍走 `run`，`waiting_human -> queued` 的恢复走 `resume`。`RunExecutor.resume` protocol 已同步。

## Minor

- `test_executor_resume_from_fallback_checkpoint_starts_at_fallback_node` 未设置 fallback 标记，且其 completed nodes 恰好覆盖目标之前的所有节点，因此即便退化为普通 resume 也会通过。现有 approve 真实闭环及代码分支已覆盖关键语义，故不阻断验收；后续可将该单测改为显式设置标记或在目标前留一个未完成节点，以直接锁定跳转门控。

## 最终结论

两项上一轮 Critical 均已真正修复。实测相关四个测试文件 `26 passed`，指定 ruff 检查通过；Task 1 可验收。
