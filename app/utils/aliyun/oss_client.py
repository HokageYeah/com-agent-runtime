"""阿里云 OSS 公共客户端。

这个文件只承载跨业务通用的 OSS 能力：
1. 从全局配置创建 OSS 客户端
2. 上传本地文件
3. 生成/刷新预签名访问地址
4. 删除对象

AI 制作里的图片、音频、视频上传都从这里走，避免各业务服务重复造客户端、
重复处理权限错误，也方便后续统一排查 OSS 配置问题。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from app.core.config import settings

TAG = "ALIYUN_OSS"


class AliyunOSSClientError(Exception):
    """阿里云 OSS 公共客户端异常。"""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class AliyunOSSUploadResult:
    """OSS 上传结果。"""

    object_key: str
    presigned_url: str
    public_url: str


class AliyunOSSClient:
    """阿里云 OSS 公共客户端。

    注意：日志中只打印 object_key 和配置是否存在，不打印完整预签名 URL，
    避免泄露带签名的临时访问地址。
    """

    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        bucket_name: str,
        region: str,
        endpoint: str,
    ):
        self.access_key_id = str(access_key_id or "").strip()
        self.access_key_secret = str(access_key_secret or "").strip()
        self.bucket_name = str(bucket_name or "").strip()
        self.region = str(region or "").strip()
        self.endpoint = str(endpoint or "").strip()
        if not self.access_key_id or not self.access_key_secret:
            raise AliyunOSSClientError("阿里云 OSS AccessKey 未配置，无法上传文件", 500)
        if not self.bucket_name or not self.region or not self.endpoint:
            raise AliyunOSSClientError("阿里云 OSS Bucket、Region 或 Endpoint 未配置，无法上传文件", 500)
        self._client: Any | None = None
        self._oss_module: Any | None = None

    @classmethod
    def from_settings(cls) -> "AliyunOSSClient":
        """从项目全局配置创建 OSS 客户端。"""
        return cls(
            settings.ACCESS_KEY_ID,
            settings.ACCESS_KEY_SECRET,
            settings.BUCKET_NAME,
            settings.REGION,
            settings.ENDPOINT,
        )

    @staticmethod
    def mask_access_key_id(value: str) -> str:
        """脱敏 AccessKeyId，供业务日志定位配置使用。"""
        if not value:
            return "未配置"
        if len(value) <= 10:
            return value[:2] + "***"
        return f"{value[:6]}***{value[-4:]}"

    def _ensure_client(self) -> tuple[Any, Any]:
        """懒加载 OSS SDK 并创建客户端。"""
        if self._client is not None and self._oss_module is not None:
            return self._client, self._oss_module

        import alibabacloud_oss_v2 as oss

        credentials_provider = oss.credentials.StaticCredentialsProvider(
            access_key_id=self.access_key_id,
            access_key_secret=self.access_key_secret,
        )
        cfg = oss.config.load_default()
        cfg.credentials_provider = credentials_provider
        cfg.region = self.region
        cfg.endpoint = self.endpoint
        self._client = oss.Client(cfg)
        self._oss_module = oss
        logger.bind(tag=TAG).info(
            "阿里云 OSS 客户端初始化完成，bucket={}，region={}，endpoint={}，access_key_id={}",
            self.bucket_name,
            self.region,
            self.endpoint,
            self.mask_access_key_id(self.access_key_id),
        )
        return self._client, self._oss_module

    def public_url(self, object_key: str) -> str:
        """把对象 Key 转成公开 URL 形式，仅用于日志展示或公开 Bucket 场景。"""
        endpoint = self.endpoint.replace("https://", "").replace("http://", "").strip("/")
        return f"https://{self.bucket_name}.{endpoint}/{object_key}"

    def presign_get_url(self, object_key: str, expires: timedelta = timedelta(days=7)) -> str:
        """为对象生成临时 GET 访问地址。"""
        if not object_key:
            raise AliyunOSSClientError("OSS 对象 Key 为空，无法生成预签名地址", 400)
        client, oss = self._ensure_client()
        try:
            result = client.presign(
                oss.GetObjectRequest(bucket=self.bucket_name, key=object_key),
                expires=expires,
            )
        except Exception as exc:
            raw_message = str(exc)
            logger.bind(tag=TAG).exception("OSS 预签名失败，object_key={}", object_key)
            if "AccessDenied" in raw_message:
                raise AliyunOSSClientError(
                    "阿里云 OSS 预签名访问地址生成失败：当前 AccessKey 可能缺少 oss:GetObject 权限。"
                    f"原始错误摘要：{raw_message[:500]}",
                    403,
                ) from exc
            raise AliyunOSSClientError(f"OSS 预签名 URL 生成失败：{raw_message[:500]}", 502) from exc
        if not result.url:
            raise AliyunOSSClientError("OSS 预签名 URL 生成失败，未返回可访问地址", 502)
        logger.bind(tag=TAG).info("OSS 预签名地址生成完成，object_key={}，expires={}", object_key, expires)
        return result.url

    def upload_file(
        self,
        local_path: Path | str,
        object_key: str,
        expires: timedelta = timedelta(days=7),
    ) -> AliyunOSSUploadResult:
        """上传本地文件并返回预签名访问地址。"""
        path = Path(local_path)
        if not path.is_file():
            raise AliyunOSSClientError("待上传的本地文件不存在", 404)
        if not object_key:
            raise AliyunOSSClientError("OSS 对象 Key 为空，无法上传文件", 400)

        client, oss = self._ensure_client()
        logger.bind(tag=TAG).info(
            "准备上传文件到 OSS，local_path={}，size={}，bucket={}，object_key={}",
            path,
            path.stat().st_size,
            self.bucket_name,
            object_key,
        )
        try:
            with open(path, "rb") as file_obj:
                result = client.put_object(oss.PutObjectRequest(
                    bucket=self.bucket_name,
                    key=object_key,
                    body=file_obj,
                ))
        except Exception as exc:
            raw_message = str(exc)
            logger.bind(tag=TAG).exception("OSS 文件上传失败，object_key={}", object_key)
            if "AccessDenied" in raw_message or "bucket acl" in raw_message:
                raise AliyunOSSClientError(
                    "阿里云 OSS 上传被拒绝：当前 AccessKey 对 bucket "
                    f"{self.bucket_name} 没有写入权限，或 bucket ACL/Policy 不允许写入。"
                    "请检查 oss:PutObject 和 oss:GetObject 权限。"
                    f"原始错误摘要：{raw_message[:500]}",
                    403,
                ) from exc
            raise AliyunOSSClientError(f"阿里云 OSS 上传失败：{raw_message[:500]}", 502) from exc

        if result.status_code not in (200, 204):
            raise AliyunOSSClientError(f"OSS 上传失败，status_code={result.status_code}", 502)
        presigned_url = self.presign_get_url(object_key, expires)
        logger.bind(tag=TAG).success("OSS 文件上传完成，object_key={}，status_code={}", object_key, result.status_code)
        return AliyunOSSUploadResult(
            object_key=object_key,
            presigned_url=presigned_url,
            public_url=self.public_url(object_key),
        )

    def delete_object(self, object_key: str) -> bool:
        """删除 OSS 对象，失败时返回 False 并记录中文日志。"""
        if not object_key:
            logger.bind(tag=TAG).debug("OSS 对象 Key 为空，跳过删除")
            return False
        client, oss = self._ensure_client()
        logger.bind(tag=TAG).info("准备删除 OSS 对象，bucket={}，object_key={}", self.bucket_name, object_key)
        try:
            result = client.delete_object(oss.DeleteObjectRequest(
                bucket=self.bucket_name,
                key=object_key,
            ))
        except Exception as exc:
            raw_message = str(exc)
            if "AccessDenied" in raw_message:
                logger.bind(tag=TAG).warning(
                    "OSS 对象删除被拒绝，object_key={}，请检查 oss:DeleteObject 权限，error={}",
                    object_key,
                    raw_message[:500],
                )
            else:
                logger.bind(tag=TAG).exception("OSS 对象删除失败，object_key={}", object_key)
            return False
        if result.status_code not in (200, 204):
            logger.bind(tag=TAG).warning(
                "OSS 对象删除返回非预期状态，object_key={}，status_code={}",
                object_key,
                result.status_code,
            )
            return False
        logger.bind(tag=TAG).success("OSS 对象删除完成，object_key={}", object_key)
        return True
