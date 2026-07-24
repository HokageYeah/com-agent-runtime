"""Runtime 调用业务内部工具的最小 HMAC 签名协议。"""
from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any


def tool_signature(method: str, path: str, timestamp: str, body: bytes, secret: str) -> str:
    canonical = f"{method}\n{path}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}".encode()
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


def verify_runtime_tool(headers: dict[str, str], method: str, path: str, body: bytes, runtimes: dict[str, dict[str, object]], tolerance: int) -> str:
    runtime_id, key_id = headers.get("x-agent-runtime-id"), headers.get("x-agent-key-id")
    timestamp, signature = headers.get("x-agent-timestamp"), headers.get("x-agent-signature")
    if not all((runtime_id, key_id, timestamp, signature)):
        raise ValueError("缺少 Runtime 工具身份")
    assert runtime_id and key_id and timestamp and signature
    try:
        if abs((datetime.now(UTC) - datetime.fromtimestamp(int(timestamp), UTC)).total_seconds()) > tolerance:
            raise ValueError("Runtime 工具请求已过期")
    except (TypeError, ValueError) as exc:
        raise ValueError("Runtime 工具时间戳无效") from exc
    keys = runtimes.get(runtime_id, {}).get("keys", {})
    secret = _active_key_secret(keys.get(key_id)) if isinstance(keys, dict) else None
    if not isinstance(secret, str) or not hmac.compare_digest(signature, tool_signature(method, path, timestamp, body, secret)):
        raise ValueError("Runtime 工具签名无效")
    return runtime_id


def _active_key_secret(value: object) -> str | None:
    """轮换期允许新旧 key 并存；窗口外的 key 即使签名正确也不可用。"""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    secret = value.get("secret")
    if not isinstance(secret, str):
        return None
    now = datetime.now(UTC)
    try:
        not_before = _parse_rotation_time(value.get("not_before"))
        not_after = _parse_rotation_time(value.get("not_after"))
    except ValueError:
        return None
    if (not_before is not None and now < not_before) or (not_after is not None and now > not_after):
        return None
    return secret


def _parse_rotation_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("rotation time invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("rotation time timezone required")
    return parsed.astimezone(UTC)
