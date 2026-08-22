"""M6 回忆录媒体（图片）生成服务。

职责：对 1.0.3+ 播放文档中的 image 场景逐张生成图片——
1. 选择模式：素材含 images 且照片出域门禁开启 -> 图生图（SeedEdit 3.0），
   否则一律使用通用 3.0 文生图；
2. 生成结果 bytes 上传 OSS `memoir/images/` 前缀（UUID 不可猜测 object_key，
   对象级公共读），产出 D1 冻结六键 media_manifest 条目；
3. 单张失败/超预算/超配额 -> 该场景降级为 summary 文本卡，不重试节点、
   不回滚文案；全部失败 -> media_tasks=[]（发布纯文字 revision）；
4. 按张计量：每张成功图片写一行 AgentModelUsage（image_count=1，
   cost_unit=per_image），不经过 LLM token 计量路径。

隐私铁律：prompt（场景文案）、图片字节、照片字节、临时 URL 只在内存流转，
绝不写日志/trace/checkpoint。日志只记 scene_id、模式、成败、耗时、张数。
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping
from typing import Protocol
from urllib.parse import urlparse

from app.utils.volcano.cv_client import (
    IMAGE_TO_IMAGE_REQ_KEY,
    TEXT_TO_IMAGE_REQ_KEY,
)

# ---- D1 冻结 wire 契约常量（Runtime 侧唯一口径，runner 校验复用）----
# media_manifest 条目键集精确六键（snake_case）。
MEDIA_MANIFEST_KEYS: frozenset[str] = frozenset({
    "media_id", "kind", "object_key", "url", "mime", "scene_id",
})
MEDIA_KIND_IMAGE = "image"
MEDIA_IMAGE_PREFIX = "memoir/images/"
MEDIA_IMAGE_MIME_TYPES: frozenset[str] = frozenset({
    "image/jpeg", "image/png", "image/webp",
})
# 媒体 URL 的 OSS 域名后缀白名单（默认值，可被部署配置覆盖）。
MEDIA_URL_HOST_SUFFIXES: tuple[str, ...] = ("aliyuncs.com",)


def sniff_image_mime(data: bytes) -> str | None:
    """按魔数识别图片 MIME；仅接受契约白名单三种格式。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class MediaObjectUploader(Protocol):
    """OSS 上传最小接口（AliyunOSSClient.upload_public_bytes 满足）。"""

    def upload_public_bytes(self, data: bytes, object_key: str, mime: str) -> str: ...


class PhotoLoader(Protocol):
    """业务私有桶照片取回最小接口：object_key -> bytes。"""

    def __call__(self, object_key: str) -> bytes: ...


class MemoirMediaConfig:
    """媒体通道部署配置快照；从 Settings 收口，节点内不再散读。"""

    def __init__(
        self, *,
        provider_name: str,
        image_prefix: str,
        max_images_per_run: int,
        url_host_suffixes: tuple[str, ...],
        photo_egress_enabled: bool,
        provider_residency: str,
        node_budget_seconds: float,
    ) -> None:
        self.provider_name = provider_name
        self.image_prefix = image_prefix if image_prefix.endswith("/") else f"{image_prefix}/"
        self.max_images_per_run = max(0, int(max_images_per_run))
        self.url_host_suffixes = url_host_suffixes or MEDIA_URL_HOST_SUFFIXES
        self.photo_egress_enabled = photo_egress_enabled
        self.provider_residency = provider_residency
        self.node_budget_seconds = float(node_budget_seconds)

    @classmethod
    def from_settings(cls, settings: object) -> MemoirMediaConfig:
        raw_suffixes = str(getattr(settings, "MEMOIR_MEDIA_URL_HOST_SUFFIXES", "") or "")
        suffixes = tuple(
            item.strip() for item in raw_suffixes.split(",") if item.strip()
        ) or MEDIA_URL_HOST_SUFFIXES
        return cls(
            provider_name=str(getattr(settings, "MEMOIR_MEDIA_PROVIDER", "mock") or "mock"),
            image_prefix=str(getattr(settings, "MEMOIR_MEDIA_IMAGE_PREFIX", MEDIA_IMAGE_PREFIX)),
            max_images_per_run=int(getattr(settings, "MEMOIR_MEDIA_MAX_IMAGES_PER_RUN", 8)),
            url_host_suffixes=suffixes,
            photo_egress_enabled=bool(getattr(settings, "MEMOIR_MEDIA_PHOTO_EGRESS_ENABLED", False)),
            provider_residency=str(getattr(settings, "MEMOIR_MEDIA_PROVIDER_RESIDENCY", "private")),
            node_budget_seconds=float(getattr(settings, "MEMOIR_MEDIA_NODE_BUDGET_SECONDS", 60.0)),
        )

    @property
    def photo_egress_allowed(self) -> bool:
        """照片出域门禁：门禁开关开启且 Provider 数据驻留为 public 才放行。

        两个独立开关默认都关闭/私有——任一不满足即 fail-closed 走文生图。
        """
        return self.photo_egress_enabled and self.provider_residency == "public"


