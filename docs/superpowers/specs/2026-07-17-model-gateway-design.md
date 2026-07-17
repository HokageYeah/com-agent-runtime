# ModelGateway 与流量控制设计

## 目标

以 Redis 为多 Worker 共享流控真相，实现受信任模型路由、permit 生命周期、调用安全边界与 AgentModelUsage 安全记账。

## 边界

第一版提供可注入 HTTP Provider Adapter 与结构化 JSON 校验入口；不接入 LangChain、动态工具选择、ContextManager 或完整 PromptRegistry。路由、endpoint、密钥、价格与限流参数只能来自受信任配置。

## 组件

- `ModelRoute`：冻结 route ID、provider/model、rate limit key、并发/RPM/TPM、超时、permit TTL、熔断与价格配置；注册时验证 TTL 不小于 timeout 加 settle margin。
- `ProviderTrafficController`：用 Redis Lua 原子维护 permit 的 `acquired → started → settled` 状态，拒绝 blocked、熔断、超额或失效 permit。Redis 不可用时 fail closed。
- `ModelGateway`：只从有效 LeaseContext 构造 ModelCallContext；acquire 后、发送前、响应后重新校验运行边界。失效请求不会发送给 Provider。
- `ModelUsageService`：每个候选请求记录独立 AgentModelUsage；发送前中止不计成本，超时/崩溃的 running 记录由对账转为 outcome_unknown 并保留预留成本。

## 调用流程

1. Gateway 从受信任 route registry 解析 route，校验能力和 deadline。
2. Redis acquire 成功后再次检查 lease、cancel、privacy、authorization 与 route。
3. 写入 running usage，mark_started 成功后才发送 HTTP；发送前失效则 settle 为 aborted_before_send。
4. 响应后再次校验。有效时返回经 schema 校验的结构化结果；失效时丢弃内容，只结算 usage。
5. 429 写入共享 blocked_until；网络超时保守结算为 outcome_unknown，不记录 prompt 或响应正文。

## 运营化 Reconciler（后续阶段）

独立任务提供周期入口与多实例 lease，处理 callback dead letter、AdmissionBucket 漂移与过期 running model usage；每个批次输出安全计数，不写私密 payload。

## 验收

- 多 Worker 同 route 不会越过并发/RPM/TPM。
- Redis 故障不请求 Provider。
- acquire 后撤权、取消或失租不发送请求。
- 429 对其他 Worker 共享退避；重复 settle 不重复释放计数。
- usage 状态、成本与 token 结算正确，日志不含 prompt/私密素材。
