from __future__ import annotations

import pytest

from app.core import config
from app.core.config import Settings


def test_normalize_environment_aliases() -> None:
    assert config.normalize_environment("dev") == "development"
    assert config.normalize_environment("development") == "development"
    assert config.normalize_environment("test") == "test"
    assert config.normalize_environment("prod") == "production"
    assert config.normalize_environment("production") == "production"


def test_env_file_for_environment() -> None:
    assert config.env_file_for_environment("development") == ".env.development"
    assert config.env_file_for_environment("test") == ".env.test"
    assert config.env_file_for_environment("production") == ".env.production"


def test_env_files_for_environment_supports_local_overrides() -> None:
    assert config.env_files_for_environment("development") == (
        ".env.development",
        ".env.development.local",
        ".env.local",
    )
    assert config.env_files_for_environment("test") == (
        ".env.test",
        ".env.test.local",
        ".env.local",
    )
    assert config.env_files_for_environment("production") == (
        ".env.production",
        ".env.production.local",
        ".env.local",
    )


def test_get_runtime_environment_prefers_environment_variable() -> None:
    env = config.get_runtime_environment({"ENVIRONMENT": "test", "ENV": "development"})
    assert env == "test"


def test_get_runtime_environment_falls_back_to_env() -> None:
    env = config.get_runtime_environment({"ENV": "prod"})
    assert env == "production"


def test_detects_placeholder_database_credentials() -> None:
    assert config.has_placeholder_database_credentials(
        "your_mysql_user", "your_mysql_password"
    )
    assert not config.has_placeholder_database_credentials("root", "real_password")


def test_parse_cors_origins_supports_comma_separated_text() -> None:
    assert config.parse_cors_origins(
        "http://localhost:3000, http://127.0.0.1:5173"
    ) == ["http://localhost:3000", "http://127.0.0.1:5173"]


def test_parse_cors_origins_supports_json_array_text() -> None:
    assert config.parse_cors_origins(
        '["http://localhost:3000", "http://127.0.0.1:5173"]'
    ) == ["http://localhost:3000", "http://127.0.0.1:5173"]


def test_request_logging_related_defaults_are_stable() -> None:
    assert config.settings.REQUEST_ID_HEADER == "X-Request-ID"
    assert config.settings.SLOW_REQUEST_THRESHOLD_MS == 800


def test_application_config_group_is_stable() -> None:
    application_config = config.settings.application

    assert application_config.project_name == config.settings.PROJECT_NAME
    assert application_config.project_description == config.settings.PROJECT_DESCRIPTION
    assert application_config.project_version == config.settings.PROJECT_VERSION
    assert application_config.api_prefix == config.settings.API_PREFIX
    assert application_config.response_version == config.settings.VERSION
    assert application_config.environment == config.settings.ENVIRONMENT
    assert application_config.debug == config.settings.DEBUG


def test_server_config_group_is_stable() -> None:
    server_config = config.settings.server

    assert server_config.host == config.settings.HOST
    assert server_config.port == config.settings.PORT
    assert server_config.reload == config.settings.RELOAD


def test_request_logging_config_group_is_stable() -> None:
    request_logging_config = config.settings.request_logging

    assert request_logging_config.request_id_header == config.settings.REQUEST_ID_HEADER
    assert (
        request_logging_config.slow_request_threshold_ms
        == config.settings.SLOW_REQUEST_THRESHOLD_MS
    )


def test_cors_config_group_is_stable() -> None:
    cors_config = config.settings.cors

    assert cors_config.allow_origins == config.settings.resolved_cors_origins


def test_database_config_group_is_stable() -> None:
    database_config = config.settings.database

    assert database_config.driver == config.settings.DB_DRIVER
    assert database_config.username == config.settings.DB_USER
    assert database_config.password == config.settings.DB_PASSWORD
    assert database_config.host == config.settings.DB_HOST
    assert database_config.port == config.settings.DB_PORT
    assert database_config.database == config.settings.DB_NAME
    assert database_config.charset == config.settings.DB_CHARSET
    assert database_config.echo == config.settings.DB_ECHO
    assert database_config.pool_size == config.settings.DB_POOL_SIZE
    assert database_config.max_overflow == config.settings.DB_MAX_OVERFLOW
    assert database_config.pool_recycle == config.settings.DB_POOL_RECYCLE
    assert database_config.pool_timeout == config.settings.DB_POOL_TIMEOUT


