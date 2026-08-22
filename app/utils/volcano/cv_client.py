"""火山引擎 CVProcess 图像生成 Provider 适配器。

隐私铁律：prompt 文本、图片字节、临时 URL 都只在内存中流转，绝不写日志、
不进 trace/checkpoint。日志只允许出现成败状态码与受控错误码。

该 Provider 不经过 ModelGateway/PolicyEngine 的 LLM token 计量路径；按张计量
由 MemoirMediaService 写入 AgentModelUsage（image_count 列）完成。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Protocol

import httpx

# CVProcess 固定契约：文生图 general_v30 / 图生图 seededit-3.0（D1 冻结）。
TEXT_TO_IMAGE_REQ_KEY = "general_v30"
IMAGE_TO_IMAGE_REQ_KEY = "seededit-3.0"
_API_ACTION = "CVProcess"
_API_VERSION = "2022-08-31"
_SERVICE = "cv"


class VolcanoCVError(ValueError):
    """CVProcess 调用失败的安全错误；只携带受控错误码，不携带响应正文。"""


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
    """真实火山 CVProcess 客户端（开发期默认不被测试触达，测试用 Mock）。

    超时与有限重试：单次请求超时 timeout_seconds，仅对网络错误/5xx 重试
    max_retries 次；4xx 业务失败不重试（重试也不会成功，只白烧钱）。
    """

    def __init__(
        self, *, access_key: str, secret_key: str,
        region: str = "cn-north-1", host: str = "visual.volcengineapi.com",
        timeout_seconds: float = 25.0, max_retries: int = 1,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not access_key or not secret_key:
            # 凭证缺失属于部署配置错误；只报受控码，不回显任何凭证信息。
            raise VolcanoCVError("VOLCANO_CV_CREDENTIAL_MISSING")
        self._ak, self._sk = access_key, secret_key
        self._region, self._host = region, host
        self._timeout, self._max_retries = timeout_seconds, max_retries
        # transport 仅测试注入 MockTransport 用；生产保持 None 走真实网络。
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self._timeout, trust_env=False, transport=self._transport,
        )

    def _post_cvprocess(self, body: dict[str, object]) -> dict[str, object]:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        canonical_query = f"Action={_API_ACTION}&Version={_API_VERSION}"
        x_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        authorization = build_volcano_v4_authorization(
            method="POST", canonical_uri="/", canonical_query=canonical_query,
            host=self._host, payload=payload, access_key=self._ak,
            secret_key=self._sk, region=self._region, x_date=x_date,
        )
        last_error: Exception | None = None
        failure_status: int | None = None
        provider_code = "unavailable"
        business_code = "unavailable"
        request_id_present = False
        with self._client() as client:
            for attempt in range(self._max_retries + 1):
                try:
                    response = client.post(
                        f"https://{self._host}/?{canonical_query}",
                        content=payload,
                        headers={
                            "Content-Type": "application/json",
                            "X-Date": x_date,
                            "X-Content-Sha256": _sha256_hex(payload),
                            "Authorization": authorization,
                        },
                    )
                    failure_status = response.status_code
                    if response.status_code >= 500:
                        last_error = VolcanoCVError("VOLCANO_CV_SERVER_UNAVAILABLE")
                    elif response.status_code != 200:
                        # 4xx 是签名、权限或参数错误；重复请求不会恢复，也可能重复计费。
                        provider_code, business_code, request_id_present = (
                            _response_diagnostics(response)
                        )
                        last_error = VolcanoCVError(
                            _http_failure_code(response.status_code)
                        )
                        break
                    else:
                        data = response.json()
                        if not isinstance(data, dict):
                            last_error = VolcanoCVError("VOLCANO_CV_RESPONSE_INVALID")
                            break
                        return data
                except httpx.TimeoutException:
                    # 超时异常可能携带 URL；统一压缩成受控码，禁止详情进入日志。
                    last_error = VolcanoCVError("VOLCANO_CV_TIMEOUT")
                except httpx.TransportError:
                    # 传输异常可能携带 URL；统一压缩成受控码，禁止详情进入日志。
                    last_error = VolcanoCVError("VOLCANO_CV_TRANSPORT_ERROR")
                if attempt >= self._max_retries:
                    break
                time.sleep(0.5 * (attempt + 1))
        error_code = (
            last_error.args[0]
            if isinstance(last_error, VolcanoCVError) and last_error.args
            else "VOLCANO_CV_REQUEST_FAILED"
        )
        logging.warning(
            "火山 CVProcess 调用失败 status=%s code=%s provider_code=%s "
            "business_code=%s request_id_present=%s",
            failure_status if failure_status is not None else "unavailable",
            error_code,
            provider_code,
            business_code,
            request_id_present,
        )
        raise VolcanoCVError("VOLCANO_CV_REQUEST_FAILED")

    def _generate(self, body: dict[str, object]) -> bytes:
        data = self._post_cvprocess(body)
        if data.get("code") not in (10000, 0):
            raise VolcanoCVError("VOLCANO_CV_BUSINESS_FAILED")
        result = data.get("data")
        result = result if isinstance(result, dict) else {}
        binary_list = result.get("binary_data_base64")
        if isinstance(binary_list, list) and binary_list:
            first = binary_list[0]
            if isinstance(first, str) and first:
                return base64.b64decode(first)
        # 临时 URL 只在 adapter 内部当日转存为 bytes，绝不向调用方或日志外泄。
        urls = result.get("image_urls")
        if isinstance(urls, list) and urls and isinstance(urls[0], str) and urls[0].startswith("https://"):
            with self._client() as client:
                download = client.get(urls[0])
            if download.status_code == 200 and download.content:
                return download.content
        raise VolcanoCVError("VOLCANO_CV_EMPTY_RESULT")

    def text_to_image(self, prompt: str) -> bytes:
        """文生图：req_key 冻结为 general_v30。"""
        return self._generate({
            "req_key": TEXT_TO_IMAGE_REQ_KEY,
            "prompt": prompt,
            "binary_data_base64": [],
            "return_url": True,
        })

    def image_to_image(self, prompt: str, reference: bytes) -> bytes:
        """图生图：req_key 冻结为 seededit-3.0，参考图走 binary_data_base64。"""
        return self._generate({
            "req_key": IMAGE_TO_IMAGE_REQ_KEY,
            "prompt": prompt,
            "binary_data_base64": [base64.b64encode(reference).decode("ascii")],
            "return_url": True,
        })


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
