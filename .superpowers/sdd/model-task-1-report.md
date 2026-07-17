# ModelGateway Task 1 报告

DONE

## 实现内容

- 新增冻结的 `ModelRoute`，在注册时校验 endpoint、TTL 覆盖 timeout + settle margin、限流上限、价格单位与价格值。
- `Settings.MODEL_ROUTES_JSON` 仅解析服务端预注册路由，并在读取时完成同一套校验。
- 新增 `ProviderTrafficController`：以单个 Redis Lua 脚本原子维护 `acquired → started → settled` 状态、共享并发/RPM/TPM、过期 permit 清理和 Retry-After 冷却。
- Redis/脚本响应异常均返回 `redis_unavailable`，不会降级为进程内放行；安全日志只含 operation 与 route_id，不记录 prompt 或 Provider 正文。
- `settle` 对已结算或已过期 permit 返回 `already_settled`，不会重复释放并发计数。

## 测试

已执行：

```text
poetry run pytest tests/test_provider_traffic_controller.py -q
7 passed

poetry run ruff check app/runtime/model_gateway.py app/core/config.py tests/test_provider_traffic_controller.py
All checks passed
```

## P1 审查修复

- `mark_started` 仅在 `acquired → started` 的原子转换时返回 `started`；重放返回 `already_started`，不会重复取得发送权。
- permit 首次 acquire 后绑定 Redis route key；重复 acquire、`mark_started` 和 `settle` 遇到不同 route 一律返回 `route_mismatch`，不改动计数或状态。
- Retry-After 写入共享冷却时取既有 `blocked_until` 与新截止时间的较大值，短冷却不会缩短已有冷却窗口。
- `Settings` 在实例化时校验 `MODEL_ROUTES_JSON`，重复 `route_id` 会立即拒绝；属性读取仍返回经过校验的路由。
- 安全日志仍只包含操作名和 `route_id`，不包含 prompt、Provider 正文或密钥。

## P1 验证

```text
poetry run pytest tests/test_provider_traffic_controller.py -q
12 passed

poetry run ruff check app/runtime/model_gateway.py app/core/config.py tests/test_provider_traffic_controller.py
All checks passed

git diff --check
passed
```

## P1 复审修复

- permit 现在额外持久化冻结的 `route_id`；共享 `rate_limit_key` 仅用于配额计数，不能作为 permit 的身份边界。
- 重放 acquire、`mark_started`、`settle` 均比较 permit 的 `route_id`。即使两个路由共用配额 key，不同 `route_id` 仍返回 `route_mismatch`，且不修改状态或计数。
- 回归用例改为同一 `rate_limit_key`、不同 `route_id`，覆盖三种跨路由操作。

## P1 复审验证

```text
poetry run pytest tests/test_provider_traffic_controller.py -q
12 passed

poetry run ruff check app/runtime/model_gateway.py app/core/config.py tests/test_provider_traffic_controller.py
All checks passed
```
