"""M6 回忆录媒体（图片）生成服务。

职责：逐张生成场景配图——1.0.3 仅 image 场景，1.0.4+ 每场景配图
（illustrate_all_scenes=True，全部场景按 body 生成）——
1. 一律使用通用 3.0 文生图；素材照片和参考图不读取、不出域；
2. 生成结果 bytes 上传 OSS `memoir/images/` 前缀（UUID 不可猜测 object_key，
   对象级公共读），产出 D1 冻结六键 media_manifest 条目；
3. 单张失败或超节点预算 -> 该场景降级为纯文字卡（旧模式改 summary；
   每场景配图模式保留原场景类型），不重试节点、不回滚文案；
   全部失败 -> media_tasks=[]（发布纯文字 revision）；
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

from app.core.logging_uru import log_success
from app.utils.volcano.cv_client import TEXT_TO_IMAGE_REQ_KEY

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

# ---- 手绘水彩插画 prompt 模板（统一回忆录配图风格）----
# 视觉合同固定为满幅 9:16 竖版叙事插画：前、中、远景共同承载场景，避免
# 模型把“水彩纸”“呼吸感”等字面要求误解成留白海报。竖版像素由下方
# MEDIA_IMAGE_WIDTH/HEIGHT 的 API 参数保证。
# 两条实测教训（写模板必须遵守，否则模型会照做坏结果）：
# 1. 模板禁止出现 App 名/品牌等字样——模型会把 prompt 里的文字原样
#    画进图片（曾把“情侣日记”画成顶部 Logo 和标题）；
# 2. 模板禁止出现“手机/全屏/App”等词——模型会把画面装进手机样机里
#    （曾画出带刘海屏的手机 UI 截图），而不是输出竖版纯画面。
# 场景 body 填入 {场景描述} 占位符（用 replace 填充，避开 str.format 对
# 模板中其他花括号/中文标号的解析陷阱）。
# 隐私铁律：完整 prompt 只传入火山 Provider，绝不写日志/trace/checkpoint。
_ILLUSTRATION_PROMPT_TEMPLATE = """创作一幅暖调日系叙事水彩手绘插画，画面内容：{场景描述}。
输出为 9:16 竖版成片插画，画面从边缘到边缘全画面铺满，是一张完整画面，
不是海报、拼贴、相框或卡片；主体自然延展到环境中，不在上下或四周预留空白。
采用电影分镜般的生活叙事构图：清晰安排前景、主体中景、背景远景，
让人物、环境和与回忆有关的生活物件共同讲述场景；前景可出现贴近视线的物件，
中景承载人物互动或关键事件，远景补足地点、天气、光线与空间纵深。
构图有明确视觉重心与自然的不对称平衡，画面细节丰富但不杂乱，
使用门窗、桌椅、餐具、花束、雨伞、行李箱、路灯、街道、橱窗、厨房用品等
与场景相符的物件充实空间，不重复或虚构与场景无关的内容。
整体采用透明水彩与细腻手绘线稿：湿润颜料自然晕染、颜色轻微渗化、
保留细腻纸纹与真实笔触；主体和关键细节清楚，远景柔和虚化，形成空气透视，
呈现通透明亮的手绘质感，不是数字扁平插画。
色彩以暖金、茶棕、珊瑚粉、柔和橄榄绿和灰蓝为主，局部使用鲜明但克制的色彩，
让自然光影和生活细节营造温暖、治愈、有回忆感的氛围，不用大片奶油白或纯色背景。
根据画面内容自然决定是否出现人物：可以是情侣、单人、多人，也可以无人；
如果有人物，画成半写实水彩动漫风的普通成年人，约六至七头身，
五官自然不具可识别性，发丝、衣物和动作带有生活化细节，通过互动传达情绪。
如果没有人物，就通过环境、物品、光线和使用痕迹传达正在发生的生活故事。
画面任何位置都不要出现文字（包括汉字、字母、数字、招牌、路牌、标签），
不要 Logo，不要水印，不要印章，不要签名，不要边框，不要 UI。"""

# 负面风格约束：火山该 req_key 无独立 negative_prompt 通道，直接拼接在
# prompt 末尾。前半段排除留白海报、文字与手机样机，后半段排除不符合作品
# 视觉合同的卡通、3D 和横幅形态。
_ILLUSTRATION_PROMPT_NEGATIVE = (
    "不要大面积留白、不要海报式留白布局、不要居中孤立的小主体、"
    "不要空旷纯色背景、不要拼贴、不要卡片式排版；"
    "画面中不要出现任何文字、汉字、字母、数字、招牌、路牌、标签、菜单、"
    "不要Logo、不要水印、不要印章、不要签名、不要标题；"
    "不要手机、不要手机屏幕、不要手机边框、不要刘海屏、不要设备样机、"
    "不要App界面、不要UI元素、不要界面截图、不要相框；"
    "不要Q版、不要大头娃娃、不要三头身以下比例、不要夸张二次元大眼、"
    "不要扁平矢量插画、不要儿童绘本卡通、不要贴纸风、不要厚涂赛璐璐、"
    "不要照片写实、不要3D、不要CG、不要塑料感、不要厚重油画、"
    "不要过度锐化、不要荧光色、不要霓虹色、不要大面积纯黑、"
    "不要横幅构图、不要正方形画面。"
)

# 手机全屏竖版展示尺寸：txt2img 走火山通用 3.0 官方 width/height 参数。
# 936×1664 为 9:16 比例，且两边均在官方 512–2048 范围内，保持约 1.3K
# 档竖向细节；回忆录链路不走 img2img，因此尺寸合同对每张图一致。
MEDIA_IMAGE_WIDTH = 936
MEDIA_IMAGE_HEIGHT = 1664

# 场景 body 为空时的兜底画面描述（保持旧版口径不变）。
_DEFAULT_SCENE_DESCRIPTION = "一段温暖的回忆画面"


def build_illustration_prompt(scene_body: str) -> str:
    """把场景文案套进手绘水彩模板，生成最终图像 prompt。

    回忆录媒体链路只使用文生图；测试侧也用它构造期望 prompt 与 MockCVClient fail_prompts
    （fail_prompts 按 prompt 全文精确匹配，必须同源构建才不失效）。
    """
    description = scene_body.strip() or _DEFAULT_SCENE_DESCRIPTION
    return (
        _ILLUSTRATION_PROMPT_TEMPLATE.replace("{场景描述}", description)
        + "\n\n"
        + _ILLUSTRATION_PROMPT_NEGATIVE
    )


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


class MemoirMediaConfig:
    """媒体通道部署配置快照；从 Settings 收口，节点内不再散读。"""

    def __init__(
        self, *,
        provider_name: str,
        image_prefix: str,
        url_host_suffixes: tuple[str, ...],
        node_budget_seconds: float,
    ) -> None:
        self.provider_name = provider_name
        self.image_prefix = image_prefix if image_prefix.endswith("/") else f"{image_prefix}/"
        self.url_host_suffixes = url_host_suffixes or MEDIA_URL_HOST_SUFFIXES
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
            url_host_suffixes=suffixes,
            node_budget_seconds=float(getattr(settings, "MEMOIR_MEDIA_NODE_BUDGET_SECONDS", 60.0)),
        )


class MemoirMediaService:
    """逐张生成 image 场景配图；任何单张异常都只降级该场景，绝不抛出。"""

    def __init__(
        self,
        provider: object,
        uploader: MediaObjectUploader,
        config: MemoirMediaConfig,
        *,
        session: object | None = None,
    ) -> None:
        self._provider = provider
        self._uploader = uploader
        self.config = config
        self._session = session

    def generate(
        self, run: object, scenes: list[dict[str, object]],
        sanitized_material: object,
        *, illustrate_all_scenes: bool = False,
        lease_context: object = None,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """返回 (media_manifest 条目列表, 更新后的场景列表)。

        新列表为新对象（不改入参）；成功场景附加 payload={image_url, title_word?}，
        失败/超限场景降级为 summary 文本卡并去掉 title_word。
        1.0.4+ 每场景配图（illustrate_all_scenes=True）：对全部场景按 body
        生成配图，不再依赖模型规划 image 场景；单张失败时该场景原样保留为
        纯文字卡（场景类型不变、顶层 title_word 剥离），封面/总结等结构稳定。
        lease_context：Executor 绑定的租约上下文；每张图片完成后做一次
        heartbeat 续约——竖版大图 8 张串行可超 90s 单节点租约窗口，节点
        执行期内心跳让长媒体节点安全存活；心跳被 fencing 拒绝（lease 被
        reaper 接管/取消/隐私变更）则停止后续生成，剩余场景统一降级，
        杜绝僵尸写。None（独立节点单测）时零行为变化。
        """
        deadline = time.monotonic() + self.config.node_budget_seconds
        media_tasks: list[dict[str, object]] = []
        updated_scenes: list[dict[str, object]] = []
        candidate_scenes = 0
        lease_lost = False
        for scene in scenes:
            # 旧模式仅 image 场景是候选，非 image 场景零改动原样透传
            # （payload 契约：非 image 场景无 payload）。
            is_candidate = isinstance(scene, Mapping) and (
                illustrate_all_scenes or scene.get("scene_type") == "image"
            )
            if not is_candidate:
                updated_scenes.append(dict(scene))
                continue
            scene_id = scene.get("scene_id")
            body = scene.get("body") if isinstance(scene.get("body"), str) else ""
            candidate_scenes += 1
            entry: dict[str, object] | None = None
            # scene_id 无效（非字符串/为空）时不生成也不记日志（值本身不可信），
            # entry 保持 None 走统一降级路径。
            scene_ok = isinstance(scene_id, str) and bool(scene_id)
            # 图片张数不设配额：场景数随用户素材增长（最少日记 7 + 赌约 7），
            # 唯一闸门是节点时间预算——预算耗尽剩余场景降级文字卡。
            if scene_ok and time.monotonic() >= deadline:
                logging.warning(
                    "MemoirAgent 媒体节点预算耗尽降级 run_id=%s scene_id=%s code=%s",
                    getattr(run, "run_id", ""), scene_id, "MEDIA_NODE_BUDGET_EXCEEDED",
                )
            elif scene_ok and lease_lost:
                logging.warning(
                    "MemoirAgent 媒体租约丢失降级 run_id=%s scene_id=%s code=%s",
                    getattr(run, "run_id", ""), scene_id, "MEDIA_LEASE_LOST",
                )
            elif scene_ok:
                entry = self._generate_one(run, scene_id, body)
                if entry is not None:
                    media_tasks.append(entry)
                # 每张完成后立即续约：单张竖版图 9-18s，90s 租约最多撑 5-6 张，
                # 不续约则多张串行必撞租约过期（reaper 接管/写入被 fencing 拒绝）。
                if not self._heartbeat_lease(run, lease_context):
                    lease_lost = True
            updated_scenes.append(
                self._scene_after_media(scene, entry, keep_type=illustrate_all_scenes)
            )
        log_success(
            "MemoirAgent 媒体生成完成 run_id=%s illustrate_all=%s candidate=%s delivered=%s",
            getattr(run, "run_id", ""), illustrate_all_scenes, candidate_scenes,
            len(media_tasks),
        )
        return media_tasks, updated_scenes

    def _heartbeat_lease(self, run: object, lease_context: object) -> bool:
        """媒体节点内逐张续约；无上下文/无会话时视为成功（单测直连场景）。"""
        if lease_context is None or self._session is None:
            return True
        try:
            from app.services.lease_service import LeaseService

            return LeaseService(self._session).heartbeat(
                str(getattr(run, "run_id", "")), lease_context,
            )
        except Exception:
            # 续约异常按租约丢失处理（fail-closed），剩余场景降级。
            logging.warning(
                "MemoirAgent 媒体租约心跳异常 run_id=%s code=%s",
                getattr(run, "run_id", ""), "MEDIA_LEASE_HEARTBEAT_FAILED",
            )
            return False

    def _scene_after_media(
        self, scene: Mapping, entry: dict[str, object] | None,
        *, keep_type: bool = False,
    ) -> dict[str, object]:
        """按生成结果重写场景：成功挂 payload，失败降级为纯文字卡。"""
        if entry is None:
            # 降级：旧模式 scene_type 改 summary；每场景配图模式保留原类型
            # （cover 失败仍是封面文字卡），仅 image 类型例外——发布契约要求
            # image 场景必须带 payload，故统一降级 summary。
            degraded_type = (
                scene.get("scene_type")
                if keep_type and scene.get("scene_type") != "image"
                else "summary"
            )
            # 降级场景不携带顶层 title_word（发布边界不接受该顶层字段）。
            return {
                "scene_id": scene.get("scene_id"),
                "scene_type": degraded_type,
                "source_refs": list(scene.get("source_refs") or []),
                **({"body": scene["body"]} if isinstance(scene.get("body"), str) else {}),
            }
        payload: dict[str, object] = {"image_url": entry["url"]}
        title_word = scene.get("title_word")
        if isinstance(title_word, str) and title_word:
            payload["title_word"] = title_word
        # payload 白名单仅 {image_url, title_word}；成功场景保留原场景类型
        # （旧模式候选必为 image，此处等价），顶层 title_word 收进 payload。
        return {
            "scene_id": scene.get("scene_id"),
            "scene_type": scene.get("scene_type"),
            "source_refs": list(scene.get("source_refs") or []),
            **({"body": scene["body"]} if isinstance(scene.get("body"), str) else {}),
            "payload": payload,
        }

    def _generate_one(
        self, run: object, scene_id: str, body: str,
    ) -> dict[str, object] | None:
        """生成并上传一张图片；任何异常都吞掉并返回 None（该场景降级）。"""
        started = time.monotonic()
        failure_stage, failure_code = "provider", "MEDIA_PROVIDER_FAILED"
        try:
            # 回忆录视觉合同只允许提示词驱动：不读取素材照片、不发送参考图，
            # 确保每个场景都使用统一的 9:16 画幅和同一套水彩构图约束。
            prompt = build_illustration_prompt(body)
            data = self._provider.text_to_image(
                prompt, width=MEDIA_IMAGE_WIDTH, height=MEDIA_IMAGE_HEIGHT,
            )
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
            log_success(
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
            logging.warning(
                "MemoirAgent 媒体单张失败降级 run_id=%s scene_id=%s "
                "stage=%s elapsed_ms=%s code=%s",
                getattr(run, "run_id", ""), scene_id, failure_stage,
                int((time.monotonic() - started) * 1000), failure_code,
            )
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
