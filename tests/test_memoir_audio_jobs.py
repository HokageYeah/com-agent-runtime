"""M8 R7 音频作业账本测试：唯一槽、费用预留、lease/fencing、恢复与维护入口。

全部使用 SQLite 内存库（PostgreSQL 行为用例按既有 harness 规范显式提供
AGENT_RUNTIME_TEST_POSTGRES_DSN 才运行），不连接真实业务库、不触网、
不调用付费接口。隐私断言：正文、私有 URL、prompt、音频字节绝不入表。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.models import AgentRun
from app.models.memoir_audio_job import MemoirAudioJob, MemoirAudioRunBudget
from app.services.memoir.memoir_audio_jobs import (
    _ACTIVE_STATES,
    NO_SEGMENT_INDEX,
    ROLE_BACKGROUND_MUSIC,
    ROLE_NARRATION,
    ROLE_SEGMENT,
    STATE_CLEANED,
    STATE_FAILED,
    STATE_PUBLISHED,
    STATE_RESERVED,
    STATE_SUBMISSION_UNKNOWN,
    STATE_SUBMITTED,
    STATE_UPLOADED,
    WORK_SCENE_ID,
    MemoirAudioCostPolicy,
    MemoirAudioJobReservation,
    MemoirAudioJobsError,
    MemoirAudioJobsService,
    estimate_music_cost,
    estimate_tts_cost,
)


def _policy(**overrides: Any) -> MemoirAudioCostPolicy:
    """测试计费策略：TTS 1.5 元/千字、音乐 0.05 元/秒、单 Run 上限 10 元。

    上限 10 覆盖 60 秒音乐 3 元的预留；预算封顶用例都显式传更小上限。
    """
    values: dict[str, Any] = {
        "currency": "CNY",
        "tts_price_per_1000_text_words": Decimal("1.5"),
        "music_price_per_second": Decimal("0.05"),
        "max_cost_per_run": Decimal("10.0"),
    }
    values.update(overrides)
    return MemoirAudioCostPolicy(**values)


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    """SQLite 内存库：只建 Runtime 元数据表，不连任何真实数据库。"""
    import app.models  # noqa: F401 确保全部模型注册进 Base.metadata
    from app.db.sqlalchemy_db import Base

    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _make_run(session: Session, run_id: str, *, business_id: str = "biz-1") -> AgentRun:
    """构造最小合法 AgentRun 行，供门禁（取消/隐私）检查读取。"""
    run = AgentRun(
        run_id=run_id,
        agent_id="memoir_agent",
        agent_version="1.0.8",
        package_digest="digest-test",
        contract_version="1.1.0",
        business_type="memory",
        business_id=business_id,
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


def _narration_reservation(
    run_id: str, scene_id: str, input_hmac: str, **overrides: Any
) -> MemoirAudioJobReservation:
    """构造旁白最终资产的预留请求；不同 scene 同正文可并存。"""
    values: dict[str, Any] = {
        "job_id": f"job-{scene_id}-{input_hmac[:6]}",
        "business_id": "biz-1",
        "run_id": run_id,
        "generation_epoch": 3,
        "package_version": "1.0.8",
        "role": ROLE_NARRATION,
        "scene_id": scene_id,
        "input_hmac": input_hmac,
        "estimated_cost": Decimal("0.15"),
        "lease_owner": "worker-a",
        "usage_text_words_estimate": 100,
    }
    values.update(overrides)
    return MemoirAudioJobReservation(**values)


def _music_reservation(run_id: str, input_hmac: str) -> MemoirAudioJobReservation:
    """构造 BGM 预留请求：scene 使用作品级哨兵 __work__。"""
    return _narration_reservation(
        run_id,
        WORK_SCENE_ID,
        input_hmac,
        job_id=f"job-bgm-{input_hmac[:6]}",
        role=ROLE_BACKGROUND_MUSIC,
        estimated_cost=estimate_music_cost(60, _policy()),
        requested_music_seconds=60,
        usage_text_words_estimate=None,
    )


def _default_music_reservation(
    run_id: str, input_hmac: str, **overrides: Any
) -> MemoirAudioJobReservation:
    """构造默认 BGM 零费预留（freeze §4.1）：estimated_cost=0 且不携带秒数。

    与付费 helper _music_reservation 互不替代：零费槽永不声明请求秒数。
    """
    values: dict[str, Any] = {
        "job_id": f"job-bgm-default-{input_hmac[:6]}",
        "role": ROLE_BACKGROUND_MUSIC,
        "estimated_cost": Decimal("0"),
        "requested_music_seconds": None,
        "usage_text_words_estimate": None,
    }
    values.update(overrides)
    return _narration_reservation(run_id, WORK_SCENE_ID, input_hmac, **values)


# ---------------------------------------------------------------------------
# Step 1：唯一槽与并发
# ---------------------------------------------------------------------------


def test_reserve_same_slot_twice_only_one_row(session_factory: sessionmaker[Session]) -> None:
    """同 Run 同输入并发抢槽：后到者拿到固定错误码，且只有一行账。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        first = service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a"))
        assert first.outcome == "created"

        with pytest.raises(MemoirAudioJobsError) as err:
            service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a"))
        assert err.value.code == "MEMOIR_AUDIO_JOB_SLOT_ACTIVE"

        session.commit()
        rows = session.scalars(sa.select(MemoirAudioJob)).all()
        assert len(rows) == 1
        budget = session.scalar(sa.select(MemoirAudioRunBudget))
        assert budget is not None
        # 失败方不占预算：预算行只记一次 0.15。
        assert float(budget.reserved_total_cost) == pytest.approx(0.15)


def test_same_text_different_scenes_hold_independent_slots(
    session_factory: sessionmaker[Session],
) -> None:
    """相同正文不同 scene：各自独立占槽，互不冲突、不共享资产。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-same"))
        service.reserve_job(_narration_reservation("run-1", "scene-2", "hmac-same"))
        session.commit()
        assert len(session.scalars(sa.select(MemoirAudioJob)).all()) == 2


def test_budget_cap_rejects_reservation(session_factory: sessionmaker[Session]) -> None:
    """Run 剩余预算不足时拒绝新预留，且不留下半开的账。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        tight = _policy(max_cost_per_run=Decimal("0.4"))
        service = MemoirAudioJobsService(session, tight)
        service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a"))
        service.reserve_job(_narration_reservation("run-1", "scene-2", "hmac-b"))
        with pytest.raises(MemoirAudioJobsError) as err:
            service.reserve_job(_narration_reservation("run-1", "scene-3", "hmac-c"))
        assert err.value.code == "MEMOIR_AUDIO_BUDGET_EXCEEDED"
        session.commit()
        budget = session.scalar(sa.select(MemoirAudioRunBudget))
        assert budget is not None
        assert float(budget.reserved_total_cost) == pytest.approx(0.30)


def test_interleaved_sessions_never_overspend(session_factory: sessionmaker[Session]) -> None:
    """跨 Session 交错预留（SQLite 单写者语义）：后开事务必须看到已提交余额。"""
    with session_factory() as session_a, session_factory() as session_b:
        _make_run(session_a, "run-1")
        tight = _policy(max_cost_per_run=Decimal("0.3"))
        service_a = MemoirAudioJobsService(session_a, tight)
        service_a.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a"))
        service_a.reserve_job(_narration_reservation("run-1", "scene-2", "hmac-b"))
        session_a.commit()

        service_b = MemoirAudioJobsService(session_b, tight)
        with pytest.raises(MemoirAudioJobsError) as err:
            service_b.reserve_job(_narration_reservation("run-1", "scene-3", "hmac-c"))
        assert err.value.code == "MEMOIR_AUDIO_BUDGET_EXCEEDED"
        session_b.commit()
        with session_factory() as check:
            budget = check.scalar(sa.select(MemoirAudioRunBudget))
            assert budget is not None
            assert float(budget.reserved_total_cost) == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# Step 2：状态机、对象键固定与结算
# ---------------------------------------------------------------------------


