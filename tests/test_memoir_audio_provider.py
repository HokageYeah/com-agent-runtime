"""M8 R6 音频 Provider（火山 TTS SSE / 音乐 GenBGM）测试。

全部使用 httpx.MockTransport 模拟 HTTP，不调用真实付费接口。
隐私断言：正文、音频字节、TaskID 响应体、临时 URL 绝不进入日志或异常文本。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import httpx
import pytest

from app.services.memoir.memoir_audio_provider import (
    BGM_PROMPT_TEXT,
    MUSIC_API_VERSION,
    MUSIC_MODEL_VERSION,
    TTS_ADDITIONS_INSTRUCTION,
    TTS_EVENT_AUDIO_DATA,
    TTS_EVENT_SESSION_CANCEL,
    TTS_EVENT_SESSION_FAILED,
    TTS_EVENT_SESSION_FINISH,
    TTS_SUCCESS_CODE,
    MemoirAudioProviderError,
    MusicTaskSnapshot,
    VolcanoMusicClient,
    VolcanoTTSClient,
    split_narration_segments,
)

MP3_FRAME_A = b"\xff\xfb\x90\x00-mock-mp3-a"
MP3_FRAME_B = b"\xff\xfb\x90\x00-mock-mp3-b"


def _sse(*events: tuple[int, dict[str, Any]]) -> bytes:
    """构造官方 SSE 响应字节流：event/data 成对帧。"""
    lines: list[str] = []
    for event, payload in events:
        lines.append(f"event: {event}")
        lines.append("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        lines.append("")
    return ("\n".join(lines)).encode("utf-8")


def _audio_event(data: bytes) -> tuple[int, dict[str, Any]]:
    return TTS_EVENT_AUDIO_DATA, {"code": 0, "data": base64.b64encode(data).decode("ascii")}


def _finish_event(*, code: int = TTS_SUCCESS_CODE, text_words: int | None = 11) -> tuple[int, dict[str, Any]]:
    payload: dict[str, Any] = {"code": code, "message": "OK", "data": None}
    if text_words is not None:
        payload["usage"] = {"text_words": text_words}
    return TTS_EVENT_SESSION_FINISH, payload


def _tts_client(handler, **overrides: Any) -> VolcanoTTSClient:
    defaults: dict[str, Any] = {
        "api_key": "tts-key-test",
        "transport": httpx.MockTransport(handler),
        "request_timeout_seconds": 5.0,
    }
    defaults.update(overrides)
    return VolcanoTTSClient(**defaults)


def _music_client(handler, **overrides: Any) -> VolcanoMusicClient:
    defaults: dict[str, Any] = {
        "access_key": "AKTEST",
        "secret_key": "SKTEST",
        "action": "GenBGM",
        "transport": httpx.MockTransport(handler),
        "request_timeout_seconds": 5.0,
    }
    defaults.update(overrides)
    return VolcanoMusicClient(**defaults)


# ---------------------------------------------------------------------------
# 分段规则：按句无损、≤200 Unicode 字符、≤900 UTF-8 字节
# ---------------------------------------------------------------------------


def test_split_narration_segments_is_lossless_and_bounded() -> None:
    """分段必须无损（拼接等于原文）且每段同时满足字符与字节上限。"""
    cases = [
        "我们在海边的傍晚散步，海风很轻。",
        "短。句！测试？好吗；嗯…",
        "无标点" * 300,
        "很长的句子" * 80 + "。再一句。",
        "emoji😊与中文混排" * 60,
        "Mixed 中英文 punctuation! ending.",
    ]
    for text in cases:
        segments = split_narration_segments(text)
        assert segments, text
        assert "".join(segments) == text
        for segment in segments:
            assert segment
            assert len(segment) <= 200
            assert len(segment.encode("utf-8")) <= 900


def test_split_prefers_sentence_boundaries() -> None:
    """有句读的文本优先在句边界分段，短句可合并同段。"""
    text = "第一句话。" + "第二句话。" + "第三句话。"
    segments = split_narration_segments(text)
    assert "".join(segments) == text
    # 三句共 18 字符，应合并为一段（无谓拆分即浪费一次计费请求）。
    assert len(segments) == 1


def test_split_long_sentence_falls_back_to_char_boundary() -> None:
    """无标点长句按字符边界硬切，不丢字、不超限。"""
    text = "海" * 450
    segments = split_narration_segments(text)
    assert "".join(segments) == text
    assert len(segments) >= 3
    assert all(len(s) <= 200 and len(s.encode("utf-8")) <= 900 for s in segments)


def test_split_empty_text_returns_no_segments() -> None:
    """空正文不产生任何分段（由上层决定是否需要旁白）。"""
    assert split_narration_segments("") == []
    assert split_narration_segments("   ") == []


# ---------------------------------------------------------------------------
# TTS：SSE 帧解析、成功终态、协议合同
# ---------------------------------------------------------------------------


def test_tts_success_reads_frames_and_usage() -> None:
    """成功路径：收集 352 音频帧，152 code=20000000 时返回音频与实际 text_words。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_sse(
                _audio_event(MP3_FRAME_A),
                (351, {"code": 0, "data": None, "sentence": {"text": "句"}}),
                _audio_event(MP3_FRAME_B),
                _finish_event(text_words=42),
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    result = asyncio.run(_tts_client(handler).synthesize_segment("我们在海边的傍晚散步。"))

    assert result.audio == MP3_FRAME_A + MP3_FRAME_B
    assert result.text_words == 42
    # 协议合同：鉴权头、资源 ID、随机请求 ID、用量返回标记。
    request = requests[0]
    assert request.url.host == "openspeech.bytedance.com"
    assert str(request.url.path) == "/api/v3/tts/unidirectional/sse"
    assert request.headers["X-Api-Key"] == "tts-key-test"
    assert request.headers["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert request.headers["X-Control-Require-Usage-Tokens-Return"] == "*"
    assert request.headers["X-Api-Request-Id"]


def test_tts_request_body_freezes_voice_and_audio_params() -> None:
    """body 固定：req_params.audio_params=mp3/24000/64000/-10、温柔淑女音色、
    additions 为 JSON 字符串（固定普通话指令 + 保留括号）。"""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            content=_sse(_audio_event(MP3_FRAME_A), _finish_event()),
            headers={"Content-Type": "text/event-stream"},
        )

    asyncio.run(_tts_client(handler).synthesize_segment("正文一段。"))

    body = bodies[0]
    req_params = body["req_params"]
    assert req_params["text"] == "正文一段。"
    assert req_params["speaker"] == "zh_female_wenroushunv_uranus_bigtts"
    assert req_params["audio_params"] == {
        "format": "mp3", "sample_rate": 24000, "bit_rate": 64000, "speech_rate": -10,
    }
    additions = json.loads(req_params["additions"])
    assert additions["context_texts"] == [TTS_ADDITIONS_INSTRUCTION]
    assert additions["max_length_to_filter_parenthesis"] == 0


def test_tts_request_id_is_random_per_request() -> None:
    """X-Api-Request-Id 每次请求随机生成；相同正文两次提交是独立请求。"""
    request_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_ids.append(request.headers["X-Api-Request-Id"])
        return httpx.Response(
            200,
            content=_sse(_audio_event(MP3_FRAME_A), _finish_event()),
            headers={"Content-Type": "text/event-stream"},
        )

    client = _tts_client(handler)
    same_text = "相同正文一段。"
    first = asyncio.run(client.synthesize_segment(same_text))
    second = asyncio.run(client.synthesize_segment(same_text))
    assert len(request_ids) == 2
    assert request_ids[0] != request_ids[1]
    # 相同正文不共享结果：两次独立合成（不同 scene 的资产各自独立由上层落键）。
    assert first.audio == second.audio == MP3_FRAME_A


def test_tts_bad_base64_frame_fails_whole_segment() -> None:
    """坏 Base64 帧 = 整段失败，不返回半段音频。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(
                _audio_event(MP3_FRAME_A),
                (TTS_EVENT_AUDIO_DATA, {"code": 0, "data": "!!!not-base64!!!"}),
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    with pytest.raises(MemoirAudioProviderError) as excinfo:
        asyncio.run(_tts_client(handler).synthesize_segment("正文。"))
    assert excinfo.value.code == "TTS_AUDIO_FRAME_INVALID"


def test_tts_stream_terminated_without_finish_event() -> None:
    """断流（EOF 前无 152 终态）= 失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(_audio_event(MP3_FRAME_A)),
            headers={"Content-Type": "text/event-stream"},
        )

    with pytest.raises(MemoirAudioProviderError) as excinfo:
        asyncio.run(_tts_client(handler).synthesize_segment("正文。"))
    assert excinfo.value.code == "TTS_STREAM_TERMINATED"


