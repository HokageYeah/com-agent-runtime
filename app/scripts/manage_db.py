#!/usr/bin/env python3
from __future__ import annotations

import logging
import subprocess

import click
from sqlalchemy import create_engine, text

from app.config.database_config import get_database_config, get_database_url
from app.db.sqlalchemy_db import Base

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


@click.group()
def cli():
    """数据库管理工具"""


@cli.command(name="create-db")
def create_db():
    """创建数据库。"""
    config = get_database_config()
    logger.info("manage_db.create_db -> 目标数据库: %s", config["database"])
    url_without_db = (
        f"{config['driver']}://{config['username']}:{config['password']}"
        f"@{config['host']}:{config['port']}"
    )
    engine = create_engine(url_without_db)

    with engine.connect() as conn:
        conn.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS {config['database']} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )

    click.echo(f"数据库 {config['database']} 创建成功")


@cli.command(name="drop-db")
def drop_db():
    """删除数据库。"""
    config = get_database_config()
    logger.info("manage_db.drop_db -> 目标数据库: %s", config["database"])
    url_without_db = (
        f"{config['driver']}://{config['username']}:{config['password']}"
        f"@{config['host']}:{config['port']}"
    )
    engine = create_engine(url_without_db)

    with engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {config['database']}"))

    click.echo(f"数据库 {config['database']} 删除成功")


@cli.command(name="create-tables")
def create_tables():
    """直接创建全部表。"""
    logger.info("manage_db.create_tables -> 使用 create_all 创建全部表")
    database_url = get_database_url()
    logger.info("manage_db.create_tables -> 当前数据库连接: %s", database_url)
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    click.echo("所有表创建成功")


@cli.command(name="drop-tables")
def drop_tables():
    """直接删除全部表。"""
    logger.info("manage_db.drop_tables -> 删除全部表")
    database_url = get_database_url()
    logger.info("manage_db.drop_tables -> 当前数据库连接: %s", database_url)
    engine = create_engine(database_url)
    Base.metadata.drop_all(engine)
    click.echo("所有表删除成功")


@cli.command(context_settings=dict(ignore_unknown_options=True))
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def migrate(args):
    """透传 Alembic 迁移命令。"""
    logger.info("manage_db.migrate -> alembic %s", " ".join(args))
    subprocess.run(["alembic", *args], check=True)


@cli.command()
def upgrade():
    """升级到最新迁移版本。"""
    logger.info("manage_db.upgrade -> alembic upgrade head")
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    click.echo("数据库迁移已应用到最新版本")


@cli.command()
def downgrade():
    """回滚一个迁移版本。"""
    logger.info("manage_db.downgrade -> alembic downgrade -1")
    subprocess.run(["alembic", "downgrade", "-1"], check=True)
    click.echo("数据库已回滚到上一个版本")


@cli.command()
def reset():
    """重置数据库并重新应用迁移。"""
    logger.info("manage_db.reset -> 开始重置数据库")
    subprocess.run(["alembic", "downgrade", "base"], check=True)
    config = get_database_config()
    url_without_db = (
        f"{config['driver']}://{config['username']}:{config['password']}"
        f"@{config['host']}:{config['port']}"
    )
    engine = create_engine(url_without_db)

    with engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {config['database']}"))
        conn.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS {config['database']} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )

    subprocess.run(["alembic", "upgrade", "head"], check=True)
    click.echo("数据库重置成功")


@cli.command()
def history():
    """查看迁移历史。"""
    logger.info("manage_db.history -> alembic history")
    subprocess.run(["alembic", "history"], check=True)


@cli.command()
def current():
    """查看当前迁移版本。"""
    logger.info("manage_db.current -> alembic current")
    subprocess.run(["alembic", "current"], check=True)


if __name__ == "__main__":
    cli()
