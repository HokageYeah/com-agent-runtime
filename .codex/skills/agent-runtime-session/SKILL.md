---
name: agent-runtime-session
description: 在此仓库的新会话中处理 AgentRuntime 的功能开发、缺陷诊断、集成联调、计划盘点、审查或验证时使用。先装载 AgentRuntime 设计、计划、配置和已验证状态，再按任务性质选择最小必要的 Superpowers、Matt Pocock 或 Claude Code CCG 流程。
---

# AgentRuntime 新会话工作流

将用户紧随 `$agent-runtime-session` 的文字视为本次需求。先说明目标、范围、关键假设和验证标准；需求明确时不要为澄清而停滞，需求会改变安全边界或外部状态时才提问。

## 固定上下文

1. 先读 `AGENTS.md`、`.codex/rules/AI通用编码与协作规范.mdc`；在 Claude Code 中也读 `.claude/rules/AI通用编码与协作规范.md`。
2. 再读 `README.md`、`ENV_CONFIG.md`、`VERIFICATION.md`、`头脑风暴/docs/AgentRuntime/需求设计文档.md`、`头脑风暴/docs/AgentRuntime/plans/2026-07-07-AgentRuntime-总控开发计划.md`、`头脑风暴/docs/AgentRuntime/backend/2026-07-07-AgentRuntime-后端开发计划.md`。
3. 用 `rg`、相关实现、测试、迁移和 `git status --short` 校准文档状态；计划和历史进度说明只是线索，不能代替代码证据。
4. 命中专题时再读对应文件：
   - 工具、Worker、callback：`头脑风暴/docs/AgentRuntime/plans/2026-07-20-Task7-Task5.5-工具安全与Worker宽限期设计说明.md`
   - 路由、授权、模型：`头脑风暴/docs/AgentRuntime/plans/2026-07-20-Task7-Task8-工具写入与路由安全设计说明.md`
   - Archive、Snapshot、播放：`头脑风暴/docs/AgentRuntime/plans/2026-07-20-Task6.5-归档快照与播放文档收尾设计说明.md`
   - 内容安全与质量：`头脑风暴/docs/AgentRuntime/plans/2026-07-20-Task9-回忆录内容安全与质量设计说明.md`

## 不可违反的边界

- 保留脏工作区；禁止 `git reset`、`git checkout`、自动提交、推送、删除数据库或测试数据。除非用户明确要求，不创建分支或 worktree。
- 既有业务库 `couple_diary_dev`、`couple_diary_test`、`couple_diary_prod` 不可迁移、建表或删改。Runtime 仅使用 `couple_diary_agent_runtime_dev`、`couple_diary_agent_runtime_test`、`couple_diary_agent_runtime_prod`。
- 禁止 prompt、模型原文、工具载荷、私有 URL、凭据进入 Store、日志、trace、callback、审计、artifact、checkpoint、错误文本、测试夹具或测试输出；诊断时也不要要求用户粘贴真实密钥或 token。
- 文档 `[ ]` 先审计：区分真实缺口与文档滞后。仅有代码、测试、迁移或可复现验证证据后才改 checklist 或 `VERIFICATION.md`。

## 选择工作流

只调用能降低当前风险的流程；不要叠加多个同类流程。

| 任务情况 | 采用流程 | 不采用 |
| --- | --- | --- |
| 查询、解释、启动配置、单点接口联调 | 直接阅读与最小只读检查 | 不强制任何框架 |
| 计划盘点、下一阶段排序、检查 `[ ]` | `$agent-runtime-next-plan`；仅规划，不改文件 | 开发、TDD、CCG |
| 明确且小范围的修复/功能 | `$execute-development-plan` + `$tdd`；RED→GREEN→REFACTOR | 大型编排、worktree |
| 根因不明的 bug、回归或性能问题 | `$diagnosing-bugs`；复现→最小化→假设→证据→修复→回归 | 直接猜测性修改 |
| 需求模糊、涉及领域术语/跨模块协议 | Matt Pocock `$grilling`；必要时 `$domain-modeling` 或 `$codebase-design` | 未经确认就写实现 |
| 复杂且已确认的多模块开发 | Matt 完整编排（若这些技能已安装）：`grill-with-docs → to-spec → to-tickets → implement（在边界处 tdd）→ code-review`；缺少某一技能时按同一阶段手工执行，不安装或伪造技能 | 未确认需求时直接 implement |
| 外部规范、框架或安全结论需要时效证据 | `$research` 或权威一手文档；技术结论写明来源 | 用搜索结果猜测 |
| 变更完成、合入前审查 | `$code-review`，再执行项目相关门禁 | 只凭“看起来正确”交付 |

### Superpowers 映射

- 设计尚不明确：`superpowers:brainstorming`；需求已经精确时跳过。
- 编码：`superpowers:test-driven-development`；与 `$tdd` 同类，二选一，以仓库可用技能为准。
- 大计划执行：`superpowers:writing-plans`、`superpowers:executing-plans` 或 `subagent-driven-development`；仅当任务可安全拆分且当前运行环境允许委派时使用。
- 调试：`superpowers:systematic-debugging`；与 `$diagnosing-bugs` 二选一。
- 完成前：`superpowers:verification-before-completion`；不得因此自动提交或结束分支。
- 禁用 `using-git-worktrees`、`finishing-a-development-branch`，除非用户本次明确授权其副作用。

### CCG 说明

CCG 是以 **Claude Code** 为主控的多模型工作流；本项目的 Codex 环境没有 `/ccg:*` 命令，Codex 会话不得假装调用它。在 Claude Code 中，只有确认已安装 CCG、用户明确要求 CCG 或任务确属高复杂多模块时，才使用 `/ccg:go`；简单修复保持 direct-fix/quick-implement，复杂未知 bug 选 debug-investigate，计划须经用户批准后才能执行。仍以上述安全边界为准，禁止 CCG 自动提交、回滚或清理分支。

## 开发与验证

1. 对代码变更先新增/调整失败测试；按影响补 SQLite、PostgreSQL、Redis、真实 Worker 或 Docker harness 回归。
2. 只做需求所需的最小实现，新增核心逻辑、字段和状态转换添加清晰中文注释；日志只记录安全元数据。
3. 优先跑最小相关测试，再跑相称门禁：`poetry run pytest`、Ruff、Mypy、Alembic 单 head、`git diff --check`。Docker harness 每轮后 `down -v` 清理。
4. 仅在命令真实执行且结果充分时更新 `VERIFICATION.md`，写入用户可运行的命令、预期结果和仍需人工验证项。

## 交付

先给结论，再列：改动文件、已执行验证及结果、未执行验证与原因、人工操作命令和预期。涉及新会话交接时，引用本技能和本轮具体证据，不复制敏感配置值。
