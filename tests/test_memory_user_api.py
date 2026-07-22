"""回忆录用户 API 的 owner 与最小列表字段测试。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.user_auth import (
    UserIdentity,
    get_current_user_id,
    get_current_user_identity,
)
from app.db.sqlalchemy_db import Base
from app.main import app
from app.models.memory_agent_run_ref import MemoryAgentRunRef
from app.models.memory_media_asset import MemoryMediaAsset
from app.models.memory_playback_document import MemoryPlaybackDocument
from app.services.memory_archive_service import FrozenMemoryInput, MemoryArchiveService
from app.services.memory_password_service import MemoryPasswordService
from app.services.memory_s3_media_proxy import MemoryS3MediaProxyConfigError


def test_archive_list_returns_only_current_owner_minimal_fields(client) -> None:
    """未解锁列表不能泄漏摘要或对方信息，且不能读取其他 owner 的 archive。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    archives = MemoryArchiveService(session, app.state.memory_snapshot_cipher).create_archives_for_relationship(
        FrozenMemoryInput(1, "space", 1, (1, 2), {}, datetime(2026, 7, 22, tzinfo=UTC), {}, {}, "v1")
    )
    session.commit()
    app.state.session_factory = factory
    app.dependency_overrides[get_current_user_id] = lambda: 1
    try:
        response = client.get("/api/v1/memory/archives")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    records = response.json()["data"]["archives"]
    assert [item["archive_id"] for item in records] == [archives[0].archive_id]
    assert set(records[0]) == {
        "archive_id", "content_status", "enhancement_status", "generation_status",
        "generation_epoch", "unbound_at", "is_pinned",
    }


def test_password_setup_and_verify_return_session_bound_private_credential(client) -> None:
    """密码接口不回显 PIN，成功解锁仅返回短期凭证且禁止缓存。"""
    from app.core.user_auth import UserIdentity, get_current_user_identity

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    app.state.session_factory = sessionmaker(bind=engine)
    app.dependency_overrides[get_current_user_identity] = lambda: UserIdentity(1, "session-1")
    try:
        setup = client.post("/api/v1/memory/password/setup", json={"password": "1234"})
        verified = client.post("/api/v1/memory/password/verify", json={"password": "1234"})
    finally:
        app.dependency_overrides.clear()

    assert setup.status_code == 200
    assert verified.status_code == 200
    assert "credential" in verified.json()["data"]
    assert set(verified.json()["data"]) == {"credential"}
    assert verified.headers["cache-control"] == "private, no-store"


