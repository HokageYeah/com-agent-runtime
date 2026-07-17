# Reconciler 运营化设计

## 目标

把现有安全对账服务提升为可周期运行、可多实例部署、可观测且不泄露私密数据的运营组件。

## 设计

- 使用数据库 `RuntimeReconciliationLease`（或等价单行租约）保证同一时刻仅一个实例执行；lease 到期可接管，未持有 lease 的实例立即退出。
- `app.reconciler` 支持 `--once` 与默认 300 秒循环；每轮创建短生命周期 Session，避免长事务。
- 扩展 `ReconciliationReport` 为只含安全计数：扫描、修复、失败、告警、动作类型；日志仅输出计数、错误码和对象 ID。
- callback dead letter 只计数并保留原事件身份供 Dispatcher 重放；不回写终态 Run。
- 过期 `AgentModelUsage.running` 以 status 条件更新为 `outcome_unknown`，只保留预留成本，不能覆盖并发 settled 结果。
- AdmissionBucket 以 AgentRun 的 dispatch_state 聚合为权威来源；仅在 bucket version 条件成功时修复，防止覆盖并发迁移。
- 每类修复连续失败三次后输出结构化 warning；第一版不依赖外部告警系统。

## 非目标

不实现 callback 业务端重放协议、授权版本主动扫描、分布式指标平台或模型 Provider 结果补查。

## 验收

- 两实例同时运行只执行一次扫描。
- lease 到期后可接管。
- usage 并发终态不被对账覆盖。
- Admission 漂移可修复且不会覆盖并发 bucket 写入。
- full report 和日志不含 prompt、模型正文、日记或快照。
