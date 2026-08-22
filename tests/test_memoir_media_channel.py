"""M6 回忆录媒体通道（D2 Runtime 生成端）测试。

全部使用 mock Provider 与假 OSS 上传器；隐私断言保证 prompt、图片字节、
照片字节与生成 URL 绝不进入日志。迁移与包注册回归同文件收口。
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import importlib.util
import json
import logging
import re
import sys
import time
import types
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.agents.memoir_agent.runner import (
    _MEDIA_SCENE_TYPES,
    _SCENE_TYPES,
    MemoirNodeRunner,
    _media_version_enabled,
)
from app.runtime.state import AgentState
from app.runtime.tool_gateway import _TOOL_WIRE_VERSION_BY_AGENT_VERSION
from app.services.memoir_media_service import (
    MEDIA_MANIFEST_KEYS,
    MemoirMediaConfig,
    MemoirMediaService,
    sniff_image_mime,
)
from app.utils.aliyun.oss_client import AliyunOSSClient, AliyunOSSClientError
from app.utils.volcano.cv_client import (
    MockCVClient,
    VolcanoCVClient,
    VolcanoCVError,
    build_volcano_v4_authorization,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-image-body"
JPEG_BYTES = b"\xff\xd8\xff-e00"


class FakeUploader:
    """满足 MediaObjectUploader 协议的假 OSS：只记录元数据，返回契约 URL。"""

    def __init__(self, *, fail_keys: set[str] | None = None) -> None:
        self.uploads: list[tuple[str, str]] = []
        self._fail_keys = fail_keys or set()

    def upload_public_bytes(self, data: bytes, object_key: str, mime: str) -> str:
        if object_key in self._fail_keys:
            raise RuntimeError("oss upload failed")
        self.uploads.append((object_key, mime))
        return f"https://bucket.oss-cn-hangzhou.aliyuncs.com/{object_key}"


def _config(**overrides: object) -> MemoirMediaConfig:
    defaults: dict[str, object] = {
        "provider_name": "mock",
        "image_prefix": "memoir/images/",
        "max_images_per_run": 4,
        "url_host_suffixes": ("aliyuncs.com",),
        "photo_egress_enabled": False,
        "provider_residency": "private",
        "node_budget_seconds": 30.0,
    }
    defaults.update(overrides)
    return MemoirMediaConfig(**defaults)  # type: ignore[arg-type]


def _run(version: str = "1.0.3") -> object:
    return type(
        "Run", (), {"run_id": "run-media", "agent_version": version},
    )()


def _image_scene(scene_id: str, body: str, title_word: str | None = None) -> dict[str, object]:
    scene: dict[str, object] = {
        "scene_id": scene_id, "scene_type": "image",
        "source_refs": ["diary:diary-1"], "body": body,
    }
    if title_word is not None:
        scene["title_word"] = title_word
    return scene


def _text_scene(scene_id: str) -> dict[str, object]:
    return {
        "scene_id": scene_id, "scene_type": "summary",
        "source_refs": ["diary:diary-1"], "body": "温和的一段总结文案。",
    }


def _scenes_with_one_image(body: str = "我们在海边的傍晚散步，海风很轻。") -> list[dict[str, object]]:
    return [_text_scene("scene-1"), _image_scene("scene-2", body, "那年海边"), _text_scene("scene-3")]


# ---------------------------------------------------------------------------
# 交付物 2/3：服务层生成、降级、配额、模式门控
# ---------------------------------------------------------------------------

def test_media_service_generates_six_key_manifest_and_payload() -> None:
    """场景 1：门禁开启时 image 场景生成六键 manifest + 两键 payload。"""
    provider = MockCVClient(text_image=PNG_BYTES)
    uploader = FakeUploader()
    service = MemoirMediaService(provider, uploader, _config())
    manifest, scenes = service.generate(_run(), _scenes_with_one_image(), None)
    assert len(manifest) == 1
    entry = manifest[0]
    assert set(entry) == MEDIA_MANIFEST_KEYS
    assert entry["kind"] == "image"
    assert entry["mime"] == "image/png"
    assert entry["scene_id"] == "scene-2"
    assert entry["object_key"].startswith("memoir/images/")
    # 不可猜测 object_key：前缀后必须是 UUID + 扩展名。
    assert re.fullmatch(r"memoir/images/[0-9a-f-]{36}\.png", entry["object_key"])
    parsed = urlparse(entry["url"])
    assert parsed.scheme == "https" and parsed.netloc.endswith("aliyuncs.com")
    assert parsed.path.startswith("/memoir/images/")
    # 场景侧：payload 两键且 image_url 与 manifest url 完全一致；顶层 title_word 被移除。
    image_scene = scenes[1]
    assert image_scene["scene_type"] == "image"
    assert set(image_scene["payload"]) == {"image_url", "title_word"}
    assert image_scene["payload"]["image_url"] == entry["url"]
    assert "title_word" not in image_scene
    # 非 image 场景零改动且不携带 payload。
    assert scenes[0] == _text_scene("scene-1")
    assert "payload" not in scenes[0]
    assert provider.text_prompts == ["我们在海边的傍晚散步，海风很轻。"]


def test_media_service_degrades_failed_scene_to_summary_text_card(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """场景 2：单张失败只降级该场景，节点不抛异常、其余场景不受影响。"""
    provider = MockCVClient(text_image=b"not-an-image")  # 魔数嗅探失败 -> 降级
    service = MemoirMediaService(provider, FakeUploader(), _config())
    with caplog.at_level(logging.INFO):
        manifest, scenes = service.generate(_run(), _scenes_with_one_image(), None)
    assert manifest == []
    degraded = scenes[1]
    assert degraded["scene_type"] == "summary"
    assert degraded["body"] == "我们在海边的傍晚散步，海风很轻。"
    assert "payload" not in degraded and "title_word" not in degraded
    assert scenes[0]["scene_type"] == "summary"
    assert "stage=image_validation" in caplog.text
    assert "code=MEDIA_IMAGE_FORMAT_INVALID" in caplog.text


def test_media_service_logs_safe_provider_failure_stage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Provider 异常仅记录固定阶段码，不泄露 prompt 或异常正文。"""
    secret_prompt = "provider-private-prompt"
    provider = MockCVClient(fail_prompts=frozenset({secret_prompt}))
    service = MemoirMediaService(provider, FakeUploader(), _config())

    with caplog.at_level(logging.INFO):
        manifest, scenes = service.generate(
            _run(), _scenes_with_one_image(secret_prompt), None,
        )

    assert manifest == []
    assert scenes[1]["scene_type"] == "summary"
    assert "stage=provider" in caplog.text
    assert "code=MEDIA_PROVIDER_FAILED" in caplog.text
    assert secret_prompt not in caplog.text
    assert "VOLCANO_CV_MOCK_FAILURE" not in caplog.text


