from __future__ import annotations

import stat
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess

import pytest

from app.scripts.agent_runtime_cli import (
    LocalSetup,
    build_parser,
    build_service_commands,
    create_local_config,
    inspect_configuration,
    run_launcher_loop,
    run_real_harness,
)


def _setup() -> LocalSetup:
    return LocalSetup(
        database_driver="mysql+mysqlconnector",
        database_user="runtime_test",
        database_password="database-secret",
        database_host="127.0.0.1",
        database_port=3306,
        database_name="runtime_test",
        redis_url="redis://127.0.0.1:6379/15",
        service_base_url="http://127.0.0.1:8002",
    )


def test_create_local_config_writes_private_file_without_overwriting(tmp_path: Path) -> None:
    secret_values = iter(("client-secret", "tool-secret", "jwt-secret"))

    path = create_local_config(
        tmp_path,
        "test",
        _setup(),
        secret_factory=lambda: next(secret_values),
        fernet_key_factory=lambda: "fernet-key",
    )

    assert path == tmp_path / ".env.test.local"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    content = path.read_text(encoding="utf-8")
    assert "# 应用运行环境：决定加载哪一套基础配置。" in content
    assert "# 数据库连接：应用账号应只授予当前业务库的最小权限。" in content
    assert "# Runtime 入站身份：与 MEMORY_RUNTIME_SECRET 使用同一 client secret。" in content
    assert "# Runtime 出站工具与 callback：三个注册表必须使用同一 tool secret。" in content
    assert "# 模型路由为空时关闭真实模型增强，使用确定性模板降级。" in content
    assert "# [必填] MySQL 应用账号：由配置时的 DB user 生成" in content
    assert "# [必填/自动生成] Snapshot Fernet 加密密钥" in content
    assert "# [可选] 模型部署路由" in content
    lines = content.splitlines()
    assignment_indexes = [
        index for index, line in enumerate(lines) if line and not line.startswith("#")
    ]
    assert assignment_indexes
    assert all(lines[index - 1].startswith("# ") for index in assignment_indexes)
    assert "HOST=127.0.0.1" in content
    assert "PORT=8002" in content
    assert "DB_USER=runtime_test" in content
    assert "RUNTIME_REDIS_URL=redis://127.0.0.1:6379/15" in content
    assert "MODEL_ROUTES_JSON=[]" in content
    assert "MEMOIR_MODEL_NODE_ROUTES_JSON={}" in content
    assert "USER_AUTH_JWT_SECRET=jwt-secret" in content
    assert inspect_configuration(tmp_path, "test", process_env={}) == ()

    with pytest.raises(FileExistsError, match="CONFIG_FILE_EXISTS"):
        create_local_config(tmp_path, "test", _setup())


def test_inspect_configuration_reports_field_names_without_secret_values(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env.test").write_text(
        "\n".join(
            (
                "DB_USER=your_mysql_user",
                "DB_PASSWORD=do-not-print-this",
                "DB_HOST=127.0.0.1",
                "DB_PORT=3306",
                "DB_NAME=runtime_test",
                "RUNTIME_AUDIT_SINK_CONFIGURED=true",
            )
        ),
        encoding="utf-8",
    )

    findings = inspect_configuration(tmp_path, "test", process_env={})
    rendered = "\n".join(f"{item.field}:{item.code}" for item in findings)

    assert "DB_USER:PLACEHOLDER_VALUE" in rendered
    assert "RUNTIME_TRUSTED_CLIENTS_JSON:MISSING_VALUE" in rendered
    assert "do-not-print-this" not in rendered


def test_production_can_be_checked_but_cannot_write_a_local_secret_file(
    tmp_path: Path,
) -> None:
    production_environment = {
        "DB_DRIVER": "mysql+mysqlconnector",
        "DB_USER": "runtime_prod",
        "DB_PASSWORD": "database-secret",
        "DB_HOST": "mysql.internal",
        "DB_PORT": "3306",
        "DB_NAME": "runtime_prod",
        "RUNTIME_REDIS_URL": "redis://redis.internal:6379/15",
        "RUNTIME_TRUSTED_CLIENTS_JSON": '{"client":{"keys":{"v1":"secret"}}}',
        "RUNTIME_BUSINESS_CONNECTORS_JSON": '{"connector":{"enabled":true}}',
        "RUNTIME_CALLBACK_TARGETS_JSON": '{"callback":{"enabled":true}}',
        "MEMORY_TOOL_TRUSTED_RUNTIMES_JSON": '{"runtime":{"keys":{"v1":"secret"}}}',
        "MEMORY_RUNTIME_SECRET": "runtime-secret-with-safe-length",
        "MEMORY_RUNTIME_BASE_URL": "https://runtime.example.com",
        "MEMORY_RUNTIME_CLIENT_ID": "client",
        "MEMORY_RUNTIME_KEY_ID": "v1",
        "MEMORY_SNAPSHOT_FERNET_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        "USER_AUTH_JWT_SECRET": "jwt-secret-with-safe-length",
        "MODEL_ROUTES_JSON": "[]",
        "MEMOIR_MODEL_NODE_ROUTES_JSON": "{}",
        "RUNTIME_AUDIT_SINK_CONFIGURED": "true",
        "DEBUG": "false",
        "DB_ECHO": "false",
        "BACKEND_CORS_ORIGINS": "https://frontend.example.com",
    }

    assert inspect_configuration(
        tmp_path,
        "production",
        process_env=production_environment,
    ) == ()
    with pytest.raises(ValueError, match="LOCAL_CONFIG_FOR_PRODUCTION_FORBIDDEN"):
        create_local_config(tmp_path, "production", _setup())


