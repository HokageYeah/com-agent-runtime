# ModelGateway Task 2 报告

## Review P1 修复（2026-07-17）

- `ModelUsageService.create_running` 回读权威 `AgentRun`，验证 execution attempt、lease owner 和 fencing token；调用上下文中的 capability、prompt 与 pricing 元数据一律不落库。
- Gateway 在 permit acquire 后、usage 创建后、`mark_started` 后紧贴 HTTP 调用前，以及响应后均复核 deadline、context lease 与 LeaseService 边界。过期 request deadline、失租、撤权或取消均不会发送 Provider 请求。
- `LeaseService.can_write` 现在检查数据库 lease 到期、context lease 到期和 run deadline；为 SQLite 读回的无时区时间按 UTC 归一。
- Provider 成功响应仅解析 `usage.input_tokens/output_tokens`（兼容 `prompt_tokens/completion_tokens`），并使用本次冻结 route 的 input/output price 计算实际成本；没有有效 usage 时 tokens 保持空值且保留预估成本。
- `mark_started` 与 `settle` 改为带前置状态的条件 UPDATE；多个 worker/session 的重放或竞态仅第一个能写入终态、tokens 与成本。
- Redis 仍保持 fail-closed；新增逻辑不记录请求、响应或私密元数据。

验证：

```text
poetry run pytest tests/test_model_gateway.py tests/runtime_test_worker_lease_fencing.py tests/test_provider_traffic_controller.py -q
29 passed

poetry run ruff check app/runtime/model_gateway.py app/services/model_usage_service.py app/services/lease_service.py tests/test_model_gateway.py
All checks passed

git diff --check
passed
```

DONE

## 路由授权来源 P1 修复（2026-07-17）

- `AgentRunService` 只接收应用层注入的、已验证服务端模型 route ID，并在 create 时冻结为 `capability_snapshot_json.allowed_model_route_ids`；默认空集合 fail-closed。
- Run 创建 API 从 `request.app.state.settings.model_routes` 提取 route ID 注入服务；`CreateRunCommand` 与 `input` 无法覆盖该冻结集合。
- 端到端回归通过真实 `AgentRunService.create` 构造 Run：服务端允许的 `summary` 可调用，而 command input 伪造的 `other` route 仍被 Gateway 在 acquire 前拒绝。

DONE

## 路由授权 P1 修复（2026-07-17）

- `ModelCallContext.from_authoritative` 从 `AgentRun.capability_snapshot_json.allowed_model_route_ids` 冻结允许的 route ID；缺失或畸形字段按空集合 fail-closed。
- Gateway 在 Redis acquire 前验证请求的 route 是否在该冻结集合中。未授权 route 返回 `route_not_allowed`，不会写 usage、申请 permit 或调用 Provider。
- usage 建账再次比较冻结 route 集合并校验当前 route，防止绕过 Gateway 的直接建账路径。

新增回归断言：任一有效 Step 请求已注册但未冻结授权的 route 时，Provider 调用数为零、Redis permit 为空、usage 表无记录。

DONE

## P1 复审补充修复（2026-07-17）

- Reconciler 的过期 usage 收敛改为单条带 `status IN (running, started)` 的条件 UPDATE；它不能再覆盖其他 worker 已写入的最终状态和成本。
- Gateway 在 acquire 成功后对 usage 建账使用异常安全边界；建账拒绝或失败会立即 settle Redis permit，并以安全的 `aborted_before_send` 返回，不会泄漏共享并发配额。
- `ModelCallContext` 已取消公开 dataclass 构造与调用方传入的 step attempt、token 估算、deadline 和私密元数据。`from_authoritative` 仅从数据库中同一 Run 的 `running` Step、Lease 和 Step input summary 派生这些字段；usage 建账再次核验 Run/Step/attempt、估算与 deadline，拒绝伪造的 step 或预算。

复审验证：

```text
poetry run pytest tests/test_model_gateway.py tests/runtime_test_worker_lease_fencing.py tests/test_provider_traffic_controller.py -q
31 passed

poetry run ruff check app/runtime/model_gateway.py app/services/model_usage_service.py app/services/lease_service.py tests/test_model_gateway.py
All checks passed

git diff --check
passed
```

DONE

## 完成内容

- 新增 `ModelCallContext.from_lease`：调用身份必须关联既有 `LeaseContext`，不接受请求方提供路由、endpoint、价格或 ownership。
- 新增 `ModelUsageService`：持久化每次物理 attempt 的安全计量元数据，维护 `running → started → outcome`；发送前中止计为零成本，超时和网络未知结果保留预留成本；重复结算保持原结果。
- 新增可注入的 `HttpProviderAdapter` 和 `ModelGateway`：从已注册 route 获取 Provider 配置，acquire 后、发送前及响应后复核 `LeaseService.can_write`。取消、失租、隐私或授权变化均不会在发送前触网；响应后的失效会丢弃正文。
- 429 读取 `Retry-After` 并结算 Redis permit 以写入共享冷却；超时、网络失败和非预期 Provider 异常保守结算为 `outcome_unknown`。
- Gateway、usage 服务和日志均不持久化或记录 prompt、请求正文或 Provider 响应正文。

## TDD 证据

RED：

```text
poetry run pytest tests/test_model_gateway.py -q
ImportError: cannot import name 'ModelCallContext'
```

测试先定义了 acquire 后取消、发送前撤权、429、超时 unknown 和重复结算行为；导入失败是因为实现尚不存在。

GREEN / 验证：

```text
poetry run pytest tests/test_model_gateway.py tests/test_provider_traffic_controller.py -q
17 passed

poetry run ruff check app/services/model_usage_service.py app/runtime/model_gateway.py tests/test_model_gateway.py
All checks passed

git diff --check
passed
```

为兼容 SQLite 测试与 MySQL 运行环境，usage 服务生成随机 63-bit 数值主键；原因是现有 ORM 的 `BigInteger` 主键在 SQLite 中不映射为 rowid 自动生成。
