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
- 唯一总控计划：`头脑风暴/docs/superpowers/回忆录/plans/2026-08-06-回忆录-总控开发计划.md`
- 前端详细计划：`头脑风暴/docs/superpowers/回忆录/frontend/2026-08-06-回忆录-前端开发计划.md`
- 后端与 Runtime 详细计划：`头脑风暴/docs/superpowers/回忆录/backend/2026-08-06-回忆录-后端开发计划.md`
- 前端工程约定：`frontend/couple-diary-f/README.md`、`frontend/couple-diary-f/.claude/rules/UniApp模块化工程规范.md`
- 后端工程约定：`backend/couple-diary-b/README.md`

回忆录计划已经生成完毕。`plans/README.md` 是导航入口，`2026-08-06-回忆录-总控开发计划.md` 是唯一执行与交接入口；后续应校准或更新现有计划，不得再生成第二套并行总控。Runtime 总控/后端计划用于证明公共能力的实现状态和承接 R0–R4，不得覆盖情侣日记总控中的产品需求、业务所有权或跨仓顺序。

## 3. 已确认的整体开发计划

### 3.1 计划权威与阅读顺序

| 层级 | 权威文件 | 作用 |
|---|---|---|
| 跨项目总控 | 心约手帐 `头脑风暴/docs/superpowers/回忆录/plans/2026-08-06-回忆录-总控开发计划.md` | 唯一执行入口；冻结共享契约、M0–M5 顺序、REQ/AC 映射、V-01–V-16 验收与交接记录 |
| 前端子计划 | 心约手帐 `头脑风暴/docs/superpowers/回忆录/frontend/2026-08-06-回忆录-前端开发计划.md` | F0–F8：密码入口、列表、日期预检、生成状态、重试、详情播放器、隐私与发布门禁 |
| 后端与 Runtime 子计划 | 心约手帐 `头脑风暴/docs/superpowers/回忆录/backend/2026-08-06-回忆录-后端开发计划.md` | B0–B12 与 R0–R4：业务数据底座、生成编排、Tool/callback、Runtime 对齐、迁移与联调 |
| Runtime 公共能力计划 | AgentRuntime 总控与后端详细计划 | Runtime 实现状态、公共契约和验证证据；保留历史勾选事实，按 R0–R4 校准架构归属 |

开始实施前依次读取跨项目总控、当前里程碑对应的子计划、Runtime 公共能力计划与实际代码/测试。当前代码只用于重新校准“已实现/部分实现/未实现/待确认”，不能覆盖已确认的 `REQ-001~011`、`AC-001~011` 或总控冻结契约。

### 3.2 M0–M5 执行地图

| 里程碑 | 任务范围 | 可独立验收的结果 |
|---|---|---|
| M0 契约与迁移门禁 | B0、R0–R1、F0 | provider/consumer fixture、写命令、Tool/callback、HMAC、幂等、epoch 和文件所有权冻结；Runtime 遗留业务实现只作迁移源 |
| M1 业务数据底座与门禁 | B1–B4、F1–F2 | 密码/grant、五类素材 projection、业务 Archive/Snapshot/Playback 模型、资格/选日领域服务及前端类型/API 骨架 |
| M2 两类创建与 Snapshot 物化 | B5–B7、F3–F4 | 手动作用品与解绑双方作品可确定性创建，revision 0 baseline、manifest、Outbox 和 Snapshot 失败重放闭环 |
| M3 Runtime 生成闭环 | B8–B10、R2–R3 | held create/bind/start、真实 Business Tool provider、原子发布、callback/主动对账、五类 Snapshot 和公共 ToolGateway 对齐 |
| M4 列表管理与播放器 | B11、F5–F7 | owner 隔离的列表/详情/状态/重试/置顶/删除 API，前端轮询、失败 baseline 与安全 Scene/Action runner |
| M5 迁移、联调与交付 | R4、B12、F8、M5-V | 隔离环境迁移演练、旧路由停用、V-01–V-16、隐私扫描及两仓原生门禁；缺 staging/密钥/回滚证据时不得宣称生产闭环 |

顺序原则：先共享契约，再业务后端事实源，再 Runtime connector/workflow，最后前端消费与跨仓联调。里程碑内可按子计划标明的依赖并行，但不得让前端直连 Runtime，也不得在 Runtime 新增业务事实表/API。

### 3.3 2026-08-06 计划起点（历史基线）

