"""测试专用 Runtime 应用工厂不得依赖全局数据库或环境变量。"""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.main import create_runtime_app
from app.runtime.test_harness import (
    LoopbackTestTransport,
    RuntimeDependencies,
    RuntimeHarnessConfig,
)


def test_create_runtime_app_uses_explicit_session_factory() -> None:
    engine = create_engine("sqlite://")
    factory = sessionmaker(bind=engine)
    app = create_runtime_app(runtime_settings=settings, session_factory=factory)

    with TestClient(app) as client:
        assert client.get("/api/v1/runtime/health/live").status_code == 200
    assert app.state.session_factory is factory


def test_create_runtime_app_accepts_explicit_dependencies() -> None:
    factory = sessionmaker(bind=create_engine("sqlite://"))
    harness = RuntimeHarnessConfig(factory, {"test": {"keys": {"test": "random"}}}, "runtime-test", "http://127.0.0.1:8765")
    dependencies = RuntimeDependencies(settings, factory, None, None, None, LoopbackTestTransport(harness))
    assert create_runtime_app(dependencies=dependencies).state.session_factory is factory
