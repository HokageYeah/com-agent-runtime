"""Runtime 调用业务内部工具的最小 HMAC 签名协议。"""
from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime


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
    secret = keys.get(key_id) if isinstance(keys, dict) else None
    if not isinstance(secret, str) or not hmac.compare_digest(signature, tool_signature(method, path, timestamp, body, secret)):
        raise ValueError("Runtime 工具签名无效")
    return runtime_id
