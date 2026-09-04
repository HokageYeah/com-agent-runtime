# AgentRuntime 验证流程

> **2026-08-07 R1 路由门禁边界（迁移源盘点，非重做）：** 本文件涉及的回忆录业务路由（用户 `/api/v1/memory/*`、本地 `/api/v1/internal/agent-tools/memory.*`、业务回调 `/api/v1/internal/agent-callbacks/memory`、`memory_status_api`、`memory_callbacks_api` 以及 `app.memory_runtime_launcher` legacy 启动器）均判定为“仓内历史实现已完成、目标架构待迁移”——代码保留作为迁移证据，不删除、不重写。R1 已在 `app/api/api.py` 落地环境门禁：`production` 注册 `/api/v1/runtime/*` 公共 provider 并保留模板工程的 `demo/diary` 示例，但不注册上述回忆录业务路由；`development` / `test` 仍按现状注册以便审计与跨仓联调。原有验证步骤不变，R1 路由表测试 `tests/test_runtime_route_gating.py` 作为门禁回归证据。

## 安全前提

- 只在隔离 staging 环境执行；不得使用生产数据库、Redis、对象桶或密钥。
- 使用独立数据库、独立 Redis namespace、随机测试 HMAC key 和回环 mock 服务。
- 禁止在命令行、日志、截图或工单中放入 prompt、业务正文、模型原文、工具 payload、签名 URL、checkpoint 正文或密钥。

`SERVICE_BASE_URL`、外部 exporter、HMAC、Fernet、JWT 和私有媒体桶的生成与填写规则见 [AgentRuntime 环境配置说明](ENV_CONFIG.md)。本文只保留启动顺序和可观察的验收结果。

Runtime Dockerfile、基础 Compose、test/production Compose、Docker CI 和 tag 触发的远程部署工作流已进入本仓库。Compose 硬门禁顺序是 `prepare -> register --dry-run -> register -> 长期 workload`：test 默认启动 API、Worker、launcher、Reconciler 四个长期 workload；production 默认只启动 API、Worker、Reconciler 三个长期 workload，legacy launcher 挂 `legacy-launcher` profile 默认停用（应急时用 `--profile legacy-launcher` 显式开启）。test/production 使用独立 Compose project 和私有集成网络。当前远端 Action 是服务器本地 tag 构建，registry digest 是未来升级路径。

## Docker 部署契约验证

当前可执行的文档和代码门禁如下；这些命令不读取或输出生产凭据：

```bash
test -f docker/backend/DOCKER_DEPLOY.md
test -f README.md
test -f ENV_CONFIG.md
test -f VERIFICATION.md
poetry run pytest -q tests/test_agent_runtime_cli.py
poetry run pytest -q tests/test_runtime_process_harness.py
poetry run ruff check .
poetry run mypy app
poetry run alembic heads
git diff --check
```

使用已落地的 Dockerfile、基础 Compose、test/production Compose 构建对应环境镜像，并验证非 root 用户。当前服务器本地 tag 发布记录 image ID；未来接入 TCR 后固定并记录 digest。远程部署步骤必须复用同一验证：

```bash
docker build --pull --file docker/backend/Dockerfile \
  --tag "$IMAGE_REPOSITORY:$IMAGE_TAG" .
docker image inspect "$IMAGE_REPOSITORY:$IMAGE_TAG" \
  --format '{{json .RepoDigests}}'
docker run --rm --entrypoint id \
  "$IMAGE_REPOSITORY@$IMAGE_DIGEST" -u
```

其中 `IMAGE_TAG` 必须恰好包含 `test` 或 `production` 之一；同时包含两者、两者都不包含或使用 `latest` 时验证失败。test 注入隔离数据库/Redis 和随机 secret，production 注入外部 Runtime-only 数据库/Redis 并确认 `DB_AUTO_CREATE=false`。启动 API 后执行：

```bash
BASE_URL="${BASE_URL:?set BASE_URL}"
curl --fail --silent --show-error "$BASE_URL/healthz"
curl --fail --silent --show-error "$BASE_URL/readyz"
curl --fail --silent --show-error "$BASE_URL/api/v1/runtime/health/live"
curl --fail --silent --show-error "$BASE_URL/api/v1/runtime/health/ready"
```

四个请求必须返回 HTTP 200；长期 workload 必须分别运行（test 为 API/Worker/launcher/Reconciler 四类，production 默认为 API/Worker/Reconciler 三类），prepare/migrations 只能成功执行一次。所有输出不得包含 secret、DSN、私有 URL、prompt、正文、模型原文或工具 payload。

两套 Compose 的默认服务集合可用以下命令核对（`RUNTIME_IMAGE` 用占位值满足 production overlay 的 fail-closed 插值，不是真实镜像引用；命令不读取真实生产凭据）：

```bash
RUNTIME_IMAGE=com-agent-runtime:services-verify \
docker compose -f docker-compose.yml -f docker-compose.test.yml \
  --env-file docker/backend/test.env.example config --services
```

预期输出包含 `api`、`worker`、`reconciler` 和 `launcher`。

```bash
RUNTIME_IMAGE=com-agent-runtime:services-verify \
docker compose -f docker-compose.yml -f docker-compose.production.yml \
  --env-file docker/backend/production.env.example config --services
```

预期输出包含 `api`、`worker`、`reconciler`，且不包含 `launcher`。

```bash
RUNTIME_IMAGE=com-agent-runtime:services-verify \
docker compose -f docker-compose.yml -f docker-compose.production.yml \
  --env-file docker/backend/production.env.example \
  --profile legacy-launcher config --services
```

预期输出重新包含 `launcher`，证明 production 应急回滚入口有效（完整应急启动命令见 [Docker 部署契约](docker/backend/DOCKER_DEPLOY.md) 第 7 节）。

## 一键配置、启动与验收

项目根目录的 `agent-runtime.sh` 提供六个对外命令：

| 命令 | 用途 | 是否修改数据 |
|---|---|---|
| `configure development|test` | 交互生成 `.env.<env>.local` 和随机本机密钥 | 只写本机忽略文件 |
| `doctor development|test|production` | 检查必填字段、占位值、JSON 与文件权限 | 否 |
| `prepare development|test|production` | 先 doctor，再执行 `alembic upgrade head` 和单 head 检查 | 是，仅数据库迁移 |
| `register development|test|production --agent-id <id> --version <ver> [--dry-run]` | 先 doctor，再把部署目录内的 AgentPackage 幂等注册进 `agent_definitions` 表 | 是，仅写 `agent_definitions` |
| `start development|test|production` | 先 prepare，再前台托管 API、launcher、Worker、Reconciler | 是，运行正常业务流程 |
| `verify` | 运行隔离 PostgreSQL/Redis/真实 Worker harness | 只写临时容器，结束后 `down -v` |

