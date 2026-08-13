"""Runtime 服务到服务 HMAC 验签，不记录签名原文或密钥。"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 仅为类型提示引入；运行期保持纯标准库，避免安全核心耦合 Web 框架。
    from starlette.datastructures import Headers


class SignatureError(ValueError):
    pass


# Business → Runtime 服务认证的四个必填头；任一头重复或缺失即视为身份注入。
# dict 推导会抹掉同名头基数，因此重复头检测必须在压平前基于原始 HTTP 头完成。
SERVICE_AUTH_HEADERS: tuple[str, ...] = (
    "x-agent-client-id",
    "x-agent-key-id",
    "x-agent-timestamp",
    "x-agent-signature",
)


def assert_single_service_headers(headers: Headers) -> None:
    """认证前校验四个服务认证头各自恰好出现一次。

    HTTP 头名大小写不敏感，Starlette 的 getlist 自身按大小写无关匹配并保留全部同名值；
    任一头出现 0 次或 >1 次即 fail-closed。本函数不读取也不记录任何身份头原值，
    仅检查其基数，避免重复头被 HTTP 层任选其一后穿透验签。
    """
    for name in SERVICE_AUTH_HEADERS:
        if len(headers.getlist(name)) != 1:
            raise SignatureError("服务认证头重复或缺失")


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
    # Runtime-Id 只属于 Runtime → 业务；HTTP 头名大小写不敏感，业务 API 必须拒绝混头。
    if any(name.lower() == "x-agent-runtime-id" for name in headers):
        raise SignatureError("缺少或未知服务身份")
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
    except (OverflowError, OSError, ValueError) as exc:
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
        logging.warning("Runtime HMAC 验签失败 path=%s", path)
        raise SignatureError("签名不匹配")
    return client_id
