"""测试专用 Runtime 应用工厂不得依赖全局数据库或环境变量。"""

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.runtime import api_app_factory
from app.runtime.api_app_factory import create_runtime_app
from app.runtime.test_harness import (
    LoopbackTestTransport,
    RuntimeDependencies,
    RuntimeHarnessConfig,
)


def _runtime_settings(*, environment: str = "test") -> Settings:
    return Settings(
        _env_file=None,
        ENVIRONMENT=environment,
        MEMORY_SNAPSHOT_FERNET_KEY=Fernet.generate_key().decode(),
    )


def test_create_runtime_app_uses_explicit_session_factory() -> None:
    engine = create_engine("sqlite://")
    factory = sessionmaker(bind=engine)
    app = create_runtime_app(
        runtime_settings=_runtime_settings(), session_factory=factory
    )

    with TestClient(app) as client:
        assert client.get("/api/v1/runtime/health/live").status_code == 200
    assert app.state.session_factory is factory


def test_create_runtime_app_accepts_explicit_dependencies() -> None:
    factory = sessionmaker(bind=create_engine("sqlite://"))
    harness = RuntimeHarnessConfig(factory, {"test": {"keys": {"test": "random"}}}, "runtime-test", "http://127.0.0.1:8765")
    dependencies = RuntimeDependencies(
        _runtime_settings(),
        factory,
        None,
        None,
        None,
        LoopbackTestTransport(harness),
    )
    assert create_runtime_app(dependencies=dependencies).state.session_factory is factory


def test_create_runtime_app_uses_injected_environment_without_global_settings(
    monkeypatch,
) -> None:
    """工厂路由与密钥只来自注入设置，不触碰全局 settings。"""
    import app.core.config as config

    class _RejectGlobalSettingsAccess:
        def __getattribute__(self, name: str) -> object:
            if name.startswith("__"):
                return object.__getattribute__(self, name)
            raise AssertionError(f"禁止读取全局 settings: {name}")

    monkeypatch.setattr(config, "settings", _RejectGlobalSettingsAccess())
    factory = sessionmaker(bind=create_engine("sqlite://"))
    runtime_app = create_runtime_app(
        runtime_settings=_runtime_settings(environment="production"),
        session_factory=factory,
    )
    paths = set(runtime_app.openapi()["paths"])

    assert not hasattr(api_app_factory, "settings")
    assert "/api/v1/runtime/health/live" in paths
    assert not any(path.startswith("/api/v1/memory") for path in paths)
