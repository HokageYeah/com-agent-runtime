from __future__ import annotations

from unittest.mock import PropertyMock, patch

from app.core.api_response import build_api_response
from app.core.config import ApplicationConfig, settings
from app.schemas.common_data import PlatformEnum


def test_build_api_response_allows_explicit_platform_override() -> None:
    """显式传入 platform 参数时，应优先使用调用方指定的枚举值。"""

    response = build_api_response(
        path="/api/v1/demo/ping",
        data={"ok": True},
        ret=["SUCCESS::示例平台响应构建成功"],
        platform=PlatformEnum.DEMO,
    )

    assert response.platform == PlatformEnum.DEMO
    assert response.api == "api/v1/demo/ping"
    assert response.data == {"ok": True}
    assert response.ret == ["SUCCESS::示例平台响应构建成功"]
    assert response.v == 1


def test_build_api_response_prefers_application_group_response_version() -> None:
    """统一响应版本号应优先从 application 分组读取。

    这样后续如果配置组织继续演进，响应构建逻辑仍然只依赖“应用基础配置”
    这一层，而不是继续散落依赖扁平字段。
    """

    with patch.object(
        type(settings),
        "application",
        new_callable=PropertyMock,
        return_value=ApplicationConfig(
            project_name=settings.PROJECT_NAME,
            project_description=settings.PROJECT_DESCRIPTION,
            project_version=settings.PROJECT_VERSION,
            api_prefix=settings.API_PREFIX,
            response_version=99,
            environment=settings.ENVIRONMENT,
            debug=settings.DEBUG,
        ),
    ):
        response = build_api_response(
            path="/api/v1/demo/ping",
            data={"ok": True},
            ret=["SUCCESS::读取 application 分组版本号成功"],
            platform=PlatformEnum.DEMO,
        )

    assert response.v == 99


def test_diary_ping_returns_standard_response_for_diary_route(client) -> None:
    """正式业务样板路由应自动识别为 DIARY 平台。"""

    response = client.get("/api/v1/diary/ping")

    assert response.status_code == 200
    assert response.json()["platform"] == "DIARY"
    assert response.json()["api"] == "api/v1/diary/ping"
    assert response.json()["data"]["module"] == "diary"
    assert response.json()["data"]["status"] == "skeleton_ready"
    assert response.json()["ret"] == ["SUCCESS::获取 diary 模块骨架信息成功"]
    assert response.json()["v"] == 1
    assert response.headers["X-Request-ID"]


def test_demo_hot_topics_returns_standard_response_for_demo_route(client) -> None:
    """demo 路由应自动识别为 DEMO 平台。"""

    async def fake_get_demo_hot_topics(count: int) -> dict:
        return {
            "topics": [{"id": 1, "title": "热门话题 1"}],
            "count": count,
        }

    with patch(
        "app.api.endpoints.demo_api.get_demo_hot_topics",
        side_effect=fake_get_demo_hot_topics,
    ):
        response = client.get("/api/v1/demo/hot-topics", params={"count": 1})

    assert response.status_code == 200
    assert response.json() == {
        "platform": "DEMO",
        "api": "api/v1/demo/hot-topics",
        "data": {
            "topics": [{"id": 1, "title": "热门话题 1"}],
            "count": 1,
        },
        "ret": ["SUCCESS::获取热门话题成功"],
        "v": 1,
    }


def test_search_accounts_response_uses_integer_version_and_full_api_path(
    client,
) -> None:
    """标准响应里的版本号应该统一为 int，api 字段也应该统一使用完整路径。"""

    async def fake_search_demo_accounts(keyword: str, limit: int) -> dict:
        return {
            "accounts": [{"id": "account_1", "name": keyword}],
            "limit": limit,
        }

    with patch(
        "app.api.endpoints.demo_api.search_demo_accounts",
        side_effect=fake_search_demo_accounts,
    ):
        response = client.get(
            "/api/v1/demo/search-accounts",
            params={"keyword": "情侣日记", "limit": 1},
        )

    assert response.status_code == 200
    assert response.json()["platform"] == "DEMO"
    assert response.json()["api"] == "api/v1/demo/search-accounts"
    assert response.json()["v"] == 1


def test_request_validation_error_returns_standard_error_payload(client) -> None:
    """请求参数校验失败时，也应该返回统一错误结构。"""

    response = client.get("/api/v1/demo/search-accounts", params={"limit": 2})

    assert response.status_code == 422
    assert response.json()["platform"] == "DEMO"
    assert response.json()["api"] == "api/v1/demo/search-accounts"
    assert response.json()["v"] == 1
    assert response.json()["data"]["request_id"]
    assert response.json()["data"]["request_method"] == "GET"
    assert "validation_errors" in response.json()["data"]
    assert response.json()["data"]["validation_errors"]


def test_http_exception_endpoint_returns_standard_error_payload(client) -> None:
    """主动抛出的 HTTPException 也应该返回统一错误结构。"""

    response = client.get("/api/v1/demo/error-demo")

    assert response.status_code == 418
    assert response.json()["platform"] == "DEMO"
    assert response.json()["api"] == "api/v1/demo/error-demo"
    assert response.json()["v"] == 1
    assert response.json()["ret"] == ["ERROR::这是一个用于调试错误返回格式的演示接口"]
    assert response.json()["data"]["request_id"]
    assert response.json()["data"]["request_method"] == "GET"
    assert response.json()["data"]["status_code"] == 418


def test_response_validation_error_returns_500_standard_payload(client) -> None:
    """响应模型校验失败必须是 500，且返回统一错误结构。"""

    def invalid_hot_topics_payload(*args, **kwargs) -> dict:
        return {
            "platform": "INVALID_PLATFORM",
            "api": "api/v1/demo/hot-topics",
            "data": {"topics": [], "count": 1},
            "ret": ["SUCCESS::返回了错误的响应模型"],
            "v": 1,
        }

    with patch(
        "app.api.endpoints.demo_api.build_api_response_from_request",
        side_effect=invalid_hot_topics_payload,
    ):
        response = client.get("/api/v1/demo/hot-topics", params={"count": 1})

    assert response.status_code == 500
    assert response.json()["platform"] == "DEMO"
    assert response.json()["api"] == "api/v1/demo/hot-topics"
    assert response.json()["ret"] == ["ERROR::服务端响应格式校验失败"]
    assert response.json()["data"]["request_id"]
    assert response.json()["data"]["request_method"] == "GET"
    assert "validation_errors" in response.json()["data"]
