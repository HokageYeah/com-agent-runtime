from __future__ import annotations


def test_runtime_capabilities_rejects_unknown_caller(client) -> None:
    """Runtime 能力发现不得向未登记的调用方泄露 Agent 或模型策略。"""
    response = client.get("/api/v1/runtime/capabilities")

    assert response.status_code == 401
    assert response.json()["ret"] == ["ERROR::unknown client"]
