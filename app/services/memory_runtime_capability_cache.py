"""回忆录 Runtime 的短期兼容能力缓存。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class RuntimeCapabilityError(ValueError):
    """Runtime 未就绪或能力不兼容时使用的安全错误码。"""


@dataclass(frozen=True)
class RuntimeCapabilitySnapshot:
    """只缓存可兼容性判断所需版本和开关，不缓存 URL、密钥或额度。"""

    contract_version: str
    package_digest: str
    agent_versions: frozenset[tuple[str, str]]
    model_policies: frozenset[str]
    workflow_agent: bool
    expires_at: datetime


class MemoryRuntimeCapabilityCache:
    """单槽进程内缓存；多实例各自刷新即可，不参与业务状态正确性。"""

    def __init__(
        self, *, ttl_seconds: int, required_policies: set[str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._required_policies = frozenset(required_policies)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._snapshot: RuntimeCapabilitySnapshot | None = None

    def get_or_refresh(
        self, fetcher: Callable[[], RuntimeCapabilitySnapshot],
        *, probe: Callable[[], RuntimeCapabilitySnapshot] | None = None,
    ) -> RuntimeCapabilitySnapshot:
        """TTL 内复用摘要；探测到安全版本摘要变化时立即重新握手。"""
        now = self._clock()
        if self._snapshot is not None and now < self._snapshot.expires_at:
            if probe is None or self._fingerprint(probe()) == self._fingerprint(self._snapshot):
                return self._snapshot
        fetched = fetcher()
        self._validate(fetched)
        self._snapshot = RuntimeCapabilitySnapshot(
            contract_version=fetched.contract_version,
            package_digest=fetched.package_digest,
            agent_versions=fetched.agent_versions,
            model_policies=fetched.model_policies,
            workflow_agent=fetched.workflow_agent,
            expires_at=now + self._ttl,
        )
        return self._snapshot

    @staticmethod
    def _fingerprint(snapshot: RuntimeCapabilitySnapshot) -> tuple[object, ...]:
        """仅比较无敏感信息的协议、包和允许能力摘要。"""
        return (
            snapshot.contract_version, snapshot.package_digest,
            snapshot.agent_versions, snapshot.model_policies, snapshot.workflow_agent,
        )

    def _validate(self, snapshot: RuntimeCapabilitySnapshot) -> None:
        """拒绝 major 漂移、缺失 MemoirAgent、策略或 workflow 能力。"""
        if (
            snapshot.contract_version.split(".", 1)[0] != "1"
            or ("memoir_agent", "1.0.0") not in snapshot.agent_versions
            or not self._required_policies.issubset(snapshot.model_policies)
            or not snapshot.workflow_agent
        ):
            raise RuntimeCapabilityError("MEMORY_RUNTIME_CAPABILITY_INCOMPATIBLE")
