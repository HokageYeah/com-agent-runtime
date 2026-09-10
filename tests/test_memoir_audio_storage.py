"""M8 R6 音频存储（私有 OSS 上传 / scope / 转码 / 安全下载）测试。

不访问真实 OSS、不写真实凭据；ffmpeg 用可注入 fake runner 与真实二进制
冒烟（缺失时 skip）双重覆盖。隐私断言：官方临时 URL 不进任何输出。
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import socket
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import Settings, validate_memoir_audio_settings
from app.services.memoir.memoir_audio_storage import (
    AUDIO_ROLE_NARRATOR,
    AliyunAudioOSSUploader,
    AudioTranscoder,
    MemoirAudioStorageError,
    SecureAudioDownloader,
    build_audio_object_key,
    compute_audio_scope,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "memory_playback_shared_v2.json"
PUBLIC_HOST = "media.example-music.mock"
PUBLIC_IP = "93.184.216.34"  # is_global=True 的公网地址，仅用于注入 resolver
PRIVATE_IP = "10.0.0.5"


def _fixture() -> dict[str, Any]:
    """读取两仓冻结契约 fixture（只读）。"""
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _scope_inputs(vector: dict[str, Any]) -> dict[str, Any]:
    return {
        "business_id": vector["businessId"],
        "archive_id": vector["archiveId"],
        "run_id": vector["runId"],
        "generation_epoch": vector["generationEpoch"],
    }


# ---------------------------------------------------------------------------
# scope HMAC：与冻结 fixture 向量逐字一致
# ---------------------------------------------------------------------------


def test_scope_hmac_matches_all_frozen_vectors() -> None:
    """主向量 + 全部扩展向量：canonical JSON 数组 UTF-8 无空白、非 ASCII 不转义。"""
    scope = _fixture()["scopeHmac"]
    key = scope["keyUtf8"]
    vectors = [scope["main"], *scope["vectors"]]
    assert len(vectors) >= 5
    for vector in vectors:
        digest = compute_audio_scope(key, **_scope_inputs(vector))
        assert digest == vector["digestHex"]
        # canonical 输入由实现侧按规则重建，不信任 fixture 的字符串以外的形状。
        assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_scope_rejects_invalid_epoch_and_empty_key() -> None:
    """epoch 必须整数、key 不得为空：以受控错误拒绝，不降级为弱摘要。"""
    with pytest.raises(MemoirAudioStorageError) as excinfo:
        compute_audio_scope("", business_id="b", archive_id="a", run_id="r", generation_epoch=1)
    assert excinfo.value.code == "AUDIO_SCOPE_INVALID"
    with pytest.raises(MemoirAudioStorageError):
        compute_audio_scope(  # type: ignore[arg-type]
            "key", business_id="b", archive_id="a", run_id="r", generation_epoch="1"
        )


# ---------------------------------------------------------------------------
# object_key：角色前缀 + scope 目录 + 不透明随机名
# ---------------------------------------------------------------------------


def test_object_key_shape_matches_fixture_contract() -> None:
    """object_key 形状与冻结 fixture 逐段对齐：前缀/scope/随机名。"""
    doc = _fixture()["publishInput"]["document"]
    narration = doc["audio"]["narrations"][0]
    background = doc["audio"]["background_music"]
    scope_hex = _fixture()["scopeHmac"]["main"]["digestHex"]

    narrator_key = build_audio_object_key(
        "memoir-test/audios/narrator/", scope_hex, role=AUDIO_ROLE_NARRATOR
    )
    background_key = build_audio_object_key(
        "memoir-test/audios/background/", scope_hex, role="background"
    )

    assert narrator_key.startswith(f"memoir-test/audios/narrator/{scope_hex}/")
    assert background_key.startswith(f"memoir-test/audios/background/{scope_hex}/")
    # 冻结形状：narr-<6hex>.mp3 / bgm-<6hex>.mp3。
    assert re.fullmatch(r"memoir-test/audios/narrator/[0-9a-f]{64}/narr-[0-9a-f]{6}\.mp3", narrator_key)
    assert re.fullmatch(r"memoir-test/audios/background/[0-9a-f]{64}/bgm-[0-9a-f]{6}\.mp3", background_key)
    assert narration["object_key"] != narrator_key  # 随机名不可复现
    assert background["object_key"] != background_key


def test_object_key_random_names_are_unique_per_call() -> None:
    """不透明随机名每次调用唯一：相同正文不同 scene 的资产键天然独立。"""
    prefix = "memoir-test/audios/narrator/"
    scope = "ab" * 32
    keys = {build_audio_object_key(prefix, scope, role=AUDIO_ROLE_NARRATOR) for _ in range(50)}
    assert len(keys) == 50


def test_object_key_rejects_bad_prefix_scope_and_role() -> None:
    """非法前缀（URL/..）、非法 scope、非法 role 均拒绝。"""
    scope = "ab" * 32
    for prefix in ("", "memoir-test/audios/../escape/", "https://example.mock/a/", "/leading-slash/"):
        with pytest.raises(MemoirAudioStorageError) as excinfo:
            build_audio_object_key(prefix, scope, role=AUDIO_ROLE_NARRATOR)
        assert excinfo.value.code == "AUDIO_OBJECT_KEY_INVALID"
    with pytest.raises(MemoirAudioStorageError):
        build_audio_object_key("memoir-test/audios/narrator/", "not-hex", role=AUDIO_ROLE_NARRATOR)
    with pytest.raises(MemoirAudioStorageError):
        build_audio_object_key("memoir-test/audios/narrator/", scope, role="unknown-role")


# ---------------------------------------------------------------------------
# 私有上传：独立 private ACL 方法，不复用 public-read 路径
# ---------------------------------------------------------------------------


class _FakePutResult:
    status_code = 200


class _RecordingOSSClient:
    """记录 PutObjectRequest 的假 OSS client；不触网。"""

    def __init__(self, *, exc: Exception | None = None) -> None:
        self.requests: list[Any] = []
        self._exc = exc

    def put_object(self, request: Any) -> _FakePutResult:
        self.requests.append(request)
        if self._exc is not None:
            raise self._exc
        return _FakePutResult()


def _uploader(client: _RecordingOSSClient) -> AliyunAudioOSSUploader:
    return AliyunAudioOSSUploader(
        access_key_id="ak-audio-test",
        access_key_secret="sk-audio-test",
        bucket="memoir-audio-bucket-test",
        endpoint="https://oss-cn-hangzhou.example.mock",
        client=client,
    )


def test_private_upload_sets_private_acl_and_dedicated_method() -> None:
    """上传即 private：ACL 直接为 private，不存在先 public 再补改。"""
    client = _RecordingOSSClient()
    uploader = _uploader(client)
    data = b"\xff\xfb-mock-mp3"
    uploader.upload_private_bytes(data, "memoir-test/audios/narrator/" + "ab" * 32 + "/narr-000001.mp3", "audio/mpeg")
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.acl == "private"
    assert request.bucket == "memoir-audio-bucket-test"
    assert request.key.endswith(".mp3")
    assert request.content_type == "audio/mpeg"
    assert request.body == data


def test_private_upload_rejects_empty_payload_and_key() -> None:
    """空字节/空 key 受控拒绝，不产生半配置上传。"""
    uploader = _uploader(_RecordingOSSClient())
    with pytest.raises(MemoirAudioStorageError) as excinfo:
        uploader.upload_private_bytes(b"", "memoir-test/audios/narrator/x.mp3", "audio/mpeg")
    assert excinfo.value.code == "AUDIO_UPLOAD_PAYLOAD_INVALID"
    with pytest.raises(MemoirAudioStorageError):
        uploader.upload_private_bytes(b"data", "", "audio/mpeg")


def test_private_upload_maps_oss_failure_to_safe_code_without_secret() -> None:
    """OSS 异常只映射安全枚举，凭据/endpoint 不出现在错误文本。"""
    client = _RecordingOSSClient(
        exc=RuntimeError("AccessDenied ak=sk-audio-test https://internal-endpoint")
    )
    uploader = _uploader(client)
    with pytest.raises(MemoirAudioStorageError) as excinfo:
        uploader.upload_private_bytes(b"data", "memoir-test/audios/narrator/k.mp3", "audio/mpeg")
    assert excinfo.value.code in ("AUDIO_UPLOAD_ACCESS_DENIED", "AUDIO_UPLOAD_FAILED")
    assert "sk-audio-test" not in str(excinfo.value)


def test_private_upload_requires_config() -> None:
    """缺凭据/桶/endpoint 时受控失败（能力关闭不触网）。"""
    with pytest.raises(MemoirAudioStorageError) as excinfo:
        AliyunAudioOSSUploader(access_key_id="", access_key_secret="", bucket="", endpoint="")
    assert excinfo.value.code == "AUDIO_UPLOAD_CONFIG_INVALID"


# ---------------------------------------------------------------------------
# 转码：ffmpeg 解码拼接、ffprobe 真实时长、临时文件纪律
# ---------------------------------------------------------------------------


class _FakeCommandRunner:
    """假 ffmpeg/ffprobe：记录 argv、检查临时文件权限并产出可控输出。"""

    def __init__(self, *, duration_text: str = "1.234567") -> None:
        self.duration_text = duration_text
        self.calls: list[list[str]] = []
        self.observed: dict[str, Any] = {}

    async def __call__(self, argv: list[str]) -> tuple[int, bytes, bytes]:
        self.calls.append(list(argv))
        binary, rest = argv[0], argv[1:]
        if "ffprobe" in binary:
            return 0, (self.duration_text + "\n").encode("ascii"), b""
        # ffmpeg：输入 concat 列表在 -i 后；输出文件是最后一个参数。
        output_path = Path(argv[-1])
        list_path = Path(rest[rest.index("-i") + 1])
        list_dir = list_path.parent
        segment_files = sorted(p for p in list_dir.iterdir() if p.name.startswith("seg-"))
        self.observed["concat_list"] = list_path.read_text(encoding="utf-8")
        self.observed["segment_count"] = len(segment_files)
        self.observed["segment_modes"] = [stat.S_IMODE(p.stat().st_mode) for p in segment_files]
        self.observed["segment_bytes"] = [p.read_bytes() for p in segment_files]
        self.observed["list_mode"] = stat.S_IMODE(list_path.stat().st_mode)
        self.observed["tmp_left_after_run"] = sorted(p.name for p in list_dir.iterdir())
        output_path.write_bytes(b"FAKE-FINAL-MP3")
        return 0, b"", b""


def _transcoder(runner: _FakeCommandRunner) -> AudioTranscoder:
    return AudioTranscoder(
        ffmpeg_path="/usr/bin/ffmpeg",
        ffprobe_path="/usr/bin/ffprobe",
        subprocess_timeout_seconds=10.0,
        command_runner=runner,
    )


def test_concat_decodes_via_ffmpeg_demuxer_not_byte_join() -> None:
    """拼接必须经 ffmpeg 解码重编（concat demuxer + libmp3lame），非字节连接。"""
    runner = _FakeCommandRunner(duration_text="2.000500")
    segments = [b"SEG-A", b"SEG-B", b"SEG-C"]
    result = asyncio.run(_transcoder(runner).concat_mp3_segments(segments))
    ffmpeg_argv = runner.calls[0]
    assert "-f" in ffmpeg_argv and "concat" in ffmpeg_argv
    assert "-c:a" in ffmpeg_argv and "libmp3lame" in ffmpeg_argv
    # 段内容无损进入临时文件，顺序保持。
    assert runner.observed["segment_bytes"] == segments
    # ffprobe 测真实时长：2.000500s → 2001ms（向上取整）。
    assert result.audio == b"FAKE-FINAL-MP3"
    assert result.duration_ms == 2001


def test_concat_temp_files_are_0600_and_cleaned() -> None:
    """临时段/列表文件 0600、随机目录，finally 全清理。"""
    runner = _FakeCommandRunner()
    transcoder = _transcoder(runner)
    tmp_root = Path(tempfile.gettempdir())
    before = set(tmp_root.glob("memoir-audio-*"))
    asyncio.run(transcoder.concat_mp3_segments([b"A", b"B"]))
    after = set(tmp_root.glob("memoir-audio-*"))
    # 运行后没有新增残留（fake runner 运行期间曾观察到文件存在）。
    assert runner.observed["segment_count"] == 2
    assert all(mode == 0o600 for mode in runner.observed["segment_modes"])
    assert runner.observed["list_mode"] == 0o600
    assert after == before


def test_concat_empty_segments_rejected() -> None:
    """空段列表直接受控失败，不起子进程。"""
    runner = _FakeCommandRunner()
    with pytest.raises(MemoirAudioStorageError) as excinfo:
        asyncio.run(_transcoder(runner).concat_mp3_segments([]))
    assert excinfo.value.code == "AUDIO_TRANSCODE_INPUT_INVALID"
    assert runner.calls == []


def test_concat_ffmpeg_failure_maps_safe_code_without_stderr() -> None:
    """ffmpeg rc!=0 只映射安全枚举，stderr 原文不进异常。"""

    async def runner(argv: list[str]) -> tuple[int, bytes, bytes]:
        return 1, b"", b"/tmp/secret-path detail: Invalid data found when processing"

    transcoder = AudioTranscoder(
        ffmpeg_path="/usr/bin/ffmpeg",
        ffprobe_path="/usr/bin/ffprobe",
        subprocess_timeout_seconds=10.0,
        command_runner=runner,
    )
    with pytest.raises(MemoirAudioStorageError) as excinfo:
        asyncio.run(transcoder.concat_mp3_segments([b"A"]))
    assert excinfo.value.code == "AUDIO_TRANSCODE_FAILED"
    assert "secret-path" not in str(excinfo.value)
    assert "Invalid data" not in str(excinfo.value)


def test_concat_ffprobe_invalid_duration_rejected() -> None:
    """ffprobe 输出不可解析 → 受控失败，不得默认 0 毫秒。"""

    async def runner(argv: list[str]) -> tuple[int, bytes, bytes]:
        if "ffprobe" in argv[0]:
            return 0, b"not-a-number\n", b""
        Path(argv[-1]).write_bytes(b"X")
        return 0, b"", b""

    transcoder = AudioTranscoder(
        ffmpeg_path="/usr/bin/ffmpeg",
        ffprobe_path="/usr/bin/ffprobe",
        subprocess_timeout_seconds=10.0,
        command_runner=runner,
    )
    with pytest.raises(MemoirAudioStorageError) as excinfo:
        asyncio.run(transcoder.concat_mp3_segments([b"A"]))
    assert excinfo.value.code == "AUDIO_DURATION_INVALID"


def test_transcode_to_mp3_enforces_size_cap_and_decode_check() -> None:
    """超限字节前置拒绝；解码失败（rc!=0）映射 AUDIO_DECODE_FAILED。"""
    async def runner(argv: list[str]) -> tuple[int, bytes, bytes]:
        return 1, b"", b"decode boom"

    transcoder = AudioTranscoder(
        ffmpeg_path="/usr/bin/ffmpeg",
        ffprobe_path="/usr/bin/ffprobe",
        subprocess_timeout_seconds=10.0,
        command_runner=runner,
        max_input_bytes=100,
    )
    with pytest.raises(MemoirAudioStorageError) as excinfo:
        asyncio.run(transcoder.transcode_to_mp3(b"x" * 101))
    assert excinfo.value.code == "AUDIO_FILE_TOO_LARGE"
    with pytest.raises(MemoirAudioStorageError) as excinfo:
        asyncio.run(transcoder.transcode_to_mp3(b"valid-size-but-bad"))
    assert excinfo.value.code == "AUDIO_DECODE_FAILED"


def _sleep_script(directory: Path, seconds: str) -> Path:
    """生成真实 sleep 脚本充当假 ffmpeg，用于超时/取消的真子进程验证。"""
    script = directory / "fake-ffmpeg.sh"
    script.write_text(f"#!/bin/sh\nsleep {seconds}\n", encoding="ascii")
    script.chmod(0o755)
    return script


def test_transcoder_timeout_kills_subprocess() -> None:
    """子进程超时被终止并映射 AUDIO_TRANSCODE_TIMEOUT，临时目录清理。"""
    workdir = Path(tempfile.mkdtemp(prefix="memoir-audio-test-"))
    script = _sleep_script(workdir, "30")
    try:
        transcoder = AudioTranscoder(
            ffmpeg_path=str(script),
            ffprobe_path="/usr/bin/ffprobe",
            subprocess_timeout_seconds=0.3,
        )
        with pytest.raises(MemoirAudioStorageError) as excinfo:
            asyncio.run(transcoder.concat_mp3_segments([b"A"]))
        assert excinfo.value.code == "AUDIO_TRANSCODE_TIMEOUT"
        # 脚本自身应已被终止（不留 sleep 孤儿）。
        result = subprocess.run(
            ["pgrep", "-f", "sleep 30"], capture_output=True, text=True
        )
        assert result.stdout.strip() == ""
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_transcoder_cancellation_terminates_and_cleans() -> None:
    """取消传播 CancelledError，子进程终止、无临时残留。"""

    async def scenario() -> None:
        workdir = Path(tempfile.mkdtemp(prefix="memoir-audio-test-"))
        script = _sleep_script(workdir, "30")
        tmp_root = Path(tempfile.gettempdir())
        before = set(tmp_root.glob("memoir-audio-*"))
        try:
            transcoder = AudioTranscoder(ffmpeg_path=str(script), ffprobe_path="/usr/bin/ffprobe")
            task = asyncio.ensure_future(transcoder.concat_mp3_segments([b"A"]))
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.1)
            assert set(tmp_root.glob("memoir-audio-*")) == before
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    asyncio.run(scenario())


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="本机无 ffmpeg/ffprobe，真实解码冒烟由部署环境执行",
)
def test_real_ffmpeg_concat_smoke() -> None:
    """真实 ffmpeg 冒烟：两段正弦 MP3 解码拼接后时长约等于两段之和。"""
    workdir = Path(tempfile.mkdtemp(prefix="memoir-audio-smoke-"))
    try:
        def make_tone(name: str, freq: str) -> bytes:
            out = workdir / name
            subprocess.run(
                [
                    shutil.which("ffmpeg") or "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1.2",
                    "-c:a", "libmp3lame", "-ar", "24000", "-b:a", "64k", str(out),
                ], check=True, capture_output=True)
            return out.read_bytes()

        seg1 = make_tone("seg1.mp3", "200")
        seg2 = make_tone("seg2.mp3", "400")
        transcoder = AudioTranscoder(
            ffmpeg_path=shutil.which("ffmpeg") or "ffmpeg",
            ffprobe_path=shutil.which("ffprobe") or "ffprobe",
            subprocess_timeout_seconds=60.0,
        )
        result = asyncio.run(transcoder.concat_mp3_segments([seg1, seg2]))
        assert result.audio
        assert result.duration_ms > 2000
    finally:
        for path in workdir.iterdir():
            path.unlink()
        workdir.rmdir()


# ---------------------------------------------------------------------------
# 官方临时 URL 下载：HTTPS、精确 host 白名单、每跳 SSRF 校验、字节上限
# ---------------------------------------------------------------------------


def _downloader(
    handler,
    *,
    allowed_hosts: frozenset[str] | None = None,
    max_bytes: int = 1024 * 1024,
    resolver: Any = None,
) -> SecureAudioDownloader:
    return SecureAudioDownloader(
        allowed_hosts=allowed_hosts or frozenset({PUBLIC_HOST}),
        max_bytes=max_bytes,
        transport=httpx.MockTransport(handler),
        resolver=resolver or (lambda host: [PUBLIC_IP]),
    )


def test_download_requires_https_and_exact_host() -> None:
    """仅 HTTPS + 精确 host（子域/大写绕过均拒绝）；非白名单 host 拒绝。"""
    downloader = _downloader(lambda request: httpx.Response(200, content=b"x"))

    with pytest.raises(MemoirAudioStorageError) as excinfo:
        asyncio.run(downloader.download(f"http://{PUBLIC_HOST}/audio.mp3"))
    assert excinfo.value.code == "AUDIO_DOWNLOAD_URL_INVALID"

    with pytest.raises(MemoirAudioStorageError) as excinfo:
        asyncio.run(downloader.download("https://evil.example.mock/audio.mp3"))
    assert excinfo.value.code == "AUDIO_DOWNLOAD_HOST_FORBIDDEN"

    with pytest.raises(MemoirAudioStorageError) as excinfo:
        asyncio.run(downloader.download(f"https://sub.{PUBLIC_HOST}/audio.mp3"))
    assert excinfo.value.code == "AUDIO_DOWNLOAD_HOST_FORBIDDEN"

    # userinfo / 非法端口拒绝。
    with pytest.raises(MemoirAudioStorageError):
        asyncio.run(downloader.download(f"https://user:pass@{PUBLIC_HOST}/audio.mp3"))
    with pytest.raises(MemoirAudioStorageError):
        asyncio.run(downloader.download(f"https://{PUBLIC_HOST}:22/audio.mp3"))


def test_download_rejects_private_dns_resolution() -> None:
    """DNS 解析到私网/非全局 IP 一律拒绝（SSRF）。"""
    downloader = _downloader(
        lambda request: httpx.Response(200, content=b"x"),
        resolver=lambda host: [PRIVATE_IP],
    )
    with pytest.raises(MemoirAudioStorageError) as excinfo:
        asyncio.run(downloader.download(f"https://{PUBLIC_HOST}/audio.mp3"))
    assert excinfo.value.code == "AUDIO_DOWNLOAD_ADDRESS_UNSAFE"

    ip_literal_downloader = _downloader(
        lambda request: httpx.Response(200, content=b"x"),
        resolver=lambda host: ["203.0.113.10"],
    )
    with pytest.raises(MemoirAudioStorageError):
        asyncio.run(
            ip_literal_downloader.download("https://93-184-216-34.nip.io/audio.mp3")
        )


def test_download_real_dns_path_reads_sockaddr_first_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：resolver=None 走真实 socket.getaddrinfo，地址取 sockaddr 首元素。

    旧实现误写 info[0][4][0]（对 AddressFamily 枚举取下标），真实 DNS 分支
    必崩 TypeError；既有测试全部注入 resolver 绕开了该分支。本测试补住它。
    """

    def fake_getaddrinfo(host: str, port: int, *, proto: int | None = None) -> list[tuple]:
        # getaddrinfo 每项为 (family, type, proto, canonname, sockaddr) 五元组；
        # IPv4 的 sockaddr 是 (ip, port)，地址在第 5 项首元素。
        return [
            (
                socket.AddressFamily.AF_INET,
                socket.SocketKind.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (PUBLIC_IP, port),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    downloader = SecureAudioDownloader(
        allowed_hosts=frozenset({PUBLIC_HOST}),
        max_bytes=1024,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x")),
        resolver=None,
    )
    # 走完整下载入口：真实分支解析出公网地址应放行且不再崩溃。
    assert asyncio.run(downloader.download(f"https://{PUBLIC_HOST}/a.mp3")) == b"x"


def test_download_verifies_each_redirect_hop() -> None:
    """重定向逐跳校验：白名单内 host 放行，跳到陌生 host 拒绝。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/hop":
            return httpx.Response(
                302, headers={"Location": f"https://{PUBLIC_HOST}/final.mp3"}
            )
        if request.url.path == "/to-evil":
            return httpx.Response(
                302, headers={"Location": "https://evil.example.mock/a.mp3"}
            )
        return httpx.Response(200, content=b"REAL-AUDIO")

    downloader = _downloader(handler)
    # 白名单内跳转放行（每跳都过 host/DNS 校验）。
    assert asyncio.run(downloader.download(f"https://{PUBLIC_HOST}/hop")) == b"REAL-AUDIO"
    # 跳出白名单拒绝。
    with pytest.raises(MemoirAudioStorageError) as excinfo:
        asyncio.run(downloader.download(f"https://{PUBLIC_HOST}/to-evil"))
    assert excinfo.value.code == "AUDIO_DOWNLOAD_HOST_FORBIDDEN"


def test_download_limits_redirect_chain_length() -> None:
    """重定向链超过上限即失败，不无限跟随。"""
    def handler(request: httpx.Request) -> httpx.Response:
        hop = int(request.url.params.get("hop", "0"))
        return httpx.Response(
            302, headers={"Location": f"https://{PUBLIC_HOST}/?hop={hop + 1}"}
        )

    downloader = _downloader(handler)
    with pytest.raises(MemoirAudioStorageError) as excinfo:
        asyncio.run(downloader.download(f"https://{PUBLIC_HOST}/?hop=0"))
    assert excinfo.value.code == "AUDIO_DOWNLOAD_REDIRECTS_EXCEEDED"


def test_download_enforces_byte_cap_while_streaming() -> None:
    """超限在流式读取中途中断并映射 AUDIO_DOWNLOAD_TOO_LARGE。"""

    async def big_stream():
        for _ in range(64):
            yield b"x" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big_stream())

    downloader = _downloader(handler, max_bytes=8192)
    with pytest.raises(MemoirAudioStorageError) as excinfo:
        asyncio.run(downloader.download(f"https://{PUBLIC_HOST}/big.mp3"))
    assert excinfo.value.code == "AUDIO_DOWNLOAD_TOO_LARGE"


# ---------------------------------------------------------------------------
# 配置门禁：默认关闭；启用时缺任何必需项都拒绝，未知不得以 0/空串顶替
# ---------------------------------------------------------------------------


def _audio_settings(**overrides: Any) -> Settings:
    """构造覆盖了音频配置的 Settings（隔离 env 文件）。"""
    values: dict[str, Any] = {
        "ENVIRONMENT": "test",
        "MEMOIR_AUDIO_ENABLED": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_audio_settings_disabled_by_default_passes() -> None:
    """默认关闭：缺 Key/单价/桶不阻断启动（不影响其他 Agent）。"""
    validate_memoir_audio_settings(_audio_settings())


def test_audio_settings_enabled_requires_full_configuration() -> None:
    """启用时缺任一必需项报安全错误并列出字段名，不回显值。"""
    with pytest.raises(ValueError) as excinfo:
        validate_memoir_audio_settings(_audio_settings(MEMOIR_AUDIO_ENABLED=True))
    message = str(excinfo.value)
    for field in (
        "MEMOIR_TTS_API_KEY",
        "MEMOIR_MUSIC_ACTION",
        "MEMOIR_TTS_PRICE_PER_1000_TEXT_WORDS",
        "MEMOIR_MUSIC_PRICE_PER_SECOND",
        "MEMOIR_AUDIO_MAX_COST_PER_RUN",
        "MEMOIR_AUDIO_COST_CURRENCY",
        "MEMORY_AUDIO_OSS_BUCKET",
        "MEMORY_AUDIO_SCOPE_HMAC_KEY",
        "MEMOIR_AUDIO_INPUT_HMAC_KEY",
        "MEMORY_AUDIO_NARRATOR_PREFIX",
        "MEMORY_AUDIO_BACKGROUND_PREFIX",
        "MEMOIR_MUSIC_DOWNLOAD_ALLOWED_HOSTS_JSON",
    ):
        assert field in message


def test_audio_settings_enabled_full_config_passes_and_bad_values_rejected() -> None:
    """完整配置通过；未知价格填 0 之外的非法值、非法前缀、坏 Action 拒绝。"""
    valid = dict(
        MEMOIR_AUDIO_ENABLED=True,
        MEMOIR_TTS_API_KEY="key-test",
        MEMOIR_MUSIC_ACTION="GenBGM",
        VOLCANO_CV_ACCESS_KEY="ak",
        VOLCANO_CV_SECRET_KEY="sk",
        MEMOIR_TTS_PRICE_PER_1000_TEXT_WORDS="0.5",
        MEMOIR_MUSIC_PRICE_PER_SECOND="0.02",
        MEMOIR_AUDIO_MAX_COST_PER_RUN="2.0",
        MEMOIR_AUDIO_COST_CURRENCY="CNY",
        MEMORY_AUDIO_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        MEMORY_AUDIO_OSS_BUCKET="bucket",
        MEMORY_AUDIO_OSS_ACCESS_KEY_ID="ak",
        MEMORY_AUDIO_OSS_ACCESS_KEY_SECRET="sk",
        MEMORY_AUDIO_NARRATOR_PREFIX="memoir-test/audios/narrator/",
        MEMORY_AUDIO_BACKGROUND_PREFIX="memoir-test/audios/background/",
        MEMORY_AUDIO_SCOPE_HMAC_KEY="scope-key",
        MEMOIR_AUDIO_INPUT_HMAC_KEY="test-input-key",
        MEMOIR_MUSIC_DOWNLOAD_ALLOWED_HOSTS_JSON='["music.tbs4.example.mock"]',
    )
    validate_memoir_audio_settings(_audio_settings(**valid))

    # 价格必须可解析为非负十进制；预算为正数；缺用量不得以 0 顶替由"空值即缺"保证。
    for bad in (
        {"MEMOIR_TTS_PRICE_PER_1000_TEXT_WORDS": "abc"},
        {"MEMOIR_MUSIC_PRICE_PER_SECOND": "-0.1"},
        {"MEMOIR_AUDIO_MAX_COST_PER_RUN": "0"},
        {"MEMOIR_MUSIC_ACTION": "GenSong"},
        {"MEMOIR_TTS_SPEECH_RATE": "101"},
        {"MEMOIR_MUSIC_DOWNLOAD_ALLOWED_HOSTS_JSON": '["*.example.mock"]'},
        {"MEMORY_AUDIO_NARRATOR_PREFIX": "memoir-test/audios/narrator"},
    ):
        with pytest.raises(ValueError):
            validate_memoir_audio_settings(_audio_settings(**{**valid, **bad}))


def test_audio_settings_production_rejects_test_prefixes() -> None:
    """生产环境拒绝 memoir-test/ 前缀；测试环境拒绝正式前缀混用。"""
    base = dict(
        MEMOIR_AUDIO_ENABLED=True,
        MEMOIR_TTS_API_KEY="key-test",
        MEMOIR_MUSIC_ACTION="GenBGM",
        VOLCANO_CV_ACCESS_KEY="ak",
        VOLCANO_CV_SECRET_KEY="sk",
        MEMOIR_TTS_PRICE_PER_1000_TEXT_WORDS="0.5",
        MEMOIR_MUSIC_PRICE_PER_SECOND="0.02",
        MEMOIR_AUDIO_MAX_COST_PER_RUN="2.0",
        MEMOIR_AUDIO_COST_CURRENCY="CNY",
        MEMORY_AUDIO_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        MEMORY_AUDIO_OSS_BUCKET="bucket",
        MEMORY_AUDIO_OSS_ACCESS_KEY_ID="ak",
        MEMORY_AUDIO_OSS_ACCESS_KEY_SECRET="sk",
        MEMORY_AUDIO_SCOPE_HMAC_KEY="scope-key",
        MEMOIR_AUDIO_INPUT_HMAC_KEY="test-input-key",
        MEMOIR_MUSIC_DOWNLOAD_ALLOWED_HOSTS_JSON='["music.tbs4.example.mock"]',
    )
    with pytest.raises(ValueError):
        validate_memoir_audio_settings(_audio_settings(
            ENVIRONMENT="production",
            MEMORY_AUDIO_NARRATOR_PREFIX="memoir-test/audios/narrator/",
            MEMORY_AUDIO_BACKGROUND_PREFIX="memoir-test/audios/background/",
            **base,
        ))
    # 角色前缀重叠（narrator 是 background 前缀的前缀）同样拒绝。
    with pytest.raises(ValueError):
        validate_memoir_audio_settings(_audio_settings(
            MEMORY_AUDIO_NARRATOR_PREFIX="memoir-test/audios/",
            MEMORY_AUDIO_BACKGROUND_PREFIX="memoir-test/audios/background/",
            **{k: v for k, v in base.items() if k != "ENVIRONMENT"},
        ))