### 1. 准备运行依赖

所有命令都在仓库根目录执行。

```bash
poetry install
poetry run python --version
poetry run alembic heads
```

预期：Python 和 Poetry 命令成功，Alembic 只输出 `20260820_0900 (head)`。本地启动需要可连接的 MySQL 服务和 Redis；脚本不会自动创建数据库账号或复用生产实例。development/test 的 Runtime 专库缺失时可由 `DB_AUTO_CREATE=true` 自动创建。

首次本地测试可以在 MySQL 客户端内创建隔离库和最小权限账号：

```sql
CREATE DATABASE couple_diary_agent_runtime_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'runtime_test'@'127.0.0.1' IDENTIFIED BY '<只在 MySQL 交互终端输入的随机密码>';
GRANT ALL PRIVILEGES ON couple_diary_agent_runtime_test.* TO 'runtime_test'@'127.0.0.1';
FLUSH PRIVILEGES;
```

不要把真实密码写入 SQL 文件、shell history 或本文档。生产环境由 DBA/部署平台预建数据库和账号，应用账号不需要建库权限。

Redis 使用独立 DB 或 namespace。连接检查只应返回 `PONG`，不要打印带凭据的 Redis URL。

### 2. 生成本地测试配置

```bash
./agent-runtime.sh configure test
./agent-runtime.sh doctor test
```

`configure` 会询问 DB user/password/host/port/name、Redis URL 和服务 base URL。密码输入不回显；脚本会生成服务 HMAC secret、Snapshot Fernet key 和用户 JWT secret，并把 `.env.test.local` 设为 `0600`。

生成文件中每个配置项都有中文注释，并标记 `[必填/手动输入]`、`[必填/自动生成]`、`[必填/自动填写]` 或 `[可选]`。手动输入项的填写规则如下：

| 交互项 | 必填 | 如何配置 |
|---|---|---|
| `DB user` | 是 | 填 MySQL 应用账号；建议为当前环境创建独立账号，只授予对应库权限，生产不使用 `root`。 |
| `DB password` | 是 | 在隐藏输入中填 MySQL 应用账号的密码；不要写入 shell history、文档或聊天。 |
| `DB host` | 是 | 应用在宿主机运行时填 `127.0.0.1`；应用在 Docker 内运行时填 Compose 中 MySQL 的 service 名，例如 `mysql`。 |
| `DB port` | 是 | 开发/测试宿主机填 `3306`；生产宿主机填 `3307`；应用在 Docker 内时一律填容器端口 `3306`。 |
| `DB name` | 是 | 只允许 `couple_diary_agent_runtime_dev`、`couple_diary_agent_runtime_test`、`couple_diary_agent_runtime_prod`；三个 `couple_diary_dev/test/prod` 业务库会被固定拒绝。 |
| `Redis URL` | 是 | development 宿主机填 `redis://127.0.0.1:6379/15`；test 宿主机填 `redis://127.0.0.1:6379/14`；production 宿主机填 `redis://127.0.0.1:6380/15`。Docker 内保留同一 DB 号，只把主机换成实际 Redis service 名。当前 `configure test` 交互提示默认仍是 `/15`，必须手动输入 `/14`；启用认证时由 secret manager 注入带认证的 URL。 |
| `Service base URL` | 是 | 开发/测试填 `http://127.0.0.1:8010`；生产填经 allowlist 的真实 HTTPS API 地址，禁止在 URL 中携带凭据。 |

client HMAC secret、Fernet key 和 JWT secret 由 `configure` 分别随机生成，无需人工编写。当前 B9/B10 冻结合同不再使用独立 tool/callback secret：同一 client HMAC secret 在业务端→Runtime 入站验签和 Runtime→业务端 connector/callback 签名中双向共用。生产环境不使用 `configure`：必须由 secret manager 注入 client HMAC secret、Fernet key、JWT secret、数据库密码与 Redis 凭据；HMAC、Fernet、JWT 与数据库密码之间不得复用。

`prepare/start` 启动数据库顺序为：先检查环境与专库名，再查询库是否存在；缺库且 `DB_AUTO_CREATE=true` 时创建，已存在时复用；最后执行 Alembic。预期分别看到 `[OK] database ... status=created` 或 `status=existing`。未知 revision、非规范库名或迁移失败会固定 fail-closed，不自动 stamp。

建库后可以使用 MySQL 客户端只读确认（`-p` 后在隐藏提示中输入密码）：

```bash
mysql -h127.0.0.1 -P3306 -uroot -p -e \
  "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME='couple_diary_agent_runtime_dev'; SELECT version_num FROM couple_diary_agent_runtime_dev.alembic_version;"
```

预期：只输出新的 `couple_diary_agent_runtime_dev` 与 `20260820_0900`。不要对 `couple_diary_dev/test/prod` 执行 `alembic upgrade`、`stamp`、`create_all`、`reset`、`DROP` 或任何 DDL。

上述密钥的手工生成命令、字段间一致性、生产注入和轮换顺序，以及 exporter/媒体的条件必填项，统一参考 [ENV_CONFIG.md](ENV_CONFIG.md)。

应用端口和 Docker 端口的最终填法：

| 环境/运行位置 | `HOST` | `PORT` | `DB_HOST:DB_PORT` | `RUNTIME_REDIS_URL` |
|---|---:|---:|---|---|
| development，应用在宿主机 | `127.0.0.1` | `8010` | `127.0.0.1:3306` | `redis://127.0.0.1:6379/15` |
| test，应用在宿主机 | `127.0.0.1` | `8010` | `127.0.0.1:3306` | `redis://127.0.0.1:6379/14` |
| 生产，应用在宿主机 | `127.0.0.1` | `8011` | `127.0.0.1:3307` | `redis://127.0.0.1:6380/15` |
| test，应用也在 Docker | `0.0.0.0` | `8010` | `mysql:3306` | `redis://redis:6379/14` |
| 生产，应用也在 Docker | `0.0.0.0` | `8011` | `mysql:3306` | `redis://redis:6379/15` |

