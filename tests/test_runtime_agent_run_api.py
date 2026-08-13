from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.main import app
from app.models import AgentDefinition
from app.services.admission_service import AdmissionLimits


def _headers(
    method: str, path: str, body: bytes, *, idempotency_key: str = "root-runtime-create-1"
) -> dict[str, str]:
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
        "Idempotency-Key": idempotency_key,
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


@pytest.mark.parametrize(
    "header_name",
    (
        "X-Agent-Client-Id",
        "X-Agent-Key-Id",
        "X-Agent-Timestamp",
        "X-Agent-Signature",
    ),
)
def test_signed_create_rejects_duplicate_service_auth_header(
    client, header_name: str
) -> None:
    """POST create 必须在认证前拒绝重复服务认证头，不能让 HTTP 层任选其一后穿透。

    用真实 Header tuple 列表携带同名头：dict 会天然丢弃重复项，
    无法证明认证边界对重复头的处理。注入真实内存库，使修复前签名有效时
    请求稳定穿透认证并创建 Run（201）；修复后认证边界须直接 401。
    """
    # StaticPool 保证请求 Session 与准备数据的 Session 共用同一内存库。
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
            agent_id="memoir_agent", version="1.0.0", runtime_type="workflow",
            definition_json={"allowed_business_types": ["couple_memory"]},
            package_digest="sha256:test", contract_version="1.0.0", status="active",
            status_changed_at=datetime.now(UTC), status_changed_by="test",
            status_change_reason="duplicate-header-test",
        )
    )
    session.commit()
    session.close()

    path = "/api/v1/runtime/agent-runs"
    body = json.dumps(
        {
            "agent_id": "memoir_agent", "agent_version": "1.0.0",
            "business_type": "couple_memory", "business_id": "archive-duplicate-header",
            "start_mode": "held", "input": {"snapshot_id": "snapshot-duplicate-header"},
            "callback_target_id": "callback", "business_connector_id": "couple_diary_backend",
        },
        separators=(",", ":"),
    ).encode()
    signed_headers = _headers("POST", path, body, idempotency_key="duplicate-header-1")
    # 追加一个同名小写头，构造真实重复认证头请求。
    headers = [
        *signed_headers.items(),
        (header_name.lower(), signed_headers[header_name]),
    ]

    response = client.post(path, content=body, headers=headers)

    assert response.status_code == 401


def test_signed_create_returns_429_without_idempotency_record_when_admission_is_full(
    client, monkeypatch
) -> None:
    """HTTP Admission 超载不得创建第二个 Run 或消费调用方的重试幂等键。"""
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
            agent_id="memoir_agent", version="1.0.0", runtime_type="workflow",
            definition_json={"allowed_business_types": ["couple_memory"]},
            package_digest="sha256:test", contract_version="1.0.0", status="active",
            status_changed_at=datetime.now(UTC), status_changed_by="test",
            status_change_reason="admission-overload-test",
        )
    )
    session.commit()
    session.close()
    monkeypatch.setattr(
        "app.api.endpoints.agent_runs_api._admission_limits",
        lambda _request: AdmissionLimits(max_held=1, max_queued=1, max_running=1),
    )

    path = "/api/v1/runtime/agent-runs"
    body = json.dumps(
        {
            "agent_id": "memoir_agent", "agent_version": "1.0.0",
            "business_type": "couple_memory", "business_id": "archive-overload",
            "start_mode": "held", "input": {"snapshot_id": "snapshot-overload"},
            "callback_target_id": "callback", "business_connector_id": "couple_diary_backend",
        },
        separators=(",", ":"),
    ).encode()

    first = client.post(
        path, content=body, headers=_headers("POST", path, body, idempotency_key="admission-1")
    )
    overloaded = client.post(
        path, content=body, headers=_headers("POST", path, body, idempotency_key="admission-2")
    )

    assert first.status_code == 201
    assert overloaded.status_code == 429
    assert overloaded.json()["ret"] == ["ERROR::RUNTIME_OVERLOADED"]
    assert overloaded.headers["Retry-After"] == "5"
