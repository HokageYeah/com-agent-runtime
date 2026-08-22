"""AgentRuntime 本地配置、启动和隔离真实验收入口。"""

from __future__ import annotations

import argparse
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from typing import Protocol
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from dotenv import dotenv_values
from sqlalchemy import URL, create_engine
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENVIRONMENTS = {
    "dev": "development",
    "development": "development",
    "test": "test",
    "prod": "production",
    "production": "production",
}
_PLACEHOLDERS = {
    "your_mysql_user",
    "your_mysql_password",
    "your_mysql_root_password",
    "development-secret",
    "replace_with_service_secret",
    "UIdCWOsJY0GWrMpXlM444_JDKJC-zFwylDAJCymPvPg=",
}
_PLAIN_ENV_VALUE = re.compile(r"^[A-Za-z0-9_./:@+\-]+$")

_LOCAL_CONFIG_SECTIONS = {
    "ENVIRONMENT": "# 应用运行环境：决定加载哪一套基础配置。",
    "DB_DRIVER": "# 数据库连接：应用账号应只授予当前业务库的最小权限。",
    "RUNTIME_ID": "# Runtime 基础治理：本机默认关闭外部导出器。",
    "RUNTIME_TRUSTED_CLIENTS_JSON": "# Runtime 入站身份：与 MEMORY_RUNTIME_SECRET 使用同一 client secret。",
    "RUNTIME_BUSINESS_CONNECTORS_JSON": "# Runtime 出站工具与 callback：身份与密钥必须与业务侧 MEMORY_RUNTIME_* 对称。",
    "MEMORY_RUNTIME_BASE_URL": "# 回忆录业务调用 Runtime 的服务身份：必须与入站注册表一致。",
    "MEMORY_SNAPSHOT_FERNET_KEY": "# 持久化加密与用户登录：轮换前必须先规划旧数据解密和 token 过渡。",
    "MODEL_ROUTES_JSON": "# 模型路由为空时关闭真实模型增强，使用确定性模板降级。",
}

_LOCAL_CONFIG_COMMENTS = {
    "ENVIRONMENT": "[必填/自动确定] 运行环境：由 configure 命令的 development/test 参数生成，不要交叉复制。",
    "HOST": "[必填/手动配置] API 监听主机：由 Service base URL 的主机生成；宿主机运行通常为 127.0.0.1。",
    "PORT": "[必填/手动配置] API 监听端口：由 Service base URL 的显式端口生成，必须与宿主机发布端口一致。",
    "DB_AUTO_CREATE": "[必填/安全默认] 缺库自动创建：development/test 默认 true；production 仅首次受控 bootstrap 临时开启。",
    "DB_DRIVER": "[必填/自动填写] 数据库驱动：当前本地启动固定使用 mysql+mysqlconnector。",
    "DB_USER": "[必填] MySQL 应用账号：由配置时的 DB user 生成；建议使用只有当前数据库权限的独立账号。",
    "DB_PASSWORD": "[必填/手动输入] MySQL 应用密码：在 configure 的隐藏输入中填写，不要复用 root 或生产密码。",
    "DB_HOST": "[必填/手动输入] MySQL 主机：宿主机运行填 127.0.0.1，Docker 内运行填 Compose 的 MySQL service 名。",
    "DB_PORT": "[必填/手动输入] MySQL 端口：容器内始终填 3306；宿主机运行则填实际发布端口。",
    "DB_NAME": "[必填/手动输入] MySQL 库名：必须由 DBA 或本地 MySQL 预先创建，不要指向生产共享库。",
    "DB_CHARSET": "[必填/自动填写] 数据库字符集：固定 utf8mb4，与建库字符集保持一致。",
    "DB_ECHO": "[可选/安全默认] SQL 输出开关：默认 false，避免业务参数进入日志。",
    "RUNTIME_ID": "[必填/自动填写] Runtime 服务身份：本地固定为 agent-runtime。",
    "RUNTIME_AUDIT_SINK_CONFIGURED": "[必填/自动填写] 审计落库就绪声明：只有审计表可持久化且访问受控时才保持 true。",
    "RUNTIME_EXTERNAL_EXPORTER_ENABLED": "[可选/安全默认] 外部观测导出：默认 false，未完成隐私治理前不得开启。",
    "RUNTIME_REDIS_URL": "[必填/手动输入] Redis 地址：宿主机填 redis://127.0.0.1:发布端口/DB，Docker 内填 redis://service:6379/DB。",
    "RUNTIME_TRUSTED_CLIENTS_JSON": "[必填/自动生成] Runtime 入站调用方注册表：含租户、allowlist、授权版本和 client secret。",
    "RUNTIME_TOOL_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS": "[必填/自动确定] 私网 connector 放行开关：business 指向本机/私网时自动 true，公网部署必须 false。",
    "RUNTIME_BUSINESS_CONNECTORS_JSON": "[必填/自动生成] 业务 connector 注册表：base_url 由 Business service base URL 生成，runtime_id/secret 与业务侧 MEMORY_RUNTIME_* 对称，不得在业务请求中覆盖。",
    "RUNTIME_CALLBACK_TARGETS_JSON": "[必填/自动生成] callback 目标注册表：URL 指向业务后端 B10 路由 /api/v1/internal/memory-callbacks，不得携带凭据。",
    "MEMORY_TOOL_TRUSTED_RUNTIMES_JSON": "[必填/自动生成] 业务端信任的 Runtime 注册表：密钥与 client secret 保持一致。",
    "MEMORY_RUNTIME_BASE_URL": "[必填/手动输入] Runtime 服务基础 URL：本地填当前 API 地址，真实环境填经 allowlist 的 HTTPS 地址。",
    "MEMORY_RUNTIME_CLIENT_ID": "[必填/自动填写] Runtime client ID：必须存在于 RUNTIME_TRUSTED_CLIENTS_JSON。",
    "MEMORY_RUNTIME_KEY_ID": "[必填/自动填写] Runtime client key ID：必须存在于对应 client 的 keys 中。",
    "MEMORY_RUNTIME_SECRET": "[必填/自动生成] Runtime client HMAC 密钥：必须与入站注册表的对应 key 完全一致。",
    "MEMORY_RUNTIME_TIMEOUT_SECONDS": "[必填/安全默认] Runtime HTTP 超时秒数：默认 5，不得由业务请求覆盖。",
    "MEMORY_RUNTIME_CAPABILITY_TTL_SECONDS": "[必填/安全默认] capability 缓存秒数：默认 60，撤权时还必须依赖实时授权版本复核。",
    "MEMORY_SNAPSHOT_FERNET_KEY": "[必填/自动生成] Snapshot Fernet 加密密钥：只能使用 Fernet.generate_key 生成，丢失后旧 Snapshot 无法解密。",
    "USER_AUTH_JWT_SECRET": "[必填/自动生成] 用户 JWT 验签密钥：每个环境独立，不得与 HMAC 或数据库密码复用。",
    "USER_AUTH_JWT_ISSUER": "[必填/自动填写] JWT issuer：必须与登录签发端保持一致。",
    "MODEL_ROUTES_JSON": "[可选] 模型部署路由：默认 [] 表示不调用外部模型；真实路由只能由部署管理员配置。",
    "MEMOIR_MODEL_NODE_ROUTES_JSON": "[可选] 回忆录节点到 route_id 的映射：默认 {} 与模型关闭状态一致。",
}