def test_media_service_logs_safe_oss_failure_stage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OSS 异常仅记录固定阶段码，不泄露底层错误正文。"""
    private_error = "oss-private-error"

    class FailingUploader:
        def upload_public_bytes(
            self, data: bytes, object_key: str, mime: str,
        ) -> str:
            raise RuntimeError(private_error)

    service = MemoirMediaService(
        MockCVClient(text_image=PNG_BYTES), FailingUploader(), _config(),
    )
    with caplog.at_level(logging.INFO):
        manifest, scenes = service.generate(_run(), _scenes_with_one_image(), None)

    assert manifest == []
    assert scenes[1]["scene_type"] == "summary"
    assert "stage=oss_upload" in caplog.text
    assert "code=MEDIA_OSS_UPLOAD_FAILED" in caplog.text
    assert private_error not in caplog.text


def test_media_service_logs_safe_url_failure_stage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """URL 合同异常仅记录固定阶段码，不泄露上传返回地址。"""
    private_url = "https://private.example/memoir/images/private.png"

    class InvalidUrlUploader:
        def upload_public_bytes(
            self, data: bytes, object_key: str, mime: str,
        ) -> str:
            return private_url

    service = MemoirMediaService(
        MockCVClient(text_image=PNG_BYTES), InvalidUrlUploader(), _config(),
    )
    with caplog.at_level(logging.INFO):
        manifest, scenes = service.generate(_run(), _scenes_with_one_image(), None)

    assert manifest == []
    assert scenes[1]["scene_type"] == "summary"
    assert "stage=url_validation" in caplog.text
    assert "code=MEDIA_URL_VALIDATION_FAILED" in caplog.text
    assert private_url not in caplog.text


def test_media_service_all_failures_publish_text_only() -> None:
    """场景 3：全部图片失败 -> media_tasks=[]，场景全部降级为文本卡。"""
    provider = MockCVClient(text_image=b"\x00\x01broken")
    scenes = [_image_scene("scene-1", "第一个画面"), _text_scene("scene-2"), _image_scene("scene-3", "第二个画面")]
    service = MemoirMediaService(provider, FakeUploader(), _config())
    manifest, updated = service.generate(_run(), scenes, None)
    assert manifest == []
    assert [scene["scene_type"] for scene in updated] == ["summary", "summary", "summary"]


def test_media_service_quota_degrades_beyond_limit() -> None:
    """场景 4：按张配额（model_policy.max_media_images 优先）超出即降级。"""
    provider = MockCVClient(text_image=PNG_BYTES)
    run = type(
        "Run", (), {
            "run_id": "run-media", "agent_version": "1.0.3",
            "capability_snapshot_json": {"model_policy": {"max_media_images": 1}},
        },
    )()
    scenes = [
        _image_scene("scene-1", "第一张画面描述"), _text_scene("scene-2"),
        _image_scene("scene-3", "第二张画面描述"),
    ]
    service = MemoirMediaService(provider, FakeUploader(), _config(max_images_per_run=8))
    manifest, updated = service.generate(run, scenes, None)
    assert len(manifest) == 1 and manifest[0]["scene_id"] == "scene-1"
    assert updated[2]["scene_type"] == "summary"


def test_media_service_img2img_only_when_both_gates_open() -> None:
    """场景 5：照片出域门禁（开关 + public 驻留）双开才图生图，否则文生图。"""
    material = {"materials": [{
        "source_ref": "diary:diary-1", "type": "diary", "sensitive": False,
        "text": "素材摘要",
        "images": [{"photo_id": "p1", "object_key": "photos/diary-1/cover.jpg", "mime": "image/jpeg"}],
    }]}
    loaded: list[str] = []

    def photo_loader(object_key: str) -> bytes:
        loaded.append(object_key)
        return JPEG_BYTES

    # 门禁关闭（默认）：即使素材含 images、loader 可用，也绝不外发照片。
    provider = MockCVClient(image_image=PNG_BYTES)
    MemoirMediaService(
        provider, FakeUploader(), _config(),
        photo_loader=photo_loader,
    ).generate(_run(), _scenes_with_one_image(), material)
    assert provider.image_prompts == [] and loaded == []

    # 只开 egress 开关、驻留仍 private：依旧 fail-closed 文生图。
    provider = MockCVClient(image_image=PNG_BYTES)
    MemoirMediaService(
        provider, FakeUploader(), _config(photo_egress_enabled=True),
        photo_loader=photo_loader,
    ).generate(_run(), _scenes_with_one_image(), material)
    assert provider.image_prompts == [] and loaded == []

    # 双开：走图生图并消费参考照片。
    provider = MockCVClient(image_image=PNG_BYTES)
    manifest, _ = MemoirMediaService(
        provider, FakeUploader(), _config(photo_egress_enabled=True, provider_residency="public"),
        photo_loader=photo_loader,
    ).generate(_run(), _scenes_with_one_image(), material)
    assert provider.image_prompts == ["我们在海边的傍晚散步，海风很轻。"]
    assert loaded == ["photos/diary-1/cover.jpg"]
    assert len(manifest) == 1


def test_media_service_never_logs_prompt_url_or_bytes(caplog: pytest.LogCaptureFixture) -> None:
    """交付物 7：日志只允许 scene_id/成败/耗时/张数，禁止 prompt、URL、字节。"""
    secret_prompt = "我们在海边的傍晚散步，海风很轻。"
    with caplog.at_level(logging.INFO):
        provider = MockCVClient(text_image=PNG_BYTES)
        uploader = FakeUploader()
        MemoirMediaService(provider, uploader, _config()).generate(
            _run(), _scenes_with_one_image(secret_prompt), None,
        )
    logged = caplog.text
    assert secret_prompt not in logged
    assert uploader.uploads[0][0] not in logged
    assert "aliyuncs.com" not in logged
    assert "PNG" not in logged
    assert "scene-2" in logged  # scene_id 是允许的观测元数据


# ---------------------------------------------------------------------------
# 交付物 5：runner 节点编排与版本门控
# ---------------------------------------------------------------------------

def test_media_node_disabled_degrades_image_scenes_for_media_versions() -> None:
    """场景 6：1.0.3 且媒体服务未装配（总开关关）-> image 场景降级文本卡。"""
    runner = MemoirNodeRunner(object())
    state = AgentState(scenes=_scenes_with_one_image())
    result = runner.run_node({"node_id": "enqueue_media_tasks"}, _run(), state)
    assert result == {
        "node_id": "enqueue_media_tasks", "skipped": True,
        "reason_code": "CAPABILITY_DISABLED",
    }
    assert state.media_tasks == []
    assert all(scene["scene_type"] == "summary" for scene in state.scenes)
    assert "title_word" not in state.scenes[1]
    # 动作按降级后的场景表重建，且与场景一一对应。
    assert len(state.actions) == len(state.scenes)
    assert {action["scene_id"] for action in state.actions} == {
        scene["scene_id"] for scene in state.scenes
    }
    assert "media_disabled_degraded" in state.fallback_flags


def test_media_node_old_versions_keep_zero_change() -> None:
    """场景 7：旧版本（<1.0.3）即使注入媒体服务也保持原跳过语义。"""
    service = MemoirMediaService(MockCVClient(text_image=PNG_BYTES), FakeUploader(), _config())
    runner = MemoirNodeRunner(object(), media_service=service)
    state = AgentState(scenes=[_text_scene("scene-1"), _text_scene("scene-2"), _text_scene("scene-3")])
    result = runner.run_node(
        {"node_id": "enqueue_media_tasks"},
        type("Run", (), {"run_id": "run-old", "agent_version": "1.0.2"})(),
        state,
    )
    assert result == {
        "node_id": "enqueue_media_tasks", "skipped": True,
        "reason_code": "CAPABILITY_DISABLED",
    }
    assert state.media_tasks == [] and state.actions is None


def test_media_node_generates_then_safety_publishes_manifest() -> None:
    """场景 8：媒体节点生成 -> safety_review 将 media_tasks 组装进播放文档。"""
    service = MemoirMediaService(MockCVClient(text_image=PNG_BYTES), FakeUploader(), _config())
    runner = MemoirNodeRunner(object(), media_service=service)
    state = AgentState(
        scenes=_scenes_with_one_image(),
        sanitized_material={"materials": [
            {"source_ref": "diary:diary-1", "type": "diary", "sensitive": False, "text": "摘要"},
        ]},
    )
    result = runner.run_node({"node_id": "enqueue_media_tasks"}, _run(), state)
    assert result == {"node_id": "enqueue_media_tasks", "skipped": False, "delivered": 1}
    runner.run_node({"node_id": "generate_actions"}, _run(), state)
    safety = runner.run_node({"node_id": "safety_review"}, _run(), state)
    assert safety == {"node_id": "safety_review", "safe": True}
    document = state.playback_document
    assert document is not None
    assert document["schema_version"] == "1.0.0"  # D1 冻结：媒体不升版
    assert len(document["media_manifest"]) == 1
    assert document["media_manifest"][0]["scene_id"] == "scene-2"
    assert document["scenes"][1]["payload"]["image_url"] == document["media_manifest"][0]["url"]


def test_safety_review_falls_back_when_image_scene_lacks_manifest_entry() -> None:
    """image 场景缺 manifest 条目（缺 payload/url）必须安全回退基础卡。"""
    runner = MemoirNodeRunner(object())
    state = AgentState(
        scenes=_scenes_with_one_image(),
        media_tasks=[],
        sanitized_material={"materials": [
            {"source_ref": "diary:diary-1", "type": "diary", "sensitive": False, "text": "摘要"},
        ]},
    )
    runner.run_node({"node_id": "generate_actions"}, _run(), state)
    safety = runner.run_node({"node_id": "safety_review"}, _run(), state)
    assert safety == {"node_id": "safety_review", "safe": False}
    assert state.playback_document is not None
    assert state.playback_document["media_manifest"] == []
    assert all(scene["scene_type"] == "summary" for scene in state.scenes)


def test_media_version_gating_and_scene_types() -> None:
    """版本门控纯函数与词表：image 只进 1.0.3+ 集合，词表零变更。"""
    assert not _media_version_enabled("1.0.2")
    assert not _media_version_enabled("")
    assert not _media_version_enabled(None)  # type: ignore[arg-type]
    assert _media_version_enabled("1.0.3")
    assert _media_version_enabled("1.1.0")
    assert "image" not in _SCENE_TYPES
    assert _MEDIA_SCENE_TYPES == _SCENE_TYPES + ("image",)


def test_valid_scenes_accepts_image_only_for_media_versions() -> None:
    """_valid_scenes：1.0.3 接受 image+title_word；旧版本拒绝；非 image 拒绝 title_word。"""
    runner = MemoirNodeRunner(object())

    def model_payload(scene_type: str, title_word: str | None = None) -> dict[str, object]:
        # 场景计划必须 3-8 张，不足会被整批拒绝，与版本门控无关。
        filler = [
            {"scene_id": "scene-fill-1", "scene_type": "cover",
             "source_refs": ["diary:diary-1"], "body": "我们的故事。"},
            {"scene_id": "scene-fill-2", "scene_type": "summary",
             "source_refs": ["diary:diary-1"], "body": "温和的一段总结文案。"},
        ]
        scene: dict[str, object] = {
            "scene_id": "scene-target", "scene_type": scene_type,
            "source_refs": ["diary:diary-1"], "body": "温和的一段总结文案。",
        }
        if title_word is not None:
            scene["title_word"] = title_word
        return {"scenes": [*filler, scene]}

    refs = ["diary:diary-1"]
    accepted = runner._valid_scenes(model_payload("image", "那年海边"), refs, "1.0.3")
    assert accepted is not None
    target = next(scene for scene in accepted if scene["scene_id"] == "scene-target")
    assert target["title_word"] == "那年海边"
    assert runner._valid_scenes(model_payload("image"), refs, "1.0.2") is None
    assert runner._valid_scenes(model_payload("summary", "标题"), refs, "1.0.3") is None
    assert runner._valid_scenes(model_payload("image", "超过六个字的标题词"), refs, "1.0.3") is None


# ---------------------------------------------------------------------------
# D1 契约负例：_is_safe_playback 的 manifest 校验
# ---------------------------------------------------------------------------

def _safe_document_parts() -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    url = "https://bucket.oss-cn-hangzhou.aliyuncs.com/memoir/images/0f14d0ab-9605-4a62-a9e4-5ed26688389b.png"
    entry = {
        "media_id": "media-1", "kind": "image",
        "object_key": "memoir/images/0f14d0ab-9605-4a62-a9e4-5ed26688389b.png",
        "url": url, "mime": "image/png", "scene_id": "scene-2",
    }
    scenes = [
        _text_scene("scene-1"),
        {
            "scene_id": "scene-2", "scene_type": "image",
            "source_refs": ["diary:diary-1"], "body": "画面描述",
            "payload": {"image_url": url, "title_word": "那年海边"},
        },
        _text_scene("scene-3"),
    ]
    return scenes, [entry], {"url": url}


def test_is_safe_playback_accepts_contract_conforming_document() -> None:
    scenes, manifest, _ = _safe_document_parts()
    actions = MemoirNodeRunner._rule_actions(scenes)
    assert MemoirNodeRunner._is_safe_playback(
        scenes, actions, media_tasks=manifest, scene_types=_MEDIA_SCENE_TYPES,
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda e: e.update({"extra": 1}), id="seven-keys"),
        pytest.param(lambda e: e.update({"kind": "video"}), id="bad-kind"),
        pytest.param(lambda e: e.update({"mime": "image/gif"}), id="bad-mime"),
        pytest.param(lambda e: e.update({"url": "http://bucket.oss-aliyuncs.com/memoir/images/x.png"}), id="http-url"),
        pytest.param(lambda e: e.update({"object_key": "memoir/images/guessable.png"}), id="guessable-key"),
        pytest.param(lambda e: e.update({"scene_id": "scene-missing"}), id="unknown-scene"),
    ],
)
def test_is_safe_playback_rejects_manifest_violations(mutate: object) -> None:
    scenes, manifest, _ = _safe_document_parts()
    mutate(manifest[0])  # type: ignore[operator]
    actions = MemoirNodeRunner._rule_actions(scenes)
    assert not MemoirNodeRunner._is_safe_playback(
        scenes, actions, media_tasks=manifest, scene_types=_MEDIA_SCENE_TYPES,
    )


def test_is_safe_playback_rejects_url_mismatch_and_payload_violations() -> None:
    # image_url 与 manifest url 不一致。
    scenes, manifest, _ = _safe_document_parts()
    scenes[1]["payload"] = {"image_url": "https://bucket.oss-cn-hangzhou.aliyuncs.com/memoir/images/other.png"}
    assert not MemoirNodeRunner._is_safe_playback(
        scenes, MemoirNodeRunner._rule_actions(scenes),
        media_tasks=manifest, scene_types=_MEDIA_SCENE_TYPES,
    )
    # title_word 超过 6 个汉字。
    scenes2, manifest2, parts = _safe_document_parts()
    scenes2[1]["payload"] = {"image_url": parts["url"], "title_word": "超过六个字的标题词"}
    assert not MemoirNodeRunner._is_safe_playback(
        scenes2, MemoirNodeRunner._rule_actions(scenes2),
        media_tasks=manifest2, scene_types=_MEDIA_SCENE_TYPES,
    )
    # 非 image 场景携带 payload（契约：保持无/空）。
    scenes3, manifest3, _ = _safe_document_parts()
    scenes3[0]["payload"] = {"image_url": "https://bucket.oss-cn-hangzhou.aliyuncs.com/memoir/images/x.png"}
    assert not MemoirNodeRunner._is_safe_playback(
        scenes3, MemoirNodeRunner._rule_actions(scenes3),
        media_tasks=manifest3, scene_types=_MEDIA_SCENE_TYPES,
    )
    # manifest 条目与场景 1:N 重复。
    scenes4, manifest4, _ = _safe_document_parts()
    manifest4.append(dict(manifest4[0]))
    assert not MemoirNodeRunner._is_safe_playback(
        scenes4, MemoirNodeRunner._rule_actions(scenes4),
        media_tasks=manifest4, scene_types=_MEDIA_SCENE_TYPES,
    )


# ---------------------------------------------------------------------------
# 交付物 1：火山视觉异步任务签名与客户端（mock transport，零真实计费调用）
# ---------------------------------------------------------------------------

def test_volcano_v4_authorization_deterministic_and_structured() -> None:
    kwargs = {
        "method": "POST",
        "canonical_uri": "/",
        "canonical_query": (
            "Action=CVSync2AsyncSubmitTask&Version=2022-08-31"
        ),
        "host": "visual.volcengineapi.com",
        "payload": b'{"req_key":"high_aes_general_v30l_zt2i"}',
        "access_key": "AKTEST",
        "secret_key": "SKTEST",
        "region": "cn-north-1",
        "x_date": "20260820T120000Z",
    }
    first = build_volcano_v4_authorization(**kwargs)
    assert first == build_volcano_v4_authorization(**kwargs)
    # 固定向量来自火山官方 SignerV4；防止误套 AWS 的密钥前缀规则。
    assert first == (
        "HMAC-SHA256 Credential=AKTEST/20260820/cn-north-1/cv/request, "
        "SignedHeaders=content-type;host;x-content-sha256;x-date, "
        "Signature=e05796fbdcef00f5782b4b984adb309ce231d8047193567a33b91f0f50e72d8a"
    )
    # 签名对 SK 敏感：密钥变化必须导致签名变化。
    changed = build_volcano_v4_authorization(**{**kwargs, "secret_key": "SKOTHER"})
    assert changed != first


def _cv_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_volcano_client_uses_async_text_to_image_contract() -> None:
    """通用 3.0 必须按官方合同先提交任务，再轮询结果。"""
    encoded = base64.b64encode(PNG_BYTES).decode("ascii")
    actions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.params["Action"]
        actions.append(action)
        body = json.loads(request.content.decode("utf-8"))
        assert request.url.params["Version"] == "2022-08-31"
        assert request.headers["Authorization"].startswith(
            "HMAC-SHA256 Credential=AKTEST/"
        )
        if action == "CVSync2AsyncSubmitTask":
            assert body == {
                "req_key": "high_aes_general_v30l_zt2i",
                "prompt": "画面",
            }
            return httpx.Response(
                200,
                json={"code": 10000, "data": {"task_id": "task-1"}},
            )
        assert action == "CVSync2AsyncGetResult"
        assert body == {
            "req_key": "high_aes_general_v30l_zt2i",
            "task_id": "task-1",
        }
        return httpx.Response(
            200,
            json={
                "code": 10000,
                "data": {
                    "status": "done",
                    "binary_data_base64": [encoded],
                },
            },
        )

    client = VolcanoCVClient(
        access_key="AKTEST", secret_key="SKTEST", transport=_cv_transport(handler),
    )
    assert client.text_to_image("画面") == PNG_BYTES
    assert actions == ["CVSync2AsyncSubmitTask", "CVSync2AsyncGetResult"]


def test_volcano_client_uses_async_seededit_contract() -> None:
    """SeedEdit 3.0 使用官方 req_key，并以单张 Base64 图片提交任务。"""
    encoded_input = base64.b64encode(JPEG_BYTES).decode("ascii")
    encoded_output = base64.b64encode(PNG_BYTES).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.params["Action"]
        body = json.loads(request.content.decode("utf-8"))
        if action == "CVSync2AsyncSubmitTask":
            assert body == {
                "req_key": "seededit_v3.0",
                "prompt": "换成海边背景",
                "binary_data_base64": [encoded_input],
            }
            return httpx.Response(
                200,
                json={"code": 10000, "data": {"task_id": "task-2"}},
            )
        assert action == "CVSync2AsyncGetResult"
        assert body == {"req_key": "seededit_v3.0", "task_id": "task-2"}
        return httpx.Response(
            200,
            json={
                "code": 10000,
                "data": {
                    "status": "done",
                    "binary_data_base64": [encoded_output],
                },
            },
        )

    client = VolcanoCVClient(
        access_key="AKTEST", secret_key="SKTEST", transport=_cv_transport(handler),
    )
    assert client.image_to_image("换成海边背景", JPEG_BYTES) == PNG_BYTES


def test_volcano_client_polls_until_task_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """排队与生成中状态继续查询，且不会把任务标识写入日志。"""
    encoded = base64.b64encode(PNG_BYTES).decode("ascii")
    query_results = iter(
        [
            {"status": "in_queue"},
            {"status": "generating"},
            {"status": "done", "binary_data_base64": [encoded]},
        ]
    )
    query_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_count
        if request.url.params["Action"] == "CVSync2AsyncSubmitTask":
            return httpx.Response(
                200,
                json={"code": 10000, "data": {"task_id": "task-private"}},
            )
        query_count += 1
        return httpx.Response(
            200,
            json={"code": 10000, "data": next(query_results)},
        )

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.utils.volcano.cv_client.asyncio.sleep", no_sleep)
    client = VolcanoCVClient(
        access_key="AKTEST", secret_key="SKTEST", transport=_cv_transport(handler),
    )
    assert client.text_to_image("画面") == PNG_BYTES
    assert query_count == 3


@pytest.mark.parametrize("status", ["not_found", "expired"])
def test_volcano_client_stops_on_terminal_task_status(status: str) -> None:
    """任务丢失或过期是终态，必须立即失败而不是无限轮询。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["Action"] == "CVSync2AsyncSubmitTask":
            return httpx.Response(
                200,
                json={"code": 10000, "data": {"task_id": "task-private"}},
            )
        return httpx.Response(
            200,
            json={"code": 10000, "data": {"status": status}},
        )

    client = VolcanoCVClient(
        access_key="AKTEST", secret_key="SKTEST", transport=_cv_transport(handler),
    )
    with pytest.raises(VolcanoCVError, match=status.upper()):
        client.text_to_image("画面")


