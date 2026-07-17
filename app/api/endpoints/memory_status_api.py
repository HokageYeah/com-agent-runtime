"""回忆录生成状态的业务服务查询接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.core.security import SignatureError, verify_signature
from app.services.memory_generation_status_service import MemoryGenerationStatusService

router = APIRouter(prefix="/memory-archives", tags=["memory-generation-status"])


@router.get("/{archive_id}/generation-status")
async def get_generation_status(archive_id: str, request: Request) -> dict[str, object]:
    """供已验签业务服务查询生成状态；前端应经业务认证层调用，禁止直接暴露私密内容。"""
    body = await request.body()
    try:
        verify_signature(
            {key.lower(): value for key, value in request.headers.items()},
            request.method,
            request.url.path,
            body,
            request.app.state.settings.trusted_clients,
            request.app.state.settings.signature_tolerance_seconds,
        )
        session = request.app.state.session_factory()
        try:
            output = MemoryGenerationStatusService(session).get(archive_id)
        finally:
            session.close()
        return {"output": output, "schema_version": "1.0.0"}
    except SignatureError as exc:
        raise HTTPException(status_code=401, detail="invalid service signature") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="memory archive unavailable") from exc
