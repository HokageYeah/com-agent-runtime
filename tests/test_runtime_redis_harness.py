"""真实 Redis 回归；仅在显式隔离的 AGENT_RUNTIME_TEST_REDIS_URL 下执行。"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from app.runtime.model_gateway import ModelRoute, ProviderTrafficController


@pytest.fixture
def redis_client():
    url = os.environ.get("AGENT_RUNTIME_TEST_REDIS_URL")
    if not url:
        pytest.skip("未显式提供 AGENT_RUNTIME_TEST_REDIS_URL")
    from redis import Redis
    client = Redis.from_url(url, decode_responses=False)
    try:
        client.ping()
    except Exception as exc:
        pytest.skip(f"隔离 Redis 不可用: {type(exc).__name__}")
    yield client
    for key in client.scan_iter(match="model_gateway:*"):
        client.delete(key)


def _route(key: str) -> ModelRoute:
    return ModelRoute(
        route_id="redis-harness", provider="harness", model="test", endpoint="https://provider.example/v1",
        rate_limit_key=key, max_concurrency=1, rpm_limit=2, tpm_limit=10,
        timeout_seconds=2, permit_ttl_seconds=15, settle_margin_seconds=1,
        price_unit="usd_per_1k_tokens", input_price=0, output_price=0,
    )


def test_real_redis_shares_permit_cooldown_and_fail_closed_boundary(redis_client) -> None:
    route = _route(f"redis-harness:{uuid4().hex}")
    first, second = ProviderTrafficController(redis_client), ProviderTrafficController(redis_client)

    assert first.acquire(route, "first", estimated_tokens=5).granted
    assert second.acquire(route, "second", estimated_tokens=1).status == "concurrency_exceeded"
    assert first.mark_started(route, "first").status == "started"
    assert first.settle(route, "first", retry_after_seconds=2).status == "settled"
    assert second.acquire(route, "after-cooldown", estimated_tokens=1).status == "blocked"


def test_real_redis_fallback_route_keeps_independent_cooldown_and_permit(
    redis_client,
) -> None:
    """主 route 的 429 冷却不得污染部署显式 fallback 的独立流量分区。"""
    primary = _route(f"redis-primary:{uuid4().hex}")
    fallback = ModelRoute(
        **{
            **primary.__dict__,
            "route_id": "redis-fallback",
            "rate_limit_key": f"redis-fallback:{uuid4().hex}",
        }
    )
    controller = ProviderTrafficController(redis_client)

    assert controller.acquire(primary, "primary", estimated_tokens=5).granted
    assert controller.mark_started(primary, "primary").status == "started"
    assert (
        controller.settle(primary, "primary", retry_after_seconds=2).status
        == "settled"
    )
    assert (
        controller.acquire(primary, "primary-blocked", estimated_tokens=1).status
        == "blocked"
    )
    assert controller.acquire(fallback, "fallback", estimated_tokens=1).granted
    assert controller.settle(fallback, "fallback").status == "settled"