def test_music_full_state_machine_with_settlement(
    session_factory: sessionmaker[Session],
) -> None:
    """BGM 全链路：reserved→submitting→submitted(TaskID)→processing→uploaded→published。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(_music_reservation("run-1", "hmac-bgm")).job
        token = reserved.lease_token

        service.mark_submitting(reserved.job_id, token)
        service.mark_submitted(reserved.job_id, token, provider_task_id="task-1")
        service.settle_music_usage(reserved.job_id, token, requested_music_seconds=60)
        service.mark_processing(reserved.job_id, token)
        service.record_object_key(
            reserved.job_id, token, "memoir-test/audios/background/scope/bgm-abc.mp3", "audio/mpeg"
        )
        service.record_upload(reserved.job_id, token, duration_ms=60_000)
        service.mark_published(reserved.job_id, token)

        job = service.find_job(
            run_id="run-1",
            generation_epoch=3,
            package_version="1.0.8",
            role=ROLE_BACKGROUND_MUSIC,
            scene_id=WORK_SCENE_ID,
            input_hmac="hmac-bgm",
        )
        assert job is not None
        assert job.state == STATE_PUBLISHED
        assert job.provider_task_id == "task-1"
        assert job.requested_music_seconds == 60
        assert float(job.settled_cost) == pytest.approx(3.0)  # 60s * 0.05 元/秒


def test_tts_segment_lifecycle_settles_by_text_words(
    session_factory: sessionmaker[Session],
) -> None:
    """TTS 分段：同步 SSE 无 TaskID，按实际 text_words 结算。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(
            _narration_reservation(
                "run-1",
                "scene-1",
                "hmac-seg",
                job_id="job-seg-1",
                role=ROLE_SEGMENT,
                segment_index=0,
                estimated_cost=estimate_tts_cost("1234567890", _policy()),
                usage_text_words_estimate=10,
            )
        ).job
        token = reserved.lease_token

        service.mark_submitting(reserved.job_id, token)
        service.mark_submitted(reserved.job_id, token)  # TTS 无 TaskID
        service.settle_tts_usage(reserved.job_id, token, usage_text_words=10)
        job = session.get(MemoirAudioJob, reserved.id)
        assert job is not None
        assert job.state == STATE_SUBMITTED
        assert job.usage_text_words == 10
        assert float(job.settled_cost) == pytest.approx(0.015)  # 10 字 * 1.5/1000


def test_music_submission_requires_task_id(session_factory: sessionmaker[Session]) -> None:
    """音乐是异步任务：submitted 必须携带 TaskID，否则拒绝写入。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(_music_reservation("run-1", "hmac-bgm")).job
        service.mark_submitting(reserved.job_id, reserved.lease_token)
        with pytest.raises(MemoirAudioJobsError) as err:
            service.mark_submitted(reserved.job_id, reserved.lease_token)
        assert err.value.code == "MEMOIR_AUDIO_INPUT_INVALID"


def test_object_key_fixed_before_upload_and_immutable(
    session_factory: sessionmaker[Session],
) -> None:
    """对象键上传前固定：同键幂等、异键冲突；未固定不得记上传。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a")).job
        token = reserved.lease_token
        key = "memoir-test/audios/narrator/scope/nar-abc.mp3"

        with pytest.raises(MemoirAudioJobsError) as err:
            service.record_upload(reserved.job_id, token, duration_ms=1_000)
        assert err.value.code == "MEMOIR_AUDIO_INPUT_INVALID"

        service.record_object_key(reserved.job_id, token, key, "audio/mpeg")
        service.record_object_key(reserved.job_id, token, key, "audio/mpeg")  # 幂等
        with pytest.raises(MemoirAudioJobsError) as err:
            service.record_object_key(reserved.job_id, token, key + "x", "audio/mpeg")
        assert err.value.code == "MEMOIR_AUDIO_OBJECT_KEY_CONFLICT"
        service.record_upload(reserved.job_id, token, duration_ms=1_000)
        job = session.get(MemoirAudioJob, reserved.id)
        assert job is not None
        assert job.object_key == key and job.state == STATE_UPLOADED


# ---------------------------------------------------------------------------
# Step 3：门禁、fencing 与恢复
# ---------------------------------------------------------------------------


def test_cancelled_run_blocks_reserve_and_writes(
    session_factory: sessionmaker[Session],
) -> None:
    """Run 取消后：新预留与状态写入都被拒绝。"""
    with session_factory() as session:
        run = _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a")).job
        token = reserved.lease_token

        run.cancel_requested_at = datetime.now(UTC)
        session.commit()

        with pytest.raises(MemoirAudioJobsError) as err:
            service.reserve_job(_narration_reservation("run-1", "scene-2", "hmac-b"))
        assert err.value.code == "MEMOIR_AUDIO_RUN_CANCELLED"
        with pytest.raises(MemoirAudioJobsError) as err:
            service.mark_submitting(reserved.job_id, token)
        assert err.value.code == "MEMOIR_AUDIO_RUN_CANCELLED"


def test_privacy_purge_blocks_writes(session_factory: sessionmaker[Session]) -> None:
    """隐私清理中的 Run：音频作业不可再写。"""
    with session_factory() as session:
        run = _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a")).job

        run.privacy_state = "purge_requested"
        session.commit()
        with pytest.raises(MemoirAudioJobsError) as err:
            service.mark_submitting(reserved.job_id, reserved.lease_token)
        assert err.value.code == "MEMOIR_AUDIO_RUN_PRIVACY_BLOCKED"


def test_stale_fencing_token_cannot_write_new_attempt(
    session_factory: sessionmaker[Session],
) -> None:
    """失败重试后 token 递增：旧 owner 的旧 token 不能再写结果。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a")).job
        old_token = reserved.lease_token
        service.mark_failed(reserved.job_id, old_token, error_code="AUDIO_PROVIDER_FAILED")

        retried = service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a"))
        assert retried.outcome == "retried"
        assert retried.job.attempt == 2
        assert retried.job.lease_token == old_token + 1

        with pytest.raises(MemoirAudioJobsError) as err:
            service.mark_submitting(reserved.job_id, old_token)
        assert err.value.code == "MEMOIR_AUDIO_FENCING_REJECTED"


def test_expired_lease_rejected_until_renewed(session_factory: sessionmaker[Session]) -> None:
    """过期 lease 拒写；同 token 心跳续租后恢复可写。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(
            _narration_reservation("run-1", "scene-1", "hmac-a", lease_ttl_seconds=1.0)
        ).job
        token = reserved.lease_token
        now_after_expiry = datetime.now(UTC) + timedelta(seconds=5)

        with pytest.raises(MemoirAudioJobsError) as err:
            service.mark_submitting(reserved.job_id, token, now=now_after_expiry)
        assert err.value.code == "MEMOIR_AUDIO_LEASE_EXPIRED"

        service.renew_lease(reserved.job_id, token, ttl_seconds=60.0, now=now_after_expiry)
        service.mark_submitting(reserved.job_id, token, now=now_after_expiry)
        job = session.get(MemoirAudioJob, reserved.id)
        assert job is not None and job.state == "submitting"


def test_rotate_lease_fences_old_session_writes(session_factory: sessionmaker[Session]) -> None:
    """R4 恢复接管：旋转 token 后旧 token 写入全被拒，新 token 可写。

    模拟跨 Session 恢复在途作业：新 Session rotate 后，上一 Session 持旧
    token 的迟到 renew/mark 必须被 fencing 拒绝（零新增上传/账本写入）。
    """
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(
            _narration_reservation("run-1", "scene-1", "hmac-a", lease_ttl_seconds=1.0)
        ).job
        old_token = reserved.lease_token
        # lease 已过期（上一 Session 崩溃）：旋转不因过期被拒（接管场景）。
        now_after_expiry = datetime.now(UTC) + timedelta(seconds=5)

        rotated = service.rotate_lease(
            reserved.job_id, old_token,
            owner="worker-b", ttl_seconds=60.0, now=now_after_expiry,
        )
        new_token = rotated.lease_token
        assert new_token == old_token + 1
        assert rotated.lease_owner == "worker-b"
        assert rotated.expires_at is not None

        # 旧 token 的迟到续约与写入必须被 fencing 拒绝。
        with pytest.raises(MemoirAudioJobsError) as err:
            service.renew_lease(reserved.job_id, old_token, now=now_after_expiry)
        assert err.value.code == "MEMOIR_AUDIO_FENCING_REJECTED"
        with pytest.raises(MemoirAudioJobsError) as err:
            service.mark_submitting(reserved.job_id, old_token, now=now_after_expiry)
        assert err.value.code == "MEMOIR_AUDIO_FENCING_REJECTED"

        # 新 token 续约与状态写入正常放行。
        service.renew_lease(reserved.job_id, new_token, ttl_seconds=60.0,
                            now=now_after_expiry)
        service.mark_submitting(reserved.job_id, new_token, now=now_after_expiry)
        job = session.get(MemoirAudioJob, reserved.id)
        assert job is not None and job.state == "submitting"


