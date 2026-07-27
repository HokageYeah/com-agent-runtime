# AgentRuntime 验证流程

## 安全前提

- 只在隔离 staging 环境执行；不得使用生产数据库、Redis、对象桶或密钥。
- 使用独立数据库、独立 Redis namespace、随机测试 HMAC key 和回环 mock 服务。
- 禁止在命令行、日志、截图或工单中放入 prompt、业务正文、模型原文、工具 payload、签名 URL、checkpoint 正文或密钥。

## 代码门禁

在仓库根目录执行：

```bash
poetry run pytest -q
poetry run ruff check .
poetry run mypy app
poetry run alembic heads
git diff --check
```

预期：pytest、Ruff、Mypy、diff 检查均成功，Alembic 只显示一个 head。

## 隔离运行验证

1. 以临时数据库运行 `poetry run alembic upgrade head`。
2. 使用测试配置分别启动 API、worker、reconciler 与业务 mock；worker 使用 `python -m app.worker --worker-id staging-worker`，reconciler 使用 `python -m app.reconciler --interval-seconds 300`。
3. 访问 `/healthz` 与 `/readyz`，确认服务存活且依赖就绪；响应不得泄露连接串或密钥。
4. 通过测试业务服务执行 held create、bind、start，确认 baseline 在发布前可读，成功后仅切换完整 revision。
5. 确认 callback 的 `event_id/event_seq/status_version` 单调；重复投递使用原事件身份，不重复推进业务状态。
6. 注入 Provider 超时、callback 暂时失败、授权撤销、generation epoch 变化和 privacy purge；确认分别模板 fallback、原事件重放、外部发送停止、旧 run 不能发布和 purge 后才完成业务侧清理。
7. 检查安全日志、public trace、callback、审计、artifact 与 checkpoint：只能包含 ID、状态、错误码、计数、预算、版本和时间摘要。

## 进程回收

测试 harness 必须为每个子进程设置有限超时；无论成功、失败或断言失败，都在 finally 中 terminate、wait，并清理临时目录。超时视为失败，禁止留下后台进程或复用临时数据库。
