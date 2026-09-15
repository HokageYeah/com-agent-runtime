"""M8 回忆录音频存储：scope/object_key、私有 OSS 上传、转码与安全下载。

职责边界（R6）：
1. `compute_audio_scope`：RunRef 四元组的 canonical JSON 数组 HMAC-SHA256
   摘要（与两仓冻结 fixture 向量逐字一致），用作 object_key 的 scope 目录。
2. `build_audio_object_key`：环境角色前缀 + scope 目录 + 不透明随机名，
   相同正文不同 scene 的资产键天然独立；随机名与正文无关、不可枚举。
3. `AliyunAudioOSSUploader`：独立的 private ACL 上传方法——音频资产永不
   public-read，禁止"先公共再补改"；不做预签名（试听签名归媒体访问层）。
4. `AudioTranscoder`：ffmpeg concat demuxer 解码重编（非字节拼接）、
   ffprobe 实测毫秒向上取整；临时文件 0600、随机目录、finally 清理；
   子进程超时/取消整组终止（进程组 SIGKILL，不留孤儿）。
5. `SecureAudioDownloader`：下载官方临时 AudioUrl——仅 HTTPS、精确 host
   白名单、每跳重定向/DNS/私网 IP 全量 SSRF 校验、流式字节上限。

隐私铁律：临时 URL、凭据、ffmpeg stderr 原文绝不进入日志或异常文本；
日志与异常只携带安全错误枚举、字节数与状态码。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import signal
import socket
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent import futures
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 角色常量：与部署前缀一一对应，object_key 文件名前缀由角色决定。
AUDIO_ROLE_NARRATOR = "narrator"
AUDIO_ROLE_BACKGROUND = "background"
# 冻结契约：narrator -> narr-<6hex>.mp3，background -> bgm-<6hex>.mp3。
_ROLE_FILE_PREFIX: dict[str, str] = {
    AUDIO_ROLE_NARRATOR: "narr",
    AUDIO_ROLE_BACKGROUND: "bgm",
}
_SCOPE_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")
# 音频统一输出规格：与 TTS 音频参数一致（24kHz / 64kbps MP3）。
_TRANSCODE_SAMPLE_RATE = 24000
_TRANSCODE_BIT_RATE = "64k"
# 重定向链上限：官方临时 URL 正常至多一跳 CDN，3 跳已是宽松上限。
_MAX_REDIRECT_HOPS = 3
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class MemoirAudioStorageError(ValueError):
    """受控错误：.code 为安全枚举，message 不含 URL/凭据/stderr 原文。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


# ---------------------------------------------------------------------------
# scope HMAC 与 object_key
# ---------------------------------------------------------------------------


