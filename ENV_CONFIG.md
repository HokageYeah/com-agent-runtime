# AgentRuntime 敏感与条件环境配置

本文说明 `development` / `test` / `production` 中的 Runtime 服务地址、外部观测治理、HMAC、Snapshot 加密、JWT 验签和私有媒体桶配置。完整启动和真实验收流程见 [VERIFICATION.md](VERIFICATION.md)。

## 1. 安全前提

- `development` 和 `test` 优先使用 `./agent-runtime.sh configure <environment>` 生成 HMAC、Fernet 和 JWT secret。
- `production` 不允许使用 `configure` 生成本地密钥文件；密钥和凭据应由 secret manager 或部署平台注入。
- development/test/production 必须使用三套完全独立的密钥，client HMAC、tool HMAC、JWT 和数据库密码之间也不得复用。
- 不得将密钥、凭据、签名 URL、私有 endpoint、prompt、模型原文或工具 payload 写入 Git、日志、trace、callback、审计、artifact、checkpoint、测试输出或工单。
- 本地 `.env.<environment>.local` 必须设为 `0600`；`.env.local` 优先级最高，其中的同名字段会覆盖环境专用配置。

```bash
chmod 600 .env.development.local .env.test.local .env.production.local
```

## 2. 各环境的生成方式

| 环境 | 推荐方式 | 服务端口 | 密钥处理 |
|---|---|---:|---|
| development | `./agent-runtime.sh configure development` | `8010` | 脚本自动生成三类密钥 |
| test | `./agent-runtime.sh configure test` | `8010` | 脚本自动生成三类密钥，不复用 development |
| production | secret manager / 部署平台 | `8011` | 在受控边界生成、注入和轮换 |

`configure` 会直接把生成的 secret 写入官方 Settings 字段和 JSON 注册表。`RUNTIME_CLIENT_HMAC_SECRET` / `RUNTIME_TOOL_HMAC_SECRET` 是手工 `.env` 模板中为避免重复而使用的辅助变量，不是 `Settings` 的业务字段。

如果必须手工生成本地密钥，可以使用：

```bash
# 生成一个 256-bit HMAC/JWT secret；client、tool、JWT 需分别执行一次。
openssl rand -hex 32

# 生成合法 Fernet key。
poetry run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

命令会把密钥显示在本机终端；不得开启录屏、shell 输出采集或 CI 命令回显。生产环境应使用 secret manager 自带的安全随机生成能力，不在普通终端生成。

## 3. AgentRuntime 专用数据库

AgentRuntime 与正在运行的回忆录业务库必须物理分离。启动器只允许以下固定映射：

| 环境 | AgentRuntime 专库 | 受保护、禁止迁移的业务库 | `DB_AUTO_CREATE` 默认 |
|---|---|---|---|
| development | `couple_diary_agent_runtime_dev` | `couple_diary_dev` | `true` |
| test | `couple_diary_agent_runtime_test` | `couple_diary_test` | `true` |
| production | `couple_diary_agent_runtime_prod` | `couple_diary_prod` | `false` |

```dotenv
DB_AUTO_CREATE=true
DB_NAME=couple_diary_agent_runtime_dev
```

`doctor` 在连接 MySQL 前就会检查该映射。任何业务库名或自定义库名都返回 `DB_NAME:RUNTIME_DATABASE_NAME_MISMATCH`，不执行建库、建表、stamp 或迁移。

`prepare/start` 的数据库行为：

1. 使用不带库名的 MySQL 服务器连接查询 `information_schema`；
2. 库不存在且 `DB_AUTO_CREATE=true` 时，仅创建当前环境的固定 Runtime 专库；
3. 库已存在时不重复创建；
4. 统一执行 `alembic upgrade head`，空库创建表，已有可解析历史的库升级表；
5. 未知 revision 或不可信 schema 固定拒绝，不会自动 `stamp`。

创建新库的 MySQL 账号必须拥有 `CREATE DATABASE`。如果没有权限，脚本返回 `RUNTIME_DATABASE_CREATE_FAILED`，不输出密码或 DSN。

生产首次 bootstrap 推荐流程：

1. 由 DBA 预建 `couple_diary_agent_runtime_prod` 并向运行账号授予该库的最小权限；
2. 保持 `DB_AUTO_CREATE=false`，执行 `./agent-runtime.sh prepare production`；
3. 如必须由脚本创建，仅首次临时使用具有建库权限的受控账号并设置 `DB_AUTO_CREATE=true`，完成 `prepare` 后立即切回最小权限运行账号和 `false`。

预期输出：首次缺库为 `[OK] database environment=<env> status=created`，后续启动为 `status=existing`。

## 4. `SERVICE_BASE_URL`（生产必须手动配置）

### 4.1 它的作用

`SERVICE_BASE_URL` 是 `.env.*.local` 模板中的复用变量，用于同时生成：

- `MEMORY_RUNTIME_BASE_URL`；
- `RUNTIME_BUSINESS_CONNECTORS_JSON` 中 connector 的 `base_url`；
- `RUNTIME_CALLBACK_TARGETS_JSON` 中 callback URL。

它不是 `app.core.config.Settings` 的正式字段。Python dotenv 加载 `.env.production.local` 时可以展开 `${SERVICE_BASE_URL}`；Docker Compose、Kubernetes 或云平台直接注入环境变量时，不应假设它们会在 JSON 字符串内自动展开。这类部署应直接注入展开后的 `MEMORY_RUNTIME_BASE_URL`、`RUNTIME_BUSINESS_CONNECTORS_JSON` 和 `RUNTIME_CALLBACK_TARGETS_JSON`。

### 4.2 值的要求

生产值必须：

- 使用 `https://`；
- 使用经 allowlist 的真实域名；
- 只包含 origin，不带路径、query 或 fragment；
- 不带 username、password、token 或 API key；
- DNS 只解析到受控公网地址，不得解析到 localhost、回环、内网、link-local 或其他非全局 IP。

