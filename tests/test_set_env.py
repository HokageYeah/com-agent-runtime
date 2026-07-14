from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from app.scripts import set_env


def test_build_command_environment_normalizes_environment_alias() -> None:
    env = set_env.build_command_environment("dev")
    assert env["ENVIRONMENT"] == "development"
    assert env["ENV"] == "development"


def test_bootstrap_database_falls_back_to_create_all_when_alembic_fails() -> None:
    env = {"ENVIRONMENT": "development"}

    with patch("app.scripts.set_env.run_command") as mock_run_command:
        mock_run_command.side_effect = [
            None,
            subprocess.CalledProcessError(
                returncode=1,
                cmd=["alembic", "upgrade", "head"],
            ),
            None,
            None,
        ]

        set_env.bootstrap_database(env)

    assert mock_run_command.call_count == 4
    assert mock_run_command.call_args_list[0].args[0] == [
        set_env.sys.executable,
        "-m",
        "app.scripts.create_database",
    ]
    assert mock_run_command.call_args_list[1].args[0] == [
        "alembic",
        "upgrade",
        "head",
    ]
    assert mock_run_command.call_args_list[2].args[0] == [
        set_env.sys.executable,
        "-m",
        "app.scripts.init_database",
    ]
    assert mock_run_command.call_args_list[3].args[0] == [
        "alembic",
        "stamp",
        "head",
    ]


def test_invoke_database_command_routes_upgrade_arguments() -> None:
    with patch("app.scripts.set_env.run_command") as mock_run_command:
        set_env.invoke_database_command("test", "upgrade", ["head"])

    mock_run_command.assert_called_once()
    assert mock_run_command.call_args.args[0] == ["alembic", "upgrade", "head"]
    assert mock_run_command.call_args.args[1]["ENVIRONMENT"] == "test"


def test_invoke_database_command_requires_migration_name() -> None:
    with pytest.raises(SystemExit) as exc_info:
        set_env.invoke_database_command("development", "create-migration", [])

    assert str(exc_info.value) == "错误: 请提供迁移名称"


def test_invoke_database_command_rejects_unknown_command() -> None:
    with pytest.raises(SystemExit) as exc_info:
        set_env.invoke_database_command("development", "unknown-command", [])

    assert str(exc_info.value) == "错误: 不支持的命令 unknown-command"
