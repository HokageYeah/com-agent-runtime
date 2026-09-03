# MemoirAgent 1.0.6 模型稳定性修复执行方案

> 日期：2026-09-03  
> 前置：先完成 [2026-09-02-production默认停用launcher部署调整说明.md](2026-09-02-production默认停用launcher部署调整说明.md) 的修改、测试和 production 部署验收。  
> 目标：保持 production 不启动 legacy launcher，通过最小的 Runtime + Couple Diary 改动，降低回忆录因模型瞬时输出异常而失败的概率。

## 1. 两次线上运行结论

停用 launcher 是正确的，也不是这次失败的原因。两次 Run 都由 `couple-diary-doc` 的 `run_memory_runtime_worker` 正常发起，并由 Runtime Worker 领取执行。

### 1.1 第一次：失败

Run ID：`4c151dff-57ad-4954-bcc0-50d1d2d676e3`

| 时间 | 证据 | 影响 |
| --- | --- | --- |
| 09:07:14～09:07:16 | `none -> held -> queued -> claimed` | API、Outbox、Worker 派发都正常 |
| 09:07:18 | 加载 22 条素材，循环启动 | Snapshot/Business Tool 正常 |
| 09:07:27 | 第 1 批 8 条素材生成 5 张 Scene | 正常 |
| 09:07:29 | 第 2 批 `JSON_PARSE_FAILED` | Provider 虽返回内容，但不是可解析的 JSON |
| 09:07:36 | 第 3 批 `MODEL_PROVIDER_PEER_MISMATCH` | 连接到的真实公网 IP 不在发送前那次 DNS 快照中，本轮 fail-closed |
| 09:07:40 | 结构修复返回 2 张卡，但没有 `summary` | `LOOP_SUMMARY_MISSING -> LOOP_BODY_FAILED` |
| 09:07:44 之后 | `run_failed` callback 成功投递 | 失败状态已正常回传业务后端 |

第一次失败是三个模型侧问题叠加，不是部署或 launcher 问题：

1. 一批模型输出无法解析为 JSON。
2. 一批遇到 DNS/CDN 地址轮换与当前安全校验之间的瞬时不一致。
3. 最后一次结构修复仍没有生成必需的 `summary` 卡。

当前 1.0.5 还有一个放大问题的实现细节：素材游标在调模型之前已经前移。因此第 2、3 批即使失败，那 14 条素材也被当作已消费；收尾时只剩第 1 批的 5 张 Scene。

### 1.2 第二次：成功

Run ID：`255132b2-f561-4f55-a359-970aabe028b4`

- 同样加载 22 条素材。
- 3 批分别消费 `8 / 8 / 6` 条素材，生成 `7 / 4 / 3` 张 Scene。
- 最终 `scene_count=14`、`covered=22`，且末批正常包含 `summary`。
- 14 张图中有 1 张火山视觉返回 `50511`，按既定设计降级为文字卡；其余 13 张正常上传 OSS。
- 安全审核通过，`memory.publish_playback_document` 成功，Run 以 `succeeded` 终结。

这证明网络、HMAC、Snapshot、Runtime Worker、媒体、OSS、发布 Tool 和 callback 整体都已联通。问题集中在模型批次的瞬时稳定性和 1.0.5 的失败处理方式。

## 2. 1.0.6 的最小设计

### 2.1 必须保持的边界

- 不修改已发布的 `memoir_agent@1.0.5` 包内容。
- 新建不可变 `memoir_agent@1.0.6`，`contract_version` 仍为 `1.0.0`。
- Tool/Snapshot/PlaybackDocument wire 合同仍为 `1.1.0`，不加数据库迁移。
- 不新增模型 route_id，继续复用 `generate_scene_batch -> deepseek-chat` 部署映射。
- 不修改前端，不修改 Runtime API、Business Tool 或 callback 字段。
- production 仍默认不启动 launcher；API、Worker、Reconciler 职责不变。

### 2.2 批次游标改为“成功后提交”

`run_loop_iteration()` 不得在模型调用前永久前移 `_loop_cursor`。