- **已实现：** 开发/测试环境 Runtime capabilities 安全代理；它只证明连接级合同。
- **部分实现：** Runtime 公共 Run/Worker/Tool/callback/MemoirAgent、稳定空间/关系段、五类素材的部分模型或读取能力；均仍有总控所列的真实 connector、契约、隐私或投影缺口。
- **未实现：** 目标业务仓中的 Archive/Snapshot/密码/Playback 正式闭环，以及回忆录正式前端页面；Runtime 同名遗留实现不得算作目标业务仓完成。
- **待确认/上线门禁：** Runtime 遗留业务数据是否有生产记录、staging HTTPS、生产密钥轮换、合法域名、备份与回滚证据。

上述四项是总控建立时的起点，不能作为当前进度结论。每次执行后只在唯一总控与对应子计划更新 checkbox/执行记录，并用两仓当前代码、迁移、测试和 Git 状态重新判断“已实现/部分实现/未实现/待确认”。技能地图只提供导航与稳定边界，不替代计划内的逐项验收。

## 4. 项目技能路由

| 项目 | 技能 | 用途 |
|---|---|---|
| AgentRuntime Codex/Claude | `agent-runtime-session` | Runtime 新会话的设计、开发、诊断、联调和验证入口 |
| AgentRuntime Codex | `agent-runtime-next-plan` | 基于代码证据盘点未完成项并排下一大块；只读 |
| 心约手帐 Codex/Claude | `couple-diary-dev` | 前后端开发、诊断、评审和验证的项目入口 |
| 心约手帐 Codex/Claude | `couple-diary-requirements` | 收敛、追踪和更新模块需求/设计；不写计划或实现 |
| 心约手帐 Codex/Claude | `couple-diary-planner` | 生成或校准总控、前端、后端开发计划；不实现代码 |
| 两项目 Codex/Claude | `memoir-runtime-integration` | 装载本地双项目地图、事实源、责任和契约，再路由到需要的专项技能 |

其它技能按风险选用：根因不明用诊断流程，合同改动用 TDD，大型已确认计划用执行计划流程，合入前用代码评审/完成验证。不因为跨项目就机械叠加所有技能。

## 5. 总体设计思路

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

## 6. 当前里程碑与契约注意

- 2026-08-06 时的起点只完成了开发/测试环境 Runtime capabilities 连接级验证。该结论已是历史基线，不能用来否定后续代码或宣称当前仍只有连接能力。当前里程碑必须从心约手帐唯一总控、对应子计划、两仓实现和实际验证重新得出。
- Runtime 当前实现与测试使用 `/api/v1/runtime/capabilities`、`/api/v1/runtime/agent-runs`等规范路径。较旧总控/需求中可能出现 `/api/v1/runtime-capabilities` 或不带 `/runtime` 段的示意路径；实施时以 `app/contracts/`、当前路由、contract fixture 和双方测试为准，并同步清理受影响的有效文档。
- 小程序进度仍以业务后端 HTTP 退避轮询为正确性基线；Runtime 原生 SSE、TTS、音乐和视频不是当前必须闭环。Runtime 已实现可选的发布前图片生成，但 `MEMOIR_MEDIA_ENABLED` 默认关闭，真实火山计费调用和真实 OSS 写入仍需目标环境受控联调。
- MemoirAgent 场景数和正文长度是 AgentPackage 版本合同：`1.0.0-1.0.3` 保持 3-8 个场景且 `body` 最多 80 字；`1.0.4` 至少 3 个场景，不设场景总数和 `body` 字数上限。媒体关闭或生成失败时显式提交空或部分 `media_manifest`，以文字卡安全降级。

## 7. 联调验收最小矩阵

| 场景 | 必须证明 |
|---|---|
| capabilities/启动 | 签名、版本、agent/package/model capability 兼容；不泄露 endpoint/digest/凭据 |
| held create/start | Runtime 在业务侧绑定 `run_id` 前不执行；重放不重复占用配额或产生 outbox |
| Snapshot/Tool | 只读冻结的 `space_id + relationship_segment_no + snapshot_id + generation_epoch` 允许集；Runtime 不读业务库 |
| 发布 | 整份 PlaybackDocument 事务校验成功才切 revision；幂等重试返回首次结果 |
| callback/对账 | 重复不重写、乱序不倒退、旧 run 不改当前 archive、无发布的 success 不伪造作品 |
| 隐私/撤权 | 删除、purge、authorization/privacy version 可阻止迟到正文复活；日志与 public trace 只含安全元数据 |
| 前端 | 只调业务后端；未发布/失败播放 baseline；后台/离页停轮询与计时；空 actions/低性能/未知 major 安全降级 |