def test_logging_config_group_is_stable() -> None:
    logging_config = config.settings.logging

    assert logging_config.logging_level == config.settings.LOGGING_LEVEL


def test_memoir_audio_settings_default_off_without_validation() -> None:
    """M8 音频开关默认关闭：不触发成组校验，存量部署行为零变化。"""
    assert config.settings.MEMOIR_AUDIO_ENABLED is False
    # 关闭状态下即使全部音频字段为空也直接放行（validate 短路）。
    config.validate_memoir_audio_settings(config.settings)


def test_memoir_audio_defaults_follow_env_config_frozen_values() -> None:
    """M8 音频 Settings 默认值与 ENV_CONFIG 冻结表逐项一致。"""
    settings = config.settings
    assert settings.MEMOIR_TTS_RESOURCE_ID == "seed-tts-2.0"
    assert settings.MEMOIR_TTS_SPEAKER == "zh_female_wenroushunv_uranus_bigtts"
    assert settings.MEMOIR_TTS_SPEECH_RATE == -10
    assert settings.MEMOIR_TTS_REQUEST_TIMEOUT_SECONDS == 45.0
    assert settings.MEMOIR_TTS_SCENE_CONCURRENCY == 2
    assert settings.MEMOIR_MUSIC_DURATION_SECONDS == 60
    assert settings.MEMOIR_MUSIC_POLL_INTERVAL_SECONDS == 5.0
    assert settings.MEMOIR_AUDIO_NODE_TIMEOUT_SECONDS == 300.0
    assert settings.MEMOIR_AUDIO_PUBLISH_RESERVE_SECONDS == 30.0
    assert settings.MEMOIR_AUDIO_WORKER_CONCURRENCY == 4
    assert settings.MEMOIR_AUDIO_MAX_FILE_BYTES == 20971520
    assert settings.MEMOIR_AUDIO_ORPHAN_RETENTION_HOURS == 24
    assert settings.MEMOIR_AUDIO_FFMPEG_PATH == "/usr/bin/ffmpeg"
    assert settings.MEMOIR_AUDIO_FFPROBE_PATH == "/usr/bin/ffprobe"
    # 音频 OSS / 前缀 / scope 密钥默认为空：未启用时不指向任何真实资源。
    assert settings.MEMORY_AUDIO_OSS_ENDPOINT == ""
    assert settings.MEMORY_AUDIO_NARRATOR_PREFIX == ""
    assert settings.MEMORY_AUDIO_BACKGROUND_PREFIX == ""
    assert settings.MEMORY_AUDIO_SCOPE_HMAC_KEY == ""


# ---------------------------------------------------------------------------
# M8 默认配乐：必填串拆分（基础组 + 默认模式条件必填默认源 key +
# 仅生成模式 3 项）与默认源 key 同根校验
# ---------------------------------------------------------------------------

# 隔离用：屏蔽开发者真实进程 env / dotenv 中全部 M8 音频变量名，
# 测试只认显式 kwargs（不加载 .env.*.local，不碰任何真实配置）。
# MEMOIR_DEFAULT_BGM_OBJECT_KEY 显式列出：D5 后它移出基础组、改为
# 默认模式条件必填，不再随 _MEMOIR_AUDIO_BASE_REQUIRED_STRINGS 展开。
_AUDIO_ISOLATED_ENV_NAMES = (
    "MEMOIR_AUDIO_ENABLED",
    "MEMOIR_MUSIC_GENERATION_ENABLED",
    "MEMOIR_DEFAULT_BGM_OBJECT_KEY",
    *config._MEMOIR_AUDIO_BASE_REQUIRED_STRINGS,
    *config._MEMOIR_AUDIO_GENERATION_REQUIRED_STRINGS,
)


def _audio_settings(monkeypatch: pytest.MonkeyPatch, **kwargs: str | bool) -> Settings:
    """构造与真实 env / dotenv 完全隔离的音频 Settings 实例。"""
    for name in _AUDIO_ISOLATED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, **kwargs)


