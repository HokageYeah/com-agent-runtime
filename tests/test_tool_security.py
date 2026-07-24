"""Runtime HMAC 接收端必须在密钥轮换窗口内验证原始 body。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.tool_security import tool_signature, verify_runtime_tool


def test_verify_runtime_tool_accepts_rotating_key_inside_window() -> None:
    body = b'{"safe":"summary"}'
    timestamp = str(int(datetime.now(UTC).timestamp()))
    runtimes = {"runtime": {"keys": {"old": {"secret": "old-secret", "not_after": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}, "new": {"secret": "new-secret", "not_before": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}}}}
    headers = {"x-agent-runtime-id": "runtime", "x-agent-key-id": "old", "x-agent-timestamp": timestamp, "x-agent-signature": tool_signature("POST", "/callback", timestamp, body, "old-secret")}

    assert verify_runtime_tool(headers, "POST", "/callback", body, runtimes, 300) == "runtime"


def test_verify_runtime_tool_rejects_key_outside_rotation_window() -> None:
    body = b"{}"
    timestamp = str(int(datetime.now(UTC).timestamp()))
    runtimes = {"runtime": {"keys": {"old": {"secret": "old-secret", "not_after": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}}}}
    headers = {"x-agent-runtime-id": "runtime", "x-agent-key-id": "old", "x-agent-timestamp": timestamp, "x-agent-signature": tool_signature("POST", "/callback", timestamp, body, "old-secret")}

    with pytest.raises(ValueError, match="Runtime 工具签名无效"):
        verify_runtime_tool(headers, "POST", "/callback", body, runtimes, 300)
