"""回忆录业务侧 callback 状态投影，archive 发布状态仍由发布工具独占。"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memory_agent_run_ref import MemoryAgentRunRef
from app.models.memory_archive import MemoryArchive


class MemoryAgentCallbackService:
    """将已验签的 Runtime 安全事件幂等投影到业务 Run 引用。"""

    _STATUS_BY_EVENT = {
        "run_started": "running",
        "step_changed": "running",
        "waiting_human": "waiting_human",
        "run_failed": "failed",
        "run_cancelled": "cancelled",
        "run_succeeded": "succeeded",
        "partial_succeeded": "partial",
    }
    # 发布前的生成状态可由 callback 展示；发布后 archive 状态只能由原子发布工具维护。
    _ARCHIVE_STATUS_BY_EVENT = {
        "run_started": "running",
        "step_changed": "running",
        "waiting_human": "waiting_human",
        "run_failed": "failed",
        "run_cancelled": "cancelled",
    }

    def __init__(self, session: Session) -> None:
        self._session = session

    def apply(self, payload: dict[str, object]) -> bool:
        """按 event_seq/status_version 前进 RunRef；过期事件只记录安全日志。"""
        run_id, archive_id = payload["run_id"], payload["business_id"]
        event, event_seq, status_version = payload["event"], payload["event_seq"], payload["status_version"]
        if not all((isinstance(run_id, str), isinstance(archive_id, str), isinstance(event, str), isinstance(event_seq, int), isinstance(status_version, int))):
            raise ValueError("MEMORY_CALLBACK_PAYLOAD_INVALID")
        expected_status = self._STATUS_BY_EVENT.get(event)
        if expected_status is None or payload.get("status") != expected_status:
            raise ValueError("MEMORY_CALLBACK_EVENT_INVALID")
        public_trace = self._safe_public_trace(payload.get("public_trace", []))
        ref = self._session.scalar(select(MemoryAgentRunRef).where(MemoryAgentRunRef.run_id == run_id).with_for_update())
        if ref is None or ref.archive_id != archive_id:
            raise ValueError("MEMORY_CALLBACK_RUN_UNAVAILABLE")
        archive = self._session.scalar(select(MemoryArchive).where(MemoryArchive.archive_id == archive_id).with_for_update())
        if archive is None or archive.active_run_id != run_id or archive.generation_epoch != ref.generation_epoch:
            raise ValueError("MEMORY_CALLBACK_RUN_NOT_ACTIVE")
        if event_seq <= ref.event_seq or status_version < ref.status_version:
            logging.info("回忆录 callback 已过期 run_id=%s event_seq=%s", run_id, event_seq)
            return False
        if event in {"run_succeeded", "partial_succeeded"} and (
            archive.published_revision <= 0 or archive.content_status != "succeeded"
        ):
            # 终态 callback 只能确认既有发布，缺失 revision 时留给对账任务处理。
            ref.reconciliation_status = "needed"
            logging.warning("回忆录成功 callback 等待发布对账 run_id=%s", run_id)
            return False
        ref.status, ref.event_seq, ref.status_version = expected_status, event_seq, status_version
        # callback 只在通过 event/status 版本校验后推进业务侧乐观锁版本。
        ref.row_version += 1
        ref.reconciliation_status = "not_needed"
        ref.public_trace_json = public_trace
        archive_status = self._ARCHIVE_STATUS_BY_EVENT.get(event)
        if archive_status is not None and archive.published_revision == 0:
            archive.content_status = archive_status
            logging.info("回忆录 callback 更新未发布归档状态 archive_id=%s status=%s", archive_id, archive_status)
        logging.info("回忆录 callback 已应用 run_id=%s event=%s event_seq=%s", run_id, event, event_seq)
        return True

    @staticmethod
    def _safe_public_trace(value: object) -> list[dict[str, str]]:
        """白名单化前端轨迹字段，拒绝把模型或素材内容透传进业务状态。"""
        if not isinstance(value, list) or len(value) > 8:
            raise ValueError("MEMORY_CALLBACK_TRACE_INVALID")
        trace: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("step"), str) or not isinstance(item.get("status"), str):
                raise ValueError("MEMORY_CALLBACK_TRACE_INVALID")
            safe_item = {"step": item["step"], "status": item["status"]}
            if isinstance(item.get("label"), str):
                safe_item["label"] = item["label"]
            trace.append(safe_item)
        return trace
