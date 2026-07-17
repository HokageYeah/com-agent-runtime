"""Runtime 到业务系统的 callback 出站网关。"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.tool_security import tool_signature


@dataclass(frozen=True)
class CallbackTarget:
    """预注册 callback 目标；业务请求不能覆盖 URL 或签名身份。"""

    url: str
    runtime_id: str
    key_id: str
    secret: str


class CallbackGateway:
    """向固定 callback target 发起签名请求，禁止自动跟随重定向。"""

    def __init__(self, targets: dict[str, CallbackTarget], client: httpx.Client) -> None:
        self._targets, self._client = targets, client

    def send(self, target_id: str, payload: dict[str, Any]) -> None:
        """发送一个不可变事件；异常交由 Dispatcher 依据 outbox 做退避重试。"""
        target = self._targets.get(target_id)
        if target is None:
            raise ValueError("CALLBACK_TARGET_UNAVAILABLE")
        event_id, run_id, business_id, event_seq = (
            payload.get("event_id"), payload.get("run_id"), payload.get("business_id"), payload.get("event_seq")
        )
        if not isinstance(event_id, str) or not isinstance(run_id, str) or not isinstance(business_id, str) or not isinstance(event_seq, int):
            raise ValueError("CALLBACK_PAYLOAD_INVALID")
        url = httpx.URL(target.url)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("CALLBACK_TARGET_INVALID")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Agent-Runtime-Id": target.runtime_id,
            "X-Agent-Key-Id": target.key_id,
            "X-Agent-Run-Id": run_id,
            "X-Agent-Business-Id": business_id,
            "X-Agent-Event-Id": event_id,
            "X-Agent-Event-Seq": str(event_seq),
            "X-Agent-Timestamp": timestamp,
            "X-Agent-Signature": tool_signature("POST", url.raw_path.decode(), timestamp, body, target.secret),
            "Idempotency-Key": f"callback:{event_id}",
        }
        response = self._client.post(str(url), content=body, headers=headers, timeout=10.0, follow_redirects=False)
        response.raise_for_status()
        logging.info("Runtime callback 投递成功 target_id=%s run_id=%s event_id=%s", target_id, run_id, event_id)
