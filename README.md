# com-agent-runtime

`com-agent-runtime` 是独立部署的公共 Agent 执行服务。它负责 AgentPackage、AgentRun、计划与步骤执行、模型和工具调用、Checkpoint、Artifact、Worker、callback、对账、观测与治理。

当前首个业务 Agent 是 `MemoirAgent`。它通过情侣日记业务后端提供的 Business Tool 读取脱敏快照并发布回忆录作品，但 Runtime 不拥有用户、关系、Archive、Snapshot、密码或 PlaybackDocument 等业务事实，也不直连情侣日记业务数据库。

本文面向第一次接触项目的开发者。按顺序阅读“项目结构 → 环境启动 → 启动脚本 → 开发验证”，即可完成本地启动和基本维护。

## 文档导航

| 想做什么 | 阅读位置 |
|---|---|
| 第一次理解工程 | 本文的“整体架构”“项目结构”“项目结构说明” |
| 启动开发、测试或生产环境 | 本文的“三套环境快速启动” |
| 理解每个脚本用途 | 本文的“启动与运维脚本说明” |
| 填写数据库、HMAC、Redis、模型和媒体配置 | [ENV_CONFIG.md](ENV_CONFIG.md) |
| 运行完整测试或真实 Docker harness | [VERIFICATION.md](VERIFICATION.md) |
| 理解公共 Runtime 需求和安全边界 | [需求设计文档](头脑风暴/docs/AgentRuntime/需求设计文档.md) |
| 确认已冻结的 API/Event/Tool/Artifact 契约 | [契约冻结记录](头脑风暴/docs/AgentRuntime/契约冻结记录.md) |
| 查看开发阶段和历史完成记录 | [总控开发计划](头脑风暴/docs/AgentRuntime/plans/2026-07-07-AgentRuntime-总控开发计划.md)、[后端开发计划](头脑风暴/docs/AgentRuntime/backend/2026-07-07-AgentRuntime-后端开发计划.md) |
| 理解当前回忆录图片生成通道 | [媒体通道设计说明](头脑风暴/docs/AgentRuntime/plans/2026-08-20-回忆录媒体通道设计说明.md) |
| 规划 Docker 部署、回滚和发布验收 | [Docker 部署契约](docker/backend/DOCKER_DEPLOY.md) |

计划文档会保留历史决策和勾选记录。判断当前接口、版本和实现状态时，以 `app/contracts/`、当前路由、Alembic history、contract fixtures 和实际测试为准。Runtime Dockerfile、基础 Compose、test/production Compose、Docker CI 和远程部署工作流已进入本仓库。容器发布必须先遵守 [Docker 部署契约](docker/backend/DOCKER_DEPLOY.md)。

## 整体架构

```text
couple-diary-f 前端
  -> couple-diary-b 业务 API
     -> 用户、关系、权限、Archive、Snapshot、PlaybackDocument
     -> HMAC 调用 com-agent-runtime
        -> AgentRun / Plan / Step
        -> Worker + LangGraph workflow
        -> ModelGateway / ToolGateway / Guardrails
        -> Checkpoint / Artifact / Audit / Callback
        -> MemoirAgent 经 Business Tool 读取快照、发布完整作品
     <- 安全 callback 或主动状态对账
  <- 业务后端返回 published revision 或安全 baseline
```

### 系统所有权

| 数据或能力 | 唯一责任方 |
|---|---|
| AgentDefinition、AgentRun、Plan、Step、ToolCall、ModelUsage、Checkpoint、Artifact、RuntimeAuditEvent | `com-agent-runtime` |
| 用户、关系、业务权限、Archive、Snapshot、密码、PlaybackDocument、`published_revision` | `couple-diary-b` |
| 页面交互、业务状态轮询、Scene/Action 播放 | `couple-diary-f` |

必须遵守的边界：

- 前端只调用业务后端，不直连 Runtime。
- Runtime 不连接情侣日记业务库，只调用预注册且已授权的 Business Tool/callback。
- 完整作品只通过 `memory.publish_playback_document` 在业务后端原子发布。
- 创建链路采用 `held create -> 业务绑定 run_id -> start`，避免业务映射建立前执行。
- 所有副作用使用稳定幂等键；lease、fencing token、generation epoch、privacy version 和 authorization version 共同拦截迟到写入。
- prompt、模型原输出、工具原始载荷、正文、凭据和私有 URL 不得进入日志、public trace、callback、Artifact 或测试输出。

## 项目结构

下面的目录树以当前代码为准。它重点列出长期维护时需要理解的目录和入口，不列出 `.venv`、缓存、日志及本机私有配置。

