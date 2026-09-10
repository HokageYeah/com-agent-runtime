"""M8 R7 Memoir 音频作业账本模型（Runtime 执行元数据，非业务作品表）。

两张表：
1. `MemoirAudioJob`（memoir_audio_jobs）：音频副作用账本行。唯一槽由
   (run_id, generation_epoch, package_version, role, scene_id, segment_index,
   input_hmac) 组成；BGM 用显式哨兵 scene_id=`__work__`、segment_index=-1
   参与唯一约束，不依赖数据库 NULL 相等语义。
2. `MemoirAudioRunBudget`（memoir_audio_run_budgets）：Run 级音频预算行，
   run_id 唯一；预留通过条件 UPDATE（reserved_total + cost <= cap）原子封顶，
   与作业槽插入同事务，禁止多场景各自读旧余额。

隐私铁律：本表只存元数据（枚举、短 ID、HMAC、Decimal 金额），禁止落
正文、prompt、私有 URL、音频字节；错误只存固定 error_code 枚举。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.sqlalchemy_db import Base
from app.models.runtime import TimestampMixin

# ---------------------------------------------------------------------------
# 账本共享常量（服务层与迁移共用，避免字面量漂移）
# ---------------------------------------------------------------------------

# 角色枚举：旁白最终资产 / 背景音乐 / TTS 分段提交（分段临时资产不上 OSS）。
ROLE_NARRATION = "narration"
ROLE_BACKGROUND_MUSIC = "background_music"
ROLE_SEGMENT = "segment"
ALL_ROLES = (ROLE_NARRATION, ROLE_BACKGROUND_MUSIC, ROLE_SEGMENT)

# BGM 的作品级哨兵：scene_id 固定 __work__、segment_index 固定 -1，
# 使非分段作业在唯一键里取确定值，跨 SQLite/PostgreSQL/MySQL 行为一致。
WORK_SCENE_ID = "__work__"
NO_SEGMENT_INDEX = -1


def _sql_in(values: tuple[str, ...]) -> str:
    """生成跨库安全的 IN (...) 字面量（单引号，PostgreSQL/MySQL/SQLite 一致）。"""
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"

# 状态机：reserved → submitting → submitted（音乐已知 TaskID）→ processing
# → uploaded → published；终结分支 failed/cancelled/submission_unknown/cleaned。
STATE_RESERVED = "reserved"
STATE_SUBMITTING = "submitting"
STATE_SUBMITTED = "submitted"
STATE_PROCESSING = "processing"
STATE_UPLOADED = "uploaded"
STATE_PUBLISHED = "published"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_SUBMISSION_UNKNOWN = "submission_unknown"
STATE_CLEANED = "cleaned"
ALL_STATES = (
    STATE_RESERVED,
    STATE_SUBMITTING,
    STATE_SUBMITTED,
    STATE_PROCESSING,
    STATE_UPLOADED,
    STATE_PUBLISHED,
    STATE_FAILED,
    STATE_CANCELLED,
    STATE_SUBMISSION_UNKNOWN,
    STATE_CLEANED,
)


class MemoirAudioJob(Base, TimestampMixin):
    """Memoir 音频作业账本行：唯一槽、预留/结算金额、lease fencing。

    每行对应一个 (run, 输入) 的音频副作用槽；同槽重试复用同一行
    （attempt/lease_token 递增），object_key 一旦固定不可变，恢复可对账
    同一个对象，不因重试产生未知孤儿。
    """

    __tablename__ = "memoir_audio_jobs"
    __table_args__ = (
        # 唯一槽：BGM 以哨兵值参与，禁止 NULL 唯一语义。
        UniqueConstraint(
            "run_id",
            "generation_epoch",
            "package_version",
            "role",
            "scene_id",
            "segment_index",
            "input_hmac",
            name="uq_memoir_audio_job_slot",
        ),
        CheckConstraint(
            f"role IN {_sql_in(ALL_ROLES)}",
            name="ck_memoir_audio_job_role",
        ),
        CheckConstraint(
            f"state IN {_sql_in(ALL_STATES)}",
            name="ck_memoir_audio_job_state",
        ),
        CheckConstraint("attempt >= 1", name="ck_memoir_audio_job_attempt"),
        CheckConstraint("lease_token >= 1", name="ck_memoir_audio_job_lease_token"),
        CheckConstraint("segment_index >= -1", name="ck_memoir_audio_job_segment"),
        CheckConstraint("reserved_cost >= 0", name="ck_memoir_audio_job_reserved"),
        CheckConstraint("settled_cost IS NULL OR settled_cost >= 0",
                        name="ck_memoir_audio_job_settled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 作业短 ID：调用方生成的 opaque 标识，仅用于日志与维护对账，不含正文。
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # 业务定位：哪个业务对象（archive）触发的 Run；与 run_id 一起建索引。
    business_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 世代与包版本：唯一槽组成部分；换代或升包后旧槽不复用。
    generation_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    package_version: Mapped[str] = mapped_column(String(32), nullable=False)
    # 角色：narration 最终资产 / background_music 作品级 / segment 分段提交。
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    # 场景 ID：BGM 固定 __work__；segment 为所属场景；narration 为最终场景。
    scene_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 分段序号：非分段作业固定 -1（哨兵），segment 从 0 递增。
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False,
                                               default=NO_SEGMENT_INDEX)
    # 输入 HMAC：覆盖完整正文、分段规则版本与全部声音参数；不含明文。
    input_hmac: Mapped[str] = mapped_column(String(64), nullable=False)
    # 尝试次数：失败重试 +1，预算按尝试累积预留。
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 状态机当前态（枚举见 ALL_STATES）。
    state: Mapped[str] = mapped_column(String(32), nullable=False,
                                       default=STATE_RESERVED)
    # 供应商任务 ID：音乐异步任务必填；TTS 同步 SSE 为空。
    provider_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # OSS 对象键：上传前固定、此后不可变；恢复对账同一对象。
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # MIME：仅允许 audio/mpeg（上传时由服务层校验）。
    mime: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 实测时长（毫秒）：ffprobe 实测向上取整，禁止估算值入账。
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 本行累计预留金额（元）：每次尝试按保守上界累加，未知结果不释放。
    reserved_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
    # 结算金额（元）：按实际 text_words/音乐秒数结算；未结算为 NULL
    #（pending reconciliation，绝不视作 0）。
    settled_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )
    # 计费币种：与预算行一致，冻结只允许 CNY。
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    # TTS 实际用量字数（供应商返回的 text_words 口径）。
    usage_text_words: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 音乐请求秒数（协议固定 60，按请求值结算）。
    requested_music_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # lease 归属（worker 标识）与 fencing token：重试 token+1，旧 owner 拒写。
    lease_owner: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    # lease 过期时间：过期后同 token 可心跳续约，但不可写结果。
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 固定错误枚举：只存安全 code，绝不存供应商原文/URL/stderr。
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MemoirAudioRunBudget(Base, TimestampMixin):
    """Run 级音频预算行：reserved_total_cost 在同事务内原子封顶。

    预留使用条件 UPDATE（reserved_total + cost <= cap，rowcount 判定），
    与作业槽插入同 savepoint；失败方回滚即预算回滚，杜绝并发透支。
    """

    __tablename__ = "memoir_audio_run_budgets"
    __table_args__ = (
        CheckConstraint("reserved_total_cost >= 0",
                        name="ck_memoir_audio_run_budget_reserved"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Run 唯一：一个 Run 一行预算，所有场景/音乐共用同一封顶。
    run_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    # 计费币种：与作业行一致（CNY）。
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    # 已预留总额（元）：保守上界累计；未知结果不释放，结算差异由对账回收。
    reserved_total_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