`mysql`/`redis` 是示例 service 名，必须换成实际 Compose service 名。生产宿主机发布 `3307:3306` 和 `127.0.0.1:6380:6379`，不会改变 Docker 网络内仍使用 `3306/6379` 的规则。

如果文件已存在，命令返回 `CONFIG_FILE_EXISTS`，防止覆盖现有密钥。只有确认旧配置不再需要时才能使用：

```bash
./agent-runtime.sh configure test --force
```

预期：`doctor` 只输出 `[OK] configuration environment=test`。失败时只输出字段名和 `MISSING_VALUE/PLACEHOLDER_VALUE/INVALID_JSON/INSECURE_FILE_MODE` 等固定错误码，不回显值。文件权限错误可以用以下命令修复：

```bash
chmod 600 .env.test.local
```

脚本生成的本地配置默认使用 `MODEL_ROUTES_JSON=[]`，因此模型增强关闭，MemoirAgent 使用确定性模板降级。这条默认路径不会请求外部 Provider。

### 3. 一键启动完整进程

```bash
./agent-runtime.sh start test
```

`start` 的顺序为：

1. 执行无内容配置检查。
2. 迁移当前数据库到 Alembic head。
3. 启动 FastAPI，等待 `/healthz` 和 Runtime readiness 返回 200。
4. 每 5 秒消费一次回忆录 Runtime 启动 outbox。
5. 启动 Worker 和每 300 秒执行的 Reconciler。

脚本保持在前台。任一子进程退出时脚本返回失败，并回收其余子进程。按 `Ctrl-C` 时应观察到 API 关闭、Worker 进入 draining，且没有 traceback、semaphore 泄漏 warning 或遗留的 `run_app.py/app.worker/app.reconciler/launcher-loop` 进程。一键启动会禁用嵌套 Uvicorn reloader；需要热重载时单独使用 `./run.sh development`。

另开一个终端执行：

```bash
curl -fsS http://127.0.0.1:8010/healthz
curl -fsS http://127.0.0.1:8010/readyz
curl -fsS http://127.0.0.1:8010/api/v1/runtime/health/live
curl -fsS http://127.0.0.1:8010/api/v1/runtime/health/ready
```

预期：四条命令都以退出码 0 结束。`/readyz` 的 database 为 `ready`；Runtime readiness 中 `database/trusted_clients/audit_sink/callback_dispatcher` 可用、`draining=false`。响应不含 DSN、connector/callback URL、凭据或业务内容。

本地 `service_base_url=http://127.0.0.1:8010` 只用于 API 和进程启动冒烟。生产 ToolGateway 会拒绝 localhost、私网 IP、DNS 重绑定和重定向；需要验证完整工具/callback 闭环时，使用下一节的隔离 harness，或在 staging 配置经过 allowlist 的 HTTPS 业务地址。

### 4. 一键真实 PostgreSQL/Redis/Worker 验收

```bash
docker compose version
./agent-runtime.sh verify
```

脚本会：

- 生成只存在子进程环境中的随机 PostgreSQL 密码。
- 启动绑定 `127.0.0.1:54329` 的 PostgreSQL 17 和 `127.0.0.1:56379` 的 Redis。
- 执行 PostgreSQL 迁移、Redis permit、真实 API/Worker/Reconciler、迟到模型/工具/callback 隔离回归。
- 无论 pytest 成功还是失败，都先对 PostgreSQL 和 Redis 执行 `down -v`。

预期：完整 harness 全部通过，不出现 Docker 相关 skip。命令结束后，以下检查不应找到 harness 容器或 volume：

```bash
docker ps -a --filter name=agent-runtime-postgres-harness
docker ps -a --filter name=agent-runtime-redis-harness
docker volume ls --filter name=agent-runtime-postgres-harness
docker volume ls --filter name=agent-runtime-redis-harness
```

### 5. staging/生产配置

`configure` 不允许写入 `.env.production.local`。生产值从部署平台或 secret manager 注入，并由所有 Runtime 进程共享。

生产 `SERVICE_BASE_URL`、受治理 exporter、HMAC/Fernet/JWT、S3 兼容私有桶以及模型路由与 Provider API Key 的完整配置规则见 [AgentRuntime 环境配置说明](ENV_CONFIG.md)。

| 分组 | 必填配置 | 要求 |
|---|---|---|
| 应用 | `ENVIRONMENT/HOST/PORT/BACKEND_CORS_ORIGINS` | production 不允许通配 CORS，`DEBUG/DB_ECHO` 关闭 |
| 数据库 | `DB_DRIVER/DB_USER/DB_PASSWORD/DB_HOST/DB_PORT/DB_NAME` | 使用独立账号，先备份再迁移，不使用生产库做验收 |
| Runtime 入站 | `RUNTIME_ID/RUNTIME_TRUSTED_CLIENTS_JSON/RUNTIME_SIGNATURE_TOLERANCE_SECONDS` | 每个 client/key 独立，配置 agent/business/callback/connector/data-domain allowlist 和授权版本 |
| 工具与 callback | `RUNTIME_BUSINESS_CONNECTORS_JSON/RUNTIME_CALLBACK_TARGETS_JSON/MEMORY_TOOL_TRUSTED_RUNTIMES_JSON` | 只允许预注册 HTTPS 目标，禁止凭据进入 URL，密钥不共用 |
| 回忆录联通调用方 | 不适用 | `MEMORY_RUNTIME_BASE_URL`、`MEMORY_RUNTIME_CLIENT_ID`、`MEMORY_RUNTIME_KEY_ID`、`MEMORY_RUNTIME_SECRET` 与 `MEMORY_RUNTIME_TIMEOUT_SECONDS` 仅由 `couple-diary-b` 持有并创建出站 HMAC；AgentRuntime 仅作为已签名 `/api/v1/runtime/capabilities` 目标，继续使用本表既有 Runtime 入站认证配置 |
| 加密与登录 | `MEMORY_SNAPSHOT_FERNET_KEY/USER_AUTH_JWT_SECRET/USER_AUTH_JWT_ISSUER` | 从 secret manager 注入；轮换前先制定旧数据解密和 token 过渡方案 |
| 共享流控 | `RUNTIME_REDIS_URL` | 使用独立 namespace/DB，故障时模型调用 fail-closed |
| 模型路由 | `MODEL_ROUTES_JSON/MEMOIR_MODEL_NODE_ROUTES_JSON/MODEL_PROVIDER_API_KEYS_JSON` | 只从部署配置读取，业务请求、Package 和 prompt 不能覆盖 |
| 审计与观测 | `RUNTIME_AUDIT_SINK_CONFIGURED=true` | 外部 exporter 默认关闭；启用时必须补齐分级、区域、保留、访问审计和 purge 能力 |

