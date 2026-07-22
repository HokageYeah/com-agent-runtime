"""用户 JWT 身份边界测试；回忆录 owner 鉴权只能使用已验签的主体。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.user_auth import (
    UserAuthenticationError,
    UserIdentity,
    UserJwtVerifier,
    get_current_user_id,
)


def _token(payload: dict[str, object], secret: str) -> str:
    """构造测试 JWT；生产代码不提供签发能力，避免误成为登录入口。"""
    header = {"alg": "HS256", "typ": "JWT"}

    def encode(value: dict[str, object]) -> bytes:
        """返回 JWT 段的 URL-safe Base64 表示。"""
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).rstrip(b"=")

    signing_input = b".".join((encode(header), encode(payload)))
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{signing_input.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def test_jwt_verifier_returns_positive_numeric_subject_only() -> None:
    """有效 JWT 只暴露最小用户标识，避免把 claims 扩散到业务层。"""
    verifier = UserJwtVerifier("test-secret", issuer="couple-diary")
    token = _token(
        {
            "sub": "42",
            "iss": "couple-diary",
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            "jti": "session-1",
        },
        "test-secret",
    )

    assert verifier.verify(token) == 42


@pytest.mark.parametrize(
    "payload",
    [
        {"sub": "0", "iss": "couple-diary", "exp": 2_000_000_000, "jti": "session-1"},
        {"sub": "42", "iss": "other", "exp": 2_000_000_000, "jti": "session-1"},
        {"sub": "42", "iss": "couple-diary", "exp": 1, "jti": "session-1"},
    ],
)
def test_jwt_verifier_rejects_invalid_identity_claims(payload: dict[str, object]) -> None:
    """错误主体、签发者或过期时间一律不能成为回忆录 owner 身份。"""
    verifier = UserJwtVerifier("test-secret", issuer="couple-diary")

    with pytest.raises(UserAuthenticationError, match="USER_AUTH_TOKEN_INVALID"):
        verifier.verify(_token(payload, "test-secret"))


def test_current_user_dependency_accepts_only_bearer_jwt() -> None:
    """路由依赖从部署配置验签，缺少 Bearer 前缀时不能降级为匿名用户。"""
    token = _token(
        {"sub": "42", "iss": "couple-diary", "exp": 2_000_000_000, "jti": "session-1"}, "test-secret"
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(
                    USER_AUTH_JWT_SECRET="test-secret",
                    USER_AUTH_JWT_ISSUER="couple-diary",
                )
            )
        )
    )

    assert get_current_user_id(request, f"Bearer {token}") == 42
    with pytest.raises(HTTPException, match="USER_AUTH_TOKEN_INVALID"):
        get_current_user_id(request, "Token ignored")


def test_jwt_verifier_requires_jti_for_a_session_bound_unlock_credential() -> None:
    """密码解锁必须绑定可撤销的登录会话，不能只绑定长期 user_id。"""
    verifier = UserJwtVerifier("test-secret", issuer="couple-diary")
    payload = {"sub": "42", "iss": "couple-diary", "exp": 2_000_000_000, "jti": "session-1"}

    assert verifier.verify_identity(_token(payload, "test-secret")) == UserIdentity(42, "session-1")
    payload.pop("jti")
    with pytest.raises(UserAuthenticationError, match="USER_AUTH_TOKEN_INVALID"):
        verifier.verify_identity(_token(payload, "test-secret"))
