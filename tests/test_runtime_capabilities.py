from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

import pytest

from app.api.endpoints.health_api import RuntimeHealth
from app.core.config import settings

_CAPABILITIES_PATH = "/api/v1/runtime/capabilities"
_FIXTURE_SECRETS = ("development-secret", "runtime-tool-development-secret")


def _runtime_capability_headers(
    timestamp: str, hmac_secret: bytes = b"development-secret"
) -> dict[str, str]:
    """构造 capabilities 探针的服务签名，测试拒绝路径时可替换 HMAC 密钥。"""
    canonical = f"GET\n{_CAPABILITIES_PATH}\n{timestamp}\n{hashlib.sha256(b'').hexdigest()}"
    return {
        "X-Agent-Client-Id": "couple-diary",
        "X-Agent-Key-Id": "dev",
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Signature": hmac.new(
            hmac_secret, canonical.encode(), hashlib.sha256
        ).hexdigest(),
    }


def test_runtime_capabilities_rejects_invalid_service_signatures_without_secret_leakage(
    client,
) -> None:
    """未签名、错误 HMAC 和过期签名均不得泄露测试夹具中的服务密钥。"""
    timestamp = str(int(datetime.now(UTC).timestamp()))
    responses = (
        client.get(_CAPABILITIES_PATH),
        client.get(
            _CAPABILITIES_PATH,
            headers=_runtime_capability_headers(timestamp, b"wrong-hmac-key"),
        ),
        client.get(_CAPABILITIES_PATH, headers=_runtime_capability_headers("0")),
    )

    for response in responses:
        assert response.status_code == 401
        serialized = str(response.json())
        assert all(secret not in serialized for secret in _FIXTURE_SECRETS)


@pytest.mark.parametrize(
    "header_name",
    (
        "X-Agent-Client-Id",
        "X-Agent-Key-Id",
        "X-Agent-Timestamp",
        "X-Agent-Signature",
    ),
)
def test_runtime_capabilities_rejects_duplicate_service_auth_header(
    client, header_name: str
) -> None:
    """认证前拒绝重复大小写变体，不能让 HTTP 层任意选择其中一个值。"""

    timestamp = str(int(datetime.now(UTC).timestamp()))
    signed_headers = _runtime_capability_headers(timestamp)
    headers = [
        *signed_headers.items(),
        (header_name.lower(), signed_headers[header_name]),
    ]

    response = client.get(_CAPABILITIES_PATH, headers=headers)

    assert response.status_code == 401


def test_runtime_capabilities_requires_valid_service_signature(client) -> None:
    """能力清单属于服务间协商数据，不能仅凭伪造 client id 获取。"""
    timestamp = str(int(datetime.now(UTC).timestamp()))
    headers = {"X-Agent-Client-Id": "couple-diary"}

    assert client.get(_CAPABILITIES_PATH, headers=headers).status_code == 401
    response = client.get(
        _CAPABILITIES_PATH, headers=_runtime_capability_headers(timestamp)
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["contract_version"] == "1.0.0"
    assert payload["agents"] == [{"agent_id": "memoir_agent", "version": "1.0.5"}]
    assert set(payload["capabilities"]) == {
        "workflow_agent",
        "native_sse",
        "media",
        "model_enhancement_available",
    }
    serialized = str(payload)
    assert all(secret not in serialized for secret in _FIXTURE_SECRETS)


def test_runtime_capabilities_exposes_only_safe_model_enhancement_summary(client) -> None:
    timestamp = str(int(datetime.now(UTC).timestamp()))
    response = client.get(
        _CAPABILITIES_PATH, headers=_runtime_capability_headers(timestamp)
    )

    assert response.status_code == 200
    capabilities = response.json()["capabilities"]
    assert isinstance(capabilities["model_enhancement_available"], bool)
    assert isinstance(response.json()["model_policies"], list)
    serialized = str(response.json())
    assert all(forbidden not in serialized for forbidden in ("endpoint", "provider", "route_id", "secret"))


def test_runtime_ready_health_reports_configured_dependencies(client) -> None:
    """根应用必须初始化 Runtime readiness 状态，不能因迁移遗漏而返回 500。"""
    response = client.get("/api/v1/runtime/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_runtime_health_rejects_unready_root_database() -> None:
    """Runtime 不能把根数据库不可用误报为可执行。"""
    health = RuntimeHealth(settings, database_ready=lambda: (False, {}))

    ready, checks = health.check_ready()

    assert ready is False
    assert checks["database"] == "not_ready"
