from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.main import app
from app.models import AgentDefinition


def _headers(method: str, path: str, body: bytes) -> dict[str, str]:
    """构造根 Runtime 写接口需要的 HMAC 请求头，测试密钥不进入生产日志。"""
    timestamp = str(int(datetime.now(UTC).timestamp()))
    canonical = f"{method}\n{path}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}"
    signature = hmac.new(
        b"development-secret", canonical.encode(), hashlib.sha256
    ).hexdigest()
    return {
        "X-Agent-Client-Id": "couple-diary",
        "X-Agent-Key-Id": "dev",
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Signature": signature,
        "Idempotency-Key": "root-runtime-create-1",
        "Content-Type": "application/json",
    }


def test_signed_create_uses_root_runtime_route_and_replays_response(client) -> None:
    """根应用的 held create 重放同一结果，不能创建第二个 AgentRun。"""
    # StaticPool 保证请求 Session 与准备数据的 Session 共用同一个内存数据库。
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    app.state.session_factory = factory
    session = factory()
    session.add(
        AgentDefinition(
            agent_id="memoir_agent",
            version="1.0.0",
            runtime_type="workflow",
            definition_json={"allowed_business_types": ["couple_memory"]},
            package_digest="sha256:test",
            contract_version="1.0.0",
            status="active",
            status_changed_at=datetime.now(UTC),
            status_changed_by="test",
            status_change_reason="root-api-test",
        )
    )
    session.commit()
    session.close()

    path = "/api/v1/runtime/agent-runs"
    body = json.dumps(
        {
            "agent_id": "memoir_agent",
            "agent_version": "1.0.0",
            "business_type": "couple_memory",
            "business_id": "archive-root-api",
            "start_mode": "held",
            "input": {"snapshot_id": "snapshot-root-api"},
            "callback_target_id": "callback",
            "business_connector_id": "couple_diary_backend",
        },
        separators=(",", ":"),
    ).encode()

    first = client.post(path, content=body, headers=_headers("POST", path, body))
    second = client.post(path, content=body, headers=_headers("POST", path, body))

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["run_id"] == second.json()["run_id"]
