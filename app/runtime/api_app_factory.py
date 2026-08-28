"""Runtime API 应用工厂；只使用调用方显式注入的测试依赖。"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.api import build_api_router
from app.api.endpoints.health_api import RuntimeHealth
from app.runtime.test_harness import RuntimeDependencies
from app.services.memoir.memory_archive_service import FernetSnapshotCipher


def create_runtime_app(
    dependencies: RuntimeDependencies,
) -> FastAPI:
    """使用唯一显式 Runtime 依赖集创建 API，不读全局配置。"""
    runtime_settings = dependencies.settings
    session_factory = dependencies.session_factory

    runtime_app = FastAPI(title="AgentRuntime test harness")
    runtime_app.state.settings = runtime_settings
    runtime_app.state.session_factory = session_factory
    runtime_app.state.memory_snapshot_cipher = FernetSnapshotCipher(
        runtime_settings.MEMORY_SNAPSHOT_FERNET_KEY.encode()
    )
    runtime_app.state.runtime_health = RuntimeHealth(
        runtime_settings, database_ready=lambda: (True, {"database": "ready"})
    )
    runtime_app.include_router(
        build_api_router(runtime_settings.ENVIRONMENT),
        prefix=runtime_settings.application.api_prefix,
    )
    return runtime_app
