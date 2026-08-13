from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime

import pytest

from app.core.authorization import AuthorizationService
from app.core.security import SignatureError, verify_signature


def test_hmac_signature_accepts_exact_body_and_rejects_tampering(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body, timestamp, secret = (
        b'{"x":1}',
        str(int(datetime.now(UTC).timestamp())),
        "secret",
    )
    canonical = f"POST\n/api/v1/agent-runs\n{timestamp}\n{hashlib.sha256(body).hexdigest()}".encode()
    signature = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    headers = {
        "x-agent-client-id": "client",
        "x-agent-key-id": "key",
        "x-agent-timestamp": timestamp,
        "x-agent-signature": signature,
    }
    clients = {"client": {"keys": {"key": secret}}}
    assert (
        verify_signature(headers, "POST", "/api/v1/agent-runs", body, clients, 300)
        == "client"
    )
    with caplog.at_level("WARNING"), pytest.raises(SignatureError):
        verify_signature(
            headers, "POST", "/api/v1/agent-runs", b'{"x":2}', clients, 300
        )
    assert all("client" not in record.getMessage() for record in caplog.records)

@pytest.mark.parametrize(
    ("identity_header_case", "accepted"),
    (
        ("client_only", True),
        ("missing", False),
        ("runtime_only", False),
        ("mixed", False),
        ("mixed_http_cased", False),
    ),
)
def test_business_signature_accepts_only_client_identity_header(
    identity_header_case: str, accepted: bool
) -> None:
    """业务入站只接受 Client-Id，Runtime-Id 不能反向或并存。"""

    body = b'{"x":1}'
    timestamp = str(int(datetime.now(UTC).timestamp()))
    secret = "secret"
    canonical = f"POST\n/api/v1/agent-runs\n{timestamp}\n{hashlib.sha256(body).hexdigest()}"
    headers = {
        "x-agent-client-id": "client",
        "x-agent-key-id": "key",
        "x-agent-timestamp": timestamp,
        "x-agent-signature": hmac.new(
            secret.encode(), canonical.encode(), hashlib.sha256
        ).hexdigest(),
    }
    if identity_header_case == "missing":
        del headers["x-agent-client-id"]
    elif identity_header_case == "runtime_only":
        headers["x-agent-runtime-id"] = headers.pop("x-agent-client-id")
    elif identity_header_case == "mixed":
        headers["x-agent-runtime-id"] = "agent-runtime"
    elif identity_header_case == "mixed_http_cased":
        headers["X-Agent-Runtime-Id"] = "agent-runtime"

    clients = {"client": {"keys": {"key": secret}}}
    if accepted:
        assert (
            verify_signature(headers, "POST", "/api/v1/agent-runs", body, clients, 300)
            == "client"
        )
    else:
        with pytest.raises(SignatureError):
            verify_signature(headers, "POST", "/api/v1/agent-runs", body, clients, 300)


def test_signature_rejects_unrepresentable_timestamp() -> None:
    """超出 datetime 可表示范围的时间戳也必须按认证失败处理。"""

    body = b'{}'
    timestamp = "9999999999999999999999999"
    headers = {
        "x-agent-client-id": "client",
        "x-agent-key-id": "key",
        "x-agent-timestamp": timestamp,
        "x-agent-signature": "irrelevant",
    }

    with pytest.raises(SignatureError, match="timestamp 无效"):
        verify_signature(
            headers,
            "POST",
            "/api/v1/agent-runs",
            body,
            {"client": {"keys": {"key": "secret"}}},
            300,
        )


def test_authorization_log_does_not_expose_client_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """授权成功日志只能留下资源摘要，不能写入入站 Client 身份。"""

    client_id = "identity-header-value-must-not-log"
    service = AuthorizationService(
        {
            client_id: {
                "agent_ids": ["memoir_agent"],
                "business_types": ["couple_memory"],
                "callback_target_ids": ["memoir"],
                "connector_ids": ["couple-diary"],
                "data_domains": ["private"],
            }
        }
    )

    with caplog.at_level(logging.INFO):
        service.authorize_create(
            client_id=client_id,
            agent_id="memoir_agent",
            business_type="couple_memory",
            callback_target_id="memoir",
            connector_id="couple-diary",
            data_domain="private",
        )

    assert all(client_id not in record.getMessage() for record in caplog.records)
