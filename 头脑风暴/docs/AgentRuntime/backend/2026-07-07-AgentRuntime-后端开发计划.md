# AgentRuntime 后端 Implementation Plan

> **2026-08-13 当前跨仓门禁：M3 COMPLETE / M4 GO。** 专用 Docker MySQL
> `127.0.0.1:33306` 已真实通过 same/conflicting fingerprint、目标锁等待与权限负向验证
> （`3 passed, 47 deselected`）；离线 guard `14 passed, 3 skipped, 47 deselected`。Runtime
> 全量 `733 passed, 22 skipped`、Ruff/Mypy/Alembic/diff-check 均通过。M4 仅可开始
> B11、F5–F7，仍无 Runtime 生产代码所有权。

> **2026-08-06 跨项目校准：** Runtime 只保留公共 Run/Worker/Tool/Callback 责任；本仓库现存回忆录 Archive、Snapshot、密码、播放文档和关系解绑实现属于迁移证据，目标归属为 `couple-diary-b`。公共 API 以当前代码 `/api/v1/runtime/health/*`、`/api/v1/runtime/capabilities`、`/api/v1/runtime/agent-runs` 为准。`memoir_agent@1.0.0` 输入不得携带 owner/space/关系段等业务身份字段。历史本地 revision 0 的来源派生统计只用于迁移盘点，目标 revision 0 采用情侣日记计划冻结的通用安全 baseline。

> **2026-08-13 历史代码闭合记录：** MySQL 运行时观测曾待显式隔离 DSN；现已在业务端 harness 实测 `2 passed, 47 deselected`，该待验证项关闭，以页首 **M3 COMPLETE / M4 GO** 为准。fixture SHA：v1.0 `04a0c12594e0ee1ca062b40842d1d4140aaad52d7f63b9a6c8dc03f9cba1b929`、v1.1 `7500539a671d13e58d688c95b78eaf8d74c06c80bc146142b64dda40907553c4`。

> **本复核对历史记录的优先级说明：** 下文所有“M3 COMPLETE/M4 GO”“v1.0 已包含九码五字段强化”或“v1.0/所有版本均要求显式 `details_visible_to_model`”的陈述均保留为历史审计，现已被本段取代。v1.0 保持四字段 HEAD wire，省略的 `details_visible_to_model` 默认 `false`；显式五字段/九码只属于经 `X-Agent-Tool-Contract-Version` 协商的 v1.1。内部 `memory.*` Tool 的非 2xx 直接返回协商 ToolError JSON，不得借用普通业务 `ret/data` 或 FastAPI `detail`；普通业务 API 合同不变。context `business_id` 必须与实际 Archive 业务 ID 相同，旧“仅校验存在”的历史决定失效。

> **2026-08-07 R1 路由门禁边界（迁移源盘点，非重做）：** Task 6.5（归档/快照/播放文档收尾）与 Task 10.75（密码/列表/用户侧 API）对应的本仓代码均判定为“仓内历史实现已完成、目标架构待迁移”：模型、迁移、路由、service 不删除、不重写，作为迁移证据保留；本仓已在 R1 实现 `app/api/api.py` 的环境门禁——`production` 仅注册 `/api/v1/runtime/*` provider，业务路由仅在 `development` / `test` 注册以保留审计能力。下方 checkbox 维持历史勾选状态，不再作为目标 baseline 重审；目标实现以 `couple-diary-b` 计划为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在当前 com-agent-runtime 根工程内建设公共 AgentRuntime 模块，提供公共 Agent 注册、运行、工具调用、模型调用、checkpoint、评价、callback 和观测能力，并以 `memoir_agent@1.0.0` 跑通第一版 Workflow Agent 闭环。

**Architecture:** Runtime 直接位于当前 com-agent-runtime 根工程的 `app/` 目录。FastAPI 处理短请求，权威数据库保存 run、outbox、lease、fencing、checkpoint 和幂等记录，后台 worker 执行 LangGraph workflow。Arq/Redis 只负责调度通知和限流加速；消息丢失或重复不能改变正确性。ToolGateway 通过预注册 HTTP Business Connector 访问情侣日记后端，回忆录作品只通过 `memory.publish_playback_document` 原子发布。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL（推荐）或具备同等事务/锁语义的 MySQL、Redis + Arq（可选通知）、LangGraph Python、LangChain Python、LiteLLM、httpx、pytest、ruff、mypy、OpenTelemetry / LangSmith 可选。

## Global Constraints

- 当前 com-agent-runtime 根工程是唯一的 Runtime 工程；禁止创建嵌套 `services/agent-runtime`、第二份 `pyproject.toml`、Alembic 或同名 `app` 包。
- 当前工作区 `回忆录技术探索/00-README.md` 至 `15-业务Agent接入规范.md` 是权威基线。
- 第一版只支持 Workflow Agent；动态规划、MCP 完整接入、Runtime 原生 SSE 放二期。
- API/Event/Tool/Artifact 使用版本化 Pydantic + JSON Schema Contract，破坏性变更提升 major version。
- Runtime 不直连业务数据库，不把回忆录正文当作权威副本；Artifact 默认只存摘要、digest 和业务引用。
- 交付语义固定为 at-least-once + 幂等副作用，不宣称 exactly-once。
- worker 单写者由数据库 lease + fencing token 保证，Redis lock 只能作为优化。
- 副作用幂等键按逻辑操作稳定，不能包含 execution/step/tool attempt。
- 小程序通过业务后端 HTTP 退避轮询状态；SSE 仅作已验证平台的可选增强。
- 第一版默认关闭 TTS、封面和视频；媒体能力只保留契约插槽。
- 所有私密 payload 使用加密、TTL、privacy version 条件写和 purge 删除联动。

---

## 0. 输入资料

实现前必须阅读：

- `情侣日记文档/头脑风暴/回忆录技术探索/00-README.md`
- `情侣日记文档/头脑风暴/回忆录技术探索/02-总体技术架构.md`
- `情侣日记文档/头脑风暴/回忆录技术探索/04-MemoirAgent业务工作流.md`
- `情侣日记文档/头脑风暴/回忆录技术探索/06-后端接口与AgentRuntime集成.md`
- `情侣日记文档/头脑风暴/回忆录技术探索/07-安全隐私与情绪安全.md`
- `情侣日记文档/头脑风暴/回忆录技术探索/08-实施路线图.md`
- `情侣日记文档/头脑风暴/回忆录技术探索/09-公共AgentRuntime架构.md`
- `情侣日记文档/头脑风暴/回忆录技术探索/10-Agent工具协议与MCP适配.md`
- `情侣日记文档/头脑风暴/回忆录技术探索/11-LangGraph与LangChain工作流设计.md`
- `情侣日记文档/头脑风暴/回忆录技术探索/12-模型网关与Prompt工程.md`
- `情侣日记文档/头脑风暴/回忆录技术探索/13-观测评测与运行治理.md`
- `情侣日记文档/头脑风暴/回忆录技术探索/14-AgentRuntime运行机制.md`
- `情侣日记文档/头脑风暴/回忆录技术探索/15-业务Agent接入规范.md`
- `情侣日记文档/头脑风暴/AgentRuntime/需求设计文档.md`

## 1. 成功标准

- `/api/v1/runtime/health/live`、`/api/v1/runtime/health/ready` 和鉴权的 `/api/v1/runtime/capabilities` 可用，draining 与依赖故障语义符合契约。
- `POST /api/v1/runtime/agent-runs(start_mode=held)` 快速返回 `run_id/contract_version/package_digest/authorization_version`，显式 `/start` 后才允许执行。
- create/start/retry/cancel/human-approval/purge 校验 caller/tenant、时间戳、HMAC、`X-Agent-Key-Id`、target/connector allowlist 和 `Idempotency-Key`。
- AgentRun 与 steps 查询校验服务身份、签名和 caller 可见性，但不要求 `Idempotency-Key`。
- Runtime Contract 为 API/Event/Tool/Artifact 导出版本化 JSON Schema，并有 major/minor 兼容性测试。
- AgentPackage 构建不可变 digest；Registry 支持 `active/deprecated/revoked` 及安全撤销传播。
- auto create 或 start 在本地事务写 `run_dispatch` outbox；dispatcher 可用 Arq/Redis 唤醒 Worker。
- Worker 使用数据库条件写原子认领 run，取得 lease、heartbeat、fencing token 和 execution attempt；旧 worker 迟到写入失败。
- Runtime 为 Workflow Agent 生成静态 `AgentPlan`，执行 `memoir_agent` LangGraph 图，并记录 Step/ToolCall/ModelUsage 的 execution attempt。
- ToolGateway 只通过预注册 connector 调用 `memory.get_snapshot` 和 `memory.publish_playback_document`；后者原子发布完整 document/scenes/actions/`media_manifest`，媒体能力关闭时仍提交必填空清单。
- side effect 幂等键按逻辑操作稳定，网络重试、checkpoint resume 和 worker 接管不会产生第二次业务写入。
- ModelGateway 记录 policy、route config、provider、model、token、成本和 parse/safety 状态，并实施 provider 共享限流与 fallback。
- ContextManager 隔离 trusted instructions/untrusted content；schema 后的语义校验覆盖引用、ID、数量、时长、统计和工具参数。
- Checkpoint 使用加密 payload、TTL、data classification 和 privacy version 条件写；Artifact 默认只保存摘要、digest 和业务引用。
- PolicyEngine 分别限制活跃执行、held、queued、waiting_human 和 wall clock；AdmissionController 对系统及 provider 过载返回 429 + Retry-After。
- run/step 状态变化与 CallbackEvent、callback outbox 同事务提交；dead letter 重放复用原事件身份。
- run_dispatch dead letter 重放失败后 run 进入 `failed(DISPATCH_FAILED)`，不会永久 pending/queued。
- privacy purge 先写 tombstone/version，迟到私密结果不能落库，purge 后禁止 retry/resume。
- Runtime 在 execution attempt、模型、工具和 callback 前复核当前 authorization version，撤销后停止动作。
- `public_trace` 不含 prompt、模型原始输出、工具原始输入输出和亲密关系原文。
- 第一版媒体能力关闭时媒体节点为 `skipped(capability_disabled)`；不因未启用 TTS 进入 partial。
- 契约、并发恢复、删除竞态、提示注入、授权撤销、dead letter、无素材和模型降级场景有测试覆盖。
- 执行 `ruff check .`、`mypy app`、`pytest` 通过。

## 2. 推荐工程结构

新建目录：

```text

  pyproject.toml
  README.md
  .env.example
  alembic.ini
  alembic/
    env.py
    versions/
  app/
    main.py
    worker.py
    dispatcher.py
    reconciler.py
    api/
      __init__.py
      router.py
      endpoints/
        health_api.py
        capabilities_api.py
        agent_runs_api.py
    contracts/
      api.py
      events.py
      tools.py
      artifacts.py
      errors.py
      schema_export.py
    core/
      config.py
      logging.py
      security.py
      authorization.py
      admission.py
      connectors.py
      provider_security.py
      model_policy.yaml
    db/
      session.py
      base.py
    models/
      agent_definition.py
      agent_run.py
      admission_bucket.py
      agent_plan.py
      agent_step.py
      agent_tool_call.py
      agent_evaluation.py
      agent_checkpoint.py
      agent_artifact.py
      agent_model_usage.py
      callback_event.py
      runtime_outbox_event.py
      idempotency_record.py
    schemas/
      agent_run.py
      agent_package.py
      tool.py
      callback.py
      public_trace.py
      plan.py
      execution.py
      model.py
      context.py
      evaluation.py
      reconciliation.py
      audit.py
    services/
      agent_run_service.py
      run_queue_service.py
      callback_service.py
      outbox_service.py
      lease_service.py
      privacy_service.py
      authorization_service.py
      admission_service.py
      model_usage_service.py
      audit_service.py
      agent_package_service.py
      public_trace_service.py
      reconciliation_service.py
    runtime/
      interfaces.py
      planner.py
      executor.py
      graph_builder.py
      state.py
      checkpoint.py
      evaluator.py
      policy.py
      guardrails.py
      artifact_store.py
      context_manager.py
      model_gateway.py
      provider_traffic.py
      prompt_registry.py
      json_repair.py
      langchain_components.py
      tool_gateway.py
      reconciliation.py
      semantic_validation.py
      memoir_nodes.py
      tools/
        http_business_tool.py
        native_tools.py
        langchain_adapter.py
    agents/
      memoir_agent/
        agent.yaml
        input.schema.json
        output.schema.json
        workflow.graph.py
        tools.manifest.json
        guardrails.yaml
        callbacks.yaml
        ui-trace.yaml
        prompts/
          highlight-extract.v1.md
          chapter-plan.v1.md
          scene-generate.v1.md
          safety-review.v1.md
          action-generate.v1.md
        evals/
          minimal.jsonl
  tests/
    conftest.py
    test_health_api.py
    test_contract_compatibility.py
    test_runtime_capabilities.py
    test_models_metadata.py
    test_agent_run_api.py
    test_run_start_handshake.py
    test_worker_lease_fencing.py
    test_outbox_delivery.py
    test_privacy_purge.py
    test_runtime_security.py
    test_authorization_revocation.py
    test_untrusted_content.py
    test_model_usage_service.py
    test_admission_controller.py
    test_run_queue_service.py
    test_agent_package_loader.py
    test_planner.py
    test_workflow_executor.py
    test_tool_gateway.py
    test_tool_adapters.py
    test_reconciliation_service.py
    test_model_gateway.py
    test_provider_traffic_controller.py
    test_context_manager.py
    test_policy_engine.py
    test_evaluator_guardrails.py
    test_checkpoint_resume.py
    test_artifact_store.py
    test_callback_service.py
    test_memoir_agent_nodes.py
    test_memoir_agent_e2e.py
    fixtures/
      memoir_snapshots/
```

