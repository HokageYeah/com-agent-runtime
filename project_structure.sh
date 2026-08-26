#!/bin/bash

set -euo pipefail

# 只输出稳定的架构边界，不枚举易随版本变化的每个文件。
echo "当前工程: com-agent-runtime（公共 Agent 执行服务）"
echo "下面输出稳定目录边界与关键入口："
echo

cat <<'EOF'
.
├── alembic/                 # Runtime 权威数据库迁移谱系
├── app/
│   ├── agents/              # 版本化、不可变的 AgentPackage
│   ├── api/                 # FastAPI 路由聚合与环境路由门禁
│   ├── contracts/           # API/Event/Tool/Artifact 契约与 schema 导出
│   ├── core/                # 配置、认证、授权、connector、日志与安全策略
│   ├── db/                  # SQLAlchemy 会话、metadata 与迁移 bootstrap
│   ├── middleware/          # 请求日志与统一异常处理
│   ├── models/              # Runtime 权威模型及保留的迁移模型
│   ├── runtime/             # Planner/Executor/Gateway/Checkpoint/Guardrail 内核
│   ├── schemas/             # Pydantic 边界模型
│   ├── scripts/             # Runtime CLI、环境与迁移辅助入口
│   ├── services/            # Run/outbox/lease/callback/audit/reconciliation 服务
│   ├── dispatcher.py        # outbox 派发进程
│   ├── worker.py            # Runtime Worker
│   ├── reconciler.py        # 对账进程
│   └── main.py              # FastAPI 应用入口
├── tests/
│   └── fixtures/            # Runtime、Tool、Playback 与 Snapshot 合同夹具
├── docs/superpowers/        # 已执行的专题设计与实施记录
├── 头脑风暴/docs/AgentRuntime/ # 当前需求、冻结契约与总控计划
├── agent-runtime.sh         # configure/doctor/prepare/register/start/verify 统一入口
├── ENV_CONFIG.md            # 敏感与条件配置说明
├── VERIFICATION.md          # 验证命令与预期
└── pyproject.toml           # Python 依赖与质量门禁

关键约定：
1. 公共 Runtime 能力沿 contracts/api/runtime/services 边界扩展。
2. 回忆录业务事实属于 couple-diary-b；本仓 memory 业务代码只作迁移和回归证据。
3. demo/diary 模块不是新增 Runtime 能力的推荐样板。
4. 不创建第二套 app、alembic 或嵌套 Runtime 工程。
5. 建库、迁移、Package 注册和完整启动统一使用 agent-runtime.sh。
EOF

echo
echo "完整架构、所有权与历史兼容边界请阅读 README.md。"
