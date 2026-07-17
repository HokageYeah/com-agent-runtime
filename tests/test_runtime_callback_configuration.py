"""callback target 必须来自部署配置，不能由业务请求指定。"""

from app.core.config import Settings


def test_settings_parses_registered_callback_targets() -> None:
    settings = Settings(
        RUNTIME_CALLBACK_TARGETS_JSON='{"memory":{"enabled":true,"url":"https://callback.example/internal","runtime_id":"runtime","key_id":"key","secret":"secret"}}'
    )

    assert settings.callback_targets["memory"]["url"] == "https://callback.example/internal"