```text
com-agent-runtime/
├── .github/
│   └── workflows/
│       └── com-agent-runtime.yml             # CI：测试、Ruff、Mypy 等质量门禁
├── alembic/
│   ├── env.py                                # Alembic metadata 与数据库环境入口
│   ├── script.py.mako                        # 迁移文件模板
│   └── versions/                             # 从历史业务表到 Runtime/M6 的完整迁移谱系
├── app/
│   ├── agents/
│   │   └── memoir_agent/
│   │       ├── runner.py                     # MemoirAgent 节点执行与版本门控
│   │       ├── model_gateway.py              # Memoir 节点到公共 ModelGateway 的适配
│   │       ├── 1.0.0/ ... 1.0.7/             # 不可变的版本化 AgentPackage
│   │       │   ├── agent.yaml                # 包身份、版本、策略、Prompt 清单
│   │       │   ├── workflow.graph.py         # 受信任静态工作流声明
│   │       │   ├── input.schema.json         # Run 输入 JSON Schema
│   │       │   ├── output.schema.json        # Agent 输出 JSON Schema
│   │       │   ├── tools.manifest.json       # 允许调用的 Tool 与副作用声明
│   │       │   ├── guardrails.yaml           # 内容与安全护栏
│   │       │   ├── callbacks.yaml            # callback 能力声明
│   │       │   ├── ui-trace.yaml             # 面向业务端的安全进度摘要
│   │       │   ├── prompts/                   # 版本化 Prompt 文件
│   │       │   └── evals/                     # Agent 最小评测集
│   │       └── ...
│   ├── api/
│   │   ├── api.py                            # 路由聚合与 production 路由门禁
│   │   └── endpoints/
│   │       ├── health_api.py                 # Runtime live/ready
│   │       ├── capabilities_api.py           # 已验签能力发现
│   │       ├── agent_runs_api.py             # Run 创建、启动、查询、重试、取消、清理
│   │       ├── memoir/                       # 回忆录业务迁移路由子包
│   │       │   ├── memory_api.py             # dev/test 用户侧回忆录路由
│   │       │   ├── memory_tools_api.py       # dev/test 本地 memory Tool handler
│   │       │   ├── memory_callbacks_api.py   # dev/test 业务 callback consumer
│   │       │   └── memory_status_api.py      # dev/test 历史生成状态路由
│   │       ├── demo_api.py                   # 历史工程示例接口
│   │       └── diary_api.py                  # 历史业务骨架接口
│   ├── contracts/
│   │   ├── api.py                            # AgentRun API wire contract
│   │   ├── events.py                         # RuntimeEvent 与 callback event 枚举
│   │   ├── tools.py                          # ToolManifest/Request/Result/Error
│   │   ├── artifacts.py                      # Artifact envelope
│   │   ├── errors.py                         # 稳定错误码
│   │   └── schema_export.py                  # JSON Schema 导出
│   ├── config/
│   │   └── database_config.py                # 历史数据库 URL 兼容入口
│   ├── core/
│   │   ├── config.py                         # 环境识别、配置加载和 Settings
│   │   ├── security.py                       # Runtime HMAC 签名与请求安全
│   │   ├── authorization.py                  # caller/tenant/connector/target 授权
│   │   ├── connectors.py                     # Business connector 配置解析
│   │   ├── tool_security.py                  # Tool 入站签名与安全检查
│   │   ├── user_auth.py                      # 历史用户 JWT 验证边界
│   │   ├── api_response.py                   # 历史统一业务响应结构
│   │   ├── logging_uru.py                    # Loguru 初始化、脱敏和关闭
│   │   └── model_policy.yaml                 # 逻辑模型策略名
│   ├── db/
│   │   ├── sqlalchemy_db.py                  # SQLAlchemy Engine/Session 工厂
│   │   ├── metadata.py                       # Alembic 使用的统一 metadata
│   │   └── alembic_schema_bootstrap.py       # 隔离 schema bootstrap 支持
│   ├── decorators/
│   │   └── cache_decorator.py                # 历史接口缓存装饰器
│   ├── middleware/
│   │   ├── request_logging.py                # request_id、耗时和安全请求日志
│   │   └── exception_handlers.py             # 统一异常响应
│   ├── models/
│   │   ├── runtime.py                        # Run/Plan/Step/ToolCall/Usage/Outbox/Audit 等权威表
│   │   └── memoir/                           # 回忆录业务迁移模型子包
│   │       ├── memory_archive.py             # Archive 迁移模型
│   │       ├── memory_snapshot.py            # 加密 Snapshot 迁移模型
│   │       ├── memory_playback_document.py   # PlaybackDocument 迁移模型
│   │       ├── memory_scene.py / memory_action.py
│   │       ├── memory_media_asset.py / memory_agent_run_ref.py
│   │       └── bet.py / couple_relationship.py / diary_entry.py / ...
│   ├── runtime/
│   │   ├── planner.py                        # 静态 AgentPlan 构建与校验
│   │   ├── graph_builder.py                  # 静态 LangGraph StateGraph 编译
│   │   ├── executor.py                       # workflow 执行、恢复和安全边界
│   │   ├── state.py                          # 不含原始私密正文的图状态
│   │   ├── context_manager.py                # 上下文预算与敏感信息控制
│   │   ├── model_gateway.py                  # Provider 路由、限流、用量和 fallback
│   │   ├── tool_gateway.py                   # Business Tool 授权、签名、幂等和调用
│   │   ├── callback_gateway.py               # callback 目标与安全发送
│   │   ├── checkpoint.py                     # 加密 checkpoint 存取
│   │   ├── artifact.py                       # 安全 Artifact 存取
│   │   ├── guardrails.py                     # Memoir 内容护栏
│   │   ├── evaluator.py                      # 结构与质量评价
│   │   ├── policy_engine.py                  # step/model/tool/token/成本硬限制
│   │   ├── prompt_registry.py                # 版本化 Prompt 加载
│   │   ├── structured_output.py              # Pydantic 结构化输出解析
│   │   ├── json_repair.py                    # 本地一次 JSON 修复
│   │   ├── semantic_validation.py            # 引用、数量和业务语义校验
│   │   ├── native_tools.py                   # 固定 allowlist 的本地安全工具
│   │   ├── langchain_components.py           # LangChain message 组装
│   │   ├── langchain_tools.py                # LangChain Tool adapter
│   │   ├── observability.py                  # 安全聚合观测对象
│   │   └── *harness*.py / mock_*.py          # 隔离进程、Provider、业务服务测试设施
│   ├── schemas/
│   │   ├── agent_package.py                  # AgentPackage Pydantic 定义
│   │   ├── agent_run.py                      # API 层 Run DTO
│   │   ├── plan.py                           # Plan DTO
│   │   ├── callback.py                       # callback payload
│   │   ├── audit.py                          # 安全审计事件
│   │   ├── public_trace.py                   # 前端可见的安全 trace
│   │   ├── context.py                        # 节点上下文 DTO
│   │   ├── evaluation.py                     # 评价结果 DTO
│   │   └── model.py                          # 结构化模型结果 DTO
│   ├── services/
│   │   ├── agent_package_service.py          # Package 加载、digest 和不可变校验
│   │   ├── agent_run_service.py              # Run 生命周期事务
│   │   ├── admission_service.py              # held/queued/running 容量控制
│   │   ├── idempotency_service.py            # API 写操作幂等
│   │   ├── run_queue_service.py              # Worker claim、lease 和 fencing
│   │   ├── lease_service.py                  # 失效 lease 回收
│   │   ├── outbox_service.py                 # 持久 outbox 创建
│   │   ├── callback_service.py               # callback 事件生成
│   │   ├── callback_delivery_service.py      # callback 投递
│   │   ├── reconciliation_service.py         # Runtime 状态对账与修复
│   │   ├── audit_service.py                  # 无正文持久审计
│   │   ├── observability_service.py          # 安全聚合报告
│   │   ├── traffic_event_service.py          # Provider/安全流量窗口计数
│   │   ├── model_usage_service.py            # 模型 attempt、token 和成本账本
│   │   ├── tool_call_audit_service.py        # ToolCall 生命周期审计
│   │   └── memoir/                           # 回忆录专属服务子包
│   │       ├── memoir_media_service.py       # 图片生成与上传
│   │       ├── memory_*.py                   # 业务迁移、联调和补偿服务
│   │       ├── relationship_archive_service.py # 解绑归档迁移服务
│   │       └── runtime_launcher.py           # 启动 outbox 与 callback 补偿实现
│   ├── scripts/
│   │   ├── agent_runtime_cli.py               # agent-runtime.sh 的真实 CLI 实现
│   │   ├── register_agent_package.py          # Package 注册底层实现
│   │   ├── set_env.py / manage_db.py          # 历史通用脚本，非 Runtime 首选入口
│   │   ├── create_database.py                 # 历史建库脚本，禁止用于 Runtime 专库
│   │   ├── init_database.py                   # 历史 create_all 初始化脚本
│   │   └── docker-entrypoint.sh               # 容器入口辅助脚本
│   ├── utils/
│   │   ├── aliyun/oss_client.py               # 阿里云 OSS 上传客户端
│   │   └── volcano/cv_client.py               # 火山视觉图片生成客户端
│   ├── main.py                                # FastAPI 应用与基础探针
│   ├── worker.py                              # Runtime Worker 常驻进程
│   ├── dispatcher.py                          # Worker 使用的 outbox dispatcher 组件
│   ├── reconciler.py                          # Runtime 对账常驻进程
│   └── memory_runtime_launcher.py             # 保留原 -m 命令的薄兼容入口
├── tests/
│   ├── fixtures/
│   │   ├── runtime-contract-v1.0.0.json       # 公共 Runtime 契约夹具
│   │   ├── memory-runtime-contract-v1.*.json  # 跨仓 Tool 契约夹具
│   │   ├── memory_playback_shared_v1.json     # 播放文档共享夹具
│   │   └── memoir_snapshots/                  # MemoirAgent 场景输入夹具
│   ├── runtime_test_*.py                      # Worker/Executor/安全/并发行为回归
│   └── test_*.py                              # API、服务、迁移、Gateway、媒体和治理测试
├── docs/superpowers/
│   ├── specs/                                 # 已确认的专题设计记录
│   └── plans/                                 # 已执行的专题实施计划
├── 头脑风暴/docs/AgentRuntime/
│   ├── 需求设计文档.md                         # 公共 Runtime 总需求
│   ├── 契约冻结记录.md                         # 当前冻结契约入口
│   ├── backend/                               # 后端详细计划
│   └── plans/                                 # 总控与专题计划
├── .env.development                           # development 团队基础模板
├── .env.test                                  # test 团队基础模板
├── .env.production                            # production 团队基础模板
├── .env.example                               # 最小字段示例
├── .gitignore                                 # 本地配置、日志和缓存忽略规则
├── .pre-commit-config.yaml                    # 提交前自动检查配置
├── AGENTS.md                                  # Agent/Codex 项目级工作约定
├── agent-runtime.sh                           # 配置、迁移、注册、启动、验收统一入口
├── run.sh                                     # 只启动 API 的开发调试入口
├── run_app.py                                 # Uvicorn 启动实现
├── project_structure.sh                       # 输出稳定目录边界
├── docker-compose.postgres-harness.yml        # 隔离 PostgreSQL 真实验证
├── docker-compose.redis-harness.yml           # 隔离 Redis 真实验证
├── alembic.ini                                # Alembic 配置
├── pyproject.toml                             # 依赖、pytest、Ruff、Mypy 配置
├── poetry.lock                                # 锁定依赖版本
├── poetry.toml                                # Poetry 本项目行为配置
├── requirements.txt                           # 历史依赖清单，仅作参考
├── ENV_CONFIG.md                              # 完整配置说明
├── VERIFICATION.md                            # 完整验证说明
└── LLM_PROMPTS.md                             # 可复制的 LLM 协作上下文
```