## 3. API 契约

### 3.1 Health

| 接口 | 方法 | 责任 |
|---|---|---|
| `/api/v1/runtime/health/live` | GET | 进程与事件循环存活检查 |
| `/api/v1/runtime/health/ready` | GET | 数据库 schema、Registry、outbox/queue、签名配置和 draining 检查 |
| `/api/v1/runtime/capabilities` | GET | 鉴权返回 Contract、Agent、逻辑 policy 和可选能力 |

### 3.2 AgentRun API

| 接口 | 方法 | 责任 |
|---|---|---|
| `/api/v1/runtime/agent-runs` | POST | 创建 AgentRun |
| `/api/v1/runtime/agent-runs/{run_id}/start` | POST | 幂等执行 held -> queued |
| `/api/v1/runtime/agent-runs/{run_id}` | GET | 查询 Run 摘要 |
| `/api/v1/runtime/agent-runs/{run_id}/steps` | GET | 查询步骤摘要 |
| `/api/v1/runtime/agent-runs/{run_id}/retry` | POST | 从失败节点重试 |
| `/api/v1/runtime/agent-runs/{run_id}/cancel` | POST | 取消 Run |
| `/api/v1/runtime/agent-runs/{run_id}/human-approval` | POST | 最小 approve/reject 状态迁移，无复杂审核台 |
| `/api/v1/runtime/agent-runs/{run_id}/purge-private-data` | POST | 写 privacy tombstone/version 并请求清理 |

### 3.3 创建 AgentRun 请求

```json
{
  "agent_id": "memoir_agent",
  "agent_version": "1.0.0",
  "business_type": "couple_memory",
  "business_id": "archive_123",
  "start_mode": "held",
  "input": {
    "archive_id": "archive_123",
    "snapshot_id": "snapshot_456",
    "generation_epoch": 1,
    "locale": "zh-CN"
  },
  "callback_target_id": "memory_callback",
  "business_connector_id": "couple_diary_backend",
  "data_domain": "couple_memory"
}
```

### 3.4 创建 AgentRun 响应

```json
{
  "run_id": "run_abc",
  "status": "pending",
  "dispatch_state": "held",
  "contract_version": "1.0.0",
  "package_digest": "sha256:...",
  "authorization_version": 12
}
```

### 3.5 状态枚举

```python
class AgentRunStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    EVALUATING = "evaluating"
    WAITING_HUMAN = "waiting_human"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

### 3.6 Public Trace 等级

```python
class PublicTraceMode(StrEnum):
    NONE = "none"
    STATUS_ONLY = "status_only"
    PUBLIC_SUMMARY = "public_summary"
    DEBUG_STAFF = "debug_staff"
    FULL_INTERNAL = "full_internal"
```

## 4. 数据模型

### `agent_definitions`

```text
id bigint pk
agent_id varchar(80) not null
version varchar(40) not null
runtime_type varchar(24) not null
definition_json json not null
package_digest varchar(80) not null
contract_version varchar(40) not null
status varchar(24) not null
status_changed_at datetime not null
status_changed_by varchar(120) not null
status_change_reason varchar(500) not null
revoked_at datetime nullable
revocation_reason varchar(500) nullable
created_at datetime not null
updated_at datetime not null
unique(agent_id, version)
```

### `agent_runs`

```text
id bigint pk
run_id varchar(80) unique not null
agent_id varchar(80) not null index
agent_version varchar(40) not null
package_digest varchar(80) not null
contract_version varchar(40) not null
business_type varchar(80) not null index
business_id varchar(120) not null index
status varchar(32) not null index
dispatch_state varchar(24) not null index
input_json json not null
capability_snapshot_json json nullable
authorization_version bigint not null
output_summary_json json nullable
error_code varchar(80) nullable
error_message varchar(500) nullable
caller_id varchar(120) not null
tenant_id varchar(120) not null
create_idempotency_key varchar(200) not null index
callback_target_id varchar(120) not null
business_connector_id varchar(120) not null
trace_id varchar(120) not null index
manual_retry_count int not null default 0
auto_retry_count int not null default 0
status_version int not null default 1
last_event_seq int not null default 0
execution_attempt int not null default 0
lease_owner varchar(120) nullable
lease_expires_at datetime nullable index
fencing_token bigint not null default 0
cancel_requested_at datetime nullable
privacy_state varchar(24) not null default 'active'
privacy_version bigint not null default 1
privacy_purge_requested_at datetime nullable
private_data_purged_at datetime nullable
held_expires_at datetime nullable index
queued_at datetime nullable
claimed_at datetime nullable
active_elapsed_ms bigint not null default 0
run_deadline_at datetime not null index
waiting_expires_at datetime nullable
started_at datetime nullable
finished_at datetime nullable
created_at datetime not null
updated_at datetime not null
```

### `admission_buckets`

```text
id bigint pk
scope_type varchar(32) not null  # global / caller / tenant / agent
scope_key varchar(160) not null
held_count int not null default 0
queued_count int not null default 0
running_count int not null default 0
version bigint not null default 1
created_at datetime not null
updated_at datetime not null
unique(scope_type, scope_key)
check(held_count >= 0 and queued_count >= 0 and running_count >= 0)
```

`AgentRun.dispatch_state` 是账本重建来源，`AdmissionBucket` 是事务内配额判定来源。global scope 固定使用 `scope_key='*'`，其他 key 从认证上下文取规范化 ID。任何容量迁移都锁定 AgentRun/幂等记录，幂等 upsert 缺失 bucket 后按 `(scope_type, scope_key)` 固定顺序锁定 global、caller、tenant、agent bucket，防止并发超卖与交叉 scope 死锁。

### `agent_plans`

```text
id bigint pk
plan_id varchar(80) unique not null
run_id varchar(80) not null index
strategy varchar(24) not null
steps_json json not null
dependencies_json json nullable
stop_conditions_json json not null
fallback_policy_json json not null
status varchar(24) not null
created_at datetime not null
updated_at datetime not null
```

### `agent_steps`

```text
id bigint pk
step_id varchar(80) unique not null
run_id varchar(80) not null index
step_name varchar(120) not null
step_type varchar(32) not null
status varchar(32) not null
execution_attempt int not null
step_attempt int not null default 1
input_summary json nullable
output_summary json nullable
error_code varchar(80) nullable
error_message varchar(500) nullable
started_at datetime nullable
finished_at datetime nullable
created_at datetime not null
updated_at datetime not null
```

### `agent_tool_calls`

```text
id bigint pk
tool_call_id varchar(80) unique not null
run_id varchar(80) not null index
step_id varchar(80) not null index
tool_name varchar(120) not null
tool_version varchar(40) nullable
transport varchar(32) not null
side_effect boolean not null default false
idempotency_key varchar(200) nullable
logical_operation_key varchar(200) nullable
request_digest varchar(128) nullable
execution_attempt int not null
tool_attempt int not null default 1
input_summary json nullable
output_summary json nullable
status varchar(32) not null
duration_ms int nullable
error_code varchar(80) nullable
error_message varchar(500) nullable
created_at datetime not null
```

### `agent_evaluations`

```text
id bigint pk
evaluation_id varchar(80) unique not null
run_id varchar(80) not null index
step_id varchar(80) nullable index
target_type varchar(60) not null
target_id varchar(120) nullable
evaluator_type varchar(60) not null
score_json json nullable
decision varchar(32) not null
reason_summary varchar(500) nullable
created_at datetime not null
```

### `agent_checkpoints`

```text
id bigint pk
checkpoint_id varchar(80) unique not null
run_id varchar(80) not null index
checkpoint_key varchar(160) not null
state_schema_version varchar(40) not null
data_classification varchar(32) not null
privacy_version bigint not null
encrypted_state_blob blob nullable
storage_ref varchar(500) nullable
state_summary json nullable
content_digest varchar(128) not null
expires_at datetime not null index
created_at datetime not null
unique(run_id, checkpoint_key)
```

### `agent_artifacts`

```text
id bigint pk
artifact_id varchar(80) unique not null
run_id varchar(80) not null index
step_id varchar(80) nullable index
artifact_type varchar(80) not null
artifact_schema_version varchar(40) not null
data_classification varchar(32) not null
privacy_version bigint not null
summary_json json nullable
content_digest varchar(128) not null
payload_ref varchar(500) nullable
retention_until datetime nullable
created_at datetime not null
```

### `agent_model_usages`

```text
id bigint pk
usage_id varchar(80) unique not null
run_id varchar(80) not null index
step_id varchar(80) not null index
execution_attempt int not null
model_attempt int not null
status varchar(32) not null  # running / aborted_before_send / succeeded / failed / outcome_unknown
model_policy varchar(80) not null
route_config_version varchar(80) not null
pricing_config_version varchar(80) not null
cost_unit varchar(32) not null
provider varchar(80) not null
model varchar(120) not null
capability_snapshot json not null
thinking_summary json nullable
prompt_id varchar(120) nullable
prompt_version varchar(40) nullable
permit_id varchar(80) not null index
reserved_tokens int not null
reserved_estimated_cost numeric(12, 6) not null
prompt_tokens int nullable
completion_tokens int nullable
total_tokens int nullable
estimated_cost numeric(12, 6) nullable
provider_request_id varchar(160) nullable
error_code varchar(80) nullable
latency_ms int nullable
parse_status varchar(32) nullable
safety_status varchar(32) nullable
request_deadline_at datetime not null index
started_at datetime not null
finished_at datetime nullable
usage_observed_at datetime nullable
created_at datetime not null
```

每行记录一次候选物理模型 attempt。`aborted_before_send` 确认未请求 provider，不计调用次数和成本；`running/outcome_unknown` 用 `reserved_estimated_cost` 参与预算，观察到 provider usage 后用同一行的 `estimated_cost` 替换预留成本。token 空值表示未知，不能用 0 冒充已观察到的零消耗。模型表不得保存 prompt、模型原文或业务正文。

### `callback_events`

```text
id bigint pk
event_id varchar(80) unique not null
run_id varchar(80) not null index
event_seq int not null
status_version int not null
event_type varchar(60) not null
payload_json json not null
created_at datetime not null
unique(run_id, event_seq)
```

`callback_events` 是不可变事件事实。delivery state、attempt、next retry、lease 和 dead letter 只写 `runtime_outbox_events`，dispatcher 不更新 CallbackEvent。

### `runtime_outbox_events`

```text
id bigint pk
event_id varchar(80) unique not null
event_type varchar(32) not null index  # run_dispatch / callback
run_id varchar(80) not null index
event_seq int nullable
status_version int nullable
target_id varchar(120) not null
payload_json json nullable
payload_ref varchar(500) nullable
delivery_state varchar(32) not null index  # pending/delivering/delivered/dead_letter
attempt_count int not null default 0
next_attempt_at datetime nullable index
lease_owner varchar(120) nullable
lease_expires_at datetime nullable index
last_error_code varchar(80) nullable
delivered_at datetime nullable
retention_until datetime not null
created_at datetime not null
```

### `idempotency_records`

```text
id bigint pk
client_id varchar(120) not null index
idempotency_key varchar(200) not null
scope varchar(80) not null  # create / start / retry / cancel / human_approval / purge
request_hash varchar(128) not null
response_json json nullable
resource_type varchar(80) nullable
resource_id varchar(120) nullable
expires_at datetime not null index
created_at datetime not null
updated_at datetime not null
unique(client_id, idempotency_key, scope)
```

## 5. 任务拆分

### Task 0: Runtime Contract 与兼容性基线

**Files:**
- Create: `app/contracts/api.py`
- Create: `app/contracts/events.py`
- Create: `app/contracts/tools.py`
- Create: `app/contracts/artifacts.py`
- Create: `app/contracts/errors.py`
- Create: `app/contracts/schema_export.py`
- Test: `tests/test_contract_compatibility.py`

**Interfaces:**
- Produces: `contract_version=1.0.0` 的 API/Event/Tool/Artifact JSON Schema；后续任务只依赖该契约包。

- [✅] 写 create/start/query/retry/cancel/human-approval/purge、RuntimeEvent、ToolRequest/Result/Error、ArtifactEnvelope 的 Pydantic 模型。
- [ ] 将写命令模型接入真实 HTTP 路由而非仅导出：start/retry 解析可选 `expected_status_version`，cancel/purge 解析必填稳定 `reason_code`，human approval 保持必填 `decision + expected_status_version`；同步 OpenAPI、稳定 JSON Schema fixture 与跨仓 consumer fixture。
- [✅] Tool Contract 预留 `mcp_server_id/mcp_tool_name/mcp_resource_uri` 和 AI SDK 等价 tool schema fixture；第一版只做序列化/兼容性测试，不建立 MCP 连接或 ToolLoopAgent。
- [✅] 固定 RuntimeEvent 枚举 `run_started/step_started/model_call_started/model_call_finished/tool_call_started/tool_call_finished/step_failed/fallback_used/human_review_requested/partial_succeeded/run_succeeded/run_failed/run_cancelled`，并定义到安全 callback `run_started/step_changed/waiting_human/partial_succeeded/run_succeeded/run_failed/run_cancelled` 的确定性映射；`waiting_human` 仅在 AgentPackage 显式启用时发送。
- [✅] 固定错误码或治理分类 `IDEMPOTENCY_CONFLICT/RUNTIME_OVERLOADED/MODEL_TIMEOUT/MODEL_PARSE_FAILED/TOOL_TIMEOUT/TOOL_PERMISSION_DENIED/BUSINESS_DATA_INVALID/SAFETY_BLOCKED/COST_LIMIT_EXCEEDED/CALLBACK_SIGNATURE_INVALID/CALLBACK_OUT_OF_ORDER/RECONCILIATION_NEEDED/INDIRECT_PROMPT_INJECTION/GENERATION_SUPERSEDED/DISPATCH_FAILED/PACKAGE_REVOKED/PRIVATE_DATA_PURGED/AUTHORIZATION_REVOKED/SEMANTIC_VALIDATION_FAILED`。
- [✅] 导出排序稳定的 JSON Schema，并保存兼容性 fixture。
- [✅] 测试新增 optional 字段保持 minor 兼容，删除 required 字段或改变枚举必须提升 major。
- [✅] 运行 `pytest tests/test_contract_compatibility.py -q`。

**Checkpoint:** Runtime、业务 Adapter 和 AgentPackage 有一套无业务依赖的冻结契约。

### Task 1: Python 工程骨架、配置、健康检查

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `.env.example`
- Create: `app/main.py`
- Create: `app/api/router.py`
- Create: `app/api/endpoints/health_api.py`
- Create: `app/api/endpoints/capabilities_api.py`
- Create: `app/core/config.py`
- Create: `app/core/logging.py`
- Create: `app/schemas/audit.py`
- Create: `app/services/audit_service.py`
- Create: `tests/test_health_api.py`
- Create: `tests/test_runtime_capabilities.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Produces: FastAPI app、`GET /api/v1/runtime/health/live`、`GET /api/v1/runtime/health/ready`、`GET /api/v1/runtime/capabilities`。

