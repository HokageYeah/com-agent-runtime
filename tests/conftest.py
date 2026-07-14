from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest


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
    from fastapi.testclient import TestClient

    from app.main import app

    with (
        patch("app.main.setup_logging"),
        patch("app.main.database.connect"),
        patch("app.main.database.close"),
    ):
        with TestClient(app) as test_client:
            yield test_client
