from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

import pytest

from app.core.security import SignatureError, verify_signature


def test_hmac_signature_accepts_exact_body_and_rejects_tampering() -> None:
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
    with pytest.raises(SignatureError):
        verify_signature(
            headers, "POST", "/api/v1/agent-runs", b'{"x":2}', clients, 300
        )
