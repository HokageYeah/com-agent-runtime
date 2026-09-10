"""M8 R7 迁移测试：memoir_audio_jobs / memoir_audio_run_budgets 建表与约束。

在 SQLite 内存库上直接执行迁移模块的 upgrade/downgrade（不连真实库）：
1. 建表成功、两表可写；
2. 唯一槽约束挡重复槽位插入；
3. check 约束挡非法 role；
4. 同库 upgrade→downgrade 后两表消失；重复 upgrade 不炸（幂等守卫）；
5. 迁移链单 head = 20260907_1000。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

# 迁移文件：从测试文件定位仓根 alembic/versions。
_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = _REPO_ROOT / "alembic" / "versions" / "20260907_1000_add_memoir_audio_jobs.py"
REVISION = "20260907_1000"

# 最小合法作业行（SQLite 直插，绕开 ORM 默认值）。
_JOB_ROW = {
    "job_id": "job-1",
    "business_id": "biz-1",
    "run_id": "run-1",
    "generation_epoch": 3,
    "package_version": "1.0.8",
    "role": "narration",
    "scene_id": "scene-1",
    "segment_index": -1,
    "input_hmac": "hmac-a",
    "attempt": 1,
    "state": "reserved",
    "reserved_cost": 0.15,
    "currency": "CNY",
    "lease_owner": "worker-a",
    "lease_token": 1,
}


def _load_migration():
    """按路径加载迁移模块（不依赖 alembic 版本目录注册顺序）。"""
    spec = importlib.util.spec_from_file_location("memoir_audio_jobs_migration", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(engine: sa.Engine, direction: str) -> None:
    """在给定引擎上执行一次 upgrade/downgrade，注入 op 代理后还原。"""
    module = _load_migration()
    with engine.connect() as connection:
        operations = Operations(MigrationContext.configure(connection))
        Operations._install_proxy(operations)  # noqa: SLF001 官方代理注入 API
        try:
            getattr(module, direction)()
        finally:
            operations._remove_proxy()  # noqa: SLF001 实例方法：清除模块级代理
        connection.commit()


def _insert_job(engine: sa.Engine, **overrides: object) -> None:
    """text SQL 直插一行作业（override 覆盖任意列，便于构造非法值）。"""
    row = {**_JOB_ROW, **overrides}
    columns = ", ".join(row.keys())
    marks = ", ".join(f":{name}" for name in row)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                f"INSERT INTO memoir_audio_jobs ({columns}) VALUES ({marks})"
            ),
            dict(row),
        )


@pytest.fixture
def migrated_engine():
    """跑完 upgrade 的 SQLite 内存引擎；用例结束销毁。"""
    engine = sa.create_engine("sqlite://")
    _run_migration(engine, "upgrade")
    yield engine
    engine.dispose()


def test_revision_chain_text() -> None:
    """迁移标识：新 head 接在 20260820_0900 之后。"""
    module = _load_migration()
    assert module.revision == REVISION
    assert module.down_revision == "20260820_0900"


def test_single_head_in_script_directory() -> None:
    """全仓迁移链只有一个 head，即本迁移（无分叉）。"""
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [REVISION]


def test_upgrade_creates_both_tables(migrated_engine: sa.Engine) -> None:
    """upgrade 后两表存在且为空表。"""
    inspector = sa.inspect(migrated_engine)
    assert inspector.has_table("memoir_audio_jobs")
    assert inspector.has_table("memoir_audio_run_budgets")
    with migrated_engine.connect() as connection:
        count = connection.execute(sa.text("SELECT COUNT(*) FROM memoir_audio_jobs")).scalar()
        assert count == 0


def test_unique_slot_rejects_duplicate(migrated_engine: sa.Engine) -> None:
    """唯一槽约束：同槽位七元组只允许一行（job_id 不同也算冲突）。"""
    _insert_job(migrated_engine)
    with pytest.raises(sa.exc.IntegrityError):
        _insert_job(migrated_engine, job_id="job-2")


def test_check_constraint_rejects_bad_role(migrated_engine: sa.Engine) -> None:
    """check 约束：非法 role 不得入账。"""
    with pytest.raises(sa.exc.IntegrityError):
        _insert_job(migrated_engine, job_id="job-bad", role="suspicious_role")


def test_downgrade_drops_both_tables() -> None:
    """同库 upgrade→downgrade：两表被删除。"""
    engine = sa.create_engine("sqlite://")
    _run_migration(engine, "upgrade")
    _run_migration(engine, "downgrade")
    inspector = sa.inspect(engine)
    assert not inspector.has_table("memoir_audio_jobs")
    assert not inspector.has_table("memoir_audio_run_budgets")
    engine.dispose()


def test_upgrade_twice_is_idempotent() -> None:
    """同库连续两次 upgrade 不报错（has_table 守卫防重复建表）。"""
    engine = sa.create_engine("sqlite://")
    _run_migration(engine, "upgrade")
    _run_migration(engine, "upgrade")
    assert sa.inspect(engine).has_table("memoir_audio_jobs")
    engine.dispose()