改为：

1. 用局部游标计算当前候选批次和 `next_cursor`。
2. 模型调用成功且输出通过 JSON、schema、source ref 和结构校验后，才把 `_loop_cursor` 提交到 `next_cursor`。
3. `JSON_PARSE_FAILED`、Provider 不可用、peer mismatch 或结构不合法时，保留原游标；下一轮在剩余预算内重试同一批。
4. 重试仍由 `max_model_calls/max_tokens/max_model_cost/max_run_seconds` 硬上限控制，不加无界重试。
5. 日志只记录 `run_id/iteration/batch_size/reason_code/retry_pending`，不记录素材或模型原文。

这是 1.0.6 的核心修复：模型瞬时失败不再等于业务素材已经被消费。

### 2.3 首批/末批结构改为硬校验

`_parse_batch_output()` 当前只检查“如果出现 cover/summary，位置必须正确”，没有检查“首批/末批必须存在目标卡”。

1. `is_first_batch=true` 时，必须有且只有一张 `cover`，并位于第一张。
2. `is_final_batch=true` 时，必须有且只有一张 `summary`，并位于最后一张。
3. 目标卡缺失时，本批不提交 Scene 和游标，在预算内重试。
4. 新行为必须按 `agent_version >= 1.0.6` 分支，不改写 1.0.5 历史 Run 的恢复语义。

### 2.4 结构修复明确指定目标卡

不增加新模型路由和新 workflow 节点，继续复用 `generate_scene_batch`。

- 1.0.6 的结构修复请求增加受信任控制字段 `required_scene_type=cover|summary`。
- 1.0.6 的 `scene-batch-generate` prompt 明确要求：存在该字段时只返回一张指定类型的卡，不生成多余中间 Scene。
- Runner 仍对 scene type、位置、source refs 和正文执行同样的安全校验。
- 修复输出仍不合法时继续 fail-closed，不用确定性模板编造回忆录内容。

### 2.5 降低合法 DNS/CDN 轮换的误拒绝

不能删除 peer 校验，也不能因为线上误拒绝就对任意 IP 放行。

`HttpProviderAdapter` 采用下列最小调整：

1. 发送前保留现有 DNS 解析和非公网 IP 拒绝。
2. 连接后获取真实 socket peer IP，先确认它本身是 global public IP；私网、回环、保留地址继续立即拒绝。
3. peer 不在发送前快照中时，立即对同一受信任 hostname 再解析一次。
4. 只有 peer 出现在“发送前集合 ∪ 连接后集合”中才接受；否则仍返回 `MODEL_PROVIDER_PEER_MISMATCH`。
5. 不在 HTTP adapter 内部自动重发 Provider POST；重试交给受预算管理的 bounded loop，避免绕过计费和 usage 记录。

这个方案保留 DNS rebinding/SSRF 安全边界，同时容忍合法 Provider 在两次 DNS 查询之间切换公网节点。

### 2.6 连接池顺手收口

上一次生产日志曾出现 `MySQL Connection not available`，虽然业务立即重试后成功，但 Runtime API 主引擎尚未配置 `pool_pre_ping`。

- 在 Runtime `create_engine()` 增加 `pool_pre_ping=True`。
- 保留现有 `pool_recycle`，不改数据库账号、库名和 Compose 网络。
- 本项是同版本的独立低风险加固，不是第一次 Run 最终失败的原因。

## 3. 需要修改的项目和文件

### 3.1 `com-agent-runtime`

#### AgentPackage

- 新增完整目录 `app/agents/memoir_agent/1.0.6/`，以 1.0.5 为基线复制后只修改 1.0.6 所需内容。
- `agent.yaml`：版本改为 1.0.6；建议 `max_model_calls` 从 6 调到 8，为最多 5 个正常批次保留 2 次瞬时重试和 1 次修复空间，成本仍受 `max_tokens/max_model_cost/max_run_seconds` 限制。
- `workflow.graph.py`：图结构不增节点，只更新 1.0.6 的批次重试语义注释。
- `prompts/scene-batch-generate.v1.md`：补充 `required_scene_type` 和首/末批目标卡的强约束。
- `prompts/manifest.yaml`、schema、callbacks、guardrails、tools manifest、UI trace 保持自包含；没有合同变化时不增字段。