生产模型路由示例只表示字段结构：

```env
MODEL_ROUTES_JSON=[{"route_id":"memoir-private-v1","provider":"trusted_gateway","model":"approved-structured-model","endpoint":"https://model-gateway.example.com/v1","rate_limit_key":"memoir-private","max_concurrency":4,"rpm_limit":60,"tpm_limit":120000,"timeout_seconds":30,"permit_ttl_seconds":35,"settle_margin_seconds":5,"price_unit":"usd_per_1k_tokens","input_price":0,"output_price":0,"route_config_version":"v1","pricing_config_version":"v1","capabilities":["structured_output","private_residency"],"data_residency":"private","max_context_tokens":32768,"max_output_tokens":4096,"enabled":true,"allowed_tenant_ids":["couple-diary"],"allowed_model_policies":["balanced","emotional_writing","strict"]}]
MEMOIR_MODEL_NODE_ROUTES_JSON={"extract_highlights":"memoir-private-v1","plan_chapters":"memoir-private-v1","generate_scenes":"memoir-private-v1","generate_scene_batch":"memoir-private-v1","repair_coverage_gaps":"memoir-private-v1"}
MODEL_PROVIDER_API_KEYS_JSON={"memoir-private-v1":"<由 secret manager 注入的 Provider Key>"}
```

替换示例 endpoint、model、限流和价格前，需要由部署管理员确认驻留、许可和成本单位。Provider 凭据不属于 route JSON；`openai_compatible` 路由的 API Key 通过 `MODEL_PROVIDER_API_KEYS_JSON`（route_id -> key）从部署 env 或 secret manager 注入请求头，不得放入业务请求、Package、prompt、route JSON 或 URL，也不写入日志。

注入配置后先执行：

```bash
./agent-runtime.sh doctor production
./agent-runtime.sh prepare production
```

预期：doctor 不回显任何值，迁移成功且 Alembic 仅有一个 head。单机前台验收可使用 `./agent-runtime.sh start production`；容器或 Kubernetes 部署应把 API、Worker、Reconciler 分为独立 workload 并使用同一权威数据库与配置版本，Docker production 默认不启动 legacy launcher（应急启用方式见 [Docker 部署契约](docker/backend/DOCKER_DEPLOY.md)）。

AgentPackage 不会由应用运行时自动选版本：手工部署时须显式执行下列命令；Docker Compose 部署则由 `register` 一次性服务对 `AGENT_PACKAGE_VERSION` 执行同样的 dry-run + 幂等注册，失败时不启动长期 workload：

```bash
./agent-runtime.sh register production --agent-id memoir_agent --version <部署包版本>
./agent-runtime.sh register production --agent-id memoir_agent --version <部署包版本> --dry-run
```

预期：doctor 先输出 `[OK] configuration ...`，随后输出磁盘包加载成功（含 package digest 与节点数）和 `[OK] agent package register ...`；库内已存在相同 digest 的同版本记录时幂等退出不重写。development/test 同样使用该命令，仅环境参数不同。

### 6. 常见失败和观察结果

| 现象 | 处理 | 修复后应观察到 |
|---|---|---|
| `PLACEHOLDER_VALUE` | 在 `.env.<env>.local` 或 secret manager 中替换该字段 | doctor 只输出 `[OK]` |
| `INSECURE_FILE_MODE` | `chmod 600 .env.<env>.local` | doctor 通过，Git 仍不跟踪该文件 |
| Alembic 连接失败 | 检查 DB host/port/账号/库是否存在 | `alembic upgrade head` 退出码 0 |
| Runtime readiness 503 | 根据 checks 修复 database/trusted client/audit/callback 配置 | `/api/v1/runtime/health/ready` 返回 200 |
| `model_enhancement_available=false` | 检查 Redis、route 治理字段和节点 route 映射 | 验签 capabilities 中出现允许的逻辑 model policy |
| Worker 退出 | 先保留受控错误码，检查 DB/Redis/connector 与 package | supervisor 回收其他进程，无孤儿进程 |
| create Run 409 提示 AgentPackage 不可用 | 用 `register` 把所需 `--version` 注册进目标环境库 | 注册后 create Run 正常受理 |

## 脚本行为定向回归

```bash
poetry run pytest -q tests/test_agent_runtime_cli.py
```

预期：配置文件权限、防覆盖、生产配置边界、无内容 doctor、进程命令、周期 launcher 和 harness 异常清理测试全部通过。

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

全局运行观测只能读取 Run 状态、受控错误码和已汇总的评测/成本/耗时计数；可以以下命令单独校验这个边界：

```bash
poetry run pytest -q tests/test_observability_service.py
```

预期：报告只包含运行状态/错误码，admission/queue/dead-letter/purge/授权/语义失败的计数，以及评测、成本、耗时指标；测试会明确断言 prompt、正文、错误原文和工具载荷不会进入报告。

流量账本的定向自动验证：

```bash
poetry run pytest -q tests/test_runtime_traffic_events.py tests/test_provider_traffic_controller.py
```

预期：`RuntimeTrafficEvent` 仅保存 event type、route ID、结果码、时间窗口和计数；SQLite 覆盖并发 UPSERT 与阈值首次告警，Redis 故障仍 fail-closed。显式配置 Docker PostgreSQL harness 后，`tests/test_runtime_postgres_harness.py` 还会用临时 schema 验证同一窗口聚合。

执行接管与优雅停止的定向自动验证：

```bash
poetry run pytest -q \
  tests/test_runtime_agent_run_service.py::test_partial_retry_accepts_only_post_publish_failed_optional_nodes \
  tests/runtime_test_workflow_executor.py::test_executor_partial_resume_retries_only_failed_optional_node \
  tests/runtime_test_workflow_executor.py::test_executor_draining_after_checkpoint_does_not_start_next_node \
  tests/runtime_test_workflow_executor.py::test_executor_refuses_revoked_package_before_starting_any_node \
  tests/runtime_test_worker_lease_fencing.py::test_queue_releases_lease_when_draining_begins_at_executor_safe_boundary \
  tests/runtime_test_worker_entry.py::test_worker_signal_requests_drain_without_raising_or_terminating_inflight_work
```

