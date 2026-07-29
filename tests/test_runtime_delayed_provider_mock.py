from __future__ import annotations

import socket

import httpx
import pytest

from app.runtime.model_gateway import ModelRoute
from app.runtime.process_harness import ProcessHarness
from app.runtime.test_harness import LoopbackTestTransport, RuntimeHarnessConfig
from app.runtime.test_provider import LoopbackProviderAdapter, TestModelRoute


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _route() -> ModelRoute:
    return ModelRoute(
        route_id="test", provider="test", model="test", endpoint="https://provider.example/v1",
        rate_limit_key="test", max_concurrency=1, rpm_limit=1, tpm_limit=10,
        timeout_seconds=2, permit_ttl_seconds=20, settle_margin_seconds=1,
        price_unit="usd_per_1k_tokens", input_price=0, output_price=0,
    )


def test_delayed_provider_mock_blocks_once_without_retaining_request_body() -> None:
    try:
        port = _port()
    except PermissionError:
        pytest.skip("当前受限环境禁止绑定 loopback 端口")
    with ProcessHarness(timeout_seconds=5) as harness:
        harness.start_mock_provider(port)
        base_url = f"http://127.0.0.1:{port}"
        control = {"X-Harness-Control": harness.identity_id}
        assert httpx.post(f"{base_url}/__harness__/block-next-model", headers=control, timeout=2).status_code == 202
        config = RuntimeHarnessConfig(object(), {"test": {}}, "runtime", base_url)
        adapter = LoopbackProviderAdapter(config, LoopbackTestTransport(config), httpx.Client())
        # provider 已收请求但未返回，控制状态中只能出现布尔计数，不能出现 request 正文。
        import threading
        result: list[object] = []
        thread = threading.Thread(target=lambda: result.append(adapter.call(TestModelRoute(_route(), base_url), {"prompt": "private"}, timeout_seconds=3)))
        thread.start()
        for _ in range(20):
            state = httpx.get(f"{base_url}/state", timeout=2).json()
            if state["model_started"]:
                break
        assert state == {"model_blocked": True, "model_started": True, "model_calls": 1}
        assert httpx.post(f"{base_url}/__harness__/release-model", headers=control, timeout=2).status_code == 202
        thread.join(3)
        assert not thread.is_alive()
        assert result == [{"request_id": "harness-model"}]


def test_delayed_provider_mock_can_block_only_the_second_repair_attempt() -> None:
    try:
        port = _port()
    except PermissionError:
        pytest.skip("当前受限环境禁止绑定 loopback 端口")
    with ProcessHarness(timeout_seconds=5) as harness:
        harness.start_mock_provider(port)
        base_url = f"http://127.0.0.1:{port}"
        control = {"X-Harness-Control": harness.identity_id}
        assert httpx.post(
            f"{base_url}/__harness__/block-repair-after-invalid",
            headers=control,
            timeout=2,
        ).status_code == 202
        config = RuntimeHarnessConfig(object(), {"test": {}}, "runtime", base_url)
        adapter = LoopbackProviderAdapter(
            config,
            LoopbackTestTransport(config),
            httpx.Client(),
        )
        route = TestModelRoute(_route(), base_url)

        assert adapter.call(
            route,
            {"messages": []},
            timeout_seconds=3,
        ) == {"invalid": True}

        import threading

        result: list[object] = []
        thread = threading.Thread(
            target=lambda: result.append(
                adapter.call(
                    route,
                    {"messages": []},
                    timeout_seconds=3,
                ),
            ),
        )
        thread.start()
        for _ in range(20):
            state = httpx.get(f"{base_url}/state", timeout=2).json()
            if state["model_started"]:
                break
        assert state == {
            "model_blocked": True,
            "model_started": True,
            "model_calls": 2,
        }
        assert httpx.post(
            f"{base_url}/__harness__/release-model",
            headers=control,
            timeout=2,
        ).status_code == 202
        thread.join(3)
        assert not thread.is_alive()
        assert result == [{"source_refs": []}]