## 项目结构说明

### 修改需求时应该去哪里

| 需求类型 | 首选目录 | 说明 |
|---|---|---|
| 新增或调整公共 API | `app/contracts/` + `app/api/endpoints/` | 先冻结 wire contract，再实现路由；同步 provider/consumer fixture |
| 接入新业务 Agent | `app/agents/<agent_id>/` + 各分层的 `<business>/` 子包 | AgentPackage/执行适配放 `agents`；业务专属路由、服务、模型分别放入同名子包，不散落在公共根目录 |
| 修改 Run 状态或事务 | `app/services/` + `app/models/runtime.py` | 必须考虑幂等、Admission、outbox、lease/fencing 和 callback |
| 修改 Agent 执行方式 | `app/runtime/` | Planner、Executor、Gateway、Checkpoint、Guardrail 等公共内核在这里 |
| 修改 MemoirAgent 行为 | `app/agents/memoir_agent/` | 版本化 Prompt/Workflow 放新版本目录，执行适配改 `runner.py`，回忆录模型调用改 `model_gateway.py`；不覆盖已发布包 |
| 修改模型路由 | `app/runtime/model_gateway.py` + 部署环境配置 | Provider、model、endpoint 和 key 不允许由业务请求覆盖 |
| 修改 Business Tool | `app/contracts/tools.py` + `app/runtime/tool_gateway.py` | 必须保持授权、签名、稳定幂等键和安全错误合同 |
| 修改数据库结构 | `app/models/` + `alembic/versions/` | 只对 Runtime 专库运行 Alembic；禁止 `create_all()` 兜底 |
| 修改启动或部署流程 | `app/scripts/agent_runtime_cli.py` + `agent-runtime.sh` | `agent-runtime.sh` 是对用户唯一推荐入口 |
| 修改回忆录业务数据或用户 API | `couple-diary-b` | 本仓 `memoir/` 子包中的历史业务实现是迁移与回归证据，不是生产目标归属 |

