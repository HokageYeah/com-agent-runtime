"""M8 默认配乐（freeze 2026-09-11 §5/§6/§7）service 层测试。

覆盖口径：
1. 默认模式全流程：读源 → 域拆分指纹（mode=default + source_key +
   content_digest）→ 复用/互斥 → 零费槽 → 副本上传 → 零费结算；
2. 源读失败（缺失/403/超时/超限/兜底）与空字节/损坏 → 仅 BGM 降级，
   旁白照常，绝不自动切换付费生成；
3. 同 Run 互斥（唯一防线）：源换代 / 火山在途行 → 保守降级不建第二槽；
4. 恢复路径：同源已上传资产复用、零费已结算失败槽复活重试；
5. `_bgm_slot_retryable` 零费修正与付费语义一字不动；
6. 付费生成模式回归 + 装配分叉（默认模式不要求生成三项）。

全部替身 + SQLite 内存库 + `_env_file=None` 隔离 Settings，不触真实
OSS / 付费服务 / 开发者真实 env。
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401 确保全部模型注册进 Base.metadata
from app.core.config import Settings
from app.db.sqlalchemy_db import Base
from app.models import AgentRun
from app.models.memoir_audio_job import (
    NO_SEGMENT_INDEX,
    ROLE_BACKGROUND_MUSIC,
    STATE_FAILED,
    STATE_RESERVED,
    STATE_SUBMITTED,
    STATE_UPLOADED,
    WORK_SCENE_ID,
    MemoirAudioJob,
    MemoirAudioRunBudget,
)
from app.services.memoir.memoir_audio_jobs import (
    MemoirAudioCostPolicy,
    MemoirAudioJobsService,
    estimate_tts_cost,
)
from app.services.memoir.memoir_audio_provider import (
    MusicTaskSnapshot,
    TTSSegmentResult,
    split_narration_segments,
)
from app.services.memoir.memoir_audio_service import (
    MemoirAudioConfig,
    MemoirAudioService,
    build_memoir_audio_service,
    compute_input_hmac,
)
from app.services.memoir.memoir_audio_storage import MemoirAudioStorageError

RUN_ID = "run-svc-default"
PACKAGE_VERSION = "1.0.8"
SCENE_BODY = "那年春天我们在江边老城散步看日落，晚风很轻。"
LOSER_SCENE_BODY = "那年秋天我们在海边看潮起潮落，晚风很轻。"
DEFAULT_SOURCE_KEY = "memoir-test/audios/default/memoirs.mp3"
SOURCE_BYTES = b"default-bgm-source-mp3-bytes-v1"
INPUT_KEY = "svc-test-input-key"
SCOPE_KEY = "svc-test-scope-key"
BGM_ENTRY_KEYS = {"media_id", "object_key", "mime", "duration_ms"}
BGM_AUDIO_URL = "https://bgm.example.test/a.mp3"
BGM_TASK_ID = "task-svc-1"
# 假转码返回的奇特整数毫秒：证明时长来自实测转码而非默认 60s。
TRANSCODE_DURATION_MS = 47_777


# ---------------------------------------------------------------------------
# 测试替身（与 108 / ledger_recovery 同形状：只记录安全元数据）
# ---------------------------------------------------------------------------


class FakeTTSClient:
    def __init__(self) -> None:
        self._speaker = "zh_female_wenroushunv_uranus_bigtts"
        self._speech_rate = -10
        self.calls: list[str] = []

    async def synthesize_segment(self, text: str) -> TTSSegmentResult:
        self.calls.append(text)
        return TTSSegmentResult(
            audio=b"ID3mp3frame:" + text.encode()[:12],
            text_words=max(1, len(text)),
        )


class FakeMusicClient:
    """音乐替身：默认成功；默认模式断言其计数恒为 0（零火山调用）。"""

    def __init__(self) -> None:
        self._action = "GenBGM"
        self.submit_count = 0
        self.query_count = 0

    async def submit_generation(self) -> str:
        self.submit_count += 1
        return BGM_TASK_ID

    async def query_task(self, task_id: str) -> MusicTaskSnapshot:
        self.query_count += 1
        return MusicTaskSnapshot(status=2, audio_url=BGM_AUDIO_URL)


class FakeUploader:
    """上传/读源替身：download_private_bytes 可配置源字节或受控失败。"""

    def __init__(
        self,
        *,
        source_bytes: bytes = SOURCE_BYTES,
        source_error: Exception | None = None,
    ) -> None:
        self._source_bytes = source_bytes
        self._source_error = source_error
        self.downloads: list[dict[str, Any]] = []
        self.uploads: list[tuple[str, str]] = []

    def download_private_bytes(
        self, object_key: str, *, max_bytes: int, timeout_seconds: float
    ) -> bytes:
        self.downloads.append(
            {
                "key": object_key,
                "max_bytes": max_bytes,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._source_error is not None:
            raise self._source_error
        return self._source_bytes

    def upload_private_bytes(self, data: bytes, object_key: str, mime: str) -> None:
        self.uploads.append((object_key, mime))


class FakeTranscoder:
    """转码替身：BGM 实测时长返回奇特整数毫秒；可配置转码失败。"""

    def __init__(self, *, bgm_error: Exception | None = None) -> None:
        self._bgm_error = bgm_error

    async def concat_mp3_segments(self, parts: list[bytes]) -> Any:
        return SimpleNamespace(audio=b"joined-mp3", duration_ms=4200)

    async def transcode_to_mp3(self, data: bytes) -> Any:
        if self._bgm_error is not None:
            raise self._bgm_error
        return SimpleNamespace(audio=b"bgm-mp3-copy", duration_ms=TRANSCODE_DURATION_MS)


class FakeDownloader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def download(self, url: str) -> bytes:
        self.calls.append(url)
        return b"bgm-bytes"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _cost_policy() -> MemoirAudioCostPolicy:
    return MemoirAudioCostPolicy(
        currency="CNY",
        tts_price_per_1000_text_words=Decimal("1"),
        music_price_per_second=Decimal("0.1"),
        max_cost_per_run=Decimal("10"),
    )


def _config(**overrides: Any) -> MemoirAudioConfig:
    """默认配乐模式配置（生成模式用例显式覆盖 music_generation_enabled）。"""
    values: dict[str, Any] = dict(
        narrator_prefix="memoir-test/audios/narrator/",
        background_prefix="memoir-test/audios/background/",
        scope_hmac_key=SCOPE_KEY,
        input_hmac_key=INPUT_KEY,
        music_generation_enabled=False,
        default_bgm_object_key=DEFAULT_SOURCE_KEY,
    )
    values.update(overrides)
    return MemoirAudioConfig(**values)


def _make_run(session: Session) -> AgentRun:
    run = AgentRun(
        run_id=RUN_ID,
        agent_id="memoir_agent",
        agent_version=PACKAGE_VERSION,
        package_digest="digest-test",
        contract_version="1.1.0",
        business_type="couple_memory",
        business_id="archive-svc",
        input_json={
            "archive_id": "archive-svc",
            "snapshot_id": "snapshot-svc",
            "generation_epoch": 0,
        },
        capability_snapshot_json={"execution_policy": {"max_run_seconds": 1200}},
        active_elapsed_ms=0,
        execution_attempt=1,
        authorization_version=1,
        caller_id="caller-1",
        tenant_id="couple-diary",
        create_idempotency_key=f"idem-{RUN_ID}",
        callback_target_id="memory_callback",
        business_connector_id="couple_diary_backend",
        trace_id=f"trace-{RUN_ID}",
        run_deadline_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(run)
    session.commit()
    return run


def _build(
    *,
    config: MemoirAudioConfig | None = None,
    uploader: FakeUploader | None = None,
    transcoder: FakeTranscoder | None = None,
    music: FakeMusicClient | None = None,
) -> SimpleNamespace:
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    run = _make_run(session)
    # 替身只实例化一次：服务与断言共用同一计数器。
    music = music or FakeMusicClient()
    uploader = uploader or FakeUploader()
    service = MemoirAudioService(
        tts_client=FakeTTSClient(),
        music_client=music,
        uploader=uploader,
        transcoder=transcoder or FakeTranscoder(),
        downloader=FakeDownloader(),
        jobs_service=MemoirAudioJobsService(session, _cost_policy()),
        config=config or _config(),
        session=session,
    )
    return SimpleNamespace(
        engine=engine, session=session, run=run, service=service,
        music=music, uploader=uploader,
    )


def _playback(body: str = SCENE_BODY) -> dict[str, Any]:
    return {"scenes": [{"scene_id": "scene-1", "body": body}]}


def _default_bgm_hmac(
    source_bytes: bytes, *, source_key: str = DEFAULT_SOURCE_KEY
) -> str:
    """按 freeze §5 域拆分 payload 现算默认 BGM 指纹（与服务实现同构）。"""
    return compute_input_hmac(
        INPUT_KEY,
        {
            "role": ROLE_BACKGROUND_MUSIC,
            "mode": "default",
            "source_key": source_key,
            "package_version": PACKAGE_VERSION,
            "content_digest": hashlib.sha256(source_bytes).hexdigest(),
        },
    )


def _volcano_bgm_hmac(*, action: str = "GenBGM", duration: int = 60) -> str:
    """按服务现行 payload 现算火山 BGM 指纹（与生成分支实现同构）。

    FakeMusicClient._action="GenBGM"、config 默认 music_duration_seconds=60，
    因此不带参调用即等于生成模式实际提交时使用的指纹。
    """
    return compute_input_hmac(
        INPUT_KEY,
        {
            "role": ROLE_BACKGROUND_MUSIC,
            "action": action,
            "duration": duration,
            "model": "v5.0",
        },
    )


def _seed_bgm_row(
    session: Session,
    *,
    input_hmac: str,
    state: str = STATE_UPLOADED,
    object_key: str | None = None,
    duration_ms: int | None = None,
    settled_cost: Decimal | None = None,
    reserved_cost: Decimal = Decimal("0"),
    provider_task_id: str | None = None,
    attempt: int = 1,
    error_code: str | None = None,
) -> MemoirAudioJob:
    """直接播种 BGM 账本行（构造恢复/互斥前置态，绕过服务状态机）。"""
    job = MemoirAudioJob(
        job_id=f"job-seed-{uuid4().hex[:12]}",
        business_id="archive-svc",
        run_id=RUN_ID,
        generation_epoch=0,
        package_version=PACKAGE_VERSION,
        role=ROLE_BACKGROUND_MUSIC,
        scene_id=WORK_SCENE_ID,
        segment_index=NO_SEGMENT_INDEX,
        input_hmac=input_hmac,
        attempt=attempt,
        state=state,
        object_key=object_key,
        mime="audio/mpeg" if object_key else None,
        duration_ms=duration_ms,
        reserved_cost=reserved_cost,
        settled_cost=settled_cost,
        provider_task_id=provider_task_id,
        currency="CNY",
        lease_owner="worker-seed",
        lease_token=1,
        expires_at=datetime.now(UTC) - timedelta(hours=2),
        updated_at=datetime.now(UTC) - timedelta(hours=2),
        error_code=error_code,
    )
    session.add(job)
    session.commit()
    return job


def _bgm_rows(session: Session) -> list[MemoirAudioJob]:
    return list(
        session.scalars(
            sa.select(MemoirAudioJob).where(MemoirAudioJob.role == ROLE_BACKGROUND_MUSIC)
        )
    )


def _bgm_upload_keys(harness: SimpleNamespace) -> set[str]:
    """替身上传里落在 background 前缀下的副本键（排除旁白上传）。"""
    return {
        key for key, _mime in harness.uploader.uploads
        if key.startswith("memoir-test/audios/background/")
    }


# ---------------------------------------------------------------------------
# 默认模式全流程
# ---------------------------------------------------------------------------


def test_default_mode_delivers_background_copy_with_zero_fee_ledger() -> None:
    """默认模式全流程：私有副本交付、零费槽结算 0、零火山调用。"""
    h = _build()
    result = h.service.generate(h.run, _playback())

    bgm = result["background_music"]
    assert bgm is not None
    assert set(bgm) == BGM_ENTRY_KEYS
    assert bgm["mime"] == "audio/mpeg"
    # 副本键：现行 background 工作目录 + 随机不透明名，绝不共享源 key。
    assert bgm["object_key"].startswith("memoir-test/audios/background/")
    assert "/bgm-" in bgm["object_key"] and bgm["object_key"].endswith(".mp3")
    assert bgm["object_key"] != DEFAULT_SOURCE_KEY
    # 时长来自转码实测（奇特整数毫秒），绝非默认 60s。
    assert bgm["duration_ms"] == TRANSCODE_DURATION_MS
    # 旁白照常交付。
    assert len(result["narrations"]) == 1

    # 读源：精确源 key、字节上限对齐 MEMOIR_AUDIO_MAX_FILE_BYTES、超时为正。
    assert len(h.uploader.downloads) == 1
    assert h.uploader.downloads[0]["key"] == DEFAULT_SOURCE_KEY
    assert h.uploader.downloads[0]["max_bytes"] == 20_971_520
    assert h.uploader.downloads[0]["timeout_seconds"] > 0
    # 源 key 永不进入上传（副本独立私有对象）。
    assert DEFAULT_SOURCE_KEY not in {key for key, _ in h.uploader.uploads}

    rows = _bgm_rows(h.session)
    assert len(rows) == 1
    row = rows[0]
    assert row.state == STATE_UPLOADED
    assert row.object_key == bgm["object_key"]
    assert row.settled_cost == Decimal("0")
    assert row.reserved_cost == Decimal("0")
    assert row.requested_music_seconds is None
    assert row.provider_task_id is None
    assert row.duration_ms == TRANSCODE_DURATION_MS
    assert row.scene_id == WORK_SCENE_ID and row.segment_index == NO_SEGMENT_INDEX

    # 零火山调用：不提交、不查询、不下载临时 URL。
    assert h.music.submit_count == 0 and h.music.query_count == 0
    # 预算只含 TTS 分段预留：默认 BGM 零费槽不占预算。
    budget = h.session.scalar(
        sa.select(MemoirAudioRunBudget).where(MemoirAudioRunBudget.run_id == RUN_ID)
    )
    expected_tts = sum(
        estimate_tts_cost(seg, _cost_policy())
        for seg in split_narration_segments(SCENE_BODY)
    )
    assert budget is not None
    assert budget.reserved_total_cost == expected_tts


# ---------------------------------------------------------------------------
# 源读失败 / 空字节 / 损坏：仅 BGM 降级
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "AUDIO_SOURCE_NOT_FOUND",
        "AUDIO_SOURCE_ACCESS_DENIED",
        "AUDIO_SOURCE_READ_TIMEOUT",
        "AUDIO_FILE_TOO_LARGE",
        "AUDIO_SOURCE_READ_FAILED",
    ],
)
def test_default_mode_source_read_failures_degrade_bgm_only(code: str) -> None:
    """源读五类受控失败：BGM 降级无配乐，旁白照常，零账本副作用、零火山。"""
    uploader = FakeUploader(
        source_error=MemoirAudioStorageError(code, "受控读取失败")
    )
    h = _build(uploader=uploader)
    result = h.service.generate(h.run, _playback())

    assert result["background_music"] is None
    assert len(result["narrations"]) == 1
    assert _bgm_rows(h.session) == []
    assert _bgm_upload_keys(h) == set()
    assert h.music.submit_count == 0 and h.music.query_count == 0


@pytest.mark.parametrize(
    "error",
    [
        MemoirAudioStorageError("AUDIO_TRANSCODE_INPUT_INVALID", "待转码字节为空"),
        MemoirAudioStorageError("AUDIO_DECODE_FAILED", "音频转码失败"),
        MemoirAudioStorageError("AUDIO_DURATION_INVALID", "时长非法"),
    ],
)
def test_default_mode_empty_or_corrupt_source_degrades(error: Exception) -> None:
    """空字节/解码失败/时长非法：转码入口拒绝 → 仅降级无 BGM，不切付费。"""
    h = _build(transcoder=FakeTranscoder(bgm_error=error))
    result = h.service.generate(h.run, _playback())

    assert result["background_music"] is None
    assert len(result["narrations"]) == 1
    rows = _bgm_rows(h.session)
    # 零费槽已预留但持键前失败：无对象键、未结算，绝不产生付费副作用。
    assert len(rows) == 1
    assert rows[0].object_key is None and rows[0].settled_cost is None
    assert _bgm_upload_keys(h) == set()
    assert h.music.submit_count == 0


# ---------------------------------------------------------------------------
# 同 Run 互斥：唯一防线（freeze §4.3）
# ---------------------------------------------------------------------------


def test_default_mode_source_replacement_degrades_without_second_slot() -> None:
    """源换代（不同 content_digest）：保守降级，不建第二槽不上传副本。"""
    h = _build()
    # 旧源字节算出的默认指纹（模拟上一代源文件已交付）。
    _seed_bgm_row(
        h.session,
        input_hmac=_default_bgm_hmac(b"old-default-source-bytes"),
        state=STATE_UPLOADED,
        object_key="memoir-test/audios/background/old-scope/bgm-old.mp3",
        duration_ms=50_000,
    )
    result = h.service.generate(h.run, _playback())

    assert result["background_music"] is None
    assert len(result["narrations"]) == 1
    # 不建第二槽：BGM 行数保持 1。
    assert len(_bgm_rows(h.session)) == 1
    assert _bgm_upload_keys(h) == set()
    assert h.music.submit_count == 0


def test_default_mode_volcano_inflight_row_blocks_new_slot() -> None:
    """火山在途/已结算行存在：默认模式不建槽、不提交付费任务、不 QuerySong。"""
    h = _build()
    _seed_bgm_row(
        h.session,
        input_hmac=_default_bgm_hmac(b"whatever-volcano-input"),
        state=STATE_SUBMITTED,
        provider_task_id="task-volcano-1",
        reserved_cost=Decimal("6"),
    )
    result = h.service.generate(h.run, _playback())

    assert result["background_music"] is None
    assert len(result["narrations"]) == 1
    assert len(_bgm_rows(h.session)) == 1
    assert _bgm_upload_keys(h) == set()
    assert h.music.submit_count == 0 and h.music.query_count == 0


def test_generation_mode_default_reserved_slot_blocks_paid_submit() -> None:
    """D2 场景1（pre-fix 红，复核已实证失败形状）：同 Run 已有默认零费
    reserved 槽，切生成模式 → 火山分支不得建第二槽、不得提交付费任务。

    复核记录（D2/P1）：播种同 Run 默认 reserved 槽再切生成模式，
    修复前音乐 submit_count=1、BGM 行数=2（find_job 携带火山 hmac 看不见
    默认行）。互斥必须两模式共用（freeze §11.1）。
    """
    h = _build()
    # 默认模式留下的零费 reserved 槽（读源后预留、交付前崩溃窗口）。
    _seed_bgm_row(
        h.session,
        input_hmac=_default_bgm_hmac(SOURCE_BYTES),
        state=STATE_RESERVED,
    )
    h.service.config.music_generation_enabled = True
    result = h.service.generate(h.run, _playback())

    # 保守降级：无 BGM、不建第二槽、不提交付费任务、不查火山。
    assert result["background_music"] is None
    assert len(result["narrations"]) == 1
    assert len(_bgm_rows(h.session)) == 1
    assert h.music.submit_count == 0 and h.music.query_count == 0


def test_generation_mode_volcano_different_input_blocks_second_slot() -> None:
    """D2 场景3（pre-fix 红）：生成输入变化（不同指纹火山行已存在）→
    不得建第二槽、不得再提交一次付费生成。

    播种 duration=90 的火山 uploaded 行，本执行配置 duration=60 →
    火山 hmac 不同；唯一约束含 hmac 挡不住换输入建第二行，互斥判定
    必须在提交前拦截。
    """
    h = _build(config=_config(music_generation_enabled=True))
    _seed_bgm_row(
        h.session,
        input_hmac=_volcano_bgm_hmac(duration=90),
        state=STATE_UPLOADED,
        object_key="memoir-test/audios/background/old-scope/bgm-volcano-90s.mp3",
        duration_ms=90_000,
        settled_cost=Decimal("9"),
        reserved_cost=Decimal("9"),
    )
    result = h.service.generate(h.run, _playback())

    assert result["background_music"] is None
    assert len(result["narrations"]) == 1
    assert len(_bgm_rows(h.session)) == 1
    assert h.music.submit_count == 0 and h.music.query_count == 0


def test_default_mode_seeded_volcano_hmac_row_degrades() -> None:
    """D2 场景2（回归，既有 §4.3 行为）：真实火山指纹（GenBGM/60s/v5.0）
    在途行存在，切默认模式 → 默认分支保守降级，不建槽零火山调用。"""
    h = _build()
    _seed_bgm_row(
        h.session,
        input_hmac=_volcano_bgm_hmac(),
        state=STATE_SUBMITTED,
        provider_task_id="task-volcano-1",
        reserved_cost=Decimal("6"),
    )
    result = h.service.generate(h.run, _playback())

    assert result["background_music"] is None
    assert len(result["narrations"]) == 1
    assert len(_bgm_rows(h.session)) == 1
    assert _bgm_upload_keys(h) == set()
    # 默认分支必须先读源才能算域拆分指纹（freeze §5），因此恰有一次读源；
    # 互斥降级后零上传、零火山调用。
    assert len(h.uploader.downloads) == 1
    assert h.music.submit_count == 0 and h.music.query_count == 0


# ---------------------------------------------------------------------------
# 恢复路径
# ---------------------------------------------------------------------------


def test_default_mode_reuses_uploaded_asset_for_same_source() -> None:
    """同 Run 同源已上传资产：直接复用（读源一次算指纹），不再上传副本。"""
    h = _build()
    seeded_key = "memoir-test/audios/background/some-scope/bgm-reuse.mp3"
    _seed_bgm_row(
        h.session,
        input_hmac=_default_bgm_hmac(SOURCE_BYTES),
        state=STATE_UPLOADED,
        object_key=seeded_key,
        duration_ms=51_234,
    )
    result = h.service.generate(h.run, _playback())

    bgm = result["background_music"]
    assert bgm is not None
    assert bgm["object_key"] == seeded_key
    assert bgm["duration_ms"] == 51_234
    # 复用路径零上传、零火山；读源一次仅为计算域拆分指纹。
    assert _bgm_upload_keys(h) == set()
    assert len(h.uploader.downloads) == 1
    assert h.music.submit_count == 0


def test_default_mode_recovers_settled_zero_fee_failed_slot() -> None:
    """零费已结算失败槽（上传后崩溃窗口）：settled=0 不得拦截恢复重试。"""
    h = _build()
    _seed_bgm_row(
        h.session,
        input_hmac=_default_bgm_hmac(SOURCE_BYTES),
        state=STATE_FAILED,
        settled_cost=Decimal("0"),
        reserved_cost=Decimal("0"),
        provider_task_id=None,
        error_code="AUDIO_UPLOAD_FAILED",
    )
    result = h.service.generate(h.run, _playback())

    bgm = result["background_music"]
    assert bgm is not None
    assert bgm["duration_ms"] == TRANSCODE_DURATION_MS
    rows = _bgm_rows(h.session)
    assert len(rows) == 1
    assert rows[0].state == STATE_UPLOADED
    assert rows[0].attempt == 2
    assert rows[0].settled_cost == Decimal("0")
    assert len(_bgm_upload_keys(h)) == 1


def test_default_mode_revival_reuses_held_object_key() -> None:
    """持旧键失败槽复活必须复用旧键重试上传（freeze §11.2 D3）。

    复活槽（retried）已持 object_key：默认路径重试必须用同一键上传，
    绝不重新 build_audio_object_key——新键会被 jobs 层
    MEMOIR_AUDIO_OBJECT_KEY_CONFLICT 拒绝并整槽降级；断言上传键集合
    恰好等于旧键（不产生第二个键）。
    """
    old_key = "memoir-test/audios/background/revive-old.mp3"
    h = _build()
    _seed_bgm_row(
        h.session,
        input_hmac=_default_bgm_hmac(SOURCE_BYTES),
        state=STATE_FAILED,
        object_key=old_key,
        settled_cost=Decimal("0"),
        reserved_cost=Decimal("0"),
        provider_task_id=None,
        error_code="AUDIO_UPLOAD_FAILED",
    )
    result = h.service.generate(h.run, _playback())

    bgm = result["background_music"]
    assert bgm is not None
    # 上传副本键与复活槽旧键完全一致：不产生第二个键。
    assert _bgm_upload_keys(h) == {old_key}
    assert bgm["object_key"] == old_key
    rows = _bgm_rows(h.session)
    assert len(rows) == 1
    assert rows[0].object_key == old_key
    assert rows[0].state == STATE_UPLOADED
    assert rows[0].attempt == 2


def test_default_mode_active_reserved_slot_degrades_conservatively() -> None:
    """在途零费槽（崩溃后 lease 语义内）：保守降级不抢占、不建第二槽。"""
    h = _build()
    _seed_bgm_row(
        h.session,
        input_hmac=_default_bgm_hmac(SOURCE_BYTES),
        state=STATE_RESERVED,
    )
    result = h.service.generate(h.run, _playback())

    assert result["background_music"] is None
    assert len(_bgm_rows(h.session)) == 1
    assert _bgm_upload_keys(h) == set()
    assert h.music.submit_count == 0


# ---------------------------------------------------------------------------
# _bgm_slot_retryable：零费修正 + 付费语义不动
# ---------------------------------------------------------------------------


def _job_stub(**kwargs: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = dict(
        state=STATE_FAILED, settled_cost=None, attempt=1,
        reserved_cost=Decimal("0"), provider_task_id=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_bgm_slot_retryable_zero_fee_and_paid_semantics() -> None:
    """零费默认槽已结算态放行恢复；付费分支语义一字不动。"""
    service = _build().service
    # 零费已结算失败槽（freeze §6 修正点）：settled=0 不再一票否决。
    assert service._bgm_slot_retryable(
        _job_stub(settled_cost=Decimal("0"))
    ) is True
    # 零费已结算但重试耗尽：拒绝。
    assert service._bgm_slot_retryable(
        _job_stub(settled_cost=Decimal("0"), attempt=2)
    ) is False
    # 付费已结算（含失败态）：仍然一票否决（防双付费）。
    assert service._bgm_slot_retryable(
        _job_stub(settled_cost=Decimal("6"), reserved_cost=Decimal("6"))
    ) is False
    # 付费未结算失败槽：允许一次重试（现行语义保留）。
    assert service._bgm_slot_retryable(
        _job_stub(reserved_cost=Decimal("6"))
    ) is True
    # 零费未结算失败槽：同现行语义。
    assert service._bgm_slot_retryable(_job_stub()) is True
    # 在途/未知态：一律不可重试。
    assert service._bgm_slot_retryable(
        _job_stub(state=STATE_RESERVED)
    ) is False
    assert service._bgm_slot_retryable(
        _job_stub(state=STATE_SUBMITTED, provider_task_id="task-x",
                  reserved_cost=Decimal("6"))
    ) is False


# ---------------------------------------------------------------------------
# 付费生成模式回归
# ---------------------------------------------------------------------------


def _cross_generate_factory(tmp_path: Path) -> sessionmaker[Session]:
    """S2 双 Session generate：文件 SQLite + NullPool，每 Session 独立连接。

    不复用 _build()（内存 sqlite:// 单连接）。SQLite 忽略 FOR UPDATE，
    本工厂只证明 generate 吸收 SLOT_ACTIVE、至多一槽、输家零付费提交。
    真库 RR 当前读走 test_memoir_audio_mysql_rr_isolation.py。
    """
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'memoir-audio-s2-generate.db'}",
        poolclass=NullPool,
        connect_args={"timeout": 15},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _make_run_for_id(session: Session, run_id: str) -> AgentRun:
    """独立 run_id：双 Session generate 不能复用硬编码 RUN_ID。"""
    run = AgentRun(
        run_id=run_id,
        agent_id="memoir_agent",
        agent_version=PACKAGE_VERSION,
        package_digest="digest-test",
        contract_version="1.1.0",
        business_type="couple_memory",
        business_id="archive-svc",
        input_json={
            "archive_id": "archive-svc",
            "snapshot_id": "snapshot-svc",
            "generation_epoch": 0,
        },
        capability_snapshot_json={"execution_policy": {"max_run_seconds": 1200}},
        active_elapsed_ms=0,
        execution_attempt=1,
        authorization_version=1,
        caller_id="caller-1",
        tenant_id="couple-diary",
        create_idempotency_key=f"idem-{run_id}",
        callback_target_id="memory_callback",
        business_connector_id="couple_diary_backend",
        trace_id=f"trace-{run_id}",
        run_deadline_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(run)
    session.commit()
    return run


def _service_on_session(
    session: Session,
    *,
    config: MemoirAudioConfig | None = None,
    uploader: FakeUploader | None = None,
    music: FakeMusicClient | None = None,
) -> SimpleNamespace:
    music = music or FakeMusicClient()
    uploader = uploader or FakeUploader()
    service = MemoirAudioService(
        tts_client=FakeTTSClient(),
        music_client=music,
        uploader=uploader,
        transcoder=FakeTranscoder(),
        downloader=FakeDownloader(),
        jobs_service=MemoirAudioJobsService(session, _cost_policy()),
        config=config or _config(),
        session=session,
    )
    return SimpleNamespace(session=session, service=service, music=music, uploader=uploader)


def _assert_single_bgm_slot(session_factory: sessionmaker[Session]) -> MemoirAudioJob:
    with session_factory() as check:
        rows = list(
            check.scalars(
                sa.select(MemoirAudioJob).where(
                    MemoirAudioJob.role == ROLE_BACKGROUND_MUSIC
                )
            )
        )
        assert len(rows) == 1
        return rows[0]


def test_dual_session_generate_default_vs_default_keeps_single_slot(
    tmp_path: Path,
) -> None:
    """S2：默认/默认不同源字节 → 至多一槽，输家零付费提交。"""
    factory = _cross_generate_factory(tmp_path)
    run_id = "run-s2-gen-dd"
    with factory() as setup:
        run = _make_run_for_id(setup, run_id)
    with factory() as session_a, factory() as session_b:
        winner = _service_on_session(
            session_a, uploader=FakeUploader(source_bytes=b"default-bgm-source-a")
        )
        loser = _service_on_session(
            session_b, uploader=FakeUploader(source_bytes=b"default-bgm-source-b")
        )
        won = winner.service.generate(run, _playback())
        session_a.commit()
        lost = loser.service.generate(run, _playback())
        session_b.commit()

    assert won["background_music"] is not None
    assert lost["background_music"] is None
    assert len(lost["narrations"]) == 1
    assert winner.music.submit_count == 0
    assert loser.music.submit_count == 0
    _assert_single_bgm_slot(factory)


def test_dual_session_generate_default_winner_paid_loser_zero_submit(
    tmp_path: Path,
) -> None:
    """S2：默认赢家 / 付费输家 → 至多一槽，输家 submit_count==0。"""
    factory = _cross_generate_factory(tmp_path)
    run_id = "run-s2-gen-dp"
    with factory() as setup:
        run = _make_run_for_id(setup, run_id)
    with factory() as session_a, factory() as session_b:
        winner = _service_on_session(session_a)
        loser = _service_on_session(
            session_b, config=_config(music_generation_enabled=True)
        )
        won = winner.service.generate(run, _playback())
        session_a.commit()
        lost = loser.service.generate(run, _playback())
        session_b.commit()

    assert won["background_music"] is not None
    assert lost["background_music"] is None
    assert len(lost["narrations"]) == 1
    assert winner.music.submit_count == 0
    assert loser.music.submit_count == 0
    _assert_single_bgm_slot(factory)


def test_dual_session_generate_paid_winner_default_loser_zero_submit(
    tmp_path: Path,
) -> None:
    """S2：付费赢家 / 默认输家 → 至多一槽，赢家提交一次，输家零提交。"""
    factory = _cross_generate_factory(tmp_path)
    run_id = "run-s2-gen-pd"
    with factory() as setup:
        run = _make_run_for_id(setup, run_id)
    with factory() as session_a, factory() as session_b:
        winner = _service_on_session(
            session_a, config=_config(music_generation_enabled=True)
        )
        loser = _service_on_session(
            session_b, uploader=FakeUploader(source_bytes=b"default-bgm-source-loser")
        )
        won = winner.service.generate(run, _playback())
        session_a.commit()
        lost = loser.service.generate(run, _playback())
        session_b.commit()

    assert won["background_music"] is not None
    assert lost["background_music"] is None
    assert len(lost["narrations"]) == 1
    assert winner.music.submit_count == 1
    assert loser.music.submit_count == 0
    _assert_single_bgm_slot(factory)


class _BarrierUploader(FakeUploader):
    """只挡住 background 前缀上传：旁白共用同一 uploader，全局挡会卡在旁白。"""

    def __init__(
        self,
        *,
        source_bytes: bytes,
        winner_committed: threading.Event,
        release_winner_upload: threading.Event,
    ) -> None:
        super().__init__(source_bytes=source_bytes)
        self._winner_committed = winner_committed
        self._release_winner_upload = release_winner_upload

    def upload_private_bytes(self, data: bytes, object_key: str, mime: str) -> None:
        if object_key.startswith("memoir-test/audios/background/"):
            self._winner_committed.set()
            assert self._release_winner_upload.wait(timeout=15)
        super().upload_private_bytes(data, object_key, mime)


def _run_interleaved_generate(
    tmp_path: Path,
    *,
    run_id: str,
    winner_config: MemoirAudioConfig | None,
    loser_config: MemoirAudioConfig | None,
    winner_bytes: bytes,
    loser_bytes: bytes,
    expect_winner_submit: int,
) -> None:
    """双方先见空后并发 generate；输家 BGM reserve 等到赢家槽提交后再交叉。

    SQLite 忽略 FOR UPDATE，本测试证明吸收 + 单槽 + 输家零提交，
    不是 InnoDB 等待。顺序 winner.generate() 再 loser.generate() 不是本验收。
    """
    factory = _cross_generate_factory(tmp_path)
    with factory() as setup:
        run = _make_run_for_id(setup, run_id)
    winner_committed = threading.Event()
    release_winner_upload = threading.Event()
    with factory() as session_a, factory() as session_b:
        jobs_a = MemoirAudioJobsService(session_a, _cost_policy())
        jobs_b = MemoirAudioJobsService(session_b, _cost_policy())
        assert jobs_a.list_work_background_music_jobs(run_id, 0, PACKAGE_VERSION) == []
        assert jobs_b.list_work_background_music_jobs(run_id, 0, PACKAGE_VERSION) == []
        winner = _service_on_session(
            session_a,
            config=winner_config,
            uploader=_BarrierUploader(
                source_bytes=winner_bytes,
                winner_committed=winner_committed,
                release_winner_upload=release_winner_upload,
            ),
        )
        loser = _service_on_session(
            session_b,
            config=loser_config,
            uploader=FakeUploader(source_bytes=loser_bytes),
        )
        # 赢家槽提交后咨询互斥会看见行并跳过 reserve；本验收要走
        # SLOT_ACTIVE 吸收，所以咨询路径强制放行。
        loser.service._bgm_mutex_degraded = (  # type: ignore[method-assign]
            lambda *args, **kwargs: False
        )
        orig_bgm = loser.service._generate_background_music

        async def _delayed_bgm(*args: Any, **kwargs: Any) -> Any:
            loop = asyncio.get_running_loop()
            asserted = await loop.run_in_executor(
                None, lambda: winner_committed.wait(timeout=15)
            )
            assert asserted
            release_winner_upload.set()
            return await orig_bgm(*args, **kwargs)

        loser.service._generate_background_music = _delayed_bgm  # type: ignore[method-assign]
        results: dict[str, Any] = {}
        errors: list[BaseException] = []

        def _run_winner() -> None:
            try:
                results["won"] = winner.service.generate(run, _playback())
                session_a.commit()
            except BaseException as exc:
                errors.append(exc)
                release_winner_upload.set()

        def _run_loser() -> None:
            try:
                results["lost"] = loser.service.generate(
                    run, _playback(LOSER_SCENE_BODY)
                )
                session_b.commit()
            except BaseException as exc:
                errors.append(exc)
            finally:
                # 咨询互斥若跳过 reserve，也必须放行赢家上传，避免死等。
                release_winner_upload.set()

        t_w = threading.Thread(target=_run_winner)
        t_l = threading.Thread(target=_run_loser)
        t_w.start()
        t_l.start()
        t_w.join(timeout=30)
        t_l.join(timeout=30)
        assert not t_w.is_alive() and not t_l.is_alive()
        assert errors == []
        won, lost = results["won"], results["lost"]
        assert won["background_music"] is not None
        assert lost["background_music"] is None
        assert len(lost["narrations"]) == 1
        assert winner.music.submit_count == expect_winner_submit
        assert loser.music.submit_count == 0
        _assert_single_bgm_slot(factory)


def test_interleaved_generate_default_vs_default_keeps_single_slot(
    tmp_path: Path,
) -> None:
    """受控交错：默认/默认不同源字节。顺序 generate 不是本验收。"""
    _run_interleaved_generate(
        tmp_path,
        run_id="run-s2-int-dd",
        winner_config=None,
        loser_config=None,
        winner_bytes=b"default-bgm-source-a",
        loser_bytes=b"default-bgm-source-b",
        expect_winner_submit=0,
    )


def test_interleaved_generate_default_winner_paid_loser_zero_submit(
    tmp_path: Path,
) -> None:
    """受控交错：默认赢家 / 付费输家。顺序 generate 不是本验收。"""
    _run_interleaved_generate(
        tmp_path,
        run_id="run-s2-int-dp",
        winner_config=None,
        loser_config=_config(music_generation_enabled=True),
        winner_bytes=SOURCE_BYTES,
        loser_bytes=b"paid-loser-unused",
        expect_winner_submit=0,
    )


def test_interleaved_generate_paid_winner_default_loser_zero_submit(
    tmp_path: Path,
) -> None:
    """受控交错：付费赢家 / 默认输家。顺序 generate 不是本验收。"""
    _run_interleaved_generate(
        tmp_path,
        run_id="run-s2-int-pd",
        winner_config=_config(music_generation_enabled=True),
        loser_config=None,
        winner_bytes=SOURCE_BYTES,
        loser_bytes=b"default-bgm-source-loser",
        expect_winner_submit=1,
    )


def test_generation_mode_keeps_volcano_paid_path() -> None:
    """生成开关 true：现行付费链路不变（提交/查询/下载/按秒结算）。"""
    h = _build(
        config=_config(music_generation_enabled=True),
        transcoder=FakeTranscoder(),
    )
    result = h.service.generate(h.run, _playback())

    bgm = result["background_music"]
    assert bgm is not None and bgm["mime"] == "audio/mpeg"
    assert h.music.submit_count == 1 and h.music.query_count >= 1
    rows = _bgm_rows(h.session)
    assert len(rows) == 1
    assert rows[0].requested_music_seconds == 60
    assert rows[0].settled_cost == Decimal("6")
    assert rows[0].provider_task_id == BGM_TASK_ID
    # 付费路径不读默认源。
    assert h.uploader.downloads == []


# ---------------------------------------------------------------------------
# 装配分叉（build_memoir_audio_service / MemoirAudioConfig）
# ---------------------------------------------------------------------------


def _isolated_kwargs() -> dict[str, Any]:
    """默认模式装配所需基础 15 项（与 A/R9 冻结口径一致；不含生成三项）。"""
    return {
        "ENVIRONMENT": "test",
        "MEMOIR_AUDIO_ENABLED": True,
        "MEMOIR_TTS_API_KEY": "test-only-key",
        "VOLCANO_CV_ACCESS_KEY": "test-ak",
        "VOLCANO_CV_SECRET_KEY": "test-sk",
        "MEMOIR_TTS_PRICE_PER_1000_TEXT_WORDS": "1",
        "MEMOIR_AUDIO_MAX_COST_PER_RUN": "10",
        "MEMOIR_AUDIO_COST_CURRENCY": "CNY",
        "MEMORY_AUDIO_OSS_ENDPOINT": "oss-cn-hangzhou.aliyuncs.com",
        "MEMORY_AUDIO_OSS_BUCKET": "bucket",
        "MEMORY_AUDIO_OSS_ACCESS_KEY_ID": "ak",
        "MEMORY_AUDIO_OSS_ACCESS_KEY_SECRET": "sk",
        "MEMORY_AUDIO_NARRATOR_PREFIX": "memoir-test/audios/narrator/",
        "MEMORY_AUDIO_BACKGROUND_PREFIX": "memoir-test/audios/background/",
        "MEMORY_AUDIO_SCOPE_HMAC_KEY": "scope-key",
        "MEMOIR_AUDIO_INPUT_HMAC_KEY": "test-input-key",
        "MEMOIR_DEFAULT_BGM_OBJECT_KEY": DEFAULT_SOURCE_KEY,
    }


def _isolated_settings(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Settings:
    """与真实 env / dotenv 完全隔离的 Settings（不加载 .env.*.local）。"""
    from app.core import config as config_module

    for name in (
        "MEMOIR_AUDIO_ENABLED",
        "MEMOIR_MUSIC_GENERATION_ENABLED",
        *config_module._MEMOIR_AUDIO_BASE_REQUIRED_STRINGS,
        *config_module._MEMOIR_AUDIO_GENERATION_REQUIRED_STRINGS,
    ):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, **kwargs)


def test_build_service_default_mode_assembles_without_generation_trio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认模式：缺音乐生成三项也能正常装配（消除 A/R9 过渡态返回 None）。"""
    settings = _isolated_settings(monkeypatch, **_isolated_kwargs())
    service = build_memoir_audio_service(settings, None)
    assert isinstance(service, MemoirAudioService)
    assert service.config.music_generation_enabled is False
    assert service.config.default_bgm_object_key == DEFAULT_SOURCE_KEY
    assert service.config.max_file_bytes == 20_971_520


