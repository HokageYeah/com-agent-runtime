# Production 默认停用 launcher 的部署调整说明

> 日期：2026-09-02  
> 目标：只让 production 默认不启动 legacy launcher；test、development 和本地 harness 保持现状。  
> 原则：不重构 launcher，不新增调度系统，只调整 Compose overlay、GitHub Actions、部署合同和定向测试。

## 1. 最终环境行为

| 环境 | 默认是否启动 launcher | 原因 |
|---|---:|---|
| production Docker | 否 | 当前回忆录由 `couple-diary-doc` 的 `run_memory_runtime_worker` 发起；Runtime 内的 `app.memory_runtime_launcher` 是历史业务兼容入口，继续空轮询浪费 CPU |
| test Docker | 是 | 保留历史链路审计和现有联调能力，本次不扩大回归范围 |
| development 本地 | 是 | development 使用 `agent-runtime.sh start development` 的本地 supervisor，不属于本次 GitHub Actions 生产部署调整 |
| PostgreSQL harness | 不涉及 | 文件只启动隔离 PostgreSQL，不包含 Runtime launcher |
| Redis harness | 不涉及 | 文件只启动隔离 Redis，不包含 Runtime launcher |

production 仍保留 API、Runtime Worker 和 Reconciler。这里停用的是历史业务 launcher，不是执行 AgentRun 的 Runtime Worker。

## 2. 五个 Compose 文件如何处理

### 2.1 `docker-compose.yml`：不改

基础 Compose 同时服务 test 和 production。若直接从这里删除 launcher，test 也会丢失现有历史联调能力；若在基础文件上加 profile，又需要 test 和其他调用方额外传 `--profile`，改动面更大。

因此基础文件继续保留 launcher 的完整定义和 `register` 门禁依赖。

### 2.2 `docker-compose.production.yml`：只增加一个 profile

这是唯一需要功能性修改的 Compose 文件。在现有 `launcher` 覆盖项中增加：

```yaml
  launcher:
    profiles:
      - legacy-launcher
    image: ${RUNTIME_IMAGE:?set RUNTIME_IMAGE to a server-built tag or repository@sha256:<digest>}
    pull_policy: ${RUNTIME_PULL_POLICY:-missing}
    environment:
      DB_AUTO_CREATE: "false"
```

原因：Compose 的 profile 服务不会被普通 `docker compose up -d` 启动。GitHub Actions 当前 production 命令不用增加任何参数，合并 production overlay 后就会自然只启动 API、Worker 和 Reconciler。

profile 保留了快速回滚入口：

```bash
docker compose \
  -p com-agent-runtime-production \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  --env-file "/usr/HokageYeah/服务端系统/env/runtime-production.env" \
  --profile legacy-launcher \
  up -d --no-deps launcher
```

### 2.3 `docker-compose.test.yml`：不改功能

test overlay 不给 launcher 增加 profile，普通 test 部署仍会启动 launcher。可选地把“四个长期 workload”的注释改成“test 的四个长期 workload”，避免被误解为 production 也必须运行四个；除此之外不改服务定义。

### 2.4 `docker-compose.postgres-harness.yml`：不改

它只提供临时 PostgreSQL 服务，Runtime 测试进程由 pytest harness 自己管理，与 production Compose 的 launcher 无关。

### 2.5 `docker-compose.redis-harness.yml`：不改

它只提供临时 Redis 服务，不包含 launcher，也不参与 GitHub Actions 的腾讯云部署服务集合。

## 3. GitHub Actions 的具体调整

修改 `.github/workflows/com-agent-runtime.yml`，只处理以下三点。

### 3.1 Docker 配置门禁增加服务集合断言

保留现有 test/production `config --quiet`。在此基础上增加：

- test 默认 `config --services` 必须包含 `launcher`。
- production 默认 `config --services` 必须包含 `api/worker/reconciler`，且不能包含 `launcher`。
- production 加 `--profile legacy-launcher` 后必须重新包含 `launcher`，证明回滚入口有效。

不要只验证 YAML 能解析；这三个断言能防止以后误把 launcher 重新放回 production 默认服务集合。

### 3.2 production 更新前清理旧 launcher 容器

profile 只影响以后是否创建服务，不一定会删除服务器上旧版本已经运行的容器。因此在构建完成、执行总 `up` 之前，增加仅 production 执行的迁移清理：

```bash
if [ "${APP_ENV}" = "production" ]; then
  echo "移除上一版本遗留的 production legacy launcher 容器..."
  docker compose ${COMPOSE_FILES} \
    --env-file "${ENV_FILE}" \
    rm --stop --force launcher
fi
```

这条命令只移除 launcher 容器，不删除 `runtime_logs` 命名卷，不动 API、Worker、Reconciler、数据库或 Redis。命令应保持失败即中止部署，不能追加 `|| true`，否则旧 launcher 删除失败时 Action 仍可能假成功。

### 3.3 部署后的运行服务断言按环境区分

