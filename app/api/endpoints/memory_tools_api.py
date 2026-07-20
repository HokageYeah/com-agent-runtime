"""仅供 AgentRuntime 调用的回忆录内部工具接口。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from app.core.security import request_hash
from app.core.tool_security import verify_runtime_tool
from app.services.idempotency_service import IdempotencyConflict, IdempotencyService
from app.services.memory_archive_service import MemoryArchiveService
from app.services.memory_snapshot_service import MemorySnapshotService

router = APIRouter(prefix="/internal/agent-tools", tags=["memory-internal-tools"])


@router.post("/memory.get_snapshot")
async def get_snapshot(request: Request) -> dict[str, object]:
    body = await request.body()
    try:
        verify_runtime_tool({k.lower(): v for k, v in request.headers.items()}, request.method, request.url.path, body, request.app.state.settings.memory_tool_runtimes, request.app.state.settings.signature_tolerance_seconds)
        payload = await request.json()
        input_data = payload["input"]
        session = request.app.state.session_factory()
        try:
            snapshot = MemorySnapshotService(
                session, request.app.state.memory_snapshot_cipher,
            ).read_for_runtime(
                input_data["archive_id"], input_data["snapshot_id"],
                input_data["run_id"], input_data["generation_epoch"],
            )
        finally:
            session.close()
        return {"output": snapshot, "schema_version": "1.0.0"}
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="memory snapshot unavailable") from exc


@router.post("/memory.publish_playback_document")
async def publish_playback_document(request: Request) -> dict[str, object]:
    """仅当前 active Run 可原子发布完整作品，正文不写日志。"""
    body = await request.body()
    try:
        runtime_id = verify_runtime_tool({k.lower(): v for k, v in request.headers.items()}, request.method, request.url.path, body, request.app.state.settings.memory_tool_runtimes, request.app.state.settings.signature_tolerance_seconds)
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            raise HTTPException(status_code=400, detail="Idempotency-Key required")
        input_data = (await request.json())["input"]
        session = request.app.state.session_factory()
        try:
            digest = request_hash(request.method, request.url.path, body)
            idempotency = IdempotencyService(session)
            scope = f"memory_publish:{input_data['run_id']}"
            replay = idempotency.replay(runtime_id, scope, idempotency_key, digest)
            if replay is not None:
                return {"output": replay, "schema_version": "1.0.0"}
            # 发布与读取使用同一四元组授权，避免 active Run 错把其他归档或旧快照结果发布出去。
            snapshot_service = MemorySnapshotService(
                session, request.app.state.memory_snapshot_cipher,
            )
            snapshot = snapshot_service.authorize_runtime(
                input_data["archive_id"], input_data["snapshot_id"],
                input_data["run_id"], input_data["generation_epoch"],
            )
            snapshot_service.validate_document_references(
                input_data["document"], snapshot,
            )
            document = MemoryArchiveService(session, request.app.state.memory_snapshot_cipher).publish_playback_document(
                input_data["archive_id"], expected_generation_epoch=input_data["generation_epoch"],
                expected_run_id=input_data["run_id"], document=input_data["document"],
            )
            output = {"revision": document.revision, "content_digest": document.content_digest}
            idempotency.store(runtime_id, scope, idempotency_key, digest, output, input_data["archive_id"])
            session.commit()
        finally:
            session.close()
        return {"output": output, "schema_version": "1.0.0"}
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="IDEMPOTENCY_CONFLICT") from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="memory publication rejected") from exc


@router.post("/memory.get_publish_result")
async def get_publish_result(request: Request) -> dict[str, object]:
    """查询未知写工具结果；只返回安全 revision/digest，不返回作品正文。"""
    body = await request.body()
    try:
        runtime_id = verify_runtime_tool({k.lower(): v for k, v in request.headers.items()}, request.method, request.url.path, body, request.app.state.settings.memory_tool_runtimes, request.app.state.settings.signature_tolerance_seconds)
        key = request.headers.get("Idempotency-Key")
        input_data = (await request.json())["input"]
        if not key:
            raise HTTPException(status_code=400, detail="Idempotency-Key required")
        session = request.app.state.session_factory()
        try:
            output = IdempotencyService(session).lookup_result(runtime_id, f"memory_publish:{input_data['run_id']}", key, input_data["archive_id"])
        finally:
            session.close()
        if output is None:
            raise HTTPException(status_code=404, detail="publish result unavailable")
        return {"output": output, "schema_version": "1.0.0"}
    except KeyError as exc:
        raise HTTPException(status_code=403, detail="publish result rejected") from exc
