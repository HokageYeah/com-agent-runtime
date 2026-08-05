# 回忆录跨项目地图

## 1. 工作区与系统边界

| 工作区 | 绝对路径 | 拥有的事实 |
|---|---|---|
| AgentRuntime | `/Users/yuye/YeahWork/AIAgent项目/com-agent-runtime` | 公共 Agent 运行时、MemoirAgent、AgentRun/Plan/Step、Worker/outbox/lease/fencing、Model/Tool Gateway、Checkpoint/Artifact、callback/public trace、治理与观测 |
| 心约手帐总工程 | `/Users/yuye/YeahWork/Python项目/couple-diary-doc` | 产品需求、总体架构、回忆录设计、前后端计划、业务联调 |
| 心约手帐前端 | `/Users/yuye/YeahWork/Python项目/couple-diary-doc/frontend/couple-diary-f` | uni-app + Vue 3 + TypeScript 小程序页面、播放器、轮询/降级体验；只调业务后端 |
| 心约手帐业务后端 | `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b` | FastAPI + MySQL 业务 API、用户/关系/权限、Archive/Snapshot/PlaybackDocument、密码、列表/详情/删除/置顶、Runtime adapter/tool/callback |

`uni-com-project-template` 仅是模板，不是回忆录实现或设计事实源。

## 2. 必读事实源

下面的路径均以所在小节对应的项目根目录为基准：“AgentRuntime”小节相对于 `/Users/yuye/YeahWork/AIAgent项目/com-agent-runtime`，“心约手帐”小节相对于 `/Users/yuye/YeahWork/Python项目/couple-diary-doc`；不相对于当前会话目录或技能目录解析。

### AgentRuntime

- 总入口：`README.md`、`ENV_CONFIG.md`、`VERIFICATION.md`
- 总设计：`头脑风暴/docs/AgentRuntime/需求设计文档.md`
- 契约实现确认：`头脑风暴/docs/AgentRuntime/契约冻结记录.md`、`app/contracts/`、`tests/fixtures/runtime-contract-v1.0.0.json`
- 总控计划：`头脑风暴/docs/AgentRuntime/plans/2026-07-07-AgentRuntime-总控开发计划.md`
- 后端详细计划：`头脑风暴/docs/AgentRuntime/backend/2026-07-07-AgentRuntime-后端开发计划.md`
- Memoir 工作流：`头脑风暴/docs/AgentRuntime/plans/2026-07-20-Task8-Memoir工作流集成计划.md`
- 内容安全：`头脑风暴/docs/AgentRuntime/plans/2026-07-20-Task9-回忆录内容安全与质量设计说明.md`
- Archive/Snapshot/播放专题：`头脑风暴/docs/AgentRuntime/plans/2026-07-20-Task6.5-归档快照与播放文档收尾设计说明.md`
- Tool/Worker/callback 安全：`头脑风暴/docs/AgentRuntime/plans/2026-07-20-Task7-Task5.5-工具安全与Worker宽限期设计说明.md`
- 路由/授权/模型安全：`头脑风暴/docs/AgentRuntime/plans/2026-07-20-Task7-Task8-工具写入与路由安全设计说明.md`

### 心约手帐

- 总需求/导航：`头脑风暴/docs/2026-06-11-情侣日记小程序-需求设计总文档.md`
- 整体功能摘要：`头脑风暴/docs/2026-06-11-情侣日记小程序-需求设计文档.md`
- 回忆录权威需求：`头脑风暴/docs/superpowers/回忆录/需求设计文档.md`
- UI 设计入口：`头脑风暴/docs/superpowers/回忆录/designs/README.md`
- 模块整体、列表、详情页指南：`头脑风暴/docs/superpowers/回忆录/designs/回忆录模块前端设计指南.md`、`头脑风暴/docs/superpowers/回忆录/designs/回忆录列表/回忆录列表页前端设计指南.md`、`头脑风暴/docs/superpowers/回忆录/designs/回忆录详情/回忆录详情页前端设计指南.md`
- 原型与参考实现：`头脑风暴/docs/superpowers/回忆录/designs/回忆录列表/code/`、`头脑风暴/docs/superpowers/回忆录/designs/回忆录详情/code/`
- 开发计划入口：`头脑风暴/docs/superpowers/回忆录/plans/README.md`
- 前端工程约定：`frontend/couple-diary-f/README.md`、`frontend/couple-diary-f/.claude/rules/UniApp模块化工程规范.md`
- 后端工程约定：`backend/couple-diary-b/README.md`

回忆录 `plans/README.md` 当前是索引与编写要求，不是可执行总控计划。需要实施计划时，使用 `couple-diary-planner` 基于当前需求、两边代码和 Runtime 契约生成，不把 Runtime 总控计划直接复制为业务计划。

## 3. 项目技能路由

