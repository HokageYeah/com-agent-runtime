# Reconciler Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Reconciler 具备周期运行、多实例互斥、usage/Admission 对账与安全观测能力。

**Architecture:** 数据库租约控制扫描所有权；每轮短事务运行既有规则对账，再条件修复 usage 与 Admission 漂移；报告只输出安全计数。

**Tech Stack:** Python、SQLAlchemy、pytest、Ruff。

## Global Constraints

- 不记录 prompt、模型正文、日记、快照、播放文档或 callback payload。
- 所有修复必须条件更新，更新失败不写副作用。
- 不实现授权版本主动扫描或外部告警平台。

### Task 1: 扫描租约与周期入口

- [ ] 写两实例互斥、lease 到期接管、`--once` 与循环间隔的失败测试。
- [ ] 实现持久租约模型/迁移、`ReconciliationLeaseService` 和 `app.reconciler` 周期入口。
- [ ] 运行租约与入口测试。

### Task 2: Usage 与 Admission 运营对账

- [ ] 写 running usage 条件转 unknown、Admission 漂移修复/并发 version 失败的测试。
- [ ] 实现 ModelUsage 对账与 Admission 聚合修复，扩展安全报告和连续失败告警计数。
- [ ] 运行 Reconciler 聚焦测试。

### Task 3: 文档与全量验证

- [ ] 更新总控计划已完成项，保留 callback 重放、授权扫描与外部告警 `[ ]`。
- [ ] 运行 `poetry run pytest -q && poetry run ruff check app tests alembic && git diff --check`。
