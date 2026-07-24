"""callback 目标配置与 dispatcher 启用条件必须一致。"""

from app.api.endpoints.health_api import RuntimeHealth
from app.core.config import Settings


def test_runtime_readiness_rejects_enabled_callback_with_incomplete_handler_config() -> None:
    settings = Settings(
        RUNTIME_AUDIT_SINK_CONFIGURED=True,
        RUNTIME_CALLBACK_TARGETS_JSON='{"memory":{"enabled":true,"url":"https://callback.example"}}',
    )

    ready, checks = RuntimeHealth(settings).check_ready()

    assert not ready
    assert checks["callback_dispatcher"] == "invalid"
