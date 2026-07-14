from __future__ import annotations

import logging
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def create_runtime_engine(database_url: str) -> Engine:
    """创建 Runtime 自己的连接池，绝不复用情侣日记业务数据库连接。"""
    logging.info("创建 Runtime 数据库引擎 dialect=%s", database_url.split(":", 1)[0])
    return create_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """返回显式提交的 Session 工厂，事务边界由 Service 层控制。"""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Generator[Session]:
    """为 FastAPI dependency 预留的 session 生命周期，异常时安全回滚。"""
    session = factory()
    try:
        yield session
    except Exception:
        logging.warning("Runtime 数据库事务异常，执行回滚")
        session.rollback()
        raise
    finally:
        session.close()
