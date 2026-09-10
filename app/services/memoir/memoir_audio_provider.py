"""M8 回忆录音频 Provider：火山 TTS（SSE 单向流）与背景音乐（imagination）。

职责边界（R6）：
1. TTS：官方 `POST /api/v3/tts/unidirectional/sse`，固定音色/采样率/语速，
   逐段完整合成并读取官方 usage.text_words 实际用量；任一帧损坏、断流、
   失败终态（151/153）或空音频即整段失败，绝不返回半段。
2. 分段：`split_narration_segments` 按句无损切分，每段同时满足
   ≤200 Unicode 字符且 ≤900 UTF-8 字节（项目策略，非厂商上限）。
3. 音乐：`https://open.volcengineapi.com/` 显式 Action（GenBGM/GenBGMForTime），
   提交返回 TaskID，QuerySong 轮询状态 0/1 等待、2 成功（AudioUrl 仅内存）、
   3 失败。签名复用 VOLCANO_CV AK/SK，但 Region/Service 独立为
   cn-beijing/imagination，不复用图片 CV 签名默认值。

隐私铁律：正文、音频字节、TaskID 响应体、临时 AudioUrl、凭据绝不写日志
或异常文本；日志与异常只携带安全错误枚举与计数。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import reduce
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---- TTS 协议常量（冻结，不可从请求改写） ----
TTS_API_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse"
TTS_RESOURCE_ID_DEFAULT = "seed-tts-2.0"
TTS_SPEAKER_DEFAULT = "zh_female_wenroushunv_uranus_bigtts"
TTS_SPEECH_RATE_DEFAULT = -10
TTS_SAMPLE_RATE = 24000
TTS_BIT_RATE = 64000
TTS_FORMAT = "mp3"
# 官方 SSE 事件号：352=音频帧（base64），351=句末（忽略），152=SessionFinish，
# 151=SessionCancel（失败），153=SessionFailed（失败）。
TTS_EVENT_AUDIO_DATA = 352
TTS_EVENT_SENTENCE_END = 351
TTS_EVENT_SESSION_FINISH = 152
TTS_EVENT_SESSION_CANCEL = 151
TTS_EVENT_SESSION_FAILED = 153
TTS_SUCCESS_CODE = 20000000
# additions 固定语音指令：标准普通话、温柔自然克制，不表演。
TTS_ADDITIONS_INSTRUCTION = "请用标准普通话，以温柔、自然、克制的语气朗读，不表演、不夸张。"
# 分段阈值：项目策略值，同时满足字符与字节上限。
SEGMENT_MAX_CHARS = 200
SEGMENT_MAX_BYTES = 900
# 句末标点：在这些字符之后优先切句。
_SENTENCE_TERMINALS = "。！？；…!?;"

# ---- 音乐协议常量 ----
MUSIC_API_HOST = "open.volcengineapi.com"
MUSIC_API_URL = f"https://{MUSIC_API_HOST}/"
MUSIC_API_VERSION = "2024-08-12"
MUSIC_MODEL_VERSION = "v5.0"
MUSIC_REGION = "cn-beijing"
MUSIC_SERVICE = "imagination"
MUSIC_ALLOWED_ACTIONS: frozenset[str] = frozenset({"GenBGM", "GenBGMForTime"})
MUSIC_QUERY_ACTION = "QuerySong"
MUSIC_DURATION_SECONDS_DEFAULT = 60
MUSIC_STATUS_WAITING = 0
MUSIC_STATUS_PROCESSING = 1
MUSIC_STATUS_SUCCESS = 2
MUSIC_STATUS_FAILED = 3
# 固定舒缓纯音乐提示词：按作品不变、不含任何私人正文（占位符也不允许）。
BGM_PROMPT_TEXT = (
    "舒缓、温柔的轻音乐纯音乐，钢琴与弦乐为主，节奏缓慢平稳，"
    "情绪温暖安宁，适合作为回忆录朗读的背景音乐，无人声。"
)


class MemoirAudioProviderError(ValueError):
    """受控错误：.code 为安全枚举，message 不含正文/URL/TaskID/凭据。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _provider_http_failure_code(prefix: str, status_code: int) -> str:
    """HTTP 状态映射安全枚举（TTS_/MUSIC_ 前缀 + 状态码数字）。"""
    return f"{prefix}_HTTP_{status_code}"


