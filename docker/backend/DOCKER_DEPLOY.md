# AgentRuntime Docker 部署契约

**状态：** 部署合同；当前仓库已提供基础 `Dockerfile`、test/production Compose、Docker CI，以及 tag 触发的腾讯云远程部署工作流（见第 9 节）。

**Last Updated:** 2026-08-26

本文定义镜像、环境、进程、依赖、探针和回滚边界。实现 Dockerfile 或编排时必须遵守本文；本文不代表 Docker 产物已经构建或已经部署。

## 1. 发布身份与环境选择

部署输入是一个唯一发布镜像引用和一个 AgentPackage 版本。当前 Action 使用含部署 tag 的服务器本地镜像；接入 TCR 后应升级为不可变 digest。镜像 tag 只负责选择运行环境，不能代替 digest，也不能代替 AgentPackage 版本。

- tag 统一使用小写环境标记，例如 `test-<git-sha>` 或 `production-<git-sha>`。
- 只包含 `test` 的 tag 选择 `test`；只包含 `production` 的 tag 选择 `production`。
- 同时包含 `test` 和 `production` 的 tag 必须拒绝，不能猜测环境。
- 两个标记都不包含的 tag（包括 `latest`）必须拒绝。
- 环境匹配先转为小写，再按 `-`、`_` 或 `.` 分隔的独立 tag 段判断；不得增加 `prod` 等未约定别名，也不得把 `contest`、`preproduction` 识别为环境。

```bash
set -eu
IMAGE_TAG="${IMAGE_TAG:?set IMAGE_TAG}"
normalized_tag=$(printf '%s' "$IMAGE_TAG" | tr '[:upper:]' '[:lower:]')
has_test=0
has_production=0
if printf '%s\n' "$normalized_tag" | grep -Eq '(^|[-_.])test([-.]|$)'; then has_test=1; fi
if printf '%s\n' "$normalized_tag" | grep -Eq '(^|[-_.])production([-.]|$)'; then has_production=1; fi
if [ "$has_test" -eq "$has_production" ]; then
  printf '%s\n' 'reject: tag must contain exactly one environment segment: test or production' >&2
  exit 2
fi
if [ "$has_test" -eq 1 ]; then ENVIRONMENT=test; else ENVIRONMENT=production; fi
printf 'environment=%s\n' "$ENVIRONMENT"
```

## 2. 镜像 tag 与 AgentPackage 版本

两者是不同的身份，发布流程必须分别记录：

| 身份 | 作用 | 来源 | 示例形式 |
|---|---|---|---|
| 镜像 tag/digest | 选择可运行的代码和依赖，并确定 test/production | 镜像仓库与发布系统 | `production-<git-sha>` / `sha256:<digest>` |
| AgentPackage version | 选择数据库中注册的 Agent 工作流包 | `app/agents/<agent_id>/<version>/` 与 `register` 参数 | `<package-version>` |

镜像 tag 不得被解析成 Package 版本。Package 必须在启动 Worker 前显式预检并注册：

```bash
./agent-runtime.sh register "$ENVIRONMENT" \
  --agent-id memoir_agent \
  --version "$AGENT_PACKAGE_VERSION" \
  --dry-run
./agent-runtime.sh register "$ENVIRONMENT" \
  --agent-id memoir_agent \
  --version "$AGENT_PACKAGE_VERSION"
```

同一 digest 可承载不同环境的发布记录，但实际部署仍须使用含有唯一环境标记的 tag，并把 tag、digest、Package version 和迁移 head 一起记录。磁盘中存在 Package 不等于目标数据库已经注册。

## 3. 镜像与进程模型

正式镜像的构建上下文是仓库根目录，预期构建命令为：

```bash
docker build --pull \
  --file docker/backend/Dockerfile \
  --tag "$IMAGE_REPOSITORY:$IMAGE_TAG" .
```

镜像合同如下：

