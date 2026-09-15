"""M8 R8 Memoir 音频协调服务：场景旁白 + 作品 BGM 的预算内编排。

职责边界（对齐 M8 设计说明 §2–§5）：
1. `generate()`：按 Run 剩余预算编排全部音频——预算取
   min(MEMOIR_AUDIO_NODE_TIMEOUT_SECONDS, Run 剩余活跃预算, 墙钟剩余)
   再减 MEMOIR_AUDIO_PUBLISH_RESERVE_SECONDS；到预算即停止新提交，
   保留已成功资源后返回，发布完整图文不受影响。
2. 旁白：`split_narration_segments` 按句无损分段，逐段提交 TTS（分段费用
   按完整输入保守预留、按官方 text_words 实际结算）；全部分段成功后
   ffmpeg 解码重拼为单场景 MP3 并私有 OSS 上传；任一分段失败该场景
   有界降级（无旁白仍发布图文），失败不另起补音。
3. BGM：按 MEMOIR_MUSIC_GENERATION_ENABLED 分叉（freeze 2026-09-11）。
   生成模式：每作品一首（固定提示词 / 固定时长），提交返回 TaskID 立即
   入账，之后只查不重建；成功后下载（SSRF 白名单）→ 转码 → 私有上传 →
   按请求秒数结算。默认模式：零费复制部署默认源（域拆分指纹 + 同 Run
   互斥 + 独立私有副本 + settled_cost=0）；两条路径失败均降级为无配乐。
4. 恢复：资产键与费用预留经 MemoirAudioJobsService 按
   (run, epoch, package, role, scene, segment, 输入 HMAC) 幂等对账——已
   上传成功资产同 Run 同输入直接复用不重复扣费；submission_unknown 永不
   自动重提；在途音乐任务凭已知 TaskID 续查（沿用账本当前 fencing token，
   不产生第二笔提交费用）。
5. 并发：场景级 asyncio.Semaphore（默认 2）+ 进程级 threading 信号量
   （默认 4，包住每次真实供应商调用）；单 Worker 顺序执行时二者均不
   争用，多线程/多任务扩展时保证全局上限。

隐私铁律：正文、音频字节、TaskID、临时 AudioUrl 绝不进日志或异常文本；
节点结果只携带安全计数与不透明资源 ID（object_key 含 scope 摘要，不含
明文，与发布文档一致）。
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.models.memoir_audio_job import (
    NO_SEGMENT_INDEX,
    ROLE_BACKGROUND_MUSIC,
    ROLE_NARRATION,
    ROLE_SEGMENT,
    STATE_FAILED,
    STATE_SUBMISSION_UNKNOWN,
    WORK_SCENE_ID,
    MemoirAudioJob,
)
from app.services.memoir.memoir_audio_jobs import (
    MemoirAudioCostPolicy,
    MemoirAudioJobReservation,
    MemoirAudioJobsError,
    MemoirAudioJobsService,
    estimate_music_cost,
    estimate_tts_cost,
)
from app.services.memoir.memoir_audio_provider import (
    MUSIC_STATUS_FAILED,
    MUSIC_STATUS_SUCCESS,
    MemoirAudioProviderError,
    VolcanoMusicClient,
    VolcanoTTSClient,
    split_narration_segments,
)
from app.services.memoir.memoir_audio_storage import (
    AUDIO_ROLE_BACKGROUND,
    AUDIO_ROLE_NARRATOR,
    AliyunAudioOSSUploader,
    AudioTranscoder,
    SecureAudioDownloader,
    build_audio_object_key,
    compute_audio_scope,
)

logger = logging.getLogger(__name__)

# 音频资产统一 MIME：全链路只发布 MP3（转码输出恒为 libmp3lame）。
AUDIO_MIME = "audio/mpeg"
# 输入 HMAC 版本与分段规则版本：任何分段阈值 / 声音参数变更都必须换版本号，
# 否则恢复路径会把新旧输入误判为同一资产。
# v1→v2 兼容口径：v1 是无密钥 SHA256，v2 起改为 keyed HMAC。算法切换后
# 旧 v1 摘要不可能与新摘要相等，因此切换前留下的在途（非终态）账本行
# 不会被新执行按指纹复用或续跑——按未知提交处理，由维护对账收敛
# （音频默认关闭且未上生产，无历史生产数据；详见 ENV_CONFIG 音频小节）。
INPUT_HMAC_VERSION = "memoir-audio-input-v2"
SEGMENT_RULE_VERSION = "split-v1"
# 明确未受理失败（限流）允许的一次退避重试上限：attempt 达 2 即不再重试。
MAX_SUBMIT_ATTEMPTS = 2
# 限流错误码（供应商明确未受理，未计费，可安全重试一次）。
_RATE_LIMIT_CODES = frozenset({"TTS_HTTP_429", "MUSIC_HTTP_429"})
# Run 门禁类账本错误：一旦出现立即停止全部音频副作用（取消/隐私/授权失效）。
_RUN_GATE_CODES = frozenset(
    {
        "MEMOIR_AUDIO_RUN_NOT_FOUND",
        "MEMOIR_AUDIO_RUN_CANCELLED",
        "MEMOIR_AUDIO_RUN_PRIVACY_BLOCKED",
        "MEMOIR_AUDIO_POLICY_INVALID",
    }
)
# 预算类错误：停止一切新提交，保留已成功资源。
_BUDGET_CODES = frozenset({"MEMOIR_AUDIO_BUDGET_EXCEEDED"})
# 账本租约时长（秒）：覆盖单次 TTS/音乐请求 + 一次轮询间隔的窗口。
_JOB_LEASE_TTL_SECONDS = 90.0

# 进程级音频供应商调用信号量：所有 MemoirAudioService 实例共享；
# 单 Worker 顺序执行时永不争用，多 Run 并发时保证全局上限。
_worker_semaphore: threading.Semaphore | None = None
_worker_semaphore_lock = threading.Lock()


def _get_worker_semaphore(concurrency: int) -> threading.Semaphore:
    """懒创建进程级信号量；并发数按首次装配的部署配置冻结。"""
    global _worker_semaphore
    with _worker_semaphore_lock:
        if _worker_semaphore is None:
            _worker_semaphore = threading.BoundedSemaphore(max(1, concurrency))
        return _worker_semaphore


class MemoirAudioConfig:
    """音频通道部署配置快照；从 Settings 收口，节点内不再散读。"""

    def __init__(
        self,
        *,
        narrator_prefix: str,
        background_prefix: str,
        scope_hmac_key: str,
        input_hmac_key: str = "",
        node_timeout_seconds: float = 300.0,
        publish_reserve_seconds: float = 30.0,
        scene_concurrency: int = 2,
        worker_concurrency: int = 4,
        # 音乐生成子开关（freeze 2026-09-11 §6）：False=默认配乐零费复制，
        # True=火山原创配乐付费链路。生产装配经 from_settings 注入 Settings
        # 值（生产默认 False）；直接构造（测试替身）默认 True 保持现行付费
        # 路径零破坏——既有付费用例不因新增开关翻转行为。
        music_generation_enabled: bool = True,
        default_bgm_object_key: str = "",
        # 默认源读取字节上限（对齐 MEMOIR_AUDIO_MAX_FILE_BYTES 契约值）。
        max_file_bytes: int = 20971520,
        music_poll_interval_seconds: float = 5.0,
        music_duration_seconds: int = 60,
        tts_request_timeout_seconds: float = 45.0,
    ) -> None:
        # input_hmac_key 与 scope_hmac_key 域隔离：前者仅 Runtime 账本内部
        # 幂等指纹使用（keyed HMAC），后者是两仓共享的对象 scope 摘要密钥，
        # 两者不得复用同一值（防跨域摘要碰撞与密钥泄露面扩大）。
        # input_hmac_key 的非空强制校验在 Settings 层
        # （_MEMOIR_AUDIO_BASE_REQUIRED_STRINGS / validate_memoir_audio_settings），
        # 生产装配路径 build_memoir_audio_service 先跑该校验再构造本对象，
        # 空钥匙到不了这里；测试替身允许空钥匙（HMAC 仍确定性）。
        if not narrator_prefix or not background_prefix or not scope_hmac_key:
            raise ValueError("MEMOIR_AUDIO_CONFIG_INVALID")
        if node_timeout_seconds <= 0 or publish_reserve_seconds < 0:
            raise ValueError("MEMOIR_AUDIO_CONFIG_INVALID")
        if scene_concurrency < 1 or worker_concurrency < 1:
            raise ValueError("MEMOIR_AUDIO_CONFIG_INVALID")
        if max_file_bytes <= 0:
            raise ValueError("MEMOIR_AUDIO_CONFIG_INVALID")
        if music_generation_enabled:
            # 仅付费生成模式校验音乐轮询/时长；默认模式不触达这两个值
            # （freeze §6：默认模式不得因未使用的音乐配置装配失败），
            # 生成模式校验一字不放宽。
            if music_poll_interval_seconds <= 0 or music_duration_seconds <= 0:
                raise ValueError("MEMOIR_AUDIO_CONFIG_INVALID")
        elif not default_bgm_object_key:
            # 默认模式必须有源 key：Settings 层基础必填的快照层防线。
            raise ValueError("MEMOIR_AUDIO_CONFIG_INVALID")
        self.narrator_prefix = narrator_prefix
        self.background_prefix = background_prefix
        self.scope_hmac_key = scope_hmac_key
        self.input_hmac_key = input_hmac_key
        self.node_timeout_seconds = float(node_timeout_seconds)
        self.publish_reserve_seconds = float(publish_reserve_seconds)
        self.scene_concurrency = int(scene_concurrency)
        self.worker_concurrency = int(worker_concurrency)
        self.music_generation_enabled = bool(music_generation_enabled)
        self.default_bgm_object_key = default_bgm_object_key
        self.max_file_bytes = int(max_file_bytes)
        self.music_poll_interval_seconds = float(music_poll_interval_seconds)
        self.music_duration_seconds = int(music_duration_seconds)
        self.tts_request_timeout_seconds = float(tts_request_timeout_seconds)

    @classmethod
    def from_settings(cls, settings: object) -> MemoirAudioConfig:
        return cls(
            narrator_prefix=str(getattr(settings, "MEMORY_AUDIO_NARRATOR_PREFIX", "")),
            background_prefix=str(getattr(settings, "MEMORY_AUDIO_BACKGROUND_PREFIX", "")),
            scope_hmac_key=str(getattr(settings, "MEMORY_AUDIO_SCOPE_HMAC_KEY", "")),
            input_hmac_key=str(getattr(settings, "MEMOIR_AUDIO_INPUT_HMAC_KEY", "")),
            node_timeout_seconds=float(
                getattr(settings, "MEMOIR_AUDIO_NODE_TIMEOUT_SECONDS", 300.0)
            ),
            publish_reserve_seconds=float(
                getattr(settings, "MEMOIR_AUDIO_PUBLISH_RESERVE_SECONDS", 30.0)
            ),
            scene_concurrency=int(
                getattr(settings, "MEMOIR_TTS_SCENE_CONCURRENCY", 2)
            ),
            worker_concurrency=int(
                getattr(settings, "MEMOIR_AUDIO_WORKER_CONCURRENCY", 4)
            ),
            music_generation_enabled=bool(
                getattr(settings, "MEMOIR_MUSIC_GENERATION_ENABLED", False)
            ),
            default_bgm_object_key=str(
                getattr(settings, "MEMOIR_DEFAULT_BGM_OBJECT_KEY", "")
            ),
            max_file_bytes=int(
                getattr(settings, "MEMOIR_AUDIO_MAX_FILE_BYTES", 20971520)
            ),
            music_poll_interval_seconds=float(
                getattr(settings, "MEMOIR_MUSIC_POLL_INTERVAL_SECONDS", 5.0)
            ),
            music_duration_seconds=int(
                getattr(settings, "MEMOIR_MUSIC_DURATION_SECONDS", 60)
            ),
            tts_request_timeout_seconds=float(
                getattr(settings, "MEMOIR_TTS_REQUEST_TIMEOUT_SECONDS", 45.0)
            ),
        )


def compute_input_hmac(key: str, payload: dict[str, object]) -> str:
    """计算音频输入摘要（keyed HMAC）：覆盖完整正文身份、分段规则版本
    与全部声音参数。

    输入为可 JSON 序列化的字典（不含明文入账本——只有本摘要入账）。
    使用 MEMOIR_AUDIO_INPUT_HMAC_KEY 做 HMAC-SHA256（密钥只进摘要计算，
    绝不入账本/日志）；账本幂等指纹由此密钥域保护，与对象 scope 摘要密钥
    （MEMORY_AUDIO_SCOPE_HMAC_KEY，两仓共享）相互隔离。
    """
    canonical = json.dumps(
        {"v": INPUT_HMAC_VERSION, **payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hmac.new(
        key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


@dataclass
class _AudioRefs:
    """一次 generate 的 Run 定位元组（全部受信任字段，可入日志的只有计数）。"""

    business_id: str
    archive_id: str
    run_id: str
    generation_epoch: int
    package_version: str
    scope_hex: str
    lease_owner: str


@dataclass
class _RunCtx:
    """单次 generate 的共享执行上下文：预算时钟 + 全局停止原因。"""

    deadline: float
    stop_reason: str | None = None
    # 已成功交付的旁白/配乐计数（仅安全计数，可入节点结果）。
    narrations: list[dict[str, object]] = field(default_factory=list)
    background_music: dict[str, object] | None = None
    # R3 专用上传线程池：由 generate() 创建并在节点返回前
    # shutdown(wait=False)。绝不用事件循环默认执行器——asyncio.run 退出
    # 时会等待默认执行器排空，慢上传会拖住节点返回（时间上限失效）。
    executor: ThreadPoolExecutor | None = None

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def should_stop(self) -> str | None:
        """返回停止原因：全局门禁失效 / 预算耗尽；None 表示可继续。"""
        if self.stop_reason is not None:
            return self.stop_reason
        if self.remaining() <= 0:
            return "AUDIO_DEADLINE_EXHAUSTED"
        return None


class MemoirAudioService:
    """场景旁白 + 作品 BGM 协调器；generate() 永不抛异常、永不改场景。"""

    def __init__(
        self,
        *,
        tts_client: Any,
        music_client: Any,
        uploader: Any,
        transcoder: Any,
        downloader: Any,
        jobs_service: MemoirAudioJobsService,
        config: MemoirAudioConfig,
        session: Any = None,
    ) -> None:
        self._tts = tts_client
        self._music = music_client
        self._uploader = uploader
        self._transcoder = transcoder
        self._downloader = downloader
        self._jobs = jobs_service
        self.config = config
        self._session = session
        self._worker_slots = _get_worker_semaphore(config.worker_concurrency)

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def generate(
        self, run: Any, playback_document: dict[str, object],
        *, lease_context: Any = None,
    ) -> dict[str, object]:
        """为已审核文档生成音频，返回 audio 契约对象。

        返回形状（2.0.0 文档 audio 键）：
        {"narrations": [{scene_id, media_id, object_key, mime, duration_ms}],
         "background_music": {media_id, object_key, mime, duration_ms} | None}
        任何失败都有界降级：缺旁白 / 无 BGM / 空音频，绝不抛异常、绝不修改
        scenes/actions（正文生成后不可再改）。
        """
        scenes = playback_document.get("scenes")
        scenes = scenes if isinstance(scenes, list) else []
        budget = self._audio_budget_seconds(run)
        if budget <= 0:
            # 图文链路已耗尽 Run 剩余预算（或预留后无余量）：音频整体跳过。
            logging.warning(
                "MemoirAgent 音频预算不足跳过 run_id=%s code=%s budget=%.1f",
                getattr(run, "run_id", ""), "AUDIO_BUDGET_EXHAUSTED", budget,
            )
            return {"narrations": [], "background_music": None}
        refs = self._resolve_refs(run)
        if refs is None:
            logging.warning(
                "MemoirAgent 音频 Run 引用非法跳过 run_id=%s code=%s",
                getattr(run, "run_id", ""), "AUDIO_REFS_INVALID",
            )
            return {"narrations": [], "background_music": None}

        ctx = _RunCtx(
            deadline=time.monotonic() + budget,
            # R3：本节点专用上传线程池（旁白并发 + BGM 最多 3 路同时上传，
            # 池深 4 留余量）。生命周期严格限定在本次节点执行内。
            executor=ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="memoir-audio-upload"
            ),
        )
        candidates = [
            (str(scene["scene_id"]), str(scene["body"]))
            for scene in scenes
            if isinstance(scene, dict)
            and isinstance(scene.get("scene_id"), str)
            and scene["scene_id"]
            and isinstance(scene.get("body"), str)
            and scene["body"].strip()
        ]
        # 旁白与 BGM 并发：BGM 提交/轮询与场景 TTS 互不阻塞；
        # asyncio.run 为每次节点执行创建独立事件循环（Worker 同步节点边界）。
        try:
            asyncio.run(
                self._orchestrate(ctx, refs, candidates, lease_context)
            )
        except Exception:
            # 编排层意外异常按全量降级处理：保留已成功条目，绝不阻断发布。
            logging.warning(
                "MemoirAgent 音频编排异常降级 run_id=%s code=%s",
                refs.run_id, "AUDIO_ORCHESTRATION_FAILED",
            )
        finally:
            # R3 节点返回护栏：wait=False 不等待在途慢上传线程自然结束
            # （cancel_futures 丢弃未开始的上传任务），节点返回时间与上传
            # 线程耗时彻底解耦——时间上限真正约束外层返回。线程跑完当前
            # 上传后自行退出；其结果不被本执行采信（record_upload 不会被
            # 迟到调用，即使被其他执行者补写也会被 R4 fence 拒绝，见
            # _upload_audio_bytes 注释）。
            ctx.executor.shutdown(wait=False, cancel_futures=True)
        # 场景并发完成后按文档场景序排序：并发完成顺序不稳定，不排序会让
        # 相同输入在恢复重算时产出不同 narrations 顺序 → 文档 digest 漂移。
        order = {scene_id: index for index, (scene_id, _body) in enumerate(candidates)}
        ordered_narrations = sorted(
            ctx.narrations,
            key=lambda entry: order.get(str(entry.get("scene_id")), len(order)),
        )
        logging.info(
            "MemoirAgent 音频节点完成 run_id=%s narrations=%s bgm=%s stop=%s "
            "code=MEMOIR_AUDIO_NODE_DONE",
            refs.run_id, len(ordered_narrations),
            ctx.background_music is not None, ctx.stop_reason or "none",
        )
        return {
            "narrations": ordered_narrations,
            "background_music": ctx.background_music,
        }

    # ------------------------------------------------------------------
    # 预算与引用
    # ------------------------------------------------------------------

    def _audio_budget_seconds(self, run: object) -> float:
        """音频可用秒数：三个上限取最小再减发布预留。

        与单请求剩余 deadline 一致性：Run 活跃预算口径与
        ModelCallContext 相同（max_run_seconds*1000 - active_elapsed_ms），
        墙钟口径与 ToolGateway deadline 相同（run_deadline_at）；发布预留
        MEMOIR_AUDIO_PUBLISH_RESERVE_SECONDS 保证音频耗尽后 publish 仍有
        单请求可用的剩余时间窗。
        """
        caps: list[float] = [self.config.node_timeout_seconds]
        snapshot = getattr(run, "capability_snapshot_json", None)
        if isinstance(snapshot, dict):
            execution_policy = snapshot.get("execution_policy")
            if isinstance(execution_policy, dict):
                max_run_seconds = execution_policy.get("max_run_seconds")
                active_elapsed_ms = getattr(run, "active_elapsed_ms", 0)
                if (
                    isinstance(max_run_seconds, int)
                    and not isinstance(max_run_seconds, bool)
                    and max_run_seconds > 0
                    and isinstance(active_elapsed_ms, int)
                ):
                    caps.append((max_run_seconds * 1000.0 - active_elapsed_ms) / 1000.0)
        wall_deadline = getattr(run, "run_deadline_at", None)
        if wall_deadline is not None:
            from datetime import UTC, datetime

            now = datetime.now(UTC)
            expires = wall_deadline
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            caps.append((expires - now).total_seconds())
        return min(caps) - self.config.publish_reserve_seconds

    def _resolve_refs(self, run: object) -> _AudioRefs | None:
        """提取受信任 Run 引用并计算 scope；非法引用返回 None（整体跳过）。"""
        input_json = getattr(run, "input_json", None)
        archive_id = input_json.get("archive_id") if isinstance(input_json, dict) else None
        epoch = input_json.get("generation_epoch") if isinstance(input_json, dict) else None
        run_id = getattr(run, "run_id", None)
        business_id = getattr(run, "business_id", None)
        package_version = getattr(run, "agent_version", None)
        if (
            not isinstance(archive_id, str) or not archive_id
            or isinstance(epoch, bool) or not isinstance(epoch, int)
            or not isinstance(run_id, str) or not run_id
            or not isinstance(business_id, str) or not business_id
            or not isinstance(package_version, str) or not package_version
        ):
            return None
        try:
            scope_hex = compute_audio_scope(
                self.config.scope_hmac_key,
                business_id=business_id,
                archive_id=archive_id,
                run_id=run_id,
                generation_epoch=epoch,
            )
        except Exception:
            return None
        return _AudioRefs(
            business_id=business_id,
            archive_id=archive_id,
            run_id=run_id,
            generation_epoch=epoch,
            package_version=package_version,
            scope_hex=scope_hex,
            # lease_owner 绑定执行尝试：崩溃恢复后新尝试沿用账本行内当前
            # fencing token 写入（token 校验而非 owner 校验），旧 token 自动被拒。
            lease_owner=f"audio:{run_id}:attempt-{getattr(run, 'execution_attempt', 1) or 1}",
        )

    # ------------------------------------------------------------------
    # 编排
    # ------------------------------------------------------------------

    async def _orchestrate(
        self,
        ctx: _RunCtx,
        refs: _AudioRefs,
        candidates: list[tuple[str, str]],
        lease_context: Any,
    ) -> None:
        # BGM 是作品级需求：即使全部场景正文为空也提交一首（作品级配乐不
        # 依赖场景正文）；预算/门禁由 _generate_background_music 自查。
        bgm_task: asyncio.Task[None] = asyncio.create_task(
            self._generate_background_music(ctx, refs, lease_context)
        )
        scene_gate = asyncio.Semaphore(self.config.scene_concurrency)

        async def _scene_task(scene_id: str, body: str) -> None:
            async with scene_gate:
                if ctx.should_stop() is not None:
                    return
                entry = await self._narrate_scene(ctx, refs, scene_id, body, lease_context)
                if entry is not None:
                    ctx.narrations.append(entry)

        await asyncio.gather(*(_scene_task(sid, body) for sid, body in candidates))
        await bgm_task

    # ------------------------------------------------------------------
    # 场景旁白
    # ------------------------------------------------------------------

    async def _narrate_scene(
        self,
        ctx: _RunCtx,
        refs: _AudioRefs,
        scene_id: str,
        body: str,
        lease_context: Any,
    ) -> dict[str, object] | None:
        """单场景旁白：复用 → 分段合成 → 拼接上传；任何失败返回 None。"""
        voice = self._voice_params()
        # narr_hmac 同时充当该场景完整正文的身份摘要：分段指纹也会携带它
        # （只传摘要、绝不传正文），保证正文任意位置（含尾部）变化都会使
        # 该场景全部分段指纹变化，账本不会跨正文版本误复用分段资产。
        narr_hmac = compute_input_hmac(
            self.config.input_hmac_key, {**voice, "role": ROLE_NARRATION, "text": body}
        )
        # 恢复复用：同 Run 同输入已有成功资产（uploaded/published）直接复用，
        # 不重复扣费；media_id 由槽位确定性派生，复用与新生成完全一致。
        reused = self._jobs.find_reusable_uploaded_asset(
            run_id=refs.run_id,
            generation_epoch=refs.generation_epoch,
            package_version=refs.package_version,
            role=ROLE_NARRATION,
            scene_id=scene_id,
            input_hmac=narr_hmac,
        )
        if reused is not None and reused.object_key and reused.duration_ms:
            logging.info(
                "MemoirAgent 旁白资产复用 run_id=%s scene=%s code=MEMOIR_AUDIO_ASSET_REUSED",
                refs.run_id, scene_id,
            )
            return self._narration_entry(scene_id, narr_hmac, refs, reused)
        segments = split_narration_segments(body)
        if not segments:
            return None
        audio_parts: list[bytes] = []
        for index, segment_text in enumerate(segments):
            segment_audio = await self._synthesize_segment(
                ctx, refs, scene_id, index, segment_text, voice, narr_hmac,
                lease_context,
            )
            if segment_audio is None:
                # 任一分段失败：整场景降级（TTS 不可断点续传，不重新付费补齐）。
                logging.warning(
                    "MemoirAgent 场景旁白降级 run_id=%s scene=%s segment=%s/%s "
                    "code=MEMOIR_AUDIO_SCENE_DEGRADED",
                    refs.run_id, scene_id, index + 1, len(segments),
                )
                return None
            audio_parts.append(segment_audio)
        return await self._upload_narration(
            ctx, refs, scene_id, narr_hmac, audio_parts, lease_context
        )

    async def _synthesize_segment(
        self,
        ctx: _RunCtx,
        refs: _AudioRefs,
        scene_id: str,
        segment_index: int,
        segment_text: str,
        voice: dict[str, object],
        full_body_hmac: str,
        lease_context: Any,
    ) -> bytes | None:
        """单段 TTS：预留 → 提交 → 结算；限流允许一次退避重试。"""
        # 分段指纹必须同时携带 segment_index 与完整场景正文摘要（只传
        # HMAC 摘要，不传正文）：正文尾部变化也改变该场景所有段指纹。
        seg_hmac = compute_input_hmac(
            self.config.input_hmac_key,
            {
                **voice,
                "role": ROLE_SEGMENT,
                "segment_index": segment_index,
                "text": segment_text,
                "full_body_hmac": full_body_hmac,
            },
        )
        for _attempt in range(MAX_SUBMIT_ATTEMPTS):
            if not self._precheck(ctx, segment_text):
                return None
            existing = self._jobs.find_job(
                run_id=refs.run_id,
                generation_epoch=refs.generation_epoch,
                package_version=refs.package_version,
                role=ROLE_SEGMENT,
                scene_id=scene_id,
                input_hmac=seg_hmac,
                segment_index=segment_index,
            )
            if existing is not None and not self._segment_slot_retryable(existing):
                # 在途/未知/已结算/重试耗尽的分段槽：不能安全重提（可能重复
                # 计费），整场景降级——与"TTS 不可断点续传"设计口径一致。
                return None
            reservation = MemoirAudioJobReservation(
                job_id=self._job_id(refs, ROLE_SEGMENT, scene_id, seg_hmac, segment_index),
                business_id=refs.business_id,
                run_id=refs.run_id,
                generation_epoch=refs.generation_epoch,
                package_version=refs.package_version,
                role=ROLE_SEGMENT,
                scene_id=scene_id,
                input_hmac=seg_hmac,
                estimated_cost=estimate_tts_cost(segment_text, self._require_policy()),
                lease_owner=refs.lease_owner,
                lease_ttl_seconds=_JOB_LEASE_TTL_SECONDS,
                segment_index=segment_index,
            )
            try:
                outcome = self._jobs.reserve_job(reservation)
                job = outcome.job
            except MemoirAudioJobsError as exc:
                self._absorb_jobs_error(ctx, exc.code, "segment_reserve")
                return None
            token = job.lease_token
            try:
                self._jobs.mark_submitting(job.job_id, token)
            except MemoirAudioJobsError as exc:
                self._absorb_jobs_error(ctx, exc.code, "segment_submitting")
                return None
            # R1 外发硬门禁：费用预留与 submitting 状态必须先在独立事务中
            # 真实 commit 成功（跨 Session 可见），才允许调用付费 Provider；
            # commit 抛错/结果未知一律视为未持久化，外发调用必须为零。
            if not self._commit_ledger(ctx):
                return None
            try:
                result = await self._call_with_slot(
                    ctx, self._tts.synthesize_segment(segment_text),
                    timeout=min(self.config.tts_request_timeout_seconds, ctx.remaining()),
                )
            except MemoirAudioProviderError as exc:
                # 提交后失败：未知结果（可能已部分计费）标 submission_unknown
                # 终态，永不自动重提；仅限流（明确未受理）允许一次重试。
                self._mark_provider_failure(ctx, job.job_id, token, exc.code)
                if exc.code in _RATE_LIMIT_CODES:
                    # 明确未受理限流：固定 1 秒退避后走唯一一次重试。
                    await asyncio.sleep(1.0)
                    continue
                return None
            except Exception:
                # 网络层意外异常（无法证明未受理）：同样按未知终态处理。
                self._mark_provider_failure(ctx, job.job_id, token, "TTS_UNKNOWN_ERROR")
                return None
            try:
                self._jobs.mark_submitted(job.job_id, token)
                self._jobs.settle_tts_usage(
                    job.job_id, token, usage_text_words=result.text_words
                )
            except MemoirAudioJobsError as exc:
                self._absorb_jobs_error(ctx, exc.code, "segment_settle")
                return None
            # 结算也必须真实落库：commit 失败说明该段用量账目不可确认，
            # 不交付音频（后续上传等副作用由各处门禁一并拦截）。
            if not self._commit_ledger(ctx):
                return None
            self._heartbeat_run_lease(ctx, refs, lease_context)
            return result.audio
        return None

    async def _upload_narration(
        self,
        ctx: _RunCtx,
        refs: _AudioRefs,
        scene_id: str,
        narr_hmac: str,
        audio_parts: list[bytes],
        lease_context: Any,
    ) -> dict[str, object] | None:
        """分段全部成功后：ffmpeg 解码重拼 → 私有上传 → 记录账本资产行。"""
        try:
            # R3：本地 ffmpeg 拼接是 CPU 密集调用，必须受节点剩余时限
            # （ctx.remaining()）约束，防止拼接耗时吃光预算导致后续步骤
            # 全部超时。超时抛 TimeoutError，被下方 except Exception 按
            # AUDIO_TRANSCODE_FAILED 降级，语义与原先一致。
            concat = await self._call_with_slot(
                ctx, self._transcoder.concat_mp3_segments(audio_parts),
                timeout=ctx.remaining(),
            )
        except Exception:
            logging.warning(
                "MemoirAgent 旁白拼接失败降级 run_id=%s scene=%s code=AUDIO_TRANSCODE_FAILED",
                refs.run_id, scene_id,
            )
            return None
        reservation = MemoirAudioJobReservation(
            job_id=self._job_id(refs, ROLE_NARRATION, scene_id, narr_hmac),
            business_id=refs.business_id,
            run_id=refs.run_id,
            generation_epoch=refs.generation_epoch,
            package_version=refs.package_version,
            role=ROLE_NARRATION,
            scene_id=scene_id,
            input_hmac=narr_hmac,
            # 本地拼接 + 上传不产生供应商费用：零成本槽只记资产对账，不占预算。
            estimated_cost=Decimal("0"),
            lease_owner=refs.lease_owner,
            lease_ttl_seconds=_JOB_LEASE_TTL_SECONDS,
        )
        try:
            outcome = self._jobs.reserve_job(reservation)
            job = outcome.job
            if outcome.outcome == "reused" and job.object_key and job.duration_ms:
                return self._narration_entry(scene_id, narr_hmac, refs, job)
            token = job.lease_token
            object_key = build_audio_object_key(
                self.config.narrator_prefix, refs.scope_hex, role=AUDIO_ROLE_NARRATOR
            )
            self._jobs.record_object_key(job.job_id, token, object_key, AUDIO_MIME)
            # R2 稳定键先行持久化：object_key 必须在上传 OSS 前真实 commit
            # （跨 Session 可见），上传成功后即使进程立即崩溃，重启也能凭
            # 账本行定位该对象做对账/清理，不产生不可追踪的孤儿对象。
            # commit 失败则禁止上传——否则会上传一个账本里找不到键的对象。
            if not self._commit_ledger(ctx):
                return None
            # R3：上传走节点专用线程池 + 剩余时限（详见 _upload_audio_bytes）。
            # 迟到副作用受控依据：R2 已保证 object_key 先持久化（上面独立
            # commit），record_upload 受 lease token 门禁，超时后本 Session
            # 不再写账，job 留在非 uploaded 态由维护对账收敛（可追踪不孤儿）。
            await self._upload_audio_bytes(ctx, refs, concat.audio, object_key)
            self._jobs.record_upload(
                job.job_id, token, duration_ms=concat.duration_ms
            )
        except MemoirAudioJobsError as exc:
            self._absorb_jobs_error(ctx, exc.code, "narration_upload")
            return None
        except Exception as exc:
            logging.warning(
                "MemoirAgent 旁白上传失败降级 run_id=%s scene=%s code=AUDIO_UPLOAD_FAILED",
                refs.run_id, scene_id,
            )
            # 节点内 wait_for 超时会落入本 except，但不得 mark_failed：
            # 超时后账本保持持键 reserved，迟到 record_upload 仍走 token
            # fencing（R3）；收敛交给 fail_abandoned_keyed_jobs（§2.2）。
            # concat 失败在 reserve 之前，由上方独立 except 返回，不 mark。
            if not isinstance(exc, TimeoutError):
                try:
                    # 复活安全性（冻结文档 §2.4）：持键旁白费用已入账（零成本槽），
                    # mark_failed / 复活不重复收费；复活保留同一 object_key，
                    # 重试上传同键，对象被删则重建。迟到 token 已被 rotate 时
                    # fencing 拒绝是正常竞态，此处吸收。24h 窗 ≫ Run 生命周期。
                    self._jobs.mark_failed(
                        job.job_id, token, error_code="AUDIO_UPLOAD_FAILED"
                    )
                    self._commit_ledger(ctx)
                except Exception:
                    logging.warning(
                        "MemoirAgent 旁白失败入账被吸收 run_id=%s "
                        "code=AUDIO_UPLOAD_FAILED",
                        refs.run_id,
                    )
            return None
        self._commit_ledger(ctx)
        self._heartbeat_run_lease(ctx, refs, lease_context)
        return {
            "scene_id": scene_id,
            "media_id": self._media_id("audio-narr", refs, scene_id, narr_hmac),
            "object_key": object_key,
            "mime": AUDIO_MIME,
            "duration_ms": concat.duration_ms,
        }

    def _narration_entry(
        self, scene_id: str, narr_hmac: str, refs: _AudioRefs, job: MemoirAudioJob
    ) -> dict[str, object]:
        """由已上传账本行构造发布条目（恢复复用与新生成同形状）。"""
        return {
            "scene_id": scene_id,
            "media_id": self._media_id("audio-narr", refs, scene_id, narr_hmac),
            "object_key": job.object_key,
            "mime": job.mime or AUDIO_MIME,
            "duration_ms": job.duration_ms or 0,
        }

    # ------------------------------------------------------------------
    # 背景音乐
    # ------------------------------------------------------------------

    async def _generate_background_music(
        self, ctx: _RunCtx, refs: _AudioRefs, lease_context: object
    ) -> None:
        """作品级 BGM：按部署模式分叉（付费生成 / 默认零费复制）；失败置 None。"""
        if self.config.music_generation_enabled:
            await self._generate_volcano_background_music(ctx, refs, lease_context)
            return
        await self._generate_default_background_music(ctx, refs, lease_context)

    async def _generate_default_background_music(
        self, ctx: _RunCtx, refs: _AudioRefs, lease_context: Any
    ) -> None:
        """默认配乐（freeze 2026-09-11 §5/§6）：零费复制默认源为独立私有副本。

        状态流（永不 mark_submitting/submitted/processing，零火山调用）：
        读源 → 域拆分指纹 → 复用检查 → 同 Run 互斥核对 → 零成本预留 →
        ffprobe 实测时长转码 → 持键独立 commit → 上传副本 → 零费结算 →
        record_upload。任何失败仅降级为无配乐（旁白与图文照常），绝不自动
        切换付费生成。
        """
        if ctx.should_stop() is not None:
            return
        # 1. 读默认源：指纹携带源字节 SHA256（freeze §5：源换代 → 新指纹），
        #    必须先读源才能计算指纹；读取失败仅降级 BGM，绝不切付费生成。
        try:
            data = await self._download_default_source(ctx)
        except Exception as exc:
            # reason 只带安全维度：存储层 MemoirAudioStorageError 携带
            # 安全枚举 .code，其余异常退化为类名——绝不携带 key/凭据/字节。
            logging.warning(
                "MemoirAgent 默认配乐源读取失败降级 run_id=%s reason=%s "
                "code=AUDIO_SOURCE_READ_FAILED",
                refs.run_id,
                getattr(exc, "code", type(exc).__name__),
            )
            return
        if not data:
            # 空字节源等同损坏：降级无配乐（拒绝语义在转码入口，这里不转码）。
            logging.warning(
                "MemoirAgent 默认配乐源为空降级 run_id=%s code=AUDIO_TRANSCODE_INPUT_INVALID",
                refs.run_id,
            )
            return
        # 2. 域拆分指纹：mode/default 域标记 + source_key + 源字节摘要，
        #    与火山分支（action/duration/model）键集不同，canonical JSON
        #    不可能相等。
        bgm_hmac = compute_input_hmac(
            self.config.input_hmac_key,
            {
                "role": ROLE_BACKGROUND_MUSIC,
                "mode": "default",
                "source_key": self.config.default_bgm_object_key,
                "package_version": refs.package_version,
                "content_digest": hashlib.sha256(data).hexdigest(),
            },
        )
        # 3. 恢复复用：同 Run 同源（同指纹）已有成功资产直接复用不再上传。
        reused = self._jobs.find_reusable_uploaded_asset(
            run_id=refs.run_id,
            generation_epoch=refs.generation_epoch,
            package_version=refs.package_version,
            role=ROLE_BACKGROUND_MUSIC,
            scene_id=WORK_SCENE_ID,
            input_hmac=bgm_hmac,
        )
        if reused is not None and reused.object_key and reused.duration_ms:
            logging.info(
                "MemoirAgent 默认配乐资产复用 run_id=%s code=MEMOIR_AUDIO_ASSET_REUSED",
                refs.run_id,
            )
            ctx.background_music = self._bgm_entry(refs, bgm_hmac, reused)
            return
        # 4. 同 Run 互斥（freeze §4.3/§11.1 两模式共用防线）：火山在途/已结算
        #    行或不同指纹默认行（源换代）存在 → 保守降级，不建第二槽。
        if self._bgm_mutex_degraded(refs, bgm_hmac, mode="default"):
            return
        existing = self._jobs.find_job(
            run_id=refs.run_id,
            generation_epoch=refs.generation_epoch,
            package_version=refs.package_version,
            role=ROLE_BACKGROUND_MUSIC,
            scene_id=WORK_SCENE_ID,
            input_hmac=bgm_hmac,
            segment_index=NO_SEGMENT_INDEX,
        )
        if existing is not None and not self._bgm_slot_retryable(existing):
            return
        # 5. 零成本预留：不占预算、不带音乐秒数（freeze §4.1 裁决）。
        reservation = MemoirAudioJobReservation(
            job_id=self._job_id(refs, ROLE_BACKGROUND_MUSIC, WORK_SCENE_ID, bgm_hmac),
            business_id=refs.business_id,
            run_id=refs.run_id,
            generation_epoch=refs.generation_epoch,
            package_version=refs.package_version,
            role=ROLE_BACKGROUND_MUSIC,
            scene_id=WORK_SCENE_ID,
            input_hmac=bgm_hmac,
            estimated_cost=Decimal("0"),
            lease_owner=refs.lease_owner,
            lease_ttl_seconds=_JOB_LEASE_TTL_SECONDS,
            requested_music_seconds=None,
        )
        try:
            outcome = self._jobs.reserve_job(reservation)
            job = outcome.job
        except MemoirAudioJobsError as exc:
            self._absorb_jobs_error(ctx, exc.code, "default_bgm_reserve")
            # S2（2026-09-15 §11.1）：reserve 已在外层事务取得 Run/BGM
            # 行锁。SAVEPOINT 不是放锁。提交结束持锁事务，旁白账本
            # 一并落库。禁止 session.rollback()。
            self._commit_ledger(ctx)
            return
        if outcome.outcome == "reused" and job.object_key and job.duration_ms:
            ctx.background_music = self._bgm_entry(refs, bgm_hmac, job)
            return
        token = job.lease_token
        # 6. 转码实测时长：ffprobe 真实整数毫秒，绝不默认 60s；空字节/损坏
        #    由转码入口拒绝 → 仅降级无配乐（零费槽无对象键，等待 lease 过期
        #    由维护对账收敛；源损坏确定性失败，重试无意义）。
        try:
            concat = await self._call_with_slot(
                ctx, self._transcoder.transcode_to_mp3(data),
                timeout=ctx.remaining(),
            )
        except Exception:
            logging.warning(
                "MemoirAgent 默认配乐转码失败降级 run_id=%s code=AUDIO_TRANSCODE_FAILED",
                refs.run_id,
            )
            return
        # 7. 副本落在现行 background 工作目录：随机不透明名，绝不共享源 key；
        #    源 key 永不进入账本 object_key / audio_object_keys / 发布清单。
        #    D3（freeze §11.2）：复活槽已持旧键时必须复用同键重试上传——
        #    无条件再生成新键会被 jobs 层 MEMOIR_AUDIO_OBJECT_KEY_CONFLICT
        #    拒绝并整槽降级；同键 record_object_key 幂等，仅无键槽才建新键。
        object_key = job.object_key or build_audio_object_key(
            self.config.background_prefix, refs.scope_hex,
            role=AUDIO_ROLE_BACKGROUND,
        )
        try:
            self._jobs.record_object_key(job.job_id, token, object_key, AUDIO_MIME)
            # R2 铁律：副本键先独立事务真实 commit 再上传 OSS（同旁白口径）。
            if not self._commit_ledger(ctx):
                return
            await self._upload_audio_bytes(ctx, refs, concat.audio, object_key)
            # 零费结算（freeze §4.2/§7）：上传成功后写 settled_cost=0；幂等，
            # 付费已结算会被 MEMOIR_AUDIO_SETTLE_CONFLICT 拒绝。
            self._jobs.settle_default_music_usage(job.job_id, token)
            self._jobs.record_upload(
                job.job_id, token, duration_ms=concat.duration_ms
            )
        except MemoirAudioJobsError as exc:
            self._absorb_jobs_error(ctx, exc.code, "default_bgm_finalize")
            return
        except Exception as exc:
            # 上传超时不得 mark_failed（迟到副作用由 token fencing + 维护
            # 对账收敛）；非超时失败转 failed 释放槽位——零费槽复活重试
            # 不产生第二笔费用。
            logging.warning(
                "MemoirAgent 默认配乐交付失败降级 run_id=%s code=AUDIO_UPLOAD_FAILED",
                refs.run_id,
            )
            if not isinstance(exc, TimeoutError):
                try:
                    self._jobs.mark_failed(
                        job.job_id, token, error_code="AUDIO_UPLOAD_FAILED"
                    )
                    self._commit_ledger(ctx)
                except Exception:
                    logging.warning(
                        "MemoirAgent 默认配乐失败入账被吸收 run_id=%s "
                        "code=AUDIO_UPLOAD_FAILED",
                        refs.run_id,
                    )
            return
        self._commit_ledger(ctx)
        self._heartbeat_run_lease(ctx, refs, lease_context)
        ctx.background_music = {
            "media_id": self._media_id("audio-bgm", refs, WORK_SCENE_ID, bgm_hmac),
            "object_key": object_key,
            "mime": AUDIO_MIME,
            "duration_ms": concat.duration_ms,
        }

    async def _download_default_source(self, ctx: _RunCtx) -> bytes:
        """读默认源字节：同步 SDK 读取走节点专用线程池，受剩余时限约束。

        字节上限 max_file_bytes（契约 MEMOIR_AUDIO_MAX_FILE_BYTES）、超时为
        节点剩余时间；存储层保证超时后调用方立即拿到失败不阻塞。
        """
        if ctx.executor is None:
            # 防御：generate() 必定注入专用执行器；缺失说明内部约定被破坏。
            raise MemoirAudioProviderError(
                "AUDIO_UPLOAD_EXECUTOR_MISSING", "音频上传执行器未初始化"
            )
        loop = asyncio.get_running_loop()
        download = functools.partial(
            self._uploader.download_private_bytes,
            self.config.default_bgm_object_key,
            max_bytes=self.config.max_file_bytes,
            timeout_seconds=ctx.remaining(),
        )
        return await asyncio.wait_for(
            loop.run_in_executor(ctx.executor, download),
            timeout=ctx.remaining(),
        )

    async def _generate_volcano_background_music(
        self, ctx: _RunCtx, refs: _AudioRefs, lease_context: object
    ) -> None:
        """火山原创配乐付费链路（现行行为，一字不动）：复用 → 在途续查 →
        新提交轮询；失败置 None。"""
        bgm_hmac = compute_input_hmac(
            self.config.input_hmac_key,
            {
                "role": ROLE_BACKGROUND_MUSIC,
                "action": str(getattr(self._music, "_action", "")),
                "duration": self.config.music_duration_seconds,
                "model": "v5.0",
            },
        )
        reused = self._jobs.find_reusable_uploaded_asset(
            run_id=refs.run_id,
            generation_epoch=refs.generation_epoch,
            package_version=refs.package_version,
            role=ROLE_BACKGROUND_MUSIC,
            scene_id=WORK_SCENE_ID,
            input_hmac=bgm_hmac,
        )
        if reused is not None and reused.object_key and reused.duration_ms:
            logging.info(
                "MemoirAgent 配乐资产复用 run_id=%s code=MEMOIR_AUDIO_ASSET_REUSED",
                refs.run_id,
            )
            ctx.background_music = self._bgm_entry(refs, bgm_hmac, reused)
            return
        job: MemoirAudioJob | None = self._jobs.find_pending_music_task(
            run_id=refs.run_id,
            generation_epoch=refs.generation_epoch,
            package_version=refs.package_version,
            input_hmac=bgm_hmac,
        )
        token: int
        task_id: str
        if job is not None:
            # 在途任务续查：只查询供应商状态，绝不重新提交产生第二笔费用。
            # R4：恢复接管必须先旋转 lease token——旋转后上一 Session 持旧
            # token 的迟到 renew/mark/upload 全部被 fencing 拒绝，旧 worker
            # 零新增上传/账本写入；本 Session 后续写入一律用新 token。
            task_id = str(job.provider_task_id)
            token = job.lease_token
            try:
                rotated = self._jobs.rotate_lease(
                    job.job_id, token,
                    owner=refs.lease_owner, ttl_seconds=_JOB_LEASE_TTL_SECONDS,
                )
                token = rotated.lease_token
                self._jobs.renew_lease(job.job_id, token, ttl_seconds=_JOB_LEASE_TTL_SECONDS)
                self._jobs.mark_processing(job.job_id, token)
            except MemoirAudioJobsError as exc:
                self._absorb_jobs_error(ctx, exc.code, "bgm_resume")
                return
            self._commit_ledger(ctx)
        else:
            submitted = await self._submit_background_music(ctx, refs, bgm_hmac)
            if submitted is None:
                return
            job, token, task_id = submitted
        ctx.background_music = await self._poll_background_music(
            ctx, refs, bgm_hmac, job, token, task_id, lease_context
        )

    async def _submit_background_music(
        self, ctx: _RunCtx, refs: _AudioRefs, bgm_hmac: str
    ) -> tuple[MemoirAudioJob, int, str] | None:
        """新提交一首 BGM；限流允许一次退避重试，未知结果永不重提。"""
        for _attempt in range(MAX_SUBMIT_ATTEMPTS):
            if ctx.should_stop() is not None:
                return None
            # D2 共用防线（freeze §11.1）：作品级互斥判定放在提交尝试循环内
            # ——建槽/提交付费任务之前必须完成；429 限流重试后回到循环顶部
            # 重新判定（重试期间其他执行者可能已为同一作品建了 BGM 槽）。
            # S2（2026-09-14 冻结裁决）：本判定是咨询性快速路径，不持任何
            # 锁；权威互斥由 reserve_job 在 AgentRun 行锁下的 hmac-less 守卫
            # 强制。与 reserve_job 之间保持无 commit：判定、占槽在同一数据
            # 库事务边界内完成，缩小（而非消除）判定与占槽之间的竞窗。
            if self._bgm_mutex_degraded(refs, bgm_hmac, mode="volcano"):
                return None
            existing = self._jobs.find_job(
                run_id=refs.run_id,
                generation_epoch=refs.generation_epoch,
                package_version=refs.package_version,
                role=ROLE_BACKGROUND_MUSIC,
                scene_id=WORK_SCENE_ID,
                input_hmac=bgm_hmac,
                segment_index=NO_SEGMENT_INDEX,
            )
            if existing is not None and not self._bgm_slot_retryable(existing):
                return None
            reservation = MemoirAudioJobReservation(
                job_id=self._job_id(refs, ROLE_BACKGROUND_MUSIC, WORK_SCENE_ID, bgm_hmac),
                business_id=refs.business_id,
                run_id=refs.run_id,
                generation_epoch=refs.generation_epoch,
                package_version=refs.package_version,
                role=ROLE_BACKGROUND_MUSIC,
                scene_id=WORK_SCENE_ID,
                input_hmac=bgm_hmac,
                estimated_cost=estimate_music_cost(
                    self.config.music_duration_seconds, self._require_policy()
                ),
                lease_owner=refs.lease_owner,
                lease_ttl_seconds=_JOB_LEASE_TTL_SECONDS,
                requested_music_seconds=self.config.music_duration_seconds,
            )
            try:
                outcome = self._jobs.reserve_job(reservation)
                job = outcome.job
            except MemoirAudioJobsError as exc:
                self._absorb_jobs_error(ctx, exc.code, "bgm_reserve")
                # S2（2026-09-15 §11.1）：与默认路径同一放锁合同——
                # 吸收已加锁的 reserve 拒绝后必须结束外层事务。
                self._commit_ledger(ctx)
                return None
            token = job.lease_token
            try:
                self._jobs.mark_submitting(job.job_id, token)
            except MemoirAudioJobsError as exc:
                self._absorb_jobs_error(ctx, exc.code, "bgm_submitting")
                return None
            # R1 外发硬门禁：与分段 TTS 相同——音乐费用预留与 submitting
            # 状态先真实 commit 成功，才允许提交付费生成任务。
            if not self._commit_ledger(ctx):
                return None
            try:
                task_id = await self._call_with_slot(
                    ctx, self._music.submit_generation(),
                    timeout=ctx.remaining(),
                )
            except MemoirAudioProviderError as exc:
                self._mark_provider_failure(ctx, job.job_id, token, exc.code)
                if exc.code in _RATE_LIMIT_CODES:
                    # 明确未受理限流：固定 1 秒退避后走唯一一次重试。
                    await asyncio.sleep(1.0)
                    continue
                return None
            except Exception:
                self._mark_provider_failure(ctx, job.job_id, token, "MUSIC_UNKNOWN_ERROR")
                return None
            try:
                # 提交返回立即存 TaskID：此后任何崩溃恢复都只查不重建。
                self._jobs.mark_submitted(
                    job.job_id, token, provider_task_id=task_id
                )
            except MemoirAudioJobsError as exc:
                self._absorb_jobs_error(ctx, exc.code, "bgm_submitted")
                return None
            # TaskID 必须真实落库后才进入轮询：commit 失败时账本里没有
            # TaskID，无法对账，不得继续当作已受理任务推进。
            if not self._commit_ledger(ctx):
                return None
            return job, token, task_id
        return None

    async def _poll_background_music(
        self,
        ctx: _RunCtx,
        refs: _AudioRefs,
        bgm_hmac: str,
        job: MemoirAudioJob,
        token: int,
        task_id: str,
        lease_context: Any,
    ) -> dict[str, object] | None:
        """轮询 TaskID 至成功/失败/预算耗尽；成功后下载转码上传结算。"""
        while True:
            stop = ctx.should_stop()
            if stop is not None:
                logging.warning(
                    "MemoirAgent 配乐轮询停止 run_id=%s reason=%s code=MEMOIR_AUDIO_MUSIC_POLL_STOPPED",
                    refs.run_id, stop,
                )
                return None
            try:
                snapshot = await self._call_with_slot(
                    ctx, self._music.query_task(task_id),
                    timeout=ctx.remaining(),
                )
            except Exception:
                # 查询失败：任务仍在供应商侧，本节点停止（保留已提交费用；
                # 恢复路径可凭 TaskID 续查），不降级为失败态。
                logging.warning(
                    "MemoirAgent 配乐查询异常停止 run_id=%s code=MEMOIR_AUDIO_MUSIC_QUERY_FAILED",
                    refs.run_id,
                )
                return None
            status = snapshot.status
            if status == MUSIC_STATUS_SUCCESS:
                return await self._finalize_background_music(
                    ctx, refs, bgm_hmac, job, token,
                    str(snapshot.audio_url or ""), lease_context,
                )
            if status == MUSIC_STATUS_FAILED:
                try:
                    self._jobs.mark_failed(
                        job.job_id, token, error_code="MUSIC_GENERATION_FAILED"
                    )
                except MemoirAudioJobsError as exc:
                    self._absorb_jobs_error(ctx, exc.code, "bgm_failed")
                self._commit_ledger(ctx)
                return None
            # 0/1 等待：续约账本与 Run 租约后按部署间隔继续轮询。
            try:
                self._jobs.renew_lease(
                    job.job_id, token, ttl_seconds=_JOB_LEASE_TTL_SECONDS
                )
            except MemoirAudioJobsError as exc:
                self._absorb_jobs_error(ctx, exc.code, "bgm_renew")
                return None
            self._heartbeat_run_lease(ctx, refs, lease_context)
            await asyncio.sleep(self.config.music_poll_interval_seconds)

    async def _finalize_background_music(
        self,
        ctx: _RunCtx,
        refs: _AudioRefs,
        bgm_hmac: str,
        job: MemoirAudioJob,
        token: int,
        audio_url: str,
        lease_context: Any,
    ) -> dict[str, object] | None:
        """下载（SSRF 白名单）→ 转码 → 私有上传 → 按请求秒数结算。"""
        try:
            self._jobs.settle_music_usage(
                job.job_id, token,
                requested_music_seconds=self.config.music_duration_seconds,
            )
        except MemoirAudioJobsError as exc:
            self._absorb_jobs_error(ctx, exc.code, "bgm_settle")
            return None
        self._commit_ledger(ctx)
        try:
            # R3：BGM 下载（网络 IO）与转码（CPU 密集）各自受节点剩余时限
            # 约束，任一步超时即放弃交付；超时抛 TimeoutError 被下方
            # except Exception 按交付失败降级，费用已结算不重提，语义不变。
            data = await self._call_with_slot(
                ctx, self._downloader.download(audio_url),
                timeout=ctx.remaining(),
            )
            concat = await self._call_with_slot(
                ctx, self._transcoder.transcode_to_mp3(data),
                timeout=ctx.remaining(),
            )
            object_key = build_audio_object_key(
                self.config.background_prefix, refs.scope_hex,
                role=AUDIO_ROLE_BACKGROUND,
            )
            self._jobs.record_object_key(job.job_id, token, object_key, AUDIO_MIME)
            # R2 与旁白上传同一口径：稳定 object_key 先独立事务 commit 持久化
            # 再上传 OSS；commit 失败禁止上传，防上传后崩溃丢失对象键。
            if not self._commit_ledger(ctx):
                return None
            # R3：与旁白上传同一口径——节点专用线程池 + 剩余时限，保证预算
            # 耗尽/取消时能及时退出。
            # 迟到副作用受控依据：object_key 已先行持久化（R2），record_upload
            # 受 token 门禁，超时后 job 留非 uploaded 态由维护对账收敛。
            await self._upload_audio_bytes(ctx, refs, concat.audio, object_key)
            self._jobs.record_upload(
                job.job_id, token, duration_ms=concat.duration_ms
            )
        except Exception as exc:
            # 下载/转码/上传失败：费用已按请求秒数结算（任务已成功），资产
            # 缺失按无配乐降级；恢复重提会被 _bgm_slot_retryable 的已结算
            # 判定拦下，绝不重复付费。
            logging.warning(
                "MemoirAgent 配乐交付失败降级 run_id=%s code=MEMOIR_AUDIO_BGM_DELIVERY_FAILED",
                refs.run_id,
            )
            # 节点内 wait_for 超时同样落入本 except，但不得 mark_failed（R3
            # 墙钟 + fencing：迟到 record_upload 仍被旧 token 拒绝）。超时
            # 与进程中断由 reaper 兜底。其余交付失败立即转 failed，释放槽位。
            if not isinstance(exc, TimeoutError):
                try:
                    # 复活安全性（冻结文档 §2.4）：BGM 持键 ⇒ settle_music_usage
                    # 已在本函数 try 外完成，mark_failed / 复活不重复收费。
                    # 复活保留同一 object_key，重试上传同键；对象被删则重建。
                    # 迟到 token 被 rotate 后 fencing 拒绝是正常竞态。
                    # 24h 窗 ≫ Run 生命周期，active 不再二次删。
                    self._jobs.mark_failed(
                        job.job_id, token, error_code="AUDIO_BGM_DELIVERY_FAILED"
                    )
                    self._commit_ledger(ctx)
                except Exception:
                    logging.warning(
                        "MemoirAgent 配乐失败入账被吸收 run_id=%s "
                        "code=AUDIO_BGM_DELIVERY_FAILED",
                        refs.run_id,
                    )
            return None
        self._commit_ledger(ctx)
        self._heartbeat_run_lease(ctx, refs, lease_context)
        return {
            "media_id": self._media_id("audio-bgm", refs, WORK_SCENE_ID, bgm_hmac),
            "object_key": object_key,
            "mime": AUDIO_MIME,
            "duration_ms": concat.duration_ms,
        }

    def _bgm_entry(
        self, refs: _AudioRefs, bgm_hmac: str, job: MemoirAudioJob
    ) -> dict[str, object]:
        """由已上传账本行构造配乐发布条目。"""
        return {
            "media_id": self._media_id("audio-bgm", refs, WORK_SCENE_ID, bgm_hmac),
            "object_key": job.object_key,
            "mime": job.mime or AUDIO_MIME,
            "duration_ms": job.duration_ms or 0,
        }

    # ------------------------------------------------------------------
    # 共享工具
    # ------------------------------------------------------------------

    def _voice_params(self) -> dict[str, object]:
        """声音参数快照：纳入 input_hmac，任何参数变更都会生成新资产。"""
        return {
            "speaker": getattr(self._tts, "_speaker", ""),
            "speech_rate": getattr(self._tts, "_speech_rate", 0),
            "format": "mp3",
            "sample_rate": 24000,
            "bit_rate": 64000,
            "segment_rule": SEGMENT_RULE_VERSION,
        }

    async def _upload_audio_bytes(
        self, ctx: _RunCtx, refs: _AudioRefs, data: bytes, object_key: str
    ) -> None:
        """R3：同步 OSS 上传统一走节点专用线程池并受剩余时限约束。

        为什么不能用 asyncio.to_thread（默认执行器）：asyncio.run 退出时会
        shutdown 默认执行器并等待在途线程自然结束——慢上传会把节点返回
        时间拖到上传完成之后，时间上限名存实亡（复现：预算 10ms 实际
        ~254ms 后才返回且上传已完成）。专用执行器 + generate() finally 中
        的 shutdown(wait=False, cancel_futures=True) 让节点返回与上传线程
        耗时彻底解耦：wait_for 超时/取消即返回，线程在后台跑完后自行退出。

        迟到副作用受控（无需新增锁）：
        1. object_key 已由 R2 在上传前独立 commit 持久化，账本可对账；
        2. 超时路径本执行不再调用 record_upload——job 停留非 uploaded 态，
           由维护对账收敛；若其他执行者（lease 已旋转 / 状态已迁移）迟到
           补写 record_upload，会被 R4 的 token+来源态 CAS 原子拒绝。
        timeout 取 ctx.remaining()：发布预留已在预算计算时扣除，上传超时
        不会侵占冻结的发布窗口（发布时限语义不变）。
        """
        if ctx.executor is None:
            # 防御：generate() 必定注入专用执行器；缺失说明内部约定被破坏。
            raise MemoirAudioProviderError(
                "AUDIO_UPLOAD_EXECUTOR_MISSING", "音频上传执行器未初始化"
            )
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(
                ctx.executor,
                self._uploader.upload_private_bytes,
                data, object_key, AUDIO_MIME,
            ),
            timeout=ctx.remaining(),
        )
        logging.info(
            "MemoirAgent 音频私有上传完成 run_id=%s code=MEMOIR_AUDIO_UPLOADED",
            refs.run_id,
        )

    async def _call_with_slot(self, ctx: _RunCtx, coro: Any, *, timeout: float) -> Any:
        """包住每次真实供应商调用：进程级信号量 + 剩余预算 deadline。"""
        if timeout <= 0:
            # 调用方已在实参处构造协程对象：预算耗尽早退时必须显式关闭，
            # 否则协程体从未执行却留下"never awaited"告警（无外发副作用）。
            coro.close()
            raise MemoirAudioProviderError("TTS_TIMEOUT", "音频预算已耗尽")
        if not self._worker_slots.acquire(timeout=1.0):
            logging.warning(
                "MemoirAgent 音频进程信号量耗尽 code=MEMOIR_AUDIO_WORKER_BUSY"
            )
            raise MemoirAudioProviderError("AUDIO_WORKER_BUSY", "音频并发已满")
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        finally:
            self._worker_slots.release()

    def _precheck(self, ctx: _RunCtx, text: str) -> bool:
        """单段提交前本地门禁：预算时钟 + 输入非空。

        Run 取消/隐私/包状态等权威门禁由 reserve_job 内的账本断言执行
        （MEMOIR_AUDIO_RUN_*），无需在此重复触库。
        """
        if ctx.should_stop() is not None:
            return False
        return bool(text and text.strip())

    def _segment_slot_retryable(self, existing: MemoirAudioJob) -> bool:
        """分段槽是否允许（重新）占位：仅终态失败且未结算且未超重试上限。"""
        if existing.state == STATE_FAILED:
            return (
                existing.settled_cost is None
                and existing.attempt < MAX_SUBMIT_ATTEMPTS
            )
        return False

    def _bgm_mutex_degraded(
        self, refs: _AudioRefs, bgm_hmac: str, *, mode: str
    ) -> bool:
        """作品级 BGM 互斥判定（freeze §11.1 D2：两模式共用防线）。

        为什么必须在同一数据库事务边界内、且紧邻占槽/提交之前调用：
        唯一约束 uq_memoir_audio_job_slot 含 input_hmac，挡不住"换输入建
        第二条 BGM 行"——只有本查询（hmac-less，§4.3）能看到同作品全部
        BGM 行。查到与自身 input_hmac 不同的行（不论对方是火山在途/已结算
        行还是不同指纹默认行）→ 保守降级为无 BGM：不建第二槽、不提交付费
        任务、不清零未知火山费用。

        S2（2026-09-14 §11.1）：本判定必须保持无锁三参查询。它在
        reserve_job 的 AgentRun 行锁之前调用，若此处 FOR UPDATE 会与
        Run→BGM 锁序构成 AB-BA。权威互斥由 reserve_job 锁内对 BGM
        的 FOR UPDATE 当前读强制；MySQL RR 下不能假设"锁了 Run 之后
        普通 SELECT 必然看见已提交行"。调用方仍应保证判定 → 预留
        之间无 commit，缩小（而非消除）竞窗。SQLite 忽略 FOR UPDATE，
        只验证判定逻辑放置；真库当前读走 MySQL RR opt-in 验收。
        """
        siblings = self._jobs.list_work_background_music_jobs(
            refs.run_id, refs.generation_epoch, refs.package_version
        )
        if any(row.input_hmac != bgm_hmac for row in siblings):
            logging.warning(
                "MemoirAgent 配乐作品级互斥降级 run_id=%s mode=%s "
                "code=MEMOIR_AUDIO_BGM_MUTEX_DEGRADED",
                refs.run_id, mode,
            )
            return True
        return False

    def _bgm_slot_retryable(self, existing: MemoirAudioJob) -> bool:
        """配乐槽是否允许重新提交：已结算/在途/未知一律禁止（防双付费）。

        例外（freeze 2026-09-11 §6）：零费默认槽（reserved_cost==0 且无
        provider_task_id）的已结算态（settled_cost=0）不得阻止恢复重试——
        默认路径零费结算先于 record_upload，崩溃窗口内 settled=0 但槽位
        仍应可复活重试；付费分支语义一字不动。
        """
        if existing.settled_cost is None:
            return self._segment_slot_retryable(existing)
        zero_fee_default = (
            (existing.reserved_cost or Decimal("0")) == 0
            and existing.provider_task_id is None
        )
        if not zero_fee_default:
            return False
        # 零费默认槽：已结算 0 不拦截，仅按终态失败 + 重试上限判定。
        return existing.state == STATE_FAILED and existing.attempt < MAX_SUBMIT_ATTEMPTS

    def _mark_provider_failure(
        self, ctx: _RunCtx, job_id: str, token: int, code: str
    ) -> None:
        """提交后供应商失败入账：限流→failed（可一次重试），其余未知终态。"""
        target = STATE_FAILED if code in _RATE_LIMIT_CODES else STATE_SUBMISSION_UNKNOWN
        try:
            if target == STATE_FAILED:
                self._jobs.mark_failed(job_id, token, error_code=code)
            else:
                self._jobs.mark_submission_unknown(job_id, token, error_code=code)
        except MemoirAudioJobsError as exc:
            self._absorb_jobs_error(ctx, exc.code, "provider_failure")
            return
        self._commit_ledger(ctx)
        logging.warning(
            "MemoirAgent 音频供应商失败入账 state=%s code=%s", target, code
        )

    def _absorb_jobs_error(self, ctx: _RunCtx, code: str, stage: str) -> None:
        """账本错误分流：门禁/预算→全局停止；其余仅记录按单元降级。

        重试决策只由各调用点的限流（429）分支控制；本函数不产生重试。
        """
        if code in _RUN_GATE_CODES:
            ctx.stop_reason = f"gate:{code}"
        elif code in _BUDGET_CODES:
            ctx.stop_reason = f"budget:{code}"
        elif code == "MEMOIR_AUDIO_FENCING_REJECTED":
            # 本执行者已过时（lease 被接管）：立即停止全部写入。
            ctx.stop_reason = "fencing"
        logging.warning(
            "MemoirAgent 音频账本拒绝 stage=%s code=%s stop=%s",
            stage, code, ctx.stop_reason is not None,
        )

    def _commit_ledger(self, ctx: _RunCtx) -> bool:
        """账本落库：预留/终态/结算必须先于下一次供应商调用持久化，
        否则崩溃恢复看不到已花费的提交，会重复付费。

        返回 commit 是否成功：无 Session（内存态）视为成功放行；抛错或
        结果未知返回 False 并置全局停止原因，调用方必须中止后续外发
        （Provider 调用 / OSS 上传），不允许在未确认持久化的账本上产生
        任何新的付费或对象副作用。
        """
        if self._session is None:
            return True
        try:
            self._session.commit()
            return True
        except Exception:
            ctx.stop_reason = "ledger_commit_failed"
            logging.warning(
                "MemoirAgent 音频账本提交失败停止 code=MEMOIR_AUDIO_LEDGER_COMMIT_FAILED"
            )
            try:
                self._session.rollback()
            except Exception:
                pass
            return False

    def _heartbeat_run_lease(
        self, ctx: _RunCtx, refs: _AudioRefs, lease_context: Any
    ) -> None:
        """音频节点内续约 Run 租约（长节点防 reaper 接管）；失败即停。"""
        if lease_context is None or self._session is None:
            return
        try:
            from app.services.lease_service import LeaseService

            if not LeaseService(self._session).heartbeat(refs.run_id, lease_context):
                ctx.stop_reason = "run_lease_lost"
        except Exception:
            ctx.stop_reason = "run_lease_lost"

    def _require_policy(self) -> MemoirAudioCostPolicy:
        """计费策略必须存在（维护态无策略时禁止任何付费提交）。"""
        policy = getattr(self._jobs, "_policy", None)
        if policy is None:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_POLICY_INVALID", "未配置音频计费策略"
            )
        return policy

    def _job_id(
        self,
        refs: _AudioRefs,
        role: str,
        scene_id: str,
        input_hmac: str,
        segment_index: int = NO_SEGMENT_INDEX,
    ) -> str:
        """账本作业短 ID：由槽位确定性派生（同槽重试复用同一行）。"""
        digest = hashlib.sha256(
            f"{refs.run_id}:{refs.generation_epoch}:{refs.package_version}:"
            f"{role}:{scene_id}:{segment_index}:{input_hmac}".encode()
        ).hexdigest()[:32]
        return f"audio-{digest}"

    def _media_id(
        self, prefix: str, refs: _AudioRefs, scene_id: str, input_hmac: str
    ) -> str:
        """发布 media_id：确定性派生（恢复复用与新生成同 ID、同文档 digest）。"""
        digest = hashlib.sha256(
            f"{refs.run_id}:{refs.generation_epoch}:{refs.package_version}:"
            f"{scene_id}:{input_hmac}".encode()
        ).hexdigest()[:24]
        return f"{prefix}-{digest}"


def build_memoir_audio_service(
    runtime_settings: Any, session: Any
) -> MemoirAudioService | None:
    """按部署配置装配完整音频栈；任何缺项都按能力关闭返回 None。

    由 Worker 装配入口调用（与 configured_media_service 同一模式）：
    先跑成组校验（开启时全部必填字段与取值合法性），再构造 TTS/音乐
    client、私有 OSS 上传器、转码器、安全下载器与计费策略。装配失败只
    记异常类名，绝不把凭据或配置值写进日志；返回 None 等价于音频能力
    关闭（1.0.8 图的音频节点按 CAPABILITY_DISABLED 跳过，行为零变化）。
    """
    from app.core.config import validate_memoir_audio_settings

    try:
        validate_memoir_audio_settings(runtime_settings)
        # 模式分叉（freeze 2026-09-11 §6）：默认模式不校验/装配音乐生成
        # 依赖（火山音乐客户端、音乐单价必填、下载白名单）；音乐单价占位
        # "0" 仅作结构完整，默认路径永不调用 estimate_music_cost。
        generation_enabled = bool(
            getattr(runtime_settings, "MEMOIR_MUSIC_GENERATION_ENABLED", False)
        )
        music_price_raw = (
            str(getattr(runtime_settings, "MEMOIR_MUSIC_PRICE_PER_SECOND", ""))
            if generation_enabled
            else "0"
        )
        policy = MemoirAudioCostPolicy(
            currency=str(getattr(runtime_settings, "MEMOIR_AUDIO_COST_CURRENCY", "")),
            tts_price_per_1000_text_words=Decimal(
                str(getattr(runtime_settings, "MEMOIR_TTS_PRICE_PER_1000_TEXT_WORDS", ""))
            ),
            music_price_per_second=Decimal(music_price_raw),
            max_cost_per_run=Decimal(
                str(getattr(runtime_settings, "MEMOIR_AUDIO_MAX_COST_PER_RUN", ""))
            ),
        )
        tts_client = VolcanoTTSClient(
            api_key=str(getattr(runtime_settings, "MEMOIR_TTS_API_KEY", "")),
            resource_id=str(
                getattr(runtime_settings, "MEMOIR_TTS_RESOURCE_ID", "seed-tts-2.0")
            ),
            speaker=str(
                getattr(
                    runtime_settings, "MEMOIR_TTS_SPEAKER",
                    "zh_female_wenroushunv_uranus_bigtts",
                )
            ),
            speech_rate=int(getattr(runtime_settings, "MEMOIR_TTS_SPEECH_RATE", -10)),
            request_timeout_seconds=float(
                getattr(runtime_settings, "MEMOIR_TTS_REQUEST_TIMEOUT_SECONDS", 45.0)
            ),
        )
        if generation_enabled:
            music_client = VolcanoMusicClient(
                access_key=str(getattr(runtime_settings, "VOLCANO_CV_ACCESS_KEY", "")),
                secret_key=str(getattr(runtime_settings, "VOLCANO_CV_SECRET_KEY", "")),
                action=str(getattr(runtime_settings, "MEMOIR_MUSIC_ACTION", "")),
                duration_seconds=int(
                    getattr(runtime_settings, "MEMOIR_MUSIC_DURATION_SECONDS", 60)
                ),
                request_timeout_seconds=float(
                    getattr(runtime_settings, "MEMOIR_MUSIC_REQUEST_TIMEOUT_SECONDS", 15.0)
                ),
            )
            downloader = SecureAudioDownloader(
                allowed_hosts=frozenset(
                    json.loads(
                        str(
                            getattr(
                                runtime_settings,
                                "MEMOIR_MUSIC_DOWNLOAD_ALLOWED_HOSTS_JSON",
                                "[]",
                            )
                        )
                    )
                ),
                max_bytes=int(
                    getattr(runtime_settings, "MEMOIR_AUDIO_MAX_FILE_BYTES", 20971520)
                ),
            )
        else:
            # 默认配乐模式：音乐生成客户端与官方 URL 下载器永不触达，
            # 置 None 由模式分叉保证零调用。
            music_client = None
            downloader = None
        uploader = AliyunAudioOSSUploader(
            access_key_id=str(
                getattr(runtime_settings, "MEMORY_AUDIO_OSS_ACCESS_KEY_ID", "")
            ),
            access_key_secret=str(
                getattr(runtime_settings, "MEMORY_AUDIO_OSS_ACCESS_KEY_SECRET", "")
            ),
            bucket=str(getattr(runtime_settings, "MEMORY_AUDIO_OSS_BUCKET", "")),
            endpoint=str(getattr(runtime_settings, "MEMORY_AUDIO_OSS_ENDPOINT", "")),
        )
        transcoder = AudioTranscoder(
            ffmpeg_path=str(
                getattr(runtime_settings, "MEMOIR_AUDIO_FFMPEG_PATH", "/usr/bin/ffmpeg")
            ),
            ffprobe_path=str(
                getattr(runtime_settings, "MEMOIR_AUDIO_FFPROBE_PATH", "/usr/bin/ffprobe")
            ),
            subprocess_timeout_seconds=float(
                getattr(runtime_settings, "MEMOIR_AUDIO_SUBPROCESS_TIMEOUT_SECONDS", 60.0)
            ),
            max_input_bytes=int(
                getattr(runtime_settings, "MEMOIR_AUDIO_MAX_FILE_BYTES", 20971520)
            ),
        )
        return MemoirAudioService(
            tts_client=tts_client,
            music_client=music_client,
            uploader=uploader,
            transcoder=transcoder,
            downloader=downloader,
            jobs_service=MemoirAudioJobsService(session, policy),
            config=MemoirAudioConfig.from_settings(runtime_settings),
            session=session,
        )
    except Exception as exc:
        # 只记异常类名：消息可能携带配置值（单价/前缀），不能进日志。
        logging.warning(
            "MemoirAgent 音频服务装配失败按能力关闭 code=%s", type(exc).__name__
        )
        return None
