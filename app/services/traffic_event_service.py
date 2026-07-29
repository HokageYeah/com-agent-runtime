"""RuntimeTrafficEvent 的窗口化追加账本与无内容阈值告警。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import RuntimeTrafficEvent
from app.runtime.interfaces import TrafficEventRecorder
from app.schemas.audit import RuntimeAuditEvent
from app.services.audit_service import AuditService

_EVENT_TYPES = frozenset({
    "permit_rejected", "retry_after_applied", "circuit_opened", "circuit_recovered",
    "redis_fail_closed", "prompt_injection_rejected", "semantic_validation_rejected",
})


class SqlAlchemyTrafficEventRecorder(TrafficEventRecorder):
    """用数据库 UPSERT 聚合同一窗口，并仅在首次越阈时写安全审计。"""

    def __init__(
        self, session: Session, *, audit_service: AuditService | None = None,
        alert_threshold: int = 10, window_seconds: int = 60,
    ) -> None:
        if alert_threshold < 1 or window_seconds < 1:
            raise ValueError("RUNTIME_TRAFFIC_RECORDER_CONFIG_INVALID")
        self._session = session
        self._audit = audit_service
        self._threshold = alert_threshold
        self._window_seconds = window_seconds

    def record(
        self, event_type: str, route_id: str, result_code: str, *, occurred_at: datetime | None = None,
    ) -> None:
        if (
            event_type not in _EVENT_TYPES or not _safe_identifier(route_id, 120)
            or not _safe_identifier(result_code, 80)
        ):
            raise ValueError("RUNTIME_TRAFFIC_EVENT_INVALID")
        timestamp = _as_utc(occurred_at or datetime.now(UTC))
        window_start = int(timestamp.timestamp()) // self._window_seconds * self._window_seconds
        window_at = datetime.fromtimestamp(window_start, UTC)
        window_key = f"{event_type}:{route_id}:{result_code}:{window_at.isoformat()}"
        values = {
            "event_type": event_type, "route_id": route_id, "result_code": result_code,
            "occurred_at": window_at, "window_key": window_key, "count": 1,
        }
        dialect = self._session.bind.dialect.name if self._session.bind is not None else ""
        insert = sqlite_insert if dialect == "sqlite" else postgresql_insert if dialect == "postgresql" else None
        if insert is None:
            raise RuntimeError("RUNTIME_TRAFFIC_EVENT_DIALECT_UNSUPPORTED")
        statement = insert(RuntimeTrafficEvent).values(**values).on_conflict_do_update(
            index_elements=[RuntimeTrafficEvent.window_key],
            set_={"count": RuntimeTrafficEvent.count + 1},
        ).returning(RuntimeTrafficEvent.count)
        count = self._session.execute(statement).scalar_one()
        if count == self._threshold and self._audit is not None:
            self._audit.append(RuntimeAuditEvent(
                audit_id=str(uuid4()), actor_type="system", actor_id="runtime",
                action="runtime_traffic_threshold_crossed", resource_type="model_route",
                resource_id=route_id, reason_code=result_code, outcome="threshold_crossed",
                occurred_at=timestamp, metadata_summary={"decision": "traffic_threshold_crossed"},
            ))


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _safe_identifier(value: str, maximum_length: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= maximum_length and all(
        character.isalnum() or character in "._:-" for character in value
    )
