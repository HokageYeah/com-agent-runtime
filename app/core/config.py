from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ENVIRONMENT_ALIASES = {
    "dev": "development",
    "development": "development",
    "test": "test",
    "testing": "test",
    "prod": "production",
    "production": "production",
}

ENVIRONMENT_FILES = {
    "development": ".env.development",
    "test": ".env.test",
    "production": ".env.production",
}

PLACEHOLDER_VALUES = {
    "your_mysql_user",
    "your_mysql_password",
    "your_mysql_root_password",
}


def normalize_environment(value: str | None) -> str:
    if not value:
        return "development"
    return ENVIRONMENT_ALIASES.get(value.strip().lower(), "development")


def get_runtime_environment(env: Mapping[str, str] | None = None) -> str:
    source = env or os.environ
    return normalize_environment(source.get("ENVIRONMENT") or source.get("ENV"))


def env_file_for_environment(environment: str) -> str:
    normalized = normalize_environment(environment)
    return ENVIRONMENT_FILES[normalized]


def env_files_for_environment(environment: str) -> tuple[str, str, str]:
    """返回当前环境的配置加载顺序。

    加载顺序说明：
    1. 先加载仓库内可跟踪的基础模板文件
    2. 再加载当前环境专属的本地覆盖文件
    3. 最后加载通用的本地覆盖文件

    这样既能保留团队共享模板，也能让个人开发者把密码放在本地文件里，不提交到仓库。
    """
    base_env_file = env_file_for_environment(environment)
    return (
        base_env_file,
        f"{base_env_file}.local",
        ".env.local",
    )


def has_placeholder_database_credentials(username: str, password: str) -> bool:
    """判断当前数据库账号密码是否还是模板占位值。

    这样可以在真正发起数据库连接前，提前给出更清晰的中文错误提示。
    """
    return username in PLACEHOLDER_VALUES or password in PLACEHOLDER_VALUES


def parse_cors_origins(value: str | list[str] | tuple[str, ...]) -> list[str]:
    """把 CORS 配置解析成标准列表。

    支持三种常见写法：
    1. 直接传 Python / pydantic 可识别的列表
    2. 传 JSON 数组字符串，例如 `["http://localhost:3000"]`
    3. 传逗号分隔字符串，例如 `http://localhost:3000,http://127.0.0.1:5173`

    这样做的目的，是让本地 `.env`、部署平台环境变量、LLM 自动写配置时，
    都能用比较自然的方式写入，而不是被单一格式卡住。
    """
    if isinstance(value, list | tuple):
        return [item.strip() for item in value if item and item.strip()]

    raw_value = value.strip()
    if not raw_value:
        return []

    if raw_value.startswith("["):
        parsed = json.loads(raw_value)
        if not isinstance(parsed, list):
            raise ValueError("BACKEND_CORS_ORIGINS 的 JSON 值必须是数组")
        return [str(item).strip() for item in parsed if str(item).strip()]

    return [item.strip() for item in raw_value.split(",") if item.strip()]


ACTIVE_ENVIRONMENT = get_runtime_environment()
ACTIVE_ENV_FILES = tuple(
    str(PROJECT_ROOT / env_file_name)
    for env_file_name in env_files_for_environment(ACTIVE_ENVIRONMENT)
)


class ApplicationConfig(BaseModel):
    """应用基础标识配置。

    这组配置描述“这个后端服务是谁、版本号是什么、接口前缀是什么”。
    后续如果让 LLM 或新同事快速理解项目身份信息，优先看这一组。
    """

    project_name: str
    project_description: str
    project_version: str
    api_prefix: str
    response_version: int
    environment: str
    debug: bool


class ServerConfig(BaseModel):
    """应用启动与监听配置。"""

    host: str
    port: int
    reload: bool


class RequestLoggingConfig(BaseModel):
    """请求日志与链路追踪配置。

    这里不追求复杂 tracing，只先把最常用的两类配置收进来：
    1. request_id 响应头字段名
    2. 慢请求阈值
    """

    request_id_header: str
    slow_request_threshold_ms: int


class LoggingConfig(BaseModel):
    """日志系统基础配置。

    这里先保持轻量，只收口当前日志初始化真正依赖的最核心字段。
    如果后续继续扩展日志落盘策略、保留天数或第三方日志级别，
    也优先继续放到这个分组里统一管理。
    """

    logging_level: str


class CorsConfig(BaseModel):
    """跨域配置。"""

    allow_origins: list[str]


