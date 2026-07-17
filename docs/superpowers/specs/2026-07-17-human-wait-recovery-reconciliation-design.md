# 人工等待恢复与完整对账设计

## 目标

让 AgentRun 在人工等待超时后按冻结 Package 策略安全恢复或终结；让独立 Reconciler 对运行时可由数据库权威状态判定的异常进行幂等修复。

## 范围与非目标

本次只覆盖 `waiting_human` 恢复、lease/dispatch/held/queued 超时和 package/authorization 撤销。复用现有 `AgentRun`、Admission、Lease、Outbox 和 Worker 协议，不引入新队列、定时框架或第二套状态机。

不在本次实现真实模型调用、通用图工作流引擎、回调密钥轮换或复杂审核台。

## 方案选择

采用“恢复目标写入 checkpoint + 既有静态执行器从目标节点继续”的方案。相比把 fallback 直接改成失败，它满足 Package 的冻结策略；相比新增工作流调度器，它只扩展现有静态 `AgentPlan` 的执行起点，边界最小。

## 人工等待与 fallback

1. 人审节点完成时，执行器先保存加密 checkpoint；checkpoint 含已完成节点和可选 `resume_from_node_id`，但安全 Artifact、callback 与日志不包含日记正文或播放文档。
2. `waiting_human_timeout_action=fallback` 时，Reconciler 从冻结 Definition 读取已校验的 fallback 节点，使用 `status=waiting_human AND dispatch_state=finished AND status_version=期望值` 的条件更新，把 run 重新置为 `pending/queued`，清除等待截止时间，写入新的 `run_dispatch` outbox，并由 Admission 迁移 `finished -> queued`。
3. Worker 取得新 lease 后调用 `WorkflowExecutor.resume`。执行器读取 checkpoint，并从 `resume_from_node_id` 开始执行；恢复节点之前的普通节点均跳过。恢复节点不存在或 checkpoint 不兼容时，安全终结为 `failed`，错误码仅表达原因。
4. 超时策略为 `failed` 或 `cancelled` 时，仍在同一条件更新中终结、释放 Admission 并写 callback。审批与对账竞争时，只有一个条件更新成功；另一个路径不写入任何状态或 outbox。

## 完整 Reconciler

单次 `run_once(now)` 扫描以下权威状态并输出只含计数的报告：

- `waiting_human + finished` 超时：fallback、failed 或 cancelled。
- `claimed` 且 lease 到期：调用既有 lease reaper，回到 queued 并写一个 dispatch。
- `held` 超时：终结为 cancelled/failed（沿用现有创建策略定义的安全错误码），释放 held Admission 并写 callback。
- `queued` 超时：终结为 failed，释放 queued Admission 并写 callback。
- `run_dispatch` dead letter：统计并将关联 run 安全终结为 `DISPATCH_FAILED`，仅处理仍 queued 的 run。
- 已撤销 Package/授权：held、queued、waiting_human 直接安全终结；claimed 仅写取消请求，仍由有效 Worker 在安全边界退出。

每一类修复以状态、dispatch_state、版本或 lease 条件限制，并且只在成功变更后创建 Outbox，确保多实例重复执行不会重复推进状态。

## 安全与可观测性

- Reconciler、Lease、Executor 日志只记录 run ID、状态、策略、计数和错误码。
- 不能记录 checkpoint 明文、工具 payload、日记正文、快照或完整播放文档。
- `ReconciliationReport` 只包含扫描、修复、跳过、失败和死信数量。

## 验收测试

1. fallback 超时会以一次条件更新重新入队，Worker 从指定 fallback 节点恢复。
2. 超时与 approval 并发时，旧 `status_version` 不会覆盖已审批或已终结状态。
3. lease、held、queued 超时与 dispatch 死信各自只修复一次，并正确迁移 Admission 与 Outbox。
4. package/authorization 撤销按 run 当前 dispatch 状态分别终结或请求取消。
5. 多次运行 Reconciler 后状态、Outbox 与 Admission 计数保持稳定；日志/摘要不含私密正文。