- 运行时使用非 root 用户；镜像默认用户的 UID 不得为 `0`。
- 生产镜像不携带 `.env*`、密钥文件、测试数据库数据或 shell history。
- 不通过 `ARG`、构建日志、镜像 label 或 tag 传递 secret。
- API、Worker、launcher、Reconciler 是四个独立 workload；每个容器只运行一个长期进程。
- 只有 API 发布 HTTP 端口；Worker、launcher 和 Reconciler 不发布公网端口。
- 不在任何一个 workload 中调用 `./agent-runtime.sh start`。该命令会把四类进程放入同一 supervisor，适合本机前台启动，不适合拆分后的容器部署。

每个 workload 的命令必须保持以下语义。镜像需要提供 Python 依赖和当前仓库入口：

| Workload | 命令 | 说明 |
|---|---|---|
| API | `python run_app.py` | 提供 HTTP API 和探针 |
| Worker | `python -m app.worker --worker-id <stable-worker-id>` | 消费 outbox、claim Run 并执行 workflow |
| launcher | `python -m app.scripts.agent_runtime_cli _launcher-loop` | 周期执行 `app.memory_runtime_launcher`，默认每 5 秒一轮 |
| Reconciler | `python -m app.reconciler --interval-seconds 300` | 周期执行 lease、状态、purge 和补偿对账 |

`<stable-worker-id>` 必须由编排系统提供稳定且可观测的实例标识，不得把 secret 拼入进程参数。

### 一次性 prepare 与迁移

迁移是独立的一次性 job，必须在 API/Worker/launcher/Reconciler 滚动发布前成功完成；四类长期 workload 不得各自启动迁移，也不得把迁移塞进每个容器的启动脚本。

```bash
./agent-runtime.sh doctor "$ENVIRONMENT"
./agent-runtime.sh prepare "$ENVIRONMENT"
```

`prepare` 只允许当前环境的固定 Runtime 专库，并执行 `alembic upgrade head` 与单 head 检查。生产发布后保持数据库在 head，不执行自动 downgrade。Package 注册是独立的幂等步骤，顺序为 `doctor -> prepare -> register -> 启动四类 workload`。

仓库 Compose 已把该顺序固化为硬门禁：`prepare` 先迁移，`register` 再对明确的 `AGENT_PACKAGE_VERSION` 执行 dry-run 和幂等注册，四个长期 workload 只依赖 `register: service_completed_successfully`。迁移、Package 校验或注册任一失败，`up` 都不会启动 API/Worker/launcher/Reconciler。

## 4. 依赖隔离

### test

- 使用仅供 test 的 MySQL/兼容数据库和 Redis 实例、namespace 或 DB；不得指向 development 或 production。
- 数据库名固定为 `couple_diary_agent_runtime_test`，不使用情侣日记业务库。
- `RUNTIME_REDIS_URL` 使用 test 专用地址；本地默认 Redis DB 为 `/14`，Docker 网络内只替换 service host，不改变隔离编号。
- test 可使用临时依赖和随机凭据；验证结束必须删除容器、volume、schema 和临时 secret。
- test 运行 `DB_AUTO_CREATE=true` 仅限隔离账号和隔离 Runtime 专库，不得用于共享实例。

验证结束的强制清理命令（删除隔离 MySQL volume，防止下次测试复用脏 schema）：

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml \
  --env-file <test-env-file> down -v --remove-orphans
```

### production

- 生产 Runtime Compose 不创建 MySQL/Redis sidecar。单 CVM 阶段允许通过 `memoir-integration-production` 复用 Couple Diary production 实例；一次性 `prepare` 与 `register` 也加入该网络，才能解析 `couple-diary-mysql` 等环境内别名。拆分后改用腾讯云专用私网实例。
- 数据库固定为 `couple_diary_agent_runtime_prod`，使用独立 `runtime_prod` 账号且不得拥有 `couple_diary_prod` 或 `*.*` 权限；Runtime 不读写业务 schema。
- 单 CVM Redis 固定使用逻辑 DB `/15`，供 Runtime 流控和模型 permit 使用；上量后迁移专用 Redis。
- 生产运行账号使用最小权限，`DB_AUTO_CREATE=false`；数据库由 DBA/平台预建，首次迁移仍通过一次性 `prepare production` 执行。
- 单 CVM 默认 `DB_HOST=couple-diary-mysql`、`RUNTIME_REDIS_URL=redis://couple-diary-redis:6379/15`；拆分后在脚本中覆盖为腾讯云私网 Host/URL。