class MemoirMediaService:
    """逐张生成 image 场景配图；任何单张异常都只降级该场景，绝不抛出。"""

    def __init__(
        self,
        provider: object,
        uploader: MediaObjectUploader,
        config: MemoirMediaConfig,
        *,
        photo_loader: PhotoLoader | None = None,
        session: object | None = None,
    ) -> None:
        self._provider = provider
        self._uploader = uploader
        self.config = config
        # photo_loader 为 None 时（未配置私有桶代理）永远走文生图。
        self._photo_loader = photo_loader
        self._session = session

    def generate(
        self, run: object, scenes: list[dict[str, object]],
        sanitized_material: object,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """返回 (media_manifest 条目列表, 更新后的场景列表)。

        新列表为新对象（不改入参）；成功场景附加 payload={image_url, title_word?}，
        失败/超限场景降级为 summary 文本卡并去掉 title_word。
        """
        deadline = time.monotonic() + self.config.node_budget_seconds
        image_quota = self._image_quota_for(run)
        media_tasks: list[dict[str, object]] = []
        updated_scenes: list[dict[str, object]] = []
        delivered = self._images_delivered(run)
        image_scenes = 0
        for scene in scenes:
            # 非 image 场景零改动原样透传（payload 契约：非 image 场景无 payload）。
            if not (isinstance(scene, Mapping) and scene.get("scene_type") == "image"):
                updated_scenes.append(dict(scene))
                continue
            scene_id = scene.get("scene_id")
            body = scene.get("body") if isinstance(scene.get("body"), str) else ""
            image_scenes += 1
            entry: dict[str, object] | None = None
            # scene_id 无效（非字符串/为空）时不生成也不记日志（值本身不可信），
            # entry 保持 None 走统一降级路径。
            scene_ok = isinstance(scene_id, str) and bool(scene_id)
            if scene_ok and delivered >= image_quota:
                logging.info(
                    "MemoirAgent 媒体配额已满降级 run_id=%s scene_id=%s code=%s",
                    getattr(run, "run_id", ""), scene_id, "MEDIA_IMAGE_QUOTA_EXCEEDED",
                )
            elif scene_ok and time.monotonic() >= deadline:
                logging.info(
                    "MemoirAgent 媒体节点预算耗尽降级 run_id=%s scene_id=%s code=%s",
                    getattr(run, "run_id", ""), scene_id, "MEDIA_NODE_BUDGET_EXCEEDED",
                )
            elif scene_ok:
                entry = self._generate_one(run, scene_id, body, sanitized_material)
                if entry is not None:
                    delivered += 1
                    media_tasks.append(entry)
            updated_scenes.append(self._scene_after_media(scene, entry))
        logging.info(
            "MemoirAgent 媒体生成完成 run_id=%s image_scene=%s delivered=%s",
            getattr(run, "run_id", ""), image_scenes, len(media_tasks),
        )
        return media_tasks, updated_scenes

    def _scene_after_media(
        self, scene: Mapping, entry: dict[str, object] | None,
    ) -> dict[str, object]:
        """按生成结果重写场景：成功挂 payload，失败降级 summary 文本卡。"""
        if entry is None:
            # 降级：scene_type 改 summary、去掉 title_word，正文保留为文本卡文案。
            return {
                "scene_id": scene.get("scene_id"),
                "scene_type": "summary",
                "source_refs": list(scene.get("source_refs") or []),
                **({"body": scene["body"]} if isinstance(scene.get("body"), str) else {}),
            }
        payload: dict[str, object] = {"image_url": entry["url"]}
        title_word = scene.get("title_word")
        if isinstance(title_word, str) and title_word:
            payload["title_word"] = title_word
        # payload 白名单仅 {image_url, title_word}；成功场景不携带顶层 title_word。
        return {
            "scene_id": scene.get("scene_id"),
            "scene_type": "image",
            "source_refs": list(scene.get("source_refs") or []),
            **({"body": scene["body"]} if isinstance(scene.get("body"), str) else {}),
            "payload": payload,
        }

    def _generate_one(
        self, run: object, scene_id: str, body: str, sanitized_material: object,
    ) -> dict[str, object] | None:
        """生成并上传一张图片；任何异常都吞掉并返回 None（该场景降级）。"""
        started = time.monotonic()
        failure_stage, failure_code = "provider", "MEDIA_PROVIDER_FAILED"
        try:
            reference = self._reference_photo(scene_id, sanitized_material)
            # 图像 prompt 只使用已过安全审核口径的场景文案（≤80 字、无敏感标识）。
            prompt = body.strip() or "一段温暖的回忆画面"
            if reference is not None:
                data = self._provider.image_to_image(prompt, reference)
                mode, req_key = "img2img", IMAGE_TO_IMAGE_REQ_KEY
            else:
                data = self._provider.text_to_image(prompt)
                mode, req_key = "txt2img", TEXT_TO_IMAGE_REQ_KEY

            failure_stage = "image_validation"
            failure_code = "MEDIA_IMAGE_FORMAT_INVALID"
            mime = sniff_image_mime(data)
            if mime is None:
                raise ValueError(failure_code)
            extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime]
            # 不可猜测 object_key：前缀 + UUID4 + 按_mime 扩展名。
            object_key = f"{self.config.image_prefix}{uuid.uuid4()}.{extension}"

            failure_stage = "oss_upload"
            failure_code = "MEDIA_OSS_UPLOAD_FAILED"
            url = self._uploader.upload_public_bytes(data, object_key, mime)

            failure_stage = "url_validation"
            failure_code = "MEDIA_URL_VALIDATION_FAILED"
            self._require_contract_url(url)
            self._record_usage(run, req_key=req_key, succeeded=True)
            logging.info(
                "MemoirAgent 媒体单张完成 run_id=%s scene_id=%s mode=%s elapsed_ms=%s",
                getattr(run, "run_id", ""), scene_id, mode, int((time.monotonic() - started) * 1000),
            )
            return {
                "media_id": f"media-{uuid.uuid4()}",
                "kind": MEDIA_KIND_IMAGE,
                "object_key": object_key,
                "url": url,
                "mime": mime,
                "scene_id": scene_id,
            }
        except Exception:
            # 隐私优先：不记录异常正文（可能含 URL/字节信息），只记受控阶段码。
            self._record_usage(run, req_key=None, succeeded=False)
            logging.info(
                "MemoirAgent 媒体单张失败降级 run_id=%s scene_id=%s "
                "stage=%s elapsed_ms=%s code=%s",
                getattr(run, "run_id", ""), scene_id, failure_stage,
                int((time.monotonic() - started) * 1000), failure_code,
            )
            return None

    def _reference_photo(self, scene_id: str, sanitized_material: object) -> bytes | None:
        """按 image 场景的素材引用解析参考照片字节；门禁未开直接返回 None。

        只消费 sanitize_materials 投影出的 images 元数据
        （{photo_id, object_key, mime}），绝不回读原始快照。
        """
        if not self.config.photo_egress_allowed or self._photo_loader is None:
            return None
        if not isinstance(sanitized_material, Mapping):
            return None
        materials = sanitized_material.get("materials")
        if not isinstance(materials, list):
            return None
        for item in materials:
            if not isinstance(item, Mapping) or not isinstance(item.get("images"), list):
                continue
            for image in item["images"]:
                if not isinstance(image, Mapping):
                    continue
                object_key = image.get("object_key")
                if isinstance(object_key, str) and object_key:
                    try:
                        return self._photo_loader(object_key)
                    except Exception:
                        # 照片取回失败按无参考图处理，回退文生图，不中断节点。
                        logging.info(
                            "MemoirAgent 参考照片取回失败回退文生图 scene_id=%s code=%s",
                            scene_id, "MEDIA_PHOTO_FETCH_FAILED",
                        )
                        return None
        return None

    def _require_contract_url(self, url: str) -> None:
        """上传返回 URL 必须满足 D1 契约：https + 域后缀白名单 + 前缀路径。"""
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("MEDIA_URL_SCHEME_INVALID")
        if not any(
            parsed.netloc == suffix or parsed.netloc.endswith(f".{suffix}")
            for suffix in self.config.url_host_suffixes
        ):
            raise ValueError("MEDIA_URL_HOST_INVALID")
        if not parsed.path.startswith(f"/{self.config.image_prefix}"):
            raise ValueError("MEDIA_URL_PATH_INVALID")

    def _image_quota_for(self, run: object) -> int:
        """按张配额：优先 run.model_policy.max_media_images，缺省用部署默认。"""
        policy = None
        snapshot = getattr(run, "capability_snapshot_json", None)
        if isinstance(snapshot, Mapping):
            candidate = snapshot.get("model_policy")
            policy = candidate if isinstance(candidate, Mapping) else None
        if policy is not None:
            value = policy.get("max_media_images")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return self.config.max_images_per_run

    def _images_delivered(self, run: object) -> int:
        """统计本 Run 已交付图片张数（按 AgentModelUsage.image_count 求和）。"""
        session = self._session
        if session is None:
            return 0
        try:
            from sqlalchemy import func, select

            from app.models import AgentModelUsage

            used = session.scalar(
                select(func.sum(AgentModelUsage.image_count)).where(
                    AgentModelUsage.run_id == getattr(run, "run_id", "")
                )
            )
            return int(used or 0)
        except Exception:
            # 统计失败不阻断节点：按 0 已用量处理，配额仍在节点内按张递减。
            logging.warning("MemoirAgent 媒体用量统计失败 code=%s", "MEDIA_USAGE_QUERY_FAILED")
            return 0

    def _record_usage(self, run: object, *, req_key: str | None, succeeded: bool) -> None:
        """按张计量：每张图片写一行 usage；不经过 LLM token 计量路径。

        session 未注入（单元测试直连 runner）时跳过落库，只保留内存行为。
        """
        session = self._session
        if session is None:
            return
        try:
            from datetime import UTC, datetime

            from app.models import AgentModelUsage

            session.add(AgentModelUsage(
                usage_id=f"memoir-media-{uuid.uuid4()}",
                run_id=str(getattr(run, "run_id", "")),
                step_id="enqueue_media_tasks",
                execution_attempt=int(getattr(run, "execution_attempt", 1) or 1),
                model_attempt=1,
                status="succeeded" if succeeded else "failed",
                provider=self.config.provider_name,
                model=req_key,
                cost_unit="per_image",
                # 仅成功交付的图片按张计数；失败尝试保留审计行但不占配额。
                image_count=1 if succeeded else 0,
                estimated_cost=0.0,
                request_deadline_at=datetime.now(UTC),
            ))
            session.commit()
        except Exception:
            # 计量失败绝不中断生成主链；回滚本次 add 防止会话残留。
            logging.warning("MemoirAgent 媒体用量落库失败 code=%s", "MEDIA_USAGE_WRITE_FAILED")
            try:
                session.rollback()
            except Exception:
                pass