```dotenv
# 示例只表示格式，必须替换为真实受控域名。
SERVICE_BASE_URL=https://api.example.com
MEMORY_RUNTIME_BASE_URL=${SERVICE_BASE_URL}
```

生产反向代理应将该 HTTPS origin 路由到 API 的 `8011` 端口。应用在 Docker 内仍可监听 `0.0.0.0:8011`，但 connector 的受信出站 origin 不应改成 `http://api:8011` 这类私网 service 地址。

配置后可以执行无凭据冒烟：

```bash
curl -fsS https://api.example.com/healthz
curl -fsS https://api.example.com/api/v1/runtime/health/ready
```

预期：两条命令返回 HTTP 200，TLS 证书校验成功，响应不包含配置 URL、密钥或业务内容。

## 5. 外部 exporter 治理

### 5.1 默认配置

development/test/production 都应默认关闭外部 exporter：

```dotenv
RUNTIME_EXTERNAL_EXPORTER_ENABLED=false
RUNTIME_EXTERNAL_EXPORTER_DATA_CLASSIFICATION=
RUNTIME_EXTERNAL_EXPORTER_SAMPLED_FIELDS=
RUNTIME_EXTERNAL_EXPORTER_REGION=
RUNTIME_EXTERNAL_EXPORTER_RETENTION_DAYS=0
RUNTIME_EXTERNAL_EXPORTER_AUDIT_PERMISSION=
RUNTIME_EXTERNAL_EXPORTER_PRIVACY_PURGE_SUPPORTED=false
```

关闭时不需要“生成”这四个值，保持空值即可。不要为了让配置看起来完整而填入虚构值。

### 5.2 启用时的完整配置

只有安全/法务/数据治理评审完成后才能开启。当 `RUNTIME_EXTERNAL_EXPORTER_ENABLED=true` 时，以下七项必须同时满足：

