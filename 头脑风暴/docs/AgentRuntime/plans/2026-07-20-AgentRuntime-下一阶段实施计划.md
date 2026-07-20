# AgentRuntime 下一阶段实施计划

**目标：** 在已有 Runtime 底座之上，先闭合“情侣解绑归档 → 可播放 baseline → 受控 AgentRun → 原子发布”的业务主链，再完善模型和工具治理。

**当前核对结论（2026-07-20）：**

- Runtime 的 Contract、Run/Outbox/Lease、静态 Executor、加密 Checkpoint、Redis provider permit、Policy 预算预留与对账基础设施已有实现和回归测试。
- 回忆录已具备加密快照、双方隔离 archive、revision 0、原子发布、内部工具与 callback 投影的基础实现。
- 本轮补齐了归档幂等重放和“非空 baseline 可播放”；全量验证为 `202 passed`、`ruff check .`、`git diff --check`。
- 不能将总控 Task 6.5 标为整体完成：解绑事务、真实冻结 manifest materializer、完整版本化领域模型、隐私删除/GC 仍缺失。

## 执行顺序

### Task A：解绑事务与冻结 manifest（优先级 P0）

**目的：** 让和平解绑和强制拉黑的业务事务成为唯一归档触发源，而非由调用方手工构造 `FrozenMemoryInput`。

**涉及文件：**

- 修改：关系解绑服务、`app/services/memory_archive_service.py`
- 新建：`app/services/memory_snapshot_materializer.py`
- 修改：解绑相关 API/outbox handler、Alembic migration
- 测试：`tests/test_memory_archive_snapshot.py`、解绑服务回归测试

**验收：**

1. 同一解绑事务冻结 `space_id + relationship_segment_no + cutoff + source manifest/version`，并同事务写 archive/snapshot outbox。
2. materializer 仅按 manifest 的 ID/version 读取日记和赌局；不读取任务执行期间新增的数据。
3. 双方 archive 隔离；重复 outbox 重放幂等；冻结输入不一致返回安全冲突码。
4. 日志、outbox、Artifact 和 checkpoint 不含日记正文、prompt、完整播放文档。

### Task B：回忆录领域模型补齐与版本化发布（P0）

**目的：** 将当前最小模型演进为需求文档规定的可恢复、可清理作品容器。

**涉及文件：**

- 修改：`app/models/memory_*.py`、`app/services/memory_archive_service.py`、`app/services/memory_player_service.py`
- 新建：对应 Alembic migration
- 测试：播放文档、Scene/Action、隐私删除、revision GC

**验收：**

1. Archive 保存关系/用户快照、时间范围、`content_status/enhancement_status`、generation epoch、published revision 与删除/置顶字段；generation status 仅派生。
2. Snapshot/Document/Scene/Action 具备 schema/version 和必要外键或等价引用约束。
3. AI Scene 的 `source_refs_json` 必须属于当前 snapshot allowlist；播放器只读取 `published_revision` 的同一完整作品。
4. 普通 superseded revision 按宽限期 GC；隐私删除立即撤权，绝不复用普通宽限。

### Task C：情侣日记与 Runtime 的 held-run 握手（P0）

**目的：** 业务库可靠绑定 `active_run_id` 后才启动 Runtime，避免“已归档但没有 Run”或重复生成。

**涉及文件：**

- 修改：`app/services/memory_agent_binding_service.py`、`app/services/memory_agent_callback_service.py`
- 新建：`app/services/memory_agent_adapter.py`、业务补偿 handler
- 测试：held create/start、pending_start 超时、callback 乱序与 epoch superseded

**验收：**

1. 创建 held run 使用稳定 create key；绑定成功后用独立 start key 入队。
2. Runtime/version/package/policy 不可用时 baseline 保持可读，补偿按同一幂等键重试。
3. 仅 active run 且 epoch 匹配的 callback 可更新 archive 摘要；发布成功仍只能由原子发布工具写入。
4. `pending_start` 超过 600 秒有可审计的修复或失败收敛路径。

### Task D：完成 ToolGateway 与 MemoirAgent 主链（P1）

**目的：** 让 `load_snapshot → sanitize → stats → templates/model → safety → publish` 通过统一 ToolGateway 执行。

**涉及文件：**

- 修改：`app/runtime/tool_gateway.py`、`app/agents/memoir_agent/runner.py`、workflow package
- 新建：工具参数/语义校验与端到端 fixture
- 测试：工具幂等、超时/未知结果、跨 archive/epoch 拒绝、模型不可用 fallback

**验收：**

1. 所有业务 HTTP 调用走 connector registry、签名、deadline 和稳定 logical key。
2. 无素材、日记-only、赌局-only、强制拉黑表达均生成安全的可播放 fallback。
3. 完整播放文档只经 `memory.publish_playback_document` 发布，媒体关闭时提交空 `media_manifest`。

### Task E：收敛 Task 8–11 的治理缺口（P1）

**目的：** 将已有模型流量/usage 基础接入 Prompt、Context、Evaluator、Callback 和对账闭环。

**依赖顺序：** ContextManager 完整预算与语义校验 → Evaluator/Guardrails/时间预算 → Callback dispatcher/PublicTrace → dead letter/TTL/隐私对账。

**验收：**

1. Prompt 版本、无正文 usage、结构化输出和语义校验在真实模型节点中串联。
2. provider permit、timeout、fallback、授权/隐私撤销均不能发送或提交过期结果。
3. callback 使用原事件身份重放，public trace 只含白名单摘要。
4. 最终端到端覆盖 held create、解绑归档、执行、原子发布、callback、purge 与 worker 接管。

## 本阶段约束

- 所有新增逻辑、模型字段和安全日志添加中文注释；日志绝不输出 prompt、日记正文、完整播放文档、密钥或签名 URL。
- 每项先写失败回归，再实现最小代码；每项完成运行 `poetry run pytest`、`poetry run ruff check .` 和 `git diff --check`。
- 不引入动态 Agent、MCP 实连、长期私密记忆或媒体生成队列，直至主链稳定。