def test_tts_session_failed_and_cancelled_events_fail() -> None:
    """153=SessionFailed、151=SessionCancel 均为失败终态。"""
    for event, expected_code in (
        (TTS_EVENT_SESSION_FAILED, "TTS_SESSION_FAILED"),
        (TTS_EVENT_SESSION_CANCEL, "TTS_SESSION_CANCELLED"),
    ):
        def handler(request: httpx.Request, event: int = event) -> httpx.Response:
            return httpx.Response(
                200,
                content=_sse((event, {"code": 3000001, "message": "err", "data": None})),
                headers={"Content-Type": "text/event-stream"},
            )

        with pytest.raises(MemoirAudioProviderError) as excinfo:
            asyncio.run(_tts_client(handler).synthesize_segment("正文。"))
        assert excinfo.value.code == expected_code


def test_tts_empty_audio_fails() -> None:
    """152 成功终态但零音频帧 = 空音频失败，不发布空资产。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(_finish_event()),
            headers={"Content-Type": "text/event-stream"},
        )

    with pytest.raises(MemoirAudioProviderError) as excinfo:
        asyncio.run(_tts_client(handler).synthesize_segment("正文。"))
    assert excinfo.value.code == "TTS_EMPTY_AUDIO"


def test_tts_finish_bad_code_or_missing_usage_fails() -> None:
    """152 非 20000000 或缺 usage.text_words 均失败（缺用量不得当 0）。"""
    for event, expected_code in (
        (_finish_event(code=3005001), "TTS_FINISH_CODE_INVALID"),
        (_finish_event(text_words=None), "TTS_USAGE_MISSING"),
    ):
        def handler(request: httpx.Request, event: tuple = event) -> httpx.Response:
            return httpx.Response(
                200,
                content=_sse(_audio_event(MP3_FRAME_A), event),
                headers={"Content-Type": "text/event-stream"},
            )

        with pytest.raises(MemoirAudioProviderError) as excinfo:
            asyncio.run(_tts_client(handler).synthesize_segment("正文。"))
        assert excinfo.value.code == expected_code


def test_tts_http_error_maps_safe_code_without_body() -> None:
    """HTTP 非 200 只映射安全枚举，响应正文不进异常文本。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "secret-token-leak"})

    with pytest.raises(MemoirAudioProviderError) as excinfo:
        asyncio.run(_tts_client(handler).synthesize_segment("正文。"))
    assert excinfo.value.code == "TTS_HTTP_401"
    assert "secret-token-leak" not in str(excinfo.value)


