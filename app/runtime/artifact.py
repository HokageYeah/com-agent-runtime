"""Runtime 产物仓储：只保存可审计摘要，不复制业务作品正文。"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import AgentArtifact, AgentRun
from app.runtime.interfaces import LeaseContext
from app.services.lease_service import LeaseService


class ArtifactError(RuntimeError):
    """产物不能在当前运行边界内安全持久化时抛出。"""


class ArtifactStore:
    """保存节点产物的最小可追溯引用，禁止持久化节点原始结果。"""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._lease = LeaseService(session)

    def save_node_result(
        self,
        run: AgentRun,
        node_id: str,
        result: Mapping[str, object],
        context: LeaseContext,
    ) -> str:
        """记录节点完成事实，摘要只保留节点名与结果字段名。"""
        if not self._lease.can_write(run.run_id, context):
            raise ArtifactError("当前 Worker 无权写入 Artifact")
        if not node_id:
            raise ArtifactError("节点标识不能为空")

        # 结果值可能是私密快照、模型原文或工具响应，只允许留下字段名用于排障。
        result_keys = sorted(key for key in result if isinstance(key, str))
        summary = {"node_id": node_id, "result_keys": result_keys}
        digest = hashlib.sha256(
            json.dumps(
                summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        artifact = AgentArtifact(
            artifact_id=str(uuid4()),
            run_id=run.run_id,
            artifact_type="workflow_node_result",
            schema_version="1.0.0",
            content_digest=digest,
            summary_json=summary,
            business_resource_ref=f"business://{run.business_type}/{run.business_id}",
            created_at=datetime.now(UTC),
        )
        self._session.add(artifact)
        self._session.flush()
        logging.info(
            "已写入节点 Artifact run_id=%s node_id=%s artifact_id=%s digest=%s",
            run.run_id,
            node_id,
            artifact.artifact_id,
            digest[:12],
        )
        return artifact.artifact_id
