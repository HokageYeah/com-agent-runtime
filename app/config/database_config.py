from __future__ import annotations

from typing import Any

from sqlalchemy.engine import URL

from app.core.config import settings


def get_database_config() -> dict[str, Any]:
    """返回数据库模块统一使用的配置快照。

    这里优先从 `settings.database` 读取，而不是继续散落读取 `settings.DB_*`。
    这样后续如果数据库配置继续演进，真正依赖数据库的代码只需要关心这一个分组。
    """
    database_config = settings.database
    return {
        "driver": database_config.driver,
        "username": database_config.username,
        "password": database_config.password,
        "host": database_config.host,
        "port": database_config.port,
        "database": database_config.database,
        "charset": database_config.charset,
        "echo": database_config.echo,
        "pool_size": database_config.pool_size,
        "max_overflow": database_config.max_overflow,
        "pool_recycle": database_config.pool_recycle,
        "pool_timeout": database_config.pool_timeout,
    }


def get_database_url() -> str:
    """按当前运行时 settings 实时拼装数据库连接串。

    这里故意保留为函数，而不是只依赖模块级常量。
    原因是本项目会通过脚本切换 ENVIRONMENT，不同命令进程可能加载不同配置，
    如果把 URL 在导入阶段就固定，后续排查会非常痛苦。
    """
    config = get_database_config()
    url = URL.create(
        drivername=config["driver"],
        username=config["username"],
        password=config["password"],
        host=config["host"],
        port=config["port"],
        database=config["database"],
        query={"charset": config["charset"]},
    )
    return url.render_as_string(hide_password=False)
