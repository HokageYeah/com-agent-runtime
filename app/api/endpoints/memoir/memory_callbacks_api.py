"""情侣日记业务侧接收 Runtime callback 的内部接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from app.core.security import request_hash
from app.core.tool_security import verify_runtime_tool
from app.services.idempotency_service import IdempotencyConflict, IdempotencyService
from app.services.memoir.memory_agent_callback_service import MemoryAgentCallbackService

router = APIRouter(prefix="/internal/agent-callbacks", tags=["memory-internal-callbacks"])


@router.post("/memory")
async def receive_memory_callback(request: Request) -> dict[str, object]:
    """验签并幂等接收 callback；只投影 RunRef，不改写回忆录发布版本。"""
    body = await request.body()
    try:
        headers = {key.lower(): value for key, value in request.headers.items()}
        runtime_id = verify_runtime_tool(headers, request.method, request.url.path, body, request.app.state.settings.memory_tool_runtimes, request.app.state.settings.signature_tolerance_seconds)
        payload = await request.json()
        _validate_callback_headers(headers, payload)
        event_id, run_id = payload["event_id"], payload["run_id"]
        if not isinstance(event_id, str) or not isinstance(run_id, str):
            raise ValueError("MEMORY_CALLBACK_PAYLOAD_INVALID")
        key = request.headers.get("Idempotency-Key")
        if key != f"callback:{event_id}":
            raise ValueError("MEMORY_CALLBACK_IDEMPOTENCY_INVALID")
        session = request.app.state.session_factory()
        try:
            idempotency = IdempotencyService(session)
            digest = request_hash(request.method, request.url.path, body)
            replay = idempotency.replay(runtime_id, f"memory_callback:{run_id}", key, digest)
            if replay is not None:
                return {"output": replay, "schema_version": "1.0.0"}
            output = {"applied": MemoryAgentCallbackService(session).apply(payload)}
            idempotency.store(runtime_id, f"memory_callback:{run_id}", key, digest, output, run_id)
            session.commit()
        finally:
            session.close()
        return {"output": output, "schema_version": "1.0.0"}
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="IDEMPOTENCY_CONFLICT") from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="memory callback rejected") from exc


def _validate_callback_headers(headers: dict[str, str], payload: object) -> None:
    """防止有效 Runtime 身份把一个事件投影到另一条 Run 或归档。"""
    if not isinstance(payload, dict):
        raise ValueError("MEMORY_CALLBACK_PAYLOAD_INVALID")
    expected = {
        "x-agent-run-id": payload.get("run_id"),
        "x-agent-business-id": payload.get("business_id"),
        "x-agent-event-id": payload.get("event_id"),
        "x-agent-event-seq": str(payload.get("event_seq")),
    }
    if any(not isinstance(value, str) or headers.get(key) != value for key, value in expected.items()):
        raise ValueError("MEMORY_CALLBACK_HEADER_MISMATCH")
