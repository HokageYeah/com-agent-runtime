"""回忆录 Runtime 能力缓存仅保存安全兼容性摘要。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.memoir.memory_runtime_capability_cache import (
    MemoryRuntimeCapabilityCache,
    RuntimeCapabilityError,
    RuntimeCapabilitySnapshot,
)


def _snapshot(*, expires_at: datetime, contract_version: str = "1.0.0") -> RuntimeCapabilitySnapshot:
    return RuntimeCapabilitySnapshot(
        contract_version=contract_version, package_digest="sha256:memoir",
        agent_versions=frozenset({("memoir_agent", "1.0.1")}),
        model_policies=frozenset({"emotional_writing", "strict"}),
        workflow_agent=True, expires_at=expires_at,
    )


def test_capability_cache_reuses_valid_snapshot_then_refreshes_after_ttl() -> None:
    now = datetime(2026, 7, 20, tzinfo=UTC)
    clock = [now]
    calls = []
    cache = MemoryRuntimeCapabilityCache(
        ttl_seconds=60, required_policies={"emotional_writing", "strict"},
        clock=lambda: clock[0],
    )

    def fetch() -> RuntimeCapabilitySnapshot:
        calls.append(1)
        return _snapshot(expires_at=clock[0] + timedelta(seconds=60))

    assert cache.get_or_refresh(fetch).package_digest == "sha256:memoir"
    assert cache.get_or_refresh(fetch).package_digest == "sha256:memoir"
    clock[0] += timedelta(seconds=61)
    cache.get_or_refresh(fetch)
    assert len(calls) == 2


def test_capability_cache_refreshes_immediately_when_probe_digest_changes() -> None:
    """TTL 未到期时，部署探测到新 package digest 也必须立即重新握手。"""
    now = datetime(2026, 7, 20, tzinfo=UTC)
    calls: list[int] = []
    cache = MemoryRuntimeCapabilityCache(
        ttl_seconds=60, required_policies={"emotional_writing", "strict"},
        clock=lambda: now,
    )

    def fetch() -> RuntimeCapabilitySnapshot:
        calls.append(1)
        return _snapshot(expires_at=now + timedelta(seconds=60))

    cache.get_or_refresh(fetch)
    refreshed = cache.get_or_refresh(
        fetch,
        probe=lambda: RuntimeCapabilitySnapshot(
            contract_version="1.0.0", package_digest="sha256:new-memoir",
            agent_versions=frozenset({("memoir_agent", "1.0.1")}),
            model_policies=frozenset({"emotional_writing", "strict"}),
            workflow_agent=True, expires_at=now,
        ),
    )

    assert len(calls) == 2
    assert refreshed.package_digest == "sha256:memoir"


@pytest.mark.parametrize("snapshot", [
    _snapshot(expires_at=datetime(2026, 7, 20, tzinfo=UTC), contract_version="2.0.0"),
    RuntimeCapabilitySnapshot("1.0.0", "d", frozenset(), frozenset({"strict"}), True, datetime(2026, 7, 20, tzinfo=UTC)),
])
def test_capability_cache_rejects_incompatible_runtime(snapshot: RuntimeCapabilitySnapshot) -> None:
    cache = MemoryRuntimeCapabilityCache(ttl_seconds=60, required_policies={"emotional_writing", "strict"})
    with pytest.raises(RuntimeCapabilityError, match="MEMORY_RUNTIME_CAPABILITY_INCOMPATIBLE"):
        cache.get_or_refresh(lambda: snapshot)
