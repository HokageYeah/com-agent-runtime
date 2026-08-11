"""Task 12 测试专用子进程入口；配置边界不得承载生产配置或真实密钥。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import uvicorn
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.sqlalchemy_db import Base
from app.main import create_runtime_app
from app.models import AgentDefinition
from app.reconciler import ReconcilerRunner
from app.runtime.test_harness import (
    LoopbackTestTransport,
    RuntimeDependencies,
    RuntimeHarnessConfig,
)
from app.services.reconciliation_service import ReconciliationReport
from app.worker import WorkerLoop, configured_callback_gateway, configured_executor

_Role = Literal["api", "worker", "reconciler"]
_ALLOWED_CONFIG_FIELDS = frozenset(
    {
        "database_url",
        "identity_id",
        "mock_port",
        "port",
        "provider_port",
        "redis_url",
        "role",
        "schema",
        "sqlite_path",
        "timeout_seconds",
    }
)


class TestSettings(Settings):
    """仅由 harness_entry 构造的测试设置；调用方不可注入环境或生产 Settings。"""


@dataclass(frozen=True)
class HarnessProcessConfig:
    """父子进程之间唯一允许传递的测试配置，字段必须可安全写入临时文件。"""

    sqlite_path: Path
    port: int
    mock_port: int
    role: _Role
    identity_id: str
    timeout_seconds: float
    provider_port: int | None = None
    redis_url: str | None = None
    database_url: str | None = None
    schema: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"api", "worker", "reconciler"}:
            raise ValueError("TEST_HARNESS_CONFIG_INVALID")
        if not self.sqlite_path.is_absolute() or not self.identity_id:
            raise ValueError("TEST_HARNESS_CONFIG_INVALID")
        if (
            not 1 <= self.port <= 65535
            or not 1 <= self.mock_port <= 65535
            or not 0 < self.timeout_seconds <= 60
        ):
            raise ValueError("TEST_HARNESS_CONFIG_INVALID")
        if (self.database_url is None) != (self.schema is None):
            raise ValueError("TEST_HARNESS_CONFIG_INVALID")
        if (self.provider_port is None) != (self.redis_url is None):
            raise ValueError("TEST_HARNESS_CONFIG_INVALID")
        if self.provider_port is not None and not 1 <= self.provider_port <= 65535:
            raise ValueError("TEST_HARNESS_CONFIG_INVALID")
        if self.redis_url is not None:
            parsed = urlsplit(self.redis_url)
            if parsed.scheme != "redis" or parsed.hostname not in {"127.0.0.1", "localhost"}:
                raise ValueError("TEST_HARNESS_CONFIG_INVALID")
        if self.database_url is not None:
            from app.runtime.postgres_harness import PostgresHarnessConfig

            if urlsplit(self.database_url).password is not None:
                raise ValueError("TEST_HARNESS_CONFIG_INVALID")
            PostgresHarnessConfig(
                self.database_url, self.schema or "", self.timeout_seconds
            )

    def to_payload(self) -> dict[str, str | int | float]:
        payload: dict[str, str | int | float] = {
            "identity_id": self.identity_id,
            "mock_port": self.mock_port,
            "port": self.port,
            "role": self.role,
            "sqlite_path": str(self.sqlite_path),
            "timeout_seconds": self.timeout_seconds,
        }
        if self.database_url is not None and self.schema is not None:
            # 配置文件只含无凭据 loopback DSN；密码通过受限子进程环境注入。
            payload["database_url"] = self.database_url
            payload["schema"] = self.schema
        if self.provider_port is not None and self.redis_url is not None:
            payload["provider_port"] = self.provider_port
            payload["redis_url"] = self.redis_url
        return payload

    @classmethod
    def from_path(cls, path: Path) -> HarnessProcessConfig:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not set(raw) <= _ALLOWED_CONFIG_FIELDS:
                raise ValueError
            return cls(
                sqlite_path=Path(_require_str(raw, "sqlite_path")),
                mock_port=_require_int(raw, "mock_port"),
                port=_require_int(raw, "port"),
                role=_require_str(raw, "role"),  # type: ignore[arg-type]
                identity_id=_require_str(raw, "identity_id"),
                timeout_seconds=_require_float(raw, "timeout_seconds"),
                provider_port=_optional_int(raw, "provider_port"),
                redis_url=_optional_str(raw, "redis_url"),
                database_url=_optional_str(raw, "database_url"),
                schema=_optional_str(raw, "schema"),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("TEST_HARNESS_CONFIG_INVALID") from exc


def _require_str(payload: dict[str, object], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise ValueError(field)
    return value


def _require_int(payload: dict[str, object], field: str) -> int:
    value = payload[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(field)
    return value


def _require_float(payload: dict[str, object], field: str) -> float:
    value = payload[field]
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(field)
    return float(value)


def _optional_str(payload: dict[str, object], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(field)
    return value


def _optional_int(payload: dict[str, object], field: str) -> int | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(field)
    return value


def build_dependencies(config: HarnessProcessConfig) -> RuntimeDependencies:
    """子进程自行产生测试身份与客户端，避免继承父进程 Settings 或密钥。"""
    if config.database_url is None:
        engine = create_engine(f"sqlite:///{config.sqlite_path}")
        Base.metadata.create_all(engine)
    else:
        engine = create_engine(
            config.database_url,
            connect_args={"options": f"-csearch_path={config.schema}"},
            pool_pre_ping=True,
        )

        @event.listens_for(engine, "connect")
        def _set_test_schema(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute(f'SET search_path TO "{config.schema}"')
            finally:
                cursor.close()

    factory = sessionmaker(bind=engine)
    _seed_test_agent_package(factory)
    # 此值仅由随机测试 identity 派生，绝不是生产或用户提供的密钥。
    test_hmac = f"harness-only-{config.identity_id}"
    mock_base_url = f"http://127.0.0.1:{config.mock_port}"
    runtime_settings = TestSettings(
        _env_file=None,
        ENVIRONMENT="test",
        RUNTIME_ID="agent-runtime-harness",
        RUNTIME_TRUSTED_CLIENTS_JSON=json.dumps(
            {
                config.identity_id: {
                    "tenant_id": config.identity_id,
                    "keys": {"test": test_hmac},
                }
            }
        ),
        RUNTIME_BUSINESS_CONNECTORS_JSON=json.dumps(
            {
                "harness_business": {
                    "enabled": True,
                    "base_url": mock_base_url,
                    "runtime_id": "agent-runtime-harness",
                    "key_id": "test",
                    "secret": test_hmac,
                }
            }
        ),
        RUNTIME_CALLBACK_TARGETS_JSON=json.dumps(
            {
                "harness_callback": {
                    "enabled": True,
                    "url": f"{mock_base_url}/callbacks",
                    "runtime_id": "agent-runtime-harness",
                    "key_id": "test",
                    "secret": test_hmac,
                }
            }
        ),
        RUNTIME_REDIS_URL=config.redis_url or "",
        MODEL_ROUTES_JSON=(
            '[{"route_id":"harness-model","provider":"harness","model":"mock",'
            '"endpoint":"https://8.8.8.8/v1","rate_limit_key":"harness:model",'
            '"max_concurrency":1,"rpm_limit":10,"tpm_limit":10000,"timeout_seconds":5,'
            '"permit_ttl_seconds":6,"settle_margin_seconds":1,"price_unit":"usd_per_1k_tokens",'
            '"input_price":0,"output_price":0,"route_config_version":"harness-v1",'
            '"pricing_config_version":"harness-v1","capabilities":["structured_output","private_residency"],'
            '"data_residency":"private","max_context_tokens":4096,"max_output_tokens":512,'
            '"enabled":true,"allowed_tenant_ids":["*"],'
            '"allowed_model_policies":["balanced","emotional_writing","strict"]}]'
            if config.provider_port is not None else "[]"
        ),
        MEMOIR_MODEL_NODE_ROUTES_JSON=(
            '{"extract_highlights":"harness-model","plan_chapters":"harness-model",'
            '"generate_scenes":"harness-model"}' if config.provider_port is not None else "{}"
        ),
    )
    harness_config = RuntimeHarnessConfig(
        session_factory=factory,
        trusted_clients=runtime_settings.trusted_clients,
        runtime_id=runtime_settings.runtime_id,
        mock_base_url=mock_base_url,
        timeout_seconds=config.timeout_seconds,
        provider_base_url=(
            f"http://127.0.0.1:{config.provider_port}"
            if config.provider_port is not None else None
        ),
    )
    transport = LoopbackTestTransport(harness_config)
    provider_adapter = None
    if config.provider_port is not None:
        from app.runtime.test_provider import LoopbackProviderAdapter

        provider_adapter = LoopbackProviderAdapter(
            harness_config, transport, httpx.Client(timeout=config.timeout_seconds, trust_env=False)
        )
    return RuntimeDependencies(
        settings=runtime_settings,
        session_factory=factory,
        clock=None,
        callback_client=httpx.Client(timeout=config.timeout_seconds, trust_env=False),
        tool_client=httpx.Client(timeout=config.timeout_seconds, trust_env=False),
        transport_verifier=transport,
        provider_adapter=provider_adapter,
    )


def _seed_test_agent_package(factory: sessionmaker) -> None:
    """子进程只 seed 固定测试 Package 元数据，不读取部署目录或用户包。"""
    session = factory()
    try:
        if (
            session.scalar(
                select(AgentDefinition).where(
                    AgentDefinition.agent_id == "memoir_agent",
                    AgentDefinition.version == "1.0.0",
                )
            )
            is not None
        ):
            return
        from datetime import UTC, datetime

        session.add(
            AgentDefinition(
                agent_id="memoir_agent",
                version="1.0.0",
                runtime_type="workflow",
                definition_json={
                    "allowed_business_types": ["couple_memory"],
                    # 必须与 app/agents/memoir_agent/1.0.0/workflow.graph.py 的节点声明保持
                    # 一致：每个节点显式 safe_to_rerun，否则 Planner 的 legacy 缺键 guard
                    # 会拒绝创建 Plan。harness 用假 digest，与真实文件 digest 解耦。
                    "workflow_nodes": [
                        {"node_id": "load_snapshot", "node_type": "tool", "safe_to_rerun": True},
                        {"node_id": "sanitize_materials", "node_type": "deterministic", "safe_to_rerun": True},
                        {"node_id": "compute_stats", "node_type": "deterministic", "safe_to_rerun": True},
                        {"node_id": "extract_highlights", "node_type": "model", "safe_to_rerun": True},
                        {"node_id": "plan_chapters", "node_type": "model", "safe_to_rerun": True},
                        {"node_id": "generate_scenes", "node_type": "model", "safe_to_rerun": True},
                        {"node_id": "generate_actions", "node_type": "deterministic", "safe_to_rerun": True},
                        {"node_id": "safety_review", "node_type": "guardrail", "safe_to_rerun": True},
                        {"node_id": "publish_document", "node_type": "tool", "safe_to_rerun": True},
                        {"node_id": "enqueue_media_tasks", "node_type": "deterministic",
                         "next_nodes": [], "optional": True, "safe_to_rerun": False},
                    ],
                },
                package_digest="sha256:harness-memoir",
                contract_version="1.0.0",
                status="active",
                status_changed_at=datetime.now(UTC),
                status_changed_by="harness",
                status_change_reason="test-only-seed",
            )
        )
        session.commit()
    finally:
        session.close()


def _ready(role: _Role) -> None:
    """ready 事件是固定安全摘要，不能承载端口、身份或任何调用内容。"""
    print(
        json.dumps({"event": "ready", "role": role}, separators=(",", ":")), flush=True
    )


def _completed(role: _Role, result_code: str) -> None:
    """仅供 harness 消费的终态事件；禁止携带异常、配置或运行数据。"""
    print(
        json.dumps(
            {"event": "completed", "role": role, "result_code": result_code},
            separators=(",", ":"),
        ),
        flush=True,
    )


class _HarnessReconciler:
    """SQLite 仅验证进程装配；真实跨 Session 对账由 PostgreSQL 集成场景覆盖。"""

    def __init__(self, _session: object) -> None:
        pass

    def run_once(self, *, lease_guard: object) -> ReconciliationReport:
        # Runner 仍显式传入 fencing guard；SQLite liveness 场景不在此执行写入扫描。
        del lease_guard
        return ReconciliationReport(
            scanned=0, repaired=0, dead_letter_callbacks=0, failures=0
        )


def run(config: HarnessProcessConfig) -> None:
    dependencies = build_dependencies(config)
    if config.role == "api":
        app = create_runtime_app(dependencies=dependencies)
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=config.port,
            access_log=False,
            log_level="warning",
        )
        return
    if config.role == "worker":
        _ready("worker")
        try:
            WorkerLoop(
                dependencies.session_factory,
                lambda session: configured_executor(session, dependencies=dependencies),
                worker_id="harness-worker",
                callback_gateway=configured_callback_gateway(dependencies),
                trusted_clients=dependencies.settings.trusted_clients,
            ).run_once()
        except Exception:
            _completed("worker", "failed")
            raise
        _completed("worker", "completed")
        return
    _ready("reconciler")
    runner_kwargs: dict[str, object] = {"interval_seconds": 1}
    # SQLite 仅用于进程存活回归；真实对账的事务/lease 行为必须走共享 PostgreSQL schema。
    if config.database_url is None:
        runner_kwargs["reconciler_factory"] = _HarnessReconciler
    try:
        ReconcilerRunner.from_dependencies(
            dependencies,
            "harness-reconciler",
            **runner_kwargs,
        ).run_once()
    except Exception:
        _completed("reconciler", "failed")
        raise
    _completed("reconciler", "completed")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AgentRuntime test harness process entry"
    )
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    run(HarnessProcessConfig.from_path(args.config))


if __name__ == "__main__":
    main()
