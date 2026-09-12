"""D6 回归：测试进程配置隔离。

证明 pytest 进程内的 Settings 单例与显式构造都读不到开发者本机
`.env.*.local` 或继承 shell 的真实密钥/数据库密码/私有 URL。隔离由
`tests/conftest.py` 在任何 `app.*` 导入前完成；这些测试失效时必须直接
失败，而不是要求开发者手工清空环境。
"""

from __future__ import annotations

from pathlib import Path

from app.core import config
from app.core.config import Settings


def test_dotenv_source_is_neutralized(tmp_path: Path) -> None:
    """显式传入 env 文件也被阻断：哨兵值不得进入 Settings。

    隔离失效（DotEnvSettingsSource 未被 conftest 置空）时，哨兵文件
    会被 pydantic-settings 读入，本测试失败。
    """
    # 哨兵键选 conftest 不注入环境变量的字段，确保 dotenv 是唯一输入源，
    # 否则环境变量优先级会压住哨兵、让测试假绿。
    env_file = tmp_path / ".env.sentinel"
    env_file.write_text("MEMOIR_TTS_SPEAKER=sentinel-from-dotenv\n", encoding="utf-8")

    settings = Settings(_env_file=str(env_file))

    assert settings.MEMOIR_TTS_SPEAKER != "sentinel-from-dotenv"


def test_singleton_environment_is_test() -> None:
    """单例环境必须是 test：由 conftest 设置，不随开发者 shell 漂移。"""
    assert config.settings.ENVIRONMENT == "test"


def test_singleton_uses_conftest_fixed_values() -> None:
    """单例只含 conftest 固定假值与类默认值。

    开发者 shell 或 `.env.*.local` 提供的真实 USER_AUTH_JWT_SECRET /
    MEMOIR_AUDIO_ENABLED 一旦渗入单例，断言失败。
    """
    assert config.settings.USER_AUTH_JWT_SECRET == "unit-test-user-jwt-secret"
    # 类默认关闭；本机 .env.test.local 的 MEMOIR_AUDIO_ENABLED=true 不得污染单例。
    assert config.settings.MEMOIR_AUDIO_ENABLED is False