@dataclass(frozen=True)
class LocalSetup:
    database_driver: str
    database_user: str
    database_password: str
    database_host: str
    database_port: int
    database_name: str
    redis_url: str
    service_base_url: str
    # 业务后端（couple-diary-b）地址：connector 工具调用与 callback 都指向它，
    # 不是 Runtime 自身地址；两个进程本机联调时默认 127.0.0.1:8008。
    business_base_url: str


@dataclass(frozen=True)
class DatabaseBootstrapConfig:
    """启动前的最小数据库创建决策；不包含账号、密码或连接串。"""

    environment: str
    database_name: str
    auto_create: bool


class DatabaseBootstrapError(RuntimeError):
    """数据库命名或创建边界被拒绝时只携带固定错误码。"""


class DatabaseServer(Protocol):
    """数据库服务器的最小 bootstrap 接口。"""

    def database_exists(self, database_name: str) -> bool: ...

    def create_database(self, database_name: str) -> None: ...

    def close(self) -> None: ...


_RUNTIME_DATABASE_NAMES = {
    "development": "couple_diary_agent_runtime_dev",
    "test": "couple_diary_agent_runtime_test",
    "production": "couple_diary_agent_runtime_prod",
}


def ensure_runtime_database(
    config: DatabaseBootstrapConfig,
    server: DatabaseServer,
) -> str:
    """只允许当前环境的独立 Runtime 库，绝不尝试业务库。"""
    environment = _normalize_local_environment(config.environment)
    expected_name = _RUNTIME_DATABASE_NAMES[environment]
    if config.database_name != expected_name:
        raise DatabaseBootstrapError("RUNTIME_DATABASE_NAME_MISMATCH")
    if server.database_exists(config.database_name):
        return "existing"
    if not config.auto_create:
        raise DatabaseBootstrapError("RUNTIME_DATABASE_MISSING")
    server.create_database(config.database_name)
    return "created"