| 字段 | 填写规则 | 示例 |
|---|---|---|
| `RUNTIME_EXTERNAL_EXPORTER_DATA_CLASSIFICATION` | 代码只接受 `public` 或 `internal`；本项目通常应选 `internal` | `internal` |
| `RUNTIME_EXTERNAL_EXPORTER_SAMPLED_FIELDS` | 逗号分隔的字段白名单；当前 readiness 检查要求包含 `run_id` | `run_id,evaluation_count,fallback_count` |
| `RUNTIME_EXTERNAL_EXPORTER_REGION` | 组织的部署/驻留区域代码，必须与 exporter 账号和合同区域一致 | `cn` |
| `RUNTIME_EXTERNAL_EXPORTER_RETENTION_DAYS` | 正整数，不得为 `0` | `7` |
| `RUNTIME_EXTERNAL_EXPORTER_AUDIT_PERMISSION` | 已在 IAM/RBAC 中存在的审计权限标识；不是密码 | `observability_audit` |
| `RUNTIME_EXTERNAL_EXPORTER_PRIVACY_PURGE_SUPPORTED` | exporter 必须具备可验证的隐私删除能力 | `true` |
| `RUNTIME_EXTERNAL_EXPORTER_ENABLED` | 最后才改为 `true` | `true` |

```dotenv
RUNTIME_EXTERNAL_EXPORTER_ENABLED=true
RUNTIME_EXTERNAL_EXPORTER_DATA_CLASSIFICATION=internal
RUNTIME_EXTERNAL_EXPORTER_SAMPLED_FIELDS=run_id,evaluation_count,fallback_count
RUNTIME_EXTERNAL_EXPORTER_REGION=cn
RUNTIME_EXTERNAL_EXPORTER_RETENTION_DAYS=7
RUNTIME_EXTERNAL_EXPORTER_AUDIT_PERMISSION=observability_audit
RUNTIME_EXTERNAL_EXPORTER_PRIVACY_PURGE_SUPPORTED=true
```

固定禁止导出的字段包括 `prompt/diary/content/checkpoint/tool_payload/signed_url/secret/token/key/reasoning`。即使将它们误加入 `SAMPLED_FIELDS`，策略也会拒绝导出。不应利用改名或嵌套字段绕过该边界。

本组字段是治理开关，不包含 exporter endpoint 或凭据。真实 exporter 的连接配置必须由另一个受控部署边界提供，不得填入上述字段。

启用后检查：

```bash
curl -fsS http://127.0.0.1:8010/api/v1/runtime/health/ready
# 生产改为真实 HTTPS URL。
```

预期：HTTP 200，`external_exporter` 为 `governed`。任一治理项缺失时 readiness 应返回失败，`external_exporter` 为 `invalid`。

## 6. Runtime HMAC 密钥

### 6.1 生成

development/test 优先交给脚本：

```bash
./agent-runtime.sh configure development
./agent-runtime.sh configure test
```

手工或 secret manager 生成时，必须创建两个不同的 256-bit secret：

1. `RUNTIME_CLIENT_HMAC_SECRET`：业务服务调用 Runtime；
2. `RUNTIME_TOOL_HMAC_SECRET`：Runtime 调用 connector 和 callback。

```bash
# 执行第一次生成 client secret，第二次生成 tool secret。
openssl rand -hex 32
```

### 6.2 一致性关系

client secret 必须同时出现在：

- `RUNTIME_TRUSTED_CLIENTS_JSON[MEMORY_RUNTIME_CLIENT_ID].keys[MEMORY_RUNTIME_KEY_ID]`；
- `MEMORY_RUNTIME_SECRET`。

tool secret 必须同时出现在：

- `RUNTIME_BUSINESS_CONNECTORS_JSON` 目标的 `secret`；
- `RUNTIME_CALLBACK_TARGETS_JSON` 目标的 `secret`；
- `MEMORY_TOOL_TRUSTED_RUNTIMES_JSON[RUNTIME_ID].keys[key_id]`。

