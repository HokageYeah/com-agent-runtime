---
name: agent-runtime-next-plan
description: Use when planning the next AgentRuntime development block in this repository from the master/backend plans, current code, tests, and unchecked or stale checklist items.
---

# AgentRuntime 下一阶段规划

基于已验证的实现状态给出可执行的下一大块开发建议；计划文档是线索，不是已完成的证据。

## 必读输入

- `README.md`、`头脑风暴/docs/AgentRuntime/需求设计文档.md`。
- `头脑风暴/docs/AgentRuntime/plans/2026-07-07-AgentRuntime-总控开发计划.md`。
- `头脑风暴/docs/AgentRuntime/backend/2026-07-07-AgentRuntime-后端开发计划.md`。
- 当前 `git status --short`、相关测试和实现文件；优先用 `rg`。可用时使用 ace-tool/codegraph 追踪关联。

## 工作流

1. 列出各 Task 的 `[ ]`、`[✅]` 和父子状态；不要把父项未勾选直接判定为未实现。
2. 以代码、测试、迁移和现有 diff 验证关键子项。区分：已验收、部分实现、文档滞后、确实未开始。
3. 按依赖、安全风险、用户价值和可独立验收性排序。先提出必须先补的“小收尾”，再给出一个推荐的“下一大块”。
4. 输出未完成模块清单、推荐顺序、每块的目标/边界/风险/完成标准；说明哪些 `[ ]` 保持不动及原因。
5. 规划请求只读：不改代码、不改 checklist、不执行提交。用户明确要求开发时，转用 `$execute-development-plan`。

## 输出格式

- 先给出“下一大块：Task X — 名称”和一句原因。
- 用紧凑表格列出优先级、模块、真实状态、建议。
- 结尾给出 3–6 步开发顺序和一段可直接交给 `$execute-development-plan` 的任务描述。
- 文件链接使用绝对路径；不泄露日记正文、prompt 或播放文档内容。