这里采用“公共根模块 + 业务同名子包”：Runtime 契约、执行内核和通用服务仍是单一公共实现；只有某个业务专属的路由、服务和数据模型才放入同名子包。后续接入新业务时可以复用这个边界，但不应复制一套 Runtime 内核。

### 不要新建或搬迁的结构

- 不创建第二套 `app/`、`alembic/`、`pyproject.toml` 或嵌套 `services/agent-runtime/`。
- 不整体搬迁 `runtime/`、`contracts/`、`services/` 等公共根模块；业务专属实现则应收口到各分层下的同名子包。
- 不把新 Runtime 能力写进 `demo_api.py`、`diary_api.py` 或 `demo_service.py`。
- 不删除历史 memory 模型或迁移来“清理目录”；迁移完成、生产数据盘点和回滚方案明确后再单独处理。

## 集成框架

以下均来自当前 `pyproject.toml` 或工程脚本，不是规划中的候选技术。

| 技术 | 用途 |
|---|---|
| Python 3.13 | 当前运行与类型检查基线 |
| FastAPI | Runtime HTTP API、依赖注入与 OpenAPI |
| Uvicorn | ASGI 进程启动 |
| Pydantic v2 / pydantic-settings | API、AgentPackage、配置和结构化输出校验 |
| SQLAlchemy 2 | Runtime 权威数据、事务、条件更新和 Session 管理 |
| Alembic | 数据库迁移谱系；当前单 head 为 `20260820_0900` |
| MySQL Connector/Python | development/test/production 默认 Runtime 数据库驱动 |
| PostgreSQL + psycopg | Docker 真实并发、锁、Worker/Reconciler harness |
| Redis | 模型并发 permit、RPM/TPM、共享冷却和 fail-closed 流控 |
| LangGraph | 将冻结的静态 AgentPlan 编译为 `StateGraph` |
| LangChain Core | 版本化 Prompt message 和 Tool adapter |
| httpx | Provider、Business Tool、callback 等受控 HTTP 调用 |
| cryptography/Fernet | Snapshot 与 Checkpoint 加密 |
| boto3 | S3/MinIO/COS 兼容私有媒体短期签名 URL |
| Alibaba Cloud OSS SDK | Agent 生成图片上传 |
| 火山视觉 HTTP Client | 可选真实图片生成 Provider |
| PyYAML | AgentPackage、guardrails、callback、UI trace 和策略文件解析 |
| Loguru | 请求、Worker 和后台任务日志；关闭时排空日志队列 |
| pytest / pytest-asyncio | 单元、契约、进程和真实依赖回归 |
| Ruff | Python 静态检查与格式约束 |
| Mypy | Runtime 核心边界类型检查 |
| pre-commit / GitHub Actions | 本地提交前和远端 CI 门禁 |

项目没有把 Provider 写死为某一家模型服务。真实 Provider、模型、endpoint、价格和 API Key 由受控部署配置决定；`MODEL_ROUTES_JSON=[]` 时不调用外部模型，MemoirAgent 走确定性模板降级。

## Docker 部署摘要

基础 Dockerfile、基础 Compose、test/production Compose、Docker CI 和 tag 触发的腾讯云远程部署工作流已进入本仓库（工作流说明见部署契约第 9 节）。部署合同集中在 [docker/backend/DOCKER_DEPLOY.md](docker/backend/DOCKER_DEPLOY.md)，实现与发布必须遵守以下边界：

- 镜像 tag 小写化后必须且只能包含 `test` 或 `production` 之一；两者同时出现或都未出现时拒绝部署。
- 镜像 tag/digest 只标识代码产物和环境；AgentPackage 版本必须另行通过 `register --version` 明确指定，不能从 tag 推导。
- API、Worker、launcher、Reconciler 是四个独立 workload；Compose 依次执行一次性 `prepare` 迁移和 `register` Package 注册，四个长期 workload 只在两道门禁都成功后启动。
- Docker 容器内 Runtime API 固定监听 `8002`；腾讯云宿主回环端口使用 test `18002`、production `18003`，情侣日记仍通过私有别名 `http://runtime-api:8002` 访问。
- production 不在 Runtime Compose 内创建 MySQL/Redis；单 CVM 可通过共享私网复用 Couple Diary 实例，但固定独立库 `couple_diary_agent_runtime_prod`、最小权限账号和 Redis `/15`，并保持 `DB_AUTO_CREATE=false`；test 继续使用完全隔离的依赖和凭据。
- production Compose 强制显式注入 `RUNTIME_IMAGE`（缺失即 fail-closed）：默认服务器本地构建 tag 模式（`RUNTIME_PULL_POLICY` 默认 `missing`），接镜像仓库后可切 `repository@sha256:<digest>` 加 `RUNTIME_PULL_POLICY: always`，可变兜底 tag 无法进入生产；Worker 设置 `stop_grace_period` 覆盖整个节点执行预算，保证 SIGTERM draining 语义不被强杀破坏。
- 镜像以非 root 用户运行；生产 secret 由 secret manager/部署平台在运行时注入，不能写入镜像、构建参数、日志或进程参数。
- 当前 Action 为服务器本地 tag 构建；接入 TCR 后才升级为 digest 发布。回滚不自动 downgrade 数据库，上线后必须完成四个 Runtime 探针。

## 环境与配置加载

### 环境差异

| 环境 | API 默认地址 | Runtime 数据库 | Redis | 建库默认 | 用途 |
|---|---|---|---|---|---|
| development | `127.0.0.1:8010` | `couple_diary_agent_runtime_dev` | `127.0.0.1:6379/15` | 允许 | 日常开发和热重载调试 |
| test | `127.0.0.1:8010` | `couple_diary_agent_runtime_test` | `127.0.0.1:6379/14` | 允许 | 独立测试和联调，不能复用 development 数据 |
| production | `127.0.0.1:8011` | `couple_diary_agent_runtime_prod` | `127.0.0.1:6380/15` | 禁止 | 生产部署；数据库由 DBA/平台预建 |