### 与业务仓的同机私有网络

test/production 分别使用 `memoir-integration-test` / `memoir-integration-production` 外部 Docker network。Runtime `api` 在各自网络内提供别名 `runtime-api`，情侣日记 backend/worker 通过 `http://runtime-api:8002` 访问；不使用 Linux 下语义不等于宿主回环的 `host.docker.internal`。容器内端口固定为 `8002`，宿主机仅回环映射为 test `127.0.0.1:18002`、production `127.0.0.1:18003`，避开服务器已有的 `8002/8003` 端口段。

字段完整性、固定库名、生产默认值和配置加载顺序以 [`ENV_CONFIG.md`](../../ENV_CONFIG.md) 为准。

## 5. Secret 注入与隐私边界

生产 secret 只能由 secret manager、Kubernetes Secret 或部署平台的受控 secret 引用在运行时注入。不得把 `.env.production.local`、密钥文件或真实凭据复制进镜像、提交 Git、写入 Dockerfile、构建参数、命令历史、进程参数或普通日志。Docker Compose 的环境变量插值不应被当作 JSON 内部展开机制；JSON 配置应注入已经展开的值。

至少包括以下受控配置类别：数据库密码、Redis 凭据、Runtime client HMAC、Snapshot Fernet、JWT 验签密钥，以及经过治理的 Provider/对象存储凭据。development/test 也必须使用各自独立的随机值；本文不展示任何真实凭据。

Runtime 的日志、trace、callback、audit、Artifact、Checkpoint、测试输出和镜像元数据只允许保存 ID、状态、错误码、计数、预算、版本和时间摘要。以下内容禁止越过隐私边界：prompt、日记或业务正文、模型原文、工具原始 payload、checkpoint 明文、签名 URL、secret、token、key、私有 URL。部署探针响应也不得返回 DSN、配置 URL、凭据或业务内容。

当前仓库的 `app/scripts/docker-entrypoint.sh` 是历史辅助脚本，不是生产 Docker 合同；它带有固定的本地启动假设，禁止用于生产部署。生产入口必须采用本文的 secret 注入、一次性 prepare 和四 workload 拆分。

## 6. 健康与 readiness 冒烟

API 容器必须先通过基础存活，再通过依赖 readiness；编排系统不应把存活失败和依赖失败混为同一探针。生产通过反向代理的 HTTPS origin 检查，test 可使用回环地址：

```bash
BASE_URL="${BASE_URL:?set BASE_URL}"
curl --fail --silent --show-error "$BASE_URL/healthz"
curl --fail --silent --show-error "$BASE_URL/readyz"
curl --fail --silent --show-error "$BASE_URL/api/v1/runtime/health/live"
curl --fail --silent --show-error "$BASE_URL/api/v1/runtime/health/ready"
```

预期：四个请求均为 HTTP 200；`/healthz` 只证明 API 存活，`/readyz` 检查基础数据库，Runtime readiness 检查数据库、trusted clients、audit sink、callback dispatcher 和 `draining=false`。Worker、launcher、Reconciler 不暴露 HTTP 端口，应由编排系统检查进程退出、重启次数、受控终态和无孤儿进程。

非 root 合同可在镜像产物生成后用以下命令验证：

```bash
docker run --rm --entrypoint id \
  "$IMAGE_REPOSITORY@$IMAGE_DIGEST" -u
```

输出必须不是 `0`。该命令不得使用带 secret 的环境文件。

## 7. 镜像身份与回滚