class MySQLDatabaseServer:
    """只连接 MySQL 服务器层，不预先选择任何业务库。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> MySQLDatabaseServer:
        if values.get("DB_DRIVER") != "mysql+mysqlconnector":
            raise DatabaseBootstrapError("RUNTIME_DATABASE_DRIVER_UNSUPPORTED")
        try:
            port = int(values.get("DB_PORT", ""))
        except ValueError as exc:
            raise DatabaseBootstrapError("RUNTIME_DATABASE_PORT_INVALID") from exc
        if not 1 <= port <= 65535:
            raise DatabaseBootstrapError("RUNTIME_DATABASE_PORT_INVALID")
        required = ("DB_USER", "DB_PASSWORD", "DB_HOST", "DB_CHARSET")
        if any(not values.get(field, "").strip() for field in required):
            raise DatabaseBootstrapError("RUNTIME_DATABASE_CONFIG_INCOMPLETE")
        url = URL.create(
            drivername="mysql+mysqlconnector",
            username=values["DB_USER"],
            password=values["DB_PASSWORD"],
            host=values["DB_HOST"],
            port=port,
            query={"charset": values["DB_CHARSET"]},
        )
        return cls(
            create_engine(
                url,
                isolation_level="AUTOCOMMIT",
                pool_pre_ping=True,
            )
        )

    def database_exists(self, database_name: str) -> bool:
        try:
            with self._engine.connect() as connection:
                result = connection.execute(
                    sql_text(
                        "SELECT 1 FROM INFORMATION_SCHEMA.SCHEMATA "
                        "WHERE SCHEMA_NAME = :database_name"
                    ),
                    {"database_name": database_name},
                )
                return result.scalar_one_or_none() is not None
        except SQLAlchemyError as exc:
            raise DatabaseBootstrapError("RUNTIME_DATABASE_SERVER_UNAVAILABLE") from exc

    def create_database(self, database_name: str) -> None:
        if not re.fullmatch(r"[a-z0-9_]{1,64}", database_name):
            raise DatabaseBootstrapError("RUNTIME_DATABASE_NAME_INVALID")
        try:
            with self._engine.connect() as connection:
                connection.exec_driver_sql(
                    f"CREATE DATABASE `{database_name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
        except SQLAlchemyError as exc:
            raise DatabaseBootstrapError("RUNTIME_DATABASE_CREATE_FAILED") from exc

    def close(self) -> None:
        self._engine.dispose()


@dataclass(frozen=True)
class ConfigFinding:
    field: str
    code: str


class CommandRunner(Protocol):
    def __call__(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> CompletedProcess[str]: ...


def _normalize_local_environment(environment: str) -> str:
    normalized = _ENVIRONMENTS.get(environment.strip().lower())
    if normalized is None:
        raise ValueError("LOCAL_ENVIRONMENT_UNSUPPORTED")
    return normalized


def _env_line(field: str, value: str | int | bool) -> str:
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    else:
        rendered = str(value)
    if rendered in {"[]", "{}"} or (rendered and _PLAIN_ENV_VALUE.fullmatch(rendered)):
        return f"{field}={rendered}"
    return f"{field}={json.dumps(rendered, ensure_ascii=False)}"


def _validated_origin_url(value: str, error_code: str):
    """校验无路径/查询/片段的 HTTP(S) origin，供 Service/Business 地址复用。"""
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(error_code) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(error_code)
    return parsed


def _business_endpoint_is_private(hostname: str | None) -> bool:
    """判定业务后端地址是否为本机/私网，仅用于 dev 联调放行开关的自动取值。"""
    if not hostname:
        return False
    if hostname.rstrip(".").lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        # DNS 名称无法静态判定，按公网处理，不放宽 SSRF 防线。
        return False
    return not address.is_global


def create_local_config(
    project_root: Path,
    environment: str,
    setup: LocalSetup,
    *,
    overwrite: bool = False,
    secret_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    fernet_key_factory: Callable[[], str] = lambda: Fernet.generate_key().decode("ascii"),
) -> Path:
    """生成只属于当前机器的最小启动配置。

    默认关闭真实模型路由，Worker 会使用确定性模板降级，避免未配置
    Provider 时误触网。生产凭据应由部署平台注入，此入口只服务本地和测试环境。
    """
    normalized = _normalize_local_environment(environment)
    if normalized == "production":
        raise ValueError("LOCAL_CONFIG_FOR_PRODUCTION_FORBIDDEN")
    path = project_root / f".env.{normalized}.local"
    if path.exists() and not overwrite:
        raise FileExistsError("CONFIG_FILE_EXISTS")
    if not setup.database_user or not setup.database_password:
        raise ValueError("DATABASE_CREDENTIALS_REQUIRED")
    if setup.database_driver != "mysql+mysqlconnector":
        raise ValueError("LOCAL_DATABASE_DRIVER_UNSUPPORTED")
    if not 1 <= setup.database_port <= 65535:
        raise ValueError("DATABASE_PORT_INVALID")
    service_url = _validated_origin_url(setup.service_base_url, "SERVICE_BASE_URL_INVALID")
    business_url = _validated_origin_url(
        setup.business_base_url, "BUSINESS_SERVICE_BASE_URL_INVALID"
    )

    # 对称密钥合同（业务侧 B9/B10 冻结）：Runtime 出站调用业务（工具 + callback）
    # 时，X-Agent-Runtime-Id / X-Agent-Key-Id / HMAC 密钥必须与业务侧
    # MEMORY_RUNTIME_CLIENT_ID / MEMORY_RUNTIME_KEY_ID / MEMORY_RUNTIME_SECRET 完全
    # 一致。因此只生成一个 client secret 双向共用，不再生成独立的 tool secret。
    client_secret = secret_factory()
    jwt_secret = secret_factory()
    trusted_clients = {
        "couple-diary": {
            "tenant_id": "couple-diary",
            "keys": {"local": client_secret},
            "agent_ids": ["memoir_agent"],
            "business_types": ["couple_memory"],
            "callback_target_ids": ["memory_callback"],
            "connector_ids": ["couple_diary_backend"],
            "data_domains": ["couple_memory"],
            "authorization_version": 1,
            "model_data_residency": "private",
        }
    }
    connector = {
        "couple_diary_backend": {
            "enabled": True,
            # connector 是 Runtime 调用业务后端的地址，不是 Runtime 自身地址。
            "base_url": setup.business_base_url,
            # 冻结合同：业务侧校验 runtime_id == MEMORY_RUNTIME_CLIENT_ID（couple-diary）。
            "runtime_id": "couple-diary",
            "key_id": "local",
            "secret": client_secret,
        }
    }
    callback_targets = {
        "memory_callback": {
            "enabled": True,
            # 业务侧 B10 的实际路由是 /api/v1/internal/memory-callbacks。
            "url": (
                f"{setup.business_base_url.rstrip('/')}"
                "/api/v1/internal/memory-callbacks"
            ),
            "runtime_id": "couple-diary",
            "key_id": "local",
            "secret": client_secret,
        }
    }
    trusted_runtimes = {"agent-runtime": {"keys": {"local": client_secret}}}
    # 本机双进程联调（business 指向 loopback/私网）时自动放行 ToolGateway 私网
    # 校验；business 指向公网时保持 False，不放宽 SSRF 防线。
    allow_private_endpoints = _business_endpoint_is_private(business_url.hostname)
    values: tuple[tuple[str, str | int | bool], ...] = (
        ("ENVIRONMENT", normalized),
        ("HOST", service_url.hostname),
        ("PORT", service_url.port),
        ("DB_AUTO_CREATE", True),
        ("DB_DRIVER", setup.database_driver),
        ("DB_USER", setup.database_user),
        ("DB_PASSWORD", setup.database_password),
        ("DB_HOST", setup.database_host),
        ("DB_PORT", setup.database_port),
        ("DB_NAME", setup.database_name),
        ("DB_CHARSET", "utf8mb4"),
        ("DB_ECHO", False),
        ("RUNTIME_ID", "agent-runtime"),
        ("RUNTIME_AUDIT_SINK_CONFIGURED", True),
        ("RUNTIME_EXTERNAL_EXPORTER_ENABLED", False),
        ("RUNTIME_REDIS_URL", setup.redis_url),
        ("RUNTIME_TOOL_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS", allow_private_endpoints),
        ("RUNTIME_TRUSTED_CLIENTS_JSON", json.dumps(trusted_clients, separators=(",", ":"))),
        ("RUNTIME_BUSINESS_CONNECTORS_JSON", json.dumps(connector, separators=(",", ":"))),
        ("RUNTIME_CALLBACK_TARGETS_JSON", json.dumps(callback_targets, separators=(",", ":"))),
        ("MEMORY_TOOL_TRUSTED_RUNTIMES_JSON", json.dumps(trusted_runtimes, separators=(",", ":"))),
        ("MEMORY_RUNTIME_BASE_URL", setup.service_base_url),
        ("MEMORY_RUNTIME_CLIENT_ID", "couple-diary"),
        ("MEMORY_RUNTIME_KEY_ID", "local"),
        ("MEMORY_RUNTIME_SECRET", client_secret),
        ("MEMORY_RUNTIME_TIMEOUT_SECONDS", 5),
        ("MEMORY_RUNTIME_CAPABILITY_TTL_SECONDS", 60),
        ("MEMORY_SNAPSHOT_FERNET_KEY", fernet_key_factory()),
        ("USER_AUTH_JWT_SECRET", jwt_secret),
        ("USER_AUTH_JWT_ISSUER", "couple-diary"),
        ("MODEL_ROUTES_JSON", "[]"),
        ("MEMOIR_MODEL_NODE_ROUTES_JSON", "{}"),
    )
    content_lines = (
        "# 由 ./agent-runtime.sh configure 在本机生成；包含凭据，禁止提交、复制到工单或输出到日志。",
        "# 标记说明：[必填/手动输入] 来自交互问答；[必填/自动生成] 由脚本安全生成；[可选] 可保留安全默认。",
    )
    rendered_lines = list(content_lines)
    for field, value in values:
        section = _LOCAL_CONFIG_SECTIONS.get(field)
        if section is not None:
            rendered_lines.extend(("", section))
        rendered_lines.extend((f"# {_LOCAL_CONFIG_COMMENTS[field]}", _env_line(field, value)))
    content = "\n".join(rendered_lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, content.encode("utf-8"))
    finally:
        os.close(descriptor)
    path.chmod(0o600)
    return path


def _load_environment(
    project_root: Path,
    environment: str,
    process_env: Mapping[str, str] | None,
) -> dict[str, str]:
    normalized = _normalize_local_environment(environment)
    merged: dict[str, str] = {}
    for path in (
        project_root / f".env.{normalized}",
        project_root / f".env.{normalized}.local",
        project_root / ".env.local",
    ):
        if path.exists():
            merged.update(
                {key: value for key, value in dotenv_values(path).items() if value is not None}
            )
    merged.update(dict(process_env if process_env is not None else os.environ))
    merged["ENVIRONMENT"] = normalized
    return merged


def inspect_configuration(
    project_root: Path,
    environment: str,
    *,
    process_env: Mapping[str, str] | None = None,
) -> tuple[ConfigFinding, ...]:
    """只报告字段名和固定错误码，不回显配置值。"""
    normalized = _normalize_local_environment(environment)
    values = _load_environment(project_root, normalized, process_env)
    required = (
        "DB_DRIVER",
        "DB_AUTO_CREATE",
        "DB_USER",
        "DB_PASSWORD",
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "RUNTIME_REDIS_URL",
        "RUNTIME_TRUSTED_CLIENTS_JSON",
        "RUNTIME_BUSINESS_CONNECTORS_JSON",
        "RUNTIME_CALLBACK_TARGETS_JSON",
        "MEMORY_TOOL_TRUSTED_RUNTIMES_JSON",
        "MEMORY_RUNTIME_BASE_URL",
        "MEMORY_RUNTIME_CLIENT_ID",
        "MEMORY_RUNTIME_KEY_ID",
        "MEMORY_RUNTIME_SECRET",
        "MEMORY_SNAPSHOT_FERNET_KEY",
        "USER_AUTH_JWT_SECRET",
    )
    findings: list[ConfigFinding] = []
    for field in required:
        value = values.get(field, "").strip()
        if not value:
            findings.append(ConfigFinding(field, "MISSING_VALUE"))
        elif value in _PLACEHOLDERS or value.startswith("your_"):
            findings.append(ConfigFinding(field, "PLACEHOLDER_VALUE"))
    auto_create = values.get("DB_AUTO_CREATE", "").strip().lower()
    if auto_create and auto_create not in {"true", "false"}:
        findings.append(ConfigFinding("DB_AUTO_CREATE", "INVALID_BOOLEAN"))
    expected_database = _RUNTIME_DATABASE_NAMES[normalized]
    database_name = values.get("DB_NAME", "").strip()
    if database_name and database_name != expected_database:
        findings.append(
            ConfigFinding("DB_NAME", "RUNTIME_DATABASE_NAME_MISMATCH")
        )
    registry_fields = (
        "RUNTIME_TRUSTED_CLIENTS_JSON",
        "RUNTIME_BUSINESS_CONNECTORS_JSON",
        "RUNTIME_CALLBACK_TARGETS_JSON",
        "MEMORY_TOOL_TRUSTED_RUNTIMES_JSON",
    )
    for field in (
        *registry_fields,
        "MODEL_ROUTES_JSON",
        "MEMOIR_MODEL_NODE_ROUTES_JSON",
    ):
        raw = values.get(field)
        if raw:
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                findings.append(ConfigFinding(field, "INVALID_JSON"))
    if normalized == "production":
        for field in registry_fields:
            raw = values.get(field)
            if not raw:
                continue
            try:
                registry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(registry, dict) or not registry:
                findings.append(ConfigFinding(field, "EMPTY_REGISTRY"))
        for field in (
            "RUNTIME_AUDIT_SINK_CONFIGURED",
            "DEBUG",
            "DB_ECHO",
        ):
            expected = "true" if field == "RUNTIME_AUDIT_SINK_CONFIGURED" else "false"
            if values.get(field, "").strip().lower() != expected:
                findings.append(ConfigFinding(field, "UNSAFE_PRODUCTION_VALUE"))
        cors_origins = {
            origin.strip()
            for origin in values.get("BACKEND_CORS_ORIGINS", "").split(",")
            if origin.strip()
        }
        if not cors_origins or "*" in cors_origins:
            findings.append(
                ConfigFinding("BACKEND_CORS_ORIGINS", "UNSAFE_PRODUCTION_VALUE")
            )
        for field in ("MEMORY_RUNTIME_SECRET", "USER_AUTH_JWT_SECRET"):
            secret = values.get(field, "")
            if secret and secret not in _PLACEHOLDERS and len(secret) < 24:
                findings.append(ConfigFinding(field, "SECRET_TOO_SHORT"))
    local_path = project_root / f".env.{normalized}.local"
    if local_path.exists() and stat.S_IMODE(local_path.stat().st_mode) & 0o077:
        findings.append(ConfigFinding(local_path.name, "INSECURE_FILE_MODE"))
    return tuple(findings)


def build_service_commands() -> tuple[tuple[str, ...], ...]:
    """返回由前台 supervisor 共同回收的进程命令。"""
    return (
        (sys.executable, "run_app.py"),
        (
            sys.executable,
            "-m",
            "app.scripts.agent_runtime_cli",
            "_launcher-loop",
        ),
        (
            sys.executable,
            "-m",
            "app.worker",
            "--worker-id",
            "agent-runtime-worker",
        ),
        (
            sys.executable,
            "-m",
            "app.reconciler",
            "--interval-seconds",
            "300",
        ),
    )


def run_launcher_loop(
    project_root: Path,
    *,
    environment: Mapping[str, str],
    runner: CommandRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
    interval_seconds: float = 5,
    max_cycles: int | None = None,
) -> None:
    """周期消费业务侧 Runtime 启动 outbox；每轮使用原幂等键。"""
    if interval_seconds <= 0:
        raise ValueError("LAUNCHER_INTERVAL_INVALID")
    command_runner = runner or _default_runner
    command = (sys.executable, "-m", "app.memory_runtime_launcher")
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        command_runner(
            command,
            cwd=project_root,
            env=dict(environment),
            check=True,
        )
        cycles += 1
        if max_cycles is None or cycles < max_cycles:
            sleep(interval_seconds)


def _default_runner(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    check: bool,
) -> CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, check=check, text=True)


def run_real_harness(
    project_root: Path,
    *,
    runner: CommandRunner = _default_runner,
    process_env: Mapping[str, str] | None = None,
    password_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
) -> None:
    """运行真实 PostgreSQL/Redis/Worker harness，并在任何结果后删除 volume。"""
    environment = dict(process_env if process_env is not None else os.environ)
    environment.update(
        {
            "POSTGRES_HARNESS_PASSWORD": password_factory(),
            "PGPASSWORD": "",
            "AGENT_RUNTIME_TEST_POSTGRES_DSN": (
                "postgresql+psycopg://test_runtime@127.0.0.1:54329/test_runtime"
            ),
            "AGENT_RUNTIME_TEST_REDIS_URL": "redis://127.0.0.1:56379/15",
        }
    )
    environment["PGPASSWORD"] = environment["POSTGRES_HARNESS_PASSWORD"]
    postgres_compose = "docker-compose.postgres-harness.yml"
    redis_compose = "docker-compose.redis-harness.yml"
    try:
        runner(
            ("docker", "compose", "-f", postgres_compose, "up", "-d", "--wait"),
            cwd=project_root,
            env=environment,
            check=True,
        )
        runner(
            ("docker", "compose", "-f", redis_compose, "up", "-d", "--wait"),
            cwd=project_root,
            env=environment,
            check=True,
        )
        runner(
            (
                "poetry",
                "run",
                "pytest",
                "-q",
                "tests/test_runtime_postgres_harness.py",
                "tests/test_runtime_process_harness.py",
                "tests/test_runtime_redis_harness.py",
            ),
            cwd=project_root,
            env=environment,
            check=True,
        )
    finally:
        cleanup_errors: list[BaseException] = []
        for compose_file in (postgres_compose, redis_compose):
            try:
                runner(
                    ("docker", "compose", "-f", compose_file, "down", "-v"),
                    cwd=project_root,
                    env=environment,
                    check=True,
                )
            except BaseException as exc:  # pragma: no cover - 仅在 Docker 清理自身失败时上报。
                cleanup_errors.append(exc)
        if cleanup_errors and sys.exc_info()[0] is None:
            raise RuntimeError("HARNESS_CLEANUP_FAILED") from cleanup_errors[0]


def _print_findings(findings: Sequence[ConfigFinding]) -> None:
    for finding in findings:
        print(f"[ERROR] {finding.field}: {finding.code}")


def _doctor(environment: str) -> dict[str, str]:
    findings = inspect_configuration(PROJECT_ROOT, environment)
    if findings:
        _print_findings(findings)
        raise SystemExit(2)
    normalized = _normalize_local_environment(environment)
    print(f"[OK] configuration environment={normalized}")
    return _load_environment(PROJECT_ROOT, normalized, os.environ)


def _prepare(environment: str) -> dict[str, str]:
    command_environment = _doctor(environment)
    database_server: MySQLDatabaseServer | None = None
    try:
        database_server = MySQLDatabaseServer.from_environment(command_environment)
        database_status = ensure_runtime_database(
            DatabaseBootstrapConfig(
                environment=environment,
                database_name=command_environment["DB_NAME"],
                auto_create=(
                    command_environment["DB_AUTO_CREATE"].strip().lower() == "true"
                ),
            ),
            database_server,
        )
    except DatabaseBootstrapError as exc:
        print(f"[ERROR] database: {exc}")
        raise SystemExit(2) from None
    finally:
        if database_server is not None:
            database_server.close()
    print(
        f"[OK] database environment={_normalize_local_environment(environment)} "
        f"status={database_status}"
    )
    try:
        subprocess.run(
            ("poetry", "run", "alembic", "upgrade", "head"),
            cwd=PROJECT_ROOT,
            env=command_environment,
            check=True,
        )
        subprocess.run(
            ("poetry", "run", "alembic", "heads"),
            cwd=PROJECT_ROOT,
            env=command_environment,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print("[ERROR] database: RUNTIME_DATABASE_MIGRATION_FAILED")
        raise SystemExit(exc.returncode or 1) from None
    return command_environment


def _register(
    environment: str, agent_id: str, version: str, *, dry_run: bool
) -> None:
    """注册部署目录内的 AgentPackage 进指定环境的 agent_definitions 表。

    流程与 prepare 同款：先 doctor 校验配置，再以带 ENVIRONMENT 的合并环境
    启动子进程复用 app.scripts.register_agent_package（与 alembic 迁移同一
    子进程模式，保证子进程按目标环境加载 .env 与 settings，不重复实现）。
    """

    command_environment = _doctor(environment)
    command = (
        "poetry",
        "run",
        "python",
        "-m",
        "app.scripts.register_agent_package",
        "--agent-id",
        agent_id,
        "--version",
        version,
    )
    if dry_run:
        command = (*command, "--dry-run")
    try:
        subprocess.run(command, cwd=PROJECT_ROOT, env=command_environment, check=True)
    except subprocess.CalledProcessError as exc:
        print("[ERROR] agent package registration failed")
        raise SystemExit(exc.returncode or 1) from None
    print(
        f"[OK] agent package register agent_id={agent_id} version={version} "
        f"environment={environment}"
    )


@dataclass(frozen=True)
class _ProcessRecord:
    """本机进程的最小身份：PID、命令、cwd、启动时间和运行环境。"""

    pid: int
    command: str
    cwd: Path | None
    start_time: str | None = None
    environment: str | None = None


ProcessReader = Callable[[int], _ProcessRecord | None]
ProcessLister = Callable[[], Sequence[_ProcessRecord]]
ProcessStopper = Callable[[_ProcessRecord], bool]

_RUNTIME_LOCK_WAIT_SECONDS = 30.0
_RUNTIME_PROCESS_STOP_SECONDS = 10.0
_RUNTIME_PROCESS_POLL_SECONDS = 0.1


def _runtime_control_paths(project_root: Path) -> tuple[Path, Path]:
    """为工程生成稳定但不落入 Git 的锁文件和状态文件路径。"""
    root = str(project_root.resolve())
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:20]
    base = Path(tempfile.gettempdir()) / f"agent-runtime-{digest}"
    return base.with_suffix(".lock"), base.with_suffix(".state")


def _process_cwd(pid: int) -> Path | None:
    """读取进程 cwd；无法确认时返回 None，调用方必须按不安全处理。"""
    proc_cwd = Path("/proc") / str(pid) / "cwd"
    try:
        if proc_cwd.exists() or proc_cwd.is_symlink():
            return proc_cwd.resolve()
    except OSError:
        pass
    try:
        result = subprocess.run(
            ("lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n") and len(line) > 1:
            return Path(line[1:]).resolve()
    return None


def _process_environment(pid: int) -> str | None:
    """只读取运行环境名，不保存或回显进程环境中的其他字段。"""
    try:
        result = subprocess.run(
            ("ps", "eww", "-p", str(pid), "-o", "command="),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    match = re.search(
        r"(?:^|\s)ENVIRONMENT=(development|test|production)(?:\s|$)",
        result.stdout,
    )
    return match.group(1) if match else None


def _parse_process_line(line: str) -> _ProcessRecord | None:
    fields = line.strip().split(maxsplit=6)
    if len(fields) < 7:
        return None
    try:
        pid = int(fields[0])
    except ValueError:
        return None
    return _ProcessRecord(
        pid=pid,
        start_time=" ".join(fields[1:6]),
        command=fields[6],
        cwd=None,
    )


def _list_process_records() -> tuple[_ProcessRecord, ...]:
    """列出进程命令；这里只获取候选，cwd 延迟到命中后再读取。"""
    try:
        result = subprocess.run(
            ("ps", "-ww", "-axo", "pid=,lstart=,command="),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ()
    if result.returncode not in (0, 1):
        return ()
    return tuple(
        record
        for line in result.stdout.splitlines()
        if (record := _parse_process_line(line)) is not None
    )


def _read_process_record(pid: int) -> _ProcessRecord | None:
    """读取一个进程的完整身份，失败时绝不猜测其 cwd。"""
    try:
        result = subprocess.run(
            ("ps", "-ww", "-p", str(pid), "-o", "pid=,lstart=,command="),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode not in (0, 1):
        return None
    records = tuple(
        record
        for line in result.stdout.splitlines()
        if (record := _parse_process_line(line)) is not None
        and record.pid == pid
    )
    if not records:
        return None
    return _record_with_cwd(records[0])


def _record_with_cwd(record: _ProcessRecord) -> _ProcessRecord:
    cwd = record.cwd if record.cwd is not None else _process_cwd(record.pid)
    environment = record.environment
    if environment is None and _is_worker_command(record.command):
        environment = _process_environment(record.pid)
    return _ProcessRecord(
        pid=record.pid,
        command=record.command,
        cwd=cwd,
        start_time=record.start_time,
        environment=environment,
    )


def _same_process(expected: _ProcessRecord, actual: _ProcessRecord | None) -> bool:
    """只有 PID、命令、cwd 和可用启动时间都一致才认为是同一进程。"""
    if actual is None or expected.pid != actual.pid:
        return False
    if expected.command != actual.command:
        return False
    if expected.cwd is None or actual.cwd is None or expected.cwd != actual.cwd:
        return False
    if not expected.start_time or not actual.start_time:
        return False
    if expected.start_time != actual.start_time:
        return False
    return not (
        expected.environment
        and actual.environment
        and expected.environment != actual.environment
    )


def _is_worker_command(command: str) -> bool:
    try:
        tokens = tuple(shlex.split(command))
    except ValueError:
        return False
    target = ("-m", "app.worker", "--worker-id", "agent-runtime-worker")
    return any(tokens[index : index + len(target)] == target for index in range(len(tokens)))


def _is_supervisor_command(command: str) -> bool:
    try:
        tokens = tuple(shlex.split(command))
    except ValueError:
        return False
    target = ("-m", "app.scripts.agent_runtime_cli", "start")
    return any(tokens[index : index + len(target)] == target for index in range(len(tokens)))


def _terminate_validated_process(
    record: _ProcessRecord,
    *,
    process_reader: ProcessReader = _read_process_record,
    process_killer: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: float = _RUNTIME_PROCESS_STOP_SECONDS,
) -> bool:
    """对已校验身份的进程执行 TERM，超时后才执行 KILL。"""
    current = process_reader(record.pid)
    if current is None:
        return True
    if not _same_process(record, current):
        return False
    try:
        process_killer(record.pid, signal.SIGTERM)
    except PermissionError as err:
        raise RuntimeError("RUNTIME_PROCESS_STOP_FORBIDDEN") from err
    except ProcessLookupError:
        return True
    deadline = monotonic() + timeout_seconds
    while True:
        current = process_reader(record.pid)
        if current is None:
            return True
        if not _same_process(record, current):
            return False
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(_RUNTIME_PROCESS_POLL_SECONDS, remaining))
    current = process_reader(record.pid)
    if current is None:
        return True
    if not _same_process(record, current):
        return False
    try:
        process_killer(record.pid, signal.SIGKILL)
    except PermissionError as err:
        raise RuntimeError("RUNTIME_PROCESS_STOP_FORBIDDEN") from err
    except ProcessLookupError:
        return True
    kill_deadline = monotonic() + timeout_seconds
    while monotonic() < kill_deadline:
        current = process_reader(record.pid)
        if current is None:
            return True
        if not _same_process(record, current):
            return False
        sleep(_RUNTIME_PROCESS_POLL_SECONDS)
    raise RuntimeError("RUNTIME_PROCESS_STOP_TIMEOUT")


def _read_runtime_state(state_path: Path) -> _ProcessRecord | None:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        pid = payload["pid"]
        command = payload["command"]
        cwd = payload["cwd"]
        start_time = payload.get("start_time")
        environment = payload.get("environment")
        if not isinstance(pid, int) or pid <= 0:
            return None
        if not isinstance(command, str) or not command:
            return None
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return None
        if start_time is not None and not isinstance(start_time, str):
            return None
        if environment is not None and not isinstance(environment, str):
            return None
    except (KeyError, TypeError):
        return None
    return _ProcessRecord(pid, command, Path(cwd), start_time, environment)


def _write_runtime_state(state_path: Path, record: _ProcessRecord) -> None:
    """原子写入当前 supervisor 身份，避免并发 start 读到半截 JSON。"""
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=state_path.parent,
            prefix=f".{state_path.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(
                {
                    "pid": record.pid,
                    "command": record.command,
                    "cwd": str(record.cwd),
                    "start_time": record.start_time,
                    "environment": record.environment,
                },
                temporary,
                ensure_ascii=True,
                separators=(",", ":"),
            )
        temporary_path.chmod(0o600)
        os.replace(temporary_path, state_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _clear_runtime_state(state_path: Path, owner_pid: int) -> None:
    state = _read_runtime_state(state_path)
    if state is None or state.pid == owner_pid:
        try:
            state_path.unlink()
        except FileNotFoundError:
            pass


def _cleanup_previous_runtime_processes(
    project_root: Path,
    *,
    environment: str,
    state_path: Path | None = None,
    process_reader: ProcessReader = _read_process_record,
    process_lister: ProcessLister = _list_process_records,
    process_stopper: ProcessStopper = _terminate_validated_process,
) -> None:
    """回收旧 supervisor 和无状态遗留 Worker，不匹配身份的进程一律跳过。"""
    root = project_root.resolve()
    if state_path is None:
        _, state_path = _runtime_control_paths(root)
    previous = _read_runtime_state(state_path)
    protected_pids: set[int] = set()
    if previous is not None:
        # state 只允许声明当前工程的 supervisor；其他内容按不可信 stale state 处理。
        protected_pids.add(previous.pid)
        current = process_reader(previous.pid)
        if (
            previous.cwd != root
            or not _is_supervisor_command(previous.command)
            or current is None
        ):
            _clear_runtime_state(state_path, previous.pid)
        elif _same_process(previous, current):
            if not process_stopper(current):
                raise RuntimeError("RUNTIME_PROCESS_IDENTITY_CHANGED")
            _clear_runtime_state(state_path, previous.pid)
        else:
            # PID 已被其他命令复用，后续候选扫描也不能再触碰这个 PID。
            _clear_runtime_state(state_path, previous.pid)
    for listed in process_lister():
        if (
            listed.pid == os.getpid()
            or listed.pid in protected_pids
            or not _is_worker_command(listed.command)
        ):
            continue
        candidate = _record_with_cwd(listed)
        if candidate.cwd == root and candidate.environment == environment:
            if not process_stopper(candidate):
                raise RuntimeError("RUNTIME_PROCESS_IDENTITY_CHANGED")


def _acquire_runtime_lock(
    project_root: Path,
    *,
    lock_path: Path | None = None,
    state_path: Path | None = None,
    process_reader: ProcessReader = _read_process_record,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: float = _RUNTIME_LOCK_WAIT_SECONDS,
):
    """用工程级锁串行化 start；新 start 会请求旧 supervisor 退出后接管。"""
    default_lock, default_state = _runtime_control_paths(project_root)
    lock_path = lock_path or default_lock
    state_path = state_path or default_state
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    lock_path.chmod(0o600)
    deadline = monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except BlockingIOError:
                previous = _read_runtime_state(state_path)
                if previous is not None:
                    current = process_reader(previous.pid)
                    if (
                        current is not None
                        and current.cwd == project_root.resolve()
                        and _is_supervisor_command(current.command)
                        and _same_process(previous, current)
                    ):
                        _terminate_validated_process(
                            current,
                            process_reader=process_reader,
                            sleep=sleep,
                            monotonic=monotonic,
                        )
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise RuntimeError("RUNTIME_START_LOCK_TIMEOUT") from None
                sleep(min(_RUNTIME_PROCESS_POLL_SECONDS, remaining))
    except BaseException:
        handle.close()
        raise


def _release_runtime_lock(handle: object) -> None:
    if not hasattr(handle, "fileno"):
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _wait_until_ready(process: subprocess.Popen[bytes], base_url: str) -> None:
    deadline = time.monotonic() + 30
    urls = (
        f"{base_url.rstrip('/')}/healthz",
        f"{base_url.rstrip('/')}/api/v1/runtime/health/ready",
    )
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("API_EXITED_BEFORE_READY")
        try:
            if all(urllib.request.urlopen(url, timeout=1).status == 200 for url in urls):
                return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(0.5)
    raise RuntimeError("API_READINESS_TIMEOUT")


def _terminate_processes(processes: Sequence[subprocess.Popen[bytes]]) -> None:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 10
    for process in reversed(processes):
        if process.poll() is None:
            try:
                process.wait(timeout=max(deadline - time.monotonic(), 0.1))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def start_service_process(
    command: tuple[str, ...],
    *,
    project_root: Path,
    environment: Mapping[str, str],
) -> subprocess.Popen[bytes]:
    """让子服务脱离终端信号组，由 supervisor 统一优雅回收。"""
    return subprocess.Popen(
        command,
        cwd=project_root,
        env=dict(environment),
        start_new_session=True,
    )


def supervised_service_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """前台 supervisor 已托管进程，禁用 Uvicorn 嵌套 reloader 避免泄漏子进程资源。"""
    result = dict(environment)
    result["RELOAD"] = "false"
    return result


def _start(environment: str) -> None:
    normalized = _normalize_local_environment(environment)
    command_environment = supervised_service_environment(_prepare(normalized))
    lock_handle = _acquire_runtime_lock(PROJECT_ROOT)
    _, state_path = _runtime_control_paths(PROJECT_ROOT)
    processes: list[subprocess.Popen[bytes]] = []
    stopping = False

    def request_stop(_signal_number: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    previous_sigint = signal.signal(signal.SIGINT, request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    try:
        _cleanup_previous_runtime_processes(
            PROJECT_ROOT,
            environment=normalized,
            state_path=state_path,
        )
        supervisor = _read_process_record(os.getpid())
        if supervisor is None or supervisor.cwd != PROJECT_ROOT.resolve():
            raise RuntimeError("RUNTIME_PROCESS_IDENTITY_UNAVAILABLE")
        _write_runtime_state(
            state_path,
            _ProcessRecord(
                pid=supervisor.pid,
                command=supervisor.command,
                cwd=supervisor.cwd,
                start_time=supervisor.start_time,
                environment=normalized,
            ),
        )
        api_command, launcher_command, worker_command, reconciler_command = build_service_commands()
        api = start_service_process(
            api_command,
            project_root=PROJECT_ROOT,
            environment=command_environment,
        )
        processes.append(api)
        _wait_until_ready(api, command_environment.get("MEMORY_RUNTIME_BASE_URL", "http://127.0.0.1:8010"))
        processes.append(
            start_service_process(
                launcher_command,
                project_root=PROJECT_ROOT,
                environment=command_environment,
            )
        )
        processes.append(
            start_service_process(
                worker_command,
                project_root=PROJECT_ROOT,
                environment=command_environment,
            )
        )
        processes.append(
            start_service_process(
                reconciler_command,
                project_root=PROJECT_ROOT,
                environment=command_environment,
            )
        )
        print("[OK] AgentRuntime API/Worker/Reconciler started; Ctrl-C stops all processes")
        while not stopping:
            exited = next((process for process in processes if process.poll() is not None), None)
            if exited is not None:
                raise RuntimeError("RUNTIME_PROCESS_EXITED")
            time.sleep(0.5)
    finally:
        try:
            _terminate_processes(processes)
        finally:
            try:
                _clear_runtime_state(state_path, os.getpid())
            finally:
                try:
                    _release_runtime_lock(lock_handle)
                finally:
                    signal.signal(signal.SIGINT, previous_sigint)
                    signal.signal(signal.SIGTERM, previous_sigterm)


def _prompt_setup(environment: str) -> LocalSetup:
    normalized = _normalize_local_environment(environment)
    default_database = _RUNTIME_DATABASE_NAMES[normalized]

    def prompt(label: str, default: str) -> str:
        value = input(f"{label} [{default}]: ").strip()
        return value or default

    database_user = prompt("DB user", "root")
    database_password = getpass.getpass("DB password (hidden): ")
    if not database_password:
        raise SystemExit("DB password is required")
    return LocalSetup(
        database_driver="mysql+mysqlconnector",
        database_user=database_user,
        database_password=database_password,
        database_host=prompt("DB host", "127.0.0.1"),
        database_port=int(prompt("DB port", "3306")),
        database_name=prompt("DB name", default_database),
        redis_url=prompt("Redis URL", "redis://127.0.0.1:6379/15"),
        service_base_url=prompt("Service base URL", "http://127.0.0.1:8010"),
        business_base_url=prompt(
            "Business backend base URL", "http://127.0.0.1:8008"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Configure, start and verify AgentRuntime without exposing secrets"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    configure = subparsers.add_parser("configure", help="create a private local env file")
    configure.add_argument("environment", choices=("development", "test"))
    configure.add_argument("--force", action="store_true", help="replace the local env file")
    command_help = {
        "doctor": "validate configuration without printing values",
        "prepare": "validate configuration and apply Alembic migrations",
        "start": "prepare and supervise API, launcher, Worker and Reconciler",
    }
    for command, help_text in command_help.items():
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument(
            "environment", choices=("development", "test", "production")
        )
    register = subparsers.add_parser(
        "register", help="register a deployed agent package into agent_definitions"
    )
    register.add_argument(
        "environment", choices=("development", "test", "production")
    )
    register.add_argument("--agent-id", default="memoir_agent", help="default memoir_agent")
    register.add_argument(
        "--version",
        required=True,
        help="package version to register (e.g. 1.0.2); must be explicit",
    )
    register.add_argument(
        "--dry-run", action="store_true", help="load and print without writing"
    )
    subparsers.add_parser("verify", help="run isolated PostgreSQL/Redis/Worker harness")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(argv) if argv is not None else tuple(sys.argv[1:])
    if arguments == ("_launcher-loop",):
        run_launcher_loop(PROJECT_ROOT, environment=os.environ)
        return 0
    args = build_parser().parse_args(arguments)
    if args.command == "configure":
        path = create_local_config(
            PROJECT_ROOT,
            args.environment,
            _prompt_setup(args.environment),
            overwrite=args.force,
        )
        print(f"[OK] private configuration created: {path.name} mode=0600")
    elif args.command == "doctor":
        _doctor(args.environment)
    elif args.command == "prepare":
        _prepare(args.environment)
    elif args.command == "register":
        _register(
            args.environment, args.agent_id, args.version, dry_run=args.dry_run
        )
    elif args.command == "start":
        _start(args.environment)
    elif args.command == "verify":
        run_real_harness(PROJECT_ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
