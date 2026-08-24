from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from app.runtime.model_gateway import ModelRoute

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


class ExternalObservabilityConfig(BaseModel):
    """外部观测治理声明；缺失字段时 exporter 必须保持关闭。"""

    enabled: bool
    data_classification: str
    sampled_fields: tuple[str, ...]
    region: str
    retention_days: int
    audit_permission: str
    privacy_purge_supported: bool


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
    RUNTIME_EXTERNAL_EXPORTER_ENABLED: bool = False
    RUNTIME_EXTERNAL_EXPORTER_DATA_CLASSIFICATION: str = ""
    RUNTIME_EXTERNAL_EXPORTER_SAMPLED_FIELDS: str = ""
    RUNTIME_EXTERNAL_EXPORTER_REGION: str = ""
    RUNTIME_EXTERNAL_EXPORTER_RETENTION_DAYS: int = 0
    RUNTIME_EXTERNAL_EXPORTER_AUDIT_PERMISSION: str = ""
    RUNTIME_EXTERNAL_EXPORTER_PRIVACY_PURGE_SUPPORTED: bool = False
    # 生产环境必须显式确认审计账本已持久化且访问受控；缺失时 readiness fail-closed。
    RUNTIME_AUDIT_SINK_CONFIGURED: bool = True
    BACKEND_CORS_ORIGINS: str = (
        "http://localhost,"
        "http://127.0.0.1,"
        "http://localhost:3000,"
        "http://127.0.0.1:3000,"
        "http://localhost:5173,"
        "http://127.0.0.1:5173"
    )

    MYSQL_ROOT_PASSWORD: str = ""
    MYSQL_DATABASE: str = "couple_diary_agent_runtime_dev"
    MYSQL_USER: str = ""
    MYSQL_PASSWORD: str = ""

    DB_DRIVER: str = "mysql+mysqlconnector"
    DB_AUTO_CREATE: bool = False
    DB_USER: str = "root"
    DB_PASSWORD: str = ""
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_NAME: str = "couple_diary_agent_runtime_dev"
    DB_CHARSET: str = "utf8mb4"
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE: int = 3600
    DB_POOL_TIMEOUT: int = 30

    # Runtime 调用方与连接器配置。仅服务端读取，绝不写入普通接口响应或日志。
    RUNTIME_ID: str = "agent-runtime"
    RUNTIME_TRUSTED_CLIENTS_JSON: str = (
        '{"couple-diary":{"tenant_id":"couple-diary",'
        '"keys":{"dev":"development-secret"}}}'
    )
    RUNTIME_BUSINESS_CONNECTORS_JSON: str = (
        '{"couple_diary_backend":{"enabled":true,'
        '"base_url":"http://127.0.0.1:8002","runtime_id":"agent-runtime",'
        '"key_id":"dev","secret":"runtime-tool-development-secret"}}'
    )
    RUNTIME_CALLBACK_TARGETS_JSON: str = (
        '{"memory_callback":{"enabled":true,'
        '"url":"http://127.0.0.1:8002/api/v1/internal/memory-callbacks",'
        '"runtime_id":"agent-runtime","key_id":"dev",'
        '"secret":"runtime-tool-development-secret"}}'
    )
    # 开发联调逃生门：connector 指向本机业务后端（127.0.0.1）时由运维显式
    # 开启，ToolGateway 跳过公网 DNS/对端复核。生产默认关闭，且仅
    # development/test 环境允许置真（见下方 validator）。
    RUNTIME_TOOL_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS: bool = False
    RUNTIME_SIGNATURE_TOLERANCE_SECONDS: int = 300
    RUNTIME_ADMISSION_MAX_HELD: int = 100
    RUNTIME_ADMISSION_MAX_QUEUED: int = 500
    RUNTIME_ADMISSION_MAX_RUNNING: int = 50
    # 模型路由只从部署配置读取；业务请求不得自带 endpoint、价格或限流参数。
    MODEL_ROUTES_JSON: str = "[]"
    RUNTIME_REDIS_URL: str = ""
    MEMOIR_MODEL_NODE_ROUTES_JSON: str = "{}"
    # Provider API Key 只经部署 env 注入（route_id -> key），不进 route JSON、日志或响应；
    # 用于 openai_compatible Provider 的 Authorization Bearer 头。
    MODEL_PROVIDER_API_KEYS_JSON: str = "{}"
    MEMORY_SNAPSHOT_FERNET_KEY: str = "UIdCWOsJY0GWrMpXlM444_JDKJC-zFwylDAJCymPvPg="
    MEMORY_TOOL_TRUSTED_RUNTIMES_JSON: str = '{"agent-runtime":{"keys":{"dev":"runtime-tool-development-secret"}}}'
    # 回忆录业务 worker 调用 Runtime 的服务身份；仅允许部署环境注入，绝不回传。
    MEMORY_RUNTIME_BASE_URL: str = "http://127.0.0.1:8002"
    MEMORY_RUNTIME_CLIENT_ID: str = "couple-diary"
    MEMORY_RUNTIME_KEY_ID: str = "dev"
    MEMORY_RUNTIME_SECRET: str = "development-secret"
    MEMORY_RUNTIME_TIMEOUT_SECONDS: float = 5.0
    MEMORY_RUNTIME_CAPABILITY_TTL_SECONDS: int = 60
    # S3 兼容私有桶配置；五项全空表示媒体能力未接入，任一项缺失则启动拒绝。
    MEMORY_MEDIA_S3_ENDPOINT_URL: str = ""
    MEMORY_MEDIA_S3_BUCKET: str = ""
    MEMORY_MEDIA_S3_REGION: str = ""
    MEMORY_MEDIA_S3_ACCESS_KEY_ID: str = ""
    MEMORY_MEDIA_S3_SECRET_ACCESS_KEY: str = ""
    # 媒体签名地址最长五分钟，默认一分钟；该值不得由用户请求覆盖。
    MEMORY_MEDIA_SIGNED_URL_TTL_SECONDS: int = 60
    # 微信登录模块签发用户 JWT 后，本服务只验签并提取数字 sub；空值 fail-closed。
    USER_AUTH_JWT_SECRET: str = ""
    USER_AUTH_JWT_ISSUER: str = "couple-diary"
    

    # 阿里云配置
    ACCESS_KEY_ID: str = '' # 阿里云Access Key ID
    ACCESS_KEY_SECRET: str = '' # 阿里云Access Key Secret
    BUCKET_NAME: str = '' # 阿里云Bucket Name
    REGION: str = '' # 阿里云Region
    ENDPOINT: str = '' # 阿里云Endpoint
    OSS_AUDIO_PREFIX: str = 'audio' # AI制作音频上传到 OSS 的目录前缀

    # M6 回忆录媒体通道（图片生成）。主开关默认关闭：关闭时 1.0.3 运行的
    # image 场景统一降级为文本卡，发布纯文字版本，旧版本行为完全不变。
    MEMOIR_MEDIA_ENABLED: bool = False # 媒体生成总开关
    MEMOIR_MEDIA_PROVIDER: str = 'mock' # 图像 provider：mock（开发/测试）| volcano（真实计费 API）
    MEMOIR_MEDIA_IMAGE_PREFIX: str = 'memoir/images/' # 生成图片对象 key 的强制前缀（D1 冻结契约）
    MEMOIR_MEDIA_MAX_IMAGES_PER_RUN: int = 8 # 单次 Run 最多生成图片张数（按张配额上限，未被 model_policy 覆盖时的默认值）
    MEMOIR_MEDIA_URL_HOST_SUFFIXES: str = 'aliyuncs.com' # 媒体 URL 域名后缀白名单（逗号分隔）
    # 单张图片提交、轮询与网络重试总超时，须显著小于 90s 节点租约。
    MEMOIR_MEDIA_IMAGE_TIMEOUT_SECONDS: float = 25.0
    MEMOIR_MEDIA_IMAGE_MAX_RETRIES: int = 1 # 单张图片有限重试次数
    MEMOIR_MEDIA_NODE_BUDGET_SECONDS: float = 75.0 # 媒体节点整体时间预算（90s 租约内留出安全余量；1.0.4 每场景配图最多 8 张、实测单张 ~7s，60s 不够）
    # 照片出域门禁：图生图需要把用户照片字节发给图像 Provider；该门禁默认
    # 关闭，关闭时即使素材含 images 也只走文生图，绝不外发照片。
    MEMOIR_MEDIA_PHOTO_EGRESS_ENABLED: bool = False
    # 图像 Provider 的数据驻留声明；仅当值为 public 时才允许照片出域，
    # 与上面门禁组成两个独立开关（都默认关闭/私有）。
    MEMOIR_MEDIA_PROVIDER_RESIDENCY: str = 'private'
    # 火山视觉智能异步图像 API 凭证；只经部署 env 注入，绝不写日志或响应。
    VOLCANO_CV_ACCESS_KEY: str = ''
    VOLCANO_CV_SECRET_KEY: str = ''
    VOLCANO_CV_REGION: str = 'cn-north-1'
    VOLCANO_CV_HOST: str = 'visual.volcengineapi.com'

    model_config = SettingsConfigDict(
        env_file=ACTIVE_ENV_FILES,
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    @field_validator("RUNTIME_TOOL_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS")
    @classmethod
    def validate_private_connector_only_in_dev(cls, value: bool, info: ValidationInfo) -> bool:
        """私网 connector 放行开关只允许 development/test 开启。

        生产/预发误配置该开关会在启动时立即失败（fail-fast），保住
        ToolGateway 的 SSRF 公网校验这一安全不变量。
        """

        environment = info.data.get("ENVIRONMENT")
        if value and environment not in {"development", "test"}:
            raise ValueError(
                "RUNTIME_TOOL_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS 仅允许在 "
                "development/test 环境开启"
            )
        return value

    @field_validator("MODEL_ROUTES_JSON")
    @classmethod
    def validate_model_routes_json(cls, value: str) -> str:
        """在配置加载时拒绝无效或重复的模型路由。"""
        from app.runtime.model_gateway import ModelRouteRegistry

        try:
            routes = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("MODEL_ROUTES_JSON 必须是 JSON 数组") from exc
        if not isinstance(routes, list) or not all(isinstance(item, dict) for item in routes):
            raise ValueError("MODEL_ROUTES_JSON 必须是对象数组")
        ModelRouteRegistry.from_config(routes)
        return value

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
    def external_observability(self) -> ExternalObservabilityConfig:
        """将环境变量收口为不含 endpoint/secret 的 exporter 治理配置。"""
        return ExternalObservabilityConfig(
            enabled=self.RUNTIME_EXTERNAL_EXPORTER_ENABLED,
            data_classification=self.RUNTIME_EXTERNAL_EXPORTER_DATA_CLASSIFICATION,
            # 环境变量常写成逗号加空格；标准化后仍只允许 exporter 白名单字段。
            sampled_fields=tuple(
                field
                for raw_field in self.RUNTIME_EXTERNAL_EXPORTER_SAMPLED_FIELDS.split(",")
                if (field := raw_field.strip())
            ),
            region=self.RUNTIME_EXTERNAL_EXPORTER_REGION,
            retention_days=self.RUNTIME_EXTERNAL_EXPORTER_RETENTION_DAYS,
            audit_permission=self.RUNTIME_EXTERNAL_EXPORTER_AUDIT_PERMISSION,
            privacy_purge_supported=self.RUNTIME_EXTERNAL_EXPORTER_PRIVACY_PURGE_SUPPORTED,
        )

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

    @property
    def runtime_id(self) -> str:
        """返回 Runtime 实例标识，仅用于安全日志和健康检查。"""
        return self.RUNTIME_ID

    @property
    def trusted_clients(self) -> dict[str, dict[str, object]]:
        return json.loads(self.RUNTIME_TRUSTED_CLIENTS_JSON)

    @property
    def business_connectors(self) -> dict[str, dict[str, object]]:
        return json.loads(self.RUNTIME_BUSINESS_CONNECTORS_JSON)

    @property
    def callback_targets(self) -> dict[str, dict[str, object]]:
        """返回部署预注册 callback 目标；业务请求不得提供出站 URL 或密钥。"""
        return json.loads(self.RUNTIME_CALLBACK_TARGETS_JSON)

    @property
    def signature_tolerance_seconds(self) -> int:
        return self.RUNTIME_SIGNATURE_TOLERANCE_SECONDS

    @property
    def memory_tool_runtimes(self) -> dict[str, dict[str, object]]:
        return json.loads(self.MEMORY_TOOL_TRUSTED_RUNTIMES_JSON)

    @property
    def admission_max_held(self) -> int:
        return self.RUNTIME_ADMISSION_MAX_HELD

    @property
    def admission_max_queued(self) -> int:
        return self.RUNTIME_ADMISSION_MAX_QUEUED

    @property
    def admission_max_running(self) -> int:
        return self.RUNTIME_ADMISSION_MAX_RUNNING

    @property
    def model_routes(self) -> list[ModelRoute]:
        """解析并校验部署预注册的模型路由。

        延迟导入避免基础配置在应用启动时引入 HTTP/Redis 运行时依赖。
        """
        from app.runtime.model_gateway import ModelRoute, ModelRouteRegistry

        try:
            routes = json.loads(self.MODEL_ROUTES_JSON)
        except json.JSONDecodeError as exc:
            raise ValueError("MODEL_ROUTES_JSON 必须是 JSON 数组") from exc
        if not isinstance(routes, list) or not all(isinstance(item, dict) for item in routes):
            raise ValueError("MODEL_ROUTES_JSON 必须是对象数组")
        model_routes = [ModelRoute.from_mapping(item) for item in routes]
        ModelRouteRegistry(model_routes)
        return model_routes

    @property
    def memoir_model_node_routes(self) -> dict[str, str]:
        """返回固定 Memoir 节点到可信 route ID 的部署映射。"""
        try:
            routes = json.loads(self.MEMOIR_MODEL_NODE_ROUTES_JSON)
        except json.JSONDecodeError as exc:
            raise ValueError("MEMOIR_MODEL_NODE_ROUTES_JSON 必须是 JSON 对象") from exc
        if not isinstance(routes, dict) or not all(
            isinstance(node_id, str) and isinstance(route_id, str) and route_id
            for node_id, route_id in routes.items()
        ):
            raise ValueError("MEMOIR_MODEL_NODE_ROUTES_JSON 必须是字符串映射")
        return routes

    @property
    def model_provider_api_keys(self) -> dict[str, str]:
        """返回 route_id 到 Provider API Key 的部署映射（仅内存使用，绝不写日志）。"""
        try:
            keys = json.loads(self.MODEL_PROVIDER_API_KEYS_JSON)
        except json.JSONDecodeError as exc:
            raise ValueError("MODEL_PROVIDER_API_KEYS_JSON 必须是 JSON 对象") from exc
        if not isinstance(keys, dict) or not all(
            isinstance(route_id, str) and isinstance(key, str)
            for route_id, key in keys.items()
        ):
            raise ValueError("MODEL_PROVIDER_API_KEYS_JSON 必须是字符串映射")
        return keys


settings = Settings()
