"""S3 兼容私有媒体短期签名适配器测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.memory_s3_media_proxy import (
    MemoryS3MediaProxy,
    MemoryS3MediaProxyConfigError,
)


class _RecordingS3Client:
    """记录 SDK 参数的最小替身，不请求真实对象存储。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], int]] = []

    def generate_presigned_url(
        self, operation: str, *, Params: dict[str, object], ExpiresIn: int,
    ) -> str:
        self.calls.append((operation, Params, ExpiresIn))
        return "https://storage.example/private-object?X-Amz-Signature=secret"


def test_s3_media_proxy_generates_short_get_object_url_without_exposing_key() -> None:
    """只能对受控 bucket/key 签发短期 get_object 地址，调用方不持久化 URL。"""
    client = _RecordingS3Client()
    proxy = MemoryS3MediaProxy(client, bucket="private-memoirs", expires_seconds=60)

    url = proxy.create_access_url("memoirs/a-1/image.png", expires_seconds=60)

    assert url.startswith("https://storage.example/")
    assert client.calls == [
        ("get_object", {"Bucket": "private-memoirs", "Key": "memoirs/a-1/image.png"}, 60),
    ]


def test_s3_media_proxy_rejects_path_escape_or_ttl_override() -> None:
    """storage key 不可逃逸对象前缀，调用方也不可把 URL 有效期扩大。"""
    proxy = MemoryS3MediaProxy(_RecordingS3Client(), bucket="private-memoirs", expires_seconds=60)

    with pytest.raises(MemoryS3MediaProxyConfigError, match="MEMORY_MEDIA_STORAGE_KEY_INVALID"):
        proxy.create_access_url("../public/image.png", expires_seconds=60)
    with pytest.raises(MemoryS3MediaProxyConfigError, match="MEMORY_MEDIA_TTL_INVALID"):
        proxy.create_access_url("memoirs/a-1/image.png", expires_seconds=61)


def test_s3_media_proxy_builds_real_boto3_client_from_complete_settings() -> None:
    """完整部署配置实际创建 boto3 S3 client，但签名过程不发起对象存储网络请求。"""
    settings = SimpleNamespace(
        MEMORY_MEDIA_S3_ENDPOINT_URL="https://s3.example.test",
        MEMORY_MEDIA_S3_BUCKET="private-memoirs",
        MEMORY_MEDIA_S3_REGION="us-east-1",
        MEMORY_MEDIA_S3_ACCESS_KEY_ID="test-access-key",
        MEMORY_MEDIA_S3_SECRET_ACCESS_KEY="test-secret-key",
        MEMORY_MEDIA_SIGNED_URL_TTL_SECONDS=60,
    )

    proxy = MemoryS3MediaProxy.from_settings(settings)

    assert proxy is not None
    assert proxy.create_access_url("memoirs/a-1/image.png", expires_seconds=60).startswith(
        "https://s3.example.test/",
    )


def test_s3_media_proxy_rejects_http_endpoint_in_production() -> None:
    """生产环境不能因 MinIO 本地开发便利而把签名媒体降级到 HTTP。"""
    settings = SimpleNamespace(
        ENVIRONMENT="production",
        MEMORY_MEDIA_S3_ENDPOINT_URL="http://s3.example.test",
        MEMORY_MEDIA_S3_BUCKET="private-memoirs",
        MEMORY_MEDIA_S3_REGION="us-east-1",
        MEMORY_MEDIA_S3_ACCESS_KEY_ID="test-access-key",
        MEMORY_MEDIA_S3_SECRET_ACCESS_KEY="test-secret-key",
        MEMORY_MEDIA_SIGNED_URL_TTL_SECONDS=60,
    )

    with pytest.raises(MemoryS3MediaProxyConfigError, match="MEMORY_MEDIA_STORAGE_CONFIG_INVALID"):
        MemoryS3MediaProxy.from_settings(settings)
