from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_capabilities_require_service_identity_and_redact_secrets() -> None:
    app = create_app()
    client = TestClient(app)

    unauthorized = client.get("/api/v1/runtime-capabilities")
    response = client.get(
        "/api/v1/runtime-capabilities",
        headers={"X-Agent-Client-Id": "couple-diary"},
    )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    assert payload["contract_version"] == "1.0.0"
    assert payload["agents"] == [{"agent_id": "memoir_agent", "version": "1.0.0"}]
    assert "secret" not in str(payload).lower()
    assert "endpoint" not in str(payload).lower()