```dotenv
RUNTIME_CLIENT_HMAC_SECRET=
RUNTIME_TOOL_HMAC_SECRET=

RUNTIME_TRUSTED_CLIENTS_JSON='{"couple-diary":{"tenant_id":"couple-diary","keys":{"production-v1":"${RUNTIME_CLIENT_HMAC_SECRET}"},"agent_ids":["memoir_agent"],"business_types":["couple_memory"],"callback_target_ids":["memory_callback"],"connector_ids":["couple_diary_backend"],"data_domains":["couple_memory"],"authorization_version":1,"model_data_residency":"private"}}'
MEMORY_RUNTIME_CLIENT_ID=couple-diary
MEMORY_RUNTIME_KEY_ID=production-v1
MEMORY_RUNTIME_SECRET=${RUNTIME_CLIENT_HMAC_SECRET}

RUNTIME_BUSINESS_CONNECTORS_JSON='{"couple_diary_backend":{"enabled":true,"base_url":"${SERVICE_BASE_URL}","runtime_id":"agent-runtime-production","key_id":"production-v1","secret":"${RUNTIME_TOOL_HMAC_SECRET}"}}'
RUNTIME_CALLBACK_TARGETS_JSON='{"memory_callback":{"enabled":true,"url":"${SERVICE_BASE_URL}/api/v1/internal/agent-callbacks/memory","runtime_id":"agent-runtime-production","key_id":"production-v1","secret":"${RUNTIME_TOOL_HMAC_SECRET}"}}'
MEMORY_TOOL_TRUSTED_RUNTIMES_JSON='{"agent-runtime-production":{"keys":{"production-v1":"${RUNTIME_TOOL_HMAC_SECRET}"}}}'
```

示例同时展示密钥关系和当前 memoir Agent 所需的最小 allowlist；新增 Agent、业务类型、callback、connector 或 data domain 时必须经授权评审，不得用通配或删除 allowlist 的方式放行。

轮换时不要直接覆盖原 key ID：先为接收方增加新 key ID，再切换发送方，等待旧 lease/callback 窗口耗尽后删除旧 key。转换期间不得把新旧 secret 写入日志或审计。

## 7. Snapshot Fernet 密钥

### 7.1 生成与配置

`MEMORY_SNAPSHOT_FERNET_KEY` 是 URL-safe Base64 编码的 32-byte Fernet key，不能用普通密码或 `openssl rand -hex` 的结果代替。

