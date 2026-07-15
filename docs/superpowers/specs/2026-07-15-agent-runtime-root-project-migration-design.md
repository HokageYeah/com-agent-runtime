# AgentRuntime 根工程化迁移设计

## 目标

将 `com-agent-runtime` 明确为唯一的后端与公共 AgentRuntime 工程。移除误建的 `services/agent-runtime/` 子工程，避免两套依赖、迁移、应用入口和同名 `app` 包并存。

本次迁移保留现有情侣日记后端能力，并把公共 Runtime 作为同一 FastAPI 应用内的独立领域模块。`MemoirAgent` 继续作为首个业务验证 Agent。

## 已确认的边界

- 业务后端负责情侣空间、日记、赌局、回忆录归档、权限和播放器数据。
- Runtime 负责 AgentPackage、AgentRun、调度、执行、工具调用、模型调用、检查点、审计和回调。
- Runtime 只能通过业务工具读写回忆录数据，不能直接查询或修改业务数据表。
- 前端继续只调用业务 API，不直接访问 Runtime 的内部运行轨迹。
- Runtime 的私密输入、Checkpoint 和工具原始内容不进入普通日志、API 响应或公开 trace。

## 目标目录

```text
com-agent-runtime/
  app/
    agents/memoir_agent/          # 首个业务 AgentPackage
    api/endpoints/                # diary、memory、runtime 等路由
    contracts/                    # 版本化 API、事件、工具与产物契约
    models/                       # 业务模型与 Runtime ORM 模型，共用 Base
    runtime/                      # 状态机、planner、executor、checkpoint
    schemas/                      # Runtime 请求、响应与 DTO
    services/                     # 业务服务与 Runtime 领域服务
  alembic/versions/               # 唯一的数据库迁移历史
  tests/                          # 唯一测试入口；Runtime 测试按领域命名
  pyproject.toml                  # 唯一依赖与测试配置
```

`app/api/api.py` 是唯一的路由聚合入口；`app/main.py` 是唯一的 ASGI 入口；根 `alembic/` 管理业务表和 Runtime 表。

## 迁移规则

1. 先在根测试目录建立 Runtime 回归测试，并验证测试会因目标模块缺失而失败。
2. 把嵌套工程的 Runtime 契约、领域实现、AgentPackage、模型和测试迁入根工程。迁移时改为复用根 `app.db.sqlalchemy_db.Base`、根配置、根日志和根 Alembic metadata。
3. 通过根 `pyproject.toml` 管理新增运行时依赖。删除嵌套工程的 `pyproject.toml`、锁文件、虚拟环境和独立 Alembic 配置。
4. 为 Runtime 运行状态、持久化字段、鉴权、隐私清理、lease、outbox、调度和失败路径补充中文注释与安全日志。日志只能记录标识、计数、状态和脱敏摘要。
5. 统一更新 README、总控开发计划、后端开发计划及需求文档中的目录与启动说明。所有路径改为根工程相对路径。
6. 在根目录运行测试、Ruff 和 Alembic metadata 检查。验证通过后删除 `services/agent-runtime/`。

## 数据库与迁移策略

根工程已有 `Base`、MySQL 配置和 Alembic 环境，Runtime ORM 模型必须直接加入这套 metadata。已有 Runtime 独立迁移不直接复制执行，而是转换为根 Alembic 的新迁移，`down_revision` 指向当前根迁移链最新版本。

迁移的表包括 AgentDefinition、AgentRun、AgentPlan、AgentStep、AgentToolCall、AgentEvaluation、AgentCheckpoint、AgentArtifact、AgentModelUsage、CallbackEvent、RuntimeOutboxEvent、AdmissionBucket、IdempotencyRecord 与 RuntimeAuditEvent。业务回忆录表保持原有所有权；Runtime 表只保存业务资源定位信息、摘要和 digest。

## API 与运行入口

Runtime 路由挂入根应用版本前缀，采用 `/api/v1/runtime/...`，避免和现有日记、回忆录业务路由混淆。根健康检查保持 `/healthz` 与 `/readyz`；Runtime 细粒度健康与能力发现作为 Runtime 子路由提供。

Worker 使用根工程模块入口，例如 `python -m app.worker`。它与 API 进程共享根配置、数据库与日志，但不在 HTTP 请求线程执行 Workflow。

## 兼容与删除

本次不保留 `services/agent-runtime/` 的可运行兼容层，避免未来再次从错误目录启动服务。删除前会将所有受版本控制的实现、AgentPackage 和测试迁入根工程，并通过根工程命令验证。

不迁移嵌套目录中的 `.venv`、缓存、SQLite 临时数据库或编译缓存。

## 验收

- 根 `pyproject.toml` 是唯一依赖来源。
- 根 `app` 是唯一 Python 应用包，根 `alembic` 是唯一迁移入口。
- Runtime 模块、MemoirAgent 与 Runtime 测试均在根工程。
- 运行时模型被根 Alembic metadata 发现。
- README 和两份开发计划不再引用 `services/agent-runtime/`。
- `services/agent-runtime/` 不再存在。
- 根工程的 Runtime 测试、全量测试与 Ruff 检查通过。
