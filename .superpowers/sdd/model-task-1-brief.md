### Task 1: Route 注册与 Redis permit 控制器

**Files:** `app/runtime/model_gateway.py`、`app/core/config.py`、`tests/test_provider_traffic_controller.py`

- [ ] 先写 Redis 不可用 fail-closed、并发上限、重复 settle、Retry-After 共享冷却的失败测试。
- [ ] 实现 `ModelRoute` 校验（TTL、价格、单位、endpoint）与 `ProviderTrafficController.acquire/mark_started/settle`。
- [ ] 运行 `poetry run pytest tests/test_provider_traffic_controller.py -q`，确认通过。