如果应用运行在 Docker 内，`HOST` 通常改为 `0.0.0.0`，数据库和 Redis 主机改为实际 Compose service 名；容器内 MySQL/Redis 端口仍是 `3306/6379`。完整规则见 [ENV_CONFIG.md](ENV_CONFIG.md)。

### 配置加载顺序

应用按当前 `ENVIRONMENT` 加载：

```text
.env.<environment>
  -> .env.<environment>.local
  -> .env.local
  -> 进程环境变量
```

后加载的同名字段覆盖前面的值。建议：

- `.env.development/.env.test/.env.production` 只保存可提交的团队模板。
- development/test 的真实密码和本机地址放在 `./agent-runtime.sh configure` 生成的 `.env.<environment>.local`。
- production 的凭据由 secret manager 或部署平台注入。
- `.env.local` 会覆盖所有环境的同名字段，除非确实需要全局本机覆盖，否则不要使用。
- 本机 `.local` 文件权限必须为 `0600`，不得提交、截图或复制到工单。

## 三套环境快速启动

### 共同前置条件

1. 安装 Python `>=3.13` 和 Poetry。
2. 在仓库根目录执行 `poetry install`。
3. development/test/production 分别准备独立 MySQL 数据库或具备对应建库权限的账号。
4. 准备独立 Redis DB；不要把测试流控数据写入生产 Redis。
5. 需要运行真实 harness 时安装 Docker 与 Docker Compose。

```bash
poetry install
poetry run python --version
poetry run alembic heads
```

预期 Python 为 3.13.x，Alembic 只显示 `20260820_0900 (head)`。

### development：日常开发

首次配置：

```bash
./agent-runtime.sh configure development
chmod 600 .env.development.local
./agent-runtime.sh doctor development
./agent-runtime.sh prepare development
./agent-runtime.sh register development --agent-id memoir_agent --version 1.0.7 --dry-run
./agent-runtime.sh register development --agent-id memoir_agent --version 1.0.7
./agent-runtime.sh start development
```

说明：

- `configure` 交互询问数据库、Redis、Runtime 地址和业务后端地址，并随机生成 HMAC、Fernet 与 JWT secret。
- 已存在 `.env.development.local` 时不会覆盖。只有确认旧配置和密钥不再需要时才使用 `--force`。
- `doctor` 只输出字段名和安全错误码，不回显值。
- `prepare` 检查固定库名，缺库且 `DB_AUTO_CREATE=true` 时建库，然后执行 Alembic。
- `register --dry-run` 先验证磁盘包和数据库现状；正式 `register` 才写 `agent_definitions`。
- `start` 保持前台运行，按 `Ctrl-C` 统一停止所有托管进程。

只调试 FastAPI、需要 development 热重载时：

```bash
./agent-runtime.sh prepare development
./run.sh development
```

这种方式不启动 Worker、Reconciler 或 launcher，不能用来验证完整 AgentRun 执行。

### test：独立测试和联调

`configure test` 使用的是通用 Redis URL 交互默认值 `/15`。为了不与
development 流控状态混用，在 `Redis URL` 提示处请明确输入
`redis://127.0.0.1:6379/14`，不要直接回车接受 `/15`。Docker 内联调时对应
使用 `redis://redis:6379/14`（`redis` 换成实际 service 名）。

```bash
./agent-runtime.sh configure test
chmod 600 .env.test.local
./agent-runtime.sh doctor test
./agent-runtime.sh prepare test
./agent-runtime.sh register test --agent-id memoir_agent --version 1.0.7 --dry-run
./agent-runtime.sh register test --agent-id memoir_agent --version 1.0.7
./agent-runtime.sh start test
```

test 与 development 即使监听同一默认端口，也必须使用不同数据库、Redis DB 和密钥。不要同时以默认端口启动两套服务；需要并行时先在各自 `.local` 配置中修改端口和 `MEMORY_RUNTIME_BASE_URL`。

如果只运行自动化测试，一般不需要启动常驻服务：

```bash
poetry run pytest -q
```

需要真实 PostgreSQL、Redis、API、Worker 和 Reconciler 的隔离验收：

```bash
docker compose version
./agent-runtime.sh verify
```

`verify` 使用独立回环端口和临时密码，无论成功或失败都会尝试执行 `down -v` 清理容器和 volume。

### production：受控部署

production 禁止运行 `configure`。先由 DBA 或部署平台创建 `couple_diary_agent_runtime_prod`，再通过 secret manager/部署平台注入完整配置。

```bash
./agent-runtime.sh doctor production
./agent-runtime.sh prepare production
./agent-runtime.sh register production --agent-id memoir_agent --version 1.0.7 --dry-run
./agent-runtime.sh register production --agent-id memoir_agent --version 1.0.7
./agent-runtime.sh start production
```

上线前必须确认：

- `DB_AUTO_CREATE=false`，运行账号不是 root，只拥有 Runtime 专库最小权限。
- production 使用独立 HMAC、Fernet、JWT、数据库密码、Redis 凭据和 Provider Key。
- `DEBUG=false`、`RELOAD=false`，CORS 不使用 `*`。
- Runtime 和业务 connector/callback 使用受控 HTTPS 域名，禁止 localhost、私网地址和凭据 URL。
- 模型路由的 Provider、价格、驻留、并发和 API Key 已经过部署评审。
- 外部 exporter 未完成数据治理前保持关闭。
- AgentPackage 已按明确版本注册；磁盘上存在包不等于数据库已注册。

单机部署可以使用 `start production` 前台托管全部进程。容器或 Kubernetes 应把 API、Worker、Reconciler 和周期 launcher 拆成独立 workload，并注入同一套受控配置；不要在多个容器内重复运行 supervisor。

## 启动与运维脚本说明

### 推荐用户入口

