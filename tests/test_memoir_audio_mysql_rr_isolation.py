"""S2 MySQL REPEATABLE READ 当前读验收（显式 opt-in）。

只在 AGENT_RUNTIME_TEST_MYSQL_DSN 通过安检后连接：mysql 驱动、loopback、
test_ 用户与 test_ 库。未提供或不安全则 skip，禁止把 SQLite 当真行锁通过。
本文件只 SET SESSION 隔离级别，不改公共库全局 isolation，不迁移、不部署。
PG 仅补充：无 AGENT_RUNTIME_TEST_POSTGRES_DSN 或安检失败则 skip。

资源所有权：GET_LOCK 命名锁独占保护下只接受空目标，非空目标拒绝且零写
零删；锁内查空 + create_all，建表后全部表都可证明由本次创建；__enter__
失败只 dispose、不 DROP；__exit__ 只 drop owned；禁止 metadata.drop_all。
离线替身覆盖非空拒绝/初始化竞争/失败不误删/重复运行零残留，不连真实库。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event, make_url
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.models import AgentRun
from app.models.memoir_audio_job import MemoirAudioJob
from app.services.memoir.memoir_audio_jobs import (
    ROLE_BACKGROUND_MUSIC,
    WORK_SCENE_ID,
    MemoirAudioCostPolicy,
    MemoirAudioJobReservation,
    MemoirAudioJobsError,
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


def _list_table_names(engine: Any) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def _drop_owned_tables(engine: Any, metadata: Any, owned: frozenset[str]) -> None:
    """只按 owned 单表 drop（checkfirst）。禁止 metadata.drop_all。"""
    if not owned:
        return
    for table in reversed(list(metadata.sorted_tables)):
        if table.name in owned:
            table.drop(engine, checkfirst=True)


class _MysqlInitLockError(RuntimeError):
    """初始化锁不可得：另一初始化者正在使用目标库，本测试未写删任何资源。"""


class _MysqlTargetNotEmptyError(RuntimeError):
    """目标库非空：无法证明独占，拒绝写入，要求改用专用空 test_ 目标。"""


class _GetLockInitGuard:
    """MySQL 命名锁独占保护：锁内完成"查空 + create_all"，串行化并发初始化。

    GET_LOCK 与业务行锁无关，只用于测试初始化互斥；锁随持锁连接断开自动
    释放，因此独占一条物理连接（NullPool 下 close 即真实断开）。
    """

    def __init__(self, engine: Any, lock_name: str, timeout_s: int = 10) -> None:
        self._engine = engine
        self._lock_name = lock_name
        self._timeout_s = int(timeout_s)
        self._conn: Any = None

    def __enter__(self) -> _GetLockInitGuard:
        self._conn = self._engine.connect()
        acquired = self._conn.execute(
            sa.text("SELECT GET_LOCK(:lock_name, :timeout)"),
            {"lock_name": self._lock_name, "timeout": self._timeout_s},
        ).scalar()
        if acquired != 1:
            # GET_LOCK 返回 0=超时 / NULL=出错，都按未取得处理：零写零删。
            self._conn.close()
            self._conn = None
            raise _MysqlInitLockError(
                f"{self._timeout_s}s 内未取得初始化锁 {self._lock_name!r}："
                "另一初始化者正在使用目标库"
            )
        return self

    def __exit__(self, *_: object) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.execute(
                sa.text("SELECT RELEASE_LOCK(:lock_name)"),
                {"lock_name": self._lock_name},
            )
        except Exception:
            # 释放失败由连接关闭兜底（断连自动放锁），不掩盖主流程。
            pass
        finally:
            conn.close()


class _MysqlOwnedTableHarness:
    """MySQL 测试资源所有权：独占保护下只接受空目标，只 drop 本测试的表。

    init_guard（GET_LOCK 命名锁）把"查空 + create_all"关进同一临界区：
    非空目标直接拒绝（零写零删）；锁内已证空后，create_all 产生的全部表
    都可证明由本次创建。__enter__ 失败只 dispose、不 DROP；__exit__ 只按
    owned 单表 drop。禁止 metadata.drop_all。不改公共 sqlalchemy_db。
    """

    def __init__(
        self,
        engine: Any,
        metadata: Any,
        *,
        init_guard: Any,
        list_tables: Callable[[Any], set[str]] | None = None,
    ) -> None:
        self.engine = engine
        self.metadata = metadata
        self._init_guard = init_guard
        self._list_tables = list_tables or _list_table_names
        self.owned_tables: frozenset[str] = frozenset()
        self._create_succeeded = False
        self.session_factory: sessionmaker[Session] | None = None

    def __enter__(self) -> _MysqlOwnedTableHarness:
        try:
            with self._init_guard:
                # 锁内查空：非空目标无法证明独占，拒绝且不建表、不删表。
                before = self._list_tables(self.engine)
                if before:
                    raise _MysqlTargetNotEmptyError(
                        "目标库非空（已有表："
                        f"{', '.join(sorted(before))}）；只接受独占空库，"
                        "请改用专用空 test_ 目标"
                    )
                self.metadata.create_all(self.engine)
                # 锁内已证空：建表后的全部表都由本次 create_all 创建。
                self.owned_tables = frozenset(self._list_tables(self.engine))
                self._create_succeeded = True
            if isinstance(self.engine, Engine):
                self.session_factory = sessionmaker(
                    bind=self.engine, expire_on_commit=False
                )
            return self
        except Exception:
            # 初始化失败：只 dispose，不 drop（无法证明残留表归属时不碰）。
            self.engine.dispose()
            raise

    def __exit__(self, *_: object) -> None:
        try:
            if self._create_succeeded:
                _drop_owned_tables(self.engine, self.metadata, self.owned_tables)
        finally:
            self.engine.dispose()


def _open_mysql_harness(url: URL) -> _MysqlOwnedTableHarness:
    """安检后的真库入口：命名锁独占初始化，RR 只绑本引擎，失败只 dispose。"""
    import app.models  # noqa: F401
    from app.db.sqlalchemy_db import Base

    engine = sa.create_engine(
        url,
        poolclass=NullPool,
        connect_args=_mysql_connect_args(url),
        pool_pre_ping=True,
    )
    try:
        _attach_mysql_rr(engine)
        init_guard = _GetLockInitGuard(
            engine,
            # 锁名含库名：不同 test_ 库互不阻塞；不含用户名/口令等敏感值。
            lock_name=f"agent_runtime_test_init:{url.database}",
            timeout_s=10,
        )
        return _MysqlOwnedTableHarness(engine, Base.metadata, init_guard=init_guard)
    except Exception:
        engine.dispose()
        raise


def _set_lock_wait_timeout(session: Session, seconds: int) -> None:
    """本连接短等锁，避免把整测试拖死。seconds 仅测试常量。"""
    timeout = int(seconds)
    session.execute(sa.text(f"SET SESSION innodb_lock_wait_timeout = {timeout}"))


def _is_lock_wait_timeout(exc: BaseException) -> bool:
    """识别 InnoDB 1205，不把其它 OperationalError 当成持锁证明。"""
    texts = [str(exc).lower()]
    orig = getattr(exc, "orig", None)
    if orig is not None:
        texts.append(str(orig).lower())
        errno = getattr(orig, "errno", None)
        if errno == 1205:
            return True
        args = getattr(orig, "args", ())
        if args and args[0] == 1205:
            return True
    return any("lock wait timeout" in text for text in texts)


class _RecordingEngine:
    """离线替身：记录建/删/释放，不连 127.0.0.1。

    tables 故意接收外部 set 引用：竞争回归里赢家/输家替身共享同一表空间，
    才能证明输家看得见、但绝不动赢家的表。rows 是哨兵数据，任何 harness
    路径都不得增删。
    """

    def __init__(self, tables: set[str], rows: frozenset[str] = frozenset()) -> None:
        self.tables = tables
        self.rows = rows
        self.disposed = False
        self.dropped: list[str] = []

    def dispose(self) -> None:
        self.disposed = True


class _RecordingTable:
    def __init__(self, name: str) -> None:
        self.name = name

    def drop(self, engine: _RecordingEngine, checkfirst: bool = False) -> None:
        # 真实 DROP 会移除表：供"重复运行零残留"断言使用。
        engine.tables.discard(self.name)
        engine.dropped.append(self.name)


class _RecordingMetadata:
    def __init__(self, names: list[str]) -> None:
        self.sorted_tables = [_RecordingTable(name) for name in names]
        self.tables = {table.name: table for table in self.sorted_tables}
        self.create_all_calls = 0
        self.drop_all_calls = 0

    def create_all(self, engine: _RecordingEngine) -> None:
        self.create_all_calls += 1
        # 模拟 SQLAlchemy create_all：把元数据中的表登记进共享表空间。
        for name in self.tables:
            engine.tables.add(name)

    def drop_all(self, engine: _RecordingEngine) -> None:
        self.drop_all_calls += 1
        engine.dropped.extend(sorted(engine.tables))


class _LockState:
    """模拟 MySQL 命名锁的服务器侧状态：held_by 标识当前持锁初始化者。"""

    def __init__(self) -> None:
        self.held_by: str | None = None


class _RecordingInitGuard:
    """离线替身：按 _LockState 模拟 GET_LOCK 独占，不连任何库。"""

    def __init__(self, state: _LockState, *, owner: str = "harness") -> None:
        self._state = state
        self._owner = owner

    def __enter__(self) -> _RecordingInitGuard:
        if self._state.held_by is not None:
            raise _MysqlInitLockError("另一初始化者持锁（离线模拟 GET_LOCK 超时）")
        self._state.held_by = self._owner
        return self

    def __exit__(self, *_: object) -> None:
        self._state.held_by = None


def test_mysql_nonempty_target_rejected_zero_write_zero_delete() -> None:
    """已有表与哨兵数据 → 拒绝非空目标，零写入零删除。无 DSN 也必须跑。"""
    engine = _RecordingEngine(
        {"agent_runs"}, rows=frozenset({"agent_runs:sentinel-row"})
    )
    metadata = _RecordingMetadata(["agent_runs", "memoir_audio_jobs"])
    harness = _MysqlOwnedTableHarness(
        engine,
        metadata,
        init_guard=_RecordingInitGuard(_LockState()),
        list_tables=lambda item: set(item.tables),
    )
    with pytest.raises(_MysqlTargetNotEmptyError, match="目标库非空"):
        harness.__enter__()
    # 零写：未 create_all；零删：未 drop、未 drop_all。
    assert metadata.create_all_calls == 0
    assert engine.dropped == []
    assert metadata.drop_all_calls == 0
    # 已有表与哨兵数据原样保留。
    assert engine.tables == {"agent_runs"}
    assert engine.rows == frozenset({"agent_runs:sentinel-row"})
    assert engine.disposed


def test_mysql_init_race_loser_lock_timeout_never_writes_or_drops() -> None:
    """两初始化者竞争（输家取锁超时）：输家零写零删、不认领任何表。"""
    lock_state = _LockState()
    # 模拟赢家正处于锁内初始化（查空 + create_all 期间）。
    lock_state.held_by = "winner-init"
    engine = _RecordingEngine(set())
    metadata = _RecordingMetadata(["agent_runs", "memoir_audio_jobs"])
    harness = _MysqlOwnedTableHarness(
        engine,
        metadata,
        init_guard=_RecordingInitGuard(lock_state),
        list_tables=lambda item: set(item.tables),
    )
    with pytest.raises(_MysqlInitLockError, match="另一初始化者"):
        harness.__enter__()
    assert metadata.create_all_calls == 0
    assert engine.dropped == []
    assert engine.tables == set()
    assert harness.owned_tables == frozenset()
    assert engine.disposed


def test_mysql_init_race_loser_rejects_winner_resources() -> None:
    """赢家已建表未清理时输家进入：拒绝且不动赢家资源，输家只 dispose 自己。"""
    shared_tables: set[str] = set()
    lock_state = _LockState()
    winner_engine = _RecordingEngine(shared_tables)
    loser_engine = _RecordingEngine(shared_tables)
    winner_meta = _RecordingMetadata(["agent_runs", "memoir_audio_jobs"])
    loser_meta = _RecordingMetadata(["agent_runs", "memoir_audio_jobs"])
    winner = _MysqlOwnedTableHarness(
        winner_engine,
        winner_meta,
        init_guard=_RecordingInitGuard(lock_state),
        list_tables=lambda item: set(item.tables),
    )
    with winner:
        assert winner.owned_tables == frozenset({"agent_runs", "memoir_audio_jobs"})
        # 赢家初始化已完成（锁已释放）但表仍在：输家取得锁后必须拒绝。
        loser = _MysqlOwnedTableHarness(
            loser_engine,
            loser_meta,
            init_guard=_RecordingInitGuard(lock_state),
            list_tables=lambda item: set(item.tables),
        )
        with pytest.raises(_MysqlTargetNotEmptyError, match="目标库非空"):
            loser.__enter__()
        # 输家不认领、不清理赢家资源；只 dispose 自己的引擎。
        assert loser.owned_tables == frozenset()
        assert loser_meta.create_all_calls == 0
        assert loser_engine.dropped == []
        assert shared_tables == {"agent_runs", "memoir_audio_jobs"}
        assert loser_engine.disposed
        assert not winner_engine.disposed
        # 输家拒绝后锁已归还，赢家不受影响。
        assert lock_state.held_by is None
    # 赢家退出只清理自己创建的表。
    assert winner_engine.dropped == ["memoir_audio_jobs", "agent_runs"]
    assert shared_tables == set()


def test_mysql_owned_init_failure_does_not_drop_any_table() -> None:
    """create_all / 初始化失败只 dispose，不得 drop 任何表。"""
    engine = _RecordingEngine(set())

    class _BoomMetadata(_RecordingMetadata):
        def create_all(self, engine: _RecordingEngine) -> None:
            raise RuntimeError("create_all 失败")

    metadata = _BoomMetadata(["agent_runs"])
    with pytest.raises(RuntimeError, match="create_all"):
        with _MysqlOwnedTableHarness(
            engine,
            metadata,
            init_guard=_RecordingInitGuard(_LockState()),
            list_tables=lambda item: set(item.tables),
        ):
            raise AssertionError("初始化失败不得进入 with 体")
    assert engine.dropped == []
    assert metadata.drop_all_calls == 0
    assert engine.tables == set()
    assert engine.disposed


def test_mysql_owned_cleanup_never_calls_metadata_drop_all() -> None:
    """禁止 create_all-then-unconditional-drop_all 所有权模型。"""
    engine = _RecordingEngine(set())
    metadata = _RecordingMetadata(["agent_runs", "memoir_audio_jobs"])
    with _MysqlOwnedTableHarness(
        engine,
        metadata,
        init_guard=_RecordingInitGuard(_LockState()),
        list_tables=lambda item: set(item.tables),
    ) as harness:
        assert harness.owned_tables == frozenset({"agent_runs", "memoir_audio_jobs"})
    assert metadata.drop_all_calls == 0
    assert set(engine.dropped) == {"agent_runs", "memoir_audio_jobs"}
    assert engine.tables == set()
    assert engine.disposed


def test_mysql_repeat_runs_cleanup_own_resources_no_residue() -> None:
    """成功后清理自身资源；重复运行两轮后表空间零残留。"""
    engine = _RecordingEngine(set())
    metadata = _RecordingMetadata(["agent_runs", "memoir_audio_jobs"])
    for _ in range(2):
        with _MysqlOwnedTableHarness(
            engine,
            metadata,
            init_guard=_RecordingInitGuard(_LockState()),
            list_tables=lambda item: set(item.tables),
        ) as harness:
            # 空目标两轮都可证明独占：owned 恒为本次 create_all 的全部表。
            assert harness.owned_tables == frozenset(
                {"agent_runs", "memoir_audio_jobs"}
            )
        # 每轮退出后表空间必须干净，否则下一轮会按非空目标拒绝。
        assert engine.tables == set()
    assert engine.dropped == ["memoir_audio_jobs", "agent_runs"] * 2
    assert metadata.create_all_calls == 2
    assert metadata.drop_all_calls == 0
    assert engine.disposed


def test_mysql_dsn_reject_does_not_drop_tables() -> None:
    """安检失败不得建连、不得 drop。不连真实库。"""
    engine = _RecordingEngine({"agent_runs"})
    metadata = _RecordingMetadata(["agent_runs"])
    assert _safe_mysql_url("mysql+mysqlconnector://root@8.8.8.8/prod") is None
    assert _safe_mysql_url("postgresql+psycopg://test_u@8.8.8.8/test_db") is None
    _drop_owned_tables(engine, metadata, frozenset())
    assert engine.dropped == []
    assert metadata.drop_all_calls == 0


def test_mysql_rr_run_lock_plain_select_stale_for_update_sees_committed() -> None:
    """MySQL 默认 RR：锁 Run 后普通 SELECT 仍空，BGM FOR UPDATE 看见已提交行。"""
    url = _mysql_url_or_skip()
    with _open_mysql_harness(url) as harness:
        factory = harness.session_factory
        assert factory is not None
        run_id = "run-s2-mysql-rr"
        with factory() as setup:
            _make_run(setup, run_id)
        _assert_rr_current_read_after_run_lock(factory, run_id)


def test_mysql_rr_slot_active_commit_releases_run_and_bgm_locks() -> None:
    """SLOT_ACTIVE 后外层 commit 才放锁；另一连接才能及时取得 Run/BGM 锁。

    jobs 层 savepoint 回滚不是放锁。验收主路径是 commit 结束外层事务，
    对齐冻结「吸收后 _commit_ledger」。本测试只调 reserve_job，不改 jobs。
    """
    url = _mysql_url_or_skip()
    with _open_mysql_harness(url) as harness:
        factory = harness.session_factory
        assert factory is not None
        run_id = "run-s2-mysql-reject-unlock"
        with factory() as setup:
            _make_run(setup, run_id)

        # 1. Session W 占槽并 commit。
        with factory() as session_w:
            MemoirAudioJobsService(session_w, _policy()).reserve_job(
                _default_music_reservation(run_id, "hmac-winner")
            )
            session_w.commit()

        session_l = factory()
        try:
            # 2. Session L 不同 hmac → SLOT_ACTIVE；外层事务仍持 Run/BGM 锁。
            with pytest.raises(MemoirAudioJobsError) as reserve_exc:
                MemoirAudioJobsService(session_l, _policy()).reserve_job(
                    _default_music_reservation(run_id, "hmac-loser")
                )
            assert reserve_exc.value.code == "MEMOIR_AUDIO_JOB_SLOT_ACTIVE"

            # 3. L 不结束事务时，C 对同一 Run FOR UPDATE 应等锁/超时。
            with factory() as session_wait:
                _set_lock_wait_timeout(session_wait, 1)
                with pytest.raises(DBAPIError) as wait_exc:
                    _lock_run(session_wait, run_id)
                assert _is_lock_wait_timeout(wait_exc.value)

            # 4. 主路径：commit 结束持锁外层事务（不用 rollback 当放锁方案）。
            session_l.commit()
        finally:
            session_l.close()

        # 5. 之后 Session C 必须及时取得 Run/BGM 锁。
        with factory() as session_c:
            _set_lock_wait_timeout(session_c, 2)
            _lock_run(session_c, run_id)
            locked = _list_bgm(
                MemoirAudioJobsService(session_c, _policy()),
                run_id,
                for_update=True,
            )
            assert len(locked) == 1
            assert locked[0].input_hmac == "hmac-winner"
            session_c.commit()

        # 6. 至多一个 BGM 槽。
        with factory() as check:
            rows = _list_bgm(MemoirAudioJobsService(check, _policy()), run_id)
            assert len(rows) == 1


def test_postgres_rr_not_claimed_as_mysql_current_read() -> None:
    """PG 仅补充 skip：RR 是快照隔离，FOR UPDATE 看不见快照后插入。

    S2 当前读针对 MySQL InnoDB locking read。无安全 DSN 如实 skip；
    有安全 DSN 也不把 PG 当成本项行锁通过。不 drop 别人的 schema。
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
