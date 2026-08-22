"""火山引擎视觉智能异步图像生成 Provider 适配器。

隐私铁律：prompt 文本、图片字节、临时 URL 都只在内存中流转，绝不写日志、
不进 trace/checkpoint。日志只允许出现成败状态码与受控错误码。

该 Provider 不经过 ModelGateway/PolicyEngine 的 LLM token 计量路径；按张计量
由 MemoirMediaService 写入 AgentModelUsage（image_count 列）完成。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Mapping
from typing import Protocol

import httpx

# 火山视觉智能 3.0 固定合同：先提交异步任务，再按同一 req_key 查询结果。
TEXT_TO_IMAGE_REQ_KEY = "high_aes_general_v30l_zt2i"
IMAGE_TO_IMAGE_REQ_KEY = "seededit_v3.0"
_SUBMIT_ACTION = "CVSync2AsyncSubmitTask"
_GET_RESULT_ACTION = "CVSync2AsyncGetResult"
_API_VERSION = "2022-08-31"
_SERVICE = "cv"
_PENDING_STATUSES = frozenset({"in_queue", "generating"})
_FAILED_STATUSES = frozenset({"not_found", "expired"})
_POLL_INTERVAL_SECONDS = 0.5


class VolcanoCVError(ValueError):
    """火山视觉任务失败的安全错误；只携带受控码，不携带响应正文。"""


def _safe_diagnostic_code(value: object) -> str:
    """只保留可安全写入日志的短错误码，拒绝消息正文和控制字符。"""
    if isinstance(value, bool):
        return "unavailable"
    if isinstance(value, int):
        return str(value)
    if (
        isinstance(value, str)
        and value
        and value.isascii()
        and len(value) <= 64
        and all(char.isalnum() or char in "_.-" for char in value)
    ):
        return value
    return "unavailable"


def _http_failure_code(status_code: int) -> str:
    """将 HTTP 状态映射为可检索、不可泄漏的受控错误码。"""
    return {
        400: "HTTP_400_PARAMETER",
        401: "HTTP_401_SIGNATURE",
        403: "HTTP_403_PERMISSION",
    }.get(status_code, "HTTP_4XX" if 400 <= status_code < 500 else "HTTP_ERROR")


def _response_diagnostics(
    response: httpx.Response,
) -> tuple[str, str, bool]:
    """提取状态排障所需的三个安全字段，不保留响应正文。"""
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return "unavailable", "unavailable", False
    if not isinstance(payload, dict):
        return "unavailable", "unavailable", False

    metadata = payload.get("ResponseMetadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    error = metadata.get("Error") or metadata.get("error")
    error = error if isinstance(error, dict) else {}
    provider_code = _safe_diagnostic_code(error.get("Code", error.get("code")))
    business_code = _safe_diagnostic_code(payload.get("code"))
    request_id_present = any(
        isinstance(candidate, str) and bool(candidate)
        for candidate in (
            payload.get("request_id"),
            payload.get("requestId"),
            metadata.get("RequestId"),
            metadata.get("request_id"),
        )
    )
    return provider_code, business_code, request_id_present


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def build_volcano_v4_authorization(
    *, method: str, canonical_uri: str, canonical_query: str,
    host: str, payload: bytes, access_key: str, secret_key: str,
    region: str, x_date: str,
) -> str:
    """构造火山引擎 V4 HMAC-SHA256 签名的 Authorization 头。

    x_date 形如 ``20260820T120000Z``（短日期为前 8 位）。签名算法与火山官方
    文档一致：canonical request -> string to sign -> 派生密钥链 -> 签名。
    """
    short_date = x_date[:8]
    payload_hash = _sha256_hex(payload)
    canonical_headers = (
        f"content-type:application/json\n"
        f"host:{host}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
    )
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_request = "\n".join((
        method, canonical_uri, canonical_query,
        canonical_headers, signed_headers, payload_hash,
    ))
    credential_scope = f"{short_date}/{region}/{_SERVICE}/request"
    string_to_sign = "\n".join((
        "HMAC-SHA256", x_date, credential_scope,
        _sha256_hex(canonical_request.encode("utf-8")),
    ))
    # 火山官方 V4 使用裸 secret key 作为首段密钥，不使用 AWS 风格前缀。
    k_date = _hmac_sha256(secret_key.encode("utf-8"), short_date)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, _SERVICE)
    k_signing = _hmac_sha256(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


class CVImageProvider(Protocol):
    """图像 Provider 最小接口：入参 prompt/参考图字节，出参统一为图片 bytes。"""

    def text_to_image(self, prompt: str) -> bytes: ...

    def image_to_image(self, prompt: str, reference: bytes) -> bytes: ...


class VolcanoCVClient:
    """真实火山视觉智能异步客户端，向上仍提供同步图片 bytes 接口。

    ``timeout_seconds`` 覆盖单张图片的提交、查询和网络重试总时长；仅网络错误
    与 5xx 有限重试，4xx 和业务失败立即终止，避免重复无效请求。
    """

    def __init__(
        self,
        *,
        access_key: str,
        secret_key: str,
        region: str = "cn-north-1",
        host: str = "visual.volcengineapi.com",
        timeout_seconds: float = 25.0,
        max_retries: int = 1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not access_key or not secret_key:
            # 凭证缺失属于部署配置错误；只报受控码，不回显任何凭证信息。
            raise VolcanoCVError("VOLCANO_CV_CREDENTIAL_MISSING")
        if timeout_seconds <= 0 or max_retries < 0:
            raise VolcanoCVError("VOLCANO_CV_CONFIG_INVALID")
        self._ak, self._sk = access_key, secret_key
        self._region, self._host = region, host
        self._timeout, self._max_retries = timeout_seconds, max_retries
        # transport 仅测试注入 MockTransport 用；生产保持 None 走真实网络。
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            trust_env=False,
            transport=self._transport,
        )

    @staticmethod
    def _remaining_seconds(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VolcanoCVError("VOLCANO_CV_TIMEOUT")
        return remaining

    async def _sleep_with_deadline(self, delay: float, deadline: float) -> None:
        await asyncio.sleep(min(delay, self._remaining_seconds(deadline)))

    async def _signed_post(
        self,
        client: httpx.AsyncClient,
        action: str,
        body: Mapping[str, object],
        timeout: float,
    ) -> httpx.Response:
        payload = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        canonical_query = f"Action={action}&Version={_API_VERSION}"
        x_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        authorization = build_volcano_v4_authorization(
            method="POST",
            canonical_uri="/",
            canonical_query=canonical_query,
            host=self._host,
            payload=payload,
            access_key=self._ak,
            secret_key=self._sk,
            region=self._region,
            x_date=x_date,
        )
        request = client.post(
            f"https://{self._host}/?{canonical_query}",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Date": x_date,
                "X-Content-Sha256": _sha256_hex(payload),
                "Authorization": authorization,
            },
            timeout=timeout,
        )
        # HTTPX 的标量 timeout 只约束各网络阶段；wait_for 才是整次请求墙钟上限。
        return await asyncio.wait_for(request, timeout=timeout)

    async def _post_action(
        self,
        client: httpx.AsyncClient,
        action: str,
        body: Mapping[str, object],
        deadline: float,
    ) -> dict[str, object]:
        last_error = VolcanoCVError("VOLCANO_CV_REQUEST_FAILED")
        failure_status: int | None = None
        provider_code = "unavailable"
        business_code = "unavailable"
        request_id_present = False
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._signed_post(
                    client,
                    action,
                    body,
                    min(self._timeout, self._remaining_seconds(deadline)),
                )
                failure_status = response.status_code
                if response.status_code >= 500:
                    provider_code, business_code, request_id_present = (
                        _response_diagnostics(response)
                    )
                    last_error = VolcanoCVError(
                        "VOLCANO_CV_SERVER_UNAVAILABLE"
                    )
                elif response.status_code != 200:
                    # 4xx 是签名、权限或参数错误；重复请求不会恢复。
                    provider_code, business_code, request_id_present = (
                        _response_diagnostics(response)
                    )
                    last_error = VolcanoCVError(
                        _http_failure_code(response.status_code)
                    )
                    break
                else:
                    try:
                        payload = response.json()
                    except (TypeError, ValueError):
                        last_error = VolcanoCVError(
                            "VOLCANO_CV_RESPONSE_INVALID"
                        )
                        break
                    if isinstance(payload, dict):
                        return payload
                    last_error = VolcanoCVError(
                        "VOLCANO_CV_RESPONSE_INVALID"
                    )
                    break
            except (TimeoutError, httpx.TimeoutException):
                # 异常可能携带 URL；只保留受控码，禁止详情进入日志。
                last_error = VolcanoCVError("VOLCANO_CV_TIMEOUT")
            except httpx.TransportError:
                last_error = VolcanoCVError("VOLCANO_CV_TRANSPORT_ERROR")
            except VolcanoCVError as error:
                last_error = error
                break
            if attempt >= self._max_retries:
                break
            try:
                await self._sleep_with_deadline(0.5 * (attempt + 1), deadline)
            except VolcanoCVError as error:
                last_error = error
                break
        error_code = last_error.args[0] if last_error.args else "VOLCANO_CV_REQUEST_FAILED"
        logging.warning(
            "火山视觉任务调用失败 action=%s status=%s code=%s "
            "provider_code=%s business_code=%s request_id_present=%s",
            action,
            failure_status if failure_status is not None else "unavailable",
            error_code,
            provider_code,
            business_code,
            request_id_present,
        )
        raise last_error

    @staticmethod
    def _require_business_success(
        payload: dict[str, object],
        action: str,
    ) -> None:
        code = payload.get("code")
        if not isinstance(code, bool) and code in (10000, 0):
            return
        request_id_present = isinstance(payload.get("request_id"), str) and bool(
            payload.get("request_id")
        )
        logging.warning(
            "火山视觉任务业务失败 action=%s business_code=%s "
            "request_id_present=%s",
            action,
            _safe_diagnostic_code(code),
            request_id_present,
        )
        raise VolcanoCVError("VOLCANO_CV_BUSINESS_FAILED")

    async def _submit_task(
        self,
        client: httpx.AsyncClient,
        body: Mapping[str, object],
        deadline: float,
    ) -> str:
        payload = await self._post_action(client, _SUBMIT_ACTION, body, deadline)
        self._require_business_success(payload, _SUBMIT_ACTION)
        result = payload.get("data")
        result = result if isinstance(result, dict) else {}
        task_id = result.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise VolcanoCVError("VOLCANO_CV_TASK_ID_MISSING")
        return task_id

    @staticmethod
    def _decode_image(result: dict[str, object]) -> bytes:
        binary_list = result.get("binary_data_base64")
        first = binary_list[0] if isinstance(binary_list, list) and binary_list else None
        if not isinstance(first, str) or not first:
            raise VolcanoCVError("VOLCANO_CV_EMPTY_RESULT")
        try:
            image = base64.b64decode(first, validate=True)
        except ValueError as error:
            raise VolcanoCVError("VOLCANO_CV_RESPONSE_INVALID") from error
        if not image:
            raise VolcanoCVError("VOLCANO_CV_EMPTY_RESULT")
        return image

    async def _poll_task(
        self,
        client: httpx.AsyncClient,
        req_key: str,
        task_id: str,
        deadline: float,
    ) -> bytes:
        query_body = {"req_key": req_key, "task_id": task_id}
        while True:
            payload = await self._post_action(
                client,
                _GET_RESULT_ACTION,
                query_body,
                deadline,
            )
            self._require_business_success(payload, _GET_RESULT_ACTION)
            result = payload.get("data")
            result = result if isinstance(result, dict) else {}
            status = result.get("status")
            if status == "done":
                return self._decode_image(result)
            if isinstance(status, str) and status in _FAILED_STATUSES:
                raise VolcanoCVError(f"VOLCANO_CV_TASK_{status.upper()}")
            if not isinstance(status, str) or status not in _PENDING_STATUSES:
                raise VolcanoCVError("VOLCANO_CV_TASK_STATUS_INVALID")
            await self._sleep_with_deadline(_POLL_INTERVAL_SECONDS, deadline)

    async def _generate_async(self, body: Mapping[str, object]) -> bytes:
        req_key = body.get("req_key")
        if not isinstance(req_key, str) or not req_key:
            raise VolcanoCVError("VOLCANO_CV_REQ_KEY_INVALID")
        deadline = time.monotonic() + self._timeout
        async with self._client() as client:
            task_id = await self._submit_task(client, body, deadline)
            return await self._poll_task(client, req_key, task_id, deadline)

    def _generate(self, body: Mapping[str, object]) -> bytes:
        return asyncio.run(self._generate_async(body))

    def text_to_image(self, prompt: str) -> bytes:
        """通用 3.0 文生图；提交体不伪造空参考图或 URL 返回参数。"""
        return self._generate(
            {
                "req_key": TEXT_TO_IMAGE_REQ_KEY,
                "prompt": prompt,
            }
        )

    def image_to_image(self, prompt: str, reference: bytes) -> bytes:
        """SeedEdit 3.0 图生图；参考图只在请求内以单元素 Base64 数组传输。"""
        return self._generate(
            {
                "req_key": IMAGE_TO_IMAGE_REQ_KEY,
                "prompt": prompt,
                "binary_data_base64": [
                    base64.b64encode(reference).decode("ascii")
                ],
            }
        )


class MockCVClient:
    """开发/测试用 Mock Provider：同接口、确定性输出、内存记录调用。

    记录的 prompt 只留在进程内存供测试断言，绝不写日志。fail_prompts 中的
    prompt 触发固定失败，用于验证单张失败降级路径。
    """

    def __init__(
        self, *, text_image: bytes = b"\x89PNG\r\n\x1a\n-MOCK-TEXT",
        image_image: bytes = b"\x89PNG\r\n\x1a\n-MOCK-EDIT",
        fail_prompts: frozenset[str] = frozenset(),
    ) -> None:
        self._text_image, self._image_image = text_image, image_image
        self._fail_prompts = set(fail_prompts)
        self.text_prompts: list[str] = []
        self.image_prompts: list[str] = []

    def text_to_image(self, prompt: str) -> bytes:
        self.text_prompts.append(prompt)
        if prompt in self._fail_prompts:
            raise VolcanoCVError("VOLCANO_CV_MOCK_FAILURE")
        return self._text_image

    def image_to_image(self, prompt: str, reference: bytes) -> bytes:
        self.image_prompts.append(prompt)
        if prompt in self._fail_prompts:
            raise VolcanoCVError("VOLCANO_CV_MOCK_FAILURE")
        return self._image_image
