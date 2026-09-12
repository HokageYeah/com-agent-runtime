"""1.0.8 回归：memoir_agent@1.0.8 完整工作流图（含音频节点）端到端执行。

1.0.8 图在 1.0.7 十节点 DAG 的 safety_review 与 publish_document 之间插入
enqueue_audio_tasks（十一节点 DAG）。本文件把真实 1.0.8 graph 交给
WorkflowExecutor + 真实 MemoirNodeRunner 执行，模型层在网关边界打桩（与
runtime_test_memoir_106_full_graph.py 同一模式），音频层在供应商/上传边界
打桩（真实 MemoirAudioService + 真实 MemoirAudioJobsService 账本 + SQLite），
覆盖设计口径：全量成功发布 2.0.0 有声文档 / 能力关闭空音频 / 单场景降级 /
全音频失败仍单次发布图文 / 图文耗尽 Run 预算整体跳过 / 音频中途取消停止
新写入 / 崩溃恢复复用资产不重复扣费 / Worker 装配开关。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.db.sqlalchemy_db import Base
from app.models import AgentDefinition, AgentPlan, AgentRun
from app.models.memoir_audio_job import (
    ROLE_BACKGROUND_MUSIC,
    ROLE_NARRATION,
    ROLE_SEGMENT,
    STATE_SUBMISSION_UNKNOWN,
    STATE_SUBMITTED,
    STATE_UPLOADED,
    MemoirAudioJob,
    MemoirAudioRunBudget,
)
from app.runtime.artifact import ArtifactStore
from app.runtime.checkpoint import CheckpointStore, FernetCheckpointCipher
from app.runtime.executor import WorkflowExecutor
from app.runtime.interfaces import LeaseContext
from app.services.agent_package_service import AgentPackageService
from app.services.memoir.memoir_audio_jobs import (
    MemoirAudioCostPolicy,
    MemoirAudioJobsService,
    estimate_music_cost,
    estimate_tts_cost,
)
from app.services.memoir.memoir_audio_provider import (
    MemoirAudioProviderError,
    MusicTaskSnapshot,
    TTSSegmentResult,
)
from app.services.memoir.memoir_audio_service import (
    MemoirAudioConfig,
    MemoirAudioService,
)
from app.services.memoir.memoir_audio_storage import compute_audio_scope
from app.services.tool_call_audit_service import ToolCallAuditService

# Run 冻结能力快照：与 1.0.8 agent.yaml 权威值一致（max_run_seconds 300→1200）。
CAPABILITY_SNAPSHOT = {
    "model_policy": {"max_model_calls": 12, "max_tokens": 150_000, "max_model_cost": 3.0},
    "execution_policy": {"max_run_seconds": 1200, "max_steps": 32},
}

SNAPSHOT_PAYLOAD = {
    "materials": [
        {
            "material_type": "diary", "source_ref": "diary:d1",
            "sanitized_payload": {"text_digest": "我们在江边散步看日落，聊到很晚的具体画面。"},
        },
        {
            "material_type": "completed_bet", "source_ref": "completed_bet:b1",
            "sanitized_payload": {"text_digest": "赌约是谁先跑完五公里，输的人做一周早餐。"},
        },
    ],
}

RUN_ID = "memoir-108-full"
SCOPE_KEY = "runtime-108-test-scope-key"
NARRATOR_PREFIX = "memoir-test/audios/narrator/"
BACKGROUND_PREFIX = "memoir-test/audios/background/"
# 由 compute_audio_scope 现算（与 R6 实现同源），不硬编码摘要。
EXPECTED_SCOPE = compute_audio_scope(
    SCOPE_KEY,
    business_id="archive", archive_id="archive", run_id=RUN_ID, generation_epoch=0,
)

NARRATION_KEYS = {"scene_id", "media_id", "object_key", "mime", "duration_ms"}
BGM_KEYS = {"media_id", "object_key", "mime", "duration_ms"}

COVER_BODY = "那年春天我们在江边老城散步看日落。"
DIARY_BODY = "日记里写下的江边日落与晚风。"
BET_BODY = "赌约是谁先跑完五公里，输的人做了一周早餐。"


def _scene(scene_id: str, scene_type: str, refs: list[str], body: str) -> dict[str, object]:
    return {
        "scene_id": scene_id, "scene_type": scene_type,
        "source_refs": list(refs), "body": body,
    }


def _batch_payload(scenes: list[dict[str, object]]) -> str:
    return json.dumps({"scenes": scenes}, ensure_ascii=False)


def _default_scene_outputs() -> dict[str, list[object]]:
    return {
        "generate_scene_batch": [_batch_payload([
            _scene("s1-1", "cover", ["diary:d1"], COVER_BODY),
            _scene("s1-2", "diary_highlight", ["diary:d1"], DIARY_BODY),
            _scene("s1-3", "summary", ["completed_bet:b1"], BET_BODY),
        ])],
    }


class ScriptedModelGateway:
    """按 node_id 脚本化模型输出（与 106 回归同一模式）。"""

    def __init__(self, outputs: dict[str, list[object]]) -> None:
        self._outputs = {key: list(value) for key, value in outputs.items()}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
        self.calls.append((node_id, request))
        queue = self._outputs.get(node_id)
        output = queue.pop(0) if queue else None
        if output is None:
            return SimpleNamespace(status="failed", data=None)
        return SimpleNamespace(status="succeeded", data=output)


class PublishingToolGateway:
    """受控业务工具替身：get_snapshot 返回合成快照，publish 记录发布文档。"""

    def __init__(self, snapshot_payload: dict[str, object]) -> None:
        self._snapshot_payload = snapshot_payload
        self.published_documents: list[dict[str, object]] = []

    def get_snapshot(self, *args: object) -> dict[str, object]:
        return self._snapshot_payload

    def publish_playback_document(
        self, *args: object, **kwargs: object,
    ) -> dict[str, object]:
        self.published_documents.append(args[5])  # type: ignore[index]
        return {"revision": 1, "content_digest": "published-digest"}

    def get_publish_result(self, *args: object, **kwargs: object) -> dict[str, object]:
        # **kwargs：生产 publish 节点以 tool_context= 关键字传参（query-after-commit
        # 对账路径）；替身签名缺 **kwargs 曾使恢复用例在 publish_document 节点
        # 抛 TypeError → WORKFLOW_NODE_FAILED（历史红，本批修复）。
        return {"revision": 1, "content_digest": "published-digest"}


class FakeTTSClient:
    """同步 SSE TTS 桩：短正文一段一句；可按分段文本精确失败或全量失败。"""

    def __init__(
        self,
        *,
        fail_texts: frozenset[str] = frozenset(),
        fail_all: bool = False,
        on_call: Any = None,
    ) -> None:
        self._speaker = "zh_female_wenroushunv_uranus_bigtts"
        self._speech_rate = -10
        self._fail_texts = set(fail_texts)
        self._fail_all = fail_all
        self._on_call = on_call
        self.calls: list[str] = []

    async def synthesize_segment(self, text: str) -> TTSSegmentResult:
        self.calls.append(text)
        if self._on_call is not None:
            self._on_call()
        if self._fail_all or text in self._fail_texts:
            raise MemoirAudioProviderError("TTS_SESSION_FAILED", "会话失败")
        return TTSSegmentResult(
            audio=b"ID3mp3frame:" + text.encode("utf-8")[:12],
            text_words=max(1, len(text)),
        )


class FakeMusicClient:
    """音乐提交/查询桩：可配置提交失败、终态失败、等待轮数。"""

    def __init__(
        self, *, fail_submit: bool = False, fail_status: bool = False,
        wait_polls: int = 0,
    ) -> None:
        self._action = "GenBGM"
        self._fail_submit = fail_submit
        self._fail_status = fail_status
        self._wait_polls = wait_polls
        self.submit_count = 0
        self.query_count = 0

    async def submit_generation(self) -> str:
        self.submit_count += 1
        if self._fail_submit:
            raise MemoirAudioProviderError("MUSIC_HTTP_403", "提交被拒")
        return "task-m8-1"

    async def query_task(self, task_id: str) -> MusicTaskSnapshot:
        self.query_count += 1
        if self._fail_status:
            return MusicTaskSnapshot(status=3, audio_url=None)
        if self.query_count <= self._wait_polls:
            return MusicTaskSnapshot(status=1, audio_url=None)
        return MusicTaskSnapshot(status=2, audio_url="https://bgm.example.test/a.mp3")


class FakeUploader:
    """私有上传/默认源读取桩：记录上传与读源（默认配乐模式）；私有 ACL
    语义由 R6 测试覆盖。"""

    # 默认配乐源（freeze 2026-09-11）：同环境音频根 default/ 目录 .mp3。
    DEFAULT_BGM_SOURCE_KEY = "memoir-test/audios/default/memoirs.mp3"
    DEFAULT_BGM_SOURCE_BYTES = b"default-bgm-source-mp3-bytes"

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []
        self.downloads: list[str] = []

    def upload_private_bytes(self, data: bytes, object_key: str, mime: str) -> None:
        self.uploads.append((object_key, mime))

    def download_private_bytes(
        self, object_key: str, *, max_bytes: int, timeout_seconds: float
    ) -> bytes:
        self.downloads.append(object_key)
        return self.DEFAULT_BGM_SOURCE_BYTES


class FakeTranscoder:
    """转码桩：拼接/转码恒成功，时长确定性输出（真实 ffmpeg 由 R6 测试覆盖）。

    bgm_duration_ms 可注入奇特整数毫秒：证明默认配乐时长来自实测转码
    而非协议默认 60s。
    """

    def __init__(self, *, bgm_duration_ms: int = 60000) -> None:
        self._bgm_duration_ms = bgm_duration_ms

    async def concat_mp3_segments(self, segments: list[bytes]) -> SimpleNamespace:
        return SimpleNamespace(audio=b"joined-mp3", duration_ms=4200)

    async def transcode_to_mp3(self, data: bytes) -> SimpleNamespace:
        return SimpleNamespace(audio=b"bgm-mp3", duration_ms=self._bgm_duration_ms)


class FakeDownloader:
    """下载桩：SSRF/host 白名单校验由 R6 测试覆盖，这里只回字节。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def download(self, url: str) -> bytes:
        self.calls.append(url)
        return b"bgm-bytes"