| 项目 | 技能 | 用途 |
|---|---|---|
| AgentRuntime Codex/Claude | `agent-runtime-session` | Runtime 新会话的设计、开发、诊断、联调和验证入口 |
| AgentRuntime Codex | `agent-runtime-next-plan` | 基于代码证据盘点未完成项并排下一大块；只读 |
| 心约手帐 Codex/Claude | `couple-diary-dev` | 前后端开发、诊断、评审和验证的项目入口 |
| 心约手帐 Codex/Claude | `couple-diary-requirements` | 收敛、追踪和更新模块需求/设计；不写计划或实现 |
| 心约手帐 Codex/Claude | `couple-diary-planner` | 生成或校准总控、前端、后端开发计划；不实现代码 |
| 两项目 Codex/Claude | `memoir-runtime-integration` | 装载本地双项目地图、事实源、责任和契约，再路由到需要的专项技能 |

其它技能按风险选用：根因不明用诊断流程，合同改动用 TDD，大型已确认计划用执行计划流程，合入前用代码评审/完成验证。不因为跨项目就机械叠加所有技能。

## 4. 总体设计思路

```text
couple-diary-f
  -> couple-diary-b 业务 API
     -> 解绑事务冻结 source manifest
     -> 建立 Archive + revision 0 baseline + Snapshot/RunRef
     -> HMAC 调用 AgentRuntime held create/start
        -> MemoirAgent 通过 memory.get_snapshot 读脱敏快照
        -> 生成/校验 PlaybackDocument
        -> 通过 memory.publish_playback_document 原子发布 revision
        -> 安全 callback 更新 RunRef/非成功进度
     -> couple-diary-b 只返回 published revision 或 baseline
  -> couple-diary-f 轮询业务状态并播放 Scene/Action
```

不可破坏的所有权：

| 能力/数据 | 唯一所有者 |
|---|---|
| 用户、稳定双人空间、关系段、日记/赌局、密码、owner 权限 | 心约手帐业务后端 |
| Archive、Snapshot source manifest、PlaybackDocument/scenes/actions/media manifest、`published_revision` | 心约手帐业务后端 |
| AgentRun/Plan/Step/ToolCall/Evaluation/Checkpoint/Artifact/ModelUsage | AgentRuntime |
| `MemoryAgentRunRef.status` | 业务 callback adapter/补偿任务 |
| `content_status=succeeded` 和 `published_revision` 切换 | `memory.publish_playback_document` 所在业务事务 |
| 安全进度、页面状态、轮播执行 | 业务后端读模型 + 前端 |

## 5. 当前里程碑与契约注意

- 回忆录需求文档当前明确：已有改动仅是开发/测试环境的 Runtime capabilities 连接级验证；前端经 `/memory/runtime-connectivity` 调业务后端，后端代签并裁剪安全摘要。production 必须隐藏并拒绝该调试入口。
- 该阶段不创建 AgentRun、Archive、Snapshot、Worker、Callback 或 Published Revision。以后任务必须重新审计代码和测试，不把本条作为永久进度结论。
- Runtime 当前实现与测试使用 `/api/v1/runtime/capabilities`、`/api/v1/runtime/agent-runs`等规范路径。较旧总控/需求中可能出现 `/api/v1/runtime-capabilities` 或不带 `/runtime` 段的示意路径；实施时以 `app/contracts/`、当前路由、contract fixture 和双方测试为准，并同步清理受影响的有效文档。
- 第一版小程序进度以业务后端 HTTP 退避轮询为基线；Runtime 原生 SSE、TTS、封面/图片/视频媒体生成不是第一版必须闭环。
- 正常 MemoirAgent MVP 作品为 3–8 张 Scene，单卡主体不超过 80 字，绝不发布超过 16 张的作品；媒体关闭时仍显式提交空 `media_manifest`。

## 6. 联调验收最小矩阵

| 场景 | 必须证明 |
|---|---|
| capabilities/启动 | 签名、版本、agent/package/model capability 兼容；不泄露 endpoint/digest/凭据 |
| held create/start | Runtime 在业务侧绑定 `run_id` 前不执行；重放不重复占用配额或产生 outbox |
| Snapshot/Tool | 只读冻结的 `space_id + relationship_segment_no + snapshot_id + generation_epoch` 允许集；Runtime 不读业务库 |
| 发布 | 整份 PlaybackDocument 事务校验成功才切 revision；幂等重试返回首次结果 |
| callback/对账 | 重复不重写、乱序不倒退、旧 run 不改当前 archive、无发布的 success 不伪造作品 |
| 隐私/撤权 | 删除、purge、authorization/privacy version 可阻止迟到正文复活；日志与 public trace 只含安全元数据 |
| 前端 | 只调业务后端；未发布/失败播放 baseline；后台/离页停轮询与计时；空 actions/低性能/未知 major 安全降级 |