预期：全部通过。`partial` 只能把发布后失败的 optional 节点重新入队；已完成的 `publish_document` 不会再次调用。收到 `SIGTERM/SIGINT` 后 Worker 只进入 draining：当前节点在可信 lease/deadline 窗口内返回，先写受控 Artifact 与加密 checkpoint，随后不启动新模型/工具或下一节点；lease 到期后 reaper 才能以新 fencing token 接管。Package revoked、cancel、privacy、authorization 或旧 fencing 任一失效时都不得启动或写入后续节点，且测试输出不含输入、prompt、模型内容或工具载荷。

迟到副作用与 Tool 生命周期的定向自动验证：

```bash
poetry run pytest -q \
  tests/test_memoir_publish_audit.py \
  tests/test_tool_call_audit_service.py \
  tests/runtime_test_run_queue_service.py \
  tests/test_runtime_process_harness.py
```

预期：全部通过（受限环境不能绑定回环端口时 harness 用例会明确 skip）。业务 `409 IDEMPOTENCY_CONFLICT` 只能经同一稳定逻辑键的 `query_after_commit` 查询，并且返回的 `content_digest` 与本次规范化作品一致时才恢复成功；不一致仅保留 `error_code/error_type/retryable/safe_message` 等受控字段。`AgentToolCall.retention_until` 相对创建时间至少保留 30 天，记录中不含请求/响应正文。cancel/purge 在工具请求已发出后到达时，旧 Worker 仅释放匹配 fencing token 的 claimed 占用，迟到 Artifact、Checkpoint、Step、ToolCall 结果均不能恢复；随后 Reconciler 执行物理 purge。

LangGraph 静态工作流与工具边界验证：

```bash
poetry run pytest -q \
  tests/test_runtime_graph_builder.py \
  tests/runtime_test_workflow_executor.py \
  tests/test_runtime_snapshot_tool_gateway.py \
  tests/test_tool_call_audit_service.py
```

预期：冻结的 `AgentPlan` 只能被编译为线性静态 `StateGraph`；分支、动态边、重复节点和畸形节点均在执行副作用前拒绝。图状态不含 Run 输入、prompt、模型结果或工具结果。副作用 HTTP Business Tool 的 `X-Agent-Tool-Attempt` 仅从已落库的权威 `AgentToolCall.tool_attempt` 生成；只读请求不得伪造该头。它不替代稳定幂等键或 generation/authorization/fencing 校验。Native Tool 只允许固定注册表中的 JSON repair、键名摘要和敏感字段扫描，记录为 `side_effect=false` 且审计不含输入/输出正文。失败审计仅保存 `error_code/error_type/retryable/safe_message/details_visible_to_model=false`。

Task 7/8 授权拒绝审计与可信模型路由治理：

```bash
poetry run pytest -q \
  tests/test_callback_service.py \
  tests/test_runtime_snapshot_tool_gateway.py \
  tests/test_model_gateway.py \
  tests/runtime_test_worker_entry.py
```

预期：callback target 缺失、授权撤销、授权版本变化和 connector 禁用分别写入固定 reason code 的无内容 `RuntimeAuditEvent`；审计只含 Run/状态等受控摘要。模型 route 按“Runtime 紧急禁用 -> 租户/驻留 -> Agent logical policy -> 部署 route -> 显式 fallback”复核，primary 与 fallback 都不能绕过同一治理链；业务请求、Package 输入和 prompt 不能覆盖 provider、model、base URL、key 或 fallback 顺序。

Task 8 结构化输出 one-shot repair 专项：

```bash
poetry run pytest -q \
  tests/test_memoir_model_gateway.py \
  tests/test_model_gateway.py \
  -k repair
```

预期：当前为 `16 passed`。首次模型候选在本地 JSON repair、Schema 或确定性语义校验后仍无效时，只允许一次 `structured-output-repair@v1`；成功路径产生新的物理 `model_attempt`、独立 Redis permit 和 `AgentModelUsage`，并按有界 repair request 提高 token/成本预留。repair 前重新复核 cancel、purge、authorization、tenant、驻留、部署 route、旧 lease、调用预算、Redis 和 deadline；任一失效时不发送第二次 Provider 请求，repair 仍无效时直接模板降级。原始模型候选只进入短生命周期 untrusted data 槽，不进入 Store、日志、trace、callback、审计、Artifact、Checkpoint 或测试输出。

Task 6.5/7 归档、Snapshot envelope 与媒体关闭合同：

```bash
poetry run pytest -q \
  tests/test_memory_archive_snapshot.py \
  tests/test_memory_snapshot_materializer.py \
  tests/test_memory_agent_callback_state.py \
  tests/test_memory_contract_migration.py \
  tests/test_memoir_snapshot_runner.py \
  tests/test_memoir_agent_e2e.py \
  tests/test_runtime_agent_package_loader.py \
  tests/test_runtime_snapshot_tool_gateway.py
poetry run alembic heads
```

预期：全部测试通过，Alembic 只显示 `20260820_0900 (head)`。旧 `enhancement_status=not_started` 被迁为 `disabled`，未知状态和同一 `archive_id + generation_epoch` 的第二个 RunRef 被数据库拒绝；Archive 固化 partner 昵称/头像资产引用与 bound/unbound 时间，Snapshot 只保存加密的版本化白名单 envelope。发布完整作品只推进 `content_status=succeeded + published_revision`，不得改写 enhancement。`memory.enqueue_tts` 保持 `enabled=false`。对 1.0.0-1.0.2 或未装配媒体服务的运行，`enqueue_media_tasks` 不触达媒体 Provider；1.0.3-1.0.5 的具体降级与媒体契约由 `tests/test_memoir_media_channel.py` 单独验证（1.0.5：全部合法 Scene 尝试媒体，失败/关闭/预算耗尽降级同 Scene 文本卡，媒体节点位于安全审核前）。

Snapshot 版本兼容与旧 revision 迟到媒体可单独快速回归：

```bash
poetry run pytest -q tests/test_memory_archive_snapshot.py \
  -k "snapshot_service_migrates or snapshot_service_rejects or late_media"
```

预期：`4 passed`。旧的无版本 `diaries/bets` 密文负载只在读取结果中单向投影为 `1.0.0` envelope，数据库中的密文和 digest 不发生 writeback；未知未来 `schema_major` 在读取和发布共用授权入口返回固定 `MEMORY_SNAPSHOT_SCHEMA_UNSUPPORTED`。旧 document 的迟到媒体即使落库，也不会被当前 `published_revision` 的播放器查询拼入。

