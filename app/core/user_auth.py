"""用户 JWT 验签边界；业务模块只能获得已验证的当前用户 ID。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import Header, HTTPException, Request, status


class UserAuthenticationError(ValueError):
    """用户身份无效时的固定安全错误，不携带 token 或 claims 内容。"""


@dataclass(frozen=True)
class UserIdentity:
    """已验签的最小用户身份；session_id 用于绑定短期解锁凭证。"""

    user_id: int
    session_id: str


class UserJwtVerifier:
    """验证由微信登录模块签发的最小 HS256 JWT。"""

    def __init__(self, secret: str, *, issuer: str) -> None:
        # 空密钥不能进入“开发方便、生产失守”的隐式降级路径。
        if not secret or not issuer:
            raise UserAuthenticationError("USER_AUTH_CONFIG_INVALID")
        self._secret = secret.encode()
        self._issuer = issuer

    def verify(self, token: str, *, now: datetime | None = None) -> int:
        """验签并返回唯一的正整数主体；不把完整 claims 交给业务服务。"""
        return self.verify_identity(token, now=now).user_id

    def verify_identity(self, token: str, *, now: datetime | None = None) -> UserIdentity:
        """验签并返回绑定登录会话的最小身份，供隐私解锁流程使用。"""
        parts = token.split(".")
        if len(parts) != 3:
            raise UserAuthenticationError("USER_AUTH_TOKEN_INVALID")
        header = self._decode_json(parts[0])
        payload = self._decode_json(parts[1])
        if header.get("alg") != "HS256" or header.get("typ") != "JWT":
            raise UserAuthenticationError("USER_AUTH_TOKEN_INVALID")
        expected = hmac.new(
            self._secret,
            f"{parts[0]}.{parts[1]}".encode(),
            hashlib.sha256,
        ).digest()
        supplied = self._decode_base64(parts[2])
        if not hmac.compare_digest(expected, supplied):
            raise UserAuthenticationError("USER_AUTH_TOKEN_INVALID")
        subject = payload.get("sub")
        session_id = payload.get("jti")
        expires_at = payload.get("exp")
        current = now or datetime.now(UTC)
        if (
            payload.get("iss") != self._issuer
            or not isinstance(subject, str)
            or not subject.isdecimal()
            or int(subject) <= 0
            or not isinstance(session_id, str)
            or not session_id.strip()
            or len(session_id) > 200
            or not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or expires_at <= int(current.timestamp())
        ):
            raise UserAuthenticationError("USER_AUTH_TOKEN_INVALID")
        return UserIdentity(int(subject), session_id)

    @staticmethod
    def _decode_json(segment: str) -> dict[str, Any]:
        """解析 JWT JSON 段；格式问题统一收敛为安全错误码。"""
        try:
            value = json.loads(UserJwtVerifier._decode_base64(segment))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UserAuthenticationError("USER_AUTH_TOKEN_INVALID") from exc
        if not isinstance(value, dict):
            raise UserAuthenticationError("USER_AUTH_TOKEN_INVALID")
        return value

    @staticmethod
    def _decode_base64(segment: str) -> bytes:
        """严格解码 URL-safe Base64，拒绝损坏输入而不是宽松修复。"""
        try:
            return base64.b64decode(
                segment + "=" * (-len(segment) % 4), altchars=b"-_", validate=True
            )
        except ValueError as exc:
            raise UserAuthenticationError("USER_AUTH_TOKEN_INVALID") from exc


def get_current_user_id(
    request: Request,
    authorization: str | None = Header(default=None),
) -> int:
    """FastAPI 用户身份依赖：只返回验签后的 owner 用户 ID。"""
    return get_current_user_identity(request, authorization).user_id


def get_current_user_identity(
    request: Request,
    authorization: str | None = Header(default=None),
) -> UserIdentity:
    """FastAPI 用户身份依赖：密码解锁等流程需要同一 JWT 的 jti。"""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="USER_AUTH_TOKEN_INVALID",
        )
    try:
        settings = request.app.state.settings
        return UserJwtVerifier(
            settings.USER_AUTH_JWT_SECRET,
            issuer=settings.USER_AUTH_JWT_ISSUER,
        ).verify_identity(authorization.removeprefix("Bearer "))
    except UserAuthenticationError as exc:
        # 禁止记录 token、claim 或用户正文；调用方只得到固定错误码。
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="USER_AUTH_TOKEN_INVALID",
        ) from exc