1. 当前 tag Action 采用服务器本地构建：`RUNTIME_IMAGE=com-agent-runtime:<deploy-tag>`，发布记录至少保存 Git commit/tag、本地 image ID、AgentPackage version 和 Alembic head。这不等于 registry digest 发布。
2. `docker-compose.production.yml` 保留 registry digest 升级路径：接入 TCR 等镜像仓库后，把 `RUNTIME_IMAGE` 切到 `repository@sha256:<digest>` 并设 `RUNTIME_PULL_POLICY=always`。未完成镜像推送、扫描和 digest 记录前，不宣称“不可变 digest 发布已实现”。
3. 记录旧/新镜像 tag、digest、AgentPackage version、数据库迁移 head 和配置版本；记录中不得包含 secret 或业务正文。
4. Compose 先执行一次性 `prepare`，再执行 `register --dry-run` 和幂等注册；两道门禁成功后启动 API、Worker、launcher、Reconciler，并完成四个 API 冒烟请求。
5. 回滚时停止新 digest 的 workload，部署已验证的旧 digest，并使用旧 Package version 做 `register --dry-run` 后再按需幂等注册；随后重新执行健康和 readiness 冒烟。
6. 回滚镜像不等于回滚数据库。禁止为了配合旧镜像自动执行 `alembic downgrade`；若旧镜像不兼容当前 schema，必须恢复兼容镜像或走已审查的前向迁移/数据库恢复方案。

示例只展示引用格式，不代表真实仓库或 digest：

```bash
OLD_IMAGE="registry.example.invalid/com-agent-runtime@sha256:<verified-old-digest>"
docker pull "$OLD_IMAGE"
# 编排系统将四类 workload 的 image 全部切换为 OLD_IMAGE。
```

## 8. 验证命令

当前仓库可立即执行的代码和进程验证：

```bash
poetry run pytest -q tests/test_agent_runtime_cli.py
poetry run pytest -q tests/test_runtime_process_harness.py
poetry run ruff check .
poetry run mypy app
poetry run alembic heads
git diff --check
```

基础 Dockerfile、test/production Compose 和 Docker CI 已加入后，两个环境发布门禁都必须执行；远程部署步骤落地时必须复用同一门禁： 

```bash
docker build --pull --file docker/backend/Dockerfile \
  --tag "$IMAGE_REPOSITORY:$IMAGE_TAG" .
docker image inspect "$IMAGE_REPOSITORY:$IMAGE_TAG" \
  --format '{{json .RepoDigests}}'
docker run --rm --entrypoint id \
  "$IMAGE_REPOSITORY@$IMAGE_DIGEST" -u
```

随后注入 test 的隔离依赖运行第 6 节四个请求；生产注入外部 Runtime-only DB/Redis、保持 `DB_AUTO_CREATE=false`，运行 `doctor production`、一次性 `prepare production`、明确版本的 `register production`，再运行同一组请求。任何命令输出都必须不含凭据、DSN、私有 URL、prompt、正文或工具 payload。

## 9. tag 触发的远程部署工作流

`.github/workflows/com-agent-runtime.yml` 提供与业务仓 `couple-diary-doc` 同模式的腾讯云服务器部署，目标服务器与业务仓为同一台（复用 `SERVER_*` SSH secrets）。

触发与门禁：

- 推送 tag 触发 `deploy` job；普通分支 push 只跑 `quality` / `docker` 校验。
- `deploy` 依赖 `quality`（Ruff + pytest）与 `docker`（三套 Compose config 校验 + 镜像构建 + 非 root 验证）全部通过。
- tag 环境段判定与第 1 节脚本一致（独立段正则，`contest`/`preproduction` 不误判），同时命中或都不命中即在服务器上拒绝。
- 服务器侧使用 `master` 恢复 trap、`merge-base` 祖先校验、`fetch --force --prune-tags`，并用 GitHub job concurrency + 服务器 `flock` 双层防止并发部署。

服务器执行序列：

