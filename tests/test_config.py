from __future__ import annotations

from app.core import config


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