| 入口 | 是否首选 | 作用 | 是否连接数据库/启动进程 |
|---|---|---|---|
| `./agent-runtime.sh` | 是 | 转发到安全 Runtime CLI，统一执行配置、检查、迁移、注册、启动和真实验收 | 取决于子命令 |
| `./run.sh <env>` | 仅 API 调试 | 设置 `ENVIRONMENT` 后调用 `run_app.py`，保留 development 热重载 | 启动 API，会连接数据库；不启动后台进程 |
| `run_app.py` | 底层入口 | 按 Settings 启动 Uvicorn `app.main:app` | 启动 API，会连接数据库 |
| `project_structure.sh` | 辅助 | 打印稳定目录边界，不生成或修改文件 | 不连接数据库，不启动服务 |

### `agent-runtime.sh` 子命令

| 命令 | 作用 | 会修改什么 | 适用环境 |
|---|---|---|---|
| `configure-docker <test\|production>` | 交互创建服务器 Runtime Docker 环境配置 | 备份并追加 `runtime-<environment>.env` | test/production |
| `configure-couple-diary <test\|production>` | 从 Runtime 配置生成情侣日记联动配置，默认关闭联动门禁 | 备份并追加 `couple-diary-<environment>.env` | test/production |
| `configure-couple-diary <test\|production> --activate` | 生成情侣日记联动配置并启用 worker 与 Package 回调 | 备份并追加 `couple-diary-<environment>.env` | test/production 联调验收 |
| `configure <env>` | 交互生成本机私有配置和随机密钥 | 新建 `.env.development.local` 或 `.env.test.local` | development/test |
| `configure <env> --force` | 覆盖已有本机配置 | 替换本机密钥和配置，可能使旧加密数据不可读 | 仅确认旧配置废弃后使用 |
| `doctor <env>` | 无内容检查配置完整性、安全性和固定库名 | 不连接数据库，不打印配置值 | 全环境 |
| `prepare <env>` | 先 doctor，再检查/创建固定 Runtime 专库并迁移到 head | 可能建库并执行 Alembic DDL | 全环境；production 默认不建库 |
| `register <env> --agent-id ... --version ... --dry-run` | 加载并校验指定磁盘包，显示将发生的注册动作 | 不写 `agent_definitions` | 全环境 |
| `register <env> --agent-id ... --version ...` | 幂等注册明确版本的 AgentPackage | 写入或校准 `agent_definitions` | 全环境 |
| `start <env>` | prepare 后前台托管完整 Runtime | 迁移数据库并启动四类进程 | 全环境 |
| `verify` | 启动隔离 PostgreSQL/Redis 并运行真实进程测试 | 创建临时容器/volume，结束时清理 | 本机/CI 验收 |

查看帮助：

```bash
./agent-runtime.sh --help
./agent-runtime.sh register --help
```

### `start` 实际启动顺序

```text
取得当前工程启动锁
  -> doctor
  -> 检查/创建 Runtime 专库
  -> alembic upgrade head
  -> 清理本工程同环境的遗留托管进程
  -> 启动 API（run_app.py）
  -> 等待 /healthz 与 /api/v1/runtime/health/ready
  -> 启动 launcher loop（每 5 秒执行一次历史回忆录启动补偿）
  -> 启动 Worker（默认每 1 秒轮询数据库 outbox）
  -> 启动 Reconciler（默认每 300 秒一轮）
  -> 前台监控所有子进程
```

任一子进程异常退出时 supervisor 返回失败并回收其余进程。按 `Ctrl-C` 时 Worker 先进入 draining，API、launcher、Worker 和 Reconciler 最终由 supervisor 统一回收。supervisor 模式固定设置 `RELOAD=false`，避免 Uvicorn reloader 再生成不受控子进程。

### 独立进程入口

这些命令主要用于容器拆分部署、诊断或定向验证。新手日常开发优先使用 `agent-runtime.sh start`。

| 入口 | 用途 | 常用参数 |
|---|---|---|
| `poetry run python run_app.py` | 单独启动 API | 配置来自当前 `ENVIRONMENT` |
| `poetry run python -m app.worker` | 常驻 Worker，处理 outbox、claim 和 workflow | `--worker-id`、`--poll-seconds`、`--once` |
| `poetry run python -m app.reconciler` | 常驻状态对账、lease 回收、purge 和补偿 | `--interval-seconds`、`--once` |
| `poetry run python -m app.memory_runtime_launcher` | 执行一次历史回忆录启动 outbox 和 callback 补偿 | 没有 `--help`/参数；运行即连接数据库并执行一轮 |
| `app.dispatcher.Dispatcher` | Worker 内部使用的 outbox 派发组件 | 不是独立 CLI，不要直接当进程启动 |

单轮诊断示例：

```bash
ENVIRONMENT=test poetry run python -m app.worker --once --worker-id diagnostic-worker
ENVIRONMENT=test poetry run python -m app.reconciler --once
```

这两条命令会连接 test Runtime 数据库并可能推进状态，只能在隔离测试环境使用。

### 历史脚本边界

`app/scripts/set_env.py`、`manage_db.py`、`create_database.py` 和 `init_database.py` 来自早期后端模板，仍被部分测试或兼容逻辑引用，但不是 AgentRuntime 的安全操作入口：

- 不使用 `set_env ... bootstrap` 启动 Runtime。
- 不使用 `manage_db reset`、`create_all()` 或 `init_database.py` 初始化 Runtime。
- 不对 `couple_diary_dev/test/prod` 运行 Alembic、stamp、reset 或任何 DDL。
- 建库、迁移、注册和完整启动统一使用 `agent-runtime.sh`。

## 启动后检查

development/test 默认端口：

```bash
curl -fsS http://127.0.0.1:8010/healthz
curl -fsS http://127.0.0.1:8010/readyz
curl -fsS http://127.0.0.1:8010/api/v1/runtime/health/live
curl -fsS http://127.0.0.1:8010/api/v1/runtime/health/ready
```

production 默认端口：

```bash
curl -fsS http://127.0.0.1:8011/healthz
curl -fsS http://127.0.0.1:8011/readyz
curl -fsS http://127.0.0.1:8011/api/v1/runtime/health/live
curl -fsS http://127.0.0.1:8011/api/v1/runtime/health/ready
```