```bash
poetry run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

```dotenv
MEMORY_SNAPSHOT_FERNET_KEY=
```

development/test 由 `configure` 自动生成。production 应由 secret manager 生成或导入，并向 API、Worker、Reconciler 和 launcher 注入同一个版本。

当前运行时不提供旧 Fernet key 钥匙环自动回退解密。直接替换密钥会使旧 Snapshot 无法解密；轮换前必须另行实施受控的旧数据解密/重加密迁移、备份和回滚计划。

无内容校验：

```bash
ENVIRONMENT=development poetry run python -c 'from cryptography.fernet import Fernet; from app.core.config import settings; Fernet(settings.MEMORY_SNAPSHOT_FERNET_KEY.encode()); print("FERNET_KEY_OK")'
```

预期：只输出 `FERNET_KEY_OK`，不输出 key。测试/生产将 `ENVIRONMENT` 分别改为 `test` / `production`。

## 8. 用户 JWT 验签密钥

`USER_AUTH_JWT_SECRET` 用于验证登录模块签发的 HS256 JWT，并不是 Runtime 自行生成用户 token 的独立密钥。

- 如果登录签发端已存在：必须把签发端的同一 HS256 secret 通过 secret manager 注入本服务，不能再生成一个不同的值。
- 如果是全新本地/测试环境：`configure` 会生成；手工时可使用 `openssl rand -hex 32`。
- `USER_AUTH_JWT_ISSUER` 必须与签发端 `iss` 完全一致，当前默认为 `couple-diary`。
- token 还必须使用 `HS256`，包含正整数字符串 `sub`、非空 `jti`、未过期的整数 `exp`。

```dotenv
USER_AUTH_JWT_SECRET=
USER_AUTH_JWT_ISSUER=couple-diary
```

JWT 轮换会影响已签发 token。生产轮换前必须确定旧 token TTL、过渡窗口、重新登录策略和签发端/验签端的发布顺序。当前验签器只接受一个 secret，不得假设它能自动回退到旧 key。

## 9. S3 兼容私有媒体桶

### 9.1 关闭媒体能力

如果当前不需要图片/音频签名访问，以下五项必须全部保持空值：

```dotenv
MEMORY_MEDIA_S3_ENDPOINT_URL=
MEMORY_MEDIA_S3_BUCKET=
MEMORY_MEDIA_S3_REGION=
MEMORY_MEDIA_S3_ACCESS_KEY_ID=
MEMORY_MEDIA_S3_SECRET_ACCESS_KEY=
MEMORY_MEDIA_SIGNED_URL_TTL_SECONDS=60
```

此时媒体代理不装配，媒体 API fail-closed；不会创建签名 URL，也不应因为能力关闭而访问网络。

### 9.2 启用媒体能力

以下五项必须全部填写，任意一项缺失都会拒绝应用启动：

| 字段 | 如何获取 | 要求 |
|---|---|---|
| `MEMORY_MEDIA_S3_ENDPOINT_URL` | 对象存储服务控制台的 S3 API endpoint | 生产必须 HTTPS；不得包含 username/password |
| `MEMORY_MEDIA_S3_BUCKET` | 在 S3/MinIO/COS 中预先创建的私有桶 | 3–63 位，小写字母/数字/`.`/`-`，首尾必须是字母或数字 |
| `MEMORY_MEDIA_S3_REGION` | 桶所在 region | 必须与实际桶区域一致 |
| `MEMORY_MEDIA_S3_ACCESS_KEY_ID` | 为 AgentRuntime 创建的独立 IAM/子账号 | 不使用主账号或个人长期凭据 |
| `MEMORY_MEDIA_S3_SECRET_ACCESS_KEY` | 上述 IAM/子账号的 secret | 只通过 secret manager 注入，不写入 Git/日志 |

最小权限原则：该账号只需对受控媒体前缀拥有签发 `GetObject` 访问所需的权限，不应授予公开读、桶管理、删除或全桶写权限。桶本身必须保持 private。

```dotenv
# 仅为格式示例，必须替换为实际私有桶信息。
MEMORY_MEDIA_S3_ENDPOINT_URL=https://s3.example.com
MEMORY_MEDIA_S3_BUCKET=private-memoirs
MEMORY_MEDIA_S3_REGION=cn-region-1
MEMORY_MEDIA_S3_ACCESS_KEY_ID=
MEMORY_MEDIA_S3_SECRET_ACCESS_KEY=
MEMORY_MEDIA_SIGNED_URL_TTL_SECONDS=60
```

`MEMORY_MEDIA_SIGNED_URL_TTL_SECONDS` 必须在 `1..300` 之间，默认 `60`。运行时只签发 `get_object` 短期 URL，不持久化、记录或回传 storage key/签名 URL 到不受控边界。

无凭据配置检查：

```bash
ENVIRONMENT=development poetry run python -c 'from app.core.config import settings; from app.services.memory_s3_media_proxy import MemoryS3MediaProxy; proxy = MemoryS3MediaProxy.from_settings(settings); print("MEDIA_DISABLED" if proxy is None else "MEDIA_CONFIG_OK")'
```

预期：五项全空时只输出 `MEDIA_DISABLED`；五项完整且格式合法时只输出 `MEDIA_CONFIG_OK`；半配置、非法桶名、非法 TTL 或生产 HTTP endpoint 应以固定配置错误失败。此命令不应输出 access key、secret、endpoint 或签名 URL。

## 10. 最终检查顺序

### development / test

```bash
./agent-runtime.sh doctor development
./agent-runtime.sh doctor test
```

预期：分别只输出 `[OK] configuration environment=development` 和 `[OK] configuration environment=test`。

### production

```bash
./agent-runtime.sh doctor production
./agent-runtime.sh prepare production
```

预期：

- doctor 不回显任何配置值或 secret；
- 外部 exporter 关闭时 readiness 显示 `disabled`，启用且治理完整时显示 `governed`；
- Alembic 迁移成功且只有一个 head；
- 媒体五项全空时能力关闭，全部填写时完成 S3 代理装配，半配置时拒绝启动；
- 所有响应和输出不包含 HMAC、Fernet、JWT、S3 凭据、私有 URL 或业务内容。