- [✅] 建立 `pyproject.toml`，依赖包含 FastAPI、uvicorn、pydantic-settings、SQLAlchemy、Alembic、httpx、pytest、ruff、mypy。
- [✅] 定义 `Settings`，包含 `DATABASE_URL`、`REDIS_URL`、`RUNTIME_ID`、`AGENT_PACKAGE_ROOT`、`CALLBACK_ALLOWED_HOSTS`、`TRUSTED_CLIENTS_JSON`、`SIGNATURE_TOLERANCE_SECONDS`、`RUN_QUEUE_NAME`、`MODEL_TRAFFIC_NAMESPACE`、`MODEL_PERMIT_TTL_SECONDS`、`MAX_STEPS`、`MAX_MODEL_CALLS`、`MAX_TOOL_CALLS`、`MAX_RUN_SECONDS`、`MAX_AUTO_RETRY_PER_STEP`、`MAX_MANUAL_RUN_RETRY_COUNT`、`MAX_ESTIMATED_COST`、`HELD_TTL_SECONDS`、`QUEUE_TTL_SECONDS`、`APPROVAL_TTL_SECONDS`、`MAX_WALL_CLOCK_SECONDS`、`LEASE_TIMEOUT_SECONDS`、`CALLBACK_MAX_ATTEMPTS`、`CALLBACK_RETRY_ALERT_THRESHOLD`、`OUTBOX_RETENTION_DAYS`、`IDEMPOTENCY_TTL_DAYS`、`RECONCILIATION_INTERVAL_SECONDS`、`RECONCILIATION_FAILURE_THRESHOLD`、`AUDIT_SINK_DSN`、`AUDIT_RETENTION_DAYS`、`AUDIT_ALLOWED_ROLES`。模型流量控制固定 fail closed，不提供切换为进程内无限调用的配置。
- [✅] 新增 `/api/v1/runtime/health/live`，只检查进程与事件循环。
- [✅] 新增 `/api/v1/runtime/health/ready`，检查数据库 schema、Registry、outbox/queue、部署声明启用的 outbox event type 均有 handler、签名配置；draining 或启用类型缺 handler 时返回 503。
- [✅] 新增鉴权 capabilities，返回 Contract、Agent 版本、逻辑 model policy 和能力开关，禁止返回密钥、真实 provider/connector endpoint 和租户配额。
- [✅] 定义 `RuntimeAuditEvent(audit_id, actor_type, actor_id, action, resource_type, resource_id, reason_code, outcome, occurred_at, trace_id, metadata_summary)` 和追加写 `AuditService`；生产环境未配置持久、访问受限的 audit sink 时 readiness 失败。
- [✅] 写测试覆盖 live/ready、依赖失败、draining 和 capabilities 脱敏。
- [✅] 运行 `ruff check .`、`pytest tests/test_health_api.py -q`。

### Task 2: 数据模型与 Alembic 迁移

**Files:**
- Create: `alembic.ini`
- Create: `app/db/base.py`
- Create: `app/db/session.py`
- Create: `app/models/*.py`
- Create: `alembic/env.py`
- Create: `alembic/versions/20260707_0001_initial_agent_runtime_tables.py`
- Test: `tests/test_models_metadata.py`

**Interfaces:**
- Produces: 所有 Runtime 核心表。

- [✅] 定义 SQLAlchemy Base 和 session factory。
- [✅] 创建 `AgentDefinition`、`AgentRun`、`AdmissionBucket`、`AgentPlan`、`AgentStep`、`AgentToolCall`、`AgentEvaluation`、`AgentCheckpoint`、`AgentArtifact`、`AgentModelUsage`、`CallbackEvent`、`RuntimeOutboxEvent`、`IdempotencyRecord` 模型。
- [✅] `AgentDefinition` 生命周期字段覆盖 `status_changed_at/by/reason`，revoked 另存 revoked_at/revocation_reason；迁移不得用空操作者或空原因回填生产变更。
- [✅] `AgentRun` 增加 package/contract、caller/tenant、dispatch、authorization、execution attempt、lease/fencing、cancel、privacy、独立时钟字段和约束；`create_idempotency_key` 仅建普通索引，不建立绕过 TTL 的永久唯一约束。
- [✅] `AgentStep/AgentToolCall/AgentModelUsage` 增加 execution attempt；工具另存 tool attempt、稳定 logical operation key 和最终签名 body 的 request digest；ModelUsage 另存 model attempt、running/aborted_before_send/terminal/outcome_unknown 状态、permit、预留与实际 usage，并保证一个 `usage_id` 只按允许的状态转换结算。
- [✅] Checkpoint 使用加密 blob/storage ref、TTL、classification、privacy version；Artifact 使用 envelope，不默认保存业务正文。
- [✅] `CallbackEvent` 增加 `event_seq/status_version`，并建立 `unique(run_id, event_seq)`；两者与状态变化及 callback outbox 同事务固化。
- [✅] `IdempotencyRecord` 保存 request hash、缓存响应、资源 ID 和 TTL；Runtime 写接口按 `SHA256(upper(method) + "\n" + normalized_path + "\n" + body_sha256)` 生成 request hash，normalized path 包含 `run_id` 等资源标识，避免空 body 的资源操作复用同一 key 时误命中。
- [✅] `AdmissionBucket` 建立 scope 唯一约束、非负 check 和 version，global key 固定为 `*`；初始迁移不从不完整历史状态静默回填生产配额。
- [✅] 建立状态转换和 `status_version/fencing_token/privacy_version` 条件写所需索引。
- [✅] 创建 Alembic 初始迁移。
- [✅] 测试所有模型表名和关键唯一约束存在。
- [✅] 运行 `pytest tests/test_models_metadata.py -q`。

### Task 3: AgentPackage 文件化加载器

**Files:**
- Create: `app/schemas/agent_package.py`
- Create: `app/services/agent_package_service.py`
- Create: `app/agents/memoir_agent/*`
- Test: `tests/test_agent_package_loader.py`

**Interfaces:**
- Produces: `AgentPackageService.load(agent_id: str, version: str) -> AgentPackage`。

- [✅] 定义 `AgentPackage`、`WorkflowNodeDefinition`、`ToolManifest`、`CallbackConfig`、`UiTraceConfig` schema。
- [✅] 冻结 `policy.waiting_human_timeout_action=fallback|failed|cancelled`；只有启用 `waiting_human` callback 的 package 才允许 workflow 进入人工等待，选择 fallback 时必须存在确定性的恢复节点。
- [✅] `ToolManifest` 支持可选 `mcp_server_id/mcp_tool_name/mcp_resource_uri`，HTTP Business Tool 未使用这些字段时保持为空且不影响 digest/兼容性。
- [✅] 校验 HTTP Tool 的 `connector_id/method/relative path/input_from/output_to`；完整 URL、未声明 state 路径或写入 trusted 控制字段的映射拒绝注册。
- [✅] 加载 `agent.yaml`、JSON Schema、受信任 `workflow.graph.py`、版本化 `prompts/`、`tools.manifest.json`、`guardrails.yaml`、`callbacks.yaml`、`ui-trace.yaml` 和 `evals/` 元数据。
- [✅] 校验 `agent_id + version` 必须精确匹配，不自动使用最新版。
- [✅] 构建器按排序文件路径和内容计算 `package_digest`，排除签名文件、构建时间和 digest 自身等生成元数据；同一 `agent_id + version` digest 变化时拒绝注册，不允许 upsert 覆盖。
- [✅] `AgentPackageService.load()` 固定已注册 digest，校验 `contract_version` 和 `active/deprecated/revoked` 状态。
- [✅] active/deprecated/revoked 变化记录 `status_changed_at/by/reason` 并写 RuntimeAuditEvent；revoked 额外保存 revoked_at/revocation_reason。
- [✅] 只加载 CI 构建并由管理员注册的 package；普通业务调用方不能上传或指定 Python 文件。
- [✅] 创建 `memoir_agent@1.0.0` AgentPackage 文件。**（2026-08-11 第六次最小收口 P1：1.0.0 恢复 `enqueue_media_tasks` 缺 `safe_to_rerun` 的冻结原貌——620f44a 曾给该节点补 `safe_to_rerun=False` 违反同版本 digest 不可变铁律；另发 `memoir_agent@1.0.1` 承载显式 `safe_to_rerun=False`。`test_memoir_agent_1_0_0_and_1_0_1_are_independent_immutable_packages` 证两版本 digest 独立 + 各自可 load + `contract_version` 都 1.0.0；新 Run 路由 1.0.1，旧 Run resume 读 `steps_json` 经 `StaticWorkflowGraph.build()` 不经 Planner，1.0.0 缺键对其透明）**
- [✅] 测试缺文件、prompt 引用/版本不存在、版本不匹配、workflow 空节点、工具清单缺失、非法 `waiting_human_timeout_action`、人工等待未启用 callback、fallback 未声明恢复节点或最小 eval 用例少于 5 条时报结构化错误。
- [✅] 测试同 digest 重复加载幂等、不同 digest 冲突、deprecated 拒绝新 create、revoked 阻止 create/start/retry/resume。
- [✅] 测试签名/构建时间等排除元数据变化不改变 digest，任一受管 package 文件变化都会改变 digest。

