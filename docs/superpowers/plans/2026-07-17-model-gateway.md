# ModelGateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 Redis 共享模型流控、可信路由与安全 usage 生命周期。

**Architecture:** 受信任路由配置生成不可变 `ModelRoute`；Redis Lua/原子操作维护 permit；Gateway 在 HTTP 前后执行 Runtime 边界复核，Usage 服务只保存安全计量摘要。

**Tech Stack:** Python、SQLAlchemy、Redis、httpx、pytest、Ruff。

## Global Constraints

- Redis 不可用时 fail closed，绝不退化为进程内无限调用。
- 日志、usage、Artifact 与 callback 不得保存 prompt、日记正文或模型原文。
- route endpoint/key/价格/流控参数只能来自服务端配置。
- 不执行 git 提交或覆盖既有未提交改动。

### Task 1: Route 注册与 Redis permit 控制器

**Files:** `app/runtime/model_gateway.py`、`app/core/config.py`、`tests/test_provider_traffic_controller.py`

- [ ] 先写 Redis 不可用 fail-closed、并发上限、重复 settle、Retry-After 共享冷却的失败测试。
- [ ] 实现 `ModelRoute` 校验（TTL、价格、单位、endpoint）与 `ProviderTrafficController.acquire/mark_started/settle`。
- [ ] 运行 `poetry run pytest tests/test_provider_traffic_controller.py -q`，确认通过。

### Task 2: Usage 生命周期与 Gateway 安全边界

**Files:** `app/services/model_usage_service.py`、`app/runtime/model_gateway.py`、`tests/test_model_gateway.py`

- [ ] 写 acquire 后取消/失租不请求 Provider、发送前中止、429、超时 unknown、重复结算的失败测试。
- [ ] 实现 `ModelCallContext`、usage running/started/settled 与 HTTP 注入 adapter；发送前后调用 LeaseService 边界校验。
- [ ] 运行 `poetry run pytest tests/test_model_gateway.py -q`，确认通过。

### Task 3: Memoir 模型节点接入与回归

**Files:** `app/agents/memoir_agent/runner.py`、`tests/test_memoir_model_gateway.py`

- [ ] 写模型能力不可用时模板 fallback、结构化输出非法时安全 fallback 的失败测试。
- [ ] 仅将现有可替换的模型节点接入 Gateway，保留模板 fallback，不在本任务引入 PromptRegistry/ContextManager。
- [ ] 运行该测试及 Memoir Runner 回归。

### Task 4: 文档与全量验证

**Files:** 总控计划、当前计划。

- [ ] 仅标记实际完成的 Task 8 / P0 条目 `[✅]`，未实现的 PromptRegistry、ContextManager、Evaluator 保持 `[ ]`。
- [ ] 运行 `poetry run pytest -q && poetry run ruff check app tests alembic && git diff --check`。