公共断言仍要求：

```text
api
worker
reconciler
```

然后按环境判断：

- test：`launcher` 必须 running。
- production：`launcher` 不得出现在 running services 中。

同时把 Action 输出中的“四个长期 workload”改成环境准确的描述，例如：

```text
Runtime production 三个长期 workload 与四个 API 探针全部通过
Runtime test 四个长期 workload 与四个 API 探针全部通过
```

`prepare` 和 `register` 仍是一次性门禁，不计入长期 workload；四个 HTTP 探针全部属于 API，不受 launcher 停用影响。

## 4. 需要同步的测试和文档

### 自动化测试

调整 `tests/test_docker_deployment_contract.py`：

- 断言 production overlay 的 launcher profile 为 `legacy-launcher`。
- 断言工作流包含 production 旧 launcher 的 `rm --stop --force launcher`。
- 原来无条件要求 workflow 检查 launcher running 的断言，改成 test 必须存在、production 必须排除的环境分支断言。
- 保留基础 Compose 中 launcher 依赖 `register` 的测试，因为 test 和显式 legacy profile 仍会使用它。

`tests/test_agent_runtime_cli.py` 不需要改。它验证的是本地 `agent-runtime.sh start` supervisor，development/test 的当前行为仍然保留。

### 有效部署文档

同步修改以下文件中“所有环境都必须有四个长期 workload”的旧表述：

- `README.md`
- `ENV_CONFIG.md`
- `VERIFICATION.md`
- `docker/backend/DOCKER_DEPLOY.md`

统一写成：test/development 可保留 legacy launcher；production 默认只运行 API、Worker、Reconciler，launcher 仅通过 `legacy-launcher` profile 应急启用。

不删除 launcher 的历史实现、模型或迁移，也不修改公共 Runtime API、数据库结构、HMAC、Worker、Reconciler 或 Couple Diary 工程。

## 5. 发布与验收顺序

1. 在分支上完成上述最小修改并通过定向测试。
2. 先发布新的 test tag，确认 test 仍有 launcher，API/Worker/Reconciler 正常。
3. 再发布 production tag。Action 应先删除旧 launcher，再以默认 profile 启动三个长期 workload。
4. 服务器确认没有 launcher：

   ```bash
   docker compose \
     -p com-agent-runtime-production \
     -f docker-compose.yml \
     -f docker-compose.production.yml \
     --env-file "/usr/HokageYeah/服务端系统/env/runtime-production.env" \
     ps -a

   ps -ef | grep '[a]pp.memory_runtime_launcher'
   ```

5. 观察 15～30 分钟 CPU/load，并创建一份真实回忆录完成端到端验收。
6. 若回忆录异常，使用 `--profile legacy-launcher up -d --no-deps launcher` 回滚，然后保留日志继续定位。

## 6. 明确不做的事情

- 不把 launcher 改成长驻循环。
- 不新增轮询间隔配置、消息队列或定时平台。
- 不修改 `agent-runtime.sh start` 和 `app/scripts/agent_runtime_cli.py`。
- 不改 test、development 的默认行为。
- 不改 PostgreSQL/Redis harness。
- 不删除 legacy launcher 代码或历史表。
- 不顺手清理其他 Compose、脚本或用户现有改动。

## 7. 给 Claude Code / Codex 的实施提示词