def test_rotate_lease_rejects_wrong_expected_token(
    session_factory: sessionmaker[Session],
) -> None:
    """R4：expected_token 不匹配（他人已接管/陈旧视图）抛既有 fencing 冲突。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a")).job
        stale_view_token = reserved.lease_token - 1  # 陈旧视图中的旧值

        with pytest.raises(MemoirAudioJobsError) as err:
            service.rotate_lease(
                reserved.job_id, stale_view_token,
                owner="worker-b", ttl_seconds=60.0,
            )
        assert err.value.code == "MEMOIR_AUDIO_FENCING_REJECTED"


# ---------------------------------------------------------------------------
# C1：fail_abandoned_keyed_jobs（持键过窗收割，不扩 _ORPHAN_STATES）
# ---------------------------------------------------------------------------


def test_fail_abandoned_keyed_jobs_reaps_expired_keyed_active(
    session_factory: sessionmaker[Session],
) -> None:
    """过窗持键 active → failed + AUDIO_LEASE_ABANDONED。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(
            _narration_reservation("run-1", "scene-1", "hmac-a")
        ).job
        service.record_object_key(
            reserved.job_id, reserved.lease_token,
            "memoir-test/abandoned.mp3", "audio/mpeg",
        )
        now = datetime.now(UTC)
        reserved.expires_at = now - timedelta(hours=25)
        session.flush()
        age_before = reserved.updated_at

        count = service.fail_abandoned_keyed_jobs(now=now, grace_seconds=24 * 3600)
        assert count == 1
        job = session.get(MemoirAudioJob, reserved.id)
        assert job is not None
        assert job.state == STATE_FAILED
        assert job.error_code == "AUDIO_LEASE_ABANDONED"
        assert job.updated_at == age_before


def test_fail_abandoned_keyed_jobs_skips_fresh_or_in_grace(
    session_factory: sessionmaker[Session],
) -> None:
    """lease 未过期、或过期但仍在 grace 内 → 不动。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        now = datetime.now(UTC)
        alive = service.reserve_job(
            _narration_reservation("run-1", "scene-1", "hmac-alive", job_id="job-alive")
        ).job
        service.record_object_key(
            alive.job_id, alive.lease_token, "memoir-test/alive.mp3", "audio/mpeg",
        )
        graced = service.reserve_job(
            _narration_reservation("run-1", "scene-2", "hmac-grace", job_id="job-grace")
        ).job
        service.record_object_key(
            graced.job_id, graced.lease_token, "memoir-test/grace.mp3", "audio/mpeg",
        )
        # 过期 1 小时，grace=24h → 仍在保留窗内，不得收割。
        graced.expires_at = now - timedelta(hours=1)
        session.flush()

        count = service.fail_abandoned_keyed_jobs(now=now, grace_seconds=24 * 3600)
        assert count == 0
        assert session.get(MemoirAudioJob, alive.id).state == STATE_RESERVED
        assert session.get(MemoirAudioJob, graced.id).state == STATE_RESERVED


def test_fail_abandoned_keyed_jobs_never_reaps_submission_unknown(
    session_factory: sessionmaker[Session],
) -> None:
    """submission_unknown 即使持键且过窗也永不 reap（费用未知语义）。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(
            _narration_reservation("run-1", "scene-1", "hmac-a")
        ).job
        token = reserved.lease_token
        service.record_object_key(
            reserved.job_id, token, "memoir-test/unknown.mp3", "audio/mpeg",
        )
        service.mark_submitting(reserved.job_id, token)
        service.mark_submission_unknown(
            reserved.job_id, token, error_code="TTS_UNKNOWN_ERROR",
        )
        now = datetime.now(UTC)
        reserved.expires_at = now - timedelta(hours=25)
        session.flush()

        count = service.fail_abandoned_keyed_jobs(now=now, grace_seconds=24 * 3600)
        assert count == 0
        job = session.get(MemoirAudioJob, reserved.id)
        assert job is not None
        assert job.state == STATE_SUBMISSION_UNKNOWN
        assert job.error_code == "TTS_UNKNOWN_ERROR"


def test_fail_abandoned_keyed_jobs_skips_unkeyed_active(
    session_factory: sessionmaker[Session],
) -> None:
    """不持键 active 不 reap（无对象可清；BGM 未持键=未结算）。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(
            _narration_reservation("run-1", "scene-1", "hmac-a")
        ).job
        now = datetime.now(UTC)
        reserved.expires_at = now - timedelta(hours=25)
        session.flush()
        assert reserved.object_key is None

        count = service.fail_abandoned_keyed_jobs(now=now, grace_seconds=24 * 3600)
        assert count == 0
        assert session.get(MemoirAudioJob, reserved.id).state == STATE_RESERVED


def test_fail_abandoned_keyed_jobs_misses_after_rotate_lease(
    session_factory: sessionmaker[Session],
) -> None:
    """先 rotate_lease 再 reap：expires_at 已刷新，条件 UPDATE 不命中。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(
            _narration_reservation("run-1", "scene-1", "hmac-a", lease_ttl_seconds=1.0)
        ).job
        old_token = reserved.lease_token
        service.record_object_key(
            reserved.job_id, old_token, "memoir-test/rotated.mp3", "audio/mpeg",
        )
        now = datetime.now(UTC) + timedelta(seconds=5)
        service.rotate_lease(
            reserved.job_id, old_token,
            owner="worker-b", ttl_seconds=60.0, now=now,
        )

        count = service.fail_abandoned_keyed_jobs(now=now, grace_seconds=24 * 3600)
        assert count == 0
        job = session.get(MemoirAudioJob, reserved.id)
        assert job is not None
        assert job.state == STATE_RESERVED
        assert job.lease_token == old_token + 1


# ---------------------------------------------------------------------------
# R4：跨 Session fencing（文件 SQLite + NullPool，每 Session 独立连接）
# ---------------------------------------------------------------------------


@pytest.fixture
def cross_session_factory(tmp_path: Path) -> sessionmaker[Session]:
    """R4 跨 Session 用例专用：文件 SQLite + NullPool。

    内存 SQLite 默认同线程共享单连接，无法构造真实跨 Session 隔离；
    文件库 + NullPool 让每个 Session 独立连接，"旧 ORM 引用 vs 已提交
    新值"的竞争才能被真实还原。不连任何真实数据库。
    """
    import app.models  # noqa: F401 确保全部模型注册进 Base.metadata
    from app.db.sqlalchemy_db import Base

    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'memoir-audio-r4-cross.db'}",
        poolclass=NullPool,
        connect_args={"timeout": 15},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_r4_stale_session_reference_cannot_write_after_rotate_cross_session(
    cross_session_factory: sessionmaker[Session],
) -> None:
    """R4：A Session 持旧 ORM 引用（token=1），B Session 旋转到 token=2 并
    提交后，A 的迟到 mark/record_upload 必须被 fencing 拒绝，且 B 已提交
    的权威账本状态不被 A 污染。

    这是 populate_existing 当前读的直接回归测试：没有它，A 的 SELECT 会
    命中身份映射中的旧对象（token 仍显示 1），校验被绕过。
    """
    key = "memoir-test/audios/narrator/scope/nar-a.mp3"
    with cross_session_factory() as session_a:
        _make_run(session_a, "run-r4a")
        service_a = MemoirAudioJobsService(session_a, _policy())
        reserved = service_a.reserve_job(
            _narration_reservation("run-r4a", "scene-1", "hmac-a")
        ).job
        service_a.record_object_key(reserved.job_id, reserved.lease_token, key, "audio/mpeg")
        session_a.commit()  # 结束读事务并保留旧 ORM 引用（token 仍为 1）
        # rollback 会让 Session expire 全部实例：标识符先落到本地变量。
        job_id, old_token = reserved.job_id, reserved.lease_token

        with cross_session_factory() as session_b:
            service_b = MemoirAudioJobsService(session_b, _policy())
            rotated = service_b.rotate_lease(
                job_id, old_token, owner="worker-b", ttl_seconds=60.0,
            )
            assert rotated.lease_token == 2
            session_b.commit()

        # A 持旧 token 的迟到写入必须被拒（当前读看到已提交的 token=2）。
        with pytest.raises(MemoirAudioJobsError) as err:
            service_a.mark_processing(job_id, old_token)
        assert err.value.code == "MEMOIR_AUDIO_FENCING_REJECTED"
        session_a.rollback()
        with pytest.raises(MemoirAudioJobsError) as err:
            service_a.record_upload(job_id, old_token, duration_ms=1_000)
        assert err.value.code == "MEMOIR_AUDIO_FENCING_REJECTED"
        session_a.rollback()

    # 第三只 Session 验证权威状态：B 的旋转成果完好，A 未写入任何字段。
    with cross_session_factory() as probe:
        job = probe.scalar(
            sa.select(MemoirAudioJob).where(MemoirAudioJob.job_id == job_id)
        )
        assert job is not None
        assert job.lease_token == 2
        assert job.lease_owner == "worker-b"
        assert job.state == "reserved"
        assert job.object_key == key


