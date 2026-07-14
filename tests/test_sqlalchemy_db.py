from __future__ import annotations

from unittest.mock import PropertyMock, patch

from app.core.config import ApplicationConfig, settings
from app.db import sqlalchemy_db


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_get_sqlalchemy_db_yields_session_and_closes_after_use() -> None:
    session = FakeSession()

    def fake_session_generator():
        try:
            yield session
        finally:
            session.close()

    with patch.object(
        sqlalchemy_db.database,
        "get_session",
        return_value=fake_session_generator(),
    ):
        dependency = sqlalchemy_db.get_sqlalchemy_db()

        yielded = next(dependency)
        assert yielded is session
        assert session.closed is False

        try:
            next(dependency)
        except StopIteration:
            pass
        else:
            raise AssertionError("dependency generator 应该在第二次 next() 时结束")

        assert session.closed is True


def test_database_connect_reads_latest_runtime_config() -> None:
    """验证 connect() 会在真正连接前重新读取最新环境配置。"""

    latest_config = {
        "driver": "mysql+mysqlconnector",
        "username": "latest_user",
        "password": "latest_password",
        "host": "localhost",
        "port": 3306,
        "database": "latest_db",
        "charset": "utf8mb4",
        "echo": False,
        "pool_size": 5,
        "max_overflow": 10,
        "pool_recycle": 3600,
        "pool_timeout": 30,
    }

    class FakeSessionContext:
        def __enter__(self) -> FakeSessionContext:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def execute(self, statement) -> None:
            self.statement = statement

    fake_session_context = FakeSessionContext()

    with (
        patch.object(
            sqlalchemy_db,
            "get_database_config",
            return_value=latest_config,
        ),
        patch.object(
            sqlalchemy_db,
            "get_database_url",
            return_value="mysql+mysqlconnector://latest_user:latest_password@localhost:3306/latest_db?charset=utf8mb4",
        ),
        patch.object(
            sqlalchemy_db,
            "has_placeholder_database_credentials",
            return_value=False,
        ),
        patch.object(
            sqlalchemy_db,
            "create_engine",
            return_value="fake-engine",
        ) as mock_create_engine,
        patch.object(sqlalchemy_db, "sessionmaker") as mock_sessionmaker,
    ):
        mock_session_factory = mock_sessionmaker.return_value
        mock_session_factory.return_value = fake_session_context

        database = sqlalchemy_db.Database()
        database.db_config = {
            **latest_config,
            "username": "stale_user",
            "password": "stale_password",
            "database": "stale_db",
        }
        database.db_url = "mysql+mysqlconnector://stale_user:stale_password@localhost:3306/stale_db?charset=utf8mb4"

        database.connect()

        assert database.db_config["username"] == "latest_user"
        assert database.db_config["database"] == "latest_db"
        assert "latest_user" in database.db_url

        _, create_engine_kwargs = mock_create_engine.call_args
        assert (
            mock_create_engine.call_args.args[0]
            == "mysql+mysqlconnector://latest_user:latest_password@localhost:3306/latest_db?charset=utf8mb4"
        )
        assert create_engine_kwargs["pool_size"] == latest_config["pool_size"]


def test_database_connect_uses_application_group_environment_in_error_message() -> None:
    """占位账号密码报错时，应优先使用 application 分组里的环境信息。"""

    latest_config = {
        "driver": "mysql+mysqlconnector",
        "username": "your_mysql_user",
        "password": "your_mysql_password",
        "host": "localhost",
        "port": 3306,
        "database": "latest_db",
        "charset": "utf8mb4",
        "echo": False,
        "pool_size": 5,
        "max_overflow": 10,
        "pool_recycle": 3600,
        "pool_timeout": 30,
    }

    with (
        patch.object(
            sqlalchemy_db,
            "get_database_config",
            return_value=latest_config,
        ),
        patch.object(
            sqlalchemy_db,
            "get_database_url",
            return_value="mysql+mysqlconnector://your_mysql_user:your_mysql_password@localhost:3306/latest_db?charset=utf8mb4",
        ),
        patch.object(
            type(settings),
            "application",
            new_callable=PropertyMock,
            return_value=ApplicationConfig(
                project_name=settings.PROJECT_NAME,
                project_description=settings.PROJECT_DESCRIPTION,
                project_version=settings.PROJECT_VERSION,
                api_prefix=settings.API_PREFIX,
                response_version=settings.VERSION,
                environment="grouped-test-environment",
                debug=settings.DEBUG,
            ),
        ),
    ):
        database = sqlalchemy_db.Database()

        try:
            database.connect()
        except RuntimeError as exc:
            assert "grouped-test-environment" in str(exc)
        else:
            raise AssertionError("占位账号密码时应该抛出 RuntimeError")