### Task 4: AgentRun 生命周期 API 与幂等服务

**Files:**
- Create: `app/schemas/agent_run.py`
- Create: `app/api/endpoints/agent_runs_api.py`
- Create: `app/services/agent_run_service.py`
- Create: `app/services/privacy_service.py`
- Create: `app/core/security.py`
- Create: `app/core/authorization.py`
- Create: `app/core/connectors.py`
- Create: `app/core/admission.py`
- Create: `app/services/authorization_service.py`
- Create: `app/services/admission_service.py`
- Create: `app/services/outbox_service.py`
- Test: `tests/test_agent_run_api.py`
- Test: `tests/test_run_start_handshake.py`
- Test: `tests/test_privacy_purge.py`
- Test: `tests/test_runtime_security.py`
- Test: `tests/test_admission_controller.py`

**Interfaces:**
- Produces: `POST /api/v1/runtime/agent-runs`、`POST /start`、`GET /api/v1/runtime/agent-runs/{run_id}`、`GET /api/v1/runtime/agent-runs/{run_id}/steps`、`POST /retry`、`POST /cancel`、`POST /human-approval`、`POST /purge-private-data`。
- Produces: `AuthorizationService.authorize_run_action(...)`、`AdmissionService.reserve_transition(run_or_identity, from_state, to_state, limits)`、`OutboxService.append_run_dispatch(...)` 和 `OutboxService.append_callback(...)`，后续任务复用而不重复实现。

- [✅] 定义创建、start、查询、重试、取消、human approval、purge 的 Pydantic schema，并复用 Contract Layer。
- [✅] 所有写接口校验 `X-Agent-Client-Id`、`X-Agent-Key-Id`、`X-Agent-Timestamp`、`X-Agent-Signature`、`Idempotency-Key`。
- [✅] AgentRun 与 steps 查询接口校验四个服务签名头和调用方可见性，合法读请求不要求 `Idempotency-Key`；普通调用方只能访问自己创建的 run，内部审计身份除外。
- [✅] 创建 run 时加载 AgentPackage 并校验 input schema。
- [✅] 校验 `business_type` 属于 AgentPackage 允许范围。
- [✅] 从凭据推导 caller/tenant，校验 Agent、business_type、callback target、business connector、数据域和 Admission 配额。
- [✅] create held/auto、start、retry、human approval/fallback 恢复在同一数据库事务中执行配额预留、状态条件更新和 run_dispatch outbox；429 保持原状态且不完成 IdempotencyRecord，cancel/purge 不受 Admission 阻塞。
- [✅] `AdmissionService` 以 `dispatch_state` 映射占用：held/queued/claimed 分别对应 held/queued/running，finished 不占用；从认证身份生成规范 scope key，以方言安全的幂等 upsert 确保 bucket 存在，再按固定 scope 顺序锁定，先校验目标上限后原子增减。幂等命中、条件写失败和事务回滚都不得留下计数变化。
- [✅] 固定容量迁移矩阵：create `none -> held|queued`、start `held -> queued`、retry/approval/fallback `none -> queued`、直接 cancel/终止 `held|queued -> none`；claimed run 的释放由 Worker 安全终止事务执行，API 的 cancel/purge 请求本身不等待也不受 Admission 拒绝。
- [✅] 创建 held run，设置 `held_expires_at`、package digest、contract、authorization version 和 capability snapshot；相同 key 与 request hash 的重复创建重放首次创建响应，当前 run 状态由 AgentRun 查询接口返回。
- [✅] `/start` 条件更新 held -> queued，并在同一事务完成 Admission `held -> queued`、写 `queued_at`、run_dispatch outbox 和幂等响应；相同 key/hash 重放首次响应，使用新 key 请求已 queued/claimed 的 run 时返回当前安全摘要且不重复迁移配额或写 outbox。held 超时、已取消/结束、package revoked、privacy 非 active 或授权失效时拒绝。
- [ ] 路由按冻结合同解析全部写命令 body：start/retry 的 `expected_status_version` 提供时必须参与条件写，版本不匹配不改变状态/计数/outbox；cancel/purge 的 `reason_code` 必填并进入无自由文本的 RuntimeAuditEvent。补齐缺 body、非法 body、陈旧版本、原因码透传和幂等 request-hash 测试。
- [✅] 相同 `Idempotency-Key` 的 method、normalized path 或 body hash 任一项不一致时返回 HTTP `409 Conflict`，错误码 `IDEMPOTENCY_CONFLICT`；不同 `run_id` 不能因空 body 相同而复用旧结果。
- [✅] auto create 才在创建事务写 run_dispatch outbox；任何 HTTP 请求都不执行 workflow。
- [✅] 查询接口返回当前 `status/dispatch_state/status_version/last_event_seq/execution_attempt/privacy_state/privacy_version/privacy_purge_requested_at/private_data_purged_at/updated_at`、progress、current_step、public_trace 和安全错误摘要，不返回 lease/fencing、私密 payload 或内部错误堆栈。
- [ ] 将 `app/contracts/api.py::AgentRunQuery` 与 HTTP `RunDetail/StepSummary` 收敛为同一导出 schema：`progress` 限定 0..100，`current_step` 为安全 Step 对象或 null，顶层与 Step 只暴露稳定 `error_code`，移除跨项目自由 `error_message`；更新 endpoint、JSON fixture、provider/consumer tests，并确认 callback 仍不携带 progress/current_step。
- [✅] 取消与 retry/approval 互斥：`pending + held/queued` 或 `waiting_human + finished` 在 API 事务内直接写 `cancel_requested_at/status=cancelled/dispatch_state=finished`、递增 `status_version` 并创建 callback outbox；claimed run 只写取消请求，由有效 fencing worker 在安全边界终止，迟到 dispatch 认领失败。
- [✅] 重试接口只允许原 caller 或内部审计身份调用，且只允许 `failed/partial`、存在 checkpoint、`manual_retry_count < 3` 的 run 重新入队。
- [✅] `partial` retry 只执行 Runtime 未完成的可选步骤，复用已发布 revision，不重新执行主作品发布；业务媒体 worker 失败不触发 AgentRun retry。（依赖 Task 6/10 写入步骤状态、可选节点与 checkpoint 语义。）
- [✅] retry 创建新 execution attempt，复用逻辑副作用键；package revoked 或 privacy 非 active 时拒绝。
- [✅] human approval 只接受 `status=waiting_human AND dispatch_state=finished` 的 run，携带 `decision=approve|reject`、当前 `expected_status_version` 和独立幂等键；approve 只条件更新 `dispatch_state: finished -> queued`、递增 `status_version` 并同事务写 dispatch outbox，run 状态保持 `waiting_human`，由重新取得 lease 的 worker 迁移为 `running`；reject 按 package 策略重新入队执行 fallback，或明确终结为 `failed/cancelled`。
- [✅] purge API 在同一事务写 tombstone/version 并请求取消，提交后立即返回 `202 + privacy_state=purge_requested + privacy_version`，不等待物理清理；清理 worker 完成后才由 AgentRun 查询返回 `privacy_state=purged`。相同 key 与 request hash 的重复 POST 重放首次接受响应，不改写 `IdempotencyRecord.response_json`，也不创建第二个清理任务；当前状态只通过 AgentRun 查询。清理失败保持写屏障，由 reconciler 重试并告警，purge 后禁止 retry/resume。
- [✅] IdempotencyService 以 `client_id + scope + key` 锁定记录：未过期时复用/冲突，过期时原子换代；purge 记录在清理完成前不得过期或复用。
- [✅] 事务接受前的 429、连接失败和可重试 5xx 不完成 IdempotencyRecord。
- [✅] Runtime 自动节点重试使用 step attempt / `auto_retry_count`，与手动 run 级重试计数隔离。
- [✅] approval/cancel/retry/purge 接受事务成功后写 RuntimeAuditEvent，actor 从服务凭据推导，metadata 只含版本、scope、结果和标准错误码。
- [✅] 测试 held/start 握手、create/start 幂等重放、queued/claimed 不重复 Admission/outbox、幂等冲突/过期换代、held 超时与 429、签名读写边界、callback/purge 查询对账、key 轮换、target/connector 越权、stale approval、retry/cancel 竞态及 purge 写屏障/终态重放。

### Task 4.5: Runtime 任务队列与 Worker

**Files:**
- Create: `app/worker.py`
- Create: `app/dispatcher.py`
- Create: `app/services/run_queue_service.py`
- Modify: `app/services/outbox_service.py`
- Create: `app/services/lease_service.py`
- Modify: `app/services/agent_run_service.py`
- Create: `app/runtime/interfaces.py`
- Create: `app/schemas/execution.py`
- Test: `tests/test_run_queue_service.py`
- Test: `tests/test_outbox_delivery.py`
- Test: `tests/test_worker_lease_fencing.py`

**Interfaces:**
- Produces: `OutboxService.append_run_dispatch(run_id, reason)`、`LeaseService.claim/heartbeat/release`。
- Produces: dispatcher 可选投递 Arq 通知，`python -m app.worker` 认领并执行 run。
- Produces: `OutboxDeliveryHandler.deliver(event) -> DeliveryResult` 与按 `event_type` 的显式处理器注册表；本任务只注册并启用 `run_dispatch`。
- Produces: `RunExecutor.run(run_id: str, lease_context: LeaseContext) -> AgentRunResult` 协议；`LeaseContext` 固定包含 `execution_attempt/lease_owner/fencing_token/lease_expires_at/privacy_version/authorization_version`，`AgentRunResult` 固定包含 `run_id/status/execution_attempt/output_summary/artifact_refs/error_code/checkpoint_id` 且不含私密 state。Task 4.5 使用 fake executor 验证调度，Task 6 提供正式实现。

- [✅] auto create、start、retry、人工确认或 fallback 恢复在状态事务内写 `RuntimeOutboxEvent(event_type=run_dispatch)`。
- [✅] dispatcher 使用独立 lease 认领 outbox，可投递 Arq；投递重复或 Redis 丢失不改变数据库中的 run 真相。
- [✅] dispatcher 的认领查询只选择已启用且已注册 handler 的 `event_type`；未启用/缺 handler 的事件保持 pending、不增加 attempt、不进入 dead letter。readiness 校验部署声明启用的类型都有 handler，run_dispatch/callback 分类型轮询、指标和告警，避免头阻塞。
- [✅] Worker 启动时加载配置、数据库、Redis、AgentPackage 根目录。
- [✅] Worker 收到 `run_id` 后使用数据库条件写认领 `dispatch_state=queued` 且 status 为 `pending` 或经审批/fallback 恢复的 `waiting_human` run，实现 queued -> claimed，递增 execution attempt 与 fencing token，并写 lease owner/expiry；cancelled run 和带取消请求的 run 不能被认领。
- [✅] Worker claim 在同一事务调用 AdmissionService 执行 `queued -> running`；进入 `waiting_human` 或任一终态时执行 `running -> none`，计数迁移与 AgentRun/status_version/callback outbox 同事务。
- [✅] Worker 只依赖注入的 RunExecutor 协议，不导入 MemoirAgent 或尚未实现的 WorkflowExecutor；调度测试使用 deterministic fake executor。
- [✅] heartbeat 续租失败后 Worker 停止模型、工具、Checkpoint、Artifact 和状态写入。
- [✅] reaper 回收失效 lease，在同一事务执行 claimed -> queued、Admission `running -> queued` 并创建新的 run_dispatch；旧 worker 迟到写入被 fencing 拒绝。
- [✅] 所有状态、Checkpoint、Artifact、ToolCall 写入复用 LeaseService 的 fencing、cancel、Package revoked、privacy、authorization 和 deadline 条件闸；模型/工具的物理发送前另行实时复核同一无内容结论。
- [✅] 实例进入 draining 后立即停止认领新 run，readiness 返回 503、liveness 保持成功；在执行器安全返回边界条件更新当前 lease 为到期，Admission running 占用保留到 reaper 在 `claimed -> queued` 事务中迁移，接管 Worker 创建新的 execution attempt。
- [✅] `SIGTERM/SIGINT` 仅切换 Worker draining；当前同步节点的网络窗口受 lease/deadline 限制，节点边界 heartbeat 后先持久化安全 Artifact/checkpoint，随后不启动模型/工具或后续节点写入，主动让 lease 到期并只由 reaper 创建一个新 fencing attempt。
- [✅] 测试重复 dispatch、Redis 丢失、双 worker 竞争、heartbeat/旧 fencing、cancel、Admission claim/reaper，以及 draining 安全边界、单次接管和优雅停机；真实 PostgreSQL/Redis harness 覆盖跨 Session/进程路径。
- [✅] 测试未启用/缺 handler 的 callback 保持 pending 且 attempt 不变、readiness 对错误启用配置返回 503，以及 callback 积压不阻塞 run_dispatch。