def test_memoir_music_generation_frozen_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """M8 默认配乐冻结默认值：生成子开关 False、默认源 key 空串。"""
    isolated = _audio_settings(monkeypatch)
    assert isolated.MEMOIR_MUSIC_GENERATION_ENABLED is False
    assert isolated.MEMOIR_DEFAULT_BGM_OBJECT_KEY == ""


def _default_bgm_base_kwargs() -> dict[str, str | bool]:
    """默认配乐模式下启用音频所需的全部合法基础字段（不含生成三项）。

    显式传 ENVIRONMENT 与全部被校验字段，隔离开发者真实 env / env 文件，
    测试不依赖任何外部配置。
    """
    return {
        "ENVIRONMENT": "test",
        "MEMOIR_AUDIO_ENABLED": True,
        "MEMOIR_TTS_API_KEY": "test-only-key",
        "VOLCANO_CV_ACCESS_KEY": "test-ak",
        "VOLCANO_CV_SECRET_KEY": "test-sk",
        "MEMOIR_TTS_PRICE_PER_1000_TEXT_WORDS": "1",
        "MEMOIR_AUDIO_MAX_COST_PER_RUN": "10",
        "MEMOIR_AUDIO_COST_CURRENCY": "CNY",
        "MEMORY_AUDIO_OSS_ENDPOINT": "oss-cn-hangzhou.aliyuncs.com",
        "MEMORY_AUDIO_OSS_BUCKET": "bucket",
        "MEMORY_AUDIO_OSS_ACCESS_KEY_ID": "ak",
        "MEMORY_AUDIO_OSS_ACCESS_KEY_SECRET": "sk",
        "MEMORY_AUDIO_NARRATOR_PREFIX": "memoir-test/audios/narrator/",
        "MEMORY_AUDIO_BACKGROUND_PREFIX": "memoir-test/audios/background/",
        "MEMORY_AUDIO_SCOPE_HMAC_KEY": "scope-key",
        "MEMOIR_AUDIO_INPUT_HMAC_KEY": "test-input-key",
        "MEMOIR_DEFAULT_BGM_OBJECT_KEY": "memoir-test/audios/default/memoirs.mp3",
    }


def _generation_kwargs() -> dict[str, str | bool]:
    """生成模式（火山原创配乐）完整合法配置 = 基础 15 项 + 生成三项。"""
    kwargs = _default_bgm_base_kwargs()
    kwargs.update(
        MEMOIR_MUSIC_GENERATION_ENABLED=True,
        MEMOIR_MUSIC_ACTION="GenBGM",
        MEMOIR_MUSIC_PRICE_PER_SECOND="0.1",
        MEMOIR_MUSIC_DOWNLOAD_ALLOWED_HOSTS_JSON='["toc-host.example.com"]',
    )
    return kwargs


def test_default_mode_full_base_config_passes_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认模式：基础 15 项齐全即通过，无需任何音乐生成配置。"""
    config.validate_memoir_audio_settings(
        _audio_settings(monkeypatch, **_default_bgm_base_kwargs())
    )


def test_default_mode_requires_base_fields_including_default_bgm_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认模式缺失列表含 MEMOIR_DEFAULT_BGM_OBJECT_KEY 等基础字段。"""
    kwargs = _default_bgm_base_kwargs()
    kwargs.pop("MEMOIR_DEFAULT_BGM_OBJECT_KEY")
    with pytest.raises(ValueError, match="缺少 MEMOIR_DEFAULT_BGM_OBJECT_KEY"):
        config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))
    kwargs = _default_bgm_base_kwargs()
    kwargs.pop("MEMORY_AUDIO_SCOPE_HMAC_KEY")
    with pytest.raises(ValueError, match="缺少 MEMORY_AUDIO_SCOPE_HMAC_KEY"):
        config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))