def test_r4_run_attempt_takeover_fences_old_attempt_writes(
    cross_session_factory: sessionmaker[Session],
) -> None:
    """R4：Run 被接管（execution_attempt 1→2）但作业 token 未动——旧
    attempt 的 worker 即使持正确 token，也必须被 Run 执行尝试 fence
    拒绝；新 attempt 的 worker 可旋转接管后继续写入。"""
    run_id = "run-r4b"
    with cross_session_factory() as session_a:
        run = _make_run(session_a, run_id)
        run.execution_attempt = 1
        session_a.commit()
        service_a = MemoirAudioJobsService(session_a, _policy())
        reserved = service_a.reserve_job(
            _narration_reservation(
                run_id, "scene-1", "hmac-a", lease_owner=f"audio:{run_id}:attempt-1"
            )
        ).job
        session_a.commit()
        # 同上：rollback 会 expire 实例，标识符提前落本地变量。
        job_id, old_token, job_pk = reserved.job_id, reserved.lease_token, reserved.id

        # 另一 Session 将 Run 升级到第 2 次执行尝试（作业行未动）。
        with cross_session_factory() as session_b:
            run_b = session_b.scalar(sa.select(AgentRun).where(AgentRun.run_id == run_id))
            assert run_b is not None
            run_b.execution_attempt = 2
            session_b.commit()

        # 正确 token 也被拒：A 的 lease_owner 是旧 attempt 身份。
        with pytest.raises(MemoirAudioJobsError) as err:
            service_a.mark_submitting(job_id, old_token)
        assert err.value.code == "MEMOIR_AUDIO_FENCING_REJECTED"
        session_a.rollback()
        # 旧 attempt 的 rotate（传入 owner 也是旧尝试）同样被拒。
        with pytest.raises(MemoirAudioJobsError) as err:
            service_a.rotate_lease(
                job_id, old_token,
                owner=f"audio:{run_id}:attempt-1", ttl_seconds=60.0,
            )
        assert err.value.code == "MEMOIR_AUDIO_FENCING_REJECTED"
        session_a.rollback()

    # 新 attempt 的 worker 旋转接管后可正常写入。
    with cross_session_factory() as session_c:
        service_c = MemoirAudioJobsService(session_c, _policy())
        rotated = service_c.rotate_lease(
            job_id, old_token,
            owner=f"audio:{run_id}:attempt-2", ttl_seconds=60.0,
        )
        service_c.mark_submitting(job_id, rotated.lease_token)
        session_c.commit()
        job = session_c.get(MemoirAudioJob, job_pk)
        assert job is not None
        assert job.state == "submitting"
        assert job.lease_owner == f"audio:{run_id}:attempt-2"


def test_r4_concurrent_rotate_same_expected_token_exactly_one_wins(
    cross_session_factory: sessionmaker[Session],
) -> None:
    """R4：两 Session 以同一 expected_token 旋转，只有一方成功
    （rowcount=1）；迟到方无论在校验读还是 CAS 行数判定处都被
    MEMOIR_AUDIO_FENCING_REJECTED 拒绝，绝不出现双旋转。"""
    with cross_session_factory() as setup:
        _make_run(setup, "run-r4c")
        service = MemoirAudioJobsService(setup, _policy())
        reserved = service.reserve_job(
            _narration_reservation("run-r4c", "scene-1", "hmac-a")
        ).job
        setup.commit()
        job_id, expected_token = reserved.job_id, reserved.lease_token

    # 第一方完成完整旋转并提交。
    with cross_session_factory() as session_winner:
        MemoirAudioJobsService(session_winner, _policy()).rotate_lease(
            job_id, expected_token, owner="worker-1", ttl_seconds=60.0
        )
        session_winner.commit()

    # 第二方持同一 expected_token 迟到旋转：token 已变 → 拒绝。
    with cross_session_factory() as session_loser:
        with pytest.raises(MemoirAudioJobsError) as err:
            MemoirAudioJobsService(session_loser, _policy()).rotate_lease(
                job_id, expected_token, owner="worker-2", ttl_seconds=60.0
            )
        assert err.value.code == "MEMOIR_AUDIO_FENCING_REJECTED"
        session_loser.rollback()

    with cross_session_factory() as probe:
        job = probe.scalar(
            sa.select(MemoirAudioJob).where(MemoirAudioJob.job_id == job_id)
        )
        assert job is not None
        assert job.lease_token == expected_token + 1  # 恰好旋转一次
        assert job.lease_owner == "worker-1"

    # CAS rowcount 兜底路径：即便某 Session 持旋转前的加载结果直接发
    # UPDATE（绕过校验读），WHERE token==expected 匹配 0 行仍被拒。
    with cross_session_factory() as session_stale:
        service_stale = MemoirAudioJobsService(session_stale, _policy())
        stale_job = service_stale.find_job(
            run_id="run-r4c",
            generation_epoch=3,
            package_version="1.0.8",
            role=ROLE_NARRATION,
            scene_id="scene-1",
            input_hmac="hmac-a",
        )
        assert stale_job is not None
        token_seen = stale_job.lease_token
        session_stale.commit()  # 结束读事务，放行下一 Session 的写锁

        with cross_session_factory() as session_again:
            MemoirAudioJobsService(session_again, _policy()).rotate_lease(
                job_id, token_seen, owner="worker-3", ttl_seconds=60.0
            )
            session_again.commit()

        with pytest.raises(MemoirAudioJobsError) as err:
            service_stale._cas_update(  # noqa: SLF001 直接探测 CAS 兜底
                stale_job,
                token_seen,
                set(_ACTIVE_STATES),
                {"state": "submitting"},
            )
        assert err.value.code == "MEMOIR_AUDIO_FENCING_REJECTED"
        session_stale.rollback()


def test_submission_unknown_is_terminal_no_recharge_no_retry(
    session_factory: sessionmaker[Session],
) -> None:
    """submission_unknown：终态、不自动重提、不释放预算、不可结算。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a")).job
        token = reserved.lease_token
        service.mark_submitting(reserved.job_id, token)
        service.mark_submission_unknown(
            reserved.job_id, token, error_code="AUDIO_SUBMIT_NETWORK_UNKNOWN"
        )

        with pytest.raises(MemoirAudioJobsError) as err:
            service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a"))
        assert err.value.code == "MEMOIR_AUDIO_SUBMISSION_UNKNOWN"
        with pytest.raises(MemoirAudioJobsError) as err:
            service.settle_tts_usage(reserved.job_id, token, usage_text_words=10)
        assert err.value.code == "MEMOIR_AUDIO_JOB_STATE_INVALID"

        session.commit()
        budget = session.scalar(sa.select(MemoirAudioRunBudget))
        assert budget is not None
        # 未知结果不释放预留：预算仍占用 0.15。
        assert float(budget.reserved_total_cost) == pytest.approx(0.15)
        job = session.get(MemoirAudioJob, reserved.id)
        assert job is not None
        assert job.state == STATE_SUBMISSION_UNKNOWN
        assert job.settled_cost is None  # 缺用量：pending reconciliation，不能视作 0


def test_failed_retry_re_reserves_and_keeps_object_key(
    session_factory: sessionmaker[Session],
) -> None:
    """失败重试重新预留费用；对象键保持不变，恢复可对账同一对象。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a")).job
        token = reserved.lease_token
        key = "memoir-test/audios/narrator/scope/nar-abc.mp3"
        service.record_object_key(reserved.job_id, token, key, "audio/mpeg")
        service.mark_failed(reserved.job_id, token, error_code="AUDIO_UPLOAD_FAILED")

        retried = service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a"))
        session.commit()
        job = session.get(MemoirAudioJob, reserved.id)
        assert job is not None
        assert job.object_key == key  # 不因重试产生新孤儿键
        assert float(job.reserved_cost) == pytest.approx(0.30)  # 两次尝试累计预留
        budget = session.scalar(sa.select(MemoirAudioRunBudget))
        assert budget is not None
        assert float(budget.reserved_total_cost) == pytest.approx(0.30)
        assert retried.job.state == "reserved"


def test_uploaded_asset_reuse_is_not_recharged(
    session_factory: sessionmaker[Session],
) -> None:
    """同 Run 同输入的已成功资产：复用不重复扣费。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a")).job
        token = reserved.lease_token
        service.record_object_key(
            reserved.job_id,
            token,
            "memoir-test/audios/narrator/scope/nar-abc.mp3",
            "audio/mpeg",
        )
        service.record_upload(reserved.job_id, token, duration_ms=2_000)

        reused = service.reserve_job(_narration_reservation("run-1", "scene-1", "hmac-a"))
        assert reused.outcome == "reused"
        assert reused.job.id == reserved.id
        session.commit()
        budget = session.scalar(sa.select(MemoirAudioRunBudget))
        assert budget is not None
        assert float(budget.reserved_total_cost) == pytest.approx(0.15)

        asset = service.find_reusable_uploaded_asset(
            run_id="run-1",
            generation_epoch=3,
            package_version="1.0.8",
            role=ROLE_NARRATION,
            scene_id="scene-1",
            input_hmac="hmac-a",
        )
        assert asset is not None and asset.state == STATE_UPLOADED


def test_known_music_task_is_queried_not_recreated(
    session_factory: sessionmaker[Session],
) -> None:
    """已知 TaskID 只查不重建：submitted/processing 的音乐槽返回原任务。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(_music_reservation("run-1", "hmac-bgm")).job
        token = reserved.lease_token
        service.mark_submitting(reserved.job_id, token)
        service.mark_submitted(reserved.job_id, token, provider_task_id="task-42")

        pending = service.find_pending_music_task(
            run_id="run-1",
            generation_epoch=3,
            package_version="1.0.8",
            input_hmac="hmac-bgm",
        )
        assert pending is not None and pending.provider_task_id == "task-42"

        # 槽位仍在途：不得再抢。
        with pytest.raises(MemoirAudioJobsError) as err:
            service.reserve_job(_music_reservation("run-1", "hmac-bgm"))
        assert err.value.code == "MEMOIR_AUDIO_JOB_SLOT_ACTIVE"