def test_volcano_client_retries_only_network_and_5xx() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, json={})

    client = VolcanoCVClient(
        access_key="AKTEST", secret_key="SKTEST", max_retries=1,
        transport=_cv_transport(handler),
    )
    with pytest.raises(VolcanoCVError):
        client.text_to_image("画面")
    assert attempts["n"] == 2  # 1 次原始 + 1 次有限重试


def test_volcano_client_enforces_total_request_deadline() -> None:
    """单次慢响应必须在总期限内取消，不能依赖 HTTPX 分阶段超时。"""
    delay_seconds = 0.5

    def handler(request: httpx.Request):
        async def delayed_response() -> httpx.Response:
            await asyncio.sleep(delay_seconds)
            return httpx.Response(
                200,
                json={"code": 10000, "data": {"task_id": "task-private"}},
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            time.sleep(delay_seconds)
            return httpx.Response(
                200,
                json={"code": 10000, "data": {"task_id": "task-private"}},
            )
        return delayed_response()

    client = VolcanoCVClient(
        access_key="AKTEST",
        secret_key="SKTEST",
        timeout_seconds=0.02,
        max_retries=0,
        transport=_cv_transport(handler),
    )
    started = time.monotonic()
    with pytest.raises(VolcanoCVError, match="VOLCANO_CV_TIMEOUT"):
        client.text_to_image("prompt-private")
    assert time.monotonic() - started < 0.25


def test_volcano_client_transport_error_logs_controlled_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "https://secret.example/?Authorization=secret",
            request=request,
        )

    client = VolcanoCVClient(
        access_key="AKTEST", secret_key="SKTEST", max_retries=0,
        transport=_cv_transport(handler),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(VolcanoCVError):
        client.text_to_image("prompt-secret")
    assert "code=VOLCANO_CV_TRANSPORT_ERROR" in caplog.text
    assert "secret.example" not in caplog.text
    assert "prompt-secret" not in caplog.text


def test_volcano_client_http_4xx_does_not_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(
            401,
            json={
                "code": 50200,
                "message": "prompt-secret Authorization secret",
                "request_id": "request-secret",
                "ResponseMetadata": {
                    "Error": {
                        "Code": "SignatureDoesNotMatch",
                        "Message": "response-secret",
                    },
                },
            },
        )

    client = VolcanoCVClient(
        access_key="AKTEST", secret_key="SKTEST", max_retries=2,
        transport=_cv_transport(handler),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(VolcanoCVError):
        client.text_to_image("prompt-secret")
    assert attempts["n"] == 1
    assert "status=401" in caplog.text
    assert "code=HTTP_401_SIGNATURE" in caplog.text
    assert "provider_code=SignatureDoesNotMatch" in caplog.text
    assert "business_code=50200" in caplog.text
    assert "request_id_present=True" in caplog.text
    assert "prompt-secret" not in caplog.text
    assert "Authorization secret" not in caplog.text
    assert "request-secret" not in caplog.text
    assert "response-secret" not in caplog.text


def test_volcano_query_failure_does_not_log_task_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """查询失败日志只保留受控码，不泄漏异步任务标识或 prompt。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["Action"] == "CVSync2AsyncSubmitTask":
            return httpx.Response(
                200,
                json={"code": 10000, "data": {"task_id": "task-private"}},
            )
        return httpx.Response(
            400,
            json={
                "code": 50200,
                "message": "prompt-private task-private",
                "request_id": "request-private",
            },
        )

    client = VolcanoCVClient(
        access_key="AKTEST",
        secret_key="SKTEST",
        max_retries=0,
        transport=_cv_transport(handler),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(VolcanoCVError):
        client.text_to_image("prompt-private")
    assert "action=CVSync2AsyncGetResult" in caplog.text
    assert "business_code=50200" in caplog.text
    assert "task-private" not in caplog.text
    assert "prompt-private" not in caplog.text
    assert "request-private" not in caplog.text


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (400, "HTTP_400_PARAMETER"),
        (403, "HTTP_403_PERMISSION"),
    ],
)
def test_volcano_client_classifies_other_4xx(
    caplog: pytest.LogCaptureFixture,
    status_code: int,
    expected_code: str,
) -> None:
    client = VolcanoCVClient(
        access_key="AKTEST", secret_key="SKTEST", max_retries=0,
        transport=_cv_transport(
            lambda request: httpx.Response(status_code, json={}),
        ),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(VolcanoCVError):
        client.text_to_image("画面")
    assert f"status={status_code}" in caplog.text
    assert f"code={expected_code}" in caplog.text


def test_volcano_client_business_failure_no_retry() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(200, json={"code": 5011, "message": "quota"})

    client = VolcanoCVClient(
        access_key="AKTEST", secret_key="SKTEST", max_retries=2,
        transport=_cv_transport(handler),
    )
    with pytest.raises(VolcanoCVError):
        client.text_to_image("画面")
    assert attempts["n"] == 1  # 4xx 业务失败不重试


def test_volcano_client_credential_missing_fails_closed() -> None:
    with pytest.raises(VolcanoCVError):
        VolcanoCVClient(access_key="", secret_key="SKTEST")


def test_sniff_image_mime_whitelist() -> None:
    assert sniff_image_mime(PNG_BYTES) == "image/png"
    assert sniff_image_mime(JPEG_BYTES) == "image/jpeg"
    assert sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image_mime(b"GIF89a....") is None


# ---------------------------------------------------------------------------
# 交付物 3：OSS 对象级公共读上传（假 SDK 模块，零真实桶写入）
# ---------------------------------------------------------------------------

def test_aliyun_oss_v2_runtime_dependency_is_installed() -> None:
    """真实 OSS SDK 必须可导入且提供当前适配器依赖的顶层 API。"""
    oss = importlib.import_module("alibabacloud_oss_v2")
    assert callable(oss.Client)
    assert callable(oss.PutObjectRequest)


def test_upload_public_bytes_sets_public_read_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakePutObjectRequest:
        def __init__(self, *, bucket: str, key: str, body: object, acl: str, content_type: str) -> None:
            captured.update(
                bucket=bucket, key=key, body=body, acl=acl, content_type=content_type,
            )

    class FakeResult:
        status_code = 200

    class FakeClient:
        def put_object(self, request: FakePutObjectRequest) -> FakeResult:
            return FakeResult()

    fake_module = types.SimpleNamespace(
        credentials=types.SimpleNamespace(
            StaticCredentialsProvider=lambda **_: None,
        ),
        config=types.SimpleNamespace(load_default=lambda: types.SimpleNamespace()),
        Client=lambda _cfg: FakeClient(),
        PutObjectRequest=FakePutObjectRequest,
    )
    monkeypatch.setitem(sys.modules, "alibabacloud_oss_v2", fake_module)
    client = AliyunOSSClient("AK", "SK", "bucket", "cn-hangzhou", "oss-cn-hangzhou.aliyuncs.com")
    url = client.upload_public_bytes(PNG_BYTES, "memoir/images/x.png", "image/png")
    assert url == "https://bucket.oss-cn-hangzhou.aliyuncs.com/memoir/images/x.png"
    assert captured["acl"] == "public-read"
    assert captured["content_type"] == "image/png"
    assert captured["body"] == PNG_BYTES
    assert captured["key"] == "memoir/images/x.png"


def test_upload_public_bytes_reports_block_public_access_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公共 ACL 被 Bucket 策略拦截时给出固定诊断，不泄露 SDK 请求 URL。"""
    leaked_url = (
        "https://bucket.oss-cn-hangzhou.aliyuncs.com/"
        "memoir/images/private.png?credential=secret"
    )
    log_records: list[tuple[str, tuple[object, ...]]] = []

    class FakeLogger:
        def bind(self, **_: object) -> FakeLogger:
            return self

        def info(self, message: str, *args: object) -> None:
            log_records.append((message, args))

        def warning(self, message: str, *args: object) -> None:
            log_records.append((message, args))

    class FakePutObjectRequest:
        def __init__(self, **_: object) -> None:
            pass

    class FakeClient:
        def put_object(self, _request: FakePutObjectRequest) -> None:
            raise RuntimeError(
                "Http Status Code: 403; Error Code: AccessDenied; "
                "Message: Put public object acl is not allowed.; "
                f"EC: 0016-00000901; Request URL: {leaked_url}"
            )

    fake_module = types.SimpleNamespace(
        credentials=types.SimpleNamespace(
            StaticCredentialsProvider=lambda **_: None,
        ),
        config=types.SimpleNamespace(load_default=lambda: types.SimpleNamespace()),
        Client=lambda _cfg: FakeClient(),
        PutObjectRequest=FakePutObjectRequest,
    )
    monkeypatch.setitem(sys.modules, "alibabacloud_oss_v2", fake_module)
    monkeypatch.setattr(
        importlib.import_module("app.utils.aliyun.oss_client"),
        "logger",
        FakeLogger(),
    )
    client = AliyunOSSClient(
        "AK", "SK", "bucket", "cn-hangzhou", "oss-cn-hangzhou.aliyuncs.com",
    )

    with pytest.raises(AliyunOSSClientError) as exc_info:
        client.upload_public_bytes(
            PNG_BYTES,
            "memoir/images/private.png",
            "image/png",
        )

    rendered_logs = repr(log_records)
    assert exc_info.value.status_code == 403
    assert "OSS_PUBLIC_ACL_BLOCKED" in str(exc_info.value)
    assert "阻止公共访问" in str(exc_info.value)
    assert leaked_url not in str(exc_info.value)
    assert "OSS_PUBLIC_ACL_BLOCKED" in rendered_logs
    assert leaked_url not in rendered_logs


def test_upload_public_bytes_rejects_empty_payload() -> None:
    client = AliyunOSSClient("AK", "SK", "bucket", "cn-hangzhou", "oss-cn-hangzhou.aliyuncs.com")
    with pytest.raises(AliyunOSSClientError):
        client.upload_public_bytes(b"", "memoir/images/x.png", "image/png")


# ---------------------------------------------------------------------------
# 交付物 6：image_count 迁移回归（sqlite 单迁移）
# ---------------------------------------------------------------------------

def test_model_usage_image_count_migration_roundtrip() -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "agent_model_usages", metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("usage_id", sa.String(80), nullable=False),
        sa.Column("run_id", sa.String(80), nullable=False),
        sa.Column("step_id", sa.String(80), nullable=False),
    )
    metadata.create_all(engine)
    migration_path = (
        Path(__file__).parents[1]
        / "alembic" / "versions"
        / "20260820_0900_add_model_usage_image_count.py"
    )
    spec = importlib.util.spec_from_file_location("image_count_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("agent_model_usages")
        }
        assert "image_count" in columns
        migration.downgrade()
        columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("agent_model_usages")
        }
        assert "image_count" not in columns