#### Runtime 实现

- `app/agents/memoir_agent/runner.py`
  - 1.0.6 分支使用候选游标，只在批次校验成功后提交。
  - 首批强制 cover，末批强制 summary。
  - 结构修复传入 `required_scene_type`。
  - 1.0.5 及更早版本行为保持不变。
- `app/runtime/model_gateway.py`
  - peer 必须为公网 IP。
  - 不命中发送前 DNS 快照时只补一次连接后重解析。
  - 仍然不匹配时 fail-closed。
- `app/db/sqlalchemy_db.py`：增加 `pool_pre_ping=True`。
- `app/runtime/tool_gateway.py`：增加 `"1.0.6": "1.1.0"` 映射。
- `app/api/endpoints/capabilities_api.py`：当前活跃版本改为 1.0.6，package digest 加载 1.0.6。
- `app/agents/memoir_agent/model_gateway.py`：原则上不增 route；只在实现 `required_scene_type` 的受信任 prompt 选择确有必要时做最小调整。

#### 版本和部署配置

- `docker/backend/configure-runtime-env.sh`：默认 AgentPackage 版本改为 1.0.6，仍允许用户显式输入其他合法版本。
- `docker/backend/test.env.example`、`docker/backend/production.env.example`：`AGENT_PACKAGE_VERSION=1.0.6`。
- `.github/workflows/com-agent-runtime.yml`：所有当前活跃包的 `AGENT_PACKAGE_VERSION` 改为 1.0.6。
- `docker-compose.yml`、`docker-compose.test.yml`、`docker-compose.production.yml`：不为 1.0.6 改服务结构，继续从 env 读取 `AGENT_PACKAGE_VERSION`。
- `docker-compose.postgres-harness.yml`、`docker-compose.redis-harness.yml`：不改。
- 服务器 `runtime-test.env`、`runtime-production.env` 部署时手工或通过配置脚本更新为 `AGENT_PACKAGE_VERSION=1.0.6`；不输出或更换现有密钥。

#### Runtime 测试

- `tests/runtime_test_memoir_loop_runner.py`
  - JSON 解析失败后下轮重试同批素材。
  - Provider 不可用后游标不前移。
  - 末批缺 summary 不提交，重试后可成功。
  - `required_scene_type=summary` 修复只接收一张 summary。
  - 预算耗尽仍然有界并 fail-closed/partial 收敛。
  - 1.0.5 旧行为不变。
- `tests/test_model_gateway.py`
  - peer 命中发送前 DNS 集合。
  - peer 只命中连接后新公网集合。
  - peer 不在两次集合。
  - peer 是私网/回环地址。
  - 重解析失败继续 fail-closed。
- `tests/test_sqlalchemy_db.py`：断言 `pool_pre_ping=True`。
- `tests/test_runtime_agent_package_loader.py`：加载 1.0.6，校验新 digest 与 1.0.0～1.0.5 全部不同，并保持所有历史包 digest 不变。
- `tests/runtime_test_memoir_105_full_graph.py`：保留 1.0.5 回归；新增或复制为 1.0.6 全图测试，不把历史测试直接改名覆盖。
- `tests/runtime_test_bounded_loop_executor.py`、`tests/runtime_test_memoir_coverage_repair.py`、`tests/test_memoir_media_channel.py`：补 1.0.6 兼容点，同时保留 1.0.5 断言。
- `tests/test_runtime_capabilities.py`、`tests/test_docker_deployment_contract.py`：当前版本期望改为 1.0.6。
- `tests/fixtures/memory-runtime-contract-v1.1.0.json`：`relaxed_agent_versions` 增加 1.0.6，不删除 1.0.4/1.0.5。

#### Runtime 有效文档