预期四个请求均返回 HTTP 200。区别：

- `/healthz`：FastAPI 进程存活。
- `/readyz`：基础应用依赖就绪，当前主要检查数据库。
- `/api/v1/runtime/health/live`：Runtime 事件循环与服务存活。
- `/api/v1/runtime/health/ready`：Runtime 数据库、trusted clients、audit sink、callback dispatcher、draining 等治理条件就绪。

API 文档默认位于 `http://127.0.0.1:<PORT>/docs`，OpenAPI JSON 位于 `/api/v1/openapi.json`。

## 常见启动问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `CONFIG_FILE_EXISTS` | `.env.<env>.local` 已存在 | 直接编辑/复用；确认旧密钥废弃后才 `configure --force` |
| `INSECURE_FILE_MODE` | 私有配置可被 group/other 读取 | `chmod 600 .env.<env>.local` |
| `PLACEHOLDER_VALUE` / `MISSING_VALUE` | 必填配置未填写或仍是模板值 | 按 [ENV_CONFIG.md](ENV_CONFIG.md) 补齐对应字段 |
| `RUNTIME_DATABASE_NAME_MISMATCH` | `DB_NAME` 不是当前环境固定 Runtime 专库 | 改为 `couple_diary_agent_runtime_dev/test/prod` 对应值 |
| `RUNTIME_DATABASE_MISSING` | production 未预建库或自动建库关闭 | 让 DBA/平台预建 Runtime 专库，再执行 `prepare` |
| MySQL/Redis 连接失败 | 服务未启动、地址/端口或凭据错误 | 先用数据库/Redis 客户端确认连通，再运行 `doctor/prepare` |
| Runtime readiness 503 | trusted client、audit、callback、数据库或 draining 未就绪 | 查看 readiness 的安全状态码并修复对应配置 |
| create Run 返回 Package 不可用 | 磁盘包未注册进目标环境数据库 | 明确版本执行 `register --dry-run`，再正式 `register` |
| `model_enhancement_available=false` | 模型路由为空、Redis 不可用或治理字段不完整 | 不需要真实模型时保持模板降级；需要时按 ENV_CONFIG 配置 |
| 启动后没有执行 AgentRun | 只用了 `run.sh`，没有 Worker | 使用 `agent-runtime.sh start <env>` 或单独启动 Worker |
| 第二套环境无法启动 | development/test 默认都占用 8010 | 修改其中一套 `PORT` 与 `MEMORY_RUNTIME_BASE_URL` |

## 公共 API

