"""Task 12 测试进程编排的显式依赖声明；生产入口不得使用此模块。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RuntimeHarnessConfig:
    """测试进程只能使用临时数据库、随机身份与 loopback mock transport。"""

    session_factory: Any
    trusted_clients: dict[str, dict[str, object]]
    runtime_id: str
    mock_base_url: str
    timeout_seconds: float = 2.0
    provider_base_url: str | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.mock_base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("TEST_HARNESS_LOOPBACK_REQUIRED")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 10:
            raise ValueError("TEST_HARNESS_TIMEOUT_INVALID")
        if not self.runtime_id or not self.trusted_clients:
            raise ValueError("TEST_HARNESS_IDENTITY_INVALID")
        if self.provider_base_url is not None:
            provider = urlsplit(self.provider_base_url)
            if (
                provider.scheme != "http"
                or provider.hostname not in {"127.0.0.1", "::1", "localhost"}
                or not provider.port
            ):
                raise ValueError("TEST_HARNESS_LOOPBACK_REQUIRED")


class TransportVerifier(Protocol):
    """测试与生产 transport 的公共最小契约，不能从请求体选择 verifier。"""

    def allows(self, url: str) -> bool: ...


@dataclass(frozen=True)
class LoopbackTestTransport:
    """仅 harness 显式创建时允许回环 mock；生产路径绝不装配。"""

    config: RuntimeHarnessConfig

    def allows(self, url: str) -> bool:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
            and bool(parsed.port)
        )


@dataclass(frozen=True)
class RuntimeDependencies:
    """进程 harness 的不可变依赖集；避免通过全局环境偷偷改变运行行为。"""

    settings: Any
    session_factory: Any
    clock: Any
    callback_client: Any
    tool_client: Any
    transport_verifier: TransportVerifier
    provider_adapter: Any | None = None
