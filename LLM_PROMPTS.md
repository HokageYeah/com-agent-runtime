# AgentRuntime LLM 协作提示词

这份文档提供可复制的最小上下文。项目规则仍以 `AGENTS.md`、`.codex/rules/AI通用编码与协作规范.mdc`、[README.md](README.md) 和实际代码为准。

## 最短实用版

```text
你正在维护独立公共执行服务 com-agent-runtime，不是情侣日记业务后端模板。

开始修改前请遵守：
1. Runtime 负责 AgentPackage、AgentRun、Worker、Model/Tool Gateway、Checkpoint、Artifact、callback、对账和治理。
2. 用户、关系、Archive、Snapshot、密码、PlaybackDocument 和 published_revision 属于 couple-diary-b；Runtime 不直连业务库。
3. 生产公共接口位于 /api/v1/runtime/*。memory/* 与 internal memory handler 是 development/test 的迁移证据，不是新业务扩展点。
4. 公共契约以 app/contracts/、当前路由、tests/fixtures 和 contract tests 为准；旧计划中的示意路径不能覆盖实现。
5. 写操作必须保持 HMAC、授权、幂等、lease/fencing、generation/privacy version 与安全日志边界。
6. prompt、模型原输出、工具原始 payload、正文、凭据和私有 URL 不得进入日志、trace、callback、Artifact、测试输出或 Checkpoint（即使加密也不持久化）。
7. AgentPackage 版本和 digest 不可变；修改 Agent 行为时发布新版本，不覆盖旧包。
8. 建库、迁移、注册和完整启动统一使用 ./agent-runtime.sh；不得迁移 couple_diary_dev/test/prod 业务库。
9. 先运行最窄相关测试，再按影响运行 pytest、Ruff、Mypy、Alembic single-head 与 git diff --check。

优先阅读 README.md、ENV_CONFIG.md、VERIFICATION.md、头脑风暴/docs/AgentRuntime/需求设计文档.md 和契约冻结记录.md。
```

## 标准协作版

```text
你正在维护 com-agent-runtime：一个 FastAPI + SQLAlchemy/Alembic + LangGraph/LangChain 的公共 Agent 执行服务。

一、事实与所有权
- Runtime 拥有 AgentDefinition/Run/Plan/Step/ToolCall/ModelUsage/Checkpoint/Artifact/Audit/Outbox。
- couple-diary-b 拥有用户、关系、权限、Archive、Snapshot、密码、PlaybackDocument 和 published_revision。
- couple-diary-f 只调用业务后端，不直连 Runtime。
- Runtime 只经已授权 Business Tool/callback 与业务后端交互，不连接业务数据库。

二、代码边界
- app/contracts：版本化 API/Event/Tool/Artifact wire contract。
- app/api/endpoints：公共 Runtime API 与环境路由门禁。
- app/runtime：Planner、Executor、Model/Tool Gateway、Checkpoint、Guardrail 等执行内核。
- app/services + app/models/runtime.py：事务、幂等、lease/fencing、outbox、callback、reconciliation 和治理。
- app/agents/<agent>/<version>：不可变 AgentPackage。
- demo/diary/memory 业务代码是历史兼容或迁移证据，不是新增公共能力的默认落点。

三、运行与数据安全
- create/start 使用 held 握手；HTTP 请求不执行长 workflow。
- 数据库是 run、outbox、lease、fencing、checkpoint 和幂等的权威来源；Redis 只做共享流控或通知加速。
- 所有副作用使用稳定逻辑幂等键，attempt 只用于审计。
- 模型、工具、callback 和恢复前复核 authorization/privacy/lease/fencing/generation 边界。
- 不记录 prompt、模型原输出、工具原载荷、业务正文、凭据、私有 endpoint 或签名 URL。

四、契约与兼容
- 精确契约先查 app/contracts、tests/fixtures、provider/consumer contract tests 和当前路由。
- Runtime 公共路径为 /api/v1/runtime/health/*、/capabilities 和 /agent-runs*。
- 破坏性契约变更提升 major；同 major 只做兼容演进。
- AgentPackage 调用方必须显式指定版本，不能自动选择磁盘最新版。

五、操作入口
- 配置检查：./agent-runtime.sh doctor <environment>
- 安全建库与迁移：./agent-runtime.sh prepare <environment>
- 注册 Package：./agent-runtime.sh register <environment> --agent-id <id> --version <version>
- 完整单机启动：./agent-runtime.sh start <environment>
- 隔离真实 harness：./agent-runtime.sh verify
- run.sh/run_app.py 只启动 API，不消费 Runtime outbox。

六、验证
- 先运行与改动直接相关的最小 pytest。
- 再按风险运行 poetry run pytest -q、poetry run ruff check .、poetry run mypy app、poetry run alembic heads、git diff --check。
- 文档和计划状态只能用代码、测试、迁移或可复现命令校准；不要仅凭历史 checkbox 宣称完成。
```