def _cost_policy() -> MemoirAudioCostPolicy:
    # 单价取整便于断言：TTS 1 元/千字（0.001 元/字）、音乐 0.1 元/秒。
    return MemoirAudioCostPolicy(
        currency="CNY",
        tts_price_per_1000_text_words=Decimal("1"),
        music_price_per_second=Decimal("0.1"),
        max_cost_per_run=Decimal("10"),
    )


def _audio_config(**overrides: object) -> MemoirAudioConfig:
    # 默认构造为付费生成模式（与既有付费路径用例一致）；默认配乐模式
    # 用例显式覆盖 music_generation_enabled=False + default_bgm_object_key。
    values: dict[str, object] = dict(
        narrator_prefix=NARRATOR_PREFIX,
        background_prefix=BACKGROUND_PREFIX,
        scope_hmac_key=SCOPE_KEY,
        node_timeout_seconds=300.0,
        publish_reserve_seconds=30.0,
        scene_concurrency=2,
        worker_concurrency=4,
        music_generation_enabled=True,
        music_poll_interval_seconds=0.01,
        music_duration_seconds=60,
        tts_request_timeout_seconds=45.0,
    )
    values.update(overrides)
    return MemoirAudioConfig(**values)  # type: ignore[arg-type]


def _audio_service(
    session: Session,
    tts: FakeTTSClient,
    music: FakeMusicClient,
    uploader: FakeUploader,
    downloader: FakeDownloader,
    *,
    config: MemoirAudioConfig | None = None,
    transcoder: FakeTranscoder | None = None,
) -> MemoirAudioService:
    return MemoirAudioService(
        tts_client=tts,
        music_client=music,
        uploader=uploader,
        transcoder=transcoder or FakeTranscoder(),
        downloader=downloader,
        jobs_service=MemoirAudioJobsService(session, _cost_policy()),
        config=config or _audio_config(),
        session=session,
    )