M7 `memoir_agent@1.0.5` 聚焦回归（2026-09-01 收口轮）：

```bash
poetry run pytest -q \
  tests/test_memoir_media_channel.py \
  tests/test_memoir_snapshot_runner.py \
  tests/test_runtime_agent_package_loader.py \
  tests/runtime_test_memoir_105_full_graph.py \
  tests/runtime_test_memoir_coverage_repair.py
poetry run ruff check .
```

预期：测试全部通过（收口轮实测 `132 passed`，含评审补齐的 `test_media_service_budget_exhausted_degrades_before_generation`）、Ruff 全绿。覆盖：第 9/17 条及更多合法安全素材引用不再被旧版八条上限截断（仅 `1.0.5` 放开；`1.0.0`–`1.0.4` 保持八条上限且历史包字节与 digest 冻结）；五类素材（`diary/completed_bet/handbook_note/matured_wish/bucket_list_completion`）进入脱敏摘要与生成循环；`1.0.5` workflow 顺序固定 `generate_actions → enqueue_media_tasks → safety_review → publish_document`，媒体节点 `optional=False` 且位于最终安全审核之前，媒体关闭、单图失败或预算耗尽只降级为同 Scene 文本卡（预算耗尽用例锚定 provider 零调用与 `MEDIA_NODE_BUDGET_EXCEEDED` 观测码），不阻塞安全审核与发布。

`memoir_agent@1.0.6` 基础设施定向回归（2026-09-03）：

```bash
poetry run pytest -q \
  tests/test_model_gateway.py \
  tests/test_sqlalchemy_db.py \
  tests/test_runtime_capabilities.py \
  tests/test_docker_deployment_contract.py
poetry run ruff check app/runtime/model_gateway.py app/db/sqlalchemy_db.py app/runtime/tool_gateway.py app/api/endpoints/capabilities_api.py tests/test_model_gateway.py tests/test_sqlalchemy_db.py tests/test_runtime_capabilities.py tests/test_docker_deployment_contract.py
```

预期：`121 passed`、Ruff 全绿（2026-09-03 实测一致）。覆盖：HttpProviderAdapter 连接后 peer 校验容忍合法 DNS/CDN 轮换——peer 命中发送前快照时不触发重解析（getaddrinfo 仅 1 次）；peer 命中「发送前 ∪ 连接后重解析」集合才放行（记 `reason=dns_rotation`）；私网/回环/保留地址不进集合比对、立即 `MODEL_PROVIDER_PEER_MISMATCH`；重解析失败 fail-closed 归一为 MISMATCH；adapter 内不自动重发 Provider POST（重试交给受预算管理的 bounded loop）。同时覆盖 `create_engine` 新增 `pool_pre_ping=True`（保留 `pool_recycle`）、Tool wire 版本表登记 `"1.0.6": "1.1.0"`（未登记会导致 `load_snapshot` 无日志瞬时失败）、capabilities 活跃版本切到 `memoir_agent@1.0.6`（依赖 1.0.6 包目录通过 `AgentPackageService.load` 校验），以及 test/production env 模板、`configure-runtime-env.sh` 默认值与 GitHub Actions 四处 `AGENT_PACKAGE_VERSION` 均为 `1.0.6` 的部署契约。

`memoir_agent@1.0.7` 预算扩容定向回归（2026-09-04）：

```bash
poetry run pytest -q \
  tests/test_runtime_agent_package_loader.py \
  tests/test_runtime_capabilities.py \
  tests/test_docker_deployment_contract.py \
  tests/test_github_workflow_template.py \
  tests/test_configure_runtime_env_script.py \
  tests/runtime_test_memoir_loop_runner.py
poetry run ruff check .
```

预期：`79 passed`、Ruff 全绿（2026-09-04 实测一致）。覆盖：1.0.7 不可变包通过 `AgentPackageService.load` 校验（图结构与 1.0.6 逐节点一致、唯一 `bounded_loop` 策略逐字段相同、预算断言 `max_model_calls=12`/`max_tokens=150000`/`max_model_cost=3.0`、digest 与全部历史版本不同）；Tool wire 版本表登记 `"1.0.7": "1.1.0"`；capabilities 活跃版本切到 `memoir_agent@1.0.7`；loop runner 1.0.7 候选游标语义继承用例（瞬时失败不消费批次素材、下一轮同批重试成功，与 1.0.6 同形）；test/production env 模板、`configure-runtime-env.sh` 默认值与 GitHub Actions 四处 `AGENT_PACKAGE_VERSION` 均为 `1.0.7` 的部署契约。周边回归（`runtime_test_memoir_106_full_graph`/`runtime_test_bounded_loop_executor`/`test_memoir_media_channel`/`runtime_test_memoir_coverage_repair`）108 passed，确认 1.0.5/1.0.6 冻结语义零改动。

迁移前先在隔离数据库备份并检查旧状态分布：

```sql
SELECT content_status, enhancement_status, COUNT(*)
FROM memory_archives
GROUP BY content_status, enhancement_status
ORDER BY content_status, enhancement_status;
```

预期：升级前除历史 `not_started` 外不应出现计划枚举之外的状态；若存在未知状态，停止升级并先清理数据，不能把未知值猜成 `disabled`。升级后重新执行查询，只应看到 `content_status` 的 `baseline/pending/running/waiting_human/succeeded/failed/cancelled` 与 `enhancement_status` 的 `disabled/pending/running/succeeded/partial/failed`。

## Redis 与延迟 Provider 本机回归

以下命令只使用回环 Docker Redis 和测试 Provider mock；不要复用开发或生产 Redis。

```bash
docker compose -f docker-compose.redis-harness.yml up -d --wait
export AGENT_RUNTIME_TEST_REDIS_URL="redis://127.0.0.1:56379/15"
poetry run pytest -q \
  tests/test_runtime_redis_harness.py \
  tests/test_runtime_delayed_provider_mock.py \
  tests/test_model_gateway.py
unset AGENT_RUNTIME_TEST_REDIS_URL
docker compose -f docker-compose.redis-harness.yml down -v
```