- 更新 `README.md`、`ENV_CONFIG.md`、`VERIFICATION.md`、`docker/backend/DOCKER_DEPLOY.md`。
- 只修改“当前活跃版本/部署命令/验证方法”，历史 1.0.5 的实施记录和冻结语义保留。
- 同步当前回忆录设计/计划中的版本状态，不新建第二套总控计划。

### 3.2 `couple-diary-doc`

#### 业务后端

- `backend/couple-diary-b/app/services/memory/memory_runtime_adapter_service.py`
  - 新建回忆录的 `MEMOIR_AGENT_VERSION` 改为 1.0.6。
- `backend/couple-diary-b/app/services/memory/memory_runtime_connectivity_service.py`
  - 继续通过 `MEMOIR_AGENT_VERSION` 校验 Runtime capabilities，不新增写死版本。
- `backend/couple-diary-b/app/services/memory/memory_runtime_launch_service.py`
  - 保持已存在 Run 的 `agent_version` 不变。
  - 对“最近一个 RunRef 已是 `failed` 且版本为 1.0.5”的新一次用户重试，明确选择 1.0.6；不改写旧 RunRef，不改数据库行。
  - 其他历史版本继续保持原来的版本冻结策略，避免变成“所有失败都自动追最新版”。
- `backend/couple-diary-b/app/services/memory/memory_publish_service.py`
  - 现有 `>=1.0.4` 版本比较已能接受 1.0.6，原则上只更新注释和回归用例，不新增一套发布分支。

#### Couple Diary 测试与合同 fixture

- `backend/couple-diary-b/tests/test_memory_runtime_adapter.py`：新建 Run 发送 1.0.6。
- `backend/couple-diary-b/tests/test_memory_runtime_connectivity.py`：capabilities 必须包含当前 1.0.6。
- `backend/couple-diary-b/tests/test_memory_runtime_launch_service.py`
  - 首次创建使用 1.0.6。
  - 失败的 1.0.5 用户重试创建新 Run 时升级到 1.0.6。
  - 已经运行/成功的旧 Run 不被改写。
  - 非 1.0.5 历史版本不被泛化升级。
- `backend/couple-diary-b/tests/test_memory_agent_tools_api.py`：1.0.6 与 1.0.4/1.0.5 同属放宽场景数和正文长度档。
- `backend/couple-diary-b/tests/test_memory_cross_repo_contract.py`：双仓 fixture 一致且包含 1.0.6。
- `backend/couple-diary-b/tests/fixtures/memory-runtime-contract-v1.1.0.json`：与 Runtime fixture 同步，`relaxed_agent_versions` 增加 1.0.6。

#### Couple Diary 文档

- 校准回忆录唯一总控、后端计划和需求文档中的当前 AgentPackage 版本。
- 保留 1.0.5 历史记录，只增加 1.0.6 的故障原因、升级规则和验收证据。
- 前端代码无需修改。

## 4. 明确不做的事情

- 不重新启动或重构 legacy launcher。
- 不修改 1.0.5 包文件来假装修复已发布版本。
- 不删除 Provider peer 安全校验。
- 不在 HTTP adapter 内做未计费、未记录的隐式 POST 重试。
- 不增加新模型 route、消息队列、数据表或环境变量。
- 不要求模型失败时用模板编造 summary。
- 不修改媒体单图失败降级语义；本次第二个 Run 已证明它正常。
- 不修改 PostgreSQL/Redis harness 和前端。

## 5. 实施与验证顺序

### 阶段 A：先完成 production 默认停用 launcher

1. 执行 [2026-09-02-production默认停用launcher部署调整说明.md](2026-09-02-production默认停用launcher部署调整说明.md) 中的提示词。
2. 验证 test 仍默认启动 launcher，production 默认只有 API/Worker/Reconciler。
3. 单独提交该部署调整，不与 1.0.6 功能修复混成一个 commit。

### 阶段 B：实现 Runtime 1.0.6

1. 先增加游标不提前、末批缺 summary、peer DNS 轮换和 `pool_pre_ping` 失败测试。
2. 新建 1.0.6 包，实现版本门禁后让定向测试通过。
3. 验证 1.0.0～1.0.5 历史 digest 和 1.0.5 行为不变。
4. 更新 Runtime 当前版本、Docker 配置默认值和有效文档。
5. Runtime 单独提交，不 push、不打 tag、不部署。

