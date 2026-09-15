"""D6 回归：测试进程配置隔离。

证明 pytest 进程内的 Settings 单例与显式构造都读不到开发者本机
`.env.*.local` 或继承 shell 的真实密钥/数据库密码/私有 URL。隔离由
`tests/conftest.py` 在任何 `app.*` 导入前完成；这些测试失效时必须直接
失败，而不是要求开发者手工清空环境。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_settings.sources import DotEnvSettingsSource

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


def test_dotenv_file_reading_is_blocked_at_source(tmp_path: Path) -> None:
    """读取入口级回归（S3）：构造 Settings 时不得打开任何 dotenv 文件。

    pydantic-settings 2.14.1 在 source 构造期经 _load_env_vars →
    _read_env_files 读文件、再对每个存在的文件调 _read_env_file。
    conftest 只补 __call__ 时文件仍会被读入（旧缺陷）；本测试在
    _read_env_file 上挂 spy，断言整个构造过程读取次数为 0——补丁点
    回退到 __call__ 或被删除时立即失败。
    """
    env_file = tmp_path / ".env.spy"
    env_file.write_text("MEMOIR_TTS_SPEAKER=spy-from-dotenv\n", encoding="utf-8")
    calls: list[Path] = []
    original_read = DotEnvSettingsSource._read_env_file

    def _spy_read(self: DotEnvSettingsSource, file_path: Path) -> dict[str, str]:
        calls.append(Path(file_path))
        return original_read(self, file_path)

    import pytest

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(DotEnvSettingsSource, "_read_env_file", _spy_read)
        settings = Settings(_env_file=str(env_file))

    assert calls == []
    assert settings.MEMOIR_TTS_SPEAKER != "spy-from-dotenv"


def test_restored_dotenv_reads_only_explicit_tmp_file(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """专门 dotenv 行为测试（S3）：恢复原始入口后只读显式指定的 tmp 文件。

    monkeypatch.setattr 恢复 conftest 保存的原始 _read_env_files（测试
    结束自动还原为阻断 stub，不影响其他用例）；_env_file 只指向 tmp_path
    内自建文件（init kwarg 覆盖 config 默认的 PROJECT_ROOT 三元组），
    spy 捕获的全部读取路径必须落在 tmp_path 下——保证专门测试绝不触碰
    开发者真实 env 文件。
    """
    env_file = tmp_path / ".env.restored"
    env_file.write_text("MEMOIR_TTS_SPEAKER=sentinel-restored\n", encoding="utf-8")
    calls: list[Path] = []
    original_read = DotEnvSettingsSource._read_env_file

    def _spy_read(self: DotEnvSettingsSource, file_path: Path) -> dict[str, str]:
        calls.append(Path(file_path))
        return original_read(self, file_path)

    conftest_stub = DotEnvSettingsSource._read_env_files
    assert hasattr(conftest_stub, "original"), "conftest 必须在阻断函数上保存原始入口"
    monkeypatch.setattr(
        DotEnvSettingsSource, "_read_env_files", conftest_stub.original
    )
    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_file", _spy_read)

    settings = Settings(_env_file=str(env_file))

    # 原始 dotenv 行为恢复：哨兵值被读入。
    assert settings.MEMOIR_TTS_SPEAKER == "sentinel-restored"
    # 全部读取路径都在 tmp_path 内：从未触碰开发者真实 env 文件。
    assert calls and all(tmp_path in p.parents for p in calls)


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