def compute_audio_scope(
    key: str,
    *,
    business_id: str,
    archive_id: str,
    run_id: str,
    generation_epoch: int,
) -> str:
    """计算 RunRef 四元组的 scope 摘要（hex）。

    canonical 规则（两仓冻结契约）：JSON 数组 [business_id, archive_id,
    run_id, generation_epoch]，compact 分隔符、ensure_ascii=False、UTF-8
    编码后取 HMAC-SHA256 hex。epoch 必须是真整数（bool 除外）；key 或
    任一标识为空按配置错误拒绝，不得降级为弱摘要。
    """
    if not isinstance(key, str) or not key:
        raise MemoirAudioStorageError("AUDIO_SCOPE_INVALID", "scope HMAC key 为空")
    if (
        not isinstance(business_id, str)
        or not business_id
        or not isinstance(archive_id, str)
        or not archive_id
        or not isinstance(run_id, str)
        or not run_id
    ):
        raise MemoirAudioStorageError("AUDIO_SCOPE_INVALID", "scope 标识非法")
    # bool 是 int 子类，显式排除；epoch 必须整数（fixture 契约）。
    if isinstance(generation_epoch, bool) or not isinstance(generation_epoch, int):
        raise MemoirAudioStorageError("AUDIO_SCOPE_INVALID", "generation_epoch 必须为整数")
    canonical = json.dumps(
        [business_id, archive_id, run_id, generation_epoch],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hmac.new(
        key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    logger.info(
        "Memoir 音频 scope 已计算，code=AUDIO_SCOPE_COMPUTED，epoch=%s", generation_epoch
    )
    return digest


def build_audio_object_key(prefix: str, scope_hex: str, *, role: str) -> str:
    """构造音频对象键：{prefix}{scope}/{narr|bgm}-{6位随机hex}.mp3。

    随机名使用 secrets（密码学随机）：与正文无关、不可枚举、每次唯一，
    保证相同正文不同 scene 的资产键天然独立。前缀/scope/role 任一非法
    即拒绝，杜绝路径穿越与 URL 形态的 key。
    """
    if not isinstance(prefix, str) or not prefix:
        raise MemoirAudioStorageError("AUDIO_OBJECT_KEY_INVALID", "对象键前缀为空")
    if prefix.startswith("/"):
        raise MemoirAudioStorageError("AUDIO_OBJECT_KEY_INVALID", "对象键前缀不得以 / 开头")
    if "://" in prefix or "\\" in prefix:
        raise MemoirAudioStorageError("AUDIO_OBJECT_KEY_INVALID", "对象键前缀不得是 URL")
    if not prefix.endswith("/"):
        raise MemoirAudioStorageError("AUDIO_OBJECT_KEY_INVALID", "对象键前缀必须以 / 结尾")
    if any(part in ("", "..", ".") for part in prefix[:-1].split("/")):
        raise MemoirAudioStorageError("AUDIO_OBJECT_KEY_INVALID", "对象键前缀含非法路径段")
    if not isinstance(scope_hex, str) or not _SCOPE_HEX_PATTERN.fullmatch(scope_hex):
        raise MemoirAudioStorageError("AUDIO_OBJECT_KEY_INVALID", "scope 摘要非法")
    file_prefix = _ROLE_FILE_PREFIX.get(role)
    if file_prefix is None:
        raise MemoirAudioStorageError("AUDIO_OBJECT_KEY_INVALID", "未知音频角色")
    return f"{prefix}{scope_hex}/{file_prefix}-{secrets.token_hex(3)}.mp3"


# ---------------------------------------------------------------------------
# 私有 OSS 上传（独立 private ACL 方法，不复用 public-read 路径）
# ---------------------------------------------------------------------------


def _region_from_endpoint(endpoint: str) -> str:
    """从 OSS endpoint 提取 region（oss-cn-hangzhou.aliyuncs.com → cn-hangzhou）。

    提取失败时返回 host 首段原样；真实部署的 endpoint 形态由 R8 现场验收。
    """
    host = endpoint.replace("https://", "").replace("http://", "").split("/")[0]
    first = host.split(".")[0]
    return first[4:] if first.startswith("oss-") else first


# D1：分块读取的块大小上限（1 MiB）。真实 SDK StreamBodyReader 的
# iter_bytes(block_size=...) 逐块产出；块本身有界，累计上限由调用方
# max_bytes 在每块并入前判定，内存占用绝不超过 max_bytes + 单块。
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


class AliyunAudioOSSUploader:
    """音频私有上传器：上传即 private，无公共读路径、无预签名职责。

    M8 默认配乐新增只读通道 `download_private_bytes`：对精确默认源 key
    做有界 GetObject——只读，绝不 delete / put / 改 ACL。
    """

    def __init__(
        self,
        *,
        access_key_id: str,
        access_key_secret: str,
        bucket: str,
        endpoint: str,
        client: Any | None = None,
    ) -> None:
        if not access_key_id or not access_key_secret or not bucket or not endpoint:
            raise MemoirAudioStorageError(
                "AUDIO_UPLOAD_CONFIG_INVALID", "OSS 凭据/桶/endpoint 未配置"
            )
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._bucket = bucket
        self._endpoint = endpoint
        # 测试注入假 client；真实 SDK 客户端懒加载（首次上传才初始化）。
        self._injected_client = client
        self._client: Any | None = None
        self._oss_module: Any | None = None

    def _ensure_client(self) -> tuple[Any, Any]:
        """懒加载 OSS SDK 并创建客户端；凭据绝不写日志。"""
        if self._injected_client is not None:
            return self._injected_client, _SdkStub
        if self._client is not None and self._oss_module is not None:
            return self._client, self._oss_module
        import alibabacloud_oss_v2 as oss  # type: ignore[import-untyped]

        credentials_provider = oss.credentials.StaticCredentialsProvider(
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
        )
        cfg = oss.config.load_default()
        cfg.credentials_provider = credentials_provider
        cfg.region = _region_from_endpoint(self._endpoint)
        cfg.endpoint = self._endpoint
        self._client = oss.Client(cfg)
        self._oss_module = oss
        logger.info("Memoir 音频 OSS 客户端初始化完成，code=AUDIO_OSS_READY")
        return self._client, self._oss_module

    def upload_private_bytes(self, data: bytes, object_key: str, mime: str) -> None:
        """上传音频字节并显式设置 private ACL；失败只映射安全枚举。"""
        if not isinstance(data, bytes) or not data:
            raise MemoirAudioStorageError("AUDIO_UPLOAD_PAYLOAD_INVALID", "待上传音频为空")
        if not object_key or not mime:
            raise MemoirAudioStorageError("AUDIO_UPLOAD_PAYLOAD_INVALID", "对象键或 MIME 为空")
        client, oss = self._ensure_client()
        try:
            # 对象级 private ACL：音频资产永远私有，访问必须走签名层。
            result = client.put_object(
                oss.PutObjectRequest(
                    bucket=self._bucket,
                    key=object_key,
                    body=data,
                    acl="private",
                    content_type=mime,
                )
            )
        except Exception as exc:
            raw_message = str(exc)
            if "AccessDenied" in raw_message or "0016-00000901" in raw_message:
                logger.warning("Memoir 音频私有上传被拒绝，code=AUDIO_UPLOAD_ACCESS_DENIED")
                raise MemoirAudioStorageError(
                    "AUDIO_UPLOAD_ACCESS_DENIED", "OSS 拒绝私有写入，请检查凭据与 PutObject 权限"
                ) from exc
            logger.warning("Memoir 音频私有上传失败，code=AUDIO_UPLOAD_FAILED")
            raise MemoirAudioStorageError("AUDIO_UPLOAD_FAILED", "OSS 私有上传失败") from exc
        if getattr(result, "status_code", None) not in (200, 204):
            logger.warning(
                "Memoir 音频私有上传状态异常，status=%s，code=AUDIO_UPLOAD_FAILED",
                getattr(result, "status_code", None),
            )
            raise MemoirAudioStorageError("AUDIO_UPLOAD_FAILED", "OSS 上传返回非预期状态")
        logger.info(
            "Memoir 音频私有上传完成，size=%d，code=AUDIO_UPLOAD_DONE", len(data)
        )

    def download_private_bytes(
        self, object_key: str, *, max_bytes: int, timeout_seconds: float
    ) -> bytes:
        """默认源私有读取（freeze 2026-09-11 §3 + D1/S1 修复）：对精确 key 有界 GetObject。

        只读合同：不 delete / put / 改 ACL。字节上限与超时均由调用方传入
        （上限对齐 MEMOIR_AUDIO_MAX_FILE_BYTES，超时为节点剩余时间），
        超限/超时立即中断失败。空字节允许返回（拒绝是转码入口的职责）。

        超时语义（D1 如实表述）：future.result(timeout) 超时只是"停止等待"，
        不证明底层读取已停止，也不强杀线程；调用方通过锁保护的共享发布
        状态尽力 close 响应体打断在途读，后台读取由连接超时与资源释放
        约束最终收敛，调用方绝不因后台读取而阻塞。

        发布/迟到关闭（S1 修复）：工作线程拿到 body 后在锁内发布；
        caller 超时在锁内标记放弃并认领已发布 body，交给一次性有界后台
        关闭线程（caller 立即返回，不因 close 等在途读的锁而拖住）；caller
        已放弃后才到手的迟到 body 由工作线程在锁内发现标志并就地关闭。
        锁保证任意 body 的关闭责任恰好归属一方，杜绝"已发布无人关闭"
        的泄漏。旧实现用空列表 + truthiness 守卫，发布与消费双向死代码。
        """
        if not isinstance(object_key, str) or not object_key:
            raise MemoirAudioStorageError("AUDIO_OBJECT_KEY_INVALID", "对象键为空")
        if max_bytes <= 0 or timeout_seconds <= 0:
            raise MemoirAudioStorageError("AUDIO_SOURCE_READ_FAILED", "读取上限或超时非法")
        client, oss = self._ensure_client()
        # 锁保护的发布槽：body=None 表示未发布；abandoned=True 表示 caller
        # 已超时放弃。所有读写都在锁内完成，发布/放弃的先后次序因此可判定。
        release_lock = threading.Lock()
        shared: dict[str, Any] = {"body": None, "abandoned": False}
        executor = futures.ThreadPoolExecutor(max_workers=1)
        try:
            worker = executor.submit(
                self._get_object_bytes,
                client,
                oss,
                object_key,
                max_bytes,
                timeout_seconds,
                shared,
                release_lock,
            )
            try:
                return worker.result(timeout=timeout_seconds)
            except futures.TimeoutError:
                # S1 修复（2026-09-14 真实链路复现）：真实 SDK 的 close()
                # 链路（StreamBodyReader → requests → urllib3）会等在途
                # iter_bytes 读取持有的底层 BufferedReader 锁；caller 在
                # 此同步 close 会被拖到在途读结束（30ms 期限实测被拖到
                # ~0.4s）。因此锁内只完成"标记放弃 + 认领已发布 body"两步
                # 决策，认领到的 body 交给一次性有界后台关闭线程，caller
                # 立即按期限抛 AUDIO_SOURCE_READ_TIMEOUT，绝不在这里等 I/O。
                with release_lock:
                    shared["abandoned"] = True
                    published = shared["body"]
                if published is not None:
                    # 一次性有界后台关闭：每次超时事件至多一个短生命周期
                    # daemon 线程，close 在在途读结束后完成（生产环境由
                    # SDK 连接/读取超时有界收敛），做完即退出。绝不把
                    # close 排到下方 executor——唯一 worker 正阻塞在
                    # iter_bytes 上持有 close 需要的读锁，排队即死锁。
                    closer = threading.Thread(
                        target=self._close_response_body_best_effort,
                        args=(published,),
                        name="memoir-audio-body-closer",
                        daemon=True,
                    )
                    closer.start()
                logger.warning(
                    "Memoir 音频默认源读取超时，code=AUDIO_SOURCE_READ_TIMEOUT"
                )
                raise MemoirAudioStorageError(
                    "AUDIO_SOURCE_READ_TIMEOUT", "默认音频源读取超时"
                ) from None
        finally:
            # 不等待超时后仍在读的线程：调用方绝不因后台读取而阻塞。
            executor.shutdown(wait=False)

    @staticmethod
    def _close_response_body_best_effort(body: Any) -> None:
        """尽力释放响应体（幂等；失败仅记日志，绝不影响安全错误码）。"""
        try:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        except Exception:
            # 关闭失败只记调试日志：不能吞掉真正的超时/读取错误码。
            logger.debug(
                "Memoir 音频默认源响应体关闭失败（尽力释放路径）", exc_info=True
            )

    def _get_object_bytes(
        self,
        client: Any,
        oss: Any,
        object_key: str,
        max_bytes: int,
        timeout_seconds: float,
        shared: dict[str, Any],
        release_lock: threading.Lock,
    ) -> bytes:
        """分块有界读取 GetObject 字节；失败只映射安全枚举。

        D1 边界：真实 SDK 响应体是 StreamBodyReader，read() 无 size 参数，
        传参即 TypeError（被误标 READ_FAILED 的根因）；唯一合法入口是
        iter_bytes(block_size=...)。累计缓冲字节数绝不超过 max_bytes（每块
        并入前判上限，超限在流中途立即中断，不做无界整读后查长度）；每块
        之间按 monotonic 剩余期限检查；成功/异常/超限/超时四条路径都
        finally 关闭响应体。

        S1 发布协议：拿到 body 后在锁内发布给超时路径；若 caller 已超时
        放弃（迟到 body），就地关闭使后续 iter_bytes 立即失败，让本线程
        有界收敛退出，响应体绝不遗留。
        """
        deadline = time.monotonic() + timeout_seconds
        body: Any = None
        try:
            result = client.get_object(
                oss.GetObjectRequest(bucket=self._bucket, key=object_key)
            )
            body = getattr(result, "body", None)
            # 锁内发布/认领：abandoned 先置位则本线程独占迟到 body 的关闭
            # 责任；否则 body 交给超时路径的 caller 关闭。两种先后次序下
            # 关闭责任都恰好归属一方（修复前空列表守卫使本块是死代码）。
            with release_lock:
                late = shared["abandoned"]
                if not late:
                    shared["body"] = body
            if late:
                logger.warning(
                    "Memoir 音频默认源迟到响应体由工作线程关闭，code=AUDIO_SOURCE_BODY_LATE_RELEASE"
                )
                self._close_response_body_best_effort(body)
            chunks: list[bytes] = []
            total = 0
            for chunk in body.iter_bytes(block_size=_DOWNLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > max_bytes:
                    logger.warning(
                        "Memoir 音频默认源超过字节上限，cap=%d，code=AUDIO_FILE_TOO_LARGE",
                        max_bytes,
                    )
                    raise MemoirAudioStorageError(
                        "AUDIO_FILE_TOO_LARGE", "默认音频源超过字节上限"
                    )
                chunks.append(chunk)
                if time.monotonic() >= deadline:
                    # 块级期限：单块之间兜底，避免慢流无限占用工作线程。
                    logger.warning(
                        "Memoir 音频默认源读取超时，code=AUDIO_SOURCE_READ_TIMEOUT"
                    )
                    raise MemoirAudioStorageError(
                        "AUDIO_SOURCE_READ_TIMEOUT", "默认音频源读取超时"
                    )
            data = b"".join(chunks)
            logger.info(
                "Memoir 音频默认源读取完成，size=%d，code=AUDIO_SOURCE_READ_DONE", len(data)
            )
            return data
        except MemoirAudioStorageError:
            # 主动抛出的安全码（超限/块级超时）不得落入通用兜底被重映射。
            raise
        except Exception as exc:
            self._raise_safe_source_error(exc)
        finally:
            # 四条路径统一释放：成功读完、SDK 异常、超限中断、块级超时。
            if body is not None:
                self._close_response_body_best_effort(body)

    @staticmethod
    def _raise_safe_source_error(exc: Exception) -> None:
        """把 OSS 读取异常映射为安全枚举（消息不含 key/凭据/配置值）。"""
        raw_message = str(exc)
        if "NoSuchKey" in raw_message or "404" in raw_message:
            logger.warning("Memoir 音频默认源不存在，code=AUDIO_SOURCE_NOT_FOUND")
            raise MemoirAudioStorageError(
                "AUDIO_SOURCE_NOT_FOUND", "默认音频源不存在"
            ) from exc
        if (
            "AccessDenied" in raw_message
            or "403" in raw_message
            or "0016-00000901" in raw_message
        ):
            logger.warning("Memoir 音频默认源读取被拒绝，code=AUDIO_SOURCE_ACCESS_DENIED")
            raise MemoirAudioStorageError(
                "AUDIO_SOURCE_ACCESS_DENIED", "OSS 拒绝读取默认音频源"
            ) from exc
        logger.warning("Memoir 音频默认源读取失败，code=AUDIO_SOURCE_READ_FAILED")
        raise MemoirAudioStorageError(
            "AUDIO_SOURCE_READ_FAILED", "OSS 读取默认音频源失败"
        ) from exc


class _SdkStub:
    """注入假 client 时的占位命名空间：请求对象与 SDK 同形。"""

    class PutObjectRequest:
        """以关键字参数构造、属性暴露的请求对象（与 SDK 字段一致）。"""

        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class GetObjectRequest:
        """同款 **kwargs 存属性的读取请求对象（与 PutObjectRequest 一致）。"""

        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)


# ---------------------------------------------------------------------------
# 转码：ffmpeg 解码拼接 / 重编、ffprobe 实测时长、临时文件纪律
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConcatResult:
    """转码结果：最终 MP3 字节与 ffprobe 实测毫秒（向上取整）。"""

    audio: bytes
    duration_ms: int


def _write_private_file(path: Path, data: bytes) -> None:
    """以 0600 权限写入临时文件（先写再收紧权限，避免默认 0644 泄露窗口）。"""
    path.write_bytes(data)
    path.chmod(0o600)


class AudioTranscoder:
    """ffmpeg/ffprobe 封装：子进程可注入、超时/取消整组终止、临时文件必清理。"""

    def __init__(
        self,
        *,
        ffmpeg_path: str = "/usr/bin/ffmpeg",
        ffprobe_path: str = "/usr/bin/ffprobe",
        subprocess_timeout_seconds: float = 60.0,
        command_runner: Callable[[list[str]], Awaitable[tuple[int, bytes, bytes]]] | None = None,
        max_input_bytes: int | None = None,
    ) -> None:
        self._ffmpeg_path = ffmpeg_path
        self._ffprobe_path = ffprobe_path
        self._subprocess_timeout_seconds = subprocess_timeout_seconds
        self._command_runner = command_runner
        self._max_input_bytes = max_input_bytes

    async def concat_mp3_segments(self, segments: list[bytes]) -> ConcatResult:
        """多段 MP3 经 ffmpeg concat demuxer 解码重编拼接为单一 MP3。

        必须解码重编而非字节拼接：各段独立 MP3 流的头帧/元数据在字节级
        拼接后不可靠。任一段失败由调用方（场景合成）整体失败，这里只对
        单次拼接负责。
        """
        if not segments or any(not isinstance(seg, bytes) or not seg for seg in segments):
            raise MemoirAudioStorageError("AUDIO_TRANSCODE_INPUT_INVALID", "待拼接段为空")
        tmp_dir = Path(tempfile.mkdtemp(prefix="memoir-audio-"))
        try:
            seg_paths = [
                tmp_dir / f"seg-{index:03d}.mp3" for index in range(len(segments))
            ]
            for seg_path, seg_data in zip(seg_paths, segments, strict=True):
                _write_private_file(seg_path, seg_data)
            list_path = tmp_dir / "concat.txt"
            _write_private_file(
                list_path,
                "".join(f"file '{seg_path}'\n" for seg_path in seg_paths).encode("utf-8"),
            )
            output_path = tmp_dir / "output.mp3"
            ffmpeg_argv = [
                self._ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(list_path),
                "-c:a", "libmp3lame", "-ar", str(_TRANSCODE_SAMPLE_RATE),
                "-b:a", _TRANSCODE_BIT_RATE, str(output_path),
            ]
            return await self._run_transcode(ffmpeg_argv, output_path, "AUDIO_TRANSCODE_FAILED")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def transcode_to_mp3(self, data: bytes) -> ConcatResult:
        """单文件解码校验并转 MP3（BGM 下载通道：拒绝非音频字节）。"""
        if not isinstance(data, bytes) or not data:
            raise MemoirAudioStorageError("AUDIO_TRANSCODE_INPUT_INVALID", "待转码字节为空")
        if self._max_input_bytes is not None and len(data) > self._max_input_bytes:
            raise MemoirAudioStorageError("AUDIO_FILE_TOO_LARGE", "输入超过字节上限")
        tmp_dir = Path(tempfile.mkdtemp(prefix="memoir-audio-"))
        try:
            input_path = tmp_dir / "input.mp3"
            _write_private_file(input_path, data)
            output_path = tmp_dir / "output.mp3"
            ffmpeg_argv = [
                self._ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(input_path),
                "-c:a", "libmp3lame", "-ar", str(_TRANSCODE_SAMPLE_RATE),
                "-b:a", _TRANSCODE_BIT_RATE, str(output_path),
            ]
            return await self._run_transcode(ffmpeg_argv, output_path, "AUDIO_DECODE_FAILED")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def _run_transcode(
        self, ffmpeg_argv: list[str], output_path: Path, failure_code: str
    ) -> ConcatResult:
        """执行 ffmpeg → ffprobe → 读取产物；失败映射安全枚举。"""
        return_code, _, _ = await self._run_command(ffmpeg_argv)
        if return_code != 0:
            # stderr 原文可能含临时路径与内部细节，绝不进异常文本。
            logger.warning("Memoir 音频转码失败，rc=%d，code=%s", return_code, failure_code)
            raise MemoirAudioStorageError(failure_code, "音频转码失败")
        duration_ms = await self._probe_duration_ms(output_path)
        audio = output_path.read_bytes()
        if not audio:
            raise MemoirAudioStorageError(failure_code, "音频转码产物为空")
        logger.info("Memoir 音频转码完成，duration_ms=%d，code=AUDIO_TRANSCODE_DONE", duration_ms)
        return ConcatResult(audio=audio, duration_ms=duration_ms)

    async def _probe_duration_ms(self, media_path: Path) -> int:
        """ffprobe 实测时长并向上取整为毫秒；不可解析即失败，不得默认 0。"""
        ffprobe_argv = [
            self._ffprobe_path, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ]
        return_code, stdout, _ = await self._run_command(ffprobe_argv)
        if return_code != 0:
            raise MemoirAudioStorageError("AUDIO_DURATION_INVALID", "时长探测失败")
        try:
            seconds = Decimal(stdout.decode("ascii").strip())
        except (UnicodeDecodeError, InvalidOperation) as exc:
            raise MemoirAudioStorageError("AUDIO_DURATION_INVALID", "时长不可解析") from exc
        if seconds.is_nan() or seconds.is_infinite() or seconds <= 0:
            raise MemoirAudioStorageError("AUDIO_DURATION_INVALID", "时长非法")
        return math.ceil(seconds * 1000)

    async def _run_command(self, argv: list[str]) -> tuple[int, bytes, bytes]:
        """执行子进程：注入 runner 优先；真实子进程超时/取消整组终止。"""
        if self._command_runner is not None:
            return await self._command_runner(argv)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), self._subprocess_timeout_seconds
            )
        except TimeoutError:
            self._kill_process_group(process)
            await process.wait()
            logger.warning("Memoir 音频子进程超时，code=AUDIO_TRANSCODE_TIMEOUT")
            raise MemoirAudioStorageError("AUDIO_TRANSCODE_TIMEOUT", "转码子进程超时") from None
        except asyncio.CancelledError:
            # 外部取消：终止整组子进程后原样传播 CancelledError，不吞。
            self._kill_process_group(process)
            await process.wait()
            raise
        return process.returncode or 0, stdout, stderr

    @staticmethod
    def _kill_process_group(process: asyncio.subprocess.Process) -> None:
        """按进程组 SIGKILL，避免 shell 子进程（如 sleep）成为孤儿。"""
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()


