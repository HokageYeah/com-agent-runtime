from __future__ import annotations

from app.api.endpoints.health_api import RuntimeHealth
from app.core.config import settings


def test_runtime_capabilities_rejects_unknown_caller(client) -> None:
    """Runtime 能力发现不得向未登记的调用方泄露 Agent 或模型策略。"""
    response = client.get("/api/v1/runtime/capabilities")

    assert response.status_code == 401
    assert response.json()["ret"] == ["ERROR::unknown client"]


def test_runtime_ready_health_reports_configured_dependencies(client) -> None:
    """根应用必须初始化 Runtime readiness 状态，不能因迁移遗漏而返回 500。"""
    response = client.get("/api/v1/runtime/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_runtime_health_rejects_unready_root_database() -> None:
    """Runtime 不能把根数据库不可用误报为可执行。"""
    health = RuntimeHealth(settings, database_ready=lambda: (False, {}))

    ready, checks = health.check_ready()

    assert ready is False
    assert checks["database"] == "not_ready"