class DatabaseRuntimeConfig(BaseModel):
    """数据库运行时配置。

    这组配置是后续最容易继续长大的部分，所以先单独抽出来。
    这样后面即使继续加读写分离、不同数据库实例、连接池策略，
    也能优先在这层扩展，而不是继续把字段平铺在 `Settings` 上。
    """

    driver: str
    username: str
    password: str
    host: str
    port: int
    database: str
    charset: str
    echo: bool
    pool_size: int
    max_overflow: int
    pool_recycle: int
    pool_timeout: int


class Settings(BaseSettings):
    # 这里的项目名和描述代表“整个后端服务”的对外标识。
    # 后续如果继续扩展更多业务模块，不要再写成某一个具体子模块的名字，
    # 否则 OpenAPI 标题、日志、启动信息都会被误导。
    PROJECT_NAME: str = "Couple Diary Backend"
    PROJECT_DESCRIPTION: str = "情侣日记项目后端 API 服务"
    PROJECT_VERSION: str = "0.1.0"
    API_PREFIX: str = "/api/v1"
    DEBUG: bool = False
    ENVIRONMENT: str = ACTIVE_ENVIRONMENT
    VERSION: int = 1

    HOST: str = "127.0.0.1"
    PORT: int = 8002
    RELOAD: bool = False
    REQUEST_ID_HEADER: str = "X-Request-ID"
    SLOW_REQUEST_THRESHOLD_MS: int = 800
    LOGGING_LEVEL: str = "INFO"
    BACKEND_CORS_ORIGINS: str = (
        "http://localhost,"
        "http://127.0.0.1,"
        "http://localhost:3000,"
        "http://127.0.0.1:3000,"
        "http://localhost:5173,"
        "http://127.0.0.1:5173"
    )

    MYSQL_ROOT_PASSWORD: str = ""
    MYSQL_DATABASE: str = "couple_diary_dev"
    MYSQL_USER: str = ""
    MYSQL_PASSWORD: str = ""

    DB_DRIVER: str = "mysql+mysqlconnector"
    DB_USER: str = "root"
    DB_PASSWORD: str = ""
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_NAME: str = "couple_diary_dev"
    DB_CHARSET: str = "utf8mb4"
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE: int = 3600
    DB_POOL_TIMEOUT: int = 30

    model_config = SettingsConfigDict(
        env_file=ACTIVE_ENV_FILES,
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    @property
    def resolved_cors_origins(self) -> list[str]:
        """返回标准化后的 CORS 白名单列表。"""
        return parse_cors_origins(self.BACKEND_CORS_ORIGINS)

    @property
    def application(self) -> ApplicationConfig:
        """返回应用基础配置分组。

        保留这个聚合视图后，后续业务代码如果只关心“应用身份信息”，
        就不用再到处读取零散字段。
        """
        return ApplicationConfig(
            project_name=self.PROJECT_NAME,
            project_description=self.PROJECT_DESCRIPTION,
            project_version=self.PROJECT_VERSION,
            api_prefix=self.API_PREFIX,
            response_version=self.VERSION,
            environment=self.ENVIRONMENT,
            debug=self.DEBUG,
        )

    @property
    def server(self) -> ServerConfig:
        """返回服务监听配置分组。"""
        return ServerConfig(
            host=self.HOST,
            port=self.PORT,
            reload=self.RELOAD,
        )

    @property
    def request_logging(self) -> RequestLoggingConfig:
        """返回请求日志配置分组。"""
        return RequestLoggingConfig(
            request_id_header=self.REQUEST_ID_HEADER,
            slow_request_threshold_ms=self.SLOW_REQUEST_THRESHOLD_MS,
        )

    @property
    def cors(self) -> CorsConfig:
        """返回跨域配置分组。"""
        return CorsConfig(allow_origins=self.resolved_cors_origins)

    @property
    def logging(self) -> LoggingConfig:
        """返回日志系统配置分组。"""
        return LoggingConfig(logging_level=self.LOGGING_LEVEL)

    @property
    def database(self) -> DatabaseRuntimeConfig:
        """返回数据库运行时配置分组。

        这层是给数据库模块、脚本模块、后续部署排查统一消费的，
        避免每个地方都手写一遍字段拼装逻辑。
        """
        return DatabaseRuntimeConfig(
            driver=self.DB_DRIVER,
            username=self.DB_USER,
            password=self.DB_PASSWORD,
            host=self.DB_HOST,
            port=self.DB_PORT,
            database=self.DB_NAME,
            charset=self.DB_CHARSET,
            echo=self.DB_ECHO,
            pool_size=self.DB_POOL_SIZE,
            max_overflow=self.DB_MAX_OVERFLOW,
            pool_recycle=self.DB_POOL_RECYCLE,
            pool_timeout=self.DB_POOL_TIMEOUT,
        )


settings = Settings()
