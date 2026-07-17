### Task 2: Usage 生命周期与 Gateway 安全边界

**Files:** `app/services/model_usage_service.py`、`app/runtime/model_gateway.py`、`tests/test_model_gateway.py`

- [ ] 写 acquire 后取消/失租不请求 Provider、发送前中止、429、超时 unknown、重复结算的失败测试。
- [ ] 实现 `ModelCallContext`、usage running/started/settled 与 HTTP 注入 adapter；发送前后调用 LeaseService 边界校验。
- [ ] 运行 `poetry run pytest tests/test_model_gateway.py -q`，确认通过。