1. 按 tag 段选择 env 文件与 Compose 组合：test 用 `-f docker-compose.yml -f docker-compose.test.yml`，production 用 `-f docker-compose.yml -f docker-compose.production.yml`。
2. 强制 `COMPOSE_PROJECT_NAME=com-agent-runtime-<environment>`，确保并创建 `memoir-integration-<environment>` 私有网络。
3. 同时设置 `RUNTIME_IMAGE_REPOSITORY=com-agent-runtime`、`RUNTIME_IMAGE_TAG=<deploy-tag>` 和 `RUNTIME_IMAGE=com-agent-runtime:<deploy-tag>`，只执行 `build api` 生成所有 Runtime 服务共享的唯一发布镜像。
4. `up -d --no-build` 按 `prepare -> register --dry-run -> register -> 四个长期 workload` 执行硬门禁，并保证启动阶段不会产生与发布 tag 不同的临时镜像；门禁失败时工作流自动输出 `prepare/register` 状态与最近 200 行安全日志。
5. 重试检查四个 API 探针，并确认 API/Worker/launcher/Reconciler 全部 running。工作流不再对整台服务器执行全局 `docker image prune -f`。

服务器私有 env 文件按 `docker/backend/test.env.example` / `production.env.example` 创建，必须包含 `COMPOSE_PROJECT_NAME`、`ENVIRONMENT`、`MEMOIR_INTEGRATION_NETWORK`、`RUNTIME_ENV_FILE`、`AGENT_PACKAGE_VERSION`、当前环境 DB/Redis 与安全配置。test/production 的项目名、网络名、宿主 API 端口必须不同；当前冻结宿主端口分别是 `18002` / `18003`。

腾讯云服务器的 test/production 文件可在本仓库根目录交互创建，该命令不依赖宿主机已安装 Poetry：

```bash
cd "/usr/HokageYeah/服务端系统/com-agent-runtime"
./agent-runtime.sh configure-docker test
./agent-runtime.sh configure-docker production
```

test/production 的默认目标分别为 `/usr/HokageYeah/服务端系统/env/runtime-test.env` 与 `/usr/HokageYeah/服务端系统/env/runtime-production.env`。脚本不截断已有文件：先创建时间戳 `.bak` 备份，再追加 `#########自动化<environment>创建#########` 配置块并把文件和备份设为 `0600`。Docker env 的同名变量以后出现的值为准，因此重新执行会追加一个新的有效配置块，旧块仅用于人工对照。

test 密码和密钥输入不回显，留空可由 OpenSSL 生成。production 不生成数据库/Redis sidecar，强制 `DB_AUTO_CREATE=false`；单 CVM 默认使用共享私网别名，可在提示中覆盖为腾讯云专用私网实例。production 缺失 HMAC/Fernet/JWT 时由服务器 OpenSSL 独立生成，已有目标文件则沿用最后一组值，整个过程不回显密钥。媒体开启后脚本隐藏读取 OSS/Provider 凭据并写入 Bucket/Endpoint；关闭时不要求 OSS。

Runtime 的正式 HTTPS origin 是 DNS/证书/Nginx 部署资源，不能用随机值代替。首次可在服务器会话中提前设置 `RUNTIME_PRODUCTION_HTTPS_ORIGIN=https://<runtime 正式域名>`，或在脚本提示时输入一次；脚本会把它写为 `RUNTIME_PUBLIC_HTTPS_ORIGIN` 并在后续执行时自动沿用。Couple Diary 正式 API origin 默认读取已有值，首次默认为项目前端 production 已声明的 `https://xdsz-api.hokage-yeah.online`，可用 `COUPLE_DIARY_PRODUCTION_HTTPS_ORIGIN` 覆盖。`BACKEND_CORS_ORIGINS` 默认与 Runtime origin 相同，可用 `RUNTIME_PRODUCTION_CORS_ORIGINS` 显式覆盖。

production 数据库密码首次必须与 DBA 创建的 MySQL 账号一致，后续默认沿用。执行 `ALTER USER` 轮换后，用 `./agent-runtime.sh configure-docker production --replace-db-password` 隐藏输入新值，不要把密码放在命令参数或 shell history 中。

