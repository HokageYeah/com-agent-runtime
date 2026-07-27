from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.reconciler import ReconcilerRunner
from app.runtime.test_harness import (
    LoopbackTestTransport,
    RuntimeDependencies,
    RuntimeHarnessConfig,
)


def test_harness_config_rejects_non_loopback_transport() -> None:
    factory = sessionmaker(bind=create_engine("sqlite://"))
    config = RuntimeHarnessConfig(
        factory, {"test": {"keys": {"test": "random"}}}, "runtime-test", "http://127.0.0.1:8765"
    )
    assert config.timeout_seconds == 2.0
    try:
        RuntimeHarnessConfig(factory, {"test": {}}, "runtime-test", "https://example.com")
    except ValueError as exc:
        assert str(exc) == "TEST_HARNESS_LOOPBACK_REQUIRED"
    else:
        raise AssertionError("test harness must reject non-loopback transport")


def test_loopback_test_transport_is_explicit_and_rejects_external_url() -> None:
    factory = sessionmaker(bind=create_engine("sqlite://"))
    config = RuntimeHarnessConfig(
        factory, {"test": {"keys": {"test": "random"}}}, "runtime-test", "http://127.0.0.1:8765"
    )
    transport = LoopbackTestTransport(config)
    dependencies = RuntimeDependencies(None, factory, None, None, None, transport)
    assert dependencies.transport_verifier.allows("http://127.0.0.1:8765/health")
    assert not dependencies.transport_verifier.allows("https://example.com/health")


def test_reconciler_harness_uses_explicit_session_factory() -> None:
    factory = sessionmaker(bind=create_engine("sqlite://"))
    config = RuntimeHarnessConfig(factory, {"test": {"keys": {"test": "random"}}}, "runtime-test", "http://127.0.0.1:8765")
    dependencies = RuntimeDependencies(None, factory, None, None, None, LoopbackTestTransport(config))
    assert ReconcilerRunner.from_dependencies(dependencies, "test-owner")._session_factory is factory