# ---------------------------------------------------------------------------
# 官方临时 URL 安全下载（SSRF 全量校验）
# ---------------------------------------------------------------------------


class SecureAudioDownloader:
    """官方临时 AudioUrl 下载器：HTTPS + 精确 host 白名单 + 每跳 SSRF 校验。"""

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        max_bytes: int,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str], list[str]] | None = None,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        if not allowed_hosts:
            raise MemoirAudioStorageError("AUDIO_DOWNLOAD_HOST_FORBIDDEN", "host 白名单为空")
        if max_bytes <= 0:
            raise MemoirAudioStorageError("AUDIO_DOWNLOAD_URL_INVALID", "字节上限非法")
        self._allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self._max_bytes = max_bytes
        self._transport = transport
        self._resolver = resolver
        self._request_timeout_seconds = request_timeout_seconds

    async def download(self, url: str) -> bytes:
        """下载音频字节：URL 本身、每一跳重定向、DNS 结果全量校验。"""
        current_url = self._validate_url(url)
        hops = 0
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._request_timeout_seconds,
            follow_redirects=False,
        ) as client:
            while True:
                async with client.stream("GET", current_url) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        hops += 1
                        if hops > _MAX_REDIRECT_HOPS:
                            logger.warning(
                                "Memoir 音频下载重定向超限，hops=%d，code=AUDIO_DOWNLOAD_REDIRECTS_EXCEEDED",
                                hops,
                            )
                            raise MemoirAudioStorageError(
                                "AUDIO_DOWNLOAD_REDIRECTS_EXCEEDED", "重定向次数超限"
                            )
                        location = response.headers.get("location", "")
                        # 每一跳都完整重验 scheme/host/userinfo/端口/DNS。
                        current_url = self._validate_url(location)
                        continue
                    if response.status_code != 200:
                        code = f"AUDIO_DOWNLOAD_HTTP_{response.status_code}"
                        logger.warning("Memoir 音频下载 HTTP 失败，code=%s", code)
                        raise MemoirAudioStorageError(code, "音频下载 HTTP 请求失败")
                    return await self._read_capped(response)

    async def _read_capped(self, response: httpx.Response) -> bytes:
        """流式读取并在超过字节上限时立即中断。"""
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self._max_bytes:
                logger.warning(
                    "Memoir 音频下载超字节上限，cap=%d，code=AUDIO_DOWNLOAD_TOO_LARGE",
                    self._max_bytes,
                )
                raise MemoirAudioStorageError("AUDIO_DOWNLOAD_TOO_LARGE", "音频超过字节上限")
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise MemoirAudioStorageError("AUDIO_DOWNLOAD_EMPTY", "音频内容为空")
        logger.info("Memoir 音频下载完成，size=%d，code=AUDIO_DOWNLOAD_DONE", len(data))
        return data

    def _validate_url(self, url: str) -> str:
        """校验单个 URL：HTTPS、精确 host、无 userinfo、端口 443、DNS 全局。"""
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, ValueError) as exc:
            raise MemoirAudioStorageError("AUDIO_DOWNLOAD_URL_INVALID", "URL 非法") from exc
        if parsed.scheme != "https":
            raise MemoirAudioStorageError("AUDIO_DOWNLOAD_URL_INVALID", "仅允许 HTTPS")
        host = parsed.host
        if not host:
            raise MemoirAudioStorageError("AUDIO_DOWNLOAD_URL_INVALID", "URL 缺少 host")
        if parsed.username or parsed.password:
            raise MemoirAudioStorageError("AUDIO_DOWNLOAD_URL_INVALID", "URL 不得携带 userinfo")
        if parsed.port not in (None, 443):
            raise MemoirAudioStorageError("AUDIO_DOWNLOAD_URL_INVALID", "URL 端口非法")
        # 精确匹配：子域、大小写变体一律不放行（parsed.host 已小写）。
        if host not in self._allowed_hosts:
            logger.warning(
                "Memoir 音频下载 host 不在白名单，code=AUDIO_DOWNLOAD_HOST_FORBIDDEN"
            )
            raise MemoirAudioStorageError("AUDIO_DOWNLOAD_HOST_FORBIDDEN", "host 不在白名单")
        self._require_global_addresses(host)
        return str(parsed)

    def _require_global_addresses(self, host: str) -> None:
        """DNS 解析结果必须全部是公网地址；任何私网/保留地址即拒绝。"""
        try:
            if self._resolver is not None:
                addresses = self._resolver(host)
            else:
                # getaddrinfo 每项为 (family, type, proto, canonname, sockaddr)；
                # IP 地址取 sockaddr（第 5 位）的首元素。此前误写 info[0][4][0]，
                # 会对 AddressFamily 枚举取下标，真实 DNS 路径必然 TypeError。
                addresses = list(
                    {
                        # typeshed 把 sockaddr 首元素标为 str|int（地址/端口共用），
                        # TCP 场景运行期恒为地址字符串，str() 收敛类型即可。
                        str(info[4][0])
                        for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
                    }
                )
        except OSError as exc:
            raise MemoirAudioStorageError(
                "AUDIO_DOWNLOAD_ADDRESS_UNSAFE", "DNS 解析失败"
            ) from exc
        if not addresses:
            raise MemoirAudioStorageError("AUDIO_DOWNLOAD_ADDRESS_UNSAFE", "DNS 无解析结果")
        for address in addresses:
            try:
                is_global = ipaddress.ip_address(address).is_global
            except ValueError as exc:
                raise MemoirAudioStorageError(
                    "AUDIO_DOWNLOAD_ADDRESS_UNSAFE", "解析地址非法"
                ) from exc
            if not is_global:
                logger.warning(
                    "Memoir 音频下载解析到非全局地址，code=AUDIO_DOWNLOAD_ADDRESS_UNSAFE"
                )
                raise MemoirAudioStorageError(
                    "AUDIO_DOWNLOAD_ADDRESS_UNSAFE", "解析到私网或保留地址"
                )