# ---------------------------------------------------------------------------
# M8 默认配乐（freeze 2026-09-11 §4）：零费槽、零费结算、hmac-less 互斥查询
# ---------------------------------------------------------------------------


def test_default_music_zero_fee_slot_reserves_without_budget(
    session_factory: sessionmaker[Session],
) -> None:
    """零费 BGM 预留成功：不占预算（不建预算行）、不携带请求秒数。"""
    with session_factory() as session:
        _make_run(session, "run-d1")
        service = MemoirAudioJobsService(session, _policy())
        outcome = service.reserve_job(_default_music_reservation("run-d1", "hmac-default"))
        assert outcome.outcome == "created"
        job = outcome.job
        assert job.state == STATE_RESERVED
        assert job.role == ROLE_BACKGROUND_MUSIC
        assert job.requested_music_seconds is None
        assert float(job.reserved_cost) == 0.0
        session.commit()
        # 零费槽不碰预算行：既不首建也不扣减。
        assert session.scalar(sa.select(MemoirAudioRunBudget)) is None


def test_default_music_zero_fee_slot_rejects_seconds(
    session_factory: sessionmaker[Session],
) -> None:
    """零费槽携带请求秒数（即使合法 int）一律拒绝。"""
    with session_factory() as session:
        _make_run(session, "run-d1")
        service = MemoirAudioJobsService(session, _policy())
        with pytest.raises(MemoirAudioJobsError) as err:
            service.reserve_job(
                _default_music_reservation(
                    "run-d1", "hmac-default", requested_music_seconds=60
                )
            )
        assert err.value.code == "MEMOIR_AUDIO_INPUT_INVALID"


def test_paid_music_without_seconds_still_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    """付费 BGM（estimated_cost > 0）缺秒数仍被拒：现有付费语义保留。"""
    with session_factory() as session:
        _make_run(session, "run-d1")
        service = MemoirAudioJobsService(session, _policy())
        with pytest.raises(MemoirAudioJobsError) as err:
            service.reserve_job(
                _default_music_reservation(
                    "run-d1", "hmac-paid", estimated_cost=estimate_music_cost(60, _policy())
                )
            )
        assert err.value.code == "MEMOIR_AUDIO_INPUT_INVALID"


def test_settle_default_music_usage_zero_fee_settlement(
    session_factory: sessionmaker[Session],
) -> None:
    """零费结算：reserved 态写 settled_cost=0；uploaded 态幂等；不触碰秒数/TaskID。"""
    with session_factory() as session:
        _make_run(session, "run-d2")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(_default_music_reservation("run-d2", "hmac-default")).job
        token = reserved.lease_token

        settled = service.settle_default_music_usage(reserved.job_id, token)
        assert float(settled.settled_cost) == 0.0
        assert settled.state == STATE_RESERVED  # 零费结算不迁移状态
        assert settled.requested_music_seconds is None
        assert settled.provider_task_id is None

        # 默认路径状态流：持键 → 上传 → uploaded 态幂等再结算。
        service.record_object_key(
            reserved.job_id, token,
            "memoir-test/audios/background/scope/bgm-abc.mp3", "audio/mpeg",
        )
        service.record_upload(reserved.job_id, token, duration_ms=61_234)
        again = service.settle_default_music_usage(reserved.job_id, token)
        assert float(again.settled_cost) == 0.0
        assert again.state == STATE_UPLOADED
        assert again.requested_music_seconds is None
        assert again.provider_task_id is None
        session.commit()
        job = session.get(MemoirAudioJob, reserved.id)
        assert job is not None
        assert job.settled_cost == Decimal("0")


def test_settle_default_music_usage_refuses_paid_settlement(
    session_factory: sessionmaker[Session],
) -> None:
    """settled_cost 已为非 0 金额（付费结算）→ 拒绝零费覆盖。"""
    with session_factory() as session:
        _make_run(session, "run-d3")
        service = MemoirAudioJobsService(session, _policy())
        # 付费 BGM 全链路走到 uploaded 且已按 60 秒结算 3.0 元。
        reserved = service.reserve_job(_music_reservation("run-d3", "hmac-paid")).job
        token = reserved.lease_token
        service.mark_submitting(reserved.job_id, token)
        service.mark_submitted(reserved.job_id, token, provider_task_id="task-1")
        service.settle_music_usage(reserved.job_id, token, requested_music_seconds=60)
        service.mark_processing(reserved.job_id, token)
        service.record_object_key(
            reserved.job_id, token,
            "memoir-test/audios/background/scope/bgm-paid.mp3", "audio/mpeg",
        )
        service.record_upload(reserved.job_id, token, duration_ms=60_000)
        job = session.get(MemoirAudioJob, reserved.id)
        assert job is not None and job.state == STATE_UPLOADED
        assert float(job.settled_cost) == pytest.approx(3.0)

        with pytest.raises(MemoirAudioJobsError) as err:
            service.settle_default_music_usage(reserved.job_id, token)
        assert err.value.code == "MEMOIR_AUDIO_SETTLE_CONFLICT"
        # 拒绝后付费结算金额原样保留。
        assert float(session.get(MemoirAudioJob, reserved.id).settled_cost) == pytest.approx(3.0)


