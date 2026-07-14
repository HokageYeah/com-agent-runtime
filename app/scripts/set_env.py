from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Sequence

from app.core.config import env_file_for_environment, normalize_environment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def build_command_environment(env_type: str) -> dict[str, str]:
    """构造命令运行时环境变量。

    这里不再改写仓库内的 .env 文件，而是通过子进程环境变量显式传递当前环境。
    这样更符合主流工程实践，也更方便团队协作和 LLM 接入调试。
    """
    normalized = normalize_environment(env_type)
    command_env = os.environ.copy()
    command_env["ENVIRONMENT"] = normalized
    command_env["ENV"] = normalized
    return command_env


def print_environment_summary(env_type: str) -> None:
    """打印当前命令实际使用的环境信息，方便调试。"""
    normalized = normalize_environment(env_type)
    print("\n当前环境信息:")
    print("----------------------------------------")
    print(f"环境类型: {normalized}")
    print(f"配置文件: {env_file_for_environment(normalized)}")
    print("----------------------------------------")


def run_command(command: Sequence[str], env: dict[str, str]) -> None:
    """执行命令并打印可读日志，方便本地排查。"""
    logger.info("准备执行命令: %s", " ".join(command))
    subprocess.run(command, env=env, check=True)
    logger.info("命令执行完成: %s", " ".join(command))


def bootstrap_database(env: dict[str, str]) -> None:
    """首次初始化数据库。

    标准流程：
    1. 创建数据库
    2. 执行 Alembic 迁移
    3. 如果迁移链路异常，再回退到 create_all 建表
    """
    logger.info("开始执行数据库 bootstrap 流程")
    run_command([sys.executable, "-m", "app.scripts.create_database"], env)

    try:
        logger.info("开始执行 Alembic 迁移到最新版本")
        run_command(["alembic", "upgrade", "head"], env)
        logger.info("Alembic 迁移执行完成")
    except subprocess.CalledProcessError as exc:
        logger.warning("Alembic 迁移失败，准备回退到 create_all 建表。错误: %s", exc)
        run_command([sys.executable, "-m", "app.scripts.init_database"], env)
        logger.info("create_all 建表流程执行完成，准备同步 Alembic 版本号")
        run_command(["alembic", "stamp", "head"], env)
        logger.info("Alembic 版本号已同步到最新")


def invoke_database_command(
    env_type: str,
    command: str,
    additional_args: Sequence[str],
) -> None:
    env = build_command_environment(env_type)

    if command in {"create_db", "create-db"}:
        run_command([sys.executable, "-m", "app.scripts.create_database"], env)
    elif command in {
        "bootstrap",
        "bootstrap_db",
        "bootstrap-db",
        "prepare_db",
        "prepare-db",
    }:
        bootstrap_database(env)
    elif command in {"init_db", "init-db", "create_tables", "create-tables"}:
        run_command([sys.executable, "-m", "app.scripts.init_database"], env)
    elif command in {
        "drop_db",
        "drop-db",
        "drop_tables",
        "drop-tables",
        "reset",
        "history",
        "current",
    }:
        run_command(
            [
                sys.executable,
                "-m",
                "app.scripts.manage_db",
                command.replace("_", "-"),
            ],
            env,
        )
    elif command == "migrate":
        run_command(["alembic", *additional_args], env)
    elif command in {"upgrade", "downgrade"}:
        run_command(["alembic", command, *additional_args], env)
    elif command in {"create-migration", "create_migration"}:
        if not additional_args:
            raise SystemExit("错误: 请提供迁移名称")
        migration_name = additional_args[0]
        run_command(
            ["alembic", "revision", "--autogenerate", "-m", migration_name],
            env,
        )
    elif command in {"run", "runserver"}:
        run_command([sys.executable, "run_app.py"], env)
    else:
        raise SystemExit(f"错误: 不支持的命令 {command}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(
            "用法: python -m app.scripts.set_env [env] [command] [options]"
        )

    env_type = sys.argv[1]
    print_environment_summary(env_type)

    if len(sys.argv) > 2:
        invoke_database_command(env_type, sys.argv[2], sys.argv[3:])
