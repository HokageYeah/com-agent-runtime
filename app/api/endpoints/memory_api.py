"""回忆录面向用户的最小 API；所有归档读取都以已验签 owner 为边界。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.api_response import build_api_response_from_request
from app.core.user_auth import (
    UserIdentity,
    get_current_user_id,
    get_current_user_identity,
)
from app.models.memory_action import MemoryAction
from app.models.memory_agent_run_ref import MemoryAgentRunRef
from app.models.memory_archive import MemoryArchive
from app.models.memory_media_asset import MemoryMediaAsset
from app.models.memory_scene import MemoryScene
from app.services.memory_agent_adapter import MemoryAgentAdapter
from app.services.memory_deletion_compensation_service import (
    MemoryDeletionCompensationService,
)
from app.services.memory_generation_status_service import MemoryGenerationStatusService
from app.services.memory_password_service import (
    MemoryPasswordError,
    MemoryPasswordService,
)
from app.services.memory_player_service import MemoryPlayerService
from app.services.memory_s3_media_proxy import MemoryS3MediaProxyConfigError

router = APIRouter(prefix="/memory", tags=["memory"])


class PasswordPayload(BaseModel):
    """密码请求体；日志与响应均不回显该字段。"""

    password: str


class MemoryMediaAccessGateway(Protocol):
    """对象存储适配器的最小签发能力；URL 只能短期存在于本次响应。"""

    # 部署固定有效期，接口调用方不可自行增大。
    expires_seconds: int

    def create_access_url(self, storage_key: str, *, expires_seconds: int) -> str:
        """为已授权的私有对象签发短 TTL URL，禁止持久化或记录 URL。"""


@router.post("/password/setup")
def setup_password(request: Request, payload: PasswordPayload, identity: UserIdentity = Depends(get_current_user_identity)) -> JSONResponse:
    """为当前用户首次设置独立回忆录密码。"""
    session = request.app.state.session_factory()
    try:
        MemoryPasswordService(session).setup(identity.user_id, payload.password)
        session.commit()
    except MemoryPasswordError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    finally:
        session.close()
    return _private_response(request, {"configured": True}, "SUCCESS::回忆录密码设置成功")


@router.post("/password/verify")
def verify_password(request: Request, payload: PasswordPayload, identity: UserIdentity = Depends(get_current_user_identity)) -> JSONResponse:
    """验证 PIN 并返回仅限当前 jti 的短期解锁凭证。"""
    session = request.app.state.session_factory()
    try:
        credential = MemoryPasswordService(session).verify(
            identity.user_id, payload.password, identity.session_id, now=datetime.now(UTC)
        )
        session.commit()
    except MemoryPasswordError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    finally:
        session.close()
    return _private_response(request, {"credential": credential}, "SUCCESS::回忆录密码验证成功")


@router.get("/archives")
def list_archives(
    request: Request,
    user_id: int = Depends(get_current_user_id),
) -> JSONResponse:
    """返回当前 owner 的最小归档列表；解锁前不返回摘要或播放内容。"""
    session = request.app.state.session_factory()
    try:
        archives = session.scalars(
            select(MemoryArchive)
            .where(MemoryArchive.owner_user_id == user_id, MemoryArchive.deleted_at.is_(None))
            .order_by(MemoryArchive.is_pinned.desc(), MemoryArchive.created_at.desc())
        ).all()
        payload = {
            "archives": [
                {
                    "archive_id": archive.archive_id,
                    "content_status": archive.content_status,
                    "enhancement_status": archive.enhancement_status,
                    # generation_status 是读取派生字段，避免成为第二个可竞争写入状态。
                    "generation_status": _generation_status(archive),
                    "generation_epoch": archive.generation_epoch,
                    # 当前 archive 创建时即关系段冻结完成时间；不返回关系或对方资料。
                    "unbound_at": archive.created_at.isoformat() if archive.created_at else None,
                    "is_pinned": archive.is_pinned,
                }
                for archive in archives
            ]
        }
        logging.info("查询回忆录最小列表 owner_user_id=%s count=%s", user_id, len(archives))
        return JSONResponse(content=build_api_response_from_request(
            request, data=payload, ret=["SUCCESS::获取回忆录列表成功"]
        ).model_dump(mode="json"))
    finally:
        session.close()


@router.get("/archives/{archive_id}")
def get_archive_detail(
    archive_id: str,
    request: Request,
    identity: UserIdentity = Depends(get_current_user_identity),
    unlock_credential: str | None = Header(default=None, alias="X-Memory-Unlock"),
) -> JSONResponse:
    """读取当前 owner 的唯一已发布版本；完整作品绝不写入日志或缓存。"""
    session = request.app.state.session_factory()
    try:
        archive = _require_owned_unlocked_archive(
            session, archive_id, identity, unlock_credential,
        )
        playback = MemoryPlayerService(session).get_published_playback(archive.archive_id)
        payload = {
            "archive": _archive_detail_summary(archive),
            "playback": {
                "revision": playback.document.revision,
                "schema_major": playback.document.schema_major,
                # 文档本身是已发布作品，返回给通过密码验证的 owner；不记录它。
                "document": playback.document.document_json,
                "scenes": [_scene_dto(scene) for scene in playback.scenes],
                "actions": [_action_dto(action) for action in playback.actions],
                # storage_key 是服务端私密定位符；客户端仅拿到后续鉴权媒体接口路径。
                "media": [_media_reference(archive.archive_id, item) for item in playback.media_assets],
            },
        }
        logging.info("读取回忆录详情 archive_id=%s owner_user_id=%s", archive_id, identity.user_id)
        return _private_response(request, payload, "SUCCESS::获取回忆录详情成功")
    finally:
        session.close()


@router.get("/archives/{archive_id}/generation")
def get_archive_generation(
    archive_id: str,
    request: Request,
    identity: UserIdentity = Depends(get_current_user_identity),
    unlock_credential: str | None = Header(default=None, alias="X-Memory-Unlock"),
) -> JSONResponse:
    """以用户 owner 与解锁边界读取安全生成摘要，不暴露内部 HMAC 接口。"""
    session = request.app.state.session_factory()
    try:
        _require_owned_unlocked_archive(session, archive_id, identity, unlock_credential)
        payload = MemoryGenerationStatusService(session).get(archive_id)
        logging.info("读取回忆录生成状态 archive_id=%s owner_user_id=%s", archive_id, identity.user_id)
        return _private_response(request, payload, "SUCCESS::获取回忆录生成状态成功")
    finally:
        session.close()


@router.delete("/archives/{archive_id}")
def delete_archive(
    archive_id: str,
    request: Request,
    identity: UserIdentity = Depends(get_current_user_identity),
    unlock_credential: str | None = Header(default=None, alias="X-Memory-Unlock"),
) -> JSONResponse:
    """撤销当前 owner 的 archive，并登记 Task 10.5 的幂等隐私清理补偿。"""
    session = request.app.state.session_factory()
    try:
        _require_owned_unlocked_archive(
            session, archive_id, identity, unlock_credential, allow_deleted=True,
        )
        created_events = MemoryDeletionCompensationService(
            # 请求只提交撤权与补偿意图；外部 Runtime 由对账调度器异步投递。
            session, None,
        ).request_archive_privacy_purge(archive_id)
        session.commit()
        logging.warning(
            "用户请求删除回忆录 archive_id=%s owner_user_id=%s purge_events=%s",
            archive_id, identity.user_id, created_events,
        )
        return _private_response(
            request, {"archive_id": archive_id, "deleted": True}, "SUCCESS::回忆录删除请求已受理",
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/archives/{archive_id}/pin")
def pin_archive(
    archive_id: str,
    request: Request,
    identity: UserIdentity = Depends(get_current_user_identity),
    unlock_credential: str | None = Header(default=None, alias="X-Memory-Unlock"),
) -> JSONResponse:
    """置顶一条归档；同一 owner 的其它未删除归档自动取消置顶。"""
    session = request.app.state.session_factory()
    try:
        archive = _require_owned_unlocked_archive(session, archive_id, identity, unlock_credential)
        session.execute(update(MemoryArchive).where(
            MemoryArchive.owner_user_id == identity.user_id,
            MemoryArchive.deleted_at.is_(None),
        ).values(is_pinned=False))
        archive.is_pinned = True
        session.commit()
        logging.info("置顶回忆录 archive_id=%s owner_user_id=%s", archive_id, identity.user_id)
        return _private_response(request, {"archive_id": archive_id, "is_pinned": True}, "SUCCESS::回忆录已置顶")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/archives/{archive_id}/unpin")
def unpin_archive(
    archive_id: str,
    request: Request,
    identity: UserIdentity = Depends(get_current_user_identity),
    unlock_credential: str | None = Header(default=None, alias="X-Memory-Unlock"),
) -> JSONResponse:
    """取消当前 owner 的归档置顶，不影响其他归档。"""
    session = request.app.state.session_factory()
    try:
        archive = _require_owned_unlocked_archive(session, archive_id, identity, unlock_credential)
        archive.is_pinned = False
        session.commit()
        logging.info("取消置顶回忆录 archive_id=%s owner_user_id=%s", archive_id, identity.user_id)
        return _private_response(request, {"archive_id": archive_id, "is_pinned": False}, "SUCCESS::回忆录已取消置顶")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/archives/{archive_id}/retry")
def retry_archive_generation(
    archive_id: str,
    request: Request,
    identity: UserIdentity = Depends(get_current_user_identity),
    unlock_credential: str | None = Header(default=None, alias="X-Memory-Unlock"),
) -> JSONResponse:
    """仅将失败/部分成功的当前 Run 交给 Runtime 以原幂等键执行 checkpoint retry。"""
    session = request.app.state.session_factory()
    try:
        archive = _require_owned_unlocked_archive(session, archive_id, identity, unlock_credential)
        run_ref = session.scalar(select(MemoryAgentRunRef).where(
            MemoryAgentRunRef.archive_id == archive.archive_id,
            MemoryAgentRunRef.status.in_(("failed", "partial")),
            MemoryAgentRunRef.purge_state == "active",
        ).order_by(MemoryAgentRunRef.updated_at.desc()))
        if run_ref is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="MEMORY_RETRY_NOT_AVAILABLE")
        if not run_ref.retry_idempotency_key:
            run_ref.retry_idempotency_key = f"memory-retry:{archive.archive_id}:{run_ref.run_id}"
        _runtime_gateway(request).retry_run(run_ref.run_id, run_ref.retry_idempotency_key)
        session.commit()
        logging.info("请求回忆录重试 archive_id=%s run_id=%s", archive_id, run_ref.run_id)
        return _private_response(request, {"archive_id": archive_id, "retry_requested": True}, "SUCCESS::回忆录重试请求已受理")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.get("/archives/{archive_id}/media/{asset_id}")
def get_private_media_access(
    archive_id: str,
    asset_id: str,
    request: Request,
    identity: UserIdentity = Depends(get_current_user_identity),
    unlock_credential: str | None = Header(default=None, alias="X-Memory-Unlock"),
) -> JSONResponse:
    """仅为当前 published revision 的 ready 资产签发短期私有访问地址。"""
    session = request.app.state.session_factory()
    try:
        archive = _require_owned_unlocked_archive(session, archive_id, identity, unlock_credential)
        playback = MemoryPlayerService(session).get_published_playback(archive.archive_id)
        asset = session.scalar(select(MemoryMediaAsset).where(
            MemoryMediaAsset.asset_id == asset_id,
            MemoryMediaAsset.archive_id == archive.archive_id,
            MemoryMediaAsset.document_id == playback.document.document_id,
            MemoryMediaAsset.status == "ready",
        ))
        if asset is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MEMORY_MEDIA_UNAVAILABLE")
        gateway = getattr(request.app.state, "memory_media_proxy", None)
        if gateway is None:
            # 未接入对象存储时 fail closed，绝不能退化为返回 storage_key。
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MEMORY_MEDIA_PROXY_UNAVAILABLE")
        expires_seconds = getattr(gateway, "expires_seconds", 60)
        if not isinstance(expires_seconds, int) or not 1 <= expires_seconds <= 300:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MEMORY_MEDIA_PROXY_UNAVAILABLE")
        try:
            access_url = gateway.create_access_url(
                asset.storage_key, expires_seconds=expires_seconds,
            )
        except MemoryS3MediaProxyConfigError as exc:
            logging.warning(
                "签发回忆录私有媒体失败 archive_id=%s asset_id=%s code=%s",
                archive_id, asset_id, str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="MEMORY_MEDIA_PROXY_UNAVAILABLE",
            ) from exc
        if not isinstance(access_url, str) or not access_url.startswith(("https://", "http://")):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MEMORY_MEDIA_PROXY_UNAVAILABLE")
        logging.info("签发回忆录私有媒体访问 archive_id=%s asset_id=%s", archive_id, asset_id)
        return _private_response(
            request,
            {"asset_id": asset.asset_id, "media_type": asset.media_type, "access_url": access_url, "expires_in_seconds": expires_seconds},
            "SUCCESS::获取私有媒体访问地址成功",
        )
    finally:
        session.close()


def _private_response(request: Request, data: dict[str, object], ret: str) -> JSONResponse:
    """密码和解锁相关响应禁止被浏览器、代理或客户端缓存。"""
    return JSONResponse(
        content=build_api_response_from_request(request, data=data, ret=[ret]).model_dump(mode="json"),
        headers={"Cache-Control": "private, no-store"},
    )


def _require_owned_unlocked_archive(
    session: Session,
    archive_id: str,
    identity: UserIdentity,
    unlock_credential: str | None,
    *,
    allow_deleted: bool = False,
) -> MemoryArchive:
    """统一执行 owner 与同会话解锁校验；删除重放可安全读取既有 tombstone。"""
    conditions = [
        MemoryArchive.archive_id == archive_id,
        MemoryArchive.owner_user_id == identity.user_id,
    ]
    if not allow_deleted:
        conditions.append(MemoryArchive.deleted_at.is_(None))
    archive = session.scalar(select(MemoryArchive).where(*conditions))
    if archive is None:
        # 不透露归档是否属于其他用户，避免 resource enumeration。
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MEMORY_ARCHIVE_UNAVAILABLE")
    if not unlock_credential:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="MEMORY_UNLOCK_REQUIRED")
    try:
        unlocked = MemoryPasswordService(session).is_unlocked(
            identity.user_id, identity.session_id, unlock_credential, now=datetime.now(UTC),
        )
    except MemoryPasswordError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="MEMORY_UNLOCK_REQUIRED") from exc
    if not unlocked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="MEMORY_UNLOCK_REQUIRED")
    return archive


def _runtime_gateway(request: Request) -> MemoryAgentAdapter:
    """读取应用生命周期注入的窄 gateway；未配置时 fail closed。"""
    gateway = getattr(request.app.state, "memory_runtime_gateway", None)
    if gateway is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MEMORY_RUNTIME_GATEWAY_UNAVAILABLE",
        )
    return gateway


def _archive_detail_summary(archive: MemoryArchive) -> dict[str, object]:
    """详情的 archive 摘要只包含当前 owner 可见的状态和版本字段。"""
    return {
        "archive_id": archive.archive_id,
        "content_status": archive.content_status,
        "enhancement_status": archive.enhancement_status,
        "generation_epoch": archive.generation_epoch,
        "published_revision": archive.published_revision,
        "is_pinned": archive.is_pinned,
    }


def _generation_status(archive: MemoryArchive) -> str:
    """从 archive 的既有权威状态派生列表展示状态，不新增持久化状态机。"""
    terminal_or_active = {"running", "failed", "partial", "cancelled"}
    if archive.content_status in terminal_or_active:
        return archive.content_status
    return archive.enhancement_status if archive.enhancement_status != "not_started" else archive.content_status


def _scene_dto(scene: MemoryScene) -> dict[str, object]:
    """构建已发布场景 DTO；内容仅进入已解锁 owner 的 no-store 响应。"""
    return {
        "scene_id": scene.scene_id, "order": scene.scene_order,
        "schema_major": scene.schema_major, "scene_type": scene.scene_type,
        "safety_level": scene.safety_level, "payload": scene.payload_json,
        "source_refs": scene.source_refs_json,
    }


def _action_dto(action: MemoryAction) -> dict[str, object]:
    """构建动作 DTO，保持同一 published document 的排序与 schema 标识。"""
    return {
        "action_id": action.action_id, "scene_id": action.scene_id,
        "order": action.action_order, "schema_major": action.schema_major,
        "action_type": action.action_type, "duration_ms": action.duration_ms,
        "payload": action.payload_json,
    }


def _media_reference(archive_id: str, asset: MemoryMediaAsset) -> dict[str, object]:
    """详情只给出需再次鉴权的媒体路径，禁止泄露私有 storage key。"""
    return {
        "asset_id": asset.asset_id, "media_type": asset.media_type,
        "source_type": asset.source_type, "status": asset.status,
        "access_path": f"/api/v1/memory/archives/{archive_id}/media/{asset.asset_id}",
    }
