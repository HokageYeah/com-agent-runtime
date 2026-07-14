from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_live_health_only_reports_process_liveness() -> None:
    response = TestClient(create_app()).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live", "runtime_id": "agent-runtime"}


def test_ready_health_reports_configuration_failure() -> None:
    app = create_app()
    app.state.runtime_health.check_ready = lambda: (False, {"database": "unavailable"})

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"] == {"database": "unavailable"}


def test_ready_health_rejects_draining_or_missing_outbox_handler() -> None:
    app = create_app(Settings(enabled_outbox_event_types=["callback"]))
    client = TestClient(app)

    assert client.get("/health/ready").status_code == 503
    app.state.runtime_health.draining = True
    assert client.get("/health/ready").status_code == 503
