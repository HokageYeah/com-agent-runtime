from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import FastAPI

import app.models as runtime_models  # noqa: F401
from app.api.router import router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.base import Base
from app.db.session import create_runtime_engine, create_session_factory


@dataclass
class RuntimeHealth:
    """把 readiness 判定集中到可替换对象，便于测试和未来接入真实依赖。"""

    settings: Settings
    draining: bool = False

    def begin_draining(self) -> None:
        """停止新 claim；已有 Worker 仍可 heartbeat，保证优雅收敛。"""
        self.draining = True
        logging.warning("Runtime 进入 draining，停止新的 Worker claim")

    def check_ready(self) -> tuple[bool, dict[str, str]]:
        # 此阶段先校验“依赖是否已配置”；Task 2 接入数据库后会替换为 schema 探活。
        checks: dict[str, str] = {
            "database": "configured" if self.settings.database_url else "missing",
            "registry": "configured" if self.settings.agent_package_root else "missing",
            "signature": "configured" if self.settings.trusted_clients else "missing",
            "outbox_handlers": "configured",
        }
        registered_handlers = {"run_dispatch"}
        missing_handlers = (
            set(self.settings.enabled_outbox_event_types) - registered_handlers
        )
        if missing_handlers:
            # 未注册 handler 的 outbox 不可被 worker 正确消费，必须拒绝就绪。
            checks["outbox_handlers"] = "missing"
        if self.draining:
            checks["draining"] = "true"
        if self.settings.environment == "production":
            checks["audit_sink"] = (
                "configured" if self.settings.audit_sink_dsn else "missing"
            )
            checks["audit_access"] = (
                "configured" if self.settings.audit_allowed_roles else "missing"
            )
        ready = all(value == "configured" for value in checks.values())
        logging.info(
            "Runtime readiness 计算 runtime_id=%s ready=%s",
            self.settings.runtime_id,
            ready,
        )
        return ready, checks


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建独立 Runtime 应用，不复用情侣日记主后端的 app 或数据库连接。"""
    runtime_settings = settings or get_settings()
    configure_logging()
    app = FastAPI(title="AgentRuntime", version="1.0.0")
    app.state.settings = runtime_settings
    # 开发/测试可直接建表；生产部署仍应以 Alembic upgrade 为唯一 schema 入口。
    engine = create_runtime_engine(runtime_settings.database_url)
    if runtime_settings.environment != "production":
        Base.metadata.create_all(engine)
    app.state.session_factory = create_session_factory(engine)
    app.state.runtime_health = RuntimeHealth(runtime_settings)
    logging.info("创建 AgentRuntime 应用 runtime_id=%s", runtime_settings.runtime_id)
    app.include_router(router)
    return app


app = create_app()
