"""S3 兼容对象存储的回忆录私有媒体短期签名适配器。"""

from __future__ import annotations

import re
from typing import Protocol
from urllib.parse import urlparse


class MemoryS3MediaProxyConfigError(ValueError):
    """对象存储配置、key 或签名结果不安全时使用的固定错误码。"""


class S3PresignClient(Protocol):
    """boto3 S3 client 所需的最小接口，便于测试且不包装整个 SDK。"""

    def generate_presigned_url(
        self, operation: str, *, Params: dict[str, object], ExpiresIn: int,
    ) -> str:
        """生成受限操作的短期签名 URL。"""


class MemoryS3MediaProxy:
    """仅签发 S3 ``get_object`` 短期地址，不保存或记录对象 key/签名 URL。"""

    def __init__(
        self, client: S3PresignClient, *, bucket: str, expires_seconds: int,
    ) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise MemoryS3MediaProxyConfigError("MEMORY_MEDIA_BUCKET_INVALID")
        if not 1 <= expires_seconds <= 300:
            raise MemoryS3MediaProxyConfigError("MEMORY_MEDIA_TTL_INVALID")
        self._client = client
        self._bucket = bucket
        # API 只能使用部署固定的有效期，不能让调用者自行扩大。
        self.expires_seconds = expires_seconds

    @classmethod
    def from_settings(cls, settings: object) -> MemoryS3MediaProxy | None:
        """从部署配置创建 boto3 client；完全未配置则由媒体 API fail-closed。"""
        endpoint = getattr(settings, "MEMORY_MEDIA_S3_ENDPOINT_URL", "")
        bucket = getattr(settings, "MEMORY_MEDIA_S3_BUCKET", "")
        region = getattr(settings, "MEMORY_MEDIA_S3_REGION", "")
        access_key = getattr(settings, "MEMORY_MEDIA_S3_ACCESS_KEY_ID", "")
        secret_key = getattr(settings, "MEMORY_MEDIA_S3_SECRET_ACCESS_KEY", "")
        expires_seconds = getattr(settings, "MEMORY_MEDIA_SIGNED_URL_TTL_SECONDS", 60)
        configured = (endpoint, bucket, region, access_key, secret_key)
        if not any(configured):
            return None
        if not all(isinstance(value, str) and value.strip() for value in configured):
            raise MemoryS3MediaProxyConfigError("MEMORY_MEDIA_STORAGE_CONFIG_INVALID")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
            raise MemoryS3MediaProxyConfigError("MEMORY_MEDIA_STORAGE_CONFIG_INVALID")
        if parsed.scheme != "https" and getattr(settings, "ENVIRONMENT", "development") == "production":
            raise MemoryS3MediaProxyConfigError("MEMORY_MEDIA_STORAGE_CONFIG_INVALID")
        try:
            import boto3
        except ImportError as exc:  # 依赖缺失应使部署失败，不能退化到公开 URL。
            raise MemoryS3MediaProxyConfigError("MEMORY_MEDIA_STORAGE_SDK_UNAVAILABLE") from exc
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        return cls(client, bucket=bucket, expires_seconds=expires_seconds)

    def create_access_url(self, storage_key: str, *, expires_seconds: int) -> str:
        """对受控私有 key 签发固定 TTL 的只读 URL，签名 URL 不写日志。"""
        if expires_seconds != self.expires_seconds:
            raise MemoryS3MediaProxyConfigError("MEMORY_MEDIA_TTL_INVALID")
        if (
            not storage_key
            or storage_key.startswith(("/", "\\"))
            or ".." in storage_key.split("/")
            or "\\" in storage_key
        ):
            raise MemoryS3MediaProxyConfigError("MEMORY_MEDIA_STORAGE_KEY_INVALID")
        try:
            url = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": storage_key},
                ExpiresIn=self.expires_seconds,
            )
        except Exception as exc:
            # 不保留 SDK 响应或凭证信息，只让 API 返回固定可恢复错误。
            raise MemoryS3MediaProxyConfigError("MEMORY_MEDIA_SIGNING_FAILED") from exc
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise MemoryS3MediaProxyConfigError("MEMORY_MEDIA_SIGNING_FAILED")
        return url