Runtime 环境文件生成后，可继续为 `couple-diary-doc` 追加同环境的 Runtime 联动配置：

```bash
./agent-runtime.sh configure-couple-diary test
./agent-runtime.sh configure-couple-diary production
```

该命令默认读取 `/usr/HokageYeah/服务端系统/env/runtime-<environment>.env`，并将配置块追加到同目录的 `couple-diary-<environment>.env`。脚本从 Runtime 环境文件提取网络名、客户端 ID、Key ID 和 HMAC Secret，不会 `source` 环境文件，也不会在输出中打印密钥。它还会保留并补齐 `CD_DOCKER_NO_PROXY`，Compose 再将其同时注入业务 backend/worker 的 `NO_PROXY` 与 `no_proxy`。写入前会创建备份，并将目标文件及备份权限设置为 `0600`。

Snapshot AES-GCM Master Key 和回忆录访问密码 Pepper 属于 `couple-diary-doc`，不会复用 Runtime Fernet Key 或 HMAC Secret。test/production 环境缺失时均在服务器上独立生成，重复执行会沿用目标文件中最后一组有效值。

默认保持 worker 和 Package 回调门禁关闭。完成基础部署和连通性检查后，再显式激活：

```bash
./agent-runtime.sh configure-couple-diary test --activate
```

production 环境应在凭据核对和联调验收完成后再使用 `--activate`。如需覆盖默认路径，可使用 `--runtime-env /path/to/runtime.env` 和 `--output /path/to/couple-diary.env`。

需要配置的 GitHub Secrets（`SERVER_*` 与业务仓同名复用）：

| Secret | 说明 |
| --- | --- |
| `SERVER_HOST` / `SERVER_PORT` / `SERVER_USER` / `SERVER_SSH_KEY` | 与业务仓共用的腾讯云服务器 SSH 访问凭据 |
| `AGENT_RUNTIME_PROJECT_DIR` | 服务器上本仓库目录绝对路径 |
| `AGENT_RUNTIME_TEST_ENV_FILE` | 服务器测试环境 Compose 插值 env 文件绝对路径 |
| `AGENT_RUNTIME_PRODUCTION_ENV_FILE` | 服务器正式环境 Compose 插值 env 文件绝对路径 |

与业务仓的部署耦合点：业务仓 `CD_MEMORY_RUNTIME_BASE_URL=http://runtime-api:8002`，两仓的 `MEMOIR_INTEGRATION_NETWORK` 必须是同一环境网络，HMAC 凭据与本仓 `RUNTIME_TRUSTED_CLIENTS_JSON` 对称。Snapshot AES-GCM key 与 Runtime Fernet key 各自属于所在仓，禁止复用。

## 10. 腾讯云首次部署

1. CVM 安装 Git、Docker Engine、Docker Compose v2、curl、Nginx 和 `flock`；安全组只开放 SSH/80/443，不开放 Runtime/MySQL/Redis 宿主端口。
2. 使用专用部署用户 clone 两仓；在仓库外创建四份 0600 环境文件。
3. 预建 production Runtime 专库 `couple_diary_agent_runtime_prod` 及最小权限账号；单 CVM 可使用 Couple Diary MySQL 实例和 Redis `/15`，但不给 Runtime 业务 schema 权限。
4. 在两仓 GitHub 分别配置 `SERVER_*`、项目路径和 test/production env 路径 Secrets，并开启 master/tag 保护与 production environment 审批。
5. 先部署情侣日记 test 基础服务（memoir worker 保持关闭），再发布 Runtime test tag，最后开启业务 memoir worker 并完成 capabilities/held create/tool/callback 冒烟。
6. test 验收后备份 production 数据库，按相同顺序发布 production，记录 Git tag/commit、image ID、AgentPackage version 和 Alembic head。

相关文档：[`README.md`](../../README.md)、[`ENV_CONFIG.md`](../../ENV_CONFIG.md)、[`VERIFICATION.md`](../../VERIFICATION.md)。