```text
请在仓库 `/Users/yuye/YeahWork/AIAgent项目/com-agent-runtime` 中完成一次最小部署优化并提交 Git commit。

先读取并遵守：
- `AGENTS.md`
- `.codex/rules/AI通用编码与协作规范.mdc`
- `.codex/skills/agent-runtime-session/SKILL.md`
- `.codex/skills/memoir-runtime-integration/SKILL.md`
- `头脑风暴/docs/AgentRuntime/云服务器性能优化/2026-09-01-Runtime-launcher-CPU优化方案.md`
- `头脑风暴/docs/AgentRuntime/云服务器性能优化/2026-09-02-production默认停用launcher部署调整说明.md`

按阶段使用当前环境可用的实际技能，不伪造命令：
1. `adaptive-workflow`：确认这是局部、可逆的部署调整。
2. `agent-runtime-session`：装载 Runtime 部署和安全边界。
3. `memoir-runtime-integration`：确认停用的只是 legacy launcher，不是 Runtime Worker。
4. `code-review`：所有 Agent 交付后、commit 前做最终只读审查。

目标：
- production Docker/GitHub Actions 默认不启动 legacy `launcher`。
- production 继续正常启动 `api`、`worker`、`reconciler`，并保留 prepare/register 门禁和四个 API 探针。
- test 默认仍启动 launcher。
- development、本地 supervisor、PostgreSQL harness、Redis harness 保持现状。
- 只做完成目标所需的最少改动，不重构 launcher，不增加新调度系统。

必须先执行 Runtime 仓库的 `git status --short`。工作区可能已有用户改动；不得 reset、checkout、覆盖、格式化或提交与本任务无关的文件。

必须使用多 Agent 并明确文件所有权。主 Agent 先检查工作区，再并行分配下列三个任务：

- Agent A（Compose 所有者）：只负责 `docker-compose.production.yml`，并对 `docker-compose.yml`、`docker-compose.test.yml`、PostgreSQL/Redis harness 做只读影响检查。不修改 GitHub Actions、测试和文档。
- Agent B（CI/合同测试所有者）：只负责 `.github/workflows/com-agent-runtime.yml` 和 `tests/test_docker_deployment_contract.py`，补充 production/test/profile 服务集合合同与旧 launcher 容器清理验证。不修改 Compose 和有效文档。
- Agent C（文档与复核所有者）：只负责 `README.md`、`ENV_CONFIG.md`、`VERIFICATION.md`、`docker/backend/DOCKER_DEPLOY.md` 的现行部署说明；完成后对 Agent A/B 的 diff 做只读复核，报告是否超出本方案，不直接改写 A/B 所有文件。
- 主 Agent：负责维护任务依赖、向各 Agent 通知其他人也在同一工作区工作、检查交叉 diff、处理整合问题、执行完整验证和创建唯一 commit。不得覆盖用户或其他 Agent 的改动。

所有子 Agent 都必须知道自己不是唯一开发者：不 reset、不 checkout、不回退他人改动、不提交 Git。发现所有权冲突时立即停止并交由主 Agent 处理。主 Agent 必须等待三个 Agent 全部返回后再进行最终验证，不得因为其中一个先完成就提前提交。

具体修改：
1. `docker-compose.production.yml`
   - 只给 `services.launcher` 增加 `profiles: [legacy-launcher]`。
   - 保留现有 image、pull_policy、DB_AUTO_CREATE 和基础 Compose 定义。
2. `docker-compose.yml`
   - 不改功能；launcher 继续留在基础定义中。
3. `docker-compose.test.yml`
   - 不改服务行为；test 默认仍启动 launcher。最多只修正相关注释。
4. `docker-compose.postgres-harness.yml`、`docker-compose.redis-harness.yml`
   - 不修改。
5. `.github/workflows/com-agent-runtime.yml`
   - 保留现有 Compose config 校验，并增加服务集合断言：test 默认包含 launcher；production 默认不包含 launcher；production 使用 `--profile legacy-launcher` 时包含 launcher。
   - production 执行总 `up -d --no-build` 前，运行 `docker compose ${COMPOSE_FILES} --env-file "${ENV_FILE}" rm --stop --force launcher`，清理上一版本遗留容器。仅 production 执行，不加 `|| true`。
   - 部署后公共检查 api/worker/reconciler running；test 额外要求 launcher running；production 明确拒绝 launcher running。
   - 修正“四个长期 workload”等不再准确的日志文字。
6. `tests/test_docker_deployment_contract.py`
   - 增加 production launcher profile、旧容器清理和环境差异化服务检查的静态合同测试。
   - 保留基础 launcher 的 register 门禁断言。
   - 不修改 `tests/test_agent_runtime_cli.py` 的 supervisor 行为。
7. 同步更新 `README.md`、`ENV_CONFIG.md`、`VERIFICATION.md`、`docker/backend/DOCKER_DEPLOY.md`：production 默认只有 API/Worker/Reconciler；test/development 保留 legacy launcher；应急时用 `legacy-launcher` profile。

实现时特别注意：
- 不删除 launcher 代码、旧模型或迁移。
- 不修改 Runtime API、数据库、密钥、网络别名或 Couple Diary 工程。
- 不把 production 的 launcher 仅从运行检查中删掉；必须同时通过 profile 排除默认启动，并清理服务器旧容器。
- 不使用 `docker compose down`，避免影响 API/Worker/Reconciler。

至少执行并报告：
- `poetry run pytest -q tests/test_docker_deployment_contract.py tests/test_agent_runtime_cli.py`
- `poetry run ruff check tests/test_docker_deployment_contract.py`
- test Compose：`config --quiet`，并确认默认 services 包含 launcher。
- production Compose：`config --quiet`，确认默认 services 不包含 launcher；增加 `--profile legacy-launcher` 后包含 launcher。
- `git diff --check`

Compose 校验只使用仓库示例/占位配置，不读取或输出真实生产密钥。若本机 Docker 不可用，明确写出未执行原因，不得伪造通过结果。

验证通过后：
- 主 Agent 先汇总 Agent A/B/C 的实际改动、测试证据和复核结论，确认文件所有权没有交叉。
- 检查 `git diff`，只 stage 本任务文件以及本次部署调整说明文档。
- 创建 commit，建议消息：`perf(deploy): disable legacy launcher in production`
- 不 push、不创建 tag、不触发真实部署。
- 最终列出多 Agent 所有权与交付摘要、commit hash、改动文件、实际测试结果、未执行验证和生产发布/回滚命令。
```