# ---------------------------------------------------------------------------
# 交付物 4：1.0.3 包结构与 wire 注册
# ---------------------------------------------------------------------------

def test_1_0_3_graph_places_media_between_actions_and_safety() -> None:
    graph_path = (
        Path(__file__).parents[1]
        / "app" / "agents" / "memoir_agent" / "1.0.3" / "workflow.graph.py"
    )
    spec = importlib.util.spec_from_file_location("graph_1_0_3", graph_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    nodes = module.WORKFLOW_NODES
    order = [node["node_id"] for node in nodes]
    assert order == [
        "load_snapshot", "sanitize_materials", "compute_stats",
        "extract_highlights", "plan_chapters", "generate_scenes",
        "generate_actions", "enqueue_media_tasks", "safety_review",
        "publish_document",
    ]
    media = nodes[order.index("enqueue_media_tasks")]
    # 包策略：publish 前不允许 optional 节点；媒体节点必须非 optional。
    assert not media.get("optional", False)
    assert nodes[-1]["node_id"] == "publish_document" and nodes[-1]["next_nodes"] == []


def test_1_0_3_wire_version_registered() -> None:
    """升包登记检查：1.0.3 必须进 wire 版本表，否则 load_snapshot 瞬时失败。"""
    assert _TOOL_WIRE_VERSION_BY_AGENT_VERSION["1.0.3"] == "1.1.0"


def test_prompt_1_0_3_declares_image_and_title_word_contract() -> None:
    prompt_path = (
        Path(__file__).parents[1]
        / "app" / "agents" / "memoir_agent" / "1.0.3" / "prompts" / "scene-generate.v1.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "image：图片场景卡" in prompt
    assert "title_word" in prompt
    # JSON 契约逐字段声明（吸取 JSON_PARSE_FAILED 教训）。
    assert '"scene_id"' in prompt and '"scene_type"' in prompt
    assert '"source_refs"' in prompt and '"body"' in prompt and '"title_word"' in prompt


# ---------------------------------------------------------------------------
# 素材投影：sanitize 阶段携带 images 元数据
# ---------------------------------------------------------------------------

def test_sanitize_canonical_materials_carries_images_projection() -> None:
    raw = [{
        "source_ref": "diary:diary-1", "material_type": "diary",
        "sanitized_payload": {
            "text_digest": "我们去了海边。",
            "images": [
                {"photo_id": "p1", "object_key": "photos/a.jpg", "mime": "image/jpeg"},
                {"photo_id": "p2", "object_key": "", "mime": "image/jpeg"},  # 无效条目被剔除
                {"photo_id": "p3", "mime": "image/gif"},  # 缺键 + 非白名单 mime 被剔除
            ],
        },
    }]
    materials, sensitive, invalid = MemoirNodeRunner._sanitize_canonical_materials(raw)
    assert invalid == 0 and sensitive == 0
    assert materials[0]["images"] == [
        {"photo_id": "p1", "object_key": "photos/a.jpg", "mime": "image/jpeg"},
    ]
