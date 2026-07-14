from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime 的集中配置入口。

    这里故意不提供“Redis 不可用时退回进程内无限流”的开关：后续模型
    流量控制必须 fail-closed，避免多实例部署时突破共享限额。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./agent_runtime.db"
    redis_url: str = "redis://localhost:6379/0"
    runtime_id: str = "agent-runtime"
    agent_package_root: str = "./app/agents"
    callback_allowed_hosts: list[str] = Field(default_factory=list)
    trusted_clients_json: str = '{"couple-diary":{"tenant_id":"couple-diary"}}'
    signature_tolerance_seconds: int = Field(default=300, gt=0)
    run_queue_name: str = "agent-runtime-runs"
    model_traffic_namespace: str = "agent-runtime:model-traffic"
    model_permit_ttl_seconds: int = Field(default=90, gt=0)
    max_steps: int = Field(default=40, gt=0)
    max_model_calls: int = Field(default=12, gt=0)
    max_tool_calls: int = Field(default=20, gt=0)
    max_run_seconds: int = Field(default=600, gt=0)
    max_auto_retry_per_step: int = Field(default=2, ge=0)
    max_manual_run_retry_count: int = Field(default=3, ge=0)
    max_estimated_cost: float = Field(default=10.0, ge=0)
    held_ttl_seconds: int = Field(default=600, gt=0)
    queue_ttl_seconds: int = Field(default=900, gt=0)
    approval_ttl_seconds: int = Field(default=86_400, gt=0)
    max_wall_clock_seconds: int = Field(default=86_400, gt=0)
    lease_timeout_seconds: int = Field(default=90, gt=0)
    callback_max_attempts: int = Field(default=5, gt=0)
    callback_retry_alert_threshold: int = Field(default=3, gt=0)
    outbox_retention_days: int = Field(default=30, gt=0)
    idempotency_ttl_days: int = Field(default=7, gt=0)
    reconciliation_interval_seconds: int = Field(default=300, gt=0)
    reconciliation_failure_threshold: int = Field(default=3, gt=0)
    audit_sink_dsn: str | None = None
    audit_retention_days: int = Field(default=180, gt=0)
    audit_allowed_roles: list[str] = Field(default_factory=lambda: ["runtime_auditor"])
    enabled_outbox_event_types: list[str] = Field(default_factory=list)

    @field_validator(
        "callback_allowed_hosts",
        "audit_allowed_roles",
        "enabled_outbox_event_types",
        mode="before",
    )
    @classmethod
    def parse_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def trusted_clients(self) -> dict[str, dict[str, str]]:
        # 服务身份白名单只在 Runtime 内部使用；绝不能原样返回给 capability 接口。
        parsed = json.loads(self.trusted_clients_json)
        if not isinstance(parsed, dict):
            raise ValueError("TRUSTED_CLIENTS_JSON must be a JSON object")
        return parsed


@lru_cache
def get_settings() -> Settings:
    # 配置在单个进程内只解析一次，测试需要不同配置时直接向 create_app 注入。
    return Settings()