### Task 5: 静态 Planner 与 AgentPlan 落库

**Files:**
- Create: `app/runtime/planner.py`
- Create: `app/schemas/plan.py`
- Test: `tests/test_planner.py`

**Interfaces:**
- Produces: `StaticPlanner.create_plan(run, package) -> AgentPlanDTO`；DTO 固定包含 `plan_id/run_id/strategy/steps/stop_conditions/fallback_policy/status`。

- [✅] 从受信任 `workflow.graph.py` 导出的节点 manifest 生成 `steps_json`，不执行业务方上传代码。
- [✅] 写入 stop conditions：step/model/tool/cost、活跃执行预算、held/queue/approval TTL 和 wall clock deadline。
- [✅] 写入每个节点的 fallback policy。
- [✅] 落库 `AgentPlan`。
- [✅] 测试计划包含 `load_snapshot` 到 `publish_playback_document`，以及发布后的 optional `enqueue_media_tasks`；第一版能力关闭时节点为 skipped，未创建媒体任务或触网。

### Task 6: LangGraph Workflow Executor

**Files:**
- Create: `app/runtime/state.py`
- Create: `app/runtime/graph_builder.py`
- Create: `app/runtime/executor.py`
- Modify: `app/schemas/execution.py`
- Test: `tests/test_workflow_executor.py`

**Interfaces:**
- Produces: `WorkflowExecutor.run(run_id: str, lease_context: LeaseContext) -> AgentRunResult` 并实现 Task 4.5 的 RunExecutor 协议，复用既有 LeaseContext/AgentRunResult，不重新定义返回类型。

> 阶段完成基础：已实现可注入的 mock `WorkflowExecutor` 验证链路，按静态 AgentPlan 写 `AgentStep.running/succeeded` 与 checkpoint 安全摘要；每次节点写入前复用 LeaseService 拒绝失效 fencing context。**2026-08-06 复核更正：** `state_summary` 虽安全，但密文仍序列化完整 `AgentState`；下列原实现记录保留，内容最小化以 Task 10 新增未完成项为准。
- [✅] 定义 `AgentState`，包含 `run_input`、`snapshot`、`sanitized_material`、`stats`、`highlights`、`chapter_plan`、`scenes`、`actions`、`playback_document`、`publish_result`、`media_tasks`、`safety_report`、`trust_metadata`、`errors`、`fallback_flags`。
- [✅] 将已冻结 `AgentPlan.steps_json` 编译为受控 LangGraph `StateGraph`；第一版仅允许线性静态 DAG，图状态只保存已访问 node ID，拒绝分支、动态节点和动态边，真实节点仍由 Executor 复用 fencing/privacy/cancel 等条件闸执行。
- [✅] 每个节点执行前校验 fencing、cancel、privacy version、authorization version 与 Package revoked 状态；任一失效不启动新节点，撤销只写无内容审计结论。
- [✅] 每个节点开始前写 `AgentStep.running`。
- [✅] 每个节点结束后写 `AgentStep.succeeded/failed` 和安全摘要。
- [✅] 生成版本化 RuntimeEvent；第一版把详细事件落到 Step/ModelUsage/ToolCall/Evaluation 权威记录，业务 callback 仅接收确定性映射后的安全摘要。
- [✅] 节点失败时根据 planner fallback 进入 fallback 节点或标记 failed。
- [✅] 每次 worker 接管写新的 execution attempt；节点自身重试只递增 step attempt。
- [✅] 测试 deterministic mock workflow、非法状态转换、旧 fencing 写入、取消中止和 attempt 审计。

### Task 7: ToolGateway 与 HTTP Business Tool

**Files:**
- Create: `app/runtime/tool_gateway.py`
- Create: `app/runtime/tools/http_business_tool.py`
- Create: `app/runtime/tools/native_tools.py`
- Create: `app/runtime/tools/langchain_adapter.py`
- Modify: `app/core/security.py`
- Modify: `app/core/connectors.py`
- Modify: `app/services/authorization_service.py`
- Create: `app/schemas/tool.py`
- Test: `tests/test_tool_gateway.py`
- Test: `tests/test_tool_adapters.py`
- Test: `tests/test_authorization_revocation.py`

**Interfaces:**
- Produces: `ToolGateway.call(tool_name, run, step, state) -> ToolResult`。
- Consumes: Task 4 的 Connector Registry、AuthorizationService、签名验证和 AuditService；本任务只增加出站工具解析、签名和逐次授权能力。

- [✅] 根据 `tools.manifest.json` 校验工具在 allowlist 内。
- [✅] 实现固定注册的 Native Tool：JSON repair、键名摘要和敏感字段扫描；它们经 ToolGateway 进入冻结工具预算、无正文 `AgentToolCall` 审计和受控错误边界，且始终标记为 `side_effect=false`。
- [✅] 将 UnifiedToolDefinition 包装为 LangChain StructuredTool/BaseTool；adapter 只转换 schema/结果，实际执行必须回到 `ToolGateway.call()`，不得自行请求 connector。
- [✅] 由预注册 `connector_id + relative path` 解析 endpoint；禁止 AgentPackage 或模型提供完整 URL，并执行协议/host/port/DNS/IP allowlist、私网阻断和禁止重定向。
- [✅] 根据 `input_from` 从 trusted run/manifest/deterministic state 组装 identity、权限、generation token 和 side effect 参数；模型只提供 schema 允许的候选字段。
- [✅] 工具结果通过 output schema、敏感字段扫描和 `output_to` allowlist 后写入 AgentState；禁止覆盖 run identity、authorization、connector、generation/version token 或 operation key。
- [✅] 使用 connector 独立 secret 和 `X-Agent-Key-Id` 生成 HMAC；对原始 body bytes 计算 hash，验签使用恒定时间比较，只在密钥轮换窗口接受新旧 key。
- [✅] 仅从已落库的权威物理 `AgentToolCall.tool_attempt` 发送 `X-Agent-Tool-Attempt` 作为非权威审计字段；只读调用不得伪造该头，业务后端不得用它替代稳定幂等键、authorization version 或 generation epoch。
- [✅] side effect 工具生成 `{run_id}:{logical_step_key}:{tool_name}:{operation_key}`；attempt 只写审计 header。
- [✅] 使用 httpx 调业务工具接口，支持 timeout 和 retry。
- [✅] 每个物理调用使用独立 `tool_call_id/execution_attempt/tool_attempt`。side effect 请求先条件写 `AgentToolCall.running`、稳定 logical operation key、业务幂等键和规范签名 body 的 `request_digest`，提交成功后才允许发送 HTTP 请求；重试与接管复用相同 logical key、幂等键和 digest。
- [✅] 工具返回后只在 fencing、privacy 和 authorization 条件仍有效时把 ToolCall/AgentState 推进为 succeeded/failed；条件失效或结果不确定时保留 running 供对账用原幂等键查询，禁止用迟到结果推进 run。
- [✅] 结构化处理业务工具错误：非 2xx 严格解析冻结 ToolError、校验类型/矩阵并驱动安全审计、受控重试或终止；不记录正文、detail、堆栈、地址或私密字段。
- [✅] side effect 工具业务幂等结果默认保留 30 天；HTTP retry、resume 和 worker 接管复用原逻辑键。
- [✅] 业务端返回 `409 IDEMPOTENCY_CONFLICT` 时校验本次 digest 与已记录 digest，按不可重试契约/实现错误终止并写安全审计，不能生成新 key 绕过冲突。
- [✅] 按 `cancellation_behavior=cancellable/non_cancellable/query_after_commit` 传播取消；不可中止或已提交工具只用原逻辑幂等键查询结果，不允许旧 fencing/privacy version 推进状态。
- [✅] 每次调用前复核 authorization version；业务服务按当前 archive/owner/generation epoch 二次鉴权。
- [✅] authorization version 变化、撤销拒绝和 connector/target 授权失败写 RuntimeAuditEvent，不记录凭据、endpoint secret 或业务 payload。
- [✅] 测试 `memory.get_snapshot` 和 `memory.publish_playback_document` 参数映射、key 轮换、稳定幂等、超时/接管、SSRF、权限撤销和 `GENERATION_SUPERSEDED`。
- [✅] 对齐冻结 `ToolRequest/ToolResult`：可信 Run/Step 生成 context、发送关联 headers、双层 schema 校验和双向 contract test 已完成。

### Task 8: ModelGateway、PromptRegistry、ContextManager、结构化输出

**Files:**
- Create: `app/runtime/model_gateway.py`
- Create: `app/runtime/provider_traffic.py`
- Create: `app/runtime/prompt_registry.py`
- Create: `app/runtime/context_manager.py`
- Create: `app/runtime/json_repair.py`
- Create: `app/runtime/langchain_components.py`
- Create: `app/runtime/semantic_validation.py`
- Create: `app/services/model_usage_service.py`
- Create: `app/core/provider_security.py`
- Create: `app/core/model_policy.yaml`
- Create: `app/schemas/model.py`
- Create: `app/schemas/context.py`
- Modify: `app/api/endpoints/capabilities_api.py`
- Test: `tests/test_model_gateway.py`
- Test: `tests/test_provider_traffic_controller.py`
- Test: `tests/test_context_manager.py`
- Test: `tests/test_model_usage_service.py`
- Test: `tests/test_untrusted_content.py`

**Interfaces:**
- Produces: `ModelGateway.generate_structured(prompt_id, variables, output_schema, policy, call_context: ModelCallContext) -> StructuredResult`；结果固定包含 `validated_value/parse_status/safety_status/usage/route/fallback_used`，原始模型文本不进入跨层 DTO。
- Produces: `ProviderTrafficController.acquire(route_key, estimated_tokens, deadline_at) -> ProviderPermit`、`ProviderTrafficController.mark_started(permit) -> None`、`ProviderTrafficController.settle(permit, actual_tokens, outcome, retry_after_seconds) -> None`；ProviderPermit 固定包含 `permit_id/route_key/acquired_at/expires_at/reserved_tokens`，不含 prompt、API key 或业务正文；outcome 只允许 `succeeded/failed/timeout/rate_limited/aborted_before_send/outcome_unknown`。
- Produces: `ModelUsageService.begin_attempt(call_context, route, permit, prompt_ref, reserved_estimated_cost) -> ModelUsageAttempt`、`ModelUsageService.settle_attempt(usage_id, outcome, observed_usage, provider_request_id, error_code) -> None`；settle 只能条件补充既有 usage 行的无内容计量字段。允许 `running -> aborted_before_send/succeeded/failed/outcome_unknown`；outcome_unknown 仅在 usage/provider request 身份匹配时补充为 succeeded/failed，其余终态不可逆，且任何结算都不能推进 run/step/workflow 状态。
- Produces: `ContextManager.build_node_context(run, state, node) -> NodeContext`；上下文固定包含 `trusted_instructions/untrusted_items/token_budget/source_refs/redaction_summary`，信任域不可由模型输出覆盖。
- Produces: `SemanticValidator.validate(value, schema, trusted_refs, policy) -> SemanticValidationResult`；结果固定包含 `valid/error_codes/safe_summary/normalized_value`，不能返回未经验证的控制字段。