def test_list_work_background_music_jobs_is_hmac_less(
    session_factory: sessionmaker[Session],
) -> None:
    """同 Run 不同 hmac 的 BGM 行都能列出（hmac 不参与互斥查询）。

    S2（2026-09-14 冻结裁决）后 reserve_job 拒绝新增不同 hmac 的作品级
    行，但查询必须保持 hmac-less：它同时服务服务层咨询性预检与
    reserve_job 权威守卫，且存量历史数据（S2 之前建立的双行形态）仍须
    全量可见，供守卫判定与维护对账。第二条（付费 hmac）行因此直接造行
    模拟存量数据，绕过已被守卫收口的预留路径。
    """
    with session_factory() as session:
        _make_run(session, "run-d4")
        service = MemoirAudioJobsService(session, _policy())
        default = service.reserve_job(
            _default_music_reservation("run-d4", "hmac-default")
        ).job
        # 存量付费行（S2 前的历史形态）：直接造行，不经 reserve_job。
        paid = MemoirAudioJob(
            job_id="job-bgm-legacy-paid",
            business_id="biz-1",
            run_id="run-d4",
            generation_epoch=3,
            package_version="1.0.8",
            role=ROLE_BACKGROUND_MUSIC,
            scene_id=WORK_SCENE_ID,
            segment_index=NO_SEGMENT_INDEX,
            input_hmac="hmac-paid",
            attempt=1,
            state=STATE_RESERVED,
            reserved_cost=Decimal("3.0"),
            currency="CNY",
            requested_music_seconds=60,
            lease_owner="worker-a",
            lease_token=1,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(paid)
        # 干扰项：旁白作业不得混入作品级 BGM 互斥查询结果。
        service.reserve_job(_narration_reservation("run-d4", "scene-1", "hmac-nar"))
        session.commit()

        jobs = service.list_work_background_music_jobs("run-d4", 3, "1.0.8")
        assert {job.job_id for job in jobs} == {default.job_id, paid.job_id}
        # 不同 epoch / 包版本不串台；无 BGM 的口径返回空列表。
        assert service.list_work_background_music_jobs("run-d4", 4, "1.0.8") == []


def test_list_work_background_music_jobs_with_budget_row_keeps_it_untouched(
    session_factory: sessionmaker[Session],
) -> None:
    """已有预算行时查询为纯读：不创建、不改动、不锁定预算行。

    S2（2026-09-14 冻结裁决）删除了原"预算行先 FOR UPDATE 再查"的预锁
    （零费默认模式不建预算行，该锁对首建竞争无效且构成 AB-BA 死锁对）；
    本用例钉住删除后的语义：查询前后预算行原样。
    """
    with session_factory() as session:
        _make_run(session, "run-d5")
        service = MemoirAudioJobsService(session, _policy())
        # 付费预留建立预算行（3.0）；查询后预算行原样。
        service.reserve_job(_music_reservation("run-d5", "hmac-paid"))
        jobs = service.list_work_background_music_jobs("run-d5", 3, "1.0.8")
        assert len(jobs) == 1
        session.commit()
        budgets = session.scalars(sa.select(MemoirAudioRunBudget)).all()
        assert len(budgets) == 1
        assert float(budgets[0].reserved_total_cost) == pytest.approx(3.0)


def test_default_music_retry_keeps_requested_music_seconds_none(
    session_factory: sessionmaker[Session],
) -> None:
    """失败重试后零费槽 requested_music_seconds=None 原样存活且可零费结算。"""
    with session_factory() as session:
        _make_run(session, "run-d6")
        service = MemoirAudioJobsService(session, _policy())
        reserved = service.reserve_job(_default_music_reservation("run-d6", "hmac-default")).job
        service.mark_failed(
            reserved.job_id, reserved.lease_token, error_code="AUDIO_UPLOAD_FAILED"
        )

        retried = service.reserve_job(_default_music_reservation("run-d6", "hmac-default"))
        assert retried.outcome == "retried"
        job = session.get(MemoirAudioJob, reserved.id)
        assert job is not None
        assert job.requested_music_seconds is None  # None 必须原样存活
        assert float(job.reserved_cost) == 0.0  # 零费重试不累计费用
        assert session.scalar(sa.select(MemoirAudioRunBudget)) is None

        settled = service.settle_default_music_usage(
            retried.job.job_id, retried.job.lease_token
        )
        assert float(settled.settled_cost) == 0.0


def test_final_scene_assets_survive_peer_failure(
    session_factory: sessionmaker[Session],
) -> None:
    """分段临时资产丢失只降级该场景：其他已成功场景资产保留。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        ok = service.reserve_job(_narration_reservation("run-1", "scene-ok", "hmac-ok")).job
        service.record_object_key(
            ok.job_id,
            ok.lease_token,
            "memoir-test/audios/narrator/scope/nar-ok.mp3",
            "audio/mpeg",
        )
        service.record_upload(ok.job_id, ok.lease_token, duration_ms=3_000)

        broken = service.reserve_job(
            _narration_reservation("run-1", "scene-bad", "hmac-bad")
        ).job
        # 临时分段丢失：该场景资源失败降级，不影响 scene-ok 的 uploaded 资产。
        service.mark_failed(
            broken.job_id, broken.lease_token, error_code="AUDIO_SEGMENT_TEMP_ASSET_LOST"
        )
        session.commit()
        kept = service.find_reusable_uploaded_asset(
            run_id="run-1",
            generation_epoch=3,
            package_version="1.0.8",
            role=ROLE_NARRATION,
            scene_id="scene-ok",
            input_hmac="hmac-ok",
        )
        assert kept is not None and kept.state == STATE_UPLOADED


# ---------------------------------------------------------------------------
# 保守估算与敏感字段边界
# ---------------------------------------------------------------------------


def test_cost_estimates_round_up_conservatively() -> None:
    """预留必须按完整输入向上取整，不允许向下抹零。"""
    policy = _policy()
    # 3 字 * 1.5/1000 = 0.0045 → 6 位上界 0.004500；1001 字 → 1.5015 已是
    # 4 位小数，6 位精度向上取整仍为 1.501500（量化粒度未到进位门槛）。
    assert estimate_tts_cost("三个字", policy) == Decimal("0.004500")
    assert estimate_tts_cost("字" * 1001, policy) == Decimal("1.501500")
    assert estimate_music_cost(60, policy) == Decimal("3.000000")
    with pytest.raises(MemoirAudioJobsError):
        estimate_tts_cost("", policy)


def test_ledger_schema_holds_metadata_only() -> None:
    """账本列集合白名单：不允许出现正文/URL/prompt/字节类字段。"""
    columns = set(MemoirAudioJob.__table__.columns.keys())
    assert columns == {
        "id", "job_id", "business_id", "run_id", "generation_epoch", "package_version",
        "role", "scene_id", "segment_index", "input_hmac", "attempt", "state",
        "provider_task_id", "object_key", "mime", "duration_ms", "reserved_cost",
        "settled_cost", "currency", "usage_text_words", "requested_music_seconds",
        "lease_owner", "lease_token", "expires_at", "error_code", "created_at", "updated_at",
    }
    # 禁词不含 "text"：usage_text_words 是字数计数，不是正文本身。
    for banned in ("body", "prompt", "url", "audio", "bytes", "payload", "content"):
        assert all(banned not in name for name in columns), banned


def test_work_scene_sentinel_participates_in_unique_slot(
    session_factory: sessionmaker[Session],
) -> None:
    """BGM 使用显式哨兵 __work__/-1 参与唯一键，不依赖 NULL 相等语义。"""
    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session, _policy())
        bgm = service.reserve_job(_music_reservation("run-1", "hmac-bgm")).job
        assert bgm.scene_id == WORK_SCENE_ID
        assert bgm.segment_index == NO_SEGMENT_INDEX
        # 同 run 同正文同分段序号的 segment 与 narration 不冲突（role 不同）。
        service.reserve_job(
            _narration_reservation(
                "run-1", "scene-1", "hmac-bgm", job_id="job-nar-1", role=ROLE_NARRATION
            )
        )
        session.commit()
        assert len(session.scalars(sa.select(MemoirAudioJob)).all()) == 2


# ---------------------------------------------------------------------------
# Step 4：维护 CLI
# ---------------------------------------------------------------------------


def _seed_orphan(
    session: Session,
    service: MemoirAudioJobsService,
    *,
    run_id: str,
    scene_id: str,
    input_hmac: str,
    key: str,
    state: str,
    age_hours: int = 30,
    settled: Decimal | None = None,
    reserved: Decimal = Decimal("0.15"),
    lease_active: bool = False,
    role: str = ROLE_NARRATION,
) -> MemoirAudioJob:
    """直接在库中造出维护命令要扫描的作业行（绕过服务以构造终态）。

    state 在构造时直接给终值：不得先插入再用 UPDATE 改状态——
    Core UPDATE 会触发 updated_at 的 onupdate，把精心回拨的年龄重置。
    """
    job = MemoirAudioJob(
        job_id=f"job-m-{scene_id}-{input_hmac[:4]}",
        business_id="biz-1",
        run_id=run_id,
        generation_epoch=3,
        package_version="1.0.8",
        role=role,
        scene_id=scene_id,
        segment_index=NO_SEGMENT_INDEX,
        input_hmac=input_hmac,
        attempt=1,
        state=state,
        object_key=key,
        mime="audio/mpeg",
        reserved_cost=reserved,
        settled_cost=settled,
        currency="CNY",
        lease_owner="worker-a",
        lease_token=1,
        expires_at=(
            datetime.now(UTC) + timedelta(hours=1) if lease_active else None
        ),
        updated_at=datetime.now(UTC) - timedelta(hours=age_hours),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    assert service is not None
    return job


class _FakeDeleter:
    """可注入 OSS 删除口：记录键；missing 中的键模拟 404。"""

    def __init__(self, missing: frozenset[str] = frozenset()) -> None:
        self.deleted: list[str] = []
        self.missing = missing

    def delete_object(self, object_key: str) -> bool:
        if object_key in self.missing:
            return False  # 404：对象本就不存在，视为清理成功
        self.deleted.append(object_key)
        return True


class _FakePublishProbe:
    """可注入发布探测口（R5 三态）：按对象键返回 dict/None/抛异常。"""

    def __init__(
        self,
        published: frozenset[str] = frozenset(),
        unpublished: frozenset[str] = frozenset(),
    ) -> None:
        self.published = published
        self.unpublished = unpublished
        self.probed: list[str] = []

    def __call__(self, job: MemoirAudioJob) -> dict[str, Any] | None:
        key = job.object_key or ""
        self.probed.append(key)
        if key in self.published:
            # C1：keep_published 只看成员关系，必须显式带上该键。
            return {"document_id": f"doc-{job.job_id}", "audio_object_keys": [key]}
        if key in self.unpublished:
            return None
        raise RuntimeError("probe unavailable")


def test_maintenance_dry_run_classifies_without_deleting(
    session_factory: sessionmaker[Session],
) -> None:
    """dry-run：探测三态分类只报计数不删除（R5：发布状态以探测口为准）。"""
    from app.scripts.memoir_audio_maintenance import run_maintenance

    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session)
        _seed_orphan(session, service, run_id="run-1", scene_id="s-pub", input_hmac="h1",
                     key="memoir-test/published.mp3", state=STATE_UPLOADED)
        _seed_orphan(session, service, run_id="run-1", scene_id="s-old", input_hmac="h2",
                     key="memoir-test/old.mp3", state=STATE_UPLOADED)
        _seed_orphan(session, service, run_id="run-1", scene_id="s-unk", input_hmac="h3",
                     key="memoir-test/unknown.mp3", state=STATE_UPLOADED)
        _seed_orphan(session, service, run_id="run-1", scene_id="s-led", input_hmac="h4",
                     key="memoir-test/pending.mp3", state=STATE_SUBMISSION_UNKNOWN)
        _seed_orphan(session, service, run_id="run-1", scene_id="s-fly", input_hmac="h5",
                     key="memoir-test/inflight.mp3", state=STATE_UPLOADED, lease_active=True)
        _seed_orphan(session, service, run_id="run-1", scene_id="s-new", input_hmac="h6",
                     key="memoir-test/fresh.mp3", state=STATE_UPLOADED, age_hours=1)

        report = run_maintenance(
            session,
            oss_deleter=_FakeDeleter(),
            retention_hours=24,
            limit=100,
            execute=False,
            publish_probe=_FakePublishProbe(
                published=frozenset({"memoir-test/published.mp3"}),
                unpublished=frozenset({"memoir-test/old.mp3"}),
            ),
        )
        assert report.keep_published == 1
        assert report.keep_unknown == 1  # unknown.mp3 探测抛异常 → 未知保留
        assert report.keep_ledger_pending == 1
        assert report.keep_in_flight == 1
        assert report.keep_within_retention == 1
        assert report.delete_candidates == 1  # old.mp3 明确未发布且超窗（dry-run 不删）
        assert report.deleted == 0
        states = {
            job.job_id: job.state
            for job in session.scalars(sa.select(MemoirAudioJob)).all()
        }
        assert all(state != STATE_CLEANED for state in states.values())


def test_maintenance_execute_deletes_only_stale_unpublished(
    session_factory: sessionmaker[Session],
) -> None:
    """execute：仅探测明确未发布且超保留窗的对象删除；404 视为成功并标记 cleaned。"""
    from app.scripts.memoir_audio_maintenance import run_maintenance

    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session)
        stale = _seed_orphan(session, service, run_id="run-1", scene_id="s-old",
                             input_hmac="h2", key="memoir-test/old.mp3", state=STATE_UPLOADED)
        gone = _seed_orphan(session, service, run_id="run-1", scene_id="s-404",
                            input_hmac="h7", key="memoir-test/gone.mp3", state="failed")

        deleter = _FakeDeleter(missing=frozenset({"memoir-test/gone.mp3"}))
        report = run_maintenance(
            session, oss_deleter=deleter, retention_hours=24, limit=100, execute=True,
            publish_probe=_FakePublishProbe(unpublished=frozenset(
                {"memoir-test/old.mp3", "memoir-test/gone.mp3"}
            )),
        )
        assert report.deleted == 2
        assert deleter.deleted == ["memoir-test/old.mp3"]  # 404 未真正下发删除
        session.refresh(stale)
        session.refresh(gone)
        assert stale.state == STATE_CLEANED
        assert gone.state == STATE_CLEANED


def test_maintenance_respects_limit(session_factory: sessionmaker[Session]) -> None:
    """--limit 限制每批候选数，剩余留给下一批。

    探测口统一返回明确未发布：全部候选都是可删对象，验证 limit 截断。
    """
    from app.scripts.memoir_audio_maintenance import run_maintenance

    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session)
        for index in range(3):
            _seed_orphan(session, service, run_id="run-1", scene_id=f"s-{index}",
                         input_hmac=f"h-{index}", key=f"memoir-test/x-{index}.mp3",
                         state=STATE_UPLOADED)
        report = run_maintenance(
            session, oss_deleter=_FakeDeleter(), retention_hours=24, limit=2, execute=True,
            publish_probe=_FakePublishProbe(unpublished=frozenset(
                {f"memoir-test/x-{index}.mp3" for index in range(3)}
            )),
        )
        assert report.scanned == 2
        assert report.deleted == 2


def test_maintenance_cli_argument_contract() -> None:
    """CLI 参数合同：environment 必选二选一、dry-run/execute 互斥、limit 默认 100。"""

    from app.scripts.memoir_audio_maintenance import build_parser

    parser = build_parser()
    option_strings: set[str] = set()
    for action in parser._actions:  # noqa: SLF001 测试需要检查完整参数面
        option_strings.update(action.option_strings)
    assert option_strings == {
        "-h", "--help", "--environment", "--dry-run", "--execute", "--limit",
    }
    args = parser.parse_args(["--environment", "test"])
    assert args.environment == "test" and args.execute is False and args.limit == 100

    with pytest.raises(SystemExit):
        parser.parse_args(["--environment", "development"])
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["--environment", "test", "--dry-run", "--execute"])


def test_maintenance_main_with_injected_sqlite(session_factory: sessionmaker[Session]) -> None:
    """注入模式下 main 全链路可跑（含探测口透传），返回 0；真实 DB 路径不在测试触达。"""
    from app.scripts import memoir_audio_maintenance as cli

    with session_factory() as session:
        _make_run(session, "run-1")
        service = MemoirAudioJobsService(session)
        _seed_orphan(session, service, run_id="run-1", scene_id="s-old", input_hmac="h2",
                     key="memoir-test/old.mp3", state=STATE_UPLOADED)

    code = cli.main(
        ["--environment", "test", "--execute"],
        session_factory=session_factory,
        oss_deleter=_FakeDeleter(),
        publish_probe=_FakePublishProbe(
            unpublished=frozenset({"memoir-test/old.mp3"})
        ),
    )
    assert code == 0


def test_maintenance_guards_runtime_database_url() -> None:
    """真实运行路径守卫：目标库必须属于 agent_runtime，禁止碰业务库。"""
    from app.scripts.memoir_audio_maintenance import require_runtime_database

    require_runtime_database("mysql+pymysql://u:p@h:3306/couple_diary_agent_runtime_test")
    with pytest.raises(MemoirAudioJobsError):
        require_runtime_database("mysql+pymysql://u:p@h:3306/couple_diary_dev")


# ---------------------------------------------------------------------------
# PostgreSQL harness：同 SQLite 行为（显式 DSN 才运行）
# ---------------------------------------------------------------------------


def test_postgres_unique_slot_and_budget_cap_under_threads() -> None:
    """真实并发：同槽只有一个成功；多槽并发预留总占用不超上限。"""
    url = os.environ.get("AGENT_RUNTIME_TEST_POSTGRES_DSN")
    if not url:
        pytest.skip("未显式提供 AGENT_RUNTIME_TEST_POSTGRES_DSN")

    from app.runtime.postgres_harness import (
        PostgresHarnessConfig,
        PostgresSchemaHarness,
    )

    config = PostgresHarnessConfig(url, f"agent_runtime_test_{os.getpid()}_r7", 10)
    with PostgresSchemaHarness(config) as harness:
        assert harness.session_factory is not None
        with harness.session_factory() as setup:
            _make_run(setup, "run-pg")
            setup.commit()

        policy = _policy(max_cost_per_run=Decimal("0.6"))
        factory = harness.session_factory

        def _reserve_once(scene_id: str) -> str:
            with factory() as session:
                service = MemoirAudioJobsService(session, policy)
                try:
                    outcome = service.reserve_job(
                        _narration_reservation("run-pg", scene_id, "hmac-x")
                    )
                    session.commit()
                    return outcome.outcome
                except MemoirAudioJobsError as exc:
                    return exc.code

        with ThreadPoolExecutor(max_workers=4) as pool:
            same_slot = list(pool.map(_reserve_once, ["scene-1"] * 4))
        assert same_slot.count("created") == 1
        assert same_slot.count("MEMOIR_AUDIO_JOB_SLOT_ACTIVE") == 3

        with ThreadPoolExecutor(max_workers=4) as pool:
            budget_races = list(
                pool.map(_reserve_once, [f"scene-b{i}" for i in range(8)])
            )
        assert budget_races.count("created") == 4  # 0.15 * 4 = 0.6 恰好封顶
        assert budget_races.count("MEMOIR_AUDIO_BUDGET_EXCEEDED") == 4

        with factory() as check:
            budget = check.scalar(sa.select(MemoirAudioRunBudget))
            assert budget is not None
            assert float(budget.reserved_total_cost) == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# D2（freeze §11.1）：双 Session 首建竞争 —— 作品级 BGM 槽互斥
# ---------------------------------------------------------------------------


def test_dual_session_zero_fee_same_slot_race_keeps_single_bgm_row(
    session_factory: sessionmaker[Session],
) -> None:
    """D2 场景4a：无预算行的同槽首建竞争——双 Session 先后查空再交错预留。

    SQLite 串行交错（S1查→S2查→S1预留→S2预留）：唯一约束含 hmac 挡不住
    "先查空后抢占"，但同槽（同 hmac）竞争由 reserve_job 的 _find_by_slot /
    savepoint 兜底——最终恰一个 BGM 槽，后到者固定 SLOT_ACTIVE 拒绝。
    注意：SQLite 忽略 FOR UPDATE，本用例只验证互斥判定与占槽的放置逻辑；
    真实 PostgreSQL 行锁竞争验证未执行（无安全 DSN）。
    """
    with session_factory() as session_a, session_factory() as session_b:
        _make_run(session_a, "run-d2-zero-fee")
        service_a = MemoirAudioJobsService(session_a, _policy())
        service_b = MemoirAudioJobsService(session_b, _policy())
        # S1查 → S2查：首建竞争前置，两边的互斥查询都未见任何 BGM 行。
        assert service_a.list_work_background_music_jobs(
            "run-d2-zero-fee", 3, "1.0.8"
        ) == []
        assert service_b.list_work_background_music_jobs(
            "run-d2-zero-fee", 3, "1.0.8"
        ) == []
        # S1预留（零费）→ commit：首个槽建立。
        created = service_a.reserve_job(
            _default_music_reservation("run-d2-zero-fee", "hmac-a")
        )
        assert created.outcome == "created"
        session_a.commit()
        # S2持先前"查空"的旧视图抢占同槽：_find_by_slot 命中在途行 → 拒绝。
        with pytest.raises(MemoirAudioJobsError) as err:
            service_b.reserve_job(
                _default_music_reservation("run-d2-zero-fee", "hmac-a")
            )
        assert err.value.code == "MEMOIR_AUDIO_JOB_SLOT_ACTIVE"
        session_b.commit()

        with session_factory() as check:
            rows = check.scalars(
                sa.select(MemoirAudioJob).where(
                    MemoirAudioJob.role == ROLE_BACKGROUND_MUSIC
                )
            ).all()
            assert len(rows) == 1
            # 零费首建全程不建预算行（无锁可依是首建竞争的根因，§11.1）。
            assert check.scalar(sa.select(MemoirAudioRunBudget)) is None


def test_dual_session_paid_same_slot_race_keeps_single_reservation(
    session_factory: sessionmaker[Session],
) -> None:
    """D2 场景4b：付费 BGM 首建竞争（预算行随首个 savepoint 首建）。

    S1 付费预留建立预算行 + BGM 槽并 commit；S2 同槽抢占被 SLOT_ACTIVE
    拒绝 → 最终一个 BGM 槽、预算行恰好一笔预留（不会双计 6 元）。
    """
    with session_factory() as session_a, session_factory() as session_b:
        _make_run(session_a, "run-d2-paid")
        service_a = MemoirAudioJobsService(session_a, _policy())
        service_b = MemoirAudioJobsService(session_b, _policy())
        assert service_a.list_work_background_music_jobs("run-d2-paid", 3, "1.0.8") == []
        assert service_b.list_work_background_music_jobs("run-d2-paid", 3, "1.0.8") == []
        created = service_a.reserve_job(_music_reservation("run-d2-paid", "hmac-a"))
        assert created.outcome == "created"
        session_a.commit()
        with pytest.raises(MemoirAudioJobsError) as err:
            service_b.reserve_job(_music_reservation("run-d2-paid", "hmac-a"))
        assert err.value.code == "MEMOIR_AUDIO_JOB_SLOT_ACTIVE"
        session_b.commit()

        with session_factory() as check:
            rows = check.scalars(
                sa.select(MemoirAudioJob).where(
                    MemoirAudioJob.role == ROLE_BACKGROUND_MUSIC
                )
            ).all()
            assert len(rows) == 1
            budget = check.scalar(sa.select(MemoirAudioRunBudget))
            assert budget is not None
            # 60s × 0.05 元/s = 3.0：只有一笔付费预留入账。
            assert float(budget.reserved_total_cost) == pytest.approx(3.0)


def test_dual_session_zero_fee_different_hmac_mutex_blocks_second_row(
    session_factory: sessionmaker[Session],
) -> None:
    """S2（2026-09-14 冻结裁决）：不同 hmac 的零费 BGM 双 Session 首建互斥。

    取代 D2 时代的"缺口探针"（原断言第二行成立、2 行并存）：reserve_job
    在 _assert_run_active 取得的 AgentRun 行锁下复查 hmac-less 守卫——
    S1 提交 hmac-a 后，S2 的守卫查询必见该行并拒绝。SQLite 串行交错下
    SELECT 在 DML 之外跑 autocommit、能看到已提交行，故互斥在 SQLite
    也成立；本用例只验证串行交错的判定与占槽放置，真实并发（两事务
    真并行）的行锁竞争依赖 PostgreSQL 行锁（无安全 DSN 时 PG harness
    用例显式 skip，如实报告）。
    """
    with session_factory() as session_a, session_factory() as session_b:
        _make_run(session_a, "run-s2-mutex")
        service_a = MemoirAudioJobsService(session_a, _policy())
        service_b = MemoirAudioJobsService(session_b, _policy())
        # S1查 → S2查：首建竞争前置，两边的互斥查询都未见任何 BGM 行。
        assert service_a.list_work_background_music_jobs("run-s2-mutex", 3, "1.0.8") == []
        assert service_b.list_work_background_music_jobs("run-s2-mutex", 3, "1.0.8") == []
        # S1预留（零费 hmac-a）→ commit：首个槽建立。
        created = service_a.reserve_job(
            _default_music_reservation("run-s2-mutex", "hmac-a")
        )
        assert created.outcome == "created"
        session_a.commit()
        # S2不同 hmac 抢建第二行：守卫必见已提交的 hmac-a 行 → 拒绝。
        with pytest.raises(MemoirAudioJobsError) as err:
            service_b.reserve_job(
                _default_music_reservation("run-s2-mutex", "hmac-b")
            )
        assert err.value.code == "MEMOIR_AUDIO_JOB_SLOT_ACTIVE"
        session_b.commit()

        with session_factory() as check:
            rows = check.scalars(
                sa.select(MemoirAudioJob).where(
                    MemoirAudioJob.role == ROLE_BACKGROUND_MUSIC
                )
            ).all()
            # 互斥收口：全文只剩赢家的 1 行；零费路径全程无预算行。
            assert len(rows) == 1
            assert rows[0].input_hmac == "hmac-a"
            assert check.scalar(sa.select(MemoirAudioRunBudget)) is None


def test_inflight_default_row_blocks_paid_reservation_of_different_hmac(
    session_factory: sessionmaker[Session],
) -> None:
    """S2 跨模式互斥：默认槽在途时，付费（火山）不同输入不得抢建。

    对应合同"至多一个作品级 BGM 槽"+"切换来源前排空在途音频"：作品级
    槽一旦被默认配乐（hmac-a）占用且仍在途，付费模式（hmac-b）的首建
    在同一守卫处被拒——不存在"默认与付费各建一行"的形态。付费被拒时
    预留整体未发生：不建预算行、不产生任何金额占用。
    """
    with session_factory() as session:
        _make_run(session, "run-s2-cross-mode")
        service = MemoirAudioJobsService(session, _policy())
        created = service.reserve_job(
            _default_music_reservation("run-s2-cross-mode", "hmac-a")
        )
        assert created.outcome == "created"
        session.commit()

        with pytest.raises(MemoirAudioJobsError) as err:
            service.reserve_job(_music_reservation("run-s2-cross-mode", "hmac-b"))
        assert err.value.code == "MEMOIR_AUDIO_JOB_SLOT_ACTIVE"
        session.commit()

        rows = session.scalars(
            sa.select(MemoirAudioJob).where(
                MemoirAudioJob.role == ROLE_BACKGROUND_MUSIC
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].input_hmac == "hmac-a"
        # 付费预留被守卫拒绝在 savepoint 之前：预算表零写入。
        assert session.scalar(sa.select(MemoirAudioRunBudget)) is None


def test_failed_bgm_row_blocks_different_hmac_but_same_hmac_retry_revives(
    session_factory: sessionmaker[Session],
) -> None:
    """S2 终态阻断：failed 的作品级行仍占槽，只有同输入可复活。

    守卫刻意含终态行（failed/cancelled/cleaned）：任何不同 HMAC 的作品级
    行都阻断新来源首建；失败行仅由同 HMAC（同输入）重试经
    _reserve_existing 复活（attempt+1、outcome=retried），复活后槽仍归
    同一输入，不产生第二行。
    """
    with session_factory() as session:
        _make_run(session, "run-s2-terminal")
        service = MemoirAudioJobsService(session, _policy())
        created = service.reserve_job(
            _default_music_reservation("run-s2-terminal", "hmac-a")
        )
        assert created.outcome == "created"
        session.commit()
        service.mark_failed(
            created.job.job_id,
            created.job.lease_token,
            error_code="AUDIO_SOURCE_READ_FAILED",
        )
        session.commit()

        # 不同输入：failed 行仍占槽 → 拒绝。
        with pytest.raises(MemoirAudioJobsError) as err:
            service.reserve_job(
                _default_music_reservation("run-s2-terminal", "hmac-b")
            )
        assert err.value.code == "MEMOIR_AUDIO_JOB_SLOT_ACTIVE"
        session.commit()

        # 同输入重试：唯一合法的复活路径。
        revived = service.reserve_job(
            _default_music_reservation("run-s2-terminal", "hmac-a")
        )
        assert revived.outcome == "retried"
        session.commit()

        rows = session.scalars(
            sa.select(MemoirAudioJob).where(
                MemoirAudioJob.role == ROLE_BACKGROUND_MUSIC
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].input_hmac == "hmac-a"
        assert rows[0].state == STATE_RESERVED
        assert rows[0].attempt == 2