预期：Redis harness 不再 skip；两个 `ProviderTrafficController` 共享并发 permit 与 `Retry-After` 冷却，Redis 故障仍返回 fail-closed。延迟 Provider mock 先报告一次已收到模型请求的无正文聚合状态，cancel/purge 或 lease 失效后的响应只能被 Gateway 丢弃并结算无内容 usage，不能恢复 Artifact、Checkpoint、Step、ToolCall 或业务 revision。`down -v` 后容器与测试数据均不存在。

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

## Docker PostgreSQL 进程级验收

SQLite 仅用于 API、mock 与单进程装配测试；它不具备 reconciler 多 Session fencing 所需的锁语义。Task 12 使用 [docker-compose.postgres-harness.yml](docker-compose.postgres-harness.yml) 在 `127.0.0.1:54329` 提供独立 PostgreSQL 17，绝不使用 Homebrew 或宿主机 PostgreSQL。

1. 验证 Python 驱动与 Docker：

```bash
poetry run python -c "import psycopg; print(psycopg.__version__)"
docker compose version
```

预期：两条命令均成功；不要在终端输出、截图、日志或文档中写入测试密码。

2. 在当前 shell 交互设置一次测试密码并启动 PostgreSQL 与隔离 Redis：

```bash
read -s POSTGRES_HARNESS_PASSWORD
export POSTGRES_HARNESS_PASSWORD
docker compose -f docker-compose.postgres-harness.yml up -d --wait
docker compose -f docker-compose.redis-harness.yml up -d --wait
docker compose -f docker-compose.postgres-harness.yml ps
```

第一条命令会等待输入：由操作者输入任意仅本机测试使用的**URL 安全**随机密码（只用字母、数字、`_`、`-`）后按回车，终端不会回显字符；它不是项目预置密码，也不应写入命令历史或仓库。第二条命令仅把该值导出给当前 shell，Compose 用它创建 `test_runtime` 数据库用户。若曾以其他密码启动过该 Compose 项目，必须先执行第 4 步的 `down -v`，否则旧 volume 会保留原密码。

预期：`postgres` 状态为 `running (healthy)`；端口仅映射为 `127.0.0.1:54329`。Compose 固定使用 `test_runtime` 用户与数据库，密码不写入仓库。执行 `down -v` 后数据库 volume 被删除，下次启动可设置新密码。

3. 运行 PostgreSQL harness（含真实 API、Worker、Reconciler 与回环业务 mock 的闭环）：

```bash
export PGPASSWORD="${POSTGRES_HARNESS_PASSWORD}"
export AGENT_RUNTIME_TEST_POSTGRES_DSN="postgresql+psycopg://test_runtime@127.0.0.1:54329/test_runtime"
export AGENT_RUNTIME_TEST_REDIS_URL="redis://127.0.0.1:56379/15"
poetry run pytest -q \
  tests/test_runtime_postgres_harness.py \
  tests/test_runtime_process_harness.py \
  tests/test_runtime_redis_harness.py
```

预期：PostgreSQL 与 Redis 用例不再 skip，完整 harness 全部通过。验证范围包括旧 Memory 表真实迁移、状态/Run 代际约束、Archive 时间/用户快照、加密 Snapshot envelope、旧 Snapshot 只读迁移、未来 major 拒绝写回、旧 revision 迟到媒体隔离、schema 创建/删除、真实 `ReconcilerRunner` lease 单轮、两个独立 Session 对同一 queued Run 的竞争（仅一个 Worker 获得 attempt/fencing），以及同一临时 schema 的 `held -> bind -> start -> publish -> skipped media -> callback -> purge -> reconcile`。API 监听 socket 必须由父进程使用 `bind(("127.0.0.1", 0)) -> listen() -> pass_fds` 一次创建，并在 Uvicorn 完成 startup 后才发送 ready 事件；bootstrap 或启动失败只允许输出 `role/stage/error_type/return_code`，不得透传原始 stderr。Harness API 必须由 `api_app_factory.create_runtime_app(RuntimeDependencies)` 单入口装配；`harness_entry` 不得导入 `app.main`，factory 不得读取 `app.core.config.settings` 或真实数据库，仓库模板中 `MEMORY_SNAPSHOT_FERNET_KEY` 为空时仍必须使用显式 `TestSettings` 正常启动。真实 Worker 在 callback target 缺失或当前授权撤销时不触网，并持久化固定 reason code 的无内容审计；Redis primary 的 429 冷却不污染显式 fallback 的独立 permit 分区。Worker/Reconciler 终态仅输出 `{"event":"completed","role":"...","result_code":"completed|failed"}`，不得附带 stderr、DSN、prompt 或 payload；迟到测试应先等待该事件，不能依赖退出时序。迟到副作用回归会先让回环 mock 阻塞一次 publish，在请求已到达后并发 cancel 与 purge、再释放响应；最终 `published_revision` 仍为 `0`，且 Artifact/Checkpoint/Step/ToolCall 的私密摘要均未被旧 lease 恢复。迟到模型与迟到 one-shot repair 回归都会在 Provider 请求已发出后并发 cancel/purge、再释放响应；首次 attempt 与 repair attempt 使用不同 permit/usage，迟到 attempt 只允许无内容 `outcome_unknown/aborted_before_send` 结算，不能恢复任何 checkpoint、step、artifact、tool call 或业务 revision。测试进程只连接回环 mock；子进程配置文件只保存无凭据 loopback DSN，测试密码仅通过受限子进程环境传递；每次创建的 `agent_runtime_test_*` schema 在退出后不存在。

4. 停止并彻底删除测试数据库数据：

```bash
docker compose -f docker-compose.postgres-harness.yml down -v
docker compose -f docker-compose.redis-harness.yml down -v
unset AGENT_RUNTIME_TEST_POSTGRES_DSN AGENT_RUNTIME_TEST_REDIS_URL PGPASSWORD POSTGRES_HARNESS_PASSWORD
```

预期：容器和命名 volume 均被删除。必须在 `down -v` 之后再 `unset POSTGRES_HARNESS_PASSWORD`，因为 Compose 解析清理命令时仍需要该必填变量。

## 方案 A 回忆录 Runtime 连接级验收

当前验证证据只使用真实前端工程 `couple-diary-f`、真实业务后端 `couple-diary-b` 和本
AgentRuntime 工程。按以下顺序执行三端验证：

1. 在 `couple-diary-f` 根目录运行：

   ```bash
   node --test script/tests/memoir-runtime-connectivity-contract.test.cjs
   npm run type-check
   ```

   预期：开发/测试环境仅从“我的 -> 回忆录档案 -> Runtime 联通测试”进入；前端只使用
   `/memory/runtime-connectivity` 请求 `couple-diary-b`，不添加 `/api/v1`，不直连
   AgentRuntime。