def test_default_mode_does_not_require_generation_only_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认模式：生成三项不进缺失列表、取值也不校验（允许残缺占位值）。"""
    kwargs = _default_bgm_base_kwargs()
    kwargs.update(
        MEMOIR_MUSIC_ACTION="",
        MEMOIR_MUSIC_PRICE_PER_SECOND="not-a-decimal",
        MEMOIR_MUSIC_DOWNLOAD_ALLOWED_HOSTS_JSON="not-json",
    )
    config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))


def test_generation_mode_requires_generation_only_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生成模式：缺失列表在基础 15 项外追加生成三项。"""
    kwargs = _default_bgm_base_kwargs()
    kwargs["MEMOIR_MUSIC_GENERATION_ENABLED"] = True
    with pytest.raises(ValueError) as exc_info:
        config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))
    message = str(exc_info.value)
    for field in (
        "MEMOIR_MUSIC_ACTION",
        "MEMOIR_MUSIC_PRICE_PER_SECOND",
        "MEMOIR_MUSIC_DOWNLOAD_ALLOWED_HOSTS_JSON",
    ):
        assert f"缺少 {field}" in message


def test_generation_mode_keeps_existing_generation_validations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生成模式保留全部现行校验：Action、音乐单价、hosts 精确白名单。"""
    config.validate_memoir_audio_settings(
        _audio_settings(monkeypatch, **_generation_kwargs())
    )
    bad_cases: list[dict[str, str]] = [
        {"MEMOIR_MUSIC_ACTION": "NotAnAction"},
        {"MEMOIR_MUSIC_PRICE_PER_SECOND": "abc"},
        {"MEMOIR_MUSIC_DOWNLOAD_ALLOWED_HOSTS_JSON": '["bad*host.example.com"]'},
    ]
    for overrides in bad_cases:
        kwargs = _generation_kwargs()
        kwargs.update(overrides)
        with pytest.raises(ValueError, match="MEMOIR_AUDIO_ENABLED 配置非法"):
            config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))


@pytest.mark.parametrize(
    "bad_key",
    [
        "/memoir-test/audios/default/memoirs.mp3",  # 前导 /
        "memoir-test/audios/default/memoirs.wav",  # 非 .mp3 扩展名
        "memoir-test/audios/bg/memoirs.mp3",  # 父目录不是 default
        "memoir-test/audios/default/sub/memoirs.mp3",  # 文件不在 default 直下
        "memoir-test/audios/default/../default/memoirs.mp3",  # .. 穿越
        "memoir-test/audios//default/memoirs.mp3",  # 空段
        "https://cdn.example.com/default/memoirs.mp3",  # URL 形态
        "memoir-test/audios/narrator/default/memoirs.mp3",  # 与旁白前缀重叠
        "memoir-test/audios/background/default/memoirs.mp3",  # 与配乐前缀重叠
        "memoirs.mp3",  # 缺父目录
    ],
)
def test_default_bgm_object_key_rejects_illegal_values(
    bad_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认源 key 违反冻结 §2.3 任一规则即拒绝，消息含字段名。"""
    kwargs = _default_bgm_base_kwargs()
    kwargs["MEMOIR_DEFAULT_BGM_OBJECT_KEY"] = bad_key
    with pytest.raises(ValueError, match="MEMOIR_DEFAULT_BGM_OBJECT_KEY"):
        config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))


def test_default_bgm_object_key_rejects_test_prefix_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """production 拒 memoir-test/ 默认源；合法生产键通过。"""
    kwargs = _default_bgm_base_kwargs()
    kwargs.update(
        ENVIRONMENT="production",
        MEMORY_AUDIO_NARRATOR_PREFIX="memoir/audios/narrator/",
        MEMORY_AUDIO_BACKGROUND_PREFIX="memoir/audios/background/",
        MEMOIR_DEFAULT_BGM_OBJECT_KEY="memoir/audios/default/memoirs.mp3",
    )
    config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))

    kwargs["MEMOIR_DEFAULT_BGM_OBJECT_KEY"] = "memoir-test/audios/default/memoirs.mp3"
    with pytest.raises(ValueError, match="MEMOIR_DEFAULT_BGM_OBJECT_KEY"):
        config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))


