from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.sqlalchemy_db import Base


class TimestampMixin:
    """根工程 Runtime 表共用时间戳，便于审计状态变更。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AgentDefinition(Base, TimestampMixin):
    """已由管理员注册的不可变 AgentPackage 元数据与生命周期记录。"""

    __tablename__ = "agent_definitions"
    __table_args__ = (UniqueConstraint("agent_id", "version"),)

    # SQLite 测试环境要求 INTEGER PRIMARY KEY 才能自增；生产数据库同样可安全映射。
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    runtime_type: Mapped[str] = mapped_column(
        String(24), nullable=False, default="workflow"
    )
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    package_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    # 生命周期变更必须保留操作者和原因，禁止以空值掩盖生产变更来源。
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status_changed_by: Mapped[str] = mapped_column(String(120), nullable=False)
    status_change_reason: Mapped[str] = mapped_column(String(500), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_reason: Mapped[str | None] = mapped_column(String(500))


class AgentRun(Base, TimestampMixin):
    """Runtime 的权威运行账本，业务正文不应写入此表。"""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(80), unique=True, index=True, nullable=False
    )
    agent_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    agent_version: Mapped[str] = mapped_column(String(40), nullable=False)
    package_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(40), nullable=False)
    business_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    business_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False, default="pending"
    )
    dispatch_state: Mapped[str] = mapped_column(
        String(24), index=True, nullable=False, default="held"
    )
    # 输入仅是短期私密运行数据；后续 privacy purge 会按版本屏障删除/拒绝写入。
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    capability_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    authorization_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(500))
    caller_id: Mapped[str] = mapped_column(String(120), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(120), nullable=False)
    # 只做审计索引，真正的幂等唯一约束位于 idempotency_records 并可按 TTL 清理。
    create_idempotency_key: Mapped[str] = mapped_column(
        String(200), index=True, nullable=False
    )
    callback_target_id: Mapped[str] = mapped_column(String(120), nullable=False)
    business_connector_id: Mapped[str] = mapped_column(String(120), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    manual_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    auto_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_event_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    execution_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    privacy_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="active"
    )
    privacy_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    privacy_purge_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    private_data_purged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    held_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_elapsed_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    run_deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    waiting_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdmissionBucket(Base, TimestampMixin):
    """按 global/caller/tenant/agent 维度聚合的事务配额账本。"""

    __tablename__ = "admission_buckets"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_key"),
        CheckConstraint(
            "held_count >= 0 AND queued_count >= 0 AND running_count >= 0",
            name="non_negative_counts",
        ),
    )

    # SQLite 联调需要 INTEGER PRIMARY KEY 才会生成 rowid；生产数据库仍可映射为整型主键。
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    held_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    running_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)


class AgentPlan(Base, TimestampMixin):
    __tablename__ = "agent_plans"
    # 统一使用 INTEGER 自增主键，保证 SQLite 联调与生产 ORM 行为一致。
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    strategy: Mapped[str] = mapped_column(String(24), nullable=False)
    steps_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    dependencies_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    stop_conditions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fallback_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)


class AgentStep(Base, TimestampMixin):
    __tablename__ = "agent_steps"
    # SQLite 只有 INTEGER PRIMARY KEY 才能自动生成步骤审计行主键。
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    step_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    step_name: Mapped[str] = mapped_column(String(120), nullable=False)
    step_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    step_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(500))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentToolCall(Base):
    """每次物理工具调用均保留审计记录；重试复用稳定 logical key。"""

    __tablename__ = "agent_tool_calls"
    # SQLite 测试同样需要 INTEGER PRIMARY KEY 自动生成 rowid。
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tool_call_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    step_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_version: Mapped[str | None] = mapped_column(String(40))
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    side_effect: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    logical_operation_key: Mapped[str | None] = mapped_column(String(200))
    request_digest: Mapped[str | None] = mapped_column(String(128))
    execution_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AgentEvaluation(Base):
    __tablename__ = "agent_evaluations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    evaluation_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    step_id: Mapped[str | None] = mapped_column(String(80), index=True)
    target_type: Mapped[str] = mapped_column(String(60), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(120))
    evaluator_type: Mapped[str] = mapped_column(String(60), nullable=False)
    score_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_summary: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AgentCheckpoint(Base):
    """恢复状态只允许密文或受控 storage_ref，绝不默认保存业务原文。"""

    __tablename__ = "agent_checkpoints"
    __table_args__ = (UniqueConstraint("run_id", "checkpoint_key"),)
    # 与 AgentStep 一致，使用 SQLite/生产均兼容的整型自增主键。
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    checkpoint_key: Mapped[str] = mapped_column(String(160), nullable=False)
    state_schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    data_classification: Mapped[str] = mapped_column(String(32), nullable=False)
    privacy_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    encrypted_state_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    storage_ref: Mapped[str | None] = mapped_column(String(500))
    state_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    content_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AgentArtifact(Base, TimestampMixin):
    __tablename__ = "agent_artifacts"
    # SQLite 仅对 INTEGER PRIMARY KEY 生成 rowid；根工程测试与生产共用该模型。
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    business_resource_ref: Mapped[str] = mapped_column(String(500), nullable=False)


class AgentModelUsage(Base, TimestampMixin):
    """每行一个物理模型 attempt；未知 usage 不能伪造为零成本。"""

    __tablename__ = "agent_model_usages"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usage_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    step_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    execution_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    model_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    permit_id: Mapped[str | None] = mapped_column(String(120))
    capability_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    prompt_id: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    provider: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(120))
    pricing_config_version: Mapped[str | None] = mapped_column(String(40))
    cost_unit: Mapped[str | None] = mapped_column(String(32))
    reserved_estimated_cost: Mapped[float | None] = mapped_column(nullable=True)
    estimated_cost: Mapped[float | None] = mapped_column(nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    request_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )


class CallbackEvent(Base):
    """不可修改的安全 callback 事件；投递重试信息属于 outbox 而非这里。"""

    __tablename__ = "callback_events"
    __table_args__ = (UniqueConstraint("run_id", "event_seq"),)
    # 与其他 Runtime 表一致，SQLite 测试/本地联调使用 rowid 自增主键。
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    status_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RuntimeOutboxEvent(Base, TimestampMixin):
    __tablename__ = "runtime_outbox_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outbox_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class IdempotencyRecord(Base, TimestampMixin):
    """可过期的写请求幂等记录；scope 防止 create/start 等相互错误重放。"""

    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("client_id", "idempotency_key", "scope"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[str] = mapped_column(String(80), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    resource_type: Mapped[str | None] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(120))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )


class RuntimeAuditRecord(Base):
    """Runtime 的追加写安全审计账本，绝不存业务正文或原始模型/工具数据。"""

    __tablename__ = "runtime_audit_records"

    # SQLite 与生产环境共用整型自增主键，审计业务标识使用独立 audit_id。
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 用 UUID 标识一条审计事实，方便外部审计系统去重与关联。
    audit_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    # actor_type/actor_id 记录调用者身份，不能从敏感输入推导或回填。
    actor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    # action/resource 二元组用于按操作和资源追查状态变更。
    action: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    # reason/outcome 只保存受控错误码和结论，避免写入用户自由文本。
    reason_code: Mapped[str | None] = mapped_column(String(80))
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(120), index=True)
    # 调用方只可传入已脱敏的短字段摘要，禁止存 prompt、正文、密钥或 payload。
    metadata_summary: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