### 阶段 C：实现 Couple Diary 消费 1.0.6

1. 默认新 Run 版本改为 1.0.6。
2. 只为失败的 1.0.5 RunRef 增加受控的新 Run 升级规则。
3. 同步 provider/consumer contract fixture 和后端回归测试。
4. Couple Diary 单独提交，不 push、不打 tag、不部署。

### 阶段 D：test 环境部署

1. 备份并把服务器 `runtime-test.env` 的 `AGENT_PACKAGE_VERSION` 改为 `1.0.6`。
2. 先部署 Runtime test，确认 `prepare/register` 成功，capabilities 显示 1.0.6。
3. 再部署 Couple Diary test。两次部署间不进行回忆录生成，避免新旧默认版本暂时不一致。
4. 至少验收：正常 22 条左右素材、注入一次非法 JSON、注入一次 peer 集合轮换、末批首次缺 summary、媒体单图失败降级。
5. 确认无界重试不可能发生，失败 callback 和成功发布均能达到业务后端。

### 阶段 E：production 发布

1. 选择低流量窗口，备份 `runtime-production.env`，仅把 `AGENT_PACKAGE_VERSION` 调整为 1.0.6，不生成新密钥。
2. 先发布 Runtime production，确认 1.0.6 注册、API healthy、Worker/Reconciler running、launcher 仍不运行。
3. 立即发布 Couple Diary production。两次部署间暂停人工生成回忆录，接受这个低成本的短维护窗口，不为零停机额外扩展 capabilities 合同。
4. 生成一份新回忆录，确认批次失败时日志显示 `retry_pending=true`，且成功批次才前移游标。
5. 确认发布与 `run_succeeded` callback 完成，再观察 30 分钟 CPU、Worker 日志和 Provider 失败率。

### 回滚

- Runtime 回滚时把 `AGENT_PACKAGE_VERSION` 恢复为 1.0.5 并部署上一个 Runtime 镜像。
- Couple Diary 同步回滚到默认发起 1.0.5 的上一个镜像。
- 已经创建的 1.0.6 Run 仍绑定其原版本；不手工改 Run、RunRef 或 AgentPackage 数据库记录。
- 回滚不需要、也不应重新启动 launcher。

## 6. 建议验证命令

### Runtime

```bash
poetry run pytest -q \
  tests/runtime_test_memoir_loop_runner.py \
  tests/runtime_test_bounded_loop_executor.py \
  tests/runtime_test_memoir_coverage_repair.py \
  tests/test_model_gateway.py \
  tests/test_sqlalchemy_db.py \
  tests/test_runtime_agent_package_loader.py \
  tests/test_runtime_capabilities.py \
  tests/test_docker_deployment_contract.py

poetry run ruff check app tests
poetry run mypy app
poetry run pytest
git diff --check
```

### Couple Diary 后端

```bash
cd "/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b"

poetry run pytest -q \
  tests/test_memory_runtime_adapter.py \
  tests/test_memory_runtime_connectivity.py \
  tests/test_memory_runtime_launch_service.py \
  tests/test_memory_agent_tools_api.py \
  tests/test_memory_cross_repo_contract.py

poetry run ruff check app tests
poetry run pytest
git diff --check
```

## 7. 给 Claude Code / Codex 的实施提示词