@dataclass(frozen=True)
class TTSSegmentResult:
    """单段合成结果：完整 MP3 字节 + 官方实际计费字数。"""

    audio: bytes
    text_words: int


@dataclass(frozen=True)
class MusicTaskSnapshot:
    """音乐任务快照：status 0/1 等待、2 成功、3 失败；audio_url 仅内存流转。"""

    status: int
    audio_url: str | None


# ---------------------------------------------------------------------------
# 按句无损分段
# ---------------------------------------------------------------------------


def _split_sentences(text: str) -> list[str]:
    """按句末标点切句（标点归属前句），不丢弃任何字符。"""
    sentences: list[str] = []
    buffer: list[str] = []
    for char in text:
        buffer.append(char)
        if char in _SENTENCE_TERMINALS:
            sentences.append("".join(buffer))
            buffer = []
    if buffer:
        sentences.append("".join(buffer))
    return [s for s in sentences if s]


def _hard_split_by_chars(sentence: str) -> list[str]:
    """无标点长句按字符边界硬切：同时满足字符与字节上限、不丢字。"""
    pieces: list[str] = []
    current: list[str] = []
    current_chars = 0
    current_bytes = 0
    for char in sentence:
        char_bytes = len(char.encode("utf-8"))
        if current and (
            current_chars + 1 > SEGMENT_MAX_CHARS
            or current_bytes + char_bytes > SEGMENT_MAX_BYTES
        ):
            pieces.append("".join(current))
            current = []
            current_chars = 0
            current_bytes = 0
        current.append(char)
        current_chars += 1
        current_bytes += char_bytes
    if current:
        pieces.append("".join(current))
    return pieces


def split_narration_segments(text: str) -> list[str]:
    """按句无损分段：优先句边界合并短句；超限句按字符边界硬切。

    保证：segments 拼接等于原文、每段非空且同时满足
    ≤200 Unicode 字符与 ≤900 UTF-8 字节。空/纯空白正文返回空列表。
    """
    if not text or not text.strip():
        return []
    segments: list[str] = []
    pending = ""
    pending_bytes = 0
    for sentence in _split_sentences(text):
        # 单句自身超限：先冲刷缓冲，再按字符边界硬切该句。
        if (
            len(sentence) > SEGMENT_MAX_CHARS
            or len(sentence.encode("utf-8")) > SEGMENT_MAX_BYTES
        ):
            if pending:
                segments.append(pending)
                pending = ""
                pending_bytes = 0
            segments.extend(_hard_split_by_chars(sentence))
            continue
        sentence_bytes = len(sentence.encode("utf-8"))
        candidate_chars = len(pending) + len(sentence)
        candidate_bytes = pending_bytes + sentence_bytes
        if pending and (
            candidate_chars > SEGMENT_MAX_CHARS or candidate_bytes > SEGMENT_MAX_BYTES
        ):
            segments.append(pending)
            pending = sentence
            pending_bytes = sentence_bytes
        else:
            pending += sentence
            pending_bytes = candidate_bytes
    if pending:
        segments.append(pending)
    return segments


# ---------------------------------------------------------------------------
# 火山 V4 签名（imagination 服务专用，service/region 参数化）
# ---------------------------------------------------------------------------


