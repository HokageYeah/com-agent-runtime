"""FastAPI 路由聚合入口。

R1 路由门禁（2026-08-07）：
- `production` 环境下，本仓 Runtime 只暴露公共 provider 路由（`/runtime/*` 下的 health、
  capabilities、agent-runs）以及工程示例（demo / diary）。回忆录业务相关路由一律不注册：
  - 用户侧 `memory_api.router`（`/memory`）
  - 本地工具 handler `memory_tools_api.router`（`/internal/agent-tools`）
  - 业务回调 consumer `memory_callbacks_api.router`（`/internal/agent-callbacks`）
  - 业务生成状态 `memory_status_api.router`（`/memory-archives`）
- `development` / `test` 仍按现状注册全部历史路由，方便审计、跨仓联调与回归。
- 业务模型、迁移、service 一律不删除；门禁只控制“是否挂载路由”，不改变实现。
- `app.memory_runtime_launcher` 是独立脚本入口（cron/worker 调用），不在 FastAPI 路由表内，
  其生产启停由部署侧 cron/worker 配置控制，本文件不涉及。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.endpoints import (
    agent_runs_api,
    capabilities_api,
    demo_api,
    diary_api,
    health_api,
    memory_api,
    memory_callbacks_api,
    memory_status_api,
    memory_tools_api,
)
from app.core.config import normalize_environment

# 业务路由清单：production 不注册；development / test 注册以保留审计能力。
# 列表集中在此处，避免散落多处 if 分支，符合“消除特殊情况”原则。
_BUSINESS_ROUTERS: tuple[tuple[APIRouter, str, list[str]], ...] = (
    (memory_tools_api.router, "", ["memory-internal-tools"]),
    (memory_api.router, "", ["memory"]),
    (memory_callbacks_api.router, "", ["memory-internal-callbacks"]),
    (memory_status_api.router, "", ["memory-generation-status"]),
)


def _resolve_default_environment() -> str:
    """读取 settings.ENVIRONMENT；测试等无 settings 场景回落到 development。"""
    try:
        from app.core.config import settings  # 延迟导入避免循环依赖
        return settings.ENVIRONMENT
    except Exception:  # pragma: no cover - 配置缺失时仍允许模块加载
        return "development"


def build_api_router(environment: str) -> APIRouter:
    """根据环境构造聚合 router。

    抽出为工厂函数便于路由表测试注入不同 environment，避免依赖全局 settings。
    production 仅挂载公共 provider；development / test 追加业务路由用于审计。
    """
    env = normalize_environment(environment)
    router = APIRouter()

    # 工程示例与业务域样板：模板工程自带，保留注册以便团队协作与 LLM 快速建立上下文。
    router.include_router(demo_api.router, prefix="/demo", tags=["工程示例接口"])
    router.include_router(diary_api.router, prefix="/diary", tags=["日记业务模块"])

    # 公共 AgentRuntime provider：所有环境都必须注册，是 Runtime 的对外契约入口。
    router.include_router(health_api.router, prefix="/runtime", tags=["AgentRuntime"])
    router.include_router(
        capabilities_api.router, prefix="/runtime", tags=["AgentRuntime"]
    )
    router.include_router(
        agent_runs_api.router, prefix="/runtime", tags=["AgentRuntime"]
    )

    # 回忆录业务路由：production fail-closed，仅 development / test 注册以保留审计。
    if env != "production":
        for sub_router, prefix, tags in _BUSINESS_ROUTERS:
            router.include_router(sub_router, prefix=prefix, tags=tags)

    return router


# 模块级全局：兼容现有 `from app.api.api import api_router` 写法。
# 默认按当前 settings.ENVIRONMENT 决定路由表，production 自动收紧。
api_router = build_api_router(_resolve_default_environment())