def test_build_service_generation_mode_still_requires_trio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生成模式：缺三项必填仍按能力关闭（校验不因默认模式分叉放宽）。"""
    kwargs = _isolated_kwargs()
    kwargs["MEMOIR_MUSIC_GENERATION_ENABLED"] = True
    settings = _isolated_settings(monkeypatch, **kwargs)
    assert build_memoir_audio_service(settings, None) is None
    # 三项补齐后装配成功且模式为生成。
    kwargs.update(
        {
            "MEMOIR_MUSIC_ACTION": "GenBGM",
            "MEMOIR_MUSIC_PRICE_PER_SECOND": "0.1",
            "MEMOIR_MUSIC_DOWNLOAD_ALLOWED_HOSTS_JSON": '["toc-host.example.com"]',
        }
    )
    settings = _isolated_settings(monkeypatch, **kwargs)
    service = build_memoir_audio_service(settings, None)
    assert isinstance(service, MemoirAudioService)
    assert service.config.music_generation_enabled is True


def test_config_mode_validation_fork() -> None:
    """默认模式不触达音乐轮询/时长校验；生成模式校验一字不放宽。"""
    # 默认模式：音乐时长/轮询非法也不得装配失败（freeze §6）。
    _config(music_poll_interval_seconds=-1, music_duration_seconds=0)
    # 默认模式缺默认源 key：拒绝（Settings 层基础必填的快照层防线）。
    with pytest.raises(ValueError, match="MEMOIR_AUDIO_CONFIG_INVALID"):
        _config(default_bgm_object_key="")
    # 生成模式：轮询/时长非法仍拒绝（校验不放宽）。
    with pytest.raises(ValueError, match="MEMOIR_AUDIO_CONFIG_INVALID"):
        _config(
            music_generation_enabled=True,
            default_bgm_object_key="",
            music_poll_interval_seconds=0,
        )
