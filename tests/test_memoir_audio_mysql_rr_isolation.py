"""S2 MySQL REPEATABLE READ 当前读验收（显式 opt-in）。

只在 AGENT_RUNTIME_TEST_MYSQL_DSN 通过安检后连接：mysql 驱动、loopback、
test_ 用户与 test_ 库。未提供或不安全则 skip，禁止把 SQLite 当真行锁通过。
本文件只 SET SESSION 隔离级别，不改公共库全局 isolation，不迁移、不部署。
PG 仅补充：无 AGENT_RUNTIME_TEST_POSTGRES_DSN 或安检失败则 skip。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event, make_url
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.models import AgentRun
from app.models.memoir_audio_job import MemoirAudioJob
from app.services.memoir.memoir_audio_jobs import (
    ROLE_BACKGROUND_MUSIC,
    WORK_SCENE_ID,
    MemoirAudioCostPolicy,
    MemoirAudioJobReservation,
    MemoirAudioJobsService,
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_MYSQL_DSN_ENV = "AGENT_RUNTIME_TEST_MYSQL_DSN"
_POSTGRES_DSN_ENV = "AGENT_RUNTIME_TEST_POSTGRES_DSN"
_EPOCH = 3
_PACKAGE = "1.0.8"


def _policy() -> MemoirAudioCostPolicy:
    return MemoirAudioCostPolicy(
        currency="CNY",
        tts_price_per_1000_text_words=Decimal("1.5"),
        music_price_per_second=Decimal("0.05"),
        max_cost_per_run=Decimal("10.0"),
    )


def _make_run(session: Session, run_id: str) -> AgentRun:
    run = AgentRun(
        run_id=run_id,
        agent_id="memoir_agent",
        agent_version=_PACKAGE,
        package_digest="digest-test",
        contract_version="1.1.0",
        business_type="memory",
        business_id="biz-1",
        input_json={},
        authorization_version=1,
        caller_id="caller-1",
        tenant_id="couple-diary",
        create_idempotency_key=f"idem-{run_id}",
        callback_target_id="memory_callback",
        business_connector_id="couple_diary_backend",
        trace_id=f"trace-{run_id}",
        run_deadline_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
    )
    session.add(run)
    session.commit()
    return run


def _default_music_reservation(
    run_id: str, input_hmac: str
) -> MemoirAudioJobReservation:
    return MemoirAudioJobReservation(
        job_id=f"job-bgm-default-{input_hmac[:6]}",
        business_id="biz-1",
        run_id=run_id,
        generation_epoch=_EPOCH,
        package_version=_PACKAGE,
        role=ROLE_BACKGROUND_MUSIC,
        scene_id=WORK_SCENE_ID,
        input_hmac=input_hmac,
        estimated_cost=Decimal("0"),
        lease_owner="worker-a",
        requested_music_seconds=None,
        usage_text_words_estimate=None,
    )


def _safe_mysql_url(raw: str) -> URL | None:
    """安检失败返回 None，不把 DSN 写进异常或日志。"""
    try:
        url = make_url(raw)
    except Exception:
        return None
    if (
        not url.drivername.startswith("mysql")
        or url.host not in _LOOPBACK_HOSTS
        or not url.username
        or not url.username.startswith("test_")
        or not url.database
        or not url.database.startswith("test_")
    ):
        return None
    return url


def _mysql_url_or_skip() -> URL:
    raw = os.environ.get(_MYSQL_DSN_ENV)
    if not raw:
        pytest.skip(f"未显式提供 {_MYSQL_DSN_ENV}")
    url = _safe_mysql_url(raw)
    if url is None:
        pytest.skip(f"{_MYSQL_DSN_ENV} 未通过安全检查")
    return url


def _mysql_connect_args(url: URL) -> dict[str, Any]:
    if "mysqlconnector" in url.drivername:
        return {"connection_timeout": 10}
    return {"connect_timeout": 10}


def _attach_mysql_rr(engine: Engine) -> None:
    """每条连接 SET SESSION RR，不碰全局 isolation。"""

    @event.listens_for(engine, "connect")
    def _set_rr(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        finally:
            cursor.close()


def _lock_run(session: Session, run_id: str) -> AgentRun:
    run = session.scalar(
        sa.select(AgentRun)
        .where(AgentRun.run_id == run_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    assert run is not None
    return run


def _list_bgm(
    jobs: MemoirAudioJobsService, run_id: str, *, for_update: bool = False
) -> list[MemoirAudioJob]:
    return jobs.list_work_background_music_jobs(
        run_id, _EPOCH, _PACKAGE, for_update=for_update
    )


def _assert_rr_current_read_after_run_lock(
    factory: sessionmaker[Session], run_id: str
) -> None:
    """旧快照为空 → 旁 Session 提交 BGM → Run 锁后 FOR UPDATE 必须看见。

    同事务随后的普通 SELECT 仍应为空：这是 RR 快照未刷新的烟测，
    证明只锁 AgentRun 不能当权威互斥，必须对 BGM 做当前读。
    """
    with factory() as session_a, factory() as session_b:
        jobs_a = MemoirAudioJobsService(session_a, _policy())
        jobs_b = MemoirAudioJobsService(session_b, _policy())

        # 第一次普通 SELECT 建立一致快照：此时 BGM 必须为空。
        assert _list_bgm(jobs_a, run_id) == []

        jobs_b.reserve_job(_default_music_reservation(run_id, "hmac-rr-winner"))
        session_b.commit()

        _lock_run(session_a, run_id)
        # Run 锁之后普通 SELECT 仍读旧快照（空）。
        assert _list_bgm(jobs_a, run_id) == []
        # 当前读必须看见已提交行。
        current = _list_bgm(jobs_a, run_id, for_update=True)
        assert len(current) == 1
        assert current[0].input_hmac == "hmac-rr-winner"
        # FOR UPDATE 不改一致快照：随后普通 SELECT 仍空。
        assert _list_bgm(jobs_a, run_id) == []
        session_a.rollback()


def test_mysql_rr_run_lock_plain_select_stale_for_update_sees_committed() -> None:
    """MySQL 默认 RR：锁 Run 后普通 SELECT 仍空，BGM FOR UPDATE 看见已提交行。"""
    url = _mysql_url_or_skip()
    import app.models  # noqa: F401
    from app.db.sqlalchemy_db import Base

    engine = sa.create_engine(
        url,
        poolclass=NullPool,
        connect_args=_mysql_connect_args(url),
        pool_pre_ping=True,
    )
    _attach_mysql_rr(engine)
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        run_id = "run-s2-mysql-rr"
        with factory() as setup:
            _make_run(setup, run_id)
        _assert_rr_current_read_after_run_lock(factory, run_id)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_postgres_rr_not_claimed_as_mysql_current_read() -> None:
    """PG 仅补充 skip：RR 是快照隔离，FOR UPDATE 看不见快照后插入。

    S2 当前读针对 MySQL InnoDB locking read。无安全 DSN 如实 skip；
    有安全 DSN 也不把 PG 当成本项行锁通过。
    """
    raw = os.environ.get(_POSTGRES_DSN_ENV)
    if not raw:
        pytest.skip(f"未显式提供 {_POSTGRES_DSN_ENV}")

    from app.runtime.postgres_harness import PostgresHarnessConfig

    try:
        PostgresHarnessConfig(raw, f"agent_runtime_test_{os.getpid()}_s2rr", 10)
    except ValueError:
        pytest.skip(f"{_POSTGRES_DSN_ENV} 未通过安全检查")
    pytest.skip(
        "PG RR 为快照隔离，SELECT FOR UPDATE 看不见快照后插入；"
        "S2 当前读只对 MySQL InnoDB 验收"
    )