def _unlocked_owner(client) -> tuple[object, object, str]:
    """准备一条已解锁 owner 的归档，供用户侧敏感接口验证访问边界。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    archives = MemoryArchiveService(session, app.state.memory_snapshot_cipher).create_archives_for_relationship(
        FrozenMemoryInput(1, "space", 1, (1, 2), {}, datetime(2026, 7, 22, tzinfo=UTC), {}, {}, "v1")
    )
    password = MemoryPasswordService(session)
    password.setup(1, "1234")
    credential = password.verify(1, "1234", "session-1", now=datetime.now(UTC))
    session.commit()
    app.state.session_factory = factory
    app.dependency_overrides[get_current_user_identity] = lambda: UserIdentity(1, "session-1")
    return session, archives[0], credential


def test_archive_detail_requires_owner_and_session_bound_unlock_credential(client) -> None:
    """详情只返回当前 owner 的已发布版本，且缺少解锁凭证时必须拒绝。"""
    session, archive, credential = _unlocked_owner(client)
    try:
        denied = client.get(f"/api/v1/memory/archives/{archive.archive_id}")
        allowed = client.get(
            f"/api/v1/memory/archives/{archive.archive_id}",
            headers={"X-Memory-Unlock": credential},
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "private, no-store"
    assert allowed.json()["data"]["archive"]["archive_id"] == archive.archive_id
    assert "storage_key" not in str(allowed.json()["data"])


def test_archive_detail_hides_other_owner_archive_before_unlock_check(client) -> None:
    """跨 owner 请求统一返回不可用，避免凭密码状态枚举对方归档。"""
    session, archive, credential = _unlocked_owner(client)
    app.dependency_overrides[get_current_user_identity] = lambda: UserIdentity(2, "session-2")
    try:
        response = client.get(
            f"/api/v1/memory/archives/{archive.archive_id}",
            headers={"X-Memory-Unlock": credential},
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 404


def test_generation_and_delete_require_unlock_and_delete_reuses_privacy_compensation(client) -> None:
    """生成状态和删除必须同时校验 owner/解锁，删除后归档 tombstone 立即生效。"""
    session, archive, credential = _unlocked_owner(client)
    try:
        denied = client.get(f"/api/v1/memory/archives/{archive.archive_id}/generation")
        status_response = client.get(
            f"/api/v1/memory/archives/{archive.archive_id}/generation",
            headers={"X-Memory-Unlock": credential},
        )
        deleted = client.delete(
            f"/api/v1/memory/archives/{archive.archive_id}",
            headers={"X-Memory-Unlock": credential},
        )
        repeated = client.delete(
            f"/api/v1/memory/archives/{archive.archive_id}",
            headers={"X-Memory-Unlock": credential},
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert denied.status_code == 403
    assert status_response.status_code == 200
    assert status_response.headers["cache-control"] == "private, no-store"
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] is True
    assert repeated.status_code == 200
    assert repeated.json()["data"]["deleted"] is True
    session = app.state.session_factory()
    try:
        deleted_archive = session.scalar(
            select(type(archive)).where(type(archive).archive_id == archive.archive_id),
        )
    finally:
        session.close()
    assert deleted_archive is not None and deleted_archive.deleted_at is not None


def test_pin_retry_and_media_access_enforce_published_owner_bound_access(client) -> None:
    """置顶、重试与媒体票据均不能跨 owner 或绕过解锁；媒体不回传 storage key。"""
    from unittest.mock import Mock

    session, archive, credential = _unlocked_owner(client)
    ref = MemoryAgentRunRef(
        run_id="run-failed", archive_id=archive.archive_id, generation_epoch=0,
        status="failed", retry_idempotency_key="retry-key",
    )
    session.add(ref)
    playback = session.scalar(
        select(MemoryPlaybackDocument).where(
            MemoryPlaybackDocument.archive_id == archive.archive_id,
        )
    )
    assert playback is not None
    session.add(MemoryMediaAsset(
        asset_id="asset-1", archive_id=archive.archive_id, document_id=playback.document_id,
        media_type="image", source_type="default_asset", status="ready", storage_key="private/object-key",
    ))
    session.commit()
    runtime = Mock()
    original_runtime = app.state.memory_runtime_gateway
    app.state.memory_runtime_gateway = runtime
    app.state.memory_media_proxy = Mock(
        expires_seconds=60,
        create_access_url=Mock(return_value="https://storage.example/temp"),
    )
    try:
        pin = client.post(f"/api/v1/memory/archives/{archive.archive_id}/pin", headers={"X-Memory-Unlock": credential})
        retry = client.post(f"/api/v1/memory/archives/{archive.archive_id}/retry", headers={"X-Memory-Unlock": credential})
        media = client.get(f"/api/v1/memory/archives/{archive.archive_id}/media/asset-1", headers={"X-Memory-Unlock": credential})
    finally:
        app.dependency_overrides.clear()
        app.state.memory_runtime_gateway = original_runtime
        del app.state.memory_media_proxy
        session.close()

    assert pin.status_code == 200
    assert retry.status_code == 200
    assert media.status_code == 200
    assert media.headers["cache-control"] == "private, no-store"
    assert "storage_key" not in str(media.json()["data"])


def test_media_access_converts_storage_signing_failure_to_safe_service_error(client) -> None:
    """签名 SDK 失败不得泄露对象存储异常、对象 key 或签名参数。"""
    from unittest.mock import Mock

    session, archive, credential = _unlocked_owner(client)
    playback = session.scalar(
        select(MemoryPlaybackDocument).where(
            MemoryPlaybackDocument.archive_id == archive.archive_id,
        )
    )
    assert playback is not None
    session.add(MemoryMediaAsset(
        asset_id="asset-signing-failure", archive_id=archive.archive_id,
        document_id=playback.document_id, media_type="image", source_type="default_asset",
        status="ready", storage_key="private/object-key",
    ))
    session.commit()
    app.state.memory_media_proxy = Mock(
        expires_seconds=60,
        create_access_url=Mock(side_effect=MemoryS3MediaProxyConfigError("MEMORY_MEDIA_SIGNING_FAILED")),
    )
    try:
        response = client.get(
            f"/api/v1/memory/archives/{archive.archive_id}/media/asset-signing-failure",
            headers={"X-Memory-Unlock": credential},
        )
    finally:
        app.dependency_overrides.clear()
        del app.state.memory_media_proxy
        session.close()

    assert response.status_code == 503
    assert "private/object-key" not in response.text
