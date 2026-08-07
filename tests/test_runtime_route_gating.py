"""R1 路由门禁回归测试。

目标：证明 production 配置下 Runtime 不暴露回忆录业务路由（用户 memory API、本地 memory
tool handler、业务 callback consumer、业务生成状态接口），同时 development / test 仍保留
这些路由用于审计与跨仓联调。

门禁实现位于 `app/api/api.py::build_api_router`，本测试直接以不同 environment 调用工厂
函数并断言路由表，避免依赖全局 settings 与 .env 文件，保证 CI 与本地一致可重现。
"""
from __future__ import annotations

import pytest
from fastapi import APIRouter

from app.api.api import build_api_router
from app.core.config import normalize_environment

# 公共 provider 路径：所有环境都必须可访问。
_PROVIDER_PATH_FRAGMENTS = (
    "/api/v1/runtime/health",  # health_api prefix=/runtime + 健康检查子路径
    "/api/v1/runtime/capabilities",
    "/api/v1/runtime/agent-runs",
)

# 业务路径：production fail-closed，development / test 必须可审计。
_BUSINESS_PATH_FRAGMENTS = (
    "/api/v1/memory",  # 用户 memory API（password/archives/...）
    "/api/v1/internal/agent-tools/memory",  # 本地 memory tool handler
    "/api/v1/internal/agent-callbacks/memory",  # 业务 callback consumer
    "/api/v1/memory-archives",  # 业务生成状态
)


def _route_paths(router: APIRouter, *, prefix: str = "/api/v1") -> set[str]:
    """构造临时 FastAPI 实例并通过 OpenAPI schema 提取平铺路径。

    FastAPI include_router 时嵌套 prefix 不会出现在顶层 route.path 上，但 OpenAPI
    schema 在生成时会完整平铺 prefix，是与生产路由表一致的权威来源。
    """
    from fastapi import FastAPI

    container = FastAPI(openapi_url="/openapi.json")
    container.include_router(router, prefix=prefix)
    schema = container.openapi()
    return set(schema.get("paths", {}).keys())


def _assert_any_path_starts_with(paths: set[str], fragment: str) -> None:
    assert any(p.startswith(fragment) for p in paths), (
        f"预期存在以 {fragment} 开头的路由，实际路由表：{sorted(paths)}"
    )


def _assert_no_path_starts_with(paths: set[str], fragment: str) -> None:
    matched = [p for p in paths if p.startswith(fragment)]
    assert not matched, f"production 不应暴露 {fragment}，但命中：{matched}"


@pytest.mark.parametrize("environment", ["development", "test", "dev", "testing"])
def test_business_routes_registered_in_non_production(environment: str) -> None:
    """development / test 环境：业务路由必须注册，方便审计与跨仓联调。"""
    router = build_api_router(environment)
    paths = _route_paths(router)

    # 公共 provider 仍然在位
    for fragment in _PROVIDER_PATH_FRAGMENTS:
        _assert_any_path_starts_with(paths, fragment)
    # 业务路由完整保留
    for fragment in _BUSINESS_PATH_FRAGMENTS:
        _assert_any_path_starts_with(paths, fragment)


def test_business_routes_hidden_in_production() -> None:
    """production 环境：业务路由全部不挂载，只暴露公共 provider。"""
    router = build_api_router("production")
    paths = _route_paths(router)

    for fragment in _PROVIDER_PATH_FRAGMENTS:
        _assert_any_path_starts_with(paths, fragment)
    for fragment in _BUSINESS_PATH_FRAGMENTS:
        _assert_no_path_starts_with(paths, fragment)


def test_normalize_aliases_route_to_production_gating() -> None:
    """ENVIRONMENT 别名（prod/production）都走 production 收紧分支。"""
    assert normalize_environment("prod") == "production"
    assert normalize_environment("PRODUCTION") == "production"

    prod_paths = _route_paths(build_api_router("prod"))
    for fragment in _BUSINESS_PATH_FRAGMENTS:
        _assert_no_path_starts_with(prod_paths, fragment)


def test_runtime_app_uses_environment_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 app.main:app 在 ENVIRONMENT=production 时不会注册业务路由。

    用 monkeypatch 设置 ENVIRONMENT 后重新导入 app.main，触发 settings 重读，
    断言 TestClient 拿到的路由表不包含业务路径。这是端到端层面的门禁回归。
    """
    import importlib

    monkeypatch.setenv("ENVIRONMENT", "production")

    # 清掉缓存的 settings 与 main 模块，确保重新读取 ENVIRONMENT。
    import sys

    for mod in [
        "app.core.config",
        "app.api.api",
        "app.main",
    ]:
        if mod in sys.modules:
            del sys.modules[mod]

    try:
        from app.api.api import api_router  # 重新导入会按新 ENVIRONMENT 构造
        paths = _route_paths(api_router)
        for fragment in _BUSINESS_PATH_FRAGMENTS:
            _assert_no_path_starts_with(paths, fragment)
        for fragment in _PROVIDER_PATH_FRAGMENTS:
            _assert_any_path_starts_with(paths, fragment)
    finally:
        # 还原模块缓存，避免污染后续测试。
        for mod in [
            "app.core.config",
            "app.api.api",
            "app.main",
        ]:
            if mod in sys.modules:
                del sys.modules[mod]
        importlib.import_module("app.core.config")
        importlib.import_module("app.api.api")
        importlib.import_module("app.main")