- [✅] PromptRegistry 根据 `prompt_id@version` 加载 prompt 文件。
- [✅] 校验 prompt manifest 的 `prompt_id/version/owner_agent/input_schema/output_schema/model_policy/guardrail_policy/status`，不自动回退 latest；调用链可将 prompt id/version 写入 `AgentModelUsage`。
- [✅] 创建 `model_policy.yaml`，至少包含 `reasoning/balanced/emotional_writing/cheap_structured/strict/private_first` 六类策略。
- [✅] 每个部署 route 强制配置流控、permit、circuit、`route_config_version/pricing_config_version`、统一价格单位、能力、驻留和 token 窗口；业务请求不得覆盖。
- [✅] route 注册校验 `permit_ttl_seconds >= timeout_seconds + settle_margin_seconds`，HTTP timeout 截断到 route、Run deadline、lease 的最小窗口；非法配置拒绝加载。
- [✅] Runtime 使用 `usd_per_1k_tokens`，并将 route 的 `pricing_config_version`、价格和 cost unit 冻结至每个 usage attempt；缺少部署治理字段的 route 不可用。
- [✅] ModelGateway 根据 `model_policy.yaml` 解析 `max_output_tokens`、capability requirements 和显式 template fallback；provider/model/endpoint 仅来自部署 route，第一版不向业务请求、Package 或 prompt 暴露 temperature/provider 覆盖入口。
- [✅] 路由顺序固定为 Runtime 紧急禁用/驻留/租户策略 -> Agent logical policy -> 部署映射 -> 显式 fallback；业务请求不能覆盖 provider/model/base URL/key。
- [✅] ProviderTrafficController 使用 Redis 原子操作检查共享 blocked_until、熔断、并发 semaphore、RPM 和 TPM；按输入估算加 max_output_tokens 预留 token。拒绝时不调用上游，返回 retry_after 供 policy 在剩余 deadline 内等待或 fallback。
- [✅] permit 在 Redis 中使用 `acquired -> started -> settled` 状态机；发送边界复核后先以 CAS 执行 `mark_started`，成功后才能开始 HTTP。settle 使用 CAS 且幂等，重复/非法转换不重复增减计数。
- [✅] 每次候选模型请求独立 acquire，并在 finally settle。只有 acquired permit 可用 `aborted_before_send` 回滚并发槽与 RPM/TPM 预留；started permit 用实际 usage 结算 TPM，无 usage 或结果未知时保留 RPM/TPM 预留到窗口过期。acquired permit TTL 回收时回滚未发送预留，started permit TTL 回收只释放并发槽；重试等待期间不持有 permit。
- [✅] 上游 429 的 Retry-After 写共享 blocked_until，其他 Worker 随后 acquire 时同步退避；fallback route 使用独立 permit。共享流量控制不可用时 fail closed，按 policy provider fallback、模板 fallback 或安全失败，不得降级为单进程 limiter。
- [✅] Gateway 对 429 只在 Retry-After 未超过 route/Run/lease 最小窗口时等待一次，等待期间不持有 permit；重试和 fallback 都重新 acquire 独立 permit。fallback 只认部署 route 的 `fallback_route_id` 与 Run 快照 allowlist，不能从请求选择 Provider。
- [✅] permit 等待计入 active elapsed，acquire deadline 取节点 timeout、剩余 max_run_seconds 和 run_deadline_at 的最小值；超出 deadline 不继续睡眠或请求上游。
- [✅] `ModelCallContext` 固定包含 `run_id/step_id/execution_attempt/lease_owner_id/fencing_token/privacy_version/authorization_version/deadline_at`，只由 Executor 从有效 LeaseContext 构造，禁止从 prompt、业务 input、AgentState 或模型输出覆盖。
- [✅] `acquire` 返回后重新校验 lease/fencing、cancel、package、privacy、authorization、route/capability 和 deadline；等待期间失效时 settle permit 为 `aborted_before_send`，不写 usage、不请求 provider。
- [✅] 二次校验通过后，先由 ModelUsageService 按 ModelCallContext 条件写 `AgentModelUsage.running`，保存当前 execution/model attempt、permit、prompt 引用、capability snapshot 和保守 token/成本预留；提交后、真正发送 HTTP 前再次执行发送边界复核。此时失效则把既有 usage 与 acquired permit 结算为 `aborted_before_send`；复核通过后也必须成功 `mark_started` 才能发送。每次 retry/fallback 新建独立候选 attempt。
- [✅] provider 返回或抛错时，在 finally 中分别 settle permit 与同一 usage；响应后再次校验 ModelCallContext，失效时只允许结算无内容 token/成本/状态并丢弃输出，不能更新 AgentRun、AgentStep、Checkpoint、Artifact 或工作流状态。
- [✅] 请求超时、进程崩溃或响应归属无法确认时不写零 usage；对账将过期 running/started 标为 `outcome_unknown` 并保留预留成本。迟到 usage 只允许对同一 `usage_id` 做幂等、单调结算。
- [✅] 记录 route config version、capability snapshot，以及 permit wait/reject/TTL recovery/shared cooldown/circuit 指标；普通日志不写 route secret 或 prompt。
- [✅] `ModelCapabilityEvaluator` 只在 ProviderTrafficController 可用且 route/policy/驻留/窗口完整时宣告对应模型增强可用；`/runtime/capabilities` 仅返回安全布尔值和逻辑 policy，Redis 异常时关闭模型增强，业务 baseline 和模板能力保持可用。
- [✅] provider endpoint 只从管理员注册表解析；自定义 base URL 执行协议/host/port allowlist、DNS/IP 校验、私网阻断和逐跳重定向复检。
- [✅] Provider Adapter 每次 HTTP 发送前重新 DNS 预检，使用无代理、无 keep-alive 的受控 Transport 读取真实 TCP 对端；响应解析前仅接受本轮公网解析集合中的 peer，缺失/非法/不匹配均 fail-closed，且拒绝重定向。
- [✅] `private_first` 未配置合规私有 provider 时返回 `capability_disabled`，Memoir 节点仅执行 policy 明确的模板 fallback，不静默切换任意云 provider。
- [✅] 调用前校验 structured output、vision、上下文长度、数据驻留和 thinking 参数；能力不满足时只走当前 policy 明示 fallback，无匹配路由时返回配置错误。
- [✅] 调 LiteLLM 或 Provider Adapter。
- [✅] 使用 LangChain `ChatPromptTemplate` 拼装受信任模板与脱敏不可信数据槽，使用 Pydantic/structured output parser，并在 ModelGateway 调用边界接入 ContextManager、usage 和安全摘要；不启用动态 createAgent。
- [✅] ContextManager 使用 Prompt 的 model_policy 与 route 的 context/output 窗口计算 fail-closed 输入预算，再按 `extract_highlights/plan_chapters/generate_scenes` 的受控节点 cap 收紧；素材与工具键名/数量摘要共享同一窗口，未知节点或无效预算拒绝。
- [✅] ContextManager 对素材分块、长日记摘录与工具结果摘要压缩：固定预算下按顺序截取素材，工具结果只进入键名/数量摘要，原始 payload 不进入模型上下文。
- [✅] ContextManager 把 Runtime/Package 规则标为 trusted instructions，把日记、赌局、RAG/Web Search 和工具结果放入 untrusted 数据槽。
- [✅] ContextManager 只保存上下文摘要，不保存完整私密原文。
- [✅] 解析 JSON 输出并用 Pydantic schema 校验。
- [✅] schema 后统一校验 material/source ID 与冻结 owner scope、scene/action 引用和覆盖、数量、时长、统计及 action enum；`owner_id` 等控制字段和任意 `tool_params` fail-closed，语义越权只返回受控错误码并走模板 fallback。
- [✅] 解析失败时先执行一次无执行能力的 JSON repair；Schema 或确定性语义校验仍失败时，最多调用一次版本化 `structured-output-repair@v1`。Repair 候选只进入有界 untrusted data 槽，第二次调用重新执行预算、Redis permit、usage、lease/fencing、cancel、privacy、authorization、tenant/驻留、route/capability 与 deadline 校验，仍失败立即 template fallback。
- [✅] `AgentModelUsage` 不保存 prompt、模型原文、工具 payload 或签名 URL；privacy purge 后删除 checkpoint 并清空 Run/Step/ToolCall/Artifact 的可承载内容字段，同时对历史或异常直写的 usage JSON 做白名单净化，仅保留 status、route、token、成本、Provider 请求身份与严格 thinking summary 等无内容治理字段。
- [✅] `thinking_summary_json` 只持久化由可信 model policy/route 派生的能力开关、输入/输出预算与固定归一化版本；持久化 service 严格拒绝 hidden reasoning、模型原文和任意自由字段。
- [✅] AgentModelUsage、日志、public trace、callback、checkpoint、artifact 与审计禁止记录完整 prompt、私密素材、模型原文、隐藏推理、工具 payload、签名 URL 和密钥；安全边界仅允许受控 ID、计数、状态、错误码、预算和版本摘要。
- [✅] `RuntimeTrafficEvent` 以 event/route/result 的唯一分钟窗口原子聚合 permit 拒绝、Retry-After、熔断开闭、Redis fail-closed 与提示/语义拒绝；仅首次跨阈写无内容 `RuntimeAuditEvent`，SQLite 并发与 PostgreSQL harness 均有覆盖。
- [✅] PostgreSQL 真实子进程与隔离 Redis 回归已验证：Worker 仅以 `completed + role + result_code` 无内容终态通知测试；迟到 publish 及迟到模型响应均在 cancel/purge 后释放，旧 lease 不能恢复业务 revision、Artifact、Checkpoint、Step 或 ToolCall；两个 Controller 共享真实 Redis permit 与 429 冷却。
- [✅] 覆盖模型成功、语义非法 schema 合法的模板 fallback、Provider 异常、脏 JSON 与 privacy purge 后查询/对账的敏感字段阻断；Provider、日志、trace、callback、checkpoint、artifact 与审计均 fail-closed 或完成清除。
- [✅] 测试多 Worker 不突破共享并发/RPM/TPM、acquired/started 两类 TTL 回收、permit TTL/请求超时配置拒绝、实际 token 结算、`mark_started` 与 settle 重复调用幂等、`aborted_before_send` 只回滚 acquired permit、共享 Retry-After、fallback 分区和 Redis 不可用 fail closed。

### Task 9: Evaluator、Guardrails、PolicyEngine

**Files:**
- Create: `app/runtime/evaluator.py`
- Create: `app/runtime/guardrails.py`
- Create: `app/runtime/policy.py`
- Modify: `app/core/admission.py`
- Modify: `app/services/admission_service.py`
- Create: `app/schemas/evaluation.py`
- Test: `tests/test_policy_engine.py`
- Test: `tests/test_evaluator_guardrails.py`

**Interfaces:**
- Produces: `Evaluator.evaluate(step, state) -> EvaluationDecision`；DTO 固定包含 `decision/reasons/scores/next_node/safe_summary`，其中 decision 只能为 `pass/retry/fallback/human_review/fail`。
- Produces: `PolicyEngine.assert_can_continue(run, counters) -> None`，违反硬限制时抛出版本化安全错误，不返回可被忽略的布尔值。
- Consumes: Task 4 的 AdmissionService 管理 run 占用，Task 8 的 ProviderTrafficController 管理实际 provider/model 流量；本任务只组合执行期预算和评价策略，不重新实现两套限流器。

- [✅] 实现 schema evaluator。
- [✅] 实现素材引用 grounding evaluator。
- [✅] 实现场景长度和空作品 completeness evaluator。
- [✅] 校验 MemoirAgent MVP 正常场景数 3～8、契约硬上限 16 和单卡主体文案 80 字上限；越界进入裁剪、fallback 或拒绝发布。
- [✅] 校验 Scene type/safety level 和完整 Action enum、order/duration/reference；当前 `focus_image/play_tts` 在 capability 关闭时进入安全 fallback。
- [✅] 实现敏感字段和情绪安全 guardrails。
- [✅] 实现 step/tool/model/token/cost/time/retry 硬限制；`max_model_calls/max_estimated_cost` 按实际或可能已发出的物理 attempt 统计，aborted_before_send 不计，已观察 usage 使用实际估算成本，running/outcome_unknown 使用预留成本，同一行不重复相加。
- [✅] 区分 active elapsed、held、queue、approval 和 wall clock 时钟，禁止用 `created_at + max_run_seconds` 判断执行超时。
- [✅] AdmissionController 使用 AdmissionBucket 限制全局/caller/tenant/agent 的 held/queued/running；PolicyEngine 把剩余 active/deadline 传给 ProviderTrafficController，不能把未选路由的 run 预记到 provider，也不能绕过 Task 8 的共享 permit。
- [✅] 每次发布前评价写入 `AgentEvaluation`，仅持久化受控错误码与计数摘要。
- [✅] 测试超 step/工具/活跃时间、排队不消耗执行预算、Admission 过载、未引用真实素材和刺激性表达的决策。

