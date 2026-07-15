from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.models import AgentDefinition


def _headers(
    method: str, path: str, body: bytes, key: str = "request-1"
) -> dict[str, str]:
    timestamp = str(int(datetime.now(UTC).timestamp()))
    canonical = (
        f"{method}\n{path}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}".encode()
    )
    signature = hmac.new(b"development-secret", canonical, hashlib.sha256).hexdigest()
    return {
        "X-Agent-Client-Id": "couple-diary",
        "X-Agent-Key-Id": "dev",
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Signature": signature,
        "Idempotency-Key": key,
        "Content-Type": "application/json",
    }


def test_signed_create_replays_first_response(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
        agent_package_root="./app/agents",
    )
    app = create_app(settings)
    session = app.state.session_factory()
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
            status_change_reason="fixture",
        )
    )
    session.commit()
    session.close()
    body = json.dumps(
        {
            "agent_id": "memoir_agent",
            "agent_version": "1.0.0",
            "business_type": "couple_memory",
            "business_id": "archive",
            "start_mode": "held",
            "input": {"snapshot_id": "s"},
            "callback_target_id": "callback",
            "business_connector_id": "connector",
        },
        separators=(",", ":"),
    ).encode()
    client = TestClient(app)
    first = client.post(
        "/api/v1/agent-runs",
        content=body,
        headers=_headers("POST", "/api/v1/agent-runs", body),
    )
    second = client.post(
        "/api/v1/agent-runs",
        content=body,
        headers=_headers("POST", "/api/v1/agent-runs", body),
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["run_id"] == second.json()["run_id"]
