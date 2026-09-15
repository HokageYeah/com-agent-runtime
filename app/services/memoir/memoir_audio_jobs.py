"""M8 R7 Memoir 音频作业服务：唯一槽、原子预算、lease/fencing 与恢复对账。

职责边界（对齐设计说明 §5）：
1. `reserve_job`：提交前原子占槽 + 费用预留。预算条件 UPDATE（封顶判定）
   与作业唯一键插入在同一个 savepoint 内，失败方回滚即预算回滚，
   禁止多场景各自读取旧余额后并发透支。
2. 状态机写入：所有写入走 lease fencing（token 匹配 + 未过期）与
   Run 门禁（未取消、privacy_state=active），旧 worker 不能写新 owner 结果。
3. 结算：TTS 按实际 text_words、音乐按请求秒数；未知结果
   （submission_unknown）不释放预留、不自动重提、不可结算。
4. 维护对账：list_orphan_candidates + mark_cleaned 供维护 CLI 使用；
   发布状态由维护脚本经 Business get_publish_result 探测（R5），
   本模块不查业务发布投影表。

隐私铁律：日志与异常只携带安全枚举、短 ID、计数与金额；正文、prompt、
私有 URL、音频字节绝不入表、入日志或入错误文本。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AgentRun
from app.models.memoir_audio_job import (
    ALL_ROLES,
    NO_SEGMENT_INDEX,
    ROLE_BACKGROUND_MUSIC,
    ROLE_NARRATION,  # noqa: F401 转出口：调用方约定从服务模块统一取枚举
    ROLE_SEGMENT,
    STATE_CANCELLED,
    STATE_CLEANED,
    STATE_FAILED,
    STATE_PROCESSING,
    STATE_PUBLISHED,
    STATE_RESERVED,
    STATE_SUBMISSION_UNKNOWN,
    STATE_SUBMITTED,
    STATE_SUBMITTING,
    STATE_UPLOADED,
    WORK_SCENE_ID,
    MemoirAudioJob,
    MemoirAudioRunBudget,
)

logger = logging.getLogger(__name__)

# 金额统一 6 位小数：预留向上取整（保守上界），结算四舍五入。
_COST_EXPONENT = Decimal("0.000001")
# 默认 lease 时长（秒）：与音频节点租约配套，调用方可覆盖。
DEFAULT_LEASE_TTL_SECONDS = 60.0
# 可写状态（在途）：lease/fencing 保护下的合法写入起点。
_ACTIVE_STATES = (STATE_RESERVED, STATE_SUBMITTING, STATE_SUBMITTED, STATE_PROCESSING)
# 成功终态：同 Run 同输入复用，不再扣费。
_SUCCESS_STATES = (STATE_UPLOADED, STATE_PUBLISHED)
# 孤儿候选状态：维护命令的扫描范围（对象键非空才有清理意义）。
_ORPHAN_STATES = (STATE_UPLOADED, STATE_FAILED, STATE_CANCELLED, STATE_SUBMISSION_UNKNOWN)


class MemoirAudioJobsError(ValueError):
    """受控错误：.code 为安全枚举，message 不含正文/URL/凭据/音频字节。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MemoirAudioCostPolicy:
    """音频计费策略：币种、单价与单 Run 预算上限（全部 Decimal）。

    由部署配置（MEMOIR_*_PRICE_* / MEMOIR_AUDIO_MAX_COST_PER_RUN）解析而来，
    业务请求不得自带价格或上限。
    """

    currency: str
    tts_price_per_1000_text_words: Decimal
    music_price_per_second: Decimal
    max_cost_per_run: Decimal

    def __post_init__(self) -> None:
        if not self.currency:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_POLICY_INVALID", "币种为空")
        for name in (
            "tts_price_per_1000_text_words",
            "music_price_per_second",
            "max_cost_per_run",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or value.is_nan() or value.is_infinite():
                raise MemoirAudioJobsError("MEMOIR_AUDIO_POLICY_INVALID", f"{name} 非法")
        if self.tts_price_per_1000_text_words < 0 or self.music_price_per_second < 0:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_POLICY_INVALID", "单价不得为负")
        if self.max_cost_per_run <= 0:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_POLICY_INVALID", "预算上限必须为正")


def estimate_tts_cost(text: str, policy: MemoirAudioCostPolicy) -> Decimal:
    """TTS 保守预留上界：按完整输入字符数 × 千字单价，向上取整 6 位。

    预留口径必须覆盖供应商最终 text_words 计费（缺证据时不提交），
    字符数是当前已确认口径下的保守上界；空文本直接拒绝。
    """
    if not isinstance(text, str) or not text:
        raise MemoirAudioJobsError("MEMOIR_AUDIO_INPUT_INVALID", "TTS 输入为空")
    cost = Decimal(len(text)) * policy.tts_price_per_1000_text_words / Decimal(1000)
    return cost.quantize(_COST_EXPONENT, rounding=ROUND_CEILING)


def estimate_music_cost(seconds: int, policy: MemoirAudioCostPolicy) -> Decimal:
    """音乐保守预留上界：请求秒数 × 每秒单价，向上取整 6 位。"""
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise MemoirAudioJobsError("MEMOIR_AUDIO_INPUT_INVALID", "音乐秒数非法")
    cost = Decimal(seconds) * policy.music_price_per_second
    return cost.quantize(_COST_EXPONENT, rounding=ROUND_CEILING)


@dataclass(frozen=True)
class MemoirAudioJobReservation:
    """一次作业预留请求：唯一槽定位 + 保守费用上界 + lease 参数。

    estimated_cost 由 estimate_* 计算；usage/seconds 估算值仅用于调用方
    对账，不落账本（账本只记实际用量与金额）。
    """

    job_id: str
    business_id: str
    run_id: str
    generation_epoch: int
    package_version: str
    role: str
    scene_id: str
    input_hmac: str
    estimated_cost: Decimal
    lease_owner: str
    lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS
    segment_index: int = NO_SEGMENT_INDEX
    usage_text_words_estimate: int | None = None
    requested_music_seconds: int | None = None


@dataclass(frozen=True)
class ReservationOutcome:
    """预留结果：outcome ∈ {created, retried, reused}。

    reused 表示同 Run 同输入已有成功资产，直接复用且不重复扣费。
    """

    job: MemoirAudioJob
    outcome: str


def _require_positive_int(value: Any, code: str, message: str) -> int:
    """校验正整数（拒绝 bool/浮点/非正数），失败抛固定枚举。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MemoirAudioJobsError(code, message)
    return value


def _as_aware(value: datetime) -> datetime:
    """SQLite 返回 naive 时间戳时按 UTC 补时区，跨方言统一比较口径。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _parse_lease_owner_attempt(run_id: str, lease_owner: str) -> int | None:
    """R4 Run 执行尝试 fence：解析 lease_owner 中的尝试号。

    仅识别服务层构造的 ``audio:{run_id}:attempt-{N}`` 格式；其他格式
    （历史/测试数据如 "worker-a"、"audio:legacy"）返回 None——非该格式
    不参与执行尝试 fence，避免破坏既有数据兼容性。
    """
    prefix = f"audio:{run_id}:attempt-"
    if not lease_owner.startswith(prefix):
        return None
    suffix = lease_owner[len(prefix) :]
    if not suffix.isdigit():
        return None
    return int(suffix)


class MemoirAudioJobsService:
    """音频作业账本服务：占槽、预算、状态机、结算与维护查询。

    维护路径（孤儿扫描/发布对账/标记清理）无需计费策略，policy 可为 None；
    一旦调用预留或结算则要求策略存在（POLICY_INVALID fail-fast）。
    """

    def __init__(
        self, session: Session, policy: MemoirAudioCostPolicy | None = None
    ) -> None:
        self._session = session
        self._policy = policy

    # ------------------------------------------------------------------
    # 预留：唯一槽 + 原子预算
    # ------------------------------------------------------------------

    def reserve_job(
        self, reservation: MemoirAudioJobReservation, *, now: datetime | None = None
    ) -> ReservationOutcome:
        """原子占槽并预留费用。

        结果语义：
        - created：新槽建立，费用已预留；
        - retried：终态失败槽复活（attempt/lease_token 递增、重新预留，
          object_key 保留以便对账同一对象）；
        - reused：已有 uploaded/published 成功资产，复用不扣费；
        - 槽在途 → SLOT_ACTIVE；submission_unknown → 禁止自动重提；
        - 预算不足 → BUDGET_EXCEEDED（槽与预算一并回滚，不留半开的账）。
        """
        moment = now or datetime.now(UTC)
        self._validate_reservation(reservation)
        # S2（2026-09-15 §11.1）：持行锁写入统一 Run → Job。互斥当前读
        # 与占槽插入留在同一外层事务直到调用方 _commit_ledger。
        # begin_nested 只作该段写回滚（守卫抛错 / IntegrityError），
        # 不是放锁——InnoDB 行锁随外层 COMMIT 释放。禁止
        # session.rollback() 放锁（会丢旁白未提交账本）。MySQL RR 下
        # 锁 Run 不刷新快照，BGM 必须在 Run 锁之后 FOR UPDATE 当前读。
        with self._session.begin_nested():
            # R4：预留也走执行尝试 fence——按"本次请求的 owner"校验。
            self._assert_run_active(
                reservation.run_id, lease_owner=reservation.lease_owner
            )
            if (
                reservation.role == ROLE_BACKGROUND_MUSIC
                and reservation.scene_id == WORK_SCENE_ID
            ):
                siblings = self.list_work_background_music_jobs(
                    reservation.run_id,
                    reservation.generation_epoch,
                    reservation.package_version,
                    for_update=True,
                )
                if any(row.input_hmac != reservation.input_hmac for row in siblings):
                    raise MemoirAudioJobsError(
                        "MEMOIR_AUDIO_JOB_SLOT_ACTIVE",
                        "作品级配乐槽已被不同输入占用",
                    )

        existing = self._find_by_slot(reservation)
        if existing is not None:
            return self._reserve_existing(existing, reservation, moment)

        # 新建：预算条件更新与唯一键插入同 savepoint；唯一键冲突时
        # savepoint 回滚同时回收预算，杜绝"占槽失败却扣了钱"。
        for round_index in range(2):
            try:
                with self._session.begin_nested():
                    self._reserve_budget(reservation)
                    job = MemoirAudioJob(
                        job_id=reservation.job_id,
                        business_id=reservation.business_id,
                        run_id=reservation.run_id,
                        generation_epoch=reservation.generation_epoch,
                        package_version=reservation.package_version,
                        role=reservation.role,
                        scene_id=reservation.scene_id,
                        segment_index=reservation.segment_index,
                        input_hmac=reservation.input_hmac,
                        attempt=1,
                        state=STATE_RESERVED,
                        reserved_cost=reservation.estimated_cost,
                        currency=self._require_policy().currency,
                        requested_music_seconds=reservation.requested_music_seconds,
                        lease_owner=reservation.lease_owner,
                        lease_token=1,
                        expires_at=moment + timedelta(seconds=reservation.lease_ttl_seconds),
                    )
                    self._session.add(job)
                    self._session.flush()
                logger.info(
                    "Memoir 音频作业已预留，code=MEMOIR_AUDIO_JOB_RESERVED，"
                    "role=%s，outcome=created",
                    reservation.role,
                )
                return ReservationOutcome(job=job, outcome="created")
            except IntegrityError:
                # 并发胜者已占槽（或预算行并发首建）：savepoint 已回滚。
                winner = self._find_by_slot(reservation)
                if winner is not None:
                    return self._reserve_existing(winner, reservation, moment)
                if round_index == 0:
                    continue  # 预算行首建竞争，整体重试一次
                raise MemoirAudioJobsError(
                    "MEMOIR_AUDIO_RESERVE_CONFLICT", "预留并发冲突，请重试"
                ) from None
        raise MemoirAudioJobsError("MEMOIR_AUDIO_RESERVE_CONFLICT", "预留冲突重试耗尽")

    def _reserve_existing(
        self,
        existing: MemoirAudioJob,
        reservation: MemoirAudioJobReservation,
        moment: datetime,
    ) -> ReservationOutcome:
        """槽已存在时的分流：在途拒绝 / 成功复用 / 未知禁重提 / 终态重试。"""
        if existing.state == STATE_SUBMISSION_UNKNOWN:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_SUBMISSION_UNKNOWN", "提交结果未知，禁止自动重提收费"
            )
        if existing.state in _SUCCESS_STATES:
            logger.info(
                "Memoir 音频作业复用成功资产，code=MEMOIR_AUDIO_JOB_REUSED，role=%s",
                existing.role,
            )
            return ReservationOutcome(job=existing, outcome="reused")
        if existing.state in _ACTIVE_STATES:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_JOB_SLOT_ACTIVE", "同槽作业仍在途"
            )
        # failed / cancelled / cleaned：重试复活。预算条件更新与状态复活
        # 的条件 UPDATE 同 savepoint，并发重试只有一方成功。
        with self._session.begin_nested():
            self._reserve_budget(reservation)
            result = self._session.execute(
                sa.update(MemoirAudioJob)
                .where(
                    MemoirAudioJob.id == existing.id,
                    MemoirAudioJob.state == existing.state,
                )
                .values(
                    attempt=existing.attempt + 1,
                    lease_token=existing.lease_token + 1,
                    lease_owner=reservation.lease_owner,
                    expires_at=moment + timedelta(seconds=reservation.lease_ttl_seconds),
                    state=STATE_RESERVED,
                    error_code=None,
                    provider_task_id=None,
                    duration_ms=None,
                    reserved_cost=(existing.reserved_cost or Decimal("0"))
                    + reservation.estimated_cost,
                    usage_text_words=None,
                    requested_music_seconds=reservation.requested_music_seconds,
                )
            )
            if result.rowcount != 1:
                raise MemoirAudioJobsError(
                    "MEMOIR_AUDIO_JOB_SLOT_ACTIVE", "槽位已被其他重试占用"
                )
        self._session.refresh(existing)
        logger.info(
            "Memoir 音频作业失败重试，code=MEMOIR_AUDIO_JOB_RESERVED，"
            "role=%s，outcome=retried，attempt=%d",
            existing.role,
            existing.attempt,
        )
        return ReservationOutcome(job=existing, outcome="retried")

    def _reserve_budget(self, reservation: MemoirAudioJobReservation) -> None:
        """Run 级预算原子预留：条件 UPDATE 封顶，不足即拒绝。

        已存在行：`reserved_total + cost <= cap` 才更新（rowcount 判定）；
        不存在行：单笔未超上限即可首建（首建竞争由外层 IntegrityError 兜底）。
        cost <= 0（本地拼接零成本路径）不占预算。
        """
        cost = reservation.estimated_cost
        if cost <= 0:
            return
        policy = self._require_policy()
        if cost > policy.max_cost_per_run:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_BUDGET_EXCEEDED", "单笔预留即超 Run 预算上限"
            )
        result = self._session.execute(
            sa.update(MemoirAudioRunBudget)
            .where(
                MemoirAudioRunBudget.run_id == reservation.run_id,
                MemoirAudioRunBudget.currency == policy.currency,
                MemoirAudioRunBudget.reserved_total_cost + cost
                <= policy.max_cost_per_run,
            )
            .values(
                reserved_total_cost=MemoirAudioRunBudget.reserved_total_cost + cost
            )
        )
        if result.rowcount == 1:
            return
        budget = self._session.scalar(
            sa.select(MemoirAudioRunBudget).where(
                MemoirAudioRunBudget.run_id == reservation.run_id
            )
        )
        if budget is None:
            # 首行：插入即预留（插入竞争由 reserve_job 的 savepoint 捕获重试）。
            self._session.add(
                MemoirAudioRunBudget(
                    run_id=reservation.run_id,
                    currency=policy.currency,
                    reserved_total_cost=cost,
                )
            )
            self._session.flush()
            return
        if budget.currency != policy.currency:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_CURRENCY_MISMATCH", "预算行币种与策略不一致"
            )
        raise MemoirAudioJobsError(
            "MEMOIR_AUDIO_BUDGET_EXCEEDED", "Run 剩余音频预算不足"
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def find_job(
        self,
        *,
        run_id: str,
        generation_epoch: int,
        package_version: str,
        role: str,
        scene_id: str,
        input_hmac: str,
        segment_index: int = NO_SEGMENT_INDEX,
    ) -> MemoirAudioJob | None:
        """按唯一槽定位作业行；不存在返回 None。"""
        return self._session.scalar(
            sa.select(MemoirAudioJob).where(
                MemoirAudioJob.run_id == run_id,
                MemoirAudioJob.generation_epoch == generation_epoch,
                MemoirAudioJob.package_version == package_version,
                MemoirAudioJob.role == role,
                MemoirAudioJob.scene_id == scene_id,
                MemoirAudioJob.segment_index == segment_index,
                MemoirAudioJob.input_hmac == input_hmac,
            )
        )

    def find_reusable_uploaded_asset(
        self,
        *,
        run_id: str,
        generation_epoch: int,
        package_version: str,
        role: str,
        scene_id: str,
        input_hmac: str,
    ) -> MemoirAudioJob | None:
        """查找同 Run 同输入的可复用成功资产（uploaded/published）。"""
        return self._session.scalar(
            sa.select(MemoirAudioJob)
            .where(
                MemoirAudioJob.run_id == run_id,
                MemoirAudioJob.generation_epoch == generation_epoch,
                MemoirAudioJob.package_version == package_version,
                MemoirAudioJob.role == role,
                MemoirAudioJob.scene_id == scene_id,
                MemoirAudioJob.input_hmac == input_hmac,
                MemoirAudioJob.state.in_(_SUCCESS_STATES),
            )
            .order_by(MemoirAudioJob.id.desc())
        )

    def find_pending_music_task(
        self,
        *,
        run_id: str,
        generation_epoch: int,
        package_version: str,
        input_hmac: str,
    ) -> MemoirAudioJob | None:
        """查找在途音乐任务（submitted/processing 且已知 TaskID）。

        已知 TaskID 只查不重建：恢复路径据此继续轮询供应商，而非重新提交
        产生第二笔费用。
        """
        return self._session.scalar(
            sa.select(MemoirAudioJob)
            .where(
                MemoirAudioJob.run_id == run_id,
                MemoirAudioJob.generation_epoch == generation_epoch,
                MemoirAudioJob.package_version == package_version,
                MemoirAudioJob.role == ROLE_BACKGROUND_MUSIC,
                MemoirAudioJob.scene_id == WORK_SCENE_ID,
                MemoirAudioJob.input_hmac == input_hmac,
                MemoirAudioJob.state.in_((STATE_SUBMITTED, STATE_PROCESSING)),
                MemoirAudioJob.provider_task_id.is_not(None),
            )
            .order_by(MemoirAudioJob.id)
        )

    def get_run_budget(self, run_id: str) -> MemoirAudioRunBudget | None:
        """返回 Run 预算行（无则 None），供观测与维护对账。"""
        return self._session.scalar(
            sa.select(MemoirAudioRunBudget).where(MemoirAudioRunBudget.run_id == run_id)
        )

    def list_work_background_music_jobs(
        self,
        run_id: str,
        generation_epoch: int,
        package_version: str,
        *,
        for_update: bool = False,
    ) -> list[MemoirAudioJob]:
        """同 Run 作品级 BGM hmac-less 查询（freeze 2026-09-11 §4.3）。

        唯一约束 uq_memoir_audio_job_slot 含 input_hmac，挡不住"同 Run 换
        输入建第二条 BGM 行"；本查询刻意不带 input_hmac，把该 Run 全部
        作品级 BGM 行交给调用方做保守判断。

        S2（2026-09-15 §11.1）：默认三参路径保持无锁，供咨询性预检
        （_bgm_mutex_degraded 在 Run 锁之前调用，加锁会构成 AB-BA）。
        reserve_job 在 AgentRun FOR UPDATE 之后传 for_update=True。
        拒绝路径由 service 吸收后 _commit_ledger 结束外层事务放锁；
        savepoint 回滚不是即时放锁。MySQL RR 下只锁 Run 不会刷新
        一致快照，普通 SELECT 不能当权威互斥。
        """
        query = (
            sa.select(MemoirAudioJob)
            .where(
                MemoirAudioJob.run_id == run_id,
                MemoirAudioJob.generation_epoch == generation_epoch,
                MemoirAudioJob.package_version == package_version,
                MemoirAudioJob.role == ROLE_BACKGROUND_MUSIC,
                MemoirAudioJob.scene_id == WORK_SCENE_ID,
                MemoirAudioJob.segment_index == NO_SEGMENT_INDEX,
            )
            .order_by(MemoirAudioJob.id)
        )
        if for_update:
            query = query.execution_options(populate_existing=True).with_for_update()
        return list(self._session.scalars(query))

    # ------------------------------------------------------------------
    # 状态机写入（全部走 lease fencing + Run 门禁）
    # ------------------------------------------------------------------

    def mark_submitting(
        self, job_id: str, lease_token: int, *, now: datetime | None = None
    ) -> MemoirAudioJob:
        """reserved/submitting → submitting：即将调用供应商。

        R4：状态迁移由 _fenced_update 落盘——当前读校验后走数据库 CAS，
        不再依赖 Session 身份映射中的 ORM 旧值。
        """
        return self._fenced_update(
            job_id, lease_token, {STATE_RESERVED, STATE_SUBMITTING},
            {"state": STATE_SUBMITTING}, now=now,
        )

    def mark_submitted(
        self,
        job_id: str,
        lease_token: int,
        *,
        provider_task_id: str | None = None,
        now: datetime | None = None,
    ) -> MemoirAudioJob:
        """submitting → submitted：提交返回立即存安全 TaskID。

        音乐为异步任务，必须携带 provider_task_id；TTS 同步 SSE 无 TaskID。
        R4：附加字段校验（音乐必须带 TaskID）在 CAS 之前完成，校验失败不
        产生任何账本写入；state 与 TaskID 折叠为同一条 CAS UPDATE。
        """
        job = self._load_for_write(
            job_id, lease_token, {STATE_SUBMITTING, STATE_SUBMITTED}, now=now
        )
        if job.role == ROLE_BACKGROUND_MUSIC and not provider_task_id:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_INPUT_INVALID", "音乐任务必须携带 provider_task_id"
            )
        return self._cas_update(
            job,
            lease_token,
            {STATE_SUBMITTING, STATE_SUBMITTED},
            {"state": STATE_SUBMITTED, "provider_task_id": provider_task_id or None},
        )

    def mark_processing(
        self, job_id: str, lease_token: int, *, now: datetime | None = None
    ) -> MemoirAudioJob:
        """submitted → processing：音乐轮询/下载转码进行中。"""
        return self._fenced_update(
            job_id, lease_token, {STATE_SUBMITTED, STATE_PROCESSING},
            {"state": STATE_PROCESSING}, now=now,
        )

    def record_object_key(
        self,
        job_id: str,
        lease_token: int,
        object_key: str,
        mime: str,
        *,
        now: datetime | None = None,
    ) -> MemoirAudioJob:
        """上传前固定对象键：此后不可变，恢复可对账同一对象。

        同键重复调用幂等；异键冲突拒绝（防止重试产生新孤儿键）。
        """
        if not isinstance(object_key, str) or not object_key:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_INPUT_INVALID", "对象键为空")
        if not isinstance(mime, str) or not mime.startswith("audio/"):
            raise MemoirAudioJobsError("MEMOIR_AUDIO_INPUT_INVALID", "MIME 非法")
        job = self._load_for_write(
            job_id, lease_token, set(_ACTIVE_STATES), now=now
        )
        if job.object_key is not None:
            if job.object_key != object_key:
                raise MemoirAudioJobsError(
                    "MEMOIR_AUDIO_OBJECT_KEY_CONFLICT", "对象键已固定，不得更改"
                )
            return job  # 幂等：同键重复固定。
        # R4：对象键落盘走 CAS（token+来源态原子条件），并发接管后迟到
        # 的键固定会被 rowcount 判定拒绝，不产生第二个孤儿键。
        return self._cas_update(
            job, lease_token, set(_ACTIVE_STATES),
            {"object_key": object_key, "mime": mime},
        )

    def record_upload(
        self,
        job_id: str,
        lease_token: int,
        *,
        duration_ms: int,
        now: datetime | None = None,
    ) -> MemoirAudioJob:
        """私有上传完成：记录实测时长并进入 uploaded。

        必须先 record_object_key 固定键，再上传，再记录；时长为 ffprobe
        实测毫秒（正整数），禁止估算值入账。
        """
        _require_positive_int(duration_ms, "MEMOIR_AUDIO_INPUT_INVALID", "时长非法")
        job = self._load_for_write(job_id, lease_token, set(_ACTIVE_STATES), now=now)
        if job.object_key is None:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_INPUT_INVALID", "必须先固定对象键再记录上传"
            )
        # R4：迟到线程的 record_upload 走 token+来源态 CAS——旧 token 或
        # 已迁移状态在此被原子拒绝（迟到副作用控制的关键写入口）。
        return self._cas_update(
            job, lease_token, set(_ACTIVE_STATES),
            {"state": STATE_UPLOADED, "duration_ms": duration_ms},
        )

    def mark_published(
        self, job_id: str, lease_token: int, *, now: datetime | None = None
    ) -> MemoirAudioJob:
        """uploaded → published：资产已随文档发布，移交 Business 生命周期。"""
        return self._fenced_update(
            job_id, lease_token, {STATE_UPLOADED, STATE_PUBLISHED},
            {"state": STATE_PUBLISHED}, now=now,
        )

    def mark_failed(
        self,
        job_id: str,
        lease_token: int,
        *,
        error_code: str,
        now: datetime | None = None,
    ) -> MemoirAudioJob:
        """在途 → failed：只存固定错误枚举，可由 reserve_job 重试复活。"""
        if not isinstance(error_code, str) or not error_code:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_INPUT_INVALID", "错误枚举为空")
        return self._fenced_update(
            job_id, lease_token, set(_ACTIVE_STATES),
            {"state": STATE_FAILED, "error_code": error_code}, now=now,
        )

    def mark_cancelled(
        self,
        job_id: str,
        lease_token: int,
        *,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> MemoirAudioJob:
        """在途 → cancelled：Run 取消/门禁停止，不再自动重试。"""
        return self._fenced_update(
            job_id, lease_token, set(_ACTIVE_STATES),
            {"state": STATE_CANCELLED, "error_code": error_code}, now=now,
        )

    def mark_submission_unknown(
        self,
        job_id: str,
        lease_token: int,
        *,
        error_code: str,
        now: datetime | None = None,
    ) -> MemoirAudioJob:
        """submitting/submitted → submission_unknown：网络已可能受理但结果未知。

        终态语义：不自动重提（避免重复收费）、不释放预留（pending
        reconciliation）、缺用量不得按 0 结算。
        """
        if not isinstance(error_code, str) or not error_code:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_INPUT_INVALID", "错误枚举为空")
        return self._fenced_update(
            job_id, lease_token, {STATE_SUBMITTING, STATE_SUBMITTED},
            {"state": STATE_SUBMISSION_UNKNOWN, "error_code": error_code}, now=now,
        )

    # ------------------------------------------------------------------
    # 结算
    # ------------------------------------------------------------------

    def settle_tts_usage(
        self,
        job_id: str,
        lease_token: int,
        *,
        usage_text_words: int,
        now: datetime | None = None,
    ) -> MemoirAudioJob:
        """TTS 结算：按供应商实际 text_words 计费（四舍五入 6 位）。

        允许在 submitted/processing/uploaded/published 结算；终态失败族
        （failed/cancelled/submission_unknown/cleaned）不可结算。
        """
        if isinstance(usage_text_words, bool) or not isinstance(usage_text_words, int):
            raise MemoirAudioJobsError("MEMOIR_AUDIO_INPUT_INVALID", "用量字数非法")
        if usage_text_words < 0:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_INPUT_INVALID", "用量字数为负")
        policy = self._require_policy()
        # R4：金额先行计算（纯内存运算），用量与金额一并折叠进同一条 CAS。
        settled = (
            Decimal(usage_text_words)
            * policy.tts_price_per_1000_text_words
            / Decimal(1000)
        ).quantize(_COST_EXPONENT, rounding=ROUND_HALF_UP)
        job = self._fenced_update(
            job_id,
            lease_token,
            {STATE_SUBMITTED, STATE_PROCESSING, STATE_UPLOADED, STATE_PUBLISHED},
            {"usage_text_words": usage_text_words, "settled_cost": settled},
            now=now,
        )
        logger.info(
            "Memoir 音频 TTS 已结算，code=MEMOIR_AUDIO_TTS_SETTLED，words=%d", usage_text_words
        )
        return job

    def settle_music_usage(
        self,
        job_id: str,
        lease_token: int,
        *,
        requested_music_seconds: int,
        now: datetime | None = None,
    ) -> MemoirAudioJob:
        """音乐结算：按请求秒数计费（协议固定 60 秒，按请求值入账）。"""
        _require_positive_int(
            requested_music_seconds, "MEMOIR_AUDIO_INPUT_INVALID", "音乐秒数非法"
        )
        policy = self._require_policy()
        # R4：同 TTS——金额先算，秒数与金额折叠进同一条 CAS UPDATE。
        settled = (
            Decimal(requested_music_seconds) * policy.music_price_per_second
        ).quantize(_COST_EXPONENT, rounding=ROUND_HALF_UP)
        job = self._fenced_update(
            job_id,
            lease_token,
            {STATE_SUBMITTED, STATE_PROCESSING, STATE_UPLOADED, STATE_PUBLISHED},
            {"requested_music_seconds": requested_music_seconds, "settled_cost": settled},
            now=now,
        )
        logger.info(
            "Memoir 音频音乐已结算，code=MEMOIR_AUDIO_MUSIC_SETTLED，seconds=%d",
            requested_music_seconds,
        )
        return job

    def settle_default_music_usage(
        self, job_id: str, lease_token: int, *, now: datetime | None = None
    ) -> MemoirAudioJob:
        """默认 BGM 零费结算（freeze 2026-09-11 §4.2）：只写 settled_cost = 0。

        与 settle_music_usage 并列且互不替代：
        - 允许来源态 reserved / uploaded——默认路径状态流
          reserved → record_object_key → uploaded → published，永不
          mark_submitting/submitted/processing；
        - 不改 requested_music_seconds / provider_task_id，不校验音乐秒数、
          不查火山、绝不调用 settle_music_usage；
        - 幂等：settled_cost 已为 0 直接返回该行；已是非 0 金额（付费
          结算）→ 拒绝，禁止零费覆盖付费账；
        - 金额恒为 0，无需计费策略（不走 _require_policy）。
        """
        job = self._load_for_write(
            job_id, lease_token, {STATE_RESERVED, STATE_UPLOADED}, now=now
        )
        settled = job.settled_cost
        if settled is not None and settled != 0:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_SETTLE_CONFLICT", "已存在付费结算，禁止零费覆盖"
            )
        if settled == 0:
            return job  # 幂等：零费结算已落账，直接返回该行。
        job = self._cas_update(
            job, lease_token, {STATE_RESERVED, STATE_UPLOADED},
            {"settled_cost": Decimal("0")},
        )
        logger.info(
            "Memoir 音频默认音乐已零费结算，code=MEMOIR_AUDIO_DEFAULT_MUSIC_SETTLED"
        )
        return job

    # ------------------------------------------------------------------
    # lease 心跳
    # ------------------------------------------------------------------

    def renew_lease(
        self,
        job_id: str,
        lease_token: int,
        *,
        ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
        now: datetime | None = None,
    ) -> MemoirAudioJob:
        """同 token 心跳续约：过期后仍可续（token 不变），终态不可续。"""
        if ttl_seconds <= 0:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_INPUT_INVALID", "续约时长非法")
        moment = now or datetime.now(UTC)
        # R4：续约同样走 CAS——token+来源态原子条件，过期不阻断
        # （check_expiry=False 语义保持：过期后同 token 仍可续约复活）。
        return self._fenced_update(
            job_id, lease_token, set(_ACTIVE_STATES),
            {"expires_at": moment + timedelta(seconds=ttl_seconds)},
            now=moment, check_expiry=False,
        )

    def rotate_lease(
        self,
        job_id: str,
        expected_token: int,
        *,
        owner: str,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> MemoirAudioJob:
        """恢复接管专用：校验旧 token 后旋转 fencing token 并移交 owner。

        R4：跨 Session 恢复在途作业时必须旋转 token——旋转后上一 Session
        持旧 token 的迟到 renew/mark/upload 全部被 _load_for_write 的
        fencing 校验拒绝（MEMOIR_AUDIO_FENCING_REJECTED），杜绝旧 worker
        在新 Session 接管后写入任何账本副作用。

        与 renew_lease 同口径走五连环写前校验（含 Run 取消/隐私门禁）；
        过期不阻断恢复（check_expiry=False，接管场景 lease 多已过期）；
        expected_token 不匹配抛既有 fencing 冲突异常。commit 由调用方
        （service 层 _commit_ledger）统一执行。

        原子性（R4 新增）：旋转本身是条件 CAS——WHERE 携带
        ``lease_token == expected_token``，SET 侧用 SQL 表达式
        ``lease_token + 1``（绝不回读 Session 旧值）。两个 Session 以
        同一 expected_token 并发旋转时，数据库侧只有一方 rowcount=1，
        另一方按 MEMOIR_AUDIO_FENCING_REJECTED 拒绝，不会出现双旋转。
        Run 门禁按"传入的新 owner"校验执行尝试身份：行内旧 owner 在
        接管场景必然是旧 attempt，不能据此拒绝新尝试的接管。
        """
        if ttl_seconds <= 0:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_INPUT_INVALID", "续约时长非法")
        if not isinstance(owner, str) or not owner:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_INPUT_INVALID", "lease owner 为空")
        moment = now or datetime.now(UTC)
        job = self._load_for_write(
            job_id, expected_token, set(_ACTIVE_STATES), now=moment,
            check_expiry=False, fence_owner=owner,
        )
        # token 用 SQL 侧自增（lease_token + 1，非 ORM 旧值）；owner 与
        # 过期时间一并移交新持有者，三条字段折叠为同一条 CAS UPDATE。
        return self._cas_update(
            job, expected_token, set(_ACTIVE_STATES),
            {
                "lease_token": MemoirAudioJob.lease_token + 1,
                "lease_owner": owner,
                "expires_at": moment + timedelta(seconds=ttl_seconds),
            },
        )

    # ------------------------------------------------------------------
    # 维护对账（维护 CLI 专用，无 lease 要求）
    # ------------------------------------------------------------------

    def list_orphan_candidates(self, *, limit: int) -> list[MemoirAudioJob]:
        """扫描孤儿候选：非在途且对象键非空，按 updated_at 升序分批。"""
        return list(
            self._session.scalars(
                sa.select(MemoirAudioJob)
                .where(
                    MemoirAudioJob.state.in_(_ORPHAN_STATES),
                    MemoirAudioJob.object_key.is_not(None),
                )
                .order_by(MemoirAudioJob.updated_at.asc())
                .limit(limit)
            )
        )

    def fail_abandoned_keyed_jobs(
        self, *, now: datetime | None = None, grace_seconds: float
    ) -> int:
        """把过保留窗的持键在途作业收割为 failed（AUDIO_LEASE_ABANDONED）。

        不扩 _ORPHAN_STATES：mark_cleaned 非 CAS，若把 active 扫进去会与
        rotate_lease 竞态（恢复旋转后对象被删 → 迟到上传重建但 job=cleaned
        → 泄漏）。本方法用条件 UPDATE 本身做 fencing：恢复 worker 先
        rotate_lease → expires_at 刷新 → 本条件不命中。

        复活安全性（冻结文档 §2.4）：
        - 持键作业费用已入账（旁白零成本槽 / BGM 在 _finalize 最前 settle），
          mark_failed 与复活不重复收费。
        - 复活保留同一 object_key，重试上传同键；对象已被删则重建。
        - 迟到上传重建时 state 已是 active 且 expires_at 已刷新，既不在
          _ORPHAN_STATES 也不满足本收割条件，不会二次删除。
        - 24h 窗 ≫ Run 生命周期，实际不可观测。

        submission_unknown 不在 _ACTIVE_STATES，天然不收割。
        不持键 active 无对象可清，且 BGM 未持键=未结算，不收割。
        """
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(seconds=grace_seconds)
        result = self._session.execute(
            sa.update(MemoirAudioJob)
            .where(
                MemoirAudioJob.state.in_(_ACTIVE_STATES),
                MemoirAudioJob.object_key.is_not(None),
                MemoirAudioJob.expires_at.is_not(None),
                MemoirAudioJob.expires_at < cutoff,
            )
            .values(
                state=STATE_FAILED,
                error_code="AUDIO_LEASE_ABANDONED",
                # reaper 不得刷新 updated_at，否则保留窗被重置，同轮无法删除。
                # 显式 SET updated_at = 列自身，压住 ORM onupdate 与 MySQL
                # ON UPDATE CURRENT_TIMESTAMP。
                updated_at=MemoirAudioJob.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        # 条件 UPDATE 不刷新身份映射；同一 Session 紧接着
        # list_orphan_candidates 必须看到 failed，否则 mark_cleaned 会因
        # 缓存的 reserved 态误判「非孤儿」。
        self._session.expire_all()
        count = int(result.rowcount or 0)
        logger.info(
            "Memoir 音频持键弃置作业已收割，code=MEMOIR_AUDIO_LEASE_ABANDONED，count=%d",
            count,
        )
        return count

    def fail_abandoned_keyless_default_jobs(
        self, *, now: datetime | None = None, grace_seconds: float
    ) -> int:
        """把过保留窗的无键零费默认槽收敛为 failed + settled=0（§11.3 D4）。

        与 fail_abandoned_keyed_jobs 互补：持键在途由其收割；无键
        reserved 默认槽（确定性转码失败 / 进程中断在 record_object_key
        之前崩溃的窗口）没有对象可清、没有费可对，却是唯一会永久停留
        reserved 的形状——不收敛则该作品 BGM 永久降级。终结为 failed 且
        settled_cost=0：后续生成请求经 reserve_job 复活（attempt+1、
        lease fence 旋转保留），attempt 达上限后自然终态，有界收敛。

        仅默认零费槽：role=background_music 且 reserved_cost=0 且无
        provider_task_id——火山槽必有非零预留或任务号，天然不命中；
        不扩 _ORPHAN_STATES、不碰 submission_unknown、无对象删除。
        settled_cost 直接随条件 UPDATE 置 0，不经过
        settle_default_music_usage 的 lease CAS（维护无 lease）；WHERE
        已限定零费默认槽，不存在"零费覆盖付费账"的可能。
        updated_at 保留原值（与持键收割同口径，防保留窗被重置）。
        """
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(seconds=grace_seconds)
        result = self._session.execute(
            sa.update(MemoirAudioJob)
            .where(
                MemoirAudioJob.state == STATE_RESERVED,
                MemoirAudioJob.role == ROLE_BACKGROUND_MUSIC,
                MemoirAudioJob.object_key.is_(None),
                MemoirAudioJob.provider_task_id.is_(None),
                MemoirAudioJob.reserved_cost == Decimal("0"),
                MemoirAudioJob.expires_at.is_not(None),
                MemoirAudioJob.expires_at < cutoff,
            )
            .values(
                state=STATE_FAILED,
                error_code="AUDIO_LEASE_ABANDONED",
                settled_cost=Decimal("0"),
                updated_at=MemoirAudioJob.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        # 与持键收割同因：条件 UPDATE 不进身份映射，同 Session 后续读
        # 必须看到 failed，避免缓存旧态误判。
        self._session.expire_all()
        count = int(result.rowcount or 0)
        logger.info(
            "Memoir 音频无键默认槽已终结，code=MEMOIR_AUDIO_LEASE_ABANDONED，count=%d",
            count,
        )
        return count

    def mark_cleaned(self, job_id: str) -> MemoirAudioJob:
        """维护专用：孤儿态对象已删除/确认移交后标记 cleaned。"""
        job = self._session.scalar(
            sa.select(MemoirAudioJob).where(MemoirAudioJob.job_id == job_id)
        )
        if job is None:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_JOB_NOT_FOUND", "作业不存在")
        if job.state not in _ORPHAN_STATES:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_JOB_STATE_INVALID", "非孤儿态不得标记清理"
            )
        job.state = STATE_CLEANED
        return job

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _require_policy(self) -> MemoirAudioCostPolicy:
        """维护路径无策略；一旦涉及钱必须 fail-fast。"""
        if self._policy is None:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_POLICY_INVALID", "未配置音频计费策略"
            )
        return self._policy

    def _validate_reservation(self, reservation: MemoirAudioJobReservation) -> None:
        """预留入参校验：全部为安全枚举/短 ID 检查，不触网不查库。"""
        for name in ("job_id", "business_id", "run_id", "package_version",
                     "scene_id", "input_hmac", "lease_owner"):
            value = getattr(reservation, name)
            if not isinstance(value, str) or not value:
                raise MemoirAudioJobsError(
                    "MEMOIR_AUDIO_INPUT_INVALID", f"{name} 为空"
                )
        if isinstance(reservation.generation_epoch, bool) or not isinstance(
            reservation.generation_epoch, int
        ):
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_INPUT_INVALID", "generation_epoch 必须为整数"
            )
        if reservation.role not in ALL_ROLES:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_INPUT_INVALID", "未知音频角色")
        cost = reservation.estimated_cost
        if not isinstance(cost, Decimal) or cost.is_nan() or cost.is_infinite() or cost < 0:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_INPUT_INVALID", "预留金额非法")
        if reservation.lease_ttl_seconds <= 0:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_INPUT_INVALID", "lease 时长非法")
        # 分段角色必须携带非负分段序号；非分段角色强制哨兵 -1 参与唯一键。
        if reservation.role == ROLE_SEGMENT:
            if reservation.segment_index < 0:
                raise MemoirAudioJobsError(
                    "MEMOIR_AUDIO_INPUT_INVALID", "分段序号必须 >= 0"
                )
        elif reservation.segment_index != NO_SEGMENT_INDEX:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_INPUT_INVALID", "非分段作业分段序号必须为哨兵 -1"
            )
        # 音乐秒数三分支裁决（freeze 2026-09-11 §4.1）：
        # - 零费 BGM 槽（estimated_cost == 0，默认配乐）：requested_music_seconds
        #   必须为 None——默认模式无付费秒数概念，传 int 同样拒绝；
        # - 付费 BGM 槽（estimated_cost > 0）：必须声明非 bool 的 int 秒数
        #   （现有"音乐必须声明请求秒数"语义保留）；
        # - 其他角色不得携带音乐秒数（现有分支不动）。
        if reservation.role == ROLE_BACKGROUND_MUSIC:
            seconds = reservation.requested_music_seconds
            if reservation.estimated_cost == 0:
                if seconds is not None:
                    raise MemoirAudioJobsError(
                        "MEMOIR_AUDIO_INPUT_INVALID", "零费音乐槽不得携带请求秒数"
                    )
            elif isinstance(seconds, bool) or not isinstance(seconds, int):
                raise MemoirAudioJobsError(
                    "MEMOIR_AUDIO_INPUT_INVALID", "音乐必须声明请求秒数"
                )
        elif reservation.requested_music_seconds is not None:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_INPUT_INVALID", "非音乐作业不得携带音乐秒数"
            )

    def _find_by_slot(
        self, reservation: MemoirAudioJobReservation
    ) -> MemoirAudioJob | None:
        """按唯一槽定位既有行（含全部状态）。"""
        return self.find_job(
            run_id=reservation.run_id,
            generation_epoch=reservation.generation_epoch,
            package_version=reservation.package_version,
            role=reservation.role,
            scene_id=reservation.scene_id,
            input_hmac=reservation.input_hmac,
            segment_index=reservation.segment_index,
        )

    def _assert_run_active(self, run_id: str, *, lease_owner: str | None = None) -> None:
        """Run 门禁：存在、未请求取消、隐私状态 active 才允许音频副作用。

        R4：对 AgentRun 也做当前读（populate_existing 强制刷新 Session 身份
        映射中的旧值，MySQL 侧 with_for_update 加行锁），并叠加执行尝试
        fence——lease_owner 形如 ``audio:{run_id}:attempt-{N}`` 时，N 必须
        等于 run.execution_attempt（None/0 视为 1，与服务层构造 lease_owner
        的口径一致），旧执行尝试一律拒绝写入（MEMOIR_AUDIO_FENCING_REJECTED）。
        非 该格式的 owner（历史/测试数据如 "worker-a"）解析失败不阻断：
        仅在格式匹配且 attempt 与当前执行尝试不一致时才拒绝。
        """
        run = self._session.scalar(
            sa.select(AgentRun)
            .where(AgentRun.run_id == run_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if run is None:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_RUN_NOT_FOUND", "Run 不存在")
        if run.cancel_requested_at is not None:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_RUN_CANCELLED", "Run 已请求取消，停止音频副作用"
            )
        if run.privacy_state != "active":
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_RUN_PRIVACY_BLOCKED", "Run 隐私状态阻止音频写入"
            )
        if lease_owner is not None:
            owner_attempt = _parse_lease_owner_attempt(run_id, lease_owner)
            # 口径对齐服务层：execution_attempt 为 None/0 时按 1 记。
            current_attempt = getattr(run, "execution_attempt", None) or 1
            if owner_attempt is not None and owner_attempt != current_attempt:
                raise MemoirAudioJobsError(
                    "MEMOIR_AUDIO_FENCING_REJECTED", "旧执行尝试不得写入新尝试的作业"
                )

    def _load_for_write(
        self,
        job_id: str,
        lease_token: int,
        expected_states: set[str],
        *,
        now: datetime | None = None,
        check_expiry: bool = True,
        fence_owner: str | None = None,
    ) -> MemoirAudioJob:
        """写前统一校验（R4 升级为当前读）：存在 → Run 门禁（含执行尝试
        fence）→ fencing → 过期 → 来源态。

        S2（2026-09-15）：持行锁路径必须 Run → Job，禁止再 Job → Run。
        peek 按 job_id 无锁取不可变 run_id（此次禁止 FOR UPDATE，否则
        与 reserve 的 Run→Job 构成等待环）→ _assert_run_active（Run
        FOR UPDATE；rotate_lease 仍传 fence_owner=新 owner）→ Job
        populate_existing + FOR UPDATE → 重新校验存在 / token / 来源态
        / 过期。peek 后行消失 → MEMOIR_AUDIO_JOB_NOT_FOUND。
        """
        # 无锁 peek：只取 run_id / 默认 lease_owner，不加 Job 行锁。
        peeked = self._session.scalar(
            sa.select(MemoirAudioJob).where(MemoirAudioJob.job_id == job_id)
        )
        if peeked is None:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_JOB_NOT_FOUND", "作业不存在")
        run_id = peeked.run_id
        self._assert_run_active(
            run_id,
            lease_owner=peeked.lease_owner if fence_owner is None else fence_owner,
        )
        job = self._session.scalar(
            sa.select(MemoirAudioJob)
            .where(MemoirAudioJob.job_id == job_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if job is None:
            raise MemoirAudioJobsError("MEMOIR_AUDIO_JOB_NOT_FOUND", "作业不存在")
        if job.lease_token != lease_token:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_FENCING_REJECTED", "旧 lease 不得写入新结果"
            )
        if check_expiry:
            if job.expires_at is None or _as_aware(job.expires_at) <= (
                now or datetime.now(UTC)
            ):
                raise MemoirAudioJobsError(
                    "MEMOIR_AUDIO_LEASE_EXPIRED", "lease 已过期，先续约再写入"
                )
        if job.state not in expected_states:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_JOB_STATE_INVALID", f"状态 {job.state} 不允许该写入"
            )
        return job

    def _cas_update(
        self,
        job: MemoirAudioJob,
        lease_token: int,
        expected_states: set[str],
        values: dict[str, Any],
    ) -> MemoirAudioJob:
        """R4 权威 CAS：token + 来源态作条件的原子 UPDATE。

        rowcount != 1 说明校验与写入之间发生并发接管（token 被旋转或状态
        已迁移），一律按 MEMOIR_AUDIO_FENCING_REJECTED 拒绝。MySQL 方言由
        SQLAlchemy 强制 FOUND_ROWS 客户端标记，rowcount 为 matched 口径，
        同值空写（如同一秒内重复续约）不会误判为并发冲突。
        synchronize_session=False：不走 ORM 同步（values 可含 SQL 表达式），
        统一以 refresh 重新加载行内权威值。
        """
        result = self._session.execute(
            sa.update(MemoirAudioJob)
            .where(
                MemoirAudioJob.id == job.id,
                MemoirAudioJob.lease_token == lease_token,
                MemoirAudioJob.state.in_(tuple(expected_states)),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_FENCING_REJECTED", "并发写入被 fencing 拒绝（token/状态已被接管）"
            )
        self._session.refresh(job)
        return job

    def _fenced_update(
        self,
        job_id: str,
        lease_token: int,
        expected_states: set[str],
        values: dict[str, Any],
        *,
        now: datetime | None = None,
        check_expiry: bool = True,
    ) -> MemoirAudioJob:
        """R4 fenced 写入统一入口：当前读校验 → 数据库原子 CAS → refresh 返回。"""
        job = self._load_for_write(
            job_id, lease_token, expected_states, now=now, check_expiry=check_expiry
        )
        return self._cas_update(job, lease_token, expected_states, values)

    @staticmethod
    def _parse_lease_owner_attempt(run_id: str, lease_owner: str) -> int | None:
        """解析 lease_owner 的执行尝试号；非 audio:{run_id}:attempt-{N} 格式
        （历史/测试数据）返回 None 不阻断。"""
        return _parse_lease_owner_attempt(run_id, lease_owner)