### Task 10: Checkpoint、Resume、Fallback

**Files:**
- Create: `app/runtime/checkpoint.py`
- Create: `app/runtime/artifact.py`
- Modify: `app/runtime/executor.py`
- Test: `tests/runtime_test_checkpoint_resume.py`
- Test: `tests/runtime_test_workflow_executor.py`

**Interfaces:**
- Produces: `CheckpointStore.save(run_id, step_name, state, lease_context)`、`CheckpointStore.load_latest(run_id, actor, reason_code) -> CheckpointState`、`WorkflowExecutor.resume(run_id, lease_context) -> AgentRunResult`；读写均复用 Task 4.5 的 fencing/privacy/authorization 上下文，读取必须产生审计事件。
- Produces: `ArtifactStore.save(envelope, lease_context) -> AgentArtifactRef`、`ArtifactStore.purge_private(run_id, privacy_version) -> PurgeResult`；默认只保存摘要、digest 和业务资源引用。

- [✅] 每个节点成功后保存 checkpoint。
- [✅] checkpoint 只保存恢复必需状态，使用加密 blob/storage ref、schema version、classification、TTL、content digest 和 privacy version。
- [✅] Checkpoint/Artifact/模型工具临时结果使用 `privacy_state=active AND privacy_version=expected AND fencing_token=expected` 条件写。
- [✅] Checkpoint 每次解密读取都通过 AuditService 记录 actor/action/reason/outcome；列表、public trace 和普通运维查询禁止解密 payload。
- [✅] workflow 决策为 `human_review` 时，先持久化恢复 checkpoint；成功后由持有有效 fencing token 的 Worker 在状态事务中设置 `status=waiting_human`、`waiting_expires_at=min(now + approval_ttl_seconds, run_deadline_at)`、`dispatch_state=finished`，释放 lease，并同步完成 Admission `running -> none`、`status_version` 递增和 callback outbox。
- [✅] retry/resume 读取最近兼容 checkpoint；`partial` 仅重做 checkpoint 未完成的 optional 节点，已成功发布节点和已提交副作用不会重放。
- [✅] PostgreSQL 真实迟到模型回归在 Provider 请求已发出后并发 cancel/purge、再释放响应；仅允许无内容 `AgentModelUsage` 结算，不能复活 checkpoint、step、artifact、tool call 或业务 revision。
- [✅] 工具 side effect 节点恢复前检查幂等键。
- [✅] 实现 `template_highlights`、`template_chapters`、`template_scenes`、`default_actions` fallback。
- [✅] 测试模型节点失败后进入模板场景，原子发布失败后稳定幂等重试，purge 与迟到模型返回并发时私密 payload 不复活。
- [✅] 修正 `WorkflowExecutor` checkpoint 输入：仅投影路由进度、fallback 标记、副作用稳定逻辑键/安全结果引用与 digest，禁止 Snapshot/tool payload、`sanitized_material`、prompt/模型中间文本、Scene 和 PlaybackDocument 进入密文。**（2026-08-11 第四次最小收口 ✅：`executor.py` `_SAFE_CHECKPOINT_KEYS` 白名单（`fallback_flags/completed_node_ids/completed_steps/resume_from_node_id`）+ 非白名单键 `CHECKPOINT_STATE_INVALID` 拒绝；`test_executor_checkpoint_decrypted_blob_excludes_all_five_content_sentinels_and_playback` 五类正文哨兵 + legacy 拒绝 purge 测试为证）**
- [✅] 修正 resume：按当前 privacy/authorization 重取 Snapshot 并重算无副作用内容节点，已发布等副作用只按稳定键 query-after-commit；为旧完整状态 checkpoint 增加版本拒绝、撤销/purge 与防复活回归。**（2026-08-11 第三次最终收口 ✅：= P1 query-after-commit digest 修复 + P2 safe_to_rerun legacy 识别 + P3 authorization/privacy 防复活 + legacy purge 跨 Session finish() commit 持久化 + 日志隐私 + fail-closed；`runtime_test_workflow_executor.py` 28 passed 为证）**

### Task 11: Callback Dispatcher 与 Public Trace

**Files:**
- Create: `app/services/callback_service.py`
- Modify: `app/services/outbox_service.py`
- Modify: `app/dispatcher.py`
- Create: `app/services/public_trace_service.py`
- Create: `app/schemas/callback.py`
- Create: `app/schemas/public_trace.py`
- Test: `tests/test_callback_service.py`

**Interfaces:**
- Produces: 状态事务内 `CallbackEvent + RuntimeOutboxEvent`，并为 Task 4.5 的 OutboxDeliveryHandler 注册、启用 `callback`，支持 dispatcher lease 投递与原事件重放。

- [✅] 根据 `ui-trace.yaml` 生成 `public_trace`。
- [✅] 将详细 RuntimeEvent 确定性映射为 `run_started/step_changed/waiting_human/partial_succeeded/run_succeeded/run_failed/run_cancelled`，其中 `human_review_requested -> waiting_human`；映射结果不得携带内部模型或工具数据。
- [✅] callback payload 只包含安全摘要。
- [✅] 每个 run 的 callback 使用单调递增 `event_seq`。
- [✅] callback payload 包含 `event_id`、`event_seq`、`status_version`。
- [✅] CallbackEvent 创建后不可修改；投递状态、attempt、next retry、lease 和 dead letter 只更新 RuntimeOutboxEvent。
- [✅] callback 请求使用 HMAC-SHA256 签名，包含 `X-Agent-Runtime-Id`、`X-Agent-Key-Id`、`X-Agent-Run-Id`、`X-Agent-Business-Id`、`X-Agent-Event-Id`、`X-Agent-Event-Seq`、`X-Agent-Timestamp`、`X-Agent-Signature`、`Idempotency-Key`；幂等键固定为 `callback:{event_id}`。
- [✅] dispatcher 对原始 body bytes 计算签名，禁止重定向；接收侧测试覆盖恒定时间比较与密钥轮换窗口。
- [✅] 状态变化、event_seq 分配、CallbackEvent 和 callback outbox 在同一事务提交。
- [✅] dispatcher 使用 lease，遵守 Retry-After，失败后最多 5 次指数退避并进入 dead letter。
- [✅] callback handler 注册成功后才把 `callback` 加入 dispatcher enabled event types；Task 11 前积压的 pending callback 使用原 `event_id/event_seq/status_version` 投递，不能补造事件或把“未注册”计为失败。
- [✅] 业务接收端以 `event_id` 为权威去重标识；同一事件与 body hash 重放直接成功且不重复写入，同一事件对应不同 body hash 返回 HTTP `409 Conflict` + `IDEMPOTENCY_CONFLICT`。
- [✅] `event_seq` 在同一个 `run_id` 生命周期内全局单调递增；retry/resume 后继续从当前最大值累加。
- [✅] 首次投递、自动重试和 dead letter 重放复用原 `event_id/event_seq/status_version/Idempotency-Key`，不创建更晚事件；发送前复核 callback target 当前授权。
- [✅] 测试 `run_started`、`step_changed`、`waiting_human`、`partial_succeeded`、`run_succeeded`、`run_failed`、`run_cancelled` payload 均包含版本字段且不含敏感数据；未启用人工确认 callback 的 package 不能进入不可见的 waiting 分支。
- [✅] 测试 callback 签名原文、时间戳过期、`run_id/business_id/event_id/event_seq` header/body 不一致、签名错误、幂等键缺失或未由 event_id 正确派生、同事件相同 body 重放成功及同事件不同 body 冲突。
- [✅] 测试 callback 乱序重放时不会生成更高 `event_seq`，业务端可按版本拒绝状态倒退。
- [✅] 测试状态提交与 outbox 原子性、dispatcher 失联接管、dead letter 和授权撤销停止 callback。
- [✅] 测试 Task 11 前积压 callback 在 handler 启用后按原事件身份投递，且启用配置与 readiness 一致。

### Task 11.5: 补偿与对账任务

**Files:**
- Create: `app/reconciler.py`
- Create: `app/services/reconciliation_service.py`
- Create: `app/runtime/reconciliation.py`
- Create: `app/schemas/reconciliation.py`
- Test: `tests/test_reconciliation_service.py`

**Interfaces:**
- Produces: `ReconciliationService.scan_and_repair(now: datetime) -> ReconciliationReport`；DTO 字段固定为 `scan_id/started_at/finished_at/scanned_count/repaired_count/failed_count/alerted_count/actions_by_type/error_codes`。
- `app/reconciler.py` 负责进程入口、周期调度和多实例 lease；`runtime/reconciliation.py` 负责纯规则判定，`ReconciliationService` 负责事务、条件写与报告聚合。

- [✅] 扫描 pending/queued 与 run_dispatch outbox 不一致，重放同一 dispatch event。
- [✅] run_dispatch dead letter 超过恢复上限时，把仍未 claimed 的 run 条件更新为 `failed(DISPATCH_FAILED)`，同事务通过 AdmissionService 执行 `held|queued -> none` 并生成 callback。
- [✅] 扫描 lease/heartbeat 失效并回收，创建新 execution attempt，旧 fencing token 失效。
- [✅] 分别扫描 active elapsed、held、queued、waiting_human 和 wall clock 超时；`waiting_human` 到达 `waiting_expires_at` 后按 package 的 `waiting_human_timeout_action` 条件恢复或终止，fallback 在同一事务完成 Admission `none -> queued` 和 run_dispatch outbox，failed/cancelled 在同一事务递增 `status_version` 并创建 callback outbox，迟到审批因版本条件失败。任何 dispatch_state 变化都与对应 Admission 迁移同事务。
- [✅] 扫描 callback dead letter，保留原事件身份供重放并输出主动查询修复提示。
- [✅] 扫描 `AgentToolCall.running` 超时记录，按稳定 logical operation key 查询/重试或标记失败。
- [✅] 扫描超过 `request_deadline_at` 仍为 `AgentModelUsage.running` 的记录并条件改为 `outcome_unknown`；不猜测零 token/成本，不用旧 fencing 推进 run。后续若收到同一 usage 的可信 provider 计量，只允许幂等补充无内容 usage 字段。
- [✅] 扫描 purge_requested 清理进度、package revoked 在途 run 和 authorization version 变化；revoked 的 held/queued/waiting_human run 直接条件终止、释放 Admission 占用并生成 callback，claimed run 写取消请求后由安全边界终止并在最终收敛事务释放 running。
- [✅] create/start/retry 冻结并复核部署受控 authorization version；Executor、ModelGateway、ToolGateway、Callback Dispatcher 与 Reconciler 在外部副作用前复核版本。版本变化仅以安全结论写 `RuntimeAuditEvent` 与对账动作计数，不记录授权配置、凭据或业务 payload。
- [✅] 比较 AdmissionBucket 与按 AgentRun.dispatch_state 聚合的 global/caller/tenant/agent 占用；发现漂移时按固定锁顺序和 bucket version 条件修复，计数不得为负，并输出不含业务 payload 的审计与指标。
- [✅] 清理已过期 IdempotencyRecord；purge scope 仅在对应 privacy state 已 `purged` 且满足审计保留策略后清理。
- [✅] 默认每 5 分钟执行一次对账；同一对象连续 3 次修复失败后升级告警。
- [✅] 多实例部署时，对账任务使用数据库/分布式 lease 或按 `run_id` 哈希分片；同一对象同一时间只允许一个修复者。
- [✅] 输出不含业务 payload 的 `ReconciliationReport` 结构化日志和指标；第一版不为报告单独建表，对象级审计继续使用 AgentRun/outbox/ToolCall 权威记录。
- [✅] 测试 dispatch/callback 两类 dead letter、lease 回收、独立时钟、`waiting_human` 超时与迟到审批竞态、purge 对账、tool call 超时和 model usage running 超时处理。

### Task 12: MemoirAgent MVP 包

**Files:**
- Modify: `app/agents/memoir_agent/*`
- Create: `app/runtime/memoir_nodes.py`
- Test: `tests/test_memoir_agent_nodes.py`

**Interfaces:**
- Produces: `memoir_agent@1.0.0` 可执行 workflow。

