import httpx
import pytest

from app.runtime.model_gateway import ModelRoute
from app.runtime.test_harness import (
    LoopbackTestTransport,
    RuntimeDependencies,
    RuntimeHarnessConfig,
)
from app.runtime.test_provider import LoopbackProviderAdapter, TestModelRouteFactory


def _route(endpoint: str = "https://provider.example") -> ModelRoute:
    return ModelRoute("test", "provider", "model", endpoint, "provider:test", 1, 1, 1, 1, 2, 1, "usd_per_1k_tokens", 0, 0)


def test_production_route_rejects_loopback() -> None:
    with pytest.raises(ValueError, match="MODEL_ENDPOINT_UNSAFE"):
        _route("http://127.0.0.1:8765")


def test_explicit_harness_route_calls_loopback_mock_without_logging_body() -> None:
    config = RuntimeHarnessConfig(object(), {"test": {"keys": {"test": "random"}}}, "runtime-test", "http://127.0.0.1:8765")
    transport = LoopbackTestTransport(config)
    adapter = LoopbackProviderAdapter(config, transport, httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))))
    dependencies = RuntimeDependencies(None, config.session_factory, None, None, None, transport, adapter)
    assert dependencies.provider_adapter is adapter
    assert adapter.call(TestModelRouteFactory(config, transport).create(_route()), {"prompt": "private-marker"}) == {"ok": True}


def test_explicit_harness_adapter_accepts_production_model_route_contract() -> None:
    """Worker 的 ModelGateway 传入 ModelRoute，adapter 仍只能发往已配置回环 mock。"""
    config = RuntimeHarnessConfig(
        object(), {"test": {"keys": {"test": "random"}}}, "runtime-test",
        "http://127.0.0.1:8765", provider_base_url="http://127.0.0.1:8766",
    )
    transport = LoopbackTestTransport(config)
    adapter = LoopbackProviderAdapter(
        config, transport,
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))),
    )

    assert adapter.call(_route(), {"private": "not retained"}, timeout_seconds=1) == {"ok": True}
