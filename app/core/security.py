"""Runtime 服务到服务 HMAC 验签，不记录签名原文或密钥。"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime


class SignatureError(ValueError):
    pass


def request_hash(method: str, path: str, body: bytes) -> str:
    """幂等 hash 覆盖资源路径，避免不同 run 的空 body 请求错误重放。"""
    body_hash = hashlib.sha256(body).hexdigest()
    return hashlib.sha256(f"{method.upper()}\n{path}\n{body_hash}".encode()).hexdigest()


def verify_signature(
    headers: dict[str, str],
    method: str,
    path: str,
    body: bytes,
    clients: dict[str, dict[str, object]],
    tolerance_seconds: int,
) -> str:
    client_id, key_id = headers.get("x-agent-client-id"), headers.get("x-agent-key-id")
    timestamp, signature = (
        headers.get("x-agent-timestamp"),
        headers.get("x-agent-signature"),
    )
    if not all([client_id, key_id, timestamp, signature]):
        raise SignatureError("缺少或未知服务身份")
    # all() 不能帮助 mypy 收窄 Optional，因此在完成 header 校验后显式断言。
    assert client_id is not None and key_id is not None
    assert timestamp is not None and signature is not None
    if client_id not in clients:
        raise SignatureError("缺少或未知服务身份")
    try:
        age = abs(
            (
                datetime.now(UTC) - datetime.fromtimestamp(int(timestamp), UTC)
            ).total_seconds()
        )
    except ValueError as exc:
        raise SignatureError("timestamp 无效") from exc
    if age > tolerance_seconds:
        raise SignatureError("timestamp 已过期")
    keys = clients[client_id].get("keys", {})
    key = keys.get(key_id) if isinstance(keys, dict) else None
    if not isinstance(key, str):
        raise SignatureError("签名密钥不可用")
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"{method.upper()}\n{path}\n{timestamp}\n{body_hash}".encode()
    expected = hmac.new(key.encode(), canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        logging.warning("Runtime HMAC 验签失败 client_id=%s path=%s", client_id, path)
        raise SignatureError("签名不匹配")
    return client_id
