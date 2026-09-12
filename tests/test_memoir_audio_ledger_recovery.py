"""M8 必要修复 R1/R2/R6：音频账本事务边界与 keyed HMAC 回归。

覆盖审查记录三项硬性要求：
1. R1 外发前可靠持久化：Provider 调用（TTS/音乐提交）发生时，
   submitting 状态与费用预留必须已跨事务真实落库（独立 Session 可见）；
   commit 抛错或结果未知时，Provider 调用次数必须为零。
2. R2 上传前持久化稳定 object_key：上传 OSS 前键必须先 commit；上传
   成功后进程立即崩溃（SystemExit 注入，BaseException 穿过服务的
   except Exception 才是真崩溃），新 Session 仍可凭账本定位该对象。
3. R6 keyed HMAC 与完整正文指纹：密钥变化改变摘要；正文尾部变化改变
   该场景全部分段指纹（含文本完全相同的分段）；同正文不同场景资源
   ID 不同；旧 v1 在途账本行不被新指纹复用；账本与日志永不保存
   场景正文与音频 URL。

隔离方式（禁止内存库假隔离）：文件 SQLite + NullPool——每次 Session
checkout 都是独立连接，探针 Session 读到的数据必然来自真实 commit，
而不是与被测服务共享同一事务。不连真实数据库/OSS/供应商。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  # 触发全部模型注册，建表完整
from app.core.config import Settings, validate_memoir_audio_settings
from app.db.sqlalchemy_db import Base
from app.models import AgentRun
from app.models.memoir_audio_job import (
    ROLE_BACKGROUND_MUSIC,
    ROLE_NARRATION,
    ROLE_SEGMENT,
    STATE_FAILED,
    STATE_RESERVED,
    STATE_SUBMITTED,
    STATE_SUBMITTING,
    WORK_SCENE_ID,
    MemoirAudioJob,
    MemoirAudioRunBudget,
)
from app.services.memoir.memoir_audio_jobs import (
    MemoirAudioCostPolicy,
    MemoirAudioJobsError,
    MemoirAudioJobsService,
)
from app.services.memoir.memoir_audio_provider import (
    MemoirAudioProviderError,
    MusicTaskSnapshot,
    TTSSegmentResult,
    split_narration_segments,
)
from app.services.memoir.memoir_audio_service import (
    MemoirAudioConfig,
    MemoirAudioService,
    _RunCtx,
    compute_input_hmac,
)

RUN_ID = "run-r12-recovery"
SCENE_BODY = "那年春天我们在江边老城散步看日落，晚风很轻。"
# 隐私断言用的独特正文标记与音乐临时 URL（只允许出现在内存，不许入库入日志）。
PRIVACY_BODY = "这段旁白正文含有独特暗号词组禁止落库。"
BGM_AUDIO_URL = "https://bgm.example.test/a.mp3"
BGM_TASK_ID = "task-r12-1"


# ---------------------------------------------------------------------------
# 测试替身（与 108 全图测试同形状：只记录安全元数据）
# ---------------------------------------------------------------------------


class FakeTTSClient:
    """TTS 替身：记录调用正文计数；on_call 钩子供 R1 探针注入。"""

    def __init__(self, *, on_call: Any = None) -> None:
        self._speaker = "zh_female_wenroushunv_uranus_bigtts"
        self._speech_rate = -10
        self.calls: list[str] = []
        self._on_call = on_call

    async def synthesize_segment(self, text: str) -> TTSSegmentResult:
        self.calls.append(text)
        if self._on_call is not None:
            self._on_call()
        return TTSSegmentResult(
            audio=b"ID3mp3frame:" + text.encode()[:12],
            text_words=max(1, len(text)),
        )


class FakeMusicClient:
    """音乐替身：默认成功；fail_submit 模拟明确未受理提交失败。"""

    def __init__(self, *, fail_submit: bool = False, on_submit: Any = None) -> None:
        self._action = "GenBGM"
        self.fail_submit = fail_submit
        self.submit_count = 0
        self.query_count = 0
        self._on_submit = on_submit

    async def submit_generation(self) -> str:
        self.submit_count += 1
        if self._on_submit is not None:
            self._on_submit()
        if self.fail_submit:
            raise MemoirAudioProviderError("MUSIC_HTTP_403", "提交被拒")
        return BGM_TASK_ID

    async def query_task(self, task_id: str) -> MusicTaskSnapshot:
        self.query_count += 1
        return MusicTaskSnapshot(status=2, audio_url=BGM_AUDIO_URL)


class FakeUploader:
    """上传替身：on_upload 钩子供 R2 探针；crash_after_record 在上传
    成功后抛 SystemExit（BaseException），模拟上传后进程立即崩溃。"""

    def __init__(self, *, on_upload: Any = None, crash_after_record: bool = False) -> None:
        self.uploads: list[tuple[str, str]] = []
        self._on_upload = on_upload
        self._crash_after_record = crash_after_record

    def upload_private_bytes(self, data: bytes, object_key: str, mime: str) -> None:
        if self._on_upload is not None:
            self._on_upload(object_key)
        self.uploads.append((object_key, mime))
        if self._crash_after_record:
            raise SystemExit("模拟上传成功后进程崩溃")


class FakeTranscoder:
    async def concat_mp3_segments(self, parts: list[bytes]) -> Any:
        return SimpleNamespace(audio=b"joined-mp3", duration_ms=4200)

    async def transcode_to_mp3(self, data: bytes) -> Any:
        return SimpleNamespace(audio=b"bgm-mp3", duration_ms=60000)


class FakeDownloader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def download(self, url: str) -> bytes:
        self.calls.append(url)
        return b"bgm-bytes"


# ---------------------------------------------------------------------------
# harness：文件 SQLite + NullPool，服务与探针各持独立连接
# ---------------------------------------------------------------------------


def _make_run(session: Session) -> AgentRun:
    """最小合法 AgentRun；reserve_job 门禁要求该行存在且 active。"""
    run = AgentRun(
        run_id=RUN_ID,
        agent_id="memoir_agent",
        agent_version="1.0.8",
        package_digest="digest-test",
        contract_version="1.1.0",
        business_type="couple_memory",
        business_id="biz-r12",
        input_json={
            "archive_id": "archive-r12",
            "snapshot_id": "snapshot-r12",
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
        # run_deadline_at 必须在未来：音频预算墙钟上限取该值减发布预留。
        run_deadline_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(run)
    session.commit()
    return run


def _build_harness(
    tmp_path: Any,
    *,
    tts: FakeTTSClient | None = None,
    music: FakeMusicClient | None = None,
    uploader: FakeUploader | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> SimpleNamespace:
    engine = create_engine(
        f"sqlite:///{tmp_path}/audio-ledger.sqlite3",
        connect_args={"timeout": 30},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    run = _make_run(session)
    policy = MemoirAudioCostPolicy(
        currency="CNY",
        tts_price_per_1000_text_words=Decimal("1"),
        music_price_per_second=Decimal("0.1"),
        max_cost_per_run=Decimal("10"),
    )
    # R3 用例经 config_overrides 注入 node_timeout/publish_reserve 组合，
    # 精确控制节点预算（其余配置与既有用例完全一致）。
    config_values: dict[str, Any] = {
        "narrator_prefix": "memoir-test/audios/narrator/",
        "background_prefix": "memoir-test/audios/background/",
        "scope_hmac_key": "runtime-recovery-test-scope-key",
        "input_hmac_key": "runtime-recovery-test-input-key",
        "music_poll_interval_seconds": 0.01,
        "music_duration_seconds": 60,
    }
    config_values.update(config_overrides or {})
    config = MemoirAudioConfig(**config_values)
    service = MemoirAudioService(
        tts_client=tts or FakeTTSClient(),
        music_client=music or FakeMusicClient(),
        uploader=uploader or FakeUploader(),
        transcoder=FakeTranscoder(),
        downloader=FakeDownloader(),
        jobs_service=MemoirAudioJobsService(session, policy),
        config=config,
        session=session,
    )
    return SimpleNamespace(
        engine=engine, factory=factory, session=session, run=run,
        service=service, policy=policy, config=config,
    )


@pytest.fixture
def harness(tmp_path: Any) -> SimpleNamespace:
    h = _build_harness(tmp_path)
    yield h
    h.session.close()
    h.engine.dispose()


def _playback(body: str = SCENE_BODY, scene_id: str = "scene-1") -> dict[str, Any]:
    return {"scenes": [{"scene_id": scene_id, "body": body}]}


def _segment_hmacs(h: SimpleNamespace, scene_id: str = "scene-1") -> set[str]:
    """探针 Session 读取该场景全部分段账本行的指纹集合。"""
    with h.factory() as probe:
        return set(
            probe.execute(
                select(MemoirAudioJob.input_hmac).where(
                    MemoirAudioJob.run_id == RUN_ID,
                    MemoirAudioJob.role == ROLE_SEGMENT,
                    MemoirAudioJob.scene_id == scene_id,
                )
            ).scalars()
        )


# ---------------------------------------------------------------------------
# R1：外发前可靠持久化
# ---------------------------------------------------------------------------


def test_r1_provider_call_sees_submitting_and_reservation_from_independent_session(
    harness: SimpleNamespace,
) -> None:
    """TTS/音乐 Provider 调用瞬间，独立 Session 必须已能看到 submitting
    状态与费用预留——证明外发前账本已真实 commit，而非同 Session 假隔离。"""
    seen: list[dict[str, Any]] = []

    def probe_tts() -> None:
        with harness.factory() as probe:
            seg = probe.execute(
                select(MemoirAudioJob).where(
                    MemoirAudioJob.run_id == RUN_ID,
                    MemoirAudioJob.role == ROLE_SEGMENT,
                )
            ).scalar_one()
            budget = probe.execute(
                select(MemoirAudioRunBudget).where(
                    MemoirAudioRunBudget.run_id == RUN_ID
                )
            ).scalar_one()
            seen.append(
                {
                    "kind": "tts",
                    "state": seg.state,
                    "reserved_cost": seg.reserved_cost,
                    "budget_reserved": budget.reserved_total_cost,
                }
            )

    def probe_music() -> None:
        with harness.factory() as probe:
            bgm = probe.execute(
                select(MemoirAudioJob).where(
                    MemoirAudioJob.run_id == RUN_ID,
                    MemoirAudioJob.role == ROLE_BACKGROUND_MUSIC,
                    MemoirAudioJob.scene_id == WORK_SCENE_ID,
                )
            ).scalar_one()
            seen.append(
                {
                    "kind": "music",
                    "state": bgm.state,
                    "provider_task_id": bgm.provider_task_id,
                }
            )

    harness.service._tts = FakeTTSClient(on_call=probe_tts)
    harness.service._music = FakeMusicClient(on_submit=probe_music)
    result = harness.service.generate(harness.run, _playback())

    tts_seen = [entry for entry in seen if entry["kind"] == "tts"]
    music_seen = [entry for entry in seen if entry["kind"] == "music"]
    assert len(tts_seen) == 1 and len(music_seen) == 1
    # TTS 调用瞬间：分段行已处于 submitting 且费用预留跨 Session 可见。
    assert tts_seen[0]["state"] == STATE_SUBMITTING
    assert tts_seen[0]["reserved_cost"] > 0
    assert tts_seen[0]["budget_reserved"] > 0
    # 音乐提交瞬间：配乐行已处于 submitting，TaskID 尚未产生。
    assert music_seen[0]["state"] == STATE_SUBMITTING
    assert music_seen[0]["provider_task_id"] is None
    # 全链路正常完成：旁白与配乐都已上传。
    assert len(result["narrations"]) == 1
    assert result["background_music"] is not None
    assert len(harness.service._uploader.uploads) == 2


def test_r1_commit_failure_blocks_all_provider_calls(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """commit 抛错：所有 Provider 调用必须为零（不允许未持久化外发）。"""

    def broken_commit() -> None:
        raise RuntimeError("注入：账本 commit 抛错")

    monkeypatch.setattr(harness.session, "commit", broken_commit)
    result = harness.service.generate(harness.run, _playback())

    assert harness.service._tts.calls == []
    assert harness.service._music.submit_count == 0
    assert harness.service._uploader.uploads == []
    assert result == {"narrations": [], "background_music": None}


def test_r1_commit_outcome_unknown_blocks_provider_calls(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """commit 先真实落库再抛错（结果未知）：服务必须按失败处理，
    Provider 调用为零；同时独立 Session 能看到已持久化的 submitting 行。"""
    real_commit = harness.session.commit

    def commit_then_raise() -> None:
        real_commit()
        raise RuntimeError("注入：提交结果未知")

    monkeypatch.setattr(harness.session, "commit", commit_then_raise)
    result = harness.service.generate(harness.run, _playback())

    assert harness.service._tts.calls == []
    assert harness.service._music.submit_count == 0
    assert harness.service._uploader.uploads == []
    assert result == {"narrations": [], "background_music": None}
    # 结果未知 ≠ 未落库：新 Session 仍能看到 submitting 与费用预留，
    # 维护对账可以据此收敛，不会产生不可追踪的计费。
    # 事件循环调度顺序不保证哪个单元先撞上 commit 门禁（BGM 或分段），
    # 只断言账本存在已持久化的 submitting 行且预算预留非零。
    with harness.factory() as probe:
        submitting = (
            probe.execute(
                select(MemoirAudioJob).where(
                    MemoirAudioJob.run_id == RUN_ID,
                    MemoirAudioJob.state == STATE_SUBMITTING,
                )
            )
            .scalars()
            .all()
        )
        budget = probe.execute(
            select(MemoirAudioRunBudget).where(
                MemoirAudioRunBudget.run_id == RUN_ID
            )
        ).scalar_one()
    assert submitting and all(row.reserved_cost > 0 for row in submitting)
    assert budget.reserved_total_cost > 0


# ---------------------------------------------------------------------------
# R2：上传前持久化稳定 object_key
# ---------------------------------------------------------------------------


def test_r2_object_key_committed_before_upload_visible_to_independent_session(
    tmp_path: Any,
) -> None:
    """上传进行中（on_upload 回调瞬间），独立 Session 必须已能按
    object_key 查到账本行——键先落库、后上传。"""
    probed: list[dict[str, Any]] = []

    def probe_uploading(object_key: str) -> None:
        with harness.factory() as probe:
            row = probe.execute(
                select(MemoirAudioJob).where(
                    MemoirAudioJob.run_id == RUN_ID,
                    MemoirAudioJob.object_key == object_key,
                )
            ).scalar_one()
            probed.append(
                {
                    "object_key": object_key,
                    "row_state": row.state,
                    "mime": row.mime,
                    "duration_ms": row.duration_ms,
                }
            )

    # BGM 明确未受理失败：唯一上传是旁白，断言保持确定性。
    harness = _build_harness(
        tmp_path,
        music=FakeMusicClient(fail_submit=True),
        uploader=FakeUploader(on_upload=probe_uploading),
    )
    try:
        result = harness.service.generate(harness.run, _playback())
        assert len(probed) == 1
        entry = probed[0]
        assert entry["object_key"].startswith("memoir-test/audios/narrator/")
        assert entry["row_state"] == STATE_RESERVED  # 上传未回写完成态
        assert entry["mime"] == "audio/mpeg"
        assert entry["duration_ms"] is None  # record_upload 尚未执行
        assert len(result["narrations"]) == 1
    finally:
        harness.session.close()
        harness.engine.dispose()


def test_r2_crash_after_upload_is_recoverable_from_ledger(tmp_path: Any) -> None:
    """上传成功后进程立即崩溃（SystemExit）：新 Session 凭账本能定位
    对象键做对账，不产生不可追踪对象。"""
    harness = _build_harness(
        tmp_path,
        music=FakeMusicClient(fail_submit=True),  # 唯一上传是旁白
        uploader=FakeUploader(crash_after_record=True),
    )
    try:
        # SystemExit 是 BaseException，穿过服务的 except Exception——真崩溃。
        with pytest.raises(SystemExit):
            harness.service.generate(harness.run, _playback())
        # 上传确实发生后才崩溃。
        assert len(harness.service._uploader.uploads) == 1
        uploaded_key = harness.service._uploader.uploads[0][0]
        # 重启后（新 Session/新连接）账本可见 object_key，可对账定位对象。
        with harness.factory() as probe:
            seg_rows = probe.execute(
                select(MemoirAudioJob).where(
                    MemoirAudioJob.run_id == RUN_ID,
                    MemoirAudioJob.role == ROLE_SEGMENT,
                )
            ).scalars().all()
            narr = probe.execute(
                select(MemoirAudioJob).where(
                    MemoirAudioJob.run_id == RUN_ID,
                    MemoirAudioJob.object_key == uploaded_key,
                )
            ).scalar_one()
        # 分段已结算（TTS 完成且结算已 commit），崩溃发生在最终资产回写前。
        assert seg_rows and all(row.state == STATE_SUBMITTED for row in seg_rows)
        assert narr.state == STATE_RESERVED  # record_upload 未执行
        assert narr.mime == "audio/mpeg"
    finally:
        harness.session.close()
        harness.engine.dispose()


# ---------------------------------------------------------------------------
# R6：keyed HMAC 与完整正文指纹
# ---------------------------------------------------------------------------


def test_r6_key_change_changes_digest() -> None:
    """密钥参与摘要：换 key 必换摘要；同 key 确定性；载荷变化换摘要。"""
    payload: dict[str, Any] = {
        "role": ROLE_SEGMENT,
        "segment_index": 0,
        "text": SCENE_BODY,
        "full_body_hmac": "abc",
    }
    digest_a = compute_input_hmac("key-a", payload)
    assert digest_a != compute_input_hmac("key-b", payload)
    assert digest_a == compute_input_hmac("key-a", payload)
    assert digest_a != compute_input_hmac(
        "key-a", {**payload, "segment_index": 1}
    )
    assert digest_a != compute_input_hmac("key-a", {**payload, "text": SCENE_BODY + "尾"})


def test_r6_full_body_tail_change_changes_all_segment_fingerprints(
    tmp_path: Any,
) -> None:
    """完整正文尾部变化必须改变该场景所有段指纹——包括文本完全相同的
    分段（靠 full_body_hmac 参与 canonical 载荷实现）。"""
    sent = "老城江边的晚风把路灯的光揉碎成一片流动的金色。"  # 23 字符
    body1 = sent * 8  # 184 字符 → 单段
    body2 = sent * 9  # 207 字符 → 两段，第一段与 body1 的段文本相同
    segs1 = split_narration_segments(body1)
    segs2 = split_narration_segments(body2)
    # 前置条件：分段规则保证第一段文本完全相同（差异只能在尾部）。
    assert segs1 == [sent * 8]
    assert segs2 == [sent * 8, sent]

    harness = _build_harness(tmp_path, music=FakeMusicClient(fail_submit=True))
    try:
        run = harness.run
        harness.service.generate(run, _playback(body1))
        first = _segment_hmacs(harness)
        assert len(first) == 1
        harness.service.generate(run, _playback(body2))
        after = _segment_hmacs(harness)
        # 第二次执行的全部分段指纹均为新键：无一复用第一次的指纹，
        # 含文本相同的第一段——证明完整正文身份参与了每段指纹。
        assert len(after) == len(first) + len(segs2)
        assert not (first & after - first)
        assert first < after
    finally:
        harness.session.close()
        harness.engine.dispose()


def test_r6_same_body_different_scenes_get_distinct_resource_ids(
    tmp_path: Any,
) -> None:
    """两个正文相同的不同场景：资源 ID 与对象键必须互不相同。"""
    harness = _build_harness(tmp_path, music=FakeMusicClient(fail_submit=True))
    try:
        doc = {
            "scenes": [
                {"scene_id": "scene-a", "body": SCENE_BODY},
                {"scene_id": "scene-b", "body": SCENE_BODY},
            ]
        }
        result = harness.service.generate(harness.run, doc)
        narrations = result["narrations"]
        assert len(narrations) == 2
        media_ids = {str(entry["media_id"]) for entry in narrations}
        object_keys = {str(entry["object_key"]) for entry in narrations}
        assert len(media_ids) == 2
        assert len(object_keys) == 2
    finally:
        harness.session.close()
        harness.engine.dispose()


def test_r6_old_v1_inflight_rows_are_not_reused_by_fingerprint(
    harness: SimpleNamespace,
) -> None:
    """旧 v1（无密钥 SHA256）在途账本行：切换 keyed HMAC 后指纹必然
    失配——新执行创建新行提交（TTS 被调用），旧行原样保留交维护对账。"""
    old_digest = hashlib.sha256(SCENE_BODY.encode()).hexdigest()
    with harness.factory() as writer:
        writer.add(
            MemoirAudioJob(
                job_id="audio-legacy-v1-0001",
                business_id="biz-r12",
                run_id=RUN_ID,
                generation_epoch=0,
                package_version="1.0.8",
                role=ROLE_SEGMENT,
                scene_id="scene-1",
                segment_index=0,
                input_hmac=old_digest,
                attempt=1,
                state=STATE_SUBMITTED,
                currency="CNY",
                reserved_cost=Decimal("0.5"),
                settled_cost=None,
                lease_owner="audio:legacy",
                lease_token=1,
                expires_at=datetime.now(UTC) + timedelta(seconds=90),
            )
        )
        writer.commit()

    result = harness.service.generate(harness.run, _playback())
    # 新 v2 摘要与旧 v1 摘要不可能相等：指纹失配 → 新行创建并真实调用
    # TTS（若被复用，槽位在途判定会直接降级、不外发）。
    assert harness.service._tts.calls == [SCENE_BODY]
    assert len(result["narrations"]) == 1
    hmacs = _segment_hmacs(harness)
    # 旧 v1 行按设计原样保留在账本（交维护对账收敛），与新建的 v2 行并存。
    assert old_digest in hmacs
    assert len(hmacs) == 2
    # 旧在途行不被指纹复用、不被复活或结算：按未知提交交对账收敛。
    with harness.factory() as probe:
        old_row = probe.execute(
            select(MemoirAudioJob).where(
                MemoirAudioJob.job_id == "audio-legacy-v1-0001"
            )
        ).scalar_one()
    assert old_row.state == STATE_SUBMITTED
    assert old_row.attempt == 1
    assert old_row.settled_cost is None


def test_r6_ledger_never_stores_body_or_urls(
    harness: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """隐私铁律：账本任何字符串列与全部日志不得出现场景正文、音频
    URL 或 TaskID（TaskID 允许在账本 provider_task_id 列，绝不进日志）。"""
    with caplog.at_level(logging.INFO):
        result = harness.service.generate(harness.run, _playback(PRIVACY_BODY))
    assert len(result["narrations"]) == 1
    assert result["background_music"] is not None

    string_columns = [
        column.key
        for column in sa.inspect(MemoirAudioJob).columns
        if isinstance(column.type, sa.String)
    ]
    with harness.factory() as probe:
        rows = probe.execute(select(MemoirAudioJob)).scalars().all()
        budgets = probe.execute(select(MemoirAudioRunBudget)).scalars().all()
    for row in [*rows, *budgets]:
        for name in string_columns:
            value = getattr(row, name, None)
            if value is None:
                continue
            assert PRIVACY_BODY not in value, f"{name} 泄漏正文"
            assert BGM_AUDIO_URL not in value, f"{name} 泄漏音频 URL"
            assert "://" not in value, f"{name} 出现 URL"
    # 日志只允许安全元数据：正文、URL、TaskID 均不得出现。
    assert PRIVACY_BODY not in caplog.text
    assert BGM_AUDIO_URL not in caplog.text
    assert BGM_TASK_ID not in caplog.text


# ---------------------------------------------------------------------------
# R6 配置门禁：MEMOIR_AUDIO_INPUT_HMAC_KEY 必填
# ---------------------------------------------------------------------------


def _audio_settings_kwargs() -> dict[str, Any]:
    """启用音频所需的全部合法字段（与 108 装配测试同集合）。"""
    return {
        "MEMOIR_AUDIO_ENABLED": True,
        "MEMOIR_TTS_API_KEY": "test-only-key",
        "VOLCANO_CV_ACCESS_KEY": "test-ak",
        "VOLCANO_CV_SECRET_KEY": "test-sk",
        "MEMOIR_MUSIC_ACTION": "GenBGM",
        "MEMOIR_TTS_PRICE_PER_1000_TEXT_WORDS": "1",
        "MEMOIR_MUSIC_PRICE_PER_SECOND": "0.1",
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
        # M8 默认配乐（freeze 2026-09-11 §11.4）：默认源 key 仅默认模式
        # 必填（生成模式不强制）。本 kwargs 未开生成开关 → 默认模式，
        # 缺该键会被成组校验拒绝。
        "MEMOIR_DEFAULT_BGM_OBJECT_KEY": "memoir-test/audios/default/memoirs.mp3",
        "MEMOIR_MUSIC_DOWNLOAD_ALLOWED_HOSTS_JSON": '["toc-host.example.com"]',
    }


def test_r6_settings_require_input_hmac_key_when_audio_enabled() -> None:
    """音频启用时新密钥必填非空；音频关闭时不校验（默认关闭零破坏）。

    `_env_file=None` 隔离 dotenv：否则开发者本机 .env.*.local 会回填被
    pop 掉的密钥（历史环境泄漏红），测试语义依赖显式传入的 kwargs。
    """
    # 完整配置：校验通过。
    validate_memoir_audio_settings(
        Settings(_env_file=None, **_audio_settings_kwargs())
    )
    # 缺 MEMOIR_AUDIO_INPUT_HMAC_KEY：成组校验必须拒绝。
    kwargs = _audio_settings_kwargs()
    kwargs.pop("MEMOIR_AUDIO_INPUT_HMAC_KEY")
    with pytest.raises(ValueError, match="MEMOIR_AUDIO_INPUT_HMAC_KEY"):
        validate_memoir_audio_settings(Settings(_env_file=None, **kwargs))
    # 音频默认关闭：缺密钥不影响启动（向后兼容铁律）。
    kwargs["MEMOIR_AUDIO_ENABLED"] = False
    validate_memoir_audio_settings(Settings(**kwargs))


# ---------------------------------------------------------------------------
# R3：时间上限真正约束外层返回 + 迟到副作用受控
# ---------------------------------------------------------------------------


class SlowUploader(FakeUploader):
    """R3 慢上传替身：上传线程内 sleep 指定秒数，记录线程对象与
    started/finished 事件，供"节点返回不等待线程"与"迟到写入被 fence"
    两类断言使用。只记录安全元数据，不触碰账本。"""

    def __init__(self, delay_seconds: float) -> None:
        super().__init__()
        self.delay_seconds = delay_seconds
        self.thread: threading.Thread | None = None
        self.started = threading.Event()
        self.finished = threading.Event()

    def upload_private_bytes(self, data: bytes, object_key: str, mime: str) -> None:
        self.thread = threading.current_thread()
        self.started.set()
        time.sleep(self.delay_seconds)
        self.uploads.append((object_key, mime))
        self.finished.set()


def _no_residual_upload_threads() -> bool:
    """节点专用上传线程池已无存活线程（按线程名前缀判定）。"""
    return not any("memoir-audio-upload" in t.name for t in threading.enumerate())


def test_r3_slow_upload_does_not_block_node_return_and_late_write_is_fenced(
    tmp_path: Any,
) -> None:
    """慢上传不得拖住节点外层返回（断言外层 generate 本身，而非只断言
    内层协程抛 TimeoutError）；返回后账本无半开状态；慢线程结束后，
    旧 token 的迟到 record_upload 被 R4 fencing 原子拒绝。"""
    slow = SlowUploader(1.2)
    harness = _build_harness(
        tmp_path,
        music=FakeMusicClient(fail_submit=True),  # BGM 明确未受理：唯一慢上传是旁白
        uploader=slow,
        # 预算 = 0.40 - 0.05 = 0.35s，远小于 1.2s 上传耗时。
        config_overrides={"node_timeout_seconds": 0.40, "publish_reserve_seconds": 0.05},
    )
    try:
        t0 = time.monotonic()
        result = harness.service.generate(harness.run, _playback())
        elapsed = time.monotonic() - t0
        # 外层 generate 提前返回（远小于上传时长），按既有降级分支发布图文。
        assert result == {"narrations": [], "background_music": None}
        assert elapsed < 0.7
        # 返回瞬间上传线程仍在跑：节点返回与线程耗时彻底解耦。
        assert slow.started.wait(timeout=2.0)
        assert slow.thread is not None and slow.thread.is_alive()
        # 账本无半开状态：object_key 已先行持久化（R2），状态停留 reserved，
        # duration_ms 未回写——超时后本执行不再写该行。
        with harness.factory() as probe:
            narr = probe.execute(
                select(MemoirAudioJob).where(
                    MemoirAudioJob.run_id == RUN_ID,
                    MemoirAudioJob.role == ROLE_NARRATION,
                )
            ).scalar_one()
        assert narr.state == STATE_RESERVED
        assert narr.object_key is not None
        assert narr.duration_ms is None

        # 慢线程最终自行跑完（无人在等它）。
        assert slow.finished.wait(timeout=5.0)
        if slow.thread is not None:
            slow.thread.join(timeout=5.0)

        # 恢复 worker 旋转 lease（owner 必须匹配 run 当前 execution_attempt=1）。
        with harness.factory() as recovery:
            jobs_recovery = MemoirAudioJobsService(recovery, harness.policy)
            row = recovery.execute(
                select(MemoirAudioJob).where(
                    MemoirAudioJob.run_id == RUN_ID,
                    MemoirAudioJob.role == ROLE_NARRATION,
                )
            ).scalar_one()
            job_id, old_token = row.job_id, row.lease_token
            rotated = jobs_recovery.rotate_lease(
                job_id, old_token,
                owner=f"audio:{RUN_ID}:attempt-1", ttl_seconds=90.0,
            )
            recovery.commit()
        assert rotated.lease_token == old_token + 1
        # 迟到写入（旧 token 的 record_upload，模拟旧 worker 补写）被拒。
        with pytest.raises(MemoirAudioJobsError) as excinfo:
            harness.service._jobs.record_upload(job_id, old_token, duration_ms=4200)
        assert excinfo.value.code == "MEMOIR_AUDIO_FENCING_REJECTED"
        # 终态口径：行仍停留 reserved、时长未回写，交维护对账收敛。
        with harness.factory() as probe:
            after = probe.execute(
                select(MemoirAudioJob).where(MemoirAudioJob.job_id == job_id)
            ).scalar_one()
        assert after.state == STATE_RESERVED
        assert after.duration_ms is None
        assert after.lease_token == old_token + 1

        # C1：持键 reserved 走 reaper + 维护链可收敛（不扩 _ORPHAN_STATES）。
        from app.scripts.memoir_audio_maintenance import run_maintenance

        class _Del:
            def __init__(self) -> None:
                self.deleted: list[str] = []

            def delete_object(self, object_key: str) -> bool:
                self.deleted.append(object_key)
                return True

        reap_now = datetime.now(UTC) + timedelta(hours=25)
        deleter = _Del()
        with harness.factory() as maint:
            jobs = MemoirAudioJobsService(maint, harness.policy)
            assert jobs.fail_abandoned_keyed_jobs(
                now=reap_now, grace_seconds=24 * 3600,
            ) == 1
            report = run_maintenance(
                maint,
                oss_deleter=deleter,
                retention_hours=24,
                limit=100,
                execute=True,
                now=reap_now,
                publish_probe=lambda job: None,
            )
            maint.commit()
        assert report.deleted == 1
        with harness.factory() as probe:
            cleaned = probe.execute(
                select(MemoirAudioJob).where(MemoirAudioJob.job_id == job_id)
            ).scalar_one()
        assert cleaned.state == "cleaned"
        assert cleaned.error_code == "AUDIO_LEASE_ABANDONED"

        # 上传线程池无残留线程（shutdown(wait=False) 后线程自行退出）。
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not _no_residual_upload_threads():
            time.sleep(0.02)
        assert _no_residual_upload_threads()
    finally:
        harness.session.close()
        harness.engine.dispose()


def test_r3_external_cancel_of_upload_returns_without_waiting_thread(
    tmp_path: Any,
) -> None:
    """外部取消：等待上传的协程即时响应 CancelledError，线程池
    shutdown(wait=False) 即刻返回，节点返回不等待在途线程。"""
    slow = SlowUploader(1.0)
    harness = _build_harness(
        tmp_path, music=FakeMusicClient(fail_submit=True), uploader=slow,
    )
    # 手工构造节点执行上下文（与 generate() 内部同一创建口径）。
    ctx = _RunCtx(
        deadline=time.monotonic() + 5.0,
        executor=ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="memoir-audio-upload"
        ),
    )
    try:
        refs = harness.service._resolve_refs(harness.run)
        assert refs is not None
        timings: dict[str, float] = {}
        alive_at_return = False

        async def scenario() -> None:
            nonlocal alive_at_return
            task = asyncio.create_task(
                harness.service._upload_audio_bytes(
                    ctx, refs, b"mp3-bytes", "memoir-test/audios/narrator/cancel"
                )
            )
            # create_task 后必须让出循环再轮询：同协程内阻塞等待会饿死任务。
            poll_deadline = time.monotonic() + 2.0
            while not slow.started.is_set() and time.monotonic() < poll_deadline:
                await asyncio.sleep(0.005)
            assert slow.started.is_set()
            t_cancel = time.monotonic()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # 预期：外部取消即时生效
            timings["cancel"] = time.monotonic() - t_cancel
            alive_at_return = slow.thread is not None and slow.thread.is_alive()
            t_shutdown = time.monotonic()
            ctx.executor.shutdown(wait=False, cancel_futures=True)
            timings["shutdown"] = time.monotonic() - t_shutdown

        t0 = time.monotonic()
        asyncio.run(scenario())
        timings["outer"] = time.monotonic() - t0
        # 取消/关池/外层返回全部即时（远小于 1.0s 上传），线程未被等待。
        assert timings["cancel"] < 0.15
        assert timings["shutdown"] < 0.1
        assert timings["outer"] < 0.5
        assert alive_at_return
        # 线程最终自行结束，无残留执行器线程。
        assert slow.finished.wait(timeout=5.0)
        if slow.thread is not None:
            slow.thread.join(timeout=5.0)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not _no_residual_upload_threads():
            time.sleep(0.02)
        assert _no_residual_upload_threads()
    finally:
        ctx.executor.shutdown(wait=False, cancel_futures=True)
        harness.session.close()
        harness.engine.dispose()


def test_r3_near_zero_budget_returns_promptly_without_upload(tmp_path: Any) -> None:
    """预算近乎为零（0.20 - 0.19 = 0.01s）：首个提交即被时间门禁拦下，
    节点快速降级返回，上传从未发生。"""
    slow = SlowUploader(0.5)
    harness = _build_harness(
        tmp_path,
        music=FakeMusicClient(fail_submit=True),
        uploader=slow,
        config_overrides={"node_timeout_seconds": 0.20, "publish_reserve_seconds": 0.19},
    )
    try:
        t0 = time.monotonic()
        result = harness.service.generate(harness.run, _playback())
        elapsed = time.monotonic() - t0
        assert result == {"narrations": [], "background_music": None}
        assert elapsed < 0.5
        # 上传从未进入专用线程池。
        assert slow.uploads == []
    finally:
        harness.session.close()
        harness.engine.dispose()


class BoomUploader(FakeUploader):
    """非超时交付失败：触发 §2.1 mark_failed，不走 reaper。"""

    def upload_private_bytes(self, data: bytes, object_key: str, mime: str) -> None:
        self.uploads.append((object_key, mime))
        raise RuntimeError("oss down")


def test_narration_upload_exception_marks_failed_without_timeout(
    tmp_path: Any,
) -> None:
    """旁白上传非超时异常 → AUDIO_UPLOAD_FAILED，进入孤儿态。"""
    harness = _build_harness(
        tmp_path,
        music=FakeMusicClient(fail_submit=True),
        uploader=BoomUploader(),
    )
    try:
        result = harness.service.generate(harness.run, _playback())
        assert result == {"narrations": [], "background_music": None}
        with harness.factory() as probe:
            narr = probe.execute(
                select(MemoirAudioJob).where(
                    MemoirAudioJob.run_id == RUN_ID,
                    MemoirAudioJob.role == ROLE_NARRATION,
                )
            ).scalar_one()
        assert narr.state == STATE_FAILED
        assert narr.error_code == "AUDIO_UPLOAD_FAILED"
        assert narr.object_key is not None
    finally:
        harness.session.close()
        harness.engine.dispose()


def test_bgm_timeout_late_write_fenced_then_mark_failed_classifies(
    tmp_path: Any,
) -> None:
    """BGM 超时→迟到写入被 fence→mark_failed→维护分类（含键保留/不含键可删）。"""
    from app.scripts.memoir_audio_maintenance import run_maintenance

    slow = SlowUploader(1.2)
    harness = _build_harness(
        tmp_path,
        uploader=slow,
        config_overrides={
            "node_timeout_seconds": 0.40,
            "publish_reserve_seconds": 0.05,
        },
    )
    try:
        result = harness.service.generate(harness.run, {"scenes": []})
        assert result == {"narrations": [], "background_music": None}
        assert slow.started.wait(timeout=2.0)
        assert slow.finished.wait(timeout=5.0)
        if slow.thread is not None:
            slow.thread.join(timeout=5.0)

        with harness.factory() as probe:
            bgm = probe.execute(
                select(MemoirAudioJob).where(
                    MemoirAudioJob.run_id == RUN_ID,
                    MemoirAudioJob.role == ROLE_BACKGROUND_MUSIC,
                )
            ).scalar_one()
        assert bgm.state in (STATE_SUBMITTED, STATE_RESERVED)
        assert bgm.object_key is not None
        assert bgm.duration_ms is None
        object_key = bgm.object_key
        job_id, old_token = bgm.job_id, bgm.lease_token

        with harness.factory() as recovery:
            jobs_recovery = MemoirAudioJobsService(recovery, harness.policy)
            rotated = jobs_recovery.rotate_lease(
                job_id, old_token,
                owner=f"audio:{RUN_ID}:attempt-1", ttl_seconds=90.0,
            )
            recovery.commit()
        assert rotated.lease_token == old_token + 1
        with pytest.raises(MemoirAudioJobsError) as excinfo:
            harness.service._jobs.record_upload(job_id, old_token, duration_ms=60000)
        assert excinfo.value.code == "MEMOIR_AUDIO_FENCING_REJECTED"
        # 超时路径不 mark_failed（TimeoutError 被吸收）；过 grace 后由 reaper 收割。
        with harness.factory() as probe:
            still = probe.execute(
                select(MemoirAudioJob).where(MemoirAudioJob.job_id == job_id)
            ).scalar_one()
        assert still.state != STATE_FAILED
        assert still.object_key == object_key

        class _Del:
            def __init__(self) -> None:
                self.deleted: list[str] = []

            def delete_object(self, object_key: str) -> bool:
                self.deleted.append(object_key)
                return True

        reap_now = datetime.now(UTC) + timedelta(hours=25)
        with harness.factory() as maint:
            report = run_maintenance(
                maint,
                oss_deleter=_Del(),
                retention_hours=24,
                limit=100,
                execute=True,
                now=reap_now,
                publish_probe=lambda job: {"audio_object_keys": [object_key]},
            )
            maint.commit()
        assert report.keep_published == 1
        assert report.deleted == 0
        with harness.factory() as probe:
            reaped = probe.execute(
                select(MemoirAudioJob).where(MemoirAudioJob.job_id == job_id)
            ).scalar_one()
        assert reaped.state == STATE_FAILED
        assert reaped.error_code == "AUDIO_LEASE_ABANDONED"

        deleter = _Del()
        with harness.factory() as maint:
            report = run_maintenance(
                maint,
                oss_deleter=deleter,
                retention_hours=24,
                limit=100,
                execute=True,
                now=reap_now,
                publish_probe=lambda job: {"audio_object_keys": []},
            )
            maint.commit()
        assert report.delete_candidates == 1
        assert report.deleted == 1
        assert deleter.deleted == [object_key]
        with harness.factory() as probe:
            cleaned = probe.execute(
                select(MemoirAudioJob).where(MemoirAudioJob.job_id == job_id)
            ).scalar_one()
        assert cleaned.state == "cleaned"
        assert cleaned.error_code == "AUDIO_LEASE_ABANDONED"

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not _no_residual_upload_threads():
            time.sleep(0.02)
        assert _no_residual_upload_threads()
    finally:
        harness.session.close()
        harness.engine.dispose()
