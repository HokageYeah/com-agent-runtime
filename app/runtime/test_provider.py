"""仅供 Task 12 harness 使用的 loopback Provider；生产装配不得导入。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.runtime.model_gateway import ModelRoute
from app.runtime.test_harness import LoopbackTestTransport, RuntimeHarnessConfig


@dataclass(frozen=True)
class TestModelRoute:
    """包装已通过生产字段校验的 route，并把网络目标限定为 harness mock。"""

    route: ModelRoute
    endpoint: str

    __test__ = False


class TestModelRouteFactory:
    """必须同时持有 harness config 与 loopback transport，不能由 Settings 调用。"""

    __test__ = False

    def __init__(self, config: RuntimeHarnessConfig, transport: LoopbackTestTransport) -> None:
        if transport.config is not config:
            raise ValueError("TEST_HARNESS_TRANSPORT_MISMATCH")
        self._config, self._transport = config, transport

    def create(self, route: ModelRoute) -> TestModelRoute:
        if not self._transport.allows(self._config.mock_base_url):
            raise ValueError("TEST_HARNESS_LOOPBACK_REQUIRED")
        return TestModelRoute(route=route, endpoint=self._config.mock_base_url)


class LoopbackProviderAdapter:
    """只向 harness mock 发送请求，不复用生产 HttpProviderAdapter 的放宽路径。"""

    def __init__(self, config: RuntimeHarnessConfig, transport: LoopbackTestTransport, client: httpx.Client) -> None:
        if transport.config is not config:
            raise ValueError("TEST_HARNESS_TRANSPORT_MISMATCH")
        self._config, self._transport, self._client = config, transport, client

    def call(self, route: TestModelRoute | ModelRoute, request: object, *, timeout_seconds: float | None = None) -> object:
        endpoint = (
            route.endpoint
            if isinstance(route, TestModelRoute)
            else self._config.provider_base_url
        )
        if not isinstance(endpoint, str) or not self._transport.allows(endpoint):
            raise ValueError("TEST_HARNESS_LOOPBACK_REQUIRED")
        response = self._client.post(endpoint, json=request, timeout=timeout_seconds or self._config.timeout_seconds, follow_redirects=False)
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, (dict, list)):
            raise ValueError("TEST_PROVIDER_RESPONSE_INVALID")
        return payload