```text
请在下列两个独立 Git 仓库中实现 MemoirAgent 1.0.6 稳定性修复，验证后分别创建 commit，不 push、不打 tag、不触发真实部署：

- Runtime：`/Users/yuye/YeahWork/AIAgent项目/com-agent-runtime`
- Couple Diary：`/Users/yuye/YeahWork/Python项目/couple-diary-doc`

前置条件：`2026-09-02-production默认停用launcher部署调整说明.md` 的改动已单独完成并部署验收。不要重做、回退或把 launcher 改动混入本次提交。

必须先读取并遵守：
- Runtime `AGENTS.md`
- Runtime `.codex/rules/AI通用编码与协作规范.mdc`
- Runtime `.codex/skills/agent-runtime-session/SKILL.md`
- Runtime `.codex/skills/memoir-runtime-integration/SKILL.md`
- Couple Diary `.codex/skills/couple-diary-dev/SKILL.md`
- `头脑风暴/docs/AgentRuntime/云服务器性能优化/2026-09-03-MemoirAgent-1.0.6模型稳定性修复执行方案.md`

按阶段使用当前环境可用的实际技能，不伪造命令：
1. `adaptive-workflow`：确认跨仓范围和验证强度。
2. `memoir-runtime-integration`：冻结 Runtime/Business 之间的版本、Tool 和 callback 边界。
3. `agent-runtime-session` 与 `couple-diary-dev`：分别约束两仓实现。
4. `tdd`：每个所有者对自己的行为改动先补失败测试，再做最小实现。
5. `code-review`：全部 Agent 交付、跨仓验证通过后，commit 前执行最终只读审查。

开始前分别执行两个仓库的 `git status --short`。保留用户现有改动，禁止 reset、checkout、清理数据或提交无关文件。按测试先行执行，先证明失败再修复。

必须使用多 Agent 开发，并在任务开始时向每个 Agent 明确其文件所有权。主 Agent 先完成两仓工作区检查和共享契约校准，再并行分配：

- Agent A（Runtime AgentPackage/循环所有者）：只负责 `app/agents/memoir_agent/1.0.6/**`、`app/agents/memoir_agent/runner.py`、必要时的 `app/agents/memoir_agent/model_gateway.py`，以及直接对应的 loop/full-graph/coverage/media/package-loader 测试。1.0.5 目录只读，不得改动任何字节。
- Agent B（Runtime 基础设施/部署所有者）：只负责 `app/runtime/model_gateway.py`、`app/db/sqlalchemy_db.py`、`app/runtime/tool_gateway.py`、`app/api/endpoints/capabilities_api.py`、Runtime 一侧 contract fixture、对应测试、Docker env 默认值、GitHub Actions 版本和 Runtime 有效文档。不修改 Agent A 的 runner/package 文件。
- Agent C（Couple Diary 所有者）：只在 `/Users/yuye/YeahWork/Python项目/couple-diary-doc` 内修改 Business 后端的 adapter/connectivity/launch/publish、consumer contract fixture、对应测试和回忆录权威文档。不修改前端，不修改 Runtime 仓库。
- 主 Agent（契约与集成所有者）：负责冻结 1.0.6/1.1.0 跨仓契约，协调 Agent A 的新包与 Agent B 的 capabilities/部署版本，校对 Agent B/C 的双仓 fixture 字节一致，处理交叉依赖，运行全部门禁，并最后按仓库分别提交。不得覆盖用户或子 Agent 的改动。

所有子 Agent 都必须被明确告知：自己不是唯一开发者，不 reset、不 checkout、不回退或格式化他人文件、不提交 Git；应根据共享工作区中的新变化调整自己的实现。发现所有权重叠、契约冲突或用户已有改动与任务重叠时，立即停止相关写入并交主 Agent 处理。

主 Agent 必须等待 Agent A/B/C 全部完成，先审查每个 Agent 的 diff 和定向测试，再执行跨模块和全量验证。不得让多个 Agent 同时修改同一文件，不得在子 Agent 尚未返回时提前提交。

线上证据：
- 失败 Run `4c151dff-57ad-4954-bcc0-50d1d2d676e3`：第 2 批 `JSON_PARSE_FAILED`，第 3 批 `MODEL_PROVIDER_PEER_MISMATCH`，结构修复仍缺 `summary`，最终 `LOOP_SUMMARY_MISSING -> LOOP_BODY_FAILED`。
- 成功 Run `255132b2-f561-4f55-a359-970aabe028b4`：22 条素材按 8/8/6 完成，14 张 Scene、covered=22，发布和 callback 成功。
- 结论：launcher 与故障无关；不得重新启动 launcher。

必须完成的 Runtime 改动：
1. 不改 `app/agents/memoir_agent/1.0.5/` 的任何字节。以它为基线新建完整、自包含、不可变的 `1.0.6/` 包；`contract_version=1.0.0`，Tool wire 仍为 1.1.0。
2. 1.0.6 `agent.yaml` 的 `max_model_calls` 调为 8，保留 token/cost/time 硬上限。
3. `runner.py` 仅对 agent_version>=1.0.6 实现批次候选游标：模型调用与全部校验成功后才提交游标和 Scene；JSON/schema/semantic/provider/peer 失败时下一轮在剩余预算内重试同批，不得无界重试。
4. 1.0.6 首批必须有且只有一张居首 cover，末批必须有且只有一张居末 summary；缺失时本批不提交。
5. 结构修复继续复用 generate_scene_batch route，请求增加受信任 `required_scene_type=cover|summary`；1.0.6 prompt 要求只返回一张目标卡。不增新 route/env，不用模板编造内容。
6. `app/runtime/model_gateway.py` 保留发送前 DNS 安全检查；真实 peer 必须本身是公网 IP。peer 未命中发送前集合时，对同 hostname 做一次连接后重解析，只在命中前后集合并集时放行；其他情况继续 fail-closed。不在 adapter 内自动重发 POST。
7. Runtime 主 SQLAlchemy engine 增加 `pool_pre_ping=True`，保留 pool_recycle。
8. 登记 1.0.6 Tool wire，capabilities 切换为 1.0.6，同步 configure-runtime-env.sh、test/production env example、GitHub Actions 中的活跃包版本。Compose 服务结构和 harness 不改。
9. 更新 Runtime contract fixture 的 relaxed_agent_versions，必须保留 1.0.4/1.0.5 并追加 1.0.6。
10. 补齐文档，只修当前活跃版本和部署说明，不改写 1.0.5 历史记录。

必须完成的 Couple Diary 改动：
1. 新建回忆录的 `MEMOIR_AGENT_VERSION` 切换为 1.0.6，connectivity 继续从该常量校验 capabilities。
2. 对最近 RunRef 已 failed 且 agent_version=1.0.5 的用户重试，新 Run 明确升级到 1.0.6；不改旧 RunRef，不对其他版本做泛化追新。
3. 发布校验继续复用 >=1.0.4 放宽档，不新增 1.0.6 特殊业务分支。
4. 同步 consumer contract fixture 和 adapter/connectivity/launch/tool/cross-repo 测试。
5. 更新回忆录唯一总控、后端计划和需求中的当前版本，保留历史证据。不改前端，不做数据库迁移。

必须覆盖的回归：
- 非法 JSON 不消费批次，下轮同批重试成功。
- Provider/peer 瞬时失败不消费批次。
- 末批首次缺 summary，不提交，后续在有界预算内成功。
- 结构修复只接收指定 cover/summary。
- 预算耗尽仍收敛，没有 busy loop。
- DNS 轮换合法公网 peer 可放行，私网、不属于前后 DNS 集合或重解析失败仍拒绝。
- 1.0.0～1.0.5 包 digest 不变，1.0.5 旧行为不变。
- 1.0.6 完整图能发布，媒体单图失败仍只降级文字卡。
- Couple Diary 新建与受控重试的版本选择正确，双仓 fixture 字节完全一致。

验证命令至少包含本方案第 6 节的定向测试、两仓 Ruff、Runtime Mypy、两仓全量 pytest 和 `git diff --check`。不读取或输出真实密钥，不调用真实计费 Provider。

验证通过后分仓检查 diff，只 stage 本任务文件，然后创建两个独立 commit：
- Runtime 建议：`fix(memoir): add bounded retry semantics for agent 1.0.6`
- Couple Diary 建议：`feat(memoir): adopt runtime agent 1.0.6`

最终报告 Agent A/B/C 的所有权与交付摘要、两个 commit hash、分仓改动文件、实际测试结果、未执行项和原因，以及 test/production 的发布与回滚命令。
```
