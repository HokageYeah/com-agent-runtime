---
name: memoir-runtime-integration
description: Use when designing, planning, implementing, reviewing, or debugging the 心约手帐/情侣日记“回忆录”跨项目链路，或任务涉及 couple-diary-doc 前后端与 com-agent-runtime 之间的 Runtime API、MemoirAgent、Archive/Snapshot、PlaybackDocument、Business Tool、callback、状态对账、隐私安全或联调契约时使用。
---

# 回忆录跨项目集成

## 核心原则

把 `couple-diary-doc` 视为回忆录产品、业务数据、权限、前端交互和业务 API 的事实源；把 `com-agent-runtime` 视为 Agent 执行、模型/工具调用、恢复、观测和治理的事实源。前端只调情侣日记后端，Runtime 只通过经授权的 Business Tool/callback 与业务后端交互。

**REQUIRED REFERENCE:** 先读 [references/project-map.md](references/project-map.md)，再开始跨项目回忆录任务。

## 定位工作区

1. 确认当前目录是以下两个项目之一：
   - AgentRuntime：`/Users/yuye/YeahWork/AIAgent项目/com-agent-runtime`
   - 心约手帐：`/Users/yuye/YeahWork/Python项目/couple-diary-doc`
2. 不得把 `uni-com-project-template` 当成回忆录前端；真实前端是 `couple-diary-doc/frontend/couple-diary-f`，业务后端是 `couple-diary-doc/backend/couple-diary-b`。
3. 回忆录当前权威设计目录是 `couple-diary-doc/头脑风暴/docs/superpowers/回忆录`。`couple-diary-doc/头脑风暴/docs/回忆录` 当前不存在，不得按该路径猜测或新建第二份设计。
4. 先分别执行两个仓库的 `git status --short`，保留用户已有改动。跨仓修改时按仓库分组记录文件和验证结果。

## 装载上下文

### 在 com-agent-runtime 中

1. 使用 `agent-runtime-session` 装载 Runtime 固定设计、计划、配置、安全边界和验证状态。
2. 只做“下一大块/未完成盘点/排序”时，再使用 `agent-runtime-next-plan`；该阶段不改代码、checklist 或 Git 状态。
3. 涉及回忆录产品规则、页面、业务库或业务 API 时，必须跨到 `couple-diary-doc` 读相应事实源，不用 Runtime 计划代替产品设计。

### 在 couple-diary-doc 中

1. 使用 `couple-diary-dev` 装载心约手帐通用开发约定、安全边界和验证规则。
2. 修改回忆录需求/设计时使用 `couple-diary-requirements`；生成总控、前端、后端计划时使用 `couple-diary-planner`。只设计时不写计划或代码，只规划时不实现代码。
3. 涉及 Runtime API、AgentRun、MemoirAgent、Tool/callback、Worker、补偿或隐私治理时，必须跨到 `com-agent-runtime` 读契约、实现和测试，不根据情侣日记代码反向猜 Runtime。

## 选择任务路由

| 任务 | 主事实源 | 必要的另一侧输入 |
|---|---|---|
| 产品需求、密码、列表/详情、页面状态、视觉与交互 | `couple-diary-doc` 回忆录需求与 designs | Runtime 能力/限制，仅用于可行性和契约对齐 |
| Archive/Snapshot、业务表、owner 权限、删除、置顶、轮询接口 | `couple-diary-doc/backend/couple-diary-b` | Runtime 的 run/tool/callback 合同 |
| AgentRun、Worker、Workflow、ModelGateway、Guardrail、Checkpoint、Runtime Artifact | `com-agent-runtime` | 业务后端的 tool/callback 合同和产品限制 |
| 播放器、Action runner、进度/降级体验 | `couple-diary-doc/frontend/couple-diary-f` | PlaybackDocument/Scene/Action 契约与后端安全摘要 |
| 跨项目规划 | 回忆录需求 + Runtime 总控/后端计划 + 两边实现证据 | 先冻结共享契约，再按业务后端→Runtime→前端→联调拆分 |
| 联调/故障诊断 | 两边实际代码、测试、安全日志与契约 fixture | 用 `business_id/archive_id/run_id/event_id/event_seq/status_version/generation_epoch` 安全关联，不复制正文 |

## 交叉契约门

设计或代码进入另一项目前，先对齐：

1. API/Event/Tool/Artifact/PlaybackDocument 的 `contract_version` 或 `schema_version`。
2. AgentRun 状态到业务状态的单向映射，以及 `active_run_id + generation_epoch` 的旧结果拒绝规则。
3. `held create -> 业务绑定 run_id -> start` 握手、每个写操作的独立幂等键、HMAC 签名原文和 key 轮换。
4. Snapshot 是解绑事务冻结的源清单物化结果；Runtime 不直连业务库。
5. 只有 `memory.publish_playback_document` 可原子发布完整 revision 并切换 `published_revision`；成功 callback 不能代替发布事务。
6. 前端不直连 Runtime，不展示 prompt、模型原输出、工具原始载荷或完整 trace。
7. 日记/赌局正文、密码、openid、token、私有 URL 不得进入 Runtime 日志、Artifact、callback、public trace 或测试输出。

文档与实现冲突时，先查 `com-agent-runtime/头脑风暴/docs/AgentRuntime/契约冻结记录.md`、`app/contracts/`、双方 contract tests 和当前路由。旧总控文档中的示意路径不能覆盖已冻结并被测试验证的契约。若仍无法消除冲突，停在契约设计阶段并向用户说明差异，不在两边各写一套兼容分支。

## 实施与验证

1. 先确认当前里程碑。`couple-diary-doc` 的回忆录文档当前只声明“Runtime 连接级验证”，不代表 Archive/Snapshot/Run/Worker/Callback/Published Revision 业务闭环已完成。每次使用时用代码、测试、迁移和 Git 状态重新校准。
2. 先写或调整失败的 contract/behavior test，再做两边最小实现。共享契约改动必须同时覆盖 provider 与 consumer fixture，并记录兼容性结论。
3. 单仓任务先跑相关小测试；跨仓联调至少覆盖：契约版本、签名/幂等、权限隔离、乱序/重复 callback、失败降级、旧 run 拒绝、无素材 baseline、隐私不泄漏和前端不直连 Runtime。
4. Runtime 侧执行相关 pytest、Ruff、Mypy/Alembic 门禁与 `git diff --check`；心约手帐后端执行相关 pytest/Ruff，前端执行改动文件 lint、type-check、小程序构建和相关 contract test。不得用一侧绿灯代替另一侧验证。

## 交付合同

先给跨项目结论，再按仓库分组列出：改动文件、契约变化、实际验证结果、未执行验证与原因、当前里程碑、仍存风险和下一个可独立验收的步骤。不声称未通过代码和测试证明的跨项目闭环已完成。