def build_volcano_v4_headers(
    *,
    method: str,
    host: str,
    canonical_query: str,
    body: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
) -> dict[str, str]:
    """构造火山 V4 签名请求头（排序头签名，密钥链自裸 SK 开始）。

    与图片 CV 客户端的差异：service/region 参数化，供音乐
    imagination/cn-beijing 使用；canonical_query 必须与实际发送的
    排序后查询串逐字一致。
    """
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    body_hash = hashlib.sha256(body).hexdigest()
    headers_to_sign = {
        "content-type": "application/json; charset=utf-8",
        "host": host,
        "x-content-sha256": body_hash,
        "x-date": timestamp,
    }
    signed_keys = sorted(headers_to_sign)
    canonical_headers = "".join(f"{key}:{headers_to_sign[key]}\n" for key in signed_keys)
    signed_headers = ";".join(signed_keys)
    canonical_request = (
        f"{method}\n/\n{canonical_query}\n{canonical_headers}\n{signed_headers}\n{body_hash}"
    )
    credential_scope = f"{timestamp[:8]}/{region}/{service}/request"
    string_to_sign = (
        f"HMAC-SHA256\n{timestamp}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    signing_key = reduce(
        lambda key, message: hmac.new(key, message.encode('utf-8'), hashlib.sha256).digest(),
        [timestamp[:8], region, service, "request"],
        secret_key.encode("utf-8"),
    )
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Content-Type": headers_to_sign["content-type"],
        "x-date": timestamp,
        "x-content-sha256": body_hash,
        "Authorization": (
            f"HMAC-SHA256 Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


# ---------------------------------------------------------------------------
# TTS 客户端
# ---------------------------------------------------------------------------


class VolcanoTTSClient:
    """火山 TTS SSE 客户端：一段正文一次完整会话，失败即整段失败。"""

    def __init__(
        self,
        *,
        api_key: str,
        resource_id: str = TTS_RESOURCE_ID_DEFAULT,
        speaker: str = TTS_SPEAKER_DEFAULT,
        speech_rate: int = TTS_SPEECH_RATE_DEFAULT,
        request_timeout_seconds: float = 45.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise MemoirAudioProviderError("TTS_CONFIG_INVALID", "缺少 TTS API Key")
        self._api_key = api_key
        self._resource_id = resource_id
        self._speaker = speaker
        self._speech_rate = speech_rate
        self._request_timeout_seconds = request_timeout_seconds
        self._transport = transport

    async def synthesize_segment(self, text: str) -> TTSSegmentResult:
        """合成单段：返回完整音频字节与官方 text_words；任何异常不携载荷。"""
        if not text or not text.strip():
            raise MemoirAudioProviderError("TTS_TEXT_INVALID", "待合成正文为空")
        request_id = uuid.uuid4().hex
        additions = json.dumps(
            {
                "context_texts": [TTS_ADDITIONS_INSTRUCTION],
                "max_length_to_filter_parenthesis": 0,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = {
            "user": {"uid": "memoir-runtime"},
            "req_params": {
                "text": text,
                "speaker": self._speaker,
                "audio_params": {
                    "format": TTS_FORMAT,
                    "sample_rate": TTS_SAMPLE_RATE,
                    "bit_rate": TTS_BIT_RATE,
                    "speech_rate": self._speech_rate,
                },
                "additions": additions,
            },
        }
        headers = {
            "X-Api-Key": self._api_key,
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Request-Id": request_id,
            "X-Control-Require-Usage-Tokens-Return": "*",
            "Content-Type": "application/json",
        }
        try:
            return await asyncio.wait_for(
                self._read_sse(payload, headers), self._request_timeout_seconds
            )
        except MemoirAudioProviderError:
            raise
        except asyncio.CancelledError:
            # 取消必须原样传播，不吞、不映射。
            raise
        except TimeoutError:
            logger.warning("Memoir TTS 请求超时，code=TTS_TIMEOUT")
            raise MemoirAudioProviderError("TTS_TIMEOUT", "TTS 请求超时") from None
        except httpx.TimeoutException:
            logger.warning("Memoir TTS 网络超时，code=TTS_NETWORK_TIMEOUT")
            raise MemoirAudioProviderError("TTS_NETWORK_TIMEOUT", "TTS 网络超时") from None
        except httpx.HTTPError:
            logger.warning("Memoir TTS 网络异常，code=TTS_NETWORK_ERROR")
            raise MemoirAudioProviderError("TTS_NETWORK_ERROR", "TTS 网络异常") from None

    async def _read_sse(self, payload: dict[str, Any], headers: dict[str, str]) -> TTSSegmentResult:
        """发送请求并解析 SSE 流；供应商原始响应不入日志。"""
        async with httpx.AsyncClient(
            transport=self._transport, timeout=self._request_timeout_seconds
        ) as client:
            async with client.stream("POST", TTS_API_URL, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    code = _provider_http_failure_code("TTS", response.status_code)
                    logger.warning("Memoir TTS HTTP 失败，code=%s，status=%s", code, response.status_code)
                    raise MemoirAudioProviderError(code, "TTS HTTP 请求失败")
                return await self._consume_stream(response)

    async def _consume_stream(self, response: httpx.Response) -> TTSSegmentResult:
        """逐帧消费 SSE：收集 352 音频帧直到 152 成功终态。"""
        chunks: list[bytes] = []
        pending_event: str | None = None
        pending_data: str | None = None

        def dispatch(event: str | None, data: str | None) -> TTSSegmentResult | None:
            """处理一个完整帧；返回非 None 表示会话终结。"""
            if data is None:
                return None
            try:
                event_id = int(event) if event is not None else None
            except ValueError:
                # 未知事件号：忽略（向前兼容），不算失败。
                return None
            if event_id == TTS_EVENT_AUDIO_DATA:
                self._consume_audio_frame(data, chunks)
                return None
            if event_id == TTS_EVENT_SESSION_FINISH:
                return self._finish_session(data, chunks)
            if event_id == TTS_EVENT_SESSION_CANCEL:
                logger.warning("Memoir TTS 会话被取消，code=TTS_SESSION_CANCELLED")
                raise MemoirAudioProviderError("TTS_SESSION_CANCELLED", "TTS 会话被取消")
            if event_id == TTS_EVENT_SESSION_FAILED:
                logger.warning("Memoir TTS 会话失败，code=TTS_SESSION_FAILED")
                raise MemoirAudioProviderError("TTS_SESSION_FAILED", "TTS 会话失败")
            # 351 句末等其他事件：忽略。
            return None

        async for raw_line in response.aiter_lines():
            line = raw_line.rstrip("\r")
            if not line:
                result = dispatch(pending_event, pending_data)
                if result is not None:
                    return result
                pending_event = None
                pending_data = None
                continue
            if line.startswith("event:"):
                pending_event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                pending_data = line[len("data:"):].strip()
        # EOF 前仍可能残留最后一帧（无结尾空行）。
        if pending_data is not None:
            result = dispatch(pending_event, pending_data)
            if result is not None:
                return result
        logger.warning("Memoir TTS 流终止但无终态帧，frames=%d，code=TTS_STREAM_TERMINATED", len(chunks))
        raise MemoirAudioProviderError("TTS_STREAM_TERMINATED", "TTS 流在终态前断开")

    @staticmethod
    def _consume_audio_frame(data: str, chunks: list[bytes]) -> None:
        """解析 352 帧：JSON 内 code=0 且 data 为合法 base64 音频。"""
        try:
            payload = json.loads(data)
            frame_code = payload.get("code")
            audio_b64 = payload.get("data")
            if (
                not isinstance(payload, dict)
                or frame_code != 0
                or not isinstance(audio_b64, str)
                or not audio_b64
            ):
                raise ValueError("bad frame")
            chunks.append(base64.b64decode(audio_b64, validate=True))
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("Memoir TTS 音频帧损坏，code=TTS_AUDIO_FRAME_INVALID")
            raise MemoirAudioProviderError("TTS_AUDIO_FRAME_INVALID", "TTS 音频帧损坏") from exc

    @staticmethod
    def _finish_session(data: str, chunks: list[bytes]) -> TTSSegmentResult:
        """处理 152 终态：code 必须 20000000、有音频、有真实 text_words。"""
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            logger.warning("Memoir TTS 终态帧解析失败，code=TTS_FINISH_CODE_INVALID")
            raise MemoirAudioProviderError("TTS_FINISH_CODE_INVALID", "TTS 终态帧非法") from exc
        if not isinstance(payload, dict) or payload.get("code") != TTS_SUCCESS_CODE:
            logger.warning("Memoir TTS 终态码非成功，code=TTS_FINISH_CODE_INVALID")
            raise MemoirAudioProviderError("TTS_FINISH_CODE_INVALID", "TTS 终态码非成功")
        usage = payload.get("usage")
        text_words = usage.get("text_words") if isinstance(usage, dict) else None
        # bool 是 int 子类，显式排除；用量缺失按失败处理，不得当 0。
        if isinstance(text_words, bool) or not isinstance(text_words, int) or text_words < 0:
            logger.warning("Memoir TTS 用量缺失，code=TTS_USAGE_MISSING")
            raise MemoirAudioProviderError("TTS_USAGE_MISSING", "TTS 用量缺失")
        if not chunks:
            logger.warning("Memoir TTS 空音频会话，words=%d，code=TTS_EMPTY_AUDIO", text_words)
            raise MemoirAudioProviderError("TTS_EMPTY_AUDIO", "TTS 会话未返回音频")
        return TTSSegmentResult(audio=b"".join(chunks), text_words=text_words)


# ---------------------------------------------------------------------------
# 音乐客户端
# ---------------------------------------------------------------------------


class VolcanoMusicClient:
    """火山 imagination 音乐客户端：显式 Action，提交与查询分离。

    轮询间隔由调用方控制（R7/R8 的节点预算负责节拍）；本客户端只做
    单次提交与单次查询。临时 AudioUrl 仅在内存快照中返回，不落任何
    持久化或日志。
    """

    def __init__(
        self,
        *,
        access_key: str,
        secret_key: str,
        action: str,
        duration_seconds: int = MUSIC_DURATION_SECONDS_DEFAULT,
        request_timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not access_key or not secret_key:
            raise MemoirAudioProviderError("MUSIC_CONFIG_INVALID", "缺少音乐 AK/SK")
        if action not in MUSIC_ALLOWED_ACTIONS:
            # Action 必须显式配置且只允许两个官方值，禁止自动切换计费产品。
            raise MemoirAudioProviderError("MUSIC_CONFIG_INVALID", "音乐 Action 非法")
        if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int) or duration_seconds <= 0:
            raise MemoirAudioProviderError("MUSIC_CONFIG_INVALID", "音乐时长非法")
        self._access_key = access_key
        self._secret_key = secret_key
        self._action = action
        self._duration_seconds = duration_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._transport = transport

    async def submit_generation(self) -> str:
        """提交 BGM 生成：固定提示词，返回官方 TaskID。"""
        body = {
            "Text": BGM_PROMPT_TEXT,
            "Version": MUSIC_MODEL_VERSION,
            "EnableInputRewrite": False,
            "Duration": self._duration_seconds,
        }
        result = await self._request(self._action, body)
        task_id = result.get("TaskID") if isinstance(result, dict) else None
        if not isinstance(task_id, str) or not task_id:
            logger.warning("Memoir 音乐提交未返回 TaskID，code=MUSIC_TASK_ID_MISSING")
            raise MemoirAudioProviderError("MUSIC_TASK_ID_MISSING", "音乐任务提交无 TaskID")
        # 日志只记安全枚举；TaskID 本身属于供应商侧标识，不入日志。
        logger.info("Memoir 音乐任务已提交，code=MUSIC_SUBMITTED")
        return task_id

    async def query_task(self, task_id: str) -> MusicTaskSnapshot:
        """查询音乐任务：0/1 等待、2 成功（AudioUrl 仅内存）、3 失败。"""
        if not task_id:
            raise MemoirAudioProviderError("MUSIC_TASK_ID_MISSING", "音乐 TaskID 为空")
        result = await self._request(MUSIC_QUERY_ACTION, {"TaskID": task_id})
        status = result.get("Status") if isinstance(result, dict) else None
        if status not in (MUSIC_STATUS_WAITING, MUSIC_STATUS_PROCESSING, MUSIC_STATUS_SUCCESS, MUSIC_STATUS_FAILED):
            logger.warning("Memoir 音乐任务状态非法，code=MUSIC_STATUS_INVALID")
            raise MemoirAudioProviderError("MUSIC_STATUS_INVALID", "音乐任务状态非法")
        if status == MUSIC_STATUS_SUCCESS:
            detail = result.get("SongDetail")
            audio_url = detail.get("AudioUrl") if isinstance(detail, dict) else None
            if not isinstance(audio_url, str) or not audio_url.startswith("https://"):
                logger.warning("Memoir 音乐成功但缺 AudioUrl，code=MUSIC_AUDIO_URL_MISSING")
                raise MemoirAudioProviderError("MUSIC_AUDIO_URL_MISSING", "音乐结果缺音频地址")
            logger.info("Memoir 音乐任务成功，code=MUSIC_SUCCESS")
            return MusicTaskSnapshot(status=status, audio_url=audio_url)
        # 0/1/3：等待或失败。FailureReason 原文不读不入日志。
        logger.info("Memoir 音乐任务状态更新，status=%s", status)
        return MusicTaskSnapshot(status=status, audio_url=None)

    async def _request(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        """签名并发送 imagination 请求；响应体不进日志与异常。"""
        body_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        canonical_query = f"Action={action}&Version={MUSIC_API_VERSION}"
        headers = build_volcano_v4_headers(
            method="POST",
            host=MUSIC_API_HOST,
            canonical_query=canonical_query,
            body=body_bytes,
            access_key=self._access_key,
            secret_key=self._secret_key,
            region=MUSIC_REGION,
            service=MUSIC_SERVICE,
        )
        url = f"{MUSIC_API_URL}?{canonical_query}"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._request_timeout_seconds
            ) as client:
                response = await client.post(url, content=body_bytes, headers=headers)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning("Memoir 音乐请求超时，code=MUSIC_TIMEOUT")
            raise MemoirAudioProviderError("MUSIC_TIMEOUT", "音乐请求超时") from None
        except httpx.TimeoutException:
            logger.warning("Memoir 音乐网络超时，code=MUSIC_NETWORK_TIMEOUT")
            raise MemoirAudioProviderError("MUSIC_NETWORK_TIMEOUT", "音乐网络超时") from None
        except httpx.HTTPError:
            logger.warning("Memoir 音乐网络异常，code=MUSIC_NETWORK_ERROR")
            raise MemoirAudioProviderError("MUSIC_NETWORK_ERROR", "音乐网络异常") from None
        if response.status_code != 200:
            code = _provider_http_failure_code("MUSIC", response.status_code)
            logger.warning("Memoir 音乐 HTTP 失败，code=%s，status=%s", code, response.status_code)
            raise MemoirAudioProviderError(code, "音乐 HTTP 请求失败")
        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning("Memoir 音乐响应解析失败，code=MUSIC_RESPONSE_INVALID")
            raise MemoirAudioProviderError("MUSIC_RESPONSE_INVALID", "音乐响应非法") from exc
        result = payload.get("Result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            logger.warning("Memoir 音乐响应缺 Result，code=MUSIC_RESPONSE_INVALID")
            raise MemoirAudioProviderError("MUSIC_RESPONSE_INVALID", "音乐响应缺 Result")
        return result


# 命令 runner 类型别名（转码模块复用签名风格，这里供测试注入参考）。
CommandRunner = Callable[[list[str]], Awaitable["tuple[int, bytes, bytes]"]]
