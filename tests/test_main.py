from __future__ import annotations

from unittest.mock import patch


def test_healthz_returns_stable_service_status(client) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "Couple Diary Backend"
    assert response.json()["environment"] == "development"
    assert response.json()["version"] == "0.1.0"
    assert response.headers["X-Request-ID"]


def test_healthz_reuses_custom_request_id_header(client) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "manual-request-id"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "manual-request-id"


def test_readyz_returns_ready_when_database_dependency_is_available(client) -> None:
    with patch(
        "app.main.database.check_ready",
        return_value=(
            True,
            {"dependency": "database", "status": "ready"},
        ),
    ):
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["checks"] == [{"dependency": "database", "status": "ready"}]
    assert response.headers["X-Request-ID"]


def test_readyz_returns_503_when_database_dependency_is_not_ready(client) -> None:
    with patch(
        "app.main.database.check_ready",
        return_value=(
            False,
            {
                "dependency": "database",
                "status": "not_ready",
                "reason": "session_factory_not_initialized",
            },
        ),
    ):
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"] == [
        {
            "dependency": "database",
            "status": "not_ready",
            "reason": "session_factory_not_initialized",
        }
    ]


def test_slow_request_logs_warning_when_threshold_is_exceeded(client) -> None:
    with (
        patch("app.middleware.request_logging.settings.SLOW_REQUEST_THRESHOLD_MS", 0),
        patch("app.middleware.request_logging.logging.warning") as mock_warning,
    ):
        response = client.get("/healthz")

    assert response.status_code == 200
    mock_warning.assert_called()
    logged_message = mock_warning.call_args.args[0]
    assert "慢请求" in logged_message