- [✅] 实现 `load_snapshot` 工具节点。
- [ ] 扩展 `load_snapshot`/Snapshot reader 到跨工程 Snapshot Tool v1：读取 `schema_version + materials[]`，每项只接收 `material_type/source_ref/sanitized_payload`，目标类型限定为 `diary/completed_bet/handbook_note/matured_wish/bucket_list_completion`；把现有 `bet_items/bets`、`bet:<id>` 限定在显式 legacy reader，并在可信 allowlist 建立前单向归一化为 `completed_bet:<id>`。同一 Snapshot 混用新旧赌约引用时 fail-closed，新 provider 与发布 payload 不得输出旧格式。
- [✅] 实现 `sanitize_materials`，脱敏用户 ID、昵称、手机号、地址、openid、token。
- [✅] 实现 `compute_stats`，不依赖 AI 计算基础统计。
- [✅] 实现 `extract_highlights` 模型节点与 `template_highlights`。
- [✅] 实现 `plan_chapters` 模型节点与 `template_chapters`。
- [✅] 实现 `generate_scenes` 模型节点与 `template_scenes`。
- [✅] 实现 `generate_actions` 规则节点与 `default_actions`。
- [✅] 实现 `safety_review`。
- [✅] 构建完整 `MemoryPlaybackDocument + scenes + actions + media_manifest`，执行引用、数量、时长和 schema 语义校验；每个 AI Scene 的 `source_refs_json` 只保留由当前 snapshot allowlist 归一化通过的 material/source ID，并随发布 payload 原样提交。第一版媒体能力关闭时生成必填空清单，不省略契约字段。
- [✅] legacy 基线已实现 `publish_playback_document` 调 `memory.publish_playback_document`，以 `{"input":{...}}` 单次传 snake_case 完整 document/scenes/actions/`media_manifest`、`run_id/snapshot_id/generation_epoch` 和稳定逻辑幂等键；Runtime 与业务后端均对补默认值前的原始 `document` 以 UTF-8/不 ASCII 转义/键排序/紧凑分隔符规范 JSON 计算 digest，Runtime 接收并保存 `revision/content_digest` 到 `publish_result`。本勾选只证明发布与 digest 基线；`input+context`、关联 header 和外层 `ToolResult.schema_version` 仍属于 Task 7 未完成项。
- [✅] 只有发布成功后才允许 AgentRun 进入 succeeded/partial；`GENERATION_SUPERSEDED` 停止旧 run 的剩余副作用。
- [✅] 第一版预留 `enabled=false` 的 `memory.enqueue_tts` 契约；`enqueue_media_tasks` 在 capability 关闭时确定性返回 `skipped(CAPABILITY_DISABLED)`，不创建媒体任务或触网。业务 callback 只推进未发布内容运行/失败态，发布工具独占 `content_status=succeeded + published_revision`，enhancement 保持 `disabled`。
- [✅] 测试无素材、只有日记、只有赌局、`source_refs_json` 丢失或含已删除/未知/跨 owner/跨关系段引用、强制拉黑表达、缺失/非法 `media_manifest` 被拒绝、媒体关闭时空 `media_manifest` 被接受、digest 不一致、发布与删除/新一轮生成竞态及原子回滚。
- [ ] 增补 Snapshot Tool v1 五类 canonical material、legacy `bet` 单向归一化、同一 Snapshot 新旧赌约引用混用拒绝，以及发布 payload 不含 `bet:<id>` 的契约测试。

### Task 13: 最小评测集与端到端测试

**Files:**
- Create: `app/agents/memoir_agent/evals/minimal.jsonl`
- Create: `tests/test_memoir_agent_e2e.py`
- Create: `tests/fixtures/memoir_snapshots/*.json`

**Interfaces:**
- Produces: 可重复执行的 MemoirAgent MVP 验收集。

- [✅] 准备空核心素材 Snapshot fixture（仅验证已合法创建 Run 后的 Runtime 模板兜底；不代表业务后端允许为该场景创建 Archive/Run）。
- [✅] 准备只有日记 fixture。
- [✅] 准备只有赌局 fixture。
- [✅] 准备双方同日记录 fixture。
- [✅] 准备强制拉黑 fixture。
- [✅] 准备模型脏 JSON fixture。
- [✅] mock `memory.*` 工具服务。
- [✅] 跑完整 `held create -> 业务绑定 -> start -> dispatch -> lease claim -> execute -> atomic publish -> callback` 流程。
- [✅] 断言 baseline 在 AI 完成前可播放，原子发布后只切换完整 `published_revision`，Artifact 只保存摘要/digest/业务引用。
- [✅] 断言 public_trace 安全、callback 幂等、fallback 可播放，第一版媒体节点 skipped。
- [✅] 断言详细 RuntimeEvent 到 callback 的映射稳定且不泄露内部数据，并覆盖 3～8 张正常作品、16 张硬上限和 80 字单卡边界。
- [✅] 增加 worker 失联/旧 fencing、dispatch/callback dead letter、purge 迟到结果、package/authorization 撤销和 injection fixtures。
- [✅] 增加 provider permit 并发、acquired/started TTL、共享 Retry-After、fail-closed、permit 等待期间撤权/取消/失租，以及模型请求发出后 Worker 崩溃/usage outcome unknown fixtures；同时覆盖 side effect ToolCall 先落库后崩溃/请求 digest 冲突。
- [✅] 输出 schema/语义校验通过率、素材引用正确率、编造率、情绪安全通过率、fallback 率，以及按 execution/model attempt 聚合的 aborted_before_send、实际成本、预留成本、未知结果和耗时。
- [✅] 断言日志、trace 和评测结果不含 prompt、日记正文、工具原始 payload、签名媒体 URL、隐藏推理和 Checkpoint 内容。
- [✅] 断言 package 生命周期变更、Checkpoint 解密读取、authorization 变化、approval/cancel/retry/purge 和敏感调试访问均产生不含私密 payload 的 RuntimeAuditEvent；生产缺少持久 audit sink 时 readiness 失败。
- [✅] 断言 RuntimeTrafficEvent 在 SQLite 的唯一窗口、并发聚合、阈值首次告警与 Redis fail-closed 下均不保存敏感字段；PostgreSQL harness 使用同一 UPSERT 契约验证聚合。
- [✅] 外部 OTel/LangSmith/调试样本 exporter 默认关闭；启用配置必须显式声明数据分级、采样字段、区域/跨境、保留期、审计权限和 privacy purge 删除能力，并测试脱敏失败时拒绝导出。
- [✅] 运行 `ruff check .`、`mypy app`、`pytest`。

## 6. 第一版不做

- Agent 管理后台。
- Runtime 原生前端 SSE。
- WebSocket。
- 完整 MCP Client / Server。
- Autonomous Agent。
- Hybrid Agent。
- RAG / Retriever。
- 长期记忆。
- LLM-as-judge。
- 代码沙箱。
- 多 Agent handoff / A2A。
- 商业计费。

## 7. 验收场景总表

| 场景 | 预期 |
|---|---|
| 创建不存在版本的 AgentRun | 返回版本不存在，不自动使用最新版 |
| callback target / business connector 未注册或越权 | 创建失败 |
| 相同幂等键不同请求体 | 返回 HTTP 409 + `IDEMPOTENCY_CONFLICT` |
| start/retry/approval 并发抢最后一个 queued 配额 | AdmissionBucket 配额预留、状态迁移和 outbox 同事务；失败请求返回 429、保持原状态且不完成幂等记录 |
| claim/等待/终止/reaper 并发迁移 | bucket 与 AgentRun.dispatch_state 聚合一致，重复或旧 fencing 操作不重复增减，计数不为负 |
| 取消 held/queued/waiting_human | API 直接终止并生成 callback，不等待不存在的 worker；迟到 dispatch 无法认领 |
| workflow 请求人工确认 | 先保存恢复 checkpoint，再原子写 waiting_expires_at、释放 lease/Admission 并发送带 event_seq/status_version 的 waiting_human callback；未启用 callback 时 package 不能注册人工等待分支 |
| waiting_human 超时与审批并发 | 只有 status_version 条件成功的一方推进；超时按 package 策略 fallback、failed 或 cancelled，迟到审批不能覆盖结果 |
| 已合法创建 Run 的空核心素材 Snapshot | Runtime 生成基础卡或模板卡；业务侧门槛与 Archive 创建由 `couple-diary-b` 单独验收 |
| 只有日记 | 生成日记统计和高光 |
| 只有赌局 | 生成赌局统计和高光 |
| 模型输出脏 JSON | repair 成功或进入 fallback |
| 多 Worker 并发请求同一 provider/model | 共享 permit 保证并发、RPM/TPM 不超限，各进程不能用本地计数绕过 |
| permit 持有 Worker 崩溃 | acquired permit 的 TTL 回收并回滚未发送 RPM/TPM 预留；started permit 只回收并发槽并保留速率预留到窗口过期 |
| permit TTL 小于 request timeout + settle margin | route 注册失败且 capability disabled，不发出超出 permit 生命周期的请求 |
| route 缺少价格版本、价格非法或 cost unit 不一致 | route 注册失败且 capability disabled，未知价格不按零成本放行 |
| provider 返回 429 + Retry-After | 共享 blocked_until 使其他 Worker 同步退避，当前节点在 deadline 内等待或显式 fallback |
| ProviderTrafficController/Redis 不可用 | 不请求上游，capability 降级并走显式 fallback 或安全失败 |
| permit 等待期间或 usage 预写后、发送前发生取消、撤权、purge、route 禁用或 Worker 失租 | 发送边界校验失败，释放 permit、回滚该 permit 的 RPM/TPM 预留且不请求上游；若已预写 usage 则结算为 aborted_before_send，HTTP 开始后禁止使用该状态 |
| 模型请求发出后 Worker 崩溃或失租 | 迟到输出不能推进工作流；usage 过期后转 outcome_unknown 并按预留成本计量，后续可信计量只结算原 usage 行 |
| 工具超时 | 按 retry policy 重试 |
| 工具权限拒绝 | 立即失败并告警 |
| 取消发生在副作用工具提交前后 | 可取消工具中止；不可取消或已提交工具仅按原逻辑幂等键查结果，旧 fencing/privacy version 不能推进状态 |
| side effect ToolCall 写 running 后进程崩溃 | 对账按原幂等键和 request digest 查询或重试，业务不重复写入 |
| 相同 side effect key 对应不同 request digest | 返回 409 并按不可重试契约错误终止，不生成新 key 绕过 |
| provider endpoint 指向内网、DNS 重绑定或未授权重定向 | ModelGateway 在连接前或重定向时拒绝并记录安全指标 |
| `private_first` 没有合规私有 provider | 返回 capability disabled 或执行 policy 明示 fallback，不静默切换云 provider |
| 原子发布中 Scene/Action 任一失败 | 事务回滚，`published_revision` 不变 |
| publish 重试/worker 接管 | 稳定逻辑幂等键返回原 revision，不重复写业务数据 |
| 媒体 capability 关闭 | 节点 skipped，AgentRun 不进入 partial |
| callback 重复或 event/body 冲突 | 同一 `event_id + body hash` 使用 `callback:{event_id}` 返回成功且不重复写；同 event 不同 body 返回 409，Runtime 不生成新事件或新幂等键绕过 |
| callback 乱序 | 业务后端按 event_seq/status_version 拒绝状态倒退 |
| Runtime dispatch 失败 | outbox 重试；dead letter 超限后 `failed(DISPATCH_FAILED)` |
| callback handler 尚未启用 | callback outbox 保持 pending 且 attempt 不变，run_dispatch 正常投递；启用 handler 后复用原事件身份发送 |
| Worker 失联接管 | 新 attempt/fencing 接管，旧 worker 迟到写入失败 |
| Worker draining 时仍有在途 run | readiness 503、liveness 成功且 lease 在宽限期内持续 heartbeat；完成则正常终止，宽限期耗尽则主动让 lease 到期并停止写入，只由 reaper 创建一个新 attempt |
| 删除或重新生成并发 | `generation_epoch + active_run_id` 拒绝旧发布 |
| privacy purge 并发 | 迟到 Checkpoint/Artifact 私密 payload 条件写失败 |
| package/authorization 撤销 | held/queued/waiting_human 直接终止，claimed run 在安全边界停止；后续 create/start/retry/resume/模型/工具/callback 被拒绝 |
| 读取加密 Checkpoint 或执行敏感控制操作 | 产生不含正文、凭据和原始 payload 的持久 RuntimeAuditEvent；普通列表不解密 payload |
| 日记包含提示注入 | 不能改变 workflow、工具、connector、统计和发布参数 |
| 运行超预算 | 已发布主产物且仅剩 Runtime 可选步骤时 partial，否则 failed/fallback |
| public_trace 查询 | 不含 prompt、工具原始输入输出、模型原始输出 |