2. 在 `couple-diary-b` 根目录运行：

   ```bash
   poetry run pytest tests/test_memory_runtime_connectivity.py -q
   ```

   预期：业务后端在 development/test 才开放该代理，负责 Runtime HMAC 和安全摘要裁剪；
   production 的后端环境门禁拒绝该请求。

3. 在本 AgentRuntime 根目录运行：

   ```bash
   poetry run pytest tests/test_runtime_capabilities.py -q
   ```

   预期：Runtime capabilities 的已验签兼容合同成立，且验证输出不含密钥或完整 Runtime
   响应。

这三步绿色只证明连接级联通与兼容合同成立，**不**代表 Agent Run、Archive、Snapshot、
Worker、Callback 或 Published Revision 已可执行；这些 B1/B2 后续能力及完整回忆录生成
闭环必须另行验证。

## 历史 uni-app 回忆录手动说明（非方案 A 验收证据）

`/Users/yuye/YeahWork/Python项目/uni-com-project-template` 是旧前端目录，仅保留以下
历史说明；不得将其路径、构建结果或测试结果作为方案 A 或当前回忆录链路的验收证据。

```bash
cd /Users/yuye/YeahWork/Python项目/uni-com-project-template
npm run dev:mp-weixin
```

预期：微信开发者工具可以打开 `uni_modules/diary/pages/memoir/index`。先设置 4～6 位数字密码，再重新输入密码解锁；凭证只存在于当前运行内存，重启应用后必须再次解锁。

逐项观察：

1. 解锁前列表不出现作品正文、摘要、私有媒体 URL 或 Runtime 内部字段。
2. 置顶、取消置顶、重试和删除均经情侣日记业务 API；删除必须显示二次确认，成功后当前播放器与轮询停止。
3. 作品只按 `published_revision` 播放；未知 schema major 与空 scenes 显示静态降级，不能执行动态 Action。
4. 连续五次输入错误密码后，解锁按钮显示十分钟倒计时并不可点击；倒计时仅保存在当前页面内存，重新进入仍以服务端冷却结果为准。
5. 无 actions 时可用上一张/下一张或在作品卡上左右滑动按场景顺序切换；静态/未知 schema 作品只展示安全场景卡，不能执行动态 Action。切后台、离开页面或到达终态后轮询停止。
6. 控制台、Pinia、Storage 与分享参数中不得出现解锁凭证、prompt、模型原文、工具载荷、签名 URL 或私有正文。
7. 在开发者工具断开网络后打开详情，应显示“回忆作品暂不可读取，请稍后重试”和“重新读取作品”；恢复网络点击重试后可重新加载，不需要重新进入页面。
8. 在另一已授权测试会话删除当前正在查看的 archive，再让当前页面重新读取详情；当前页面应返回列表，停止轮询并清空场景、Action、错误态和短期图片 URL。
9. 使用隔离测试 fixture 注入同 major 未知可选 Action 时，控制台只出现 `MEMOIR_ACTION_UNSUPPORTED`，作品继续播放其余安全 Action，控制台不得出现 Action payload。
10. 使用隔离测试 fixture 注入失效图片时显示柔和占位图并继续展示文字；仅含音频引用的场景保持静音，Network 面板不应出现该音频资产的访问请求。第一版正常业务数据媒体能力关闭，因此无测试 fixture 时以自动化媒体策略测试为验收证据。

定向前端逻辑测试可在同一工作区运行（产物仅放临时目录）：

```bash
test_dir=$(mktemp -d /private/tmp/memoir-unit-test.XXXXXX)
./node_modules/.bin/tsc --module commonjs --target es2022 --esModuleInterop --skipLibCheck --outDir "$test_dir" tests/memoir-action-runner.test.ts tests/memoir-schema.test.ts tests/memoir-polling.test.ts tests/memoir-unlock-cooldown.test.ts tests/memoir-error-recovery.test.ts tests/memoir-media-fallback.test.ts src/uni_modules/diary/memoir/hooks/memoir-action-runner.ts src/uni_modules/diary/memoir/hooks/memoir-schema.ts src/uni_modules/diary/memoir/hooks/memoir-types.ts src/uni_modules/diary/memoir/hooks/use-memoir-polling.ts src/uni_modules/diary/memoir/hooks/memoir-unlock-cooldown.ts src/uni_modules/diary/memoir/hooks/memoir-detail-recovery.ts src/uni_modules/diary/memoir/hooks/memoir-media-policy.ts
node --test "$test_dir"/tests/memoir-*.test.js
```

预期：当前为 `16 passed`。除动作白名单、默认场景切换、schema major 静态降级、终态停止轮询与密码冷却外，还覆盖同 major 未知 Action 的固定无内容告警、媒体引用校验、图片失败占位、音频零请求、详情显式重试、远端删除安全返回列表，以及页面后台停止对在途轮询响应的 fencing。

业务 API 回环集成测试（不启动 Runtime）：

```bash
test_dir=$(mktemp -d /private/tmp/memoir-loopback.XXXXXX)
./node_modules/.bin/tsc --module commonjs --target es2022 --esModuleInterop --skipLibCheck --outDir "$test_dir" tests/memoir-business-client.integration.test.ts src/uni_modules/diary/memoir/hooks/memoir-business-client.ts src/uni_modules/diary/memoir/hooks/memoir-types.ts src/uni_modules/diary/memoir/hooks/memoir-schema.ts src/uni_modules/diary/memoir/hooks/memoir-action-runner.ts
node --test "$test_dir/tests/memoir-business-client.integration.test.js"
```

预期：回环业务 fixture 依次验证 baseline、生成状态轮询、`published_revision` 详情与场景播放；所有请求路径以 `/api/v1/memory/` 开头，不包含 Runtime，且不记录或持久化凭证、私有 URL、prompt 或工具载荷。

前端工程类型治理已完成：`vue-tsc`、Vue 运行时声明和编译期 WXS 文件范围已对齐；`npm run type-check` 与 `npm run type-check:diary` 都是发布门禁。运行：

```bash
npm run type-check:diary
npm run type-check
```

预期：两条命令均以退出码 0 结束，且不输出 TypeScript 错误。上述定向测试与类型检查共同构成 Task 11 当前可重复的自动门禁；真实小程序交互仍按本节的手动步骤验收。
