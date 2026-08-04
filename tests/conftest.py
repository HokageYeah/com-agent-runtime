from __future__ import annotations

import os
from collections.abc import Generator
from unittest.mock import patch

import pytest

# 测试进程必须与开发者的 .env.*.local 隔离。这些都是固定的假测试值，
# 只在当前 pytest 进程生效，不会覆盖本地文件或被应用日志输出。
os.environ.update(
    {
        "RUNTIME_ID": "agent-runtime",
        "RUNTIME_TRUSTED_CLIENTS_JSON": (
            '{"couple-diary":{"tenant_id":"couple-diary",'
            '"keys":{"dev":"development-secret"}}}'
        ),
        "RUNTIME_BUSINESS_CONNECTORS_JSON": (
            '{"couple_diary_backend":{"enabled":true,'
            '"base_url":"http://127.0.0.1:8002","runtime_id":"agent-runtime",'
            '"key_id":"dev","secret":"runtime-tool-development-secret"}}'
        ),
        "RUNTIME_CALLBACK_TARGETS_JSON": (
            '{"memory_callback":{"enabled":true,'
            '"url":"http://127.0.0.1:8002/api/v1/internal/agent-callbacks/memory",'
            '"runtime_id":"agent-runtime","key_id":"dev",'
            '"secret":"runtime-tool-development-secret"}}'
        ),
        "MEMORY_TOOL_TRUSTED_RUNTIMES_JSON": (
            '{"agent-runtime":{"keys":{"dev":"runtime-tool-development-secret"}}}'
        ),
        "MEMORY_RUNTIME_BASE_URL": "http://127.0.0.1:8002",
        "MEMORY_RUNTIME_CLIENT_ID": "couple-diary",
        "MEMORY_RUNTIME_KEY_ID": "dev",
        "MEMORY_RUNTIME_SECRET": "development-secret",
        "MEMORY_SNAPSHOT_FERNET_KEY": (
            "UIdCWOsJY0GWrMpXlM444_JDKJC-zFwylDAJCymPvPg="
        ),
        "USER_AUTH_JWT_SECRET": "unit-test-user-jwt-secret",
        "MODEL_ROUTES_JSON": "[]",
        "MEMOIR_MODEL_NODE_ROUTES_JSON": "{}",
        "RUNTIME_REDIS_URL": "",
    }
)


@pytest.fixture
def client() -> Generator[object]:
    """提供带基础 patch 的 TestClient。

    这里把应用生命周期里最常见的三个 patch 收口到 fixture：
    1. 避免测试时重复初始化真实日志
    2. 避免测试直接触发真实数据库连接
    3. 让接口测试只关注 HTTP 行为本身

    后续如果某个测试还需要额外 patch，比如 `database.check_ready()`，
    可以在测试函数里继续叠加，不会和这个 fixture 冲突。
    """
    from unittest.mock import Mock

    from fastapi.testclient import TestClient

    from app.main import app

    with (
        patch("app.main.setup_logging"),
        patch("app.main.database.connect"),
        patch("app.main.database.close"),
        patch("app.main.database.get_session_factory", return_value=Mock()),
        patch("app.main.database.check_ready", return_value=(True, {"database": "ready"})),
    ):
        with TestClient(app) as test_client:
            # 测试 fixture 不连接真实数据库；同步更新已初始化 RuntimeHealth 的探活函数。
            app.state.runtime_health.database_ready = lambda: (True, {"database": "ready"})
            yield test_client