def test_default_bgm_object_key_error_does_not_echo_config_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """校验错误消息只含字段名与约束描述，不回显任何配置值。"""
    kwargs = _default_bgm_base_kwargs()
    kwargs["MEMOIR_DEFAULT_BGM_OBJECT_KEY"] = (
        "memoir-test/audios/sentinel-dir/sentinel-source.mp3"
    )
    with pytest.raises(ValueError, match="MEMOIR_DEFAULT_BGM_OBJECT_KEY") as exc_info:
        config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))
    assert "sentinel-dir" not in str(exc_info.value)
    assert "sentinel-source" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# D5（必要修复轮）：默认源 key 同根校验 + 模式条件必填
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "object_key",
    [
        # 跨环境：test 环境读正式源（双向另一向见 production 用例）。
        "memoir/audios/default/memoirs.mp3",
        # 跨根：与两作品前缀毫无共同音频根。
        "other-app/default/memoirs.mp3",
        # 无根：不在任何共同音频根下。
        "default/memoirs.mp3",
    ],
)
def test_default_bgm_object_key_must_share_audio_root_with_work_prefixes(
    object_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D5：默认源必须位于 narrator/background 共同音频根的 default/ 下。

    test 环境两作品前缀均为 memoir-test/audios/...；独立复核记录
    2026-09-12 证实修复前上述三类 key 全部被接受，必须全部拒绝。
    """
    kwargs = _default_bgm_base_kwargs()
    kwargs["MEMOIR_DEFAULT_BGM_OBJECT_KEY"] = object_key
    with pytest.raises(ValueError, match="MEMOIR_DEFAULT_BGM_OBJECT_KEY"):
        config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))


def test_default_bgm_object_key_cross_root_rejected_in_production_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D5：同根校验由前缀推导、双向生效——production 前缀为 memoir/audios/
    时，跨根 key（不含 memoir-test 字样）也必须被拒绝。"""
    kwargs = _default_bgm_base_kwargs()
    kwargs.update(
        ENVIRONMENT="production",
        MEMORY_AUDIO_NARRATOR_PREFIX="memoir/audios/narrator/",
        MEMORY_AUDIO_BACKGROUND_PREFIX="memoir/audios/background/",
        MEMOIR_DEFAULT_BGM_OBJECT_KEY="other-app/default/memoirs.mp3",
    )
    with pytest.raises(ValueError, match="MEMOIR_DEFAULT_BGM_OBJECT_KEY"):
        config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))


def test_default_bgm_object_key_audio_root_derived_not_hardcoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D5：合法性来自前缀推导而非硬编码 memoir-test / memoir。

    未来整体换根（如 future-app/audios/）+ 与新根匹配的 default 源
    必须通过；硬编码两个冻结值的实现会在此用例上失败。
    """
    kwargs = _default_bgm_base_kwargs()
    kwargs.update(
        MEMORY_AUDIO_NARRATOR_PREFIX="future-app/audios/narrator/",
        MEMORY_AUDIO_BACKGROUND_PREFIX="future-app/audios/background/",
        MEMOIR_DEFAULT_BGM_OBJECT_KEY="future-app/audios/default/memoirs.mp3",
    )
    config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))


def test_default_bgm_object_key_rejected_when_prefixes_share_no_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D5：两作品前缀无任何共同目录段（无法推导共同音频根）时，
    默认源无从满足同根约束，必须拒绝并只报字段名与约束。"""
    kwargs = _default_bgm_base_kwargs()
    kwargs["MEMORY_AUDIO_BACKGROUND_PREFIX"] = "other-tree/audios/background/"
    with pytest.raises(ValueError, match="MEMOIR_DEFAULT_BGM_OBJECT_KEY"):
        config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))


def test_generation_mode_does_not_require_default_bgm_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D5：默认源 key 仅默认模式必填；生成模式缺失不得阻断装配。

    修复前 key 在两模式公共必填组：生成模式缺 key 即启动失败，
    无关依赖会关闭已有付费生成能力。
    """
    kwargs = _generation_kwargs()
    kwargs.pop("MEMOIR_DEFAULT_BGM_OBJECT_KEY")
    config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))


def test_generation_mode_validates_default_bgm_key_when_filled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D5：生成模式填了默认源 key 时校验规则与默认模式完全一致
    （同根约束同样生效，不给生成模式留弱化通道）。"""
    kwargs = _generation_kwargs()
    kwargs["MEMOIR_DEFAULT_BGM_OBJECT_KEY"] = "other-app/default/memoirs.mp3"
    with pytest.raises(ValueError, match="MEMOIR_DEFAULT_BGM_OBJECT_KEY"):
        config.validate_memoir_audio_settings(_audio_settings(monkeypatch, **kwargs))