def test_tts_cancellation_propagates_without_partial_result() -> None:
    """取消必须原样传播 CancelledError，且不产出半段结果。"""

    async def scenario() -> None:
        release = asyncio.Event()

        async def hanging_stream():
            yield _sse(_audio_event(MP3_FRAME_A))
            await release.wait()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=hanging_stream(),
                headers={"Content-Type": "text/event-stream"},
            )

        task = asyncio.ensure_future(
            _tts_client(handler).synthesize_segment("正文。")
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

    asyncio.run(scenario())


def test_tts_config_rejects_missing_api_key() -> None:
    """缺 API Key 属部署配置错误：受控码，不回显凭证。"""
    with pytest.raises(MemoirAudioProviderError) as excinfo:
        VolcanoTTSClient(api_key="")
    assert excinfo.value.code == "TTS_CONFIG_INVALID"


# ---------------------------------------------------------------------------
# 音乐：显式 Action、提交/查询状态、签名合同
# ---------------------------------------------------------------------------


def test_music_submit_contract_and_signature() -> None:
    """GenBGM 提交：固定 Text/Version/Duration/EnableInputRewrite，imagination 签名。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["action"] = str(request.url.params["Action"])
        captured["version"] = str(request.url.params["Version"])
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"Result": {"TaskID": "task-music-1"}})

    task_id = asyncio.run(_music_client(handler).submit_generation())

    assert task_id == "task-music-1"
    assert captured["action"] == "GenBGM"
    assert captured["version"] == MUSIC_API_VERSION == "2024-08-12"
    assert captured["body"] == {
        "Text": BGM_PROMPT_TEXT,
        "Version": MUSIC_MODEL_VERSION,
        "Duration": 60,
        "EnableInputRewrite": False,
    }
    # 音乐签名固定 Region cn-beijing / Service imagination，不沿用图片默认。
    authorization = captured["authorization"]
    assert authorization.startswith("HMAC-SHA256 Credential=AKTEST/")
    assert "/cn-beijing/imagination/request" in authorization
    # 固定提示词不含任何私人正文占位符（按作品不变）。
    assert "{" not in BGM_PROMPT_TEXT


def test_music_action_is_explicit_without_fallback() -> None:
    """Action 只允许显式 GenBGM/GenBGMForTime，非法值拒绝、不自动切换。"""
    with pytest.raises(MemoirAudioProviderError) as excinfo:
        _music_client(lambda request: httpx.Response(200), action="GenSong")
    assert excinfo.value.code == "MUSIC_CONFIG_INVALID"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url.params["Action"]) == "GenBGMForTime"
        return httpx.Response(200, json={"Result": {"TaskID": "task-2"}})

    task_id = asyncio.run(_music_client(handler, action="GenBGMForTime").submit_generation())
    assert task_id == "task-2"


def test_music_query_status_matrix() -> None:
    """QuerySong 状态矩阵：0/1 等待、2 成功带 AudioUrl（仅内存）、3 失败无 URL。"""
    for status, expect_url in ((0, None), (1, None), (3, None)):
        def handler(request: httpx.Request, status: int = status) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            assert body == {"TaskID": "task-music-1"}
            return httpx.Response(
                200, json={"Result": {"Status": status}},
            )

        snapshot = asyncio.run(_music_client(handler).query_task("task-music-1"))
        assert snapshot == MusicTaskSnapshot(status=status, audio_url=None)
        assert expect_url is None

    def success_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "Result": {
                "Status": 2,
                "SongDetail": {"AudioUrl": "https://music.example.mock/audio.mp3", "Duration": 60},
            },
        })

    snapshot = asyncio.run(_music_client(success_handler).query_task("task-music-1"))
    assert snapshot.status == 2
    assert snapshot.audio_url == "https://music.example.mock/audio.mp3"


def test_music_query_rejects_invalid_status_and_missing_url() -> None:
    """非法状态值 / 成功但缺 AudioUrl / 提交缺 TaskID 都是受控失败。"""
    cases: list[tuple[httpx.Response, str]] = [
        (httpx.Response(200, json={"Result": {"Status": 9}}), "MUSIC_STATUS_INVALID"),
        (
            httpx.Response(200, json={"Result": {"Status": 2, "SongDetail": {}}}),
            "MUSIC_AUDIO_URL_MISSING",
        ),
        (
            httpx.Response(200, json={"Result": {"Status": 2}}),
            "MUSIC_AUDIO_URL_MISSING",
        ),
        (
            httpx.Response(200, json={"Result": {"TaskID": ""}}),
            "MUSIC_TASK_ID_MISSING",
        ),
    ]
    for response, expected_code in cases:
        def handler(request: httpx.Request, response: httpx.Response = response) -> httpx.Response:
            return response

        client = _music_client(handler)
        with pytest.raises(MemoirAudioProviderError) as excinfo:
            if expected_code == "MUSIC_TASK_ID_MISSING":
                asyncio.run(client.submit_generation())
            else:
                asyncio.run(client.query_task("task-music-1"))
        assert excinfo.value.code == expected_code


def test_music_http_error_maps_safe_code() -> None:
    """音乐 HTTP 403 只映射安全枚举，正文不进异常。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ResponseMetadata": {"Error": {"Code": "AccessDenied"}}})

    with pytest.raises(MemoirAudioProviderError) as excinfo:
        asyncio.run(_music_client(handler).submit_generation())
    assert excinfo.value.code == "MUSIC_HTTP_403"
    assert "AccessDenied" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# 隐私铁律：正文/音频/TaskID/URL 不入日志
# ---------------------------------------------------------------------------


def test_provider_failures_never_log_sensitive_payload(caplog: pytest.LogCaptureFixture) -> None:
    """失败日志只允许安全枚举与计数，禁止正文、TaskID、URL、音频字节。"""
    secret_task_response = "task-secret-value"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if "TaskID" in body:
            return httpx.Response(200, json={"Result": {"Status": 3}})
        return httpx.Response(
            200,
            content=_sse(
                _audio_event(MP3_FRAME_A),
                (TTS_EVENT_SESSION_FAILED, {"code": 3000001, "message": "正文泄露测试"}),
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    with caplog.at_level(logging.DEBUG):
        client = _music_client(handler)
        asyncio.run(client.query_task(secret_task_response))
        with pytest.raises(MemoirAudioProviderError):
            asyncio.run(_tts_client(handler).synthesize_segment("私密正文内容。"))

    dumped = "\n".join(record.getMessage() for record in caplog.records)
    assert "私密正文内容" not in dumped
    assert secret_task_response not in dumped
    assert base64.b64encode(MP3_FRAME_A).decode("ascii") not in dumped