def test_production_doctor_rejects_unsafe_runtime_flags(tmp_path: Path) -> None:
    unsafe_environment = {
        "DB_DRIVER": "mysql+mysqlconnector",
        "DB_USER": "runtime_prod",
        "DB_PASSWORD": "database-secret",
        "DB_HOST": "mysql.internal",
        "DB_PORT": "3306",
        "DB_NAME": "runtime_prod",
        "RUNTIME_REDIS_URL": "redis://redis.internal:6379/15",
        "RUNTIME_TRUSTED_CLIENTS_JSON": "{}",
        "RUNTIME_BUSINESS_CONNECTORS_JSON": "{}",
        "RUNTIME_CALLBACK_TARGETS_JSON": "{}",
        "MEMORY_TOOL_TRUSTED_RUNTIMES_JSON": "{}",
        "MEMORY_RUNTIME_SECRET": "development-secret",
        "MEMORY_RUNTIME_BASE_URL": "https://runtime.example.com",
        "MEMORY_RUNTIME_CLIENT_ID": "client",
        "MEMORY_RUNTIME_KEY_ID": "v1",
        "MEMORY_SNAPSHOT_FERNET_KEY": "UIdCWOsJY0GWrMpXlM444_JDKJC-zFwylDAJCymPvPg=",
        "USER_AUTH_JWT_SECRET": "short",
        "RUNTIME_AUDIT_SINK_CONFIGURED": "false",
        "DEBUG": "true",
        "DB_ECHO": "true",
        "BACKEND_CORS_ORIGINS": "*",
    }

    rendered = "\n".join(
        f"{item.field}:{item.code}"
        for item in inspect_configuration(
            tmp_path,
            "production",
            process_env=unsafe_environment,
        )
    )

    assert "RUNTIME_TRUSTED_CLIENTS_JSON:EMPTY_REGISTRY" in rendered
    assert "RUNTIME_AUDIT_SINK_CONFIGURED:UNSAFE_PRODUCTION_VALUE" in rendered
    assert "DEBUG:UNSAFE_PRODUCTION_VALUE" in rendered
    assert "DB_ECHO:UNSAFE_PRODUCTION_VALUE" in rendered
    assert "BACKEND_CORS_ORIGINS:UNSAFE_PRODUCTION_VALUE" in rendered
    assert "MEMORY_SNAPSHOT_FERNET_KEY:PLACEHOLDER_VALUE" in rendered
    assert "USER_AUTH_JWT_SECRET:SECRET_TOO_SHORT" in rendered


def test_build_service_commands_starts_launcher_api_worker_and_reconciler() -> None:
    assert build_service_commands() == (
        ("poetry", "run", "python", "run_app.py"),
        (
            "poetry",
            "run",
            "python",
            "-m",
            "app.scripts.agent_runtime_cli",
            "_launcher-loop",
        ),
        (
            "poetry",
            "run",
            "python",
            "-m",
            "app.worker",
            "--worker-id",
            "agent-runtime-worker",
        ),
        (
            "poetry",
            "run",
            "python",
            "-m",
            "app.reconciler",
            "--interval-seconds",
            "300",
        ),
    )


def test_internal_launcher_loop_is_not_listed_as_a_public_command() -> None:
    assert "launcher-loop" not in build_parser().format_help()


def test_launcher_loop_consumes_new_outbox_events_on_each_cycle(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []
    sleeps: list[float] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> CompletedProcess[str]:
        del cwd, env, check
        commands.append(command)
        return CompletedProcess(command, 0)

    run_launcher_loop(
        tmp_path,
        environment={"ENVIRONMENT": "test"},
        runner=runner,
        sleep=sleeps.append,
        interval_seconds=5,
        max_cycles=2,
    )

    assert commands == [
        ("poetry", "run", "python", "-m", "app.memory_runtime_launcher"),
        ("poetry", "run", "python", "-m", "app.memory_runtime_launcher"),
    ]
    assert sleeps == [5]


def test_real_harness_always_removes_postgres_and_redis_before_returning(
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> CompletedProcess[str]:
        del cwd, env, check
        commands.append(command)
        if command[:4] == ("poetry", "run", "pytest", "-q"):
            raise CalledProcessError(1, command)
        return CompletedProcess(command, 0)

    with pytest.raises(CalledProcessError):
        run_real_harness(
            tmp_path,
            runner=runner,
            process_env={},
            password_factory=lambda: "temporary-password",
        )

    assert commands[-2:] == [
        (
            "docker",
            "compose",
            "-f",
            "docker-compose.postgres-harness.yml",
            "down",
            "-v",
        ),
        (
            "docker",
            "compose",
            "-f",
            "docker-compose.redis-harness.yml",
            "down",
            "-v",
        ),
    ]
    assert all("temporary-password" not in argument for command in commands for argument in command)