def _build_scenario(
    model_outputs: dict[str, list[object]],
    *,
    audio_service: object | None = None,
    active_elapsed_ms: int = 0,
):
    """装配 1.0.8 全图执行夹具：真实 graph plan + 真实 Runner + 打桩网关。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    memoir_steps = [
        node.model_dump()
        for node in AgentPackageService._load_workflow_nodes(
            Path(__file__).resolve().parents[1]
            / "app/agents/memoir_agent/1.0.8/workflow.graph.py"
        )
    ]
    run = AgentRun(
        run_id=RUN_ID, agent_id="memoir_agent", agent_version="1.0.8",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="claimed",
        input_json={"archive_id": "archive", "snapshot_id": "snapshot", "generation_epoch": 0},
        capability_snapshot_json=CAPABILITY_SNAPSHOT,
        authorization_version=1, caller_id="caller", tenant_id="tenant",
        create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace",
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now + timedelta(minutes=10), run_deadline_at=now + timedelta(days=1),
        active_elapsed_ms=active_elapsed_ms,
    )
    session.add(run)
    session.add(AgentDefinition(
        agent_id="memoir_agent", version="1.0.8", runtime_type="workflow",
        definition_json={}, package_digest="sha256:test", contract_version="1.0.0",
        status="active", status_changed_at=now, status_changed_by="test",
        status_change_reason="fixture",
    ))
    session.add(AgentPlan(
        plan_id="memoir-108-full-plan", run_id=run.run_id, strategy="static_workflow",
        steps_json=memoir_steps, stop_conditions_json={}, fallback_policy_json={},
        status="planned",
    ))
    session.commit()
    model_gateway = ScriptedModelGateway(model_outputs)
    tool_gateway = PublishingToolGateway(SNAPSHOT_PAYLOAD)
    runner = MemoirNodeRunner(
        tool_gateway, ToolCallAuditService(session),
        model_gateway=model_gateway, audio_service=audio_service,
    )
    executor = WorkflowExecutor(
        session, runner,
        CheckpointStore(session, FernetCheckpointCipher.generate()),
        ArtifactStore(session),
    )
    lease = LeaseContext(
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now + timedelta(minutes=10), privacy_version=1,
        authorization_version=1,
    )
    return executor, session, model_gateway, tool_gateway, lease, run


def _inject_audio_executor(
    session: Session, tool_gateway: PublishingToolGateway,
    model_gateway: ScriptedModelGateway, service: object,
) -> WorkflowExecutor:
    """夹具重建：真实 Runner + 注入音频服务（与 Worker configured_executor 同构）。"""
    from app.agents.memoir_agent.runner import MemoirNodeRunner as _Runner

    return WorkflowExecutor(
        session,
        _Runner(
            tool_gateway, ToolCallAuditService(session),
            model_gateway=model_gateway, audio_service=service,
        ),
        CheckpointStore(session, FernetCheckpointCipher.generate()),
        ArtifactStore(session),
    )


def test_full_graph_publishes_2_0_0_document_with_complete_audio() -> None:
    """全量成功：3 场景旁白 + 1 配乐入 2.0.0 文档，账本与预算完整入账。"""
    tts, music, uploader, downloader = (
        FakeTTSClient(), FakeMusicClient(), FakeUploader(), FakeDownloader(),
    )
    executor, session, model_gateway, tool_gateway, lease, run = _build_scenario(
        _default_scene_outputs(),
    )
    # 音频服务需要同一事务 session（与 Worker 装配同构），先建夹具再注服务。
    service = _audio_service(session, tts, music, uploader, downloader)
    executor = _inject_audio_executor(session, tool_gateway, model_gateway, service)

    result = executor.run(RUN_ID, lease)

    assert result.status == "succeeded", result.error_code
    assert len(tool_gateway.published_documents) == 1
    document = tool_gateway.published_documents[0]
    assert document["schema_version"] == "2.0.0"  # type: ignore[index]
    audio = document["audio"]  # type: ignore[index]
    # 图片 manifest 六键合同不变；audio 新键齐备且顺序与场景序一致。
    assert [entry["scene_id"] for entry in audio["narrations"]] == ["s1-1", "s1-2", "s1-3"]
    for entry in audio["narrations"]:
        assert set(entry) == NARRATION_KEYS
        assert entry["mime"] == "audio/mpeg"
        assert entry["duration_ms"] == 4200
        assert entry["object_key"].startswith(f"{NARRATOR_PREFIX}{EXPECTED_SCOPE}/narr-")
        assert entry["object_key"].endswith(".mp3")
        assert entry["media_id"].startswith("audio-narr-")
    assert set(audio["background_music"]) == BGM_KEYS
    assert audio["background_music"]["object_key"].startswith(
        f"{BACKGROUND_PREFIX}{EXPECTED_SCOPE}/bgm-"
    )
    # 媒体 ID 跨图/音唯一：audio 前缀与图片 media- 前缀天然隔离。
    media_ids = {entry["media_id"] for entry in audio["narrations"]}
    media_ids.add(audio["background_music"]["media_id"])
    assert len(media_ids) == 4
    # 私有上传：3 旁白 + 1 配乐，全部 audio/mpeg。
    assert sorted(mime for _key, mime in uploader.uploads) == ["audio/mpeg"] * 4
    assert downloader.calls == ["https://bgm.example.test/a.mp3"]
    # 账本：分段 submitted+已结算、旁白/配乐 uploaded、音乐按 60 秒结算。
    jobs = list(session.scalars(select(MemoirAudioJob)))
    segment_rows = [job for job in jobs if job.role == ROLE_SEGMENT]
    narration_rows = [job for job in jobs if job.role == ROLE_NARRATION]
    bgm_rows = [job for job in jobs if job.role == ROLE_BACKGROUND_MUSIC]
    assert len(segment_rows) == 3 and all(
        job.state == STATE_SUBMITTED and job.settled_cost is not None
        for job in segment_rows
    )
    assert len(narration_rows) == 3 and all(
        job.state == STATE_UPLOADED and job.duration_ms == 4200 for job in narration_rows
    )
    assert len(bgm_rows) == 1 and bgm_rows[0].state == STATE_UPLOADED
    assert bgm_rows[0].requested_music_seconds == 60
    # 预算原子封顶：预留 = 各分段保守预留 + 音乐 60s 预估，与账本行一致。
    budget = session.scalar(select(MemoirAudioRunBudget).where(
        MemoirAudioRunBudget.run_id == RUN_ID,
    ))
    policy = _cost_policy()
    expected_reserved = (
        estimate_tts_cost(COVER_BODY, policy)
        + estimate_tts_cost(DIARY_BODY, policy)
        + estimate_tts_cost(BET_BODY, policy)
        + estimate_music_cost(60, policy)
    )
    assert budget is not None
    assert budget.reserved_total_cost == expected_reserved


def test_full_graph_default_bgm_mode_publishes_background_copy_entry() -> None:
    """默认配乐模式（freeze 2026-09-11 §6）：2.0.0 文档含 background 副本条目。

    副本键落现行 background 工作目录（随机不透明名，绝不共享源 key）；
    时长来自实测转码（奇特整数毫秒，非默认 60s）；账本零费结算
    settled_cost=0、requested_music_seconds=None；零火山提交/查询/下载；
    源 key 永不进入上传。
    """
    tts, music, uploader, downloader = (
        FakeTTSClient(), FakeMusicClient(), FakeUploader(), FakeDownloader(),
    )
    executor, session, model_gateway, tool_gateway, lease, run = _build_scenario(
        _default_scene_outputs(),
    )
    service = _audio_service(
        session, tts, music, uploader, downloader,
        config=_audio_config(
            music_generation_enabled=False,
            default_bgm_object_key=FakeUploader.DEFAULT_BGM_SOURCE_KEY,
        ),
        transcoder=FakeTranscoder(bgm_duration_ms=47_777),
    )
    executor = _inject_audio_executor(session, tool_gateway, model_gateway, service)

    result = executor.run(RUN_ID, lease)

    assert result.status == "succeeded", result.error_code
    document = tool_gateway.published_documents[0]
    audio = document["audio"]  # type: ignore[index]
    # 旁白照常交付（3 场景）。
    assert [entry["scene_id"] for entry in audio["narrations"]] == ["s1-1", "s1-2", "s1-3"]
    bgm = audio["background_music"]
    assert bgm is not None
    assert set(bgm) == BGM_KEYS
    assert bgm["mime"] == "audio/mpeg"
    assert bgm["duration_ms"] == 47_777
    assert bgm["object_key"].startswith(f"{BACKGROUND_PREFIX}{EXPECTED_SCOPE}/bgm-")
    assert bgm["object_key"].endswith(".mp3")
    assert bgm["object_key"] != FakeUploader.DEFAULT_BGM_SOURCE_KEY
    # 读源恰一次（精确源 key）；源 key 永不进入上传。
    assert uploader.downloads == [FakeUploader.DEFAULT_BGM_SOURCE_KEY]
    assert FakeUploader.DEFAULT_BGM_SOURCE_KEY not in {
        key for key, _mime in uploader.uploads
    }
    # 零火山调用：不提交、不查询、不下载临时 URL。
    assert music.submit_count == 0 and music.query_count == 0
    assert downloader.calls == []
    # 账本：BGM 零费槽 uploaded + settled_cost=0 + 无秒数/TaskID。
    bgm_rows = list(session.scalars(select(MemoirAudioJob).where(
        MemoirAudioJob.role == ROLE_BACKGROUND_MUSIC,
    )))
    assert len(bgm_rows) == 1
    assert bgm_rows[0].state == STATE_UPLOADED
    assert bgm_rows[0].object_key == bgm["object_key"]
    assert bgm_rows[0].settled_cost == Decimal("0")
    assert bgm_rows[0].requested_music_seconds is None
    assert bgm_rows[0].provider_task_id is None
    # 预算只含 TTS 分段预留：默认 BGM 零费槽不占预算。
    budget = session.scalar(select(MemoirAudioRunBudget).where(
        MemoirAudioRunBudget.run_id == RUN_ID,
    ))
    policy = _cost_policy()
    expected_tts_only = (
        estimate_tts_cost(COVER_BODY, policy)
        + estimate_tts_cost(DIARY_BODY, policy)
        + estimate_tts_cost(BET_BODY, policy)
    )
    assert budget is not None
    assert budget.reserved_total_cost == expected_tts_only



    """能力关闭（服务未注入）：发布 2.0.0 空音频文档（空数组 + null）。"""
def test_full_graph_audio_capability_off_publishes_empty_audio_document() -> None:
    """能力关闭（服务未注入）：发布 2.0.0 空音频文档（空数组 + null）。"""
    executor, session, model_gateway, tool_gateway, lease, run = _build_scenario(
        _default_scene_outputs(), audio_service=None,
    )

    result = executor.run(RUN_ID, lease)

    assert result.status == "succeeded", result.error_code
    document = tool_gateway.published_documents[0]
    assert document["schema_version"] == "2.0.0"  # type: ignore[index]
    assert document["audio"] == {"narrations": [], "background_music": None}  # type: ignore[index]
    # 未注入服务绝不触达音频账本。
    assert session.scalars(select(MemoirAudioJob)).all() == []


def test_partial_tts_failure_degrades_only_that_scene() -> None:
    """单场景 TTS 失败：仅该场景无旁白，其余两景 + 配乐照常交付。"""
    executor, session, model_gateway, tool_gateway, lease, run = _build_scenario(
        _default_scene_outputs(),
    )
    # 短正文一段一句：按分段文本（=正文）精确失败第二个场景。
    tts = FakeTTSClient(fail_texts=frozenset([DIARY_BODY]))
    music, uploader, downloader = FakeMusicClient(), FakeUploader(), FakeDownloader()
    service = _audio_service(session, tts, music, uploader, downloader)
    executor = _inject_audio_executor(session, tool_gateway, model_gateway, service)

    result = executor.run(RUN_ID, lease)

    assert result.status == "succeeded", result.error_code
    document = tool_gateway.published_documents[0]
    audio = document["audio"]  # type: ignore[index]
    assert [entry["scene_id"] for entry in audio["narrations"]] == ["s1-1", "s1-3"]
    assert audio["background_music"] is not None
    # 失败分段入未知终态（TTS 不可断点续传，永不自动重提）。
    failed_segments = list(session.scalars(select(MemoirAudioJob).where(
        MemoirAudioJob.role == ROLE_SEGMENT,
        MemoirAudioJob.scene_id == "s1-2",
    )))
    assert len(failed_segments) == 1
    assert failed_segments[0].state == STATE_SUBMISSION_UNKNOWN


def test_all_audio_failures_still_publish_image_text_once() -> None:
    """全音频失败：空音频发布完整图文，且只发布一次（不另起补音 revision）。"""
    executor, session, model_gateway, tool_gateway, lease, run = _build_scenario(
        _default_scene_outputs(),
    )
    tts = FakeTTSClient(fail_all=True)
    music = FakeMusicClient(fail_submit=True)
    service = _audio_service(session, tts, music, FakeUploader(), FakeDownloader())
    from app.agents.memoir_agent.runner import MemoirNodeRunner as _Runner

    executor = WorkflowExecutor(
        session,
        _Runner(
            tool_gateway, ToolCallAuditService(session),
            model_gateway=model_gateway, audio_service=service,
        ),
        CheckpointStore(session, FernetCheckpointCipher.generate()),
        ArtifactStore(session),
    )

    result = executor.run(RUN_ID, lease)

    assert result.status == "succeeded", result.error_code
    assert len(tool_gateway.published_documents) == 1
    document = tool_gateway.published_documents[0]
    assert document["audio"] == {"narrations": [], "background_music": None}  # type: ignore[index]
    # TTS 全失败 + 音乐提交被拒后不再重试重提（提交一次、查询零次）。
    assert len(tts.calls) == 3
    assert music.submit_count == 1 and music.query_count == 0


def test_image_text_exhausted_run_budget_skips_audio_entirely() -> None:
    """图文耗尽 Run 剩余预算：音频整体跳过（零供应商调用、零账本行）。"""
    tts, music, uploader, downloader = (
        FakeTTSClient(), FakeMusicClient(), FakeUploader(), FakeDownloader(),
    )
    executor, session, model_gateway, tool_gateway, lease, run = _build_scenario(
        _default_scene_outputs(),
        # 1200s 预算只剩 5s：min(300, 5 - 30) < 0，音频节点直接跳过；
        # 策略引擎按 prior+delta 判定仍充足（1195s + 本轮耗时 < 1200s）。
        active_elapsed_ms=1_195_000,
    )
    service = _audio_service(session, tts, music, uploader, downloader)
    executor = _inject_audio_executor(session, tool_gateway, model_gateway, service)

    result = executor.run(RUN_ID, lease)

    assert result.status == "succeeded", result.error_code
    document = tool_gateway.published_documents[0]
    assert document["audio"] == {"narrations": [], "background_music": None}  # type: ignore[index]
    assert tts.calls == [] and music.submit_count == 0
    assert session.scalars(select(MemoirAudioJob)).all() == []


def test_cancel_mid_audio_stops_new_writes() -> None:
    """音频执行中 Run 被取消：账本门禁拒绝后续写入，无旁白/发布产出。"""
    executor, session, model_gateway, tool_gateway, lease, run = _build_scenario(
        _default_scene_outputs(),
    )
    cancelled = {"done": False}

    def _cancel_on_first_call() -> None:
        # 模拟取消信号在首个 TTS 调用期间到达（结果已返回但尚未入账）。
        if not cancelled["done"]:
            cancelled["done"] = True
            run.cancel_requested_at = datetime.now(UTC)

    tts = FakeTTSClient(on_call=_cancel_on_first_call)
    # BGM 配一次等待轮询：提交后首查返回“处理中”并 sleep，确保取消信号
    # 在其续约/下载前落地（否则假件瞬时完成会与取消形成调度竞态）。
    music = FakeMusicClient(wait_polls=1)
    uploader, downloader = FakeUploader(), FakeDownloader()
    service = _audio_service(session, tts, music, uploader, downloader)
    executor = _inject_audio_executor(session, tool_gateway, model_gateway, service)

    result = executor.run(RUN_ID, lease)

    # 取消后 Run 不再发布（执行器在安全边界按租约/取消判失败）。
    assert result.status in {"failed", "cancelled"}
    assert tool_gateway.published_documents == []
    # 取消后零新写入：取消瞬间已开出的在途槽（场景并发下可有多个）停在
    # 提交前/提交中/已提交未结算——任何新结果（结算/上传/发布）都被账本
    # Run 门禁拒绝；无私有上传、无发布产出。
    jobs = list(session.scalars(select(MemoirAudioJob)))
    assert 2 <= len(jobs) <= 4
    assert all(
        job.state in {"reserved", "submitting", "submitted"} for job in jobs
    )
    assert all(job.settled_cost is None for job in jobs)
    assert all(job.object_key is None for job in jobs)
    assert uploader.uploads == []


def test_resume_reuses_uploaded_assets_without_double_charge() -> None:
    """崩溃恢复重算：资产复用、零供应商调用、预算不再增长、发布单次。"""
    tts, music, uploader, downloader = (
        FakeTTSClient(), FakeMusicClient(), FakeUploader(), FakeDownloader(),
    )
    executor, session, model_gateway, tool_gateway, lease, run = _build_scenario(
        _default_scene_outputs(),
    )
    service = _audio_service(session, tts, music, uploader, downloader)
    from app.agents.memoir_agent.runner import MemoirNodeRunner as _Runner

    def _make_executor(current_service: object) -> WorkflowExecutor:
        return WorkflowExecutor(
            session,
            _Runner(
                tool_gateway, ToolCallAuditService(session),
                model_gateway=ScriptedModelGateway(_default_scene_outputs()),
                audio_service=current_service,
            ),
            CheckpointStore(session, FernetCheckpointCipher.generate()),
            ArtifactStore(session),
        )

    first = _make_executor(service).run(RUN_ID, lease)
    assert first.status == "succeeded", first.error_code
    budget_after_first = session.scalar(
        select(MemoirAudioRunBudget).where(MemoirAudioRunBudget.run_id == RUN_ID)
    )
    assert budget_after_first is not None
    first_reserved = budget_after_first.reserved_total_cost
    first_document = tool_gateway.published_documents[0]
    session.commit()

    # 恢复重算：全新供应商桩（计数清零）+ 同一账本；同 Run/epoch 同输入。
    tts2, music2 = FakeTTSClient(), FakeMusicClient()
    uploader2, downloader2 = FakeUploader(), FakeDownloader()
    service2 = _audio_service(session, tts2, music2, uploader2, downloader2)
    second = _make_executor(service2).run(RUN_ID, lease)

    assert second.status == "succeeded", second.error_code
    assert tts2.calls == [] and music2.submit_count == 0
    assert uploader2.uploads == [] and downloader2.calls == []
    budget_after_second = session.scalar(
        select(MemoirAudioRunBudget).where(MemoirAudioRunBudget.run_id == RUN_ID)
    )
    assert budget_after_second is not None
    assert budget_after_second.reserved_total_cost == first_reserved
    # 发布幂等：query-after-commit 对账，不重发 publish 写请求。
    assert len(tool_gateway.published_documents) == 1
    second_document = tool_gateway.published_documents[0]
    assert second_document["audio"] == first_document["audio"]  # type: ignore[index]


def test_worker_assembles_audio_service_only_when_fully_configured(monkeypatch) -> None:
    """Worker 装配门禁：默认/开关开但配置缺→能力关闭；按模式配置齐全→服务。

    M8 默认配乐（freeze 2026-09-11 §6）：基础 15 项（含默认源 key）齐全
    即可装配默认配乐模式；生成三项仅在生成开关 true 时必填。
    """
    import app.worker as worker
    from app.core.config import Settings

    # 默认关闭：不触网不装配。
    assert worker.configured_audio_service(worker.settings, None) is None
    # 开关打开但配置缺失：按能力关闭（不抛异常）。
    monkeypatch.setattr(worker.settings, "MEMOIR_AUDIO_ENABLED", True, raising=False)
    assert worker.configured_audio_service(worker.settings, None) is None
    monkeypatch.setattr(worker.settings, "MEMOIR_AUDIO_ENABLED", False, raising=False)
    # 基础公共字段（两种模式共用，占位值不触网）。
    base = dict(
        MEMOIR_AUDIO_ENABLED=True,
        MEMOIR_TTS_API_KEY="test-only-key",
        VOLCANO_CV_ACCESS_KEY="test-ak",
        VOLCANO_CV_SECRET_KEY="test-sk",
        MEMOIR_TTS_PRICE_PER_1000_TEXT_WORDS="1",
        MEMOIR_AUDIO_MAX_COST_PER_RUN="10",
        MEMOIR_AUDIO_COST_CURRENCY="CNY",
        MEMORY_AUDIO_OSS_ENDPOINT="oss-cn-hangzhou.aliyuncs.com",
        MEMORY_AUDIO_OSS_BUCKET="bucket",
        MEMORY_AUDIO_OSS_ACCESS_KEY_ID="ak",
        MEMORY_AUDIO_OSS_ACCESS_KEY_SECRET="sk",
        MEMORY_AUDIO_NARRATOR_PREFIX="memoir-test/audios/narrator/",
        MEMORY_AUDIO_BACKGROUND_PREFIX="memoir-test/audios/background/",
        MEMORY_AUDIO_SCOPE_HMAC_KEY="scope-key",
        # R6 后音频启用必填：账本输入指纹密钥（Runtime 专属，与 scope 密钥域隔离）
        MEMOIR_AUDIO_INPUT_HMAC_KEY="test-input-key",
        # M8 默认配乐：默认源 key 属基础必填（两种模式都要求）。
        MEMOIR_DEFAULT_BGM_OBJECT_KEY="memoir-test/audios/default/memoirs.mp3",
    )
    # 默认配乐模式：不含音乐生成三项也能正常装配（消除配置拆分过渡态）。
    default_mode = Settings(**base)
    service = worker.configured_audio_service(default_mode, None)
    assert isinstance(service, MemoirAudioService)
    assert service.config.worker_concurrency == 4  # 进程级信号量默认值落点
    assert service.config.music_generation_enabled is False
    assert (
        service.config.default_bgm_object_key
        == "memoir-test/audios/default/memoirs.mp3"
    )
    # 生成模式：补齐三项后装配，模式开关为 True。
    generation_mode = Settings(
        **base,
        MEMOIR_MUSIC_GENERATION_ENABLED=True,
        MEMOIR_MUSIC_ACTION="GenBGM",
        MEMOIR_MUSIC_PRICE_PER_SECOND="0.1",
        MEMOIR_MUSIC_DOWNLOAD_ALLOWED_HOSTS_JSON='["toc-host.example.com"]',
    )
    service = worker.configured_audio_service(generation_mode, None)
    assert isinstance(service, MemoirAudioService)
    assert service.config.music_generation_enabled is True