公共路径位于 `API_PREFIX`（默认 `/api/v1`）下。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/runtime/health/live` | Runtime 存活探针 |
| GET | `/runtime/health/ready` | Runtime 依赖和治理 readiness |
| GET | `/runtime/capabilities` | 已验签的契约、AgentPackage 和模型能力摘要 |
| POST | `/runtime/agent-runs` | 创建 held/auto AgentRun |
| POST | `/runtime/agent-runs/{run_id}/start` | 启动 held Run |
| GET | `/runtime/agent-runs/{run_id}` | 查询 Run 摘要 |
| GET | `/runtime/agent-runs/{run_id}/steps` | 查询安全步骤摘要 |
| POST | `/runtime/agent-runs/{run_id}/retry` | 显式重试 |
| POST | `/runtime/agent-runs/{run_id}/cancel` | 取消 Run |
| POST | `/runtime/agent-runs/{run_id}/human-approval` | 最小人工确认 |
| POST | `/runtime/agent-runs/{run_id}/purge-private-data` | 请求隐私清理 |

除部署探针外，Runtime API 受 HMAC、时间戳、caller/tenant、资源可见性和授权版本约束；写操作还要求独立 `Idempotency-Key`。精确 wire contract 以 `app/contracts/` 和 `tests/fixtures/runtime-contract-v1.0.0.json` 为准。

### 环境路由门禁

`production` 注册公共 `/api/v1/runtime/*` provider，并暂时保留 `demo/diary` 工程示例。以下 Runtime-local 回忆录路由只在 development/test 注册，用于迁移审计与跨仓回归，不是生产目标架构：

- `/api/v1/memory/*`
- `/api/v1/memory-archives/*`
- `/api/v1/internal/agent-tools/memory.*`
- `/api/v1/internal/agent-callbacks/memory`

新增回忆录业务 API、Archive/Snapshot/密码或 PlaybackDocument 能力时，应在 `couple-diary-b` 实现，不继续扩展本仓历史路由。

## AgentPackage 与 MemoirAgent

AgentPackage 位于 `app/agents/<agent_id>/<version>/`。包版本和 digest 不可变；修改受管文件应发布新版本，不覆盖旧版本。调用方和部署脚本都必须显式指定版本，不会自动选择磁盘最新版。

当前保留版本：

- `1.0.0–1.0.2`：历史文本工作流版本。
- `1.0.3`：媒体节点进入发布前链路，只为 `image` 场景生成配图。
- `1.0.4`：已部署磁盘包；至少生成 3 个场景，场景数和正文长度不设上限。媒体开启时仅用场景正文逐场景尝试文生图，不读取用户照片；预算耗尽、媒体关闭或单图失败时降级为文字卡。
- `1.0.5`：M7 目标版本，已在本仓实现（通用 `bounded_loop` + 五类素材动态生成 + 网关注册 `generate_scene_batch`/`repair_coverage_gaps` 模型节点；收口轮放开旧版八条素材引用上限，workflow 固定 `generate_actions → enqueue_media_tasks → safety_review → publish_document`，媒体节点位于安全审核前且失败降级同 Scene 文本卡），尚未部署、未注册到任何目标环境。
- `1.0.6`：批次稳定性修复版本（批次候选游标：模型调用与全部校验成功后才提交素材游标，模型瞬时失败不再消耗素材；首批强制 cover、末批强制 summary 的结构硬校验；结构修复支持受信任 `required_scene_type` 指定目标卡）。Tool/Snapshot/PlaybackDocument wire 合同仍为 `1.1.0`，无数据库迁移，不新增模型 route。
- `1.0.7`：当前活跃版本（预算扩容）。循环语义与 `1.0.6` 完全一致（由 runner 按 `agent_version >= 1.0.6` 门控天然继承），仅扩预算额度：`max_model_calls` 8→12（9 个正常批次约 72 条素材 + 2 次瞬时重试 + 1 次修复空间）、`max_tokens` 100000→150000、`max_model_cost` 2.0→3.0，`max_run_seconds`/`max_steps` 不变。动机：线上 60 条素材档案需恰好 8 个正常批次，1.0.6 的 `max_model_calls=8` 零余量，任一轮瞬时失败（如 run ab6fcbfc 的 JSON_PARSE_FAILED）烧掉迭代额度后末批永远跑不到，finalize 因末批快照从未暂存而 fail closed。契约零变更，wire 合同仍为 `1.1.0`。`1.0.0`–`1.0.6` 均为不可变历史包，旧 Run 按已绑定版本 resume/retry。

`memoir_agent@1.0.5` 与通用 `bounded_loop` 的设计记录在
[`头脑风暴/docs/AgentRuntime/plans/2026-08-31-通用受控循环与Memoir动态生成设计说明.md`](头脑风暴/docs/AgentRuntime/plans/2026-08-31-通用受控循环与Memoir动态生成设计说明.md)。**2026-09-01 更新（含最小收口轮）：该能力已实现——实施轮全量 958 passed/16 skipped（含最终评审修复轮补齐的 `repair_coverage_gaps` 节点实现与 1.0.5 全图集成测试），收口轮聚焦五件套 132 passed（含评审补齐的媒体预算耗尽降级用例；命令与预期见 [VERIFICATION.md](VERIFICATION.md)「M7 `memoir_agent@1.0.5` 聚焦回归」）。改动停留在工作区，`1.0.5` 未部署、未注册到任何目标环境；本 README 与 CI 的注册/校验命令已按 M7 目标切到 `1.0.5`，线上当前实际运行的仍是 `1.0.4`，直至按部署流程完成注册。注册 `1.0.5` 前须同步服务器 env（`AGENT_PACKAGE_VERSION` 升 1.0.5、`MEMOIR_MODEL_NODE_ROUTES_JSON` 增补 `generate_scene_batch` 与 `repair_coverage_gaps` 键），否则 register 仍会注册 `1.0.4`。**

磁盘上存在 `1.0.5` 不代表目标环境已经注册。线上当前运行 `1.0.4`；注册 `1.0.5` 前先完成上述服务器 env 前置，再使用下面的命令确认并注册：

```bash
./agent-runtime.sh register development --agent-id memoir_agent --version 1.0.5 --dry-run
./agent-runtime.sh register development --agent-id memoir_agent --version 1.0.5
```

**2026-09-03 更新（1.0.6 现行状态）**：当前活跃版本为 `1.0.6`（见上方版本列表）。`docker/backend/test.env.example`、`docker/backend/production.env.example`、`configure-runtime-env.sh` 默认值与 CI 的 `AGENT_PACKAGE_VERSION` 已全部切到 `1.0.6`；capabilities 暴露 `1.0.6` 的 package digest。设计与验收记录见 [`头脑风暴/docs/AgentRuntime/云服务器性能优化/2026-09-03-MemoirAgent-1.0.6模型稳定性修复执行方案.md`](头脑风暴/docs/AgentRuntime/云服务器性能优化/2026-09-03-MemoirAgent-1.0.6模型稳定性修复执行方案.md)；上面 M7 段落中的注册命令与“线上当前运行 1.0.4”表述是 2026-09-01 的历史记录，按该日期理解。

**2026-09-04 更新（1.0.7 现行状态）**：当前活跃版本为 `1.0.7`（见上方版本列表）。1.0.6 部署后线上出现迭代额度零余量整 Run 失败（60 条素材 = 恰好 8 批 vs `max_iterations=8`，run ab6fcbfc），1.0.7 仅扩预算额度修复该问题，循环语义与 1.0.6 一致。`docker/backend/test.env.example`、`docker/backend/production.env.example`、`configure-runtime-env.sh` 默认值、CI 的 `AGENT_PACKAGE_VERSION` 与 wire 版本登记表已全部切到 `1.0.7`；capabilities 暴露 `1.0.7` 的 package digest。部署前置：服务器 env 的 `AGENT_PACKAGE_VERSION` 须升 1.0.7，先部署 Runtime 再发业务仓；上一段 1.0.6 表述按 2026-09-03 历史记录理解。

## 开发与验证

常用最小流程：

```bash
# 只跑直接相关测试
poetry run pytest -q tests/test_runtime_capabilities.py

# 全量代码门禁
poetry run pytest -q
poetry run ruff check .
poetry run mypy app
poetry run alembic heads
git diff --check
```

当前 Alembic 预期只有 `20260820_0900 (head)`。真实 PostgreSQL/Redis/Worker/Reconciler 验收使用：

```bash
./agent-runtime.sh verify
```

修改代码前先确定它属于公共 Runtime 还是保留的业务迁移证据。文档 checkbox 只有在代码、测试、迁移或可复现命令提供证据后才能更新。完整验证矩阵见 [VERIFICATION.md](VERIFICATION.md)。

## 历史与兼容边界

仓库从情侣日记后端模板演进而来，因此仍包含 `demo_*`、`diary_*`、收口在 `memoir/` 子包中的历史回忆录实现，以及旧管理脚本。它们当前用于兼容、迁移证据或回归测试：

- 不把 `demo_api.py`、`diary_api.py` 当成新增公共 Runtime 能力的样板。
- 不用 `create_database.py`、`init_database.py`、`manage_db reset` 或 `create_all()` 代替 `agent-runtime.sh prepare` 与 Alembic。
- 不在 Runtime 中继续建设用户侧回忆录业务事实；目标归属是 `couple-diary-b`。
- 不为了目录观感删除历史模型、迁移或测试；清理必须等待跨仓迁移、生产数据盘点和回滚方案完成。

`requirements.txt` 仅作历史参考。依赖、工具链和版本锁定以 `pyproject.toml` 与 `poetry.lock` 为准。
