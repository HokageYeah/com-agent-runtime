"""MemoirAgent 已实现节点的受信任 Runner。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid as uuid_module
from collections.abc import Mapping
from itertools import zip_longest
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.logging_uru import log_success
from app.models import AgentRun
from app.runtime.bounded_loop import InheritedLoopBudget, LoopIterationResult
from app.runtime.context_manager import ContextManager
from app.runtime.evaluator import MemoirPlaybackEvaluator
from app.runtime.guardrails import MemoirGuardrails
from app.runtime.interfaces import LeaseContext
from app.runtime.json_repair import parse_json_once
from app.runtime.material_schema import (
    detect_envelope_mixing,
)
from app.runtime.prompt_registry import PromptRegistry
from app.runtime.semantic_validation import SemanticValidator
from app.runtime.state import AgentState
from app.runtime.structured_output import StructuredOutputParser
from app.runtime.tool_gateway import ToolErrorRejected, ToolGateway
from app.services.evaluation_service import EvaluationService
from app.services.memoir.memoir_media_service import (
    MEDIA_IMAGE_MIME_TYPES,
    MEDIA_IMAGE_PREFIX,
    MEDIA_KIND_IMAGE,
    MEDIA_MANIFEST_KEYS,
)
from app.services.tool_call_audit_service import ToolCallAuditService

# 回忆录摘要中的身份标识必须替换为固定占位符，避免原始值进入模型上下文。
_MATERIAL_SENSITIVE_TEXT = re.compile(
    r"(?<!\d)(?:1[3-9]\d{9}|\d{17}[\dXx])(?!\d)"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|https?://[^\s,，;；]+|www\.[^\s,，;；]+"
    r"|(?i:access[_-]?token|api[_-]?key|token|openid)\s*[:=]\s*[^\s,，;；]+"
)
# 仅归一化可明确识别的常用昵称，避免把普通中文句子误改为人名。
_MATERIAL_SELF_NICKNAME = re.compile(
    r"^(?:小|阿)[\u4e00-\u9fff](?=(?:电话|手机|邮箱|说|在|去|来|：|:|，|。|！|？|；|、|\s|$))"
)
class MemoirModelGateway(Protocol):
    """Memoir 模型节点到受信任 Gateway 的最小适配边界。

    调用方负责在此边界之下构造权威 ModelCallContext；Runner 只传递经过
    allowlist 裁剪的结构化摘要，绝不传递快照正文或 prompt 正文。
    """

    def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object: ...

    def repair(
        self,
        run_id: str,
        node_id: str,
        request: dict[str, object],
        invalid_output: object,
    ) -> object: ...


class _HighlightOutput(BaseModel):
    """高光节点的最小模型输出契约。"""

    model_config = ConfigDict(extra="forbid")

    source_refs: list[str]


class _ChapterOutput(BaseModel):
    """章节条目不允许携带正文或运行时控制字段。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    source_refs: list[str]
    kind: Literal["memory_overview"] = "memory_overview"


class _ChapterPlanOutput(BaseModel):
    """将嵌套引用汇总到顶层，供统一语义校验器校验。"""

    model_config = ConfigDict(extra="forbid")

    chapters: list[_ChapterOutput]
    source_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _collect_source_refs(self) -> _ChapterPlanOutput:
        # 不信任模型提供的顶层值，只从已通过 schema 的章节条目重新汇总。
        self.source_refs = [ref for chapter in self.chapters for ref in chapter.source_refs]
        return self


class _SceneOutput(BaseModel):
    """场景条目仅支持当前播放链路允许的安全类型。"""

    model_config = ConfigDict(extra="forbid")

    scene_id: str
    # 七种冻结场景类型（M6 起含 image），与业务端 PLAYBACK_SCENE_TYPES /
    # 前端 KNOWN_SCENE_TYPES 三端对齐；image 仅在 1.0.3+ 且媒体开关开启时
    # 由 _valid_scenes 按版本放行，旧版本模型输出 image 会被拒并模板兜底。
    scene_type: Literal["summary", "cover", "stats", "diary_highlight", "bet_highlight", "milestone", "image"]
    source_refs: list[str]
    # 正文为可选字段，最终仍由 safety_review 统一限制长度与情绪风险表达。
    body: str | None = None
    # image 场景专属标题词：≤6 汉字，可省略；仅在 1.0.3+ 透传进 payload。
    # 非 image 场景携带该字段会被 _valid_scenes 拒绝（防模型幻觉塞字段）。
    title_word: str | None = None


class _ScenePlanOutput(BaseModel):
    """将场景的嵌套引用汇总到顶层，复用统一语义校验器。"""

    model_config = ConfigDict(extra="forbid")

    scenes: list[_SceneOutput]
    source_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _collect_source_refs(self) -> _ScenePlanOutput:
        # 不信任模型提供的顶层值，只从已通过 schema 的场景条目重新汇总。
        self.source_refs = [ref for scene in self.scenes for ref in scene.source_refs]
        return self


class _BatchSceneOutput(BaseModel):
    """M7 循环体单批场景条目；与 1.0.5 scene-batch-generate 契约对齐。

    body 为必填（单批场景是最终播放卡正文，缺失会发布空白卡）；
    title_word 可省略，长度语义与 generate_scenes 路径一致（≤6 汉字）。
    """

    model_config = ConfigDict(extra="forbid")

    scene_id: str
    scene_type: Literal["summary", "cover", "stats", "diary_highlight", "bet_highlight", "milestone"]
    source_refs: list[str]
    body: str
    title_word: str | None = None


class _SceneBatchPlanOutput(BaseModel):
    """单批模型输出的顶层契约：{"scenes": [...]}，允许空数组（本批不值得成卡）。"""

    model_config = ConfigDict(extra="forbid")

    scenes: list[_BatchSceneOutput]


class _CoverageRepairSceneOutput(BaseModel):
    """M7 覆盖修复单场景契约；与 coverage-repair.v1.md prompt 逐字段对齐。

    - scene_id 必须以 "r1-" 前缀（只允许一次 repair，r1 即修复第 1 次），
      与生成批次的 s{batch_index}- 命名空间隔离；
    - scene_type 封闭四枚举：cover/summary 由生成批次固定，修复只补中间场景；
    - body 必填（修复场景同样是最终播放卡，缺失会发布空白卡）；
    - title_word 可省略，长度语义与生成路径一致（≤6 汉字）。
    """

    model_config = ConfigDict(extra="forbid")

    scene_id: str
    scene_type: Literal["stats", "diary_highlight", "bet_highlight", "milestone"]
    source_refs: list[str]
    body: str
    title_word: str | None = None


class _CoverageRepairOutput(BaseModel):
    """覆盖修复模型输出的顶层契约：{"scenes": [...]}，允许空数组（素材不足以成卡）。"""

    model_config = ConfigDict(extra="forbid")

    scenes: list[_CoverageRepairSceneOutput]


# 模板兜底场景的固定安全正文：模型不可用或输出被拒时，前端播放卡渲染 scene.body，
# 缺失正文会发布出三张空白卡。文案为字面量（不含用户素材），每条均满足
# _is_safe_playback 的 80 字上限与 MemoirGuardrails 的敏感词/情绪风险拦截。
_TEMPLATE_SCENE_BODIES: tuple[str, ...] = (
    "这一路的小事，都被好好收藏在这本回忆里。",
    "每一次并肩与交心，都是我们最珍贵的默契。",
    "往后的日子，也一起慢慢写下新的故事吧。",
)

# 播放词表（三端冻结契约的 Runtime 侧口径）：业务端发布白名单与前端 adapter
# 白名单均已放行全部 7 场景/6 动作；Runtime 生成集按版本分流——旧版本维持
# 六场景（image 不生成、media_manifest 恒空），1.0.3+ 放行 image；动作侧
# 排除 focus_image/play_tts（M4 恒关动作）。
_SCENE_TYPES: tuple[str, ...] = (
    "summary", "cover", "stats", "diary_highlight", "bet_highlight", "milestone",
)
# 1.0.3+ 媒体版本放行的场景集合（含 image）；词表本身零变更，只是版本门控。
_MEDIA_SCENE_TYPES: tuple[str, ...] = _SCENE_TYPES + ("image",)
# 媒体生成（image 场景）的最低 agent 版本；版本比较按三元组数值语义。
_MEDIA_MIN_VERSION = "1.0.3"
# 每场景配图（全场景 payload + title_word 放开）的最低版本。
_PER_SCENE_MEDIA_MIN_VERSION = "1.0.4"
_ACTION_TYPES: tuple[str, ...] = ("show_card", "type_text", "hold", "transition")

# 规则动作映射：动作是调度结构而非内容，按 scene_type 确定性推导动作类型
# （不接模型，杜绝幻觉调度与额外模型调用）；停留时长在 generate_actions 里
# 推导：show_card 固定 3000ms，type_text 按正文长度自适应（打字机 75ms/字）。
# image 场景动作冻结为 type_text（与前端 xxt-text-type 打字机渲染对齐）。
_SCENE_ACTION_RULES: dict[str, str] = {
    "cover": "show_card",
    "stats": "show_card",
    "diary_highlight": "type_text",
    "bet_highlight": "type_text",
    "milestone": "show_card",
    "summary": "show_card",
    "image": "type_text",
}

# ---- M7 bounded_loop 循环三段接口的冻结常量（仅 1.0.5+ 图可达）----
# 循环体节点：所有 memoir 图中唯一的 bounded_loop body（1.0.5 冻结声明）。
_LOOP_BODY_NODE_ID = "generate_scene_batch"
# 批内素材条数上限：与模型网关素材通道 _candidate_materials 的 8 条上限对齐，
# 超过 8 条的素材会在网关侧被静默丢弃（模型看不到正文却仍被允许引用）。
_LOOP_BATCH_MAX_MATERIALS = 8
# 网关未暴露 route 级 context_token_budget 时的保守回退值（token）：
# 素材文本已被 sanitize 截断（text ≤200 字 → ≤50 token/条），8 条上限下
# 素材侧至多 ~400 token，该回退值只作为缺失兜底，绝不放大预算。
_LOOP_BATCH_TOKEN_FALLBACK = 4096
# 五类素材类型的冻结顺序：与业务仓 MATERIAL_TYPE_ORDER 严格一致。
# available_material_types 按此序输出（evals/minimal.jsonl 第 4 行
# partial_types_diary_and_bet_only 期望 ["diary","completed_bet"] 是权威样例）。
_MATERIAL_TYPE_ORDER: tuple[str, ...] = (
    "diary", "completed_bet", "handbook_note", "matured_wish", "bucket_list_completion",
)
# 覆盖修复节点：1.0.5 图中紧随 bounded_loop 的唯一一次 repair 模型节点 id。
_REPAIR_NODE_ID = "repair_coverage_gaps"


def _version_at_least(agent_version: object, minimum: str) -> bool:
    """三元组数值版本比较；非法版本（或测试桩缺字段）返回 False。"""
    if not isinstance(agent_version, str) or not agent_version:
        return False
    try:
        current = tuple(int(part) for part in agent_version.split(".")[:3])
    except ValueError:
        return False
    return current >= tuple(int(part) for part in minimum.split("."))


def _media_version_enabled(agent_version: object) -> bool:
    """版本门控：仅 1.0.3 及以上的 Run 允许生成 image 场景。

    旧版本（或测试桩缺字段）返回 False，保证 1.0.0-1.0.2 行为零变化。
    """
    return _version_at_least(agent_version, _MEDIA_MIN_VERSION)


def _per_scene_media_enabled(agent_version: object) -> bool:
    """版本门控：仅 1.0.4 及以上的 Run 启用"每场景配图"。

    1.0.4 起媒体通道从"仅 image 场景配图"升级为"全部场景按 body 生成配图"：
    任意场景类型可携带 payload={image_url, title_word?}，title_word 顶层字段
    同步放开到任意场景；1.0.3- 行为零变化（payload 仍仅限 image 场景）。
    """
    return _version_at_least(agent_version, _PER_SCENE_MEDIA_MIN_VERSION)


class MemoirNodeRunner:
    """回忆录 MVP 节点执行器，只输出不含日记正文的结构化播放文档。"""

    def __init__(
        self,
        gateway: ToolGateway,
        audit: ToolCallAuditService | None = None,
        model_gateway: MemoirModelGateway | None = None,
        evaluation_service: EvaluationService | None = None,
        media_service: object | None = None,
    ) -> None:
        self._gateway, self._audit = gateway, audit
        self._model_gateway = model_gateway
        # 审计服务由 Worker 注入同一事务 Session；单元测试可省略持久化依赖。
        self._evaluation_service = evaluation_service
        # M6 媒体服务（MemoirMediaService）：None 时媒体节点按能力关闭跳过，
        # 旧版本 graph 与未开启媒体部署的行为完全不变。
        self._media_service = media_service
        self._playback_evaluator = MemoirPlaybackEvaluator()
        # Prompt 只从内置 package 精确读取；调用方无法指定 latest 或模板路径。
        self._prompts = PromptRegistry(Path(__file__).parents[1])
        self._contexts = ContextManager()
        self._structured_output = StructuredOutputParser()
        self._lease_context: LeaseContext | None = None
        # M7 bounded_loop 循环暂存：begin_loop 冻结素材清单与游标，迭代段消费。
        # Runner 由 Worker 按 Run 粒度构造（每次 run/resume 新建），实例级暂存
        # 不会跨 Run 泄漏；循环中途无 checkpoint，重算时 begin_loop 重新初始化。
        self._loop_materials: list[dict[str, str]] | None = None
        self._loop_cursor = 0

    def bind_lease_context(self, lease_context: LeaseContext) -> None:
        """Executor 每个节点前绑定有效写上下文，拒绝迟到工具结果落库。"""
        self._lease_context = lease_context

    def run_node(self, node: dict[str, object], run: AgentRun, state: AgentState) -> dict[str, object]:
        if node.get("node_id") == "safety_review":
            # 使用脱敏节点冻结的引用集合做 grounding；兼容独立节点单测时，
            # 仅把已在内存中的引用视为测试夹具，正式工作流一定会带 sanitized_material。
            agent_version = getattr(run, "agent_version", "")
            trusted_refs = set(
                self._safe_material_refs(state.sanitized_material, agent_version)
            )
            if not isinstance(state.sanitized_material, Mapping):
                trusted_refs = self._playback_source_refs(state.scenes)
            evaluation = self._playback_evaluator.evaluate(
                state.scenes, state.actions,
                trusted_source_refs=trusted_refs,
                enabled_capabilities=set(),
            )
            if evaluation.decision == "pass" and self._is_safe_playback(
                state.scenes, state.actions, media_tasks=state.media_tasks,
                scene_types=_MEDIA_SCENE_TYPES if _media_version_enabled(agent_version) else _SCENE_TYPES,
                # 1.0.4+ 每场景配图：任意场景可携带配图 payload。
                per_scene_media=_per_scene_media_enabled(agent_version),
                agent_version=agent_version,
            ):
                decision = "passed"
            else:
                # 不安全或不完整时回退到无素材引用的基础卡片，保证发布端不会收到畸形文档。
                state.scenes, state.actions = self._base_scenes_actions()
                # 安全回退后图片场景已不存在，媒体清单必须同步清空，
                # 否则 media_manifest 会引用已不存在的 scene_id（契约违约）。
                state.media_tasks = []
                state.fallback_flags.append("safety_fallback")
                decision = "fallback"
            if self._evaluation_service is not None:
                self._evaluation_service.record(
                    run_id=run.run_id,
                    step_id="safety_review",
                    target_type="playback_document",
                    target_id=None,
                    evaluator_type="memoir_playback",
                    evaluation=evaluation,
                )
            state.safety_report = {"decision": decision} if decision == "passed" else {"decision": decision, "reason": "INVALID_PLAYBACK_STRUCTURE"}
            # media_manifest 由媒体节点产出的六键条目直接构成（None 归一为空），
            # schema_version 维持 1.0.0 不升版（D1 冻结：媒体是增量，不是破坏性变更）。
            state.playback_document = {"schema_version": "1.0.0", "scenes": state.scenes, "actions": state.actions, "media_manifest": state.media_tasks if isinstance(state.media_tasks, list) else []}
            log_success("MemoirAgent 安全审核完成 run_id=%s decision=%s scene_count=%s", run.run_id, decision, len(state.scenes))
            return {"node_id": "safety_review", "safe": decision == "passed"}
        if node.get("node_id") == "plan_chapters":
            highlights = state.highlights if isinstance(state.highlights, dict) else {}
            refs = highlights.get("source_refs", [])
            safe_refs = [ref for ref in refs if isinstance(ref, str)][:8] if isinstance(refs, list) else []
            chapter_request: dict[str, object] = {"source_refs": safe_refs}
            # 章节规划只携带高光选中 refs 对应的素材文本，省 token 且不越权。
            chapter_materials = self._safe_material_texts(
                state.sanitized_material, safe_refs, run.agent_version,
            )
            model_data = self._model_data(
                run.run_id, "plan_chapters", chapter_request, run.agent_version,
                materials=chapter_materials,
            )
            validated_chapters = self._valid_chapters(model_data, safe_refs)
            if validated_chapters is None and model_data is not None:
                repaired = self._repair_model_data(
                    run.run_id, "plan_chapters", chapter_request, model_data,
                    run.agent_version, materials=chapter_materials,
                )
                validated_chapters = self._valid_chapters(repaired, safe_refs)
            if validated_chapters is not None:
                state.apply_tool_output(
                    "chapter_plan",
                    {"chapters": validated_chapters},
                )
                log_success(
                    "MemoirAgent 模型章节完成 run_id=%s chapter_count=%s",
                    run.run_id,
                    len(validated_chapters),
                )
                return {"node_id": "plan_chapters", "fallback": False}
            if self._model_gateway is not None:
                state.fallback_flags.append("model_invalid_chapters")
            state.apply_tool_output("chapter_plan", {"chapters": [{"chapter_id": "chapter-1", "source_refs": safe_refs, "kind": "memory_overview"}]})
            state.fallback_flags.append("template_chapters")
            logging.info("MemoirAgent 模板章节完成 run_id=%s ref_count=%s", run.run_id, len(safe_refs))
            return {"node_id": "plan_chapters", "fallback": True}
        if node.get("node_id") == "generate_scenes":
            plan = state.chapter_plan if isinstance(state.chapter_plan, dict) else {}
            chapters = plan.get("chapters", [])
            safe_chapters = self._safe_chapters(chapters)
            scene_request: dict[str, object] = {"chapters": safe_chapters}
            # 场景生成只携带章节选中 refs 对应的素材文本，省 token 且不越权。
            scene_materials = self._safe_material_texts(
                state.sanitized_material, self._source_refs(safe_chapters),
                run.agent_version,
            )
            model_data = self._model_data(
                run.run_id, "generate_scenes", scene_request, run.agent_version,
                materials=scene_materials,
            )
            validated_scenes = self._valid_scenes(
                model_data,
                self._source_refs(safe_chapters),
                run.agent_version,
            )
            if validated_scenes is None and model_data is not None:
                repaired = self._repair_model_data(
                    run.run_id, "generate_scenes", scene_request, model_data,
                    run.agent_version, materials=scene_materials,
                )
                validated_scenes = self._valid_scenes(
                    repaired,
                    self._source_refs(safe_chapters),
                    run.agent_version,
                )
            if validated_scenes is not None:
                state.apply_tool_output("scenes", validated_scenes)
                log_success(
                    "MemoirAgent 模型场景完成 run_id=%s scene_count=%s",
                    run.run_id,
                    len(validated_scenes),
                )
                return {"node_id": "generate_scenes", "fallback": False}
            if self._model_gateway is not None:
                state.fallback_flags.append("model_invalid_scenes")
            scenes: list[dict[str, object]] = []
            for index, chapter in enumerate(safe_chapters[:3], start=1):
                # _safe_chapters 已保证 source_refs 全为 str；复用 _source_refs 做类型收窄，避免再写一份推导式。
                refs = MemoirNodeRunner._source_refs([chapter])
                scenes.append(self._template_scene(index, refs))
            # 素材不足时仅补无引用基础卡，避免为凑数量重复引用用户素材。
            while len(scenes) < 3:
                scenes.append(self._template_scene(len(scenes) + 1, []))
            state.apply_tool_output("scenes", scenes)
            state.fallback_flags.append("template_scenes")
            logging.info("MemoirAgent 模板场景完成 run_id=%s scene_count=%s", run.run_id, len(state.scenes))
            return {"node_id": "generate_scenes", "fallback": True}
        if node.get("node_id") == "generate_scene_batch":
            # M7 循环体节点：真实模型调用已由 begin_loop/run_loop_iteration/
            # finalize_loop 三段接口在 bounded_loop 节点内完成（每轮至多一次）。
            # 静态计划线性遍历到达本节点时只做无副作用透传——场景已在
            # state.scenes，再次调用模型会违反单轮一次调用契约。
            return {"node_id": "generate_scene_batch", "loop_body": True}
        if node.get("node_id") == _REPAIR_NODE_ID:
            # M7 覆盖缺失收尾：覆盖完整时直通，否则唯一一次 repair 模型调用
            # 补齐缺失类型；冻结语义（fail closed、禁止模板补写）见
            # _repair_coverage_gaps 文档与设计说明 §3.3。
            return self._repair_coverage_gaps(run, state)
        if node.get("node_id") == "generate_actions":
            scenes = state.scenes if isinstance(state.scenes, list) else []
            # 动作按 scene_type 确定性映射：日记/赌约精选卡正文用打字机呈现
            # （type_text），其余用 show_card；映射表冻结在
            # _SCENE_ACTION_RULES，不接模型，保证零幻觉调度。
            state.actions = self._rule_actions(scenes)
            state.fallback_flags.append("template_actions")
            log_success("MemoirAgent 规则动作完成 run_id=%s action_count=%s", run.run_id, len(state.actions))
            return {"node_id": "generate_actions", "fallback": True}
        if node.get("node_id") == "extract_highlights":
            # 原始 snapshot 不得在此节点回读，高光只可消费脱敏视图中的非敏感引用。
            refs = self._safe_material_refs(
                state.sanitized_material, run.agent_version,
            )
            highlight_request: dict[str, object] = {"source_refs": refs}
            # 高光抽取携带全部非敏感素材文本（digest 通道），让模型看到真实细节。
            highlight_materials = self._safe_material_texts(
                state.sanitized_material, refs, run.agent_version,
            )
            model_data = self._model_data(
                run.run_id, "extract_highlights", highlight_request, run.agent_version,
                materials=highlight_materials,
            )
            validated_highlights = self._valid_highlights(model_data, refs)
            if validated_highlights is None and model_data is not None:
                repaired = self._repair_model_data(
                    run.run_id,
                    "extract_highlights",
                    highlight_request,
                    model_data,
                    run.agent_version,
                    materials=highlight_materials,
                )
                validated_highlights = self._valid_highlights(repaired, refs)
            if validated_highlights is not None:
                state.apply_tool_output(
                    "highlights",
                    {
                        "source_refs": validated_highlights,
                        "mode": "model",
                    },
                )
                log_success(
                    "MemoirAgent 模型高光完成 run_id=%s ref_count=%s",
                    run.run_id,
                    len(validated_highlights),
                )
                return {"node_id": "extract_highlights", "fallback": False}
            if self._model_gateway is not None:
                state.fallback_flags.append("model_unavailable_highlights")
            state.apply_tool_output("highlights", {"source_refs": refs, "mode": "template"})
            state.fallback_flags.append("template_highlights")
            logging.info("MemoirAgent 模板高光完成 run_id=%s ref_count=%s", run.run_id, len(refs))
            return {"node_id": "extract_highlights", "fallback": True}
        if node.get("node_id") == "compute_stats":
            # 统计只保留计数，不将任何日记/赌局正文写进后续可观测摘要。
            snapshot = state.snapshot if isinstance(state.snapshot, dict) else {}
            # compute_stats 同样要求 envelope 不混用；与 sanitize_materials 共用 fail closed。
            detect_envelope_mixing(snapshot)
            # 方案 A 契约：业务端 get_snapshot 透传 canonical materials 列表时，
            # 它是唯一事实源（按 material_type 计数），不再读 legacy envelope 键，
            # 避免同一份素材双计数；旧形状（diary_items/diaries/...）保持兼容。
            raw_materials = snapshot.get("materials")
            if isinstance(raw_materials, list):
                diary_count = sum(
                    1
                    for item in raw_materials
                    if isinstance(item, Mapping) and item.get("material_type") == "diary"
                )
                bet_count = sum(
                    1
                    for item in raw_materials
                    if isinstance(item, Mapping)
                    and item.get("material_type") == "completed_bet"
                )
                # M7 覆盖判定输入：canonical materials 的真实出现类型 ∩ 五类全集。
                present_types = {
                    str(item.get("material_type"))
                    for item in raw_materials
                    if isinstance(item, Mapping)
                    and item.get("material_type") in _MATERIAL_TYPE_ORDER
                }
            else:
                # legacy 形状：五类素材槽按 sanitize 同一组键等价推导（空列表
                # = 无真实素材，类型不算实际存在）；bet 槽与计数分支同源。
                legacy_slots: tuple[tuple[tuple[str, ...], str], ...] = (
                    (("diary_items", "diaries"), "diary"),
                    (("completed_bet_items", "completed_bets", "bet_items", "bets"), "completed_bet"),
                    (("handbook_notes",), "handbook_note"),
                    (("matured_wishes",), "matured_wish"),
                    (("bucket_list_completions",), "bucket_list_completion"),
                )
                present_types = set()
                for fields, material_type in legacy_slots:
                    raw = next((snapshot[field] for field in fields if field in snapshot), None)
                    # 槽存在且非空列表 = 该类型有真实素材（与计数分支同源取槽）。
                    if isinstance(raw, list) and raw:
                        present_types.add(material_type)
                diaries = snapshot.get("diary_items", snapshot.get("diaries", []))
                bets = snapshot.get(
                    "completed_bet_items",
                    snapshot.get("completed_bets", snapshot.get("bet_items", snapshot.get("bets", []))),
                )
                diary_count = len(diaries) if isinstance(diaries, list) else 0
                bet_count = len(bets) if isinstance(bets, list) else 0
            available_material_types = [
                material_type
                for material_type in _MATERIAL_TYPE_ORDER
                if material_type in present_types
            ]
            state.stats = {
                "diary_count": diary_count,
                "bet_count": bet_count,
                "has_material": bool(diary_count or bet_count),
                # 实际存在的合格素材类型集合（固定类型序），供下游
                # repair_coverage_gaps 做覆盖判定与缺失修复。
                "available_material_types": available_material_types,
            }
            log_success(
                "MemoirAgent 统计素材 run_id=%s diaries=%s bets=%s types=%s",
                run.run_id, diary_count, bet_count, len(available_material_types),
            )
            return {"node_id": "compute_stats", "stats_ready": True}
        if node.get("node_id") == "sanitize_materials":
            # 原始快照只允许在该节点读取；下游仅能获得最小脱敏视图。
            materials, sensitive_count, invalid_count = self._sanitize_materials(state.snapshot)
            state.apply_tool_output("sanitized_material", {"materials": materials})
            if invalid_count:
                logging.warning(
                    "MemoirAgent 素材脱敏发现异常 run_id=%s material_count=%s sensitive_count=%s error_code=%s",
                    run.run_id,
                    len(materials),
                    sensitive_count,
                    "MATERIAL_INVALID",
                )
            else:
                log_success(
                    "MemoirAgent 素材脱敏完成 run_id=%s material_count=%s sensitive_count=%s",
                    run.run_id,
                    len(materials),
                    sensitive_count,
                )
            return {"node_id": "sanitize_materials", "sanitized": True}
        if node.get("node_id") == "enqueue_media_tasks":
            # M6 媒体节点：仅 1.0.3+ 且注入了媒体服务才真正生成；其余情况保持
            # 第一版的确定性无副作用跳过语义（不解析正文、不外发请求）。
            # 节点绝不抛异常：单张失败在服务内降级为文本卡，节点永远成功返回，
            # 保证 90s 租约内 publish 前同步完成且不回滚已生成的文案。
            # 1.0.4+ 升级为每场景配图：不再要求模型规划 image 场景，媒体服务
            # 对全部场景按 body 生成配图（illustrate_all_scenes=True）。
            agent_version = getattr(run, "agent_version", "")
            per_scene = _per_scene_media_enabled(agent_version)
            scenes = state.scenes if isinstance(state.scenes, list) else []
            has_image = any(
                isinstance(scene, dict) and scene.get("scene_type") == "image"
                for scene in scenes
            )
            if not _media_version_enabled(agent_version) or self._media_service is None:
                # 能力关闭：旧版本 graph（无 image 场景）保持原样跳过、零行为变化；
                # 1.0.3 关闭媒体部署时把 image 场景降级为 summary 文本卡——否则
                # safety_review 会因 image 场景缺 manifest 条目整批回退基础卡。
                # 1.0.4+ 场景可能携带顶层 title_word（无配图时没有 payload 可收），
                # 发布边界不接受顶层 title_word，须一并剥离。
                state.media_tasks = []
                if has_image or per_scene:
                    state.scenes = [
                        dict(scene, scene_type="summary") if (
                            isinstance(scene, dict) and scene.get("scene_type") == "image"
                        ) else scene
                        for scene in scenes
                    ]
                    # 降级时同步剥离顶层 title_word（无配图场景不携带标题词）。
                    for scene in state.scenes:
                        if isinstance(scene, dict):
                            scene.pop("title_word", None)
                    state.actions = self._rule_actions(state.scenes)
                    state.fallback_flags.append("media_disabled_degraded")
                    logging.info(
                        "MemoirAgent 媒体能力关闭降级图片场景 run_id=%s code=%s",
                        run.run_id, "CAPABILITY_DISABLED",
                    )
                else:
                    logging.info(
                        "MemoirAgent 媒体能力不可用 run_id=%s code=%s",
                        run.run_id, "CAPABILITY_DISABLED",
                    )
                return {
                    "node_id": "enqueue_media_tasks",
                    "skipped": True,
                    "reason_code": "CAPABILITY_DISABLED",
                }
            if not has_image and not per_scene:
                state.media_tasks = []
                logging.info(
                    "MemoirAgent 无图片场景跳过媒体生成 run_id=%s", run.run_id,
                )
                return {"node_id": "enqueue_media_tasks", "skipped": True, "reason_code": "NO_IMAGE_SCENE"}
            # 媒体节点在最终 safety_review 之前运行，正文会成为外部图像 Provider 的
            # prompt。因此先用同一份确定性内容/引用/动作审核做外发闸门；此时尚无
            # manifest，media_pending 只放宽待生成图片的配对校验，绝不放宽正文安全。
            candidate_actions = (
                state.actions
                if isinstance(state.actions, list)
                else self._rule_actions(scenes)
            )
            trusted_refs = set(
                self._safe_material_refs(state.sanitized_material, agent_version)
            )
            if not isinstance(state.sanitized_material, Mapping):
                trusted_refs = self._playback_source_refs(scenes)
            evaluation = self._playback_evaluator.evaluate(
                scenes,
                candidate_actions,
                trusted_source_refs=trusted_refs,
                enabled_capabilities=set(),
            )
            if evaluation.decision != "pass" or not self._is_safe_playback(
                scenes,
                candidate_actions,
                scene_types=_MEDIA_SCENE_TYPES,
                per_scene_media=per_scene,
                media_pending=True,
                agent_version=agent_version,
            ):
                state.scenes, state.actions = self._base_scenes_actions()
                state.media_tasks = []
                state.fallback_flags.append("media_egress_safety_fallback")
                logging.warning(
                    "MemoirAgent 媒体外发前安全审核拒绝 run_id=%s code=%s",
                    run.run_id,
                    "MEDIA_EGRESS_SAFETY_REJECTED",
                )
                return {
                    "node_id": "enqueue_media_tasks",
                    "skipped": True,
                    "reason_code": "MEDIA_EGRESS_SAFETY_REJECTED",
                }
            # 直赋值而非 apply_tool_output：manifest 条目含敏感键集合中的 url，
            # 网关通道会拒绝；该字段由本节点受控生成，直接写入 state。
            media_tasks, updated_scenes = self._media_service.generate(
                run, scenes, state.sanitized_material,
                illustrate_all_scenes=per_scene,
                # Executor 每节点前绑定的租约上下文传给媒体服务逐张续约：
                # 8 张竖版图串行可超 90s 单节点租约，不续约会撞 reaper 接管。
                lease_context=self._lease_context,
            )
            state.media_tasks = media_tasks if isinstance(media_tasks, list) else []
            state.scenes = updated_scenes
            # 场景可能被降级为 summary，动作需按新场景表重建（规则映射，
            # 幂等无副作用），保证 safety_review 时 actions 与 scenes 一一对应。
            state.actions = self._rule_actions(state.scenes)
            # 交付目标：每场景配图模式下是全部场景，旧模式是 image 场景数；
            # 交付数少于目标即发生了单张降级，打标供发布摘要观测。
            target_count = len(scenes) if per_scene else sum(
                1 for scene in scenes
                if isinstance(scene, dict) and scene.get("scene_type") == "image"
            )
            if len(state.media_tasks) < target_count:
                state.fallback_flags.append("media_degraded")
            log_success(
                "MemoirAgent 媒体节点完成 run_id=%s per_scene=%s target=%s delivered=%s",
                run.run_id, per_scene, target_count, len(state.media_tasks),
            )
            return {
                "node_id": "enqueue_media_tasks",
                "skipped": False,
                "delivered": len(state.media_tasks),
            }
        if node.get("node_id") == "publish_document":
            # publish_document 节点 3 个工具调用共用同一 step_id 的 envelope context，
            # 统一构造一次，避免 7 字段形状在多个调用点重复拼装。
            tool_context = ToolGateway.build_tool_context(run, "publish_document")
            if not isinstance(state.playback_document, dict):
                raise ValueError("PLAYBACK_DOCUMENT_MISSING")
            archive_id, snapshot_id, epoch = run.input_json.get("archive_id"), run.input_json.get("snapshot_id"), run.input_json.get("generation_epoch")
            if not isinstance(archive_id, str) or not isinstance(snapshot_id, str) or not isinstance(epoch, int):
                raise ValueError("MEMORY_PUBLISH_REFERENCE_INVALID")
            logical_key = f"{run.run_id}:publish_document:memory.publish_playback_document:{epoch}"
            digest = hashlib.sha256(json.dumps(state.playback_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            # query-after-commit 的首要查询坐标是稳定 logical_key，不是本次重算 digest：
            # resume 时模型重算 scenes 会让 playback_document 的 digest 漂移，若用漂移 digest
            # 查询，已提交的首轮 publish 查不到 → 被迫重发 → 双发。find_publish_attempt 只按
            # run_id + logical_key 命中首轮 running/outcome_unknown/succeeded，digest 冲突保护
            # （同一 logical_key 被另一文档复用必须拒绝）留在 begin_publish/409 分支——
            # "查询已提交"与"detect 冲突"两件事彻底分离，消除摘要漂移吃掉首次提交这个特殊情况。
            committed = (
                self._audit.find_publish_attempt(run.run_id, logical_key)
                if self._audit
                else None
            )
            if committed is not None:
                reconciled = self._gateway.get_publish_result(
                    run.business_connector_id, archive_id, snapshot_id, run.run_id, epoch,
                    committed.idempotency_key, tool_context,
                )
                if reconciled is not None:
                    state.publish_result = reconciled
                    if not self._audit.succeed(
                        committed, int(reconciled["revision"]), str(reconciled["content_digest"]),
                        lease_context=self._lease_context,
                    ):
                        raise RuntimeError("PUBLISH_OUTCOME_UNKNOWN")
                    logging.info("MemoirAgent 对账恢复发布结果 run_id=%s", run.run_id)
                    return {"node_id": "publish_document", "published": True}
                logging.warning("MemoirAgent 发布未知结果尚未可对账 run_id=%s", run.run_id)
                raise RuntimeError("PUBLISH_OUTCOME_UNKNOWN")
            audit = self._audit.begin_publish(run.run_id, run.execution_attempt, logical_key, logical_key, digest, lease_context=self._lease_context) if self._audit else None
            try:
                if audit is None:
                    raise RuntimeError("PUBLISH_AUDIT_REQUIRED")
                state.publish_result = self._gateway.publish_playback_document(
                    run.business_connector_id, archive_id, run.run_id, snapshot_id, epoch,
                    state.playback_document, logical_key, audit, tool_context,
                )
            except httpx.TimeoutException:
                if audit is not None:
                    self._audit.unknown(audit, "HTTP_TIMEOUT", lease_context=self._lease_context)
                logging.warning("MemoirAgent 发布结果未知 run_id=%s", run.run_id)
                raise
            except ToolErrorRejected as exc:
                # Provider 的合法 ToolError 已由 Gateway 完整校验；这里仅消费冻结
                # code/retryable，绝不读取响应 body 或 safe_message。
                if exc.error_code == "GENERATION_SUPERSEDED":
                    if audit is not None:
                        self._audit.fail(audit, exc.error_code, retryable=False,
                                         lease_context=self._lease_context)
                    raise RuntimeError("GENERATION_SUPERSEDED") from None
                if exc.error_code == "IDEMPOTENCY_CONFLICT":
                    try:
                        reconciled = self._gateway.get_publish_result(
                            run.business_connector_id, archive_id, snapshot_id, run.run_id, epoch,
                            logical_key, tool_context,
                        )
                    except ToolErrorRejected as reconciliation_error:
                        if audit is not None:
                            self._audit.fail(
                                audit, reconciliation_error.error_code,
                                retryable=reconciliation_error.retryable,
                                error_type="business_tool_error",
                                lease_context=self._lease_context,
                            )
                        if reconciliation_error.error_code == "GENERATION_SUPERSEDED":
                            raise RuntimeError("GENERATION_SUPERSEDED") from None
                        if reconciliation_error.retryable:
                            raise RuntimeError("TOOL_RETRYABLE_FAILURE") from None
                        raise RuntimeError(reconciliation_error.error_code) from None
                    if (
                        isinstance(reconciled, dict)
                        and isinstance(reconciled.get("revision"), int)
                        and reconciled.get("content_digest") == digest
                    ):
                        state.publish_result = reconciled
                        if audit is not None and not self._audit.succeed(
                            audit, reconciled["revision"], digest,
                            lease_context=self._lease_context,
                        ):
                            state.publish_result = None
                            raise RuntimeError("PUBLISH_OUTCOME_UNKNOWN") from None
                        return {"node_id": "publish_document", "published": True}
                if audit is not None:
                    self._audit.fail(audit, exc.error_code, retryable=exc.retryable,
                                     error_type="business_tool_error",
                                     lease_context=self._lease_context)
                if exc.retryable:
                    raise RuntimeError("TOOL_RETRYABLE_FAILURE") from None
                raise RuntimeError(exc.error_code) from None
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 409:
                    # Idempotency 冲突不可信任响应正文；仅再次查询业务端已提交的
                    # revision/content_digest，并要求 digest 与本次规范化文档一致。
                    reconciled = self._gateway.get_publish_result(
                        run.business_connector_id, archive_id, snapshot_id, run.run_id, epoch,
                        logical_key, tool_context,
                    )
                    if (
                        isinstance(reconciled, dict)
                        and isinstance(reconciled.get("revision"), int)
                        and reconciled.get("content_digest") == digest
                    ):
                        state.publish_result = reconciled
                        if audit is not None and not self._audit.succeed(
                            audit, reconciled["revision"], digest,
                            lease_context=self._lease_context,
                        ):
                            state.publish_result = None
                            raise RuntimeError("PUBLISH_OUTCOME_UNKNOWN") from None
                        logging.info("MemoirAgent 409 对账恢复发布结果 run_id=%s", run.run_id)
                        return {"node_id": "publish_document", "published": True}
                    if audit is not None:
                        self._audit.fail(
                            audit, "TOOL_IDEMPOTENCY_CONFLICT", retryable=False,
                            lease_context=self._lease_context,
                        )
                    logging.warning(
                        "MemoirAgent 发布幂等冲突未通过摘要对账 run_id=%s code=%s",
                        run.run_id, "TOOL_IDEMPOTENCY_CONFLICT",
                    )
                    raise RuntimeError("TOOL_IDEMPOTENCY_CONFLICT") from None
                if audit is not None:
                    self._audit.fail(audit, f"HTTP_{exc.response.status_code}", retryable=exc.response.status_code >= 500, lease_context=self._lease_context)
                logging.warning("MemoirAgent 发布被业务端拒绝 run_id=%s status=%s", run.run_id, exc.response.status_code)
                raise
            except Exception:
                if audit is not None:
                    self._audit.fail(audit, "TOOL_CALL_FAILED", retryable=True, lease_context=self._lease_context)
                # 异常消息可能携带 HTTP 请求体；只记录受控码，不记录异常正文。
                logging.warning(
                    "MemoirAgent 发布调用异常 run_id=%s code=%s",
                    run.run_id,
                    "TOOL_CALL_FAILED",
                )
                raise
            if audit is not None:
                if not self._audit.succeed(
                    audit, int(state.publish_result["revision"]), str(state.publish_result["content_digest"]),
                    lease_context=self._lease_context,
                ):
                    state.publish_result = None
                    raise RuntimeError("PUBLISH_OUTCOME_UNKNOWN")
            log_success("MemoirAgent 已发布作品 run_id=%s archive_id=%s", run.run_id, archive_id)
            return {"node_id": "publish_document", "published": True}
        if node.get("node_id") != "load_snapshot":
            raise ValueError("MEMOIR_NODE_NOT_IMPLEMENTED")
        archive_id = run.input_json.get("archive_id")
        snapshot_id = run.input_json.get("snapshot_id")
        generation_epoch = run.input_json.get("generation_epoch")
        if (
            not isinstance(archive_id, str)
            or not isinstance(snapshot_id, str)
            or not isinstance(generation_epoch, int)
        ):
            raise ValueError("MEMORY_SNAPSHOT_REFERENCE_INVALID")
        # 读取请求必须绑定当前 Run 与 generation，防止旧 Run 读取或发布新一代归档素材。
        try:
            state.snapshot = self._gateway.get_snapshot(
                run.business_connector_id, archive_id, snapshot_id,
                run.run_id, generation_epoch,
                ToolGateway.build_tool_context(run, "load_snapshot"),
            )
        except ToolErrorRejected as exc:
            # read Tool 没有 side-effect physical attempt，不能伪造 AgentToolCall；但
            # 必须消费 Gateway 已验证的冻结分类，禁止将 HTTP/body/异常原文漏入日志。
            logging.warning(
                "MemoirAgent 快照工具被业务端拒绝 run_id=%s code=%s",
                run.run_id, exc.error_code,
            )
            if exc.error_code == "GENERATION_SUPERSEDED":
                raise RuntimeError("GENERATION_SUPERSEDED") from None
            if exc.retryable:
                raise RuntimeError("TOOL_RETRYABLE_FAILURE") from None
            raise RuntimeError(exc.error_code) from None
        log_success("MemoirAgent 已加载快照 run_id=%s archive_id=%s", run.run_id, archive_id)
        return {"node_id": "load_snapshot", "snapshot_loaded": True}

    # ------------------------------------------------------------------
    # M7 bounded_loop 三段接口：仅 1.0.5+ 图的 bounded_loop 节点可达；
    # 1.0.0-1.0.4 图无该节点类型，三个方法永远不会被调用（零影响）。
    # ------------------------------------------------------------------

    def begin_loop(
        self,
        node: dict[str, object],
        run: AgentRun,
        state: AgentState,
        budget: InheritedLoopBudget,
    ) -> None:
        """初始化循环状态：冻结素材清单 + 游标归零；不产生任何模型调用。

        fail closed 条件（缺任一即抛错，executor 统一转 LOOP_BODY_FAILED）：
        - 模型网关不可用：循环体每轮都需要一次模型调用，无网关必然整循环空转；
        - 脱敏素材视图缺失 / 无任何带安全 text 的可循环素材：没有可切批的输入。
        """
        if self._model_gateway is None:
            raise ValueError("LOOP_MODEL_GATEWAY_UNAVAILABLE")
        materials = self._loop_material_texts(state.sanitized_material)
        if not materials:
            logging.warning(
                "MemoirAgent 循环启动失败 run_id=%s code=%s",
                run.run_id, "LOOP_MATERIALS_MISSING",
            )
            raise ValueError("LOOP_MATERIALS_MISSING")
        self._loop_materials = materials
        self._loop_cursor = 0
        # 只记计数与预算快照，不记录素材正文或引用清单本身。
        logging.info(
            "MemoirAgent 循环状态初始化完成 run_id=%s material_count=%s max_iterations=%s",
            run.run_id, len(materials), budget.max_iterations,
        )

    def run_loop_iteration(
        self,
        node: dict[str, object],
        run: AgentRun,
        state: AgentState,
        iteration_index: int,
        budget: InheritedLoopBudget,
    ) -> LoopIterationResult:
        """驱动循环体 generate_scene_batch 一次模型调用（每轮至多一次）。

        - 单批切分：按素材稳定顺序装批，批内 token 总量不超过
           min(route context_token_budget, budget.remaining_tokens) 且至多 8 条；
        - 单条超限拒绝不截断：单条素材自身超限即整条剔除（安全计数），绝不
           等比压缩或截断 digest；剔除后本批为空则该轮不调模型直接 continue
           （游标已推进，不会死循环）；
        - 解析失败/结构非法：抛受控原因码（executor 按 on_iteration_error=
           continue 跳过该轮继续）；正文与模型原始输出不进异常消息与日志；
        - 完成判定：本批吃掉全部剩余素材（末批）且输出合法 → complete。
        """
        if self._loop_materials is None:
            # 契约违约：executor 保证先 begin_loop 后迭代，防御性 fail closed。
            raise ValueError("LOOP_NOT_INITIALIZED")
        materials = self._loop_materials
        if self._loop_cursor >= len(materials):
            # 素材游标已耗尽（末批可能被解析失败跳过）：防御性收敛，
            # 结构完整性交 finalize_loop 判定，不在此伪造场景。
            return LoopIterationResult(
                outcome="complete", reason_code="LOOP_MATERIALS_EXHAUSTED",
            )
        cap = self._loop_batch_token_cap(budget.remaining_tokens)
        batch: list[dict[str, str]] = []
        used_tokens = 0
        over_limit_dropped = 0
        while (
            self._loop_cursor < len(materials)
            and len(batch) < _LOOP_BATCH_MAX_MATERIALS
        ):
            material = materials[self._loop_cursor]
            tokens = MemoirNodeRunner._estimate_material_tokens(material["text"])
            if tokens > cap:
                # 单条素材自身超限：整条剔除出本批（绝不截断/压缩 digest），
                # 游标同步推进，避免下一轮重复扫描同一条造成死循环。
                self._loop_cursor += 1
                over_limit_dropped += 1
                continue
            if used_tokens + tokens > cap:
                # 本条放不进本批剩余额度：批到此为止，留给下一轮迭代
                # （稳定顺序装批，不越过本条去挑后面更小的素材）。
                break
            batch.append(material)
            used_tokens += tokens
            self._loop_cursor += 1
        if not batch:
            # 剔除后本批为空：不消耗模型调用，返回 continue；游标已推进。
            logging.warning(
                "MemoirAgent 循环批次素材全部超限剔除 run_id=%s iteration=%s dropped=%s",
                run.run_id, iteration_index, over_limit_dropped,
            )
            return LoopIterationResult(
                outcome="continue", reason_code="LOOP_BATCH_ALL_OVER_LIMIT",
            )
        # 首批看实际产出而非轮次：此前批次被跳过时本批仍可补 cover，
        # 保证 finalize 的"首 cover"结构判定可达。
        is_first_batch = not state.scenes
        is_final_batch = self._loop_cursor >= len(materials)
        batch_refs = [material["source_ref"] for material in batch]
        request: dict[str, object] = {
            # batch_index 直接用 1 基轮次：与 scene_id 的 s{batch_index}-N
            # 前缀契约配合，跨轮次天然不冲突（含被跳过的轮次）。
            "batch_index": iteration_index,
            "is_first_batch": is_first_batch,
            "is_final_batch": is_final_batch,
            "source_refs": batch_refs,
        }
        data = self._model_data(
            run.run_id, _LOOP_BODY_NODE_ID, request, run.agent_version,
            materials=batch,
        )
        if data is None:
            # 网关不可用/未成功：本批素材已消费，抛受控码交 executor 跳过继续。
            raise RuntimeError("LOOP_BATCH_MODEL_UNAVAILABLE")
        scenes = self._parse_batch_output(
            data, batch_refs,
            is_first_batch=is_first_batch, is_final_batch=is_final_batch,
        )
        if scenes is None:
            # on_iteration_error=continue：该批安全失败（素材已消费，由下游
            # repair_coverage_gaps 补覆盖），异常只携带受控原因码。
            raise RuntimeError("LOOP_BATCH_OUTPUT_INVALID")
        if scenes:
            state.apply_tool_output("scenes", [*(state.scenes or []), *scenes])
        covered_refs = {ref for scene in scenes for ref in scene["source_refs"]}
        log_success(
            "MemoirAgent 循环批次完成 run_id=%s iteration=%s batch_size=%s "
            "scene_count=%s dropped_over_limit=%s is_final=%s",
            run.run_id, iteration_index, len(batch), len(scenes),
            over_limit_dropped, is_final_batch,
        )
        if is_final_batch:
            return LoopIterationResult(
                outcome="complete", reason_code="LOOP_COMPLETE",
                output_count=len(scenes), coverage_count=len(covered_refs),
            )
        return LoopIterationResult(
            outcome="continue",
            output_count=len(scenes), coverage_count=len(covered_refs),
        )

    def finalize_loop(
        self, node: dict[str, object], run: AgentRun, state: AgentState,
    ) -> LoopIterationResult:
        """结构完整性收尾判定：>=3 场景、首个 cover、末个 summary。

        结构完整 → complete；缺首/末或不足 3 → failed（原因码，executor 转
        LOOP_BODY_FAILED）。finalize 自身不补写任何 Scene（fail closed 优于
        编造内容），覆盖补齐语义由工作流下游 repair_coverage_gaps 节点承担。
        """
        scenes = state.scenes if isinstance(state.scenes, list) else []
        reason: str | None = None
        if len(scenes) < 3:
            reason = "LOOP_SCENE_COUNT_INSUFFICIENT"
        elif scenes[0].get("scene_type") != "cover":
            reason = "LOOP_COVER_MISSING"
        elif scenes[-1].get("scene_type") != "summary":
            reason = "LOOP_SUMMARY_MISSING"
        covered_refs = {
            ref for scene in scenes if isinstance(scene, dict)
            for ref in scene.get("source_refs", []) if isinstance(ref, str)
        }
        if reason is not None:
            logging.warning(
                "MemoirAgent 循环收尾结构不完整 run_id=%s scene_count=%s code=%s",
                run.run_id, len(scenes), reason,
            )
            return LoopIterationResult(
                outcome="failed", reason_code=reason, output_count=len(scenes),
                coverage_count=len(covered_refs),
            )
        log_success(
            "MemoirAgent 循环收尾结构完整 run_id=%s scene_count=%s covered=%s",
            run.run_id, len(scenes), len(covered_refs),
        )
        return LoopIterationResult(
            outcome="complete", reason_code="LOOP_STRUCTURE_COMPLETE",
            output_count=len(scenes), coverage_count=len(covered_refs),
        )

    @staticmethod
    def _loop_material_texts(sanitized_material: object) -> list[dict[str, str]]:
        """提取循环可用的脱敏素材（sensitive=False 且带安全 text），稳定顺序去重。

        与 _safe_material_texts 的差异：不做 8 条截断（循环要遍历全部素材），
        且不带 allowlist（批内引用白名单由每轮切批结果动态决定）。
        """
        if not isinstance(sanitized_material, Mapping):
            return []
        materials = sanitized_material.get("materials")
        if not isinstance(materials, list):
            return []
        texts: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in materials:
            if not isinstance(item, Mapping):
                continue
            ref, text = item.get("source_ref"), item.get("text")
            if (
                item.get("sensitive") is not False
                or not isinstance(ref, str) or not ref or ref in seen
                or not isinstance(text, str) or not text.strip()
            ):
                continue
            seen.add(ref)
            texts.append({"source_ref": ref, "text": text})
        return texts

    @staticmethod
    def _estimate_material_tokens(text: str) -> int:
        """素材 token 估算：与 ContextManager 相同的字符近似口径（4 字符 ≈ 1）。"""
        return max(1, (len(text) + 3) // 4)

    def _loop_batch_token_cap(self, remaining_tokens: int) -> int:
        """单批素材 token 上限 = min(route 上下文窗口, Run 剩余 token)。

        route 窗口优先 duck-typing 读取网关的 context_token_budget(node_id)；
        读取失败/未暴露（当前模型网关适配器尚未提供该入口，部署接线属后续
        任务）一律回退保守冻结值，绝不放大预算。
        """
        cap = _LOOP_BATCH_TOKEN_FALLBACK
        accessor = getattr(self._model_gateway, "context_token_budget", None)
        if callable(accessor):
            try:
                value = accessor(_LOOP_BODY_NODE_ID)
            except Exception:  # noqa: BLE001 - route 预算不可得时回退冻结值。
                value = None
            if (
                isinstance(value, int) and not isinstance(value, bool) and value > 0
            ):
                cap = value
        return min(cap, max(remaining_tokens, 0))

    # ------------------------------------------------------------------
    # M7 repair_coverage_gaps 覆盖缺失收尾（仅 1.0.5+ 图可达；1.0.0-1.0.4
    # 图无该节点，方法永远不会被调用，零影响）。
    # ------------------------------------------------------------------

    def _repair_coverage_gaps(
        self, run: AgentRun, state: AgentState,
    ) -> dict[str, object]:
        """覆盖判定 + 唯一一次 repair 模型调用（设计说明 §3.3 冻结语义）。

        - 覆盖定义：available_material_types（compute_stats 产出，实际存在的
          合格素材类型）中每个类型都被任一已生成 Scene 的 source_refs 引用；
        - 全部已覆盖：不调模型直通完成，链路继续 generate_actions；
        - 有缺失：只允许一次 ModelGateway repair（coverage-repair.v1.md 契约），
          输入仅缺失类型的安全 text_digest 与真实 source_ref，走与
          generate_scene_batch 相同的网关/预算/guardrail 治理；
        - 缺失类型无安全 text_digest 投影：契约错误 fail closed（不得把无来源
          卡片或编造内容计为覆盖）；
        - 无剩余模型许可/预算、输出违反 JSON 契约、修复后仍缺失：Run failed
          （稳定原因码）。禁止 deterministic 模板补写 Scene——fail closed
          优于编造内容。
        """
        scenes = state.scenes if isinstance(state.scenes, list) else []
        available = self._available_material_types(state)
        covered_types = self._covered_material_types(scenes)
        missing = [t for t in available if t not in covered_types]
        if not missing:
            log_success(
                "MemoirAgent 覆盖完整修复节点直通 run_id=%s scene_count=%s type_count=%s",
                run.run_id, len(scenes), len(available),
            )
            return {
                "node_id": _REPAIR_NODE_ID, "repaired": False, "added_scene_count": 0,
            }
        repair_materials = self._missing_type_materials(
            state.sanitized_material, missing,
        )
        if repair_materials is None:
            # 设计 §3.3：实际存在类型缺少安全 text_digest 投影 → 契约错误
            # fail closed，不调模型、不编造（缺失类型只可能是五类枚举值）。
            logging.warning(
                "MemoirAgent 覆盖修复缺失类型无安全摘要 run_id=%s missing=%s code=%s",
                run.run_id, missing, "COVERAGE_TEXT_DIGEST_MISSING",
            )
            raise ValueError("COVERAGE_TEXT_DIGEST_MISSING")
        request: dict[str, object] = {
            "missing_material_types": missing,
            "source_refs": [material["source_ref"] for material in repair_materials],
        }
        data = self._model_data(
            run.run_id, _REPAIR_NODE_ID, request, run.agent_version,
            materials=repair_materials,
        )
        if data is None:
            # 网关不可用或无剩余模型许可/预算：Run failed（fail closed 优于
            # 模板编造），与普通生成节点的模板降级语义刻意不同。
            logging.warning(
                "MemoirAgent 覆盖修复模型能力不可用 run_id=%s missing=%s code=%s",
                run.run_id, missing, "COVERAGE_REPAIR_MODEL_UNAVAILABLE",
            )
            raise ValueError("COVERAGE_REPAIR_MODEL_UNAVAILABLE")
        repair_scenes = self._parse_coverage_repair_output(
            data,
            allowed_refs=set(request["source_refs"]),
            existing_scene_ids={
                str(scene.get("scene_id"))
                for scene in scenes
                if isinstance(scene, dict) and isinstance(scene.get("scene_id"), str)
            },
        )
        if repair_scenes is None:
            logging.warning(
                "MemoirAgent 覆盖修复输出被拒绝 run_id=%s missing=%s code=%s",
                run.run_id, missing, "COVERAGE_REPAIR_OUTPUT_INVALID",
            )
            raise ValueError("COVERAGE_REPAIR_OUTPUT_INVALID")
        # 修复后覆盖复核：仍缺失即 Run failed（模型判定素材不足以成卡也是
        # 正确结果，宁可不发布也不编造）。
        still_missing = [
            t for t in available
            if t not in covered_types | self._covered_material_types(repair_scenes)
        ]
        if still_missing:
            logging.warning(
                "MemoirAgent 覆盖修复后仍缺失 run_id=%s missing=%s code=%s",
                run.run_id, still_missing, "COVERAGE_REPAIR_INCOMPLETE",
            )
            raise ValueError("COVERAGE_REPAIR_INCOMPLETE")
        # 合并：修复场景插在末尾 summary 之前——prompt 冻结"修复只补中间
        # 场景"，全文档首 cover/末 summary 已由生成批次固定，保持播放结构。
        merged = list(scenes)
        if (
            merged
            and isinstance(merged[-1], dict)
            and merged[-1].get("scene_type") == "summary"
        ):
            merged = merged[:-1] + repair_scenes + [merged[-1]]
        else:
            merged = merged + repair_scenes
        state.apply_tool_output("scenes", merged)
        log_success(
            "MemoirAgent 覆盖修复完成 run_id=%s missing_before=%s added=%s scene_count=%s",
            run.run_id, len(missing), len(repair_scenes), len(merged),
        )
        return {
            "node_id": _REPAIR_NODE_ID, "repaired": True,
            "added_scene_count": len(repair_scenes),
        }

    @staticmethod
    def _covered_material_types(scenes: list[object]) -> set[str]:
        """从场景引用集合推导已覆盖类型（source_ref 前缀即素材类型）。"""
        return {
            str(ref).split(":", 1)[0]
            for scene in scenes
            if isinstance(scene, dict)
            for ref in scene.get("source_refs", [])
            if isinstance(ref, str)
        }

    @staticmethod
    def _available_material_types(state: AgentState) -> list[str]:
        """读取 compute_stats 产出的实际存在类型（固定类型序）。

        图顺序保证 compute_stats 先于修复节点执行；stats 缺键时从脱敏视图
        等价推导（sanitize 后真实出现的类型），只服务独立节点单测与防御。
        """
        stats = state.stats if isinstance(state.stats, dict) else {}
        raw = stats.get("available_material_types")
        if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
            return [item for item in raw if item in _MATERIAL_TYPE_ORDER]
        if not isinstance(state.sanitized_material, Mapping):
            return []
        materials = state.sanitized_material.get("materials")
        if not isinstance(materials, list):
            return []
        present = {
            item.get("type")
            for item in materials
            if isinstance(item, Mapping) and item.get("type") in _MATERIAL_TYPE_ORDER
        }
        return [t for t in _MATERIAL_TYPE_ORDER if t in present]

    @staticmethod
    def _missing_type_materials(
        sanitized_material: object, missing_types: list[str],
    ) -> list[dict[str, str]] | None:
        """收集缺失类型的安全素材（sensitive=False 且带 text）；返回 None =
        任一缺失类型无安全 text_digest 投影（契约错误 fail closed）。

        轮转交错装填（zip_longest 按位拉链）：多缺失类型时保证每类至少
        一条进入模型上下文（与循环按类型交错成批同一精神），总量对齐
        网关素材通道的 8 条上限。
        """
        if not isinstance(sanitized_material, Mapping):
            return None
        materials = sanitized_material.get("materials")
        if not isinstance(materials, list):
            return None
        by_type: dict[str, list[dict[str, str]]] = {
            material_type: [] for material_type in missing_types
        }
        for item in materials:
            if not isinstance(item, Mapping):
                continue
            ref, text = item.get("source_ref"), item.get("text")
            if (
                item.get("type") in by_type
                and item.get("sensitive") is False
                and isinstance(ref, str) and ref
                and isinstance(text, str) and text.strip()
            ):
                by_type[str(item["type"])].append({"source_ref": ref, "text": text})
        if any(not queue for queue in by_type.values()):
            return None
        return [
            entry
            for row in zip_longest(*(by_type[t] for t in missing_types))
            for entry in row
            if entry is not None
        ][:8]

    def _parse_coverage_repair_output(
        self,
        data: object,
        allowed_refs: set[str],
        existing_scene_ids: set[str],
    ) -> list[dict[str, object]] | None:
        """按 coverage-repair.v1.md JSON 契约解析修复输出；失败返回 None。

        与 _parse_batch_output 同一纪律：逐场景过受信任语义校验器（引用
        白名单 + 控制字段黑名单）+ r1- 前缀/封闭类型枚举/正文非空等逐字段
        校验；任何一步失败只记受控原因码，模型原始输出不进日志。
        """
        if isinstance(data, str):
            raw = data
        elif isinstance(data, Mapping):
            try:
                raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                return None
        else:
            logging.info(
                "MemoirAgent 覆盖修复输出被拒绝 reason=%s", "MODEL_OUTPUT_TYPE_INVALID",
            )
            return None
        value, _status = parse_json_once(raw)
        if value is None:
            logging.info(
                "MemoirAgent 覆盖修复输出被拒绝 reason=%s", "JSON_PARSE_FAILED",
            )
            return None
        try:
            output = _CoverageRepairOutput.model_validate(value)
        except ValidationError:
            logging.info(
                "MemoirAgent 覆盖修复输出被拒绝 reason=%s", "SCHEMA_VALIDATION_FAILED",
            )
            return None
        validator = SemanticValidator()
        scenes: list[dict[str, object]] = []
        for scene in output.scenes:
            # r1- 前缀：与生成批次 s{batch_index}- 命名空间隔离；且不得与已
            # 生成场景或本批内其它修复场景冲突（覆盖式合并被禁止）。
            if not scene.scene_id.startswith("r1-"):
                logging.info(
                    "MemoirAgent 覆盖修复输出被拒绝 reason=%s", "REPAIR_SCENE_ID_PREFIX_INVALID",
                )
                return None
            if scene.scene_id in existing_scene_ids or any(
                scene.scene_id == earlier["scene_id"] for earlier in scenes
            ):
                logging.info(
                    "MemoirAgent 覆盖修复输出被拒绝 reason=%s", "REPAIR_SCENE_ID_CONFLICT",
                )
                return None
            semantic = validator.validate(
                scene.model_dump(), trusted_refs=allowed_refs,
            )
            if not semantic.valid:
                recorder = getattr(self._model_gateway, "record_validation_rejection", None)
                if callable(recorder):
                    recorder(_REPAIR_NODE_ID, semantic.error_codes)
                logging.info(
                    "MemoirAgent 覆盖修复输出被拒绝 reason=%s error_codes=%s",
                    "SEMANTIC_VALIDATION_FAILED", semantic.error_codes,
                )
                return None
            if not scene.source_refs or not scene.body.strip():
                logging.info(
                    "MemoirAgent 覆盖修复输出被拒绝 reason=%s", "REPAIR_SCENE_CONTENT_EMPTY",
                )
                return None
            title_word = scene.title_word
            if title_word is not None and (not title_word or len(title_word) > 6):
                logging.info(
                    "MemoirAgent 覆盖修复输出被拒绝 reason=%s", "REPAIR_TITLE_WORD_INVALID",
                )
                return None
            entry: dict[str, object] = {
                "scene_id": scene.scene_id,
                "scene_type": scene.scene_type,
                "source_refs": list(dict.fromkeys(scene.source_refs)),
                "body": scene.body,
            }
            if title_word is not None:
                entry["title_word"] = title_word
            scenes.append(entry)
        return scenes

    def _parse_batch_output(
        self,
        data: object,
        batch_refs: list[str],
        *,
        is_first_batch: bool,
        is_final_batch: bool,
    ) -> list[dict[str, object]] | None:
        """按 1.0.5 scene-batch-generate JSON 契约解析本批场景；失败返回 None。

        与既有 _parse_structured_output 的差异：容器的 len(scenes)>=3 规则不
        适用（单批允许 1~2 个场景，prompt 冻结契约），因此改为逐场景过同一
        受信任语义校验器（场景级 source_refs ⊆ 本批引用 + 控制字段黑名单）。
        任何一步失败只记受控原因码，模型原始输出不进日志。
        """
        if isinstance(data, str):
            raw = data
        elif isinstance(data, Mapping):
            try:
                raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                return None
        else:
            logging.info(
                "MemoirAgent 循环批次输出被拒绝 reason=%s", "MODEL_OUTPUT_TYPE_INVALID",
            )
            return None
        value, _status = parse_json_once(raw)
        if value is None:
            logging.info(
                "MemoirAgent 循环批次输出被拒绝 reason=%s", "JSON_PARSE_FAILED",
            )
            return None
        try:
            output = _SceneBatchPlanOutput.model_validate(value)
        except ValidationError:
            logging.info(
                "MemoirAgent 循环批次输出被拒绝 reason=%s", "SCHEMA_VALIDATION_FAILED",
            )
            return None
        validator = SemanticValidator()
        scenes: list[dict[str, object]] = []
        for position, scene in enumerate(output.scenes):
            semantic = validator.validate(
                scene.model_dump(), trusted_refs=set(batch_refs),
            )
            if not semantic.valid:
                recorder = getattr(self._model_gateway, "record_validation_rejection", None)
                if callable(recorder):
                    recorder(_LOOP_BODY_NODE_ID, semantic.error_codes)
                logging.info(
                    "MemoirAgent 循环批次输出被拒绝 reason=%s error_codes=%s",
                    "SEMANTIC_VALIDATION_FAILED", semantic.error_codes,
                )
                return None
            if not scene.source_refs or not scene.body.strip():
                # prompt 冻结契约：source_refs 不得为空数组、body 必须可读。
                logging.info(
                    "MemoirAgent 循环批次输出被拒绝 reason=%s", "BATCH_SCENE_CONTENT_EMPTY",
                )
                return None
            # cover/summary 双重校验（prompt 之外的结构闸门）：仅首批可出 cover
            # 且必须居首；仅末批可出 summary 且必须居末。
            if scene.scene_type == "cover" and (not is_first_batch or position != 0):
                logging.info(
                    "MemoirAgent 循环批次输出被拒绝 reason=%s", "BATCH_COVER_POSITION_INVALID",
                )
                return None
            if scene.scene_type == "summary" and (
                not is_final_batch or position != len(output.scenes) - 1
            ):
                logging.info(
                    "MemoirAgent 循环批次输出被拒绝 reason=%s", "BATCH_SUMMARY_POSITION_INVALID",
                )
                return None
            entry: dict[str, object] = {
                "scene_id": scene.scene_id,
                "scene_type": scene.scene_type,
                "source_refs": list(dict.fromkeys(scene.source_refs)),
                "body": scene.body,
            }
            title_word = scene.title_word
            if title_word is not None:
                if not isinstance(title_word, str) or not title_word or len(title_word) > 6:
                    return None
                entry["title_word"] = title_word
            scenes.append(entry)
        return scenes

    @staticmethod
    def _sanitize_materials(snapshot: object) -> tuple[list[dict[str, object]], int, int]:
        """将快照转换为无正文泄漏的最小素材列表，并返回安全计数。

        R2 后：

        - ``bet_items`` / ``bets`` 与 ``completed_bet_items`` / ``completed_bets``
          不可同时出现，由 :func:`detect_envelope_mixing` 显式 fail closed。
        - legacy ``bet_items`` / ``bets`` 单向归一化为 ``completed_bet:<id>`` 前缀，
          不再向下游 allowlist/Scene/published document 回写 ``bet:`` 形状。
        - 新增 ``handbook_note`` / ``matured_wish`` / ``bucket_list_completion``
          三类只产出稳定 source_ref；正文不进入 Runtime 也不进入 sanitized 视图。
        """
        if not isinstance(snapshot, Mapping):
            return [], 0, 0
        # envelope 混用先于任何素材读取；fail closed 比产生漂移 allowlist 更安全。
        detect_envelope_mixing(snapshot)
        # 方案 A 契约：业务端 canonical materials 列表（get_snapshot 解密透传）
        # 存在时它是唯一素材来源，legacy envelope 键不再读取（避免双计数）；
        # 业务端已在物化时做白名单脱敏，本方法仍按 Runtime 最小视图二次收敛。
        raw_materials = snapshot.get("materials")
        if isinstance(raw_materials, list):
            return MemoirNodeRunner._sanitize_canonical_materials(raw_materials)
        materials: list[dict[str, object]] = []
        sensitive_count = invalid_count = 0
        # diary 与 completed_bet 走完整脱敏：保留稳定引用 + 80 字摘要。
        sanitize_slots: tuple[tuple[tuple[str, ...], str], ...] = (
            (("diary_items", "diaries"), "diary"),
            (("completed_bet_items", "completed_bets", "bet_items", "bets"), "completed_bet"),
        )
        for fields, material_type in sanitize_slots:
            raw_items = next((snapshot[field] for field in fields if field in snapshot), None)
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                if not isinstance(item, Mapping):
                    invalid_count += 1
                    continue
                material_id = item.get("id")
                if not isinstance(material_id, str) or not material_id:
                    invalid_count += 1
                    continue
                source_ref = f"{material_type}:{material_id}"
                # 显式敏感、无正文或非字符串正文都只能保留引用，不复制任何内容。
                if item.get("sensitive") is True or not isinstance(item.get("content"), str):
                    materials.append({"source_ref": source_ref, "type": material_type, "sensitive": True})
                    sensitive_count += 1
                    continue
                summary = MemoirNodeRunner._sanitize_material_summary(item["content"])
                materials.append(
                    {
                        "source_ref": source_ref,
                        "type": material_type,
                        "sensitive": False,
                        "summary": summary,
                    }
                )
        # contract 五类中剩余三类当前不脱敏正文，只产出稳定 source_ref；下游
        # allowlist / Scene 可正常引用，模型上下文不会拿到这些素材的正文。
        ref_only_slots: tuple[tuple[tuple[str, ...], str], ...] = (
            (("handbook_notes",), "handbook_note"),
            (("matured_wishes",), "matured_wish"),
            (("bucket_list_completions",), "bucket_list_completion"),
        )
        for fields, material_type in ref_only_slots:
            raw_items = next((snapshot[field] for field in fields if field in snapshot), None)
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                if not isinstance(item, Mapping):
                    invalid_count += 1
                    continue
                material_id = item.get("id")
                if not isinstance(material_id, str) or not material_id:
                    invalid_count += 1
                    continue
                materials.append(
                    {
                        "source_ref": f"{material_type}:{material_id}",
                        "type": material_type,
                        # 保守标 sensitive：模型不会拿到正文，发布端只看 source_ref。
                        "sensitive": True,
                    }
                )
                sensitive_count += 1
        return materials, sensitive_count, invalid_count

    @staticmethod
    def _sanitize_canonical_materials(
        raw_materials: list[object],
    ) -> tuple[list[dict[str, object]], int, int]:
        """消费业务端 canonical 脱敏素材列表（get_snapshot 方案 A 契约）。

        业务端物化时已做白名单脱敏（sanitized_payload 只含可定位元数据，
        不含日记/赌局正文）；本方法按 Runtime 最小视图二次收敛：

        - ``diary`` / ``completed_bet``：payload 为 Mapping 时输出 80 字元数据
          摘要（sensitive=False，source_ref 可进入模型 allowlist）；
        - 其余三类与异常项：只保留稳定 source_ref（sensitive=True），
          与 legacy 路径"三类 ref-only"语义一致。
        """

        materials: list[dict[str, object]] = []
        sensitive_count = invalid_count = 0
        # 只有这两类产出摘要：与 legacy sanitize_slots 的完整脱敏组对齐。
        summary_types = ("diary", "completed_bet")
        for item in raw_materials:
            if not isinstance(item, Mapping):
                invalid_count += 1
                continue
            source_ref = item.get("source_ref")
            material_type = item.get("material_type")
            if (
                not isinstance(source_ref, str)
                or not source_ref
                or not isinstance(material_type, str)
                or not material_type
            ):
                invalid_count += 1
                continue
            payload = item.get("sanitized_payload")
            # Phase A：业务端 text_digest（第一层脱敏后的截断摘要）存在时优先直出。
            # 全五类均可携带 digest；Runtime 只复用统一二次脱敏管道并放宽长度到
            # 200（digest 仅供模型上下文引用，无下游发布消费方，放宽无副作用）。
            # 存入 text 字段与 legacy 元数据 summary 区分：后者是 ID/日期等
            # 结构化 JSON 摘要，对模型引用真实细节没有增量价值。
            digest = payload.get("text_digest") if isinstance(payload, Mapping) else None
            if isinstance(digest, str) and digest.strip():
                entry = {
                    "source_ref": source_ref,
                    "type": material_type,
                    "sensitive": False,
                    "text": MemoirNodeRunner._sanitize_material_summary(
                        digest, limit=200
                    ),
                }
                MemoirNodeRunner._attach_material_images(entry, payload)
                materials.append(entry)
                continue
            if material_type in summary_types and isinstance(payload, Mapping):
                # 元数据摘要：白名单元数据紧凑 JSON 后复用统一截断/脱敏，
                # 与 legacy content 摘要走同一条 _sanitize_material_summary 管线。
                summary = MemoirNodeRunner._sanitize_material_summary(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                )
                entry = {
                    "source_ref": source_ref,
                    "type": material_type,
                    "sensitive": False,
                    "summary": summary,
                }
                MemoirNodeRunner._attach_material_images(entry, payload)
                materials.append(entry)
                continue
            materials.append(
                {"source_ref": source_ref, "type": material_type, "sensitive": True}
            )
            sensitive_count += 1
        return materials, sensitive_count, invalid_count

    @staticmethod
    def _attach_material_images(entry: dict[str, object], payload: object) -> None:
        """把业务端投影的照片元数据挂进脱敏素材视图（M6 图生图消费）。

        只接受 sanitize 之后的 images 键：条目精确三键 {photo_id, object_key,
        mime}，object_key/mime 必须为白名单字符串；单素材最多 4 张（模型节点
        不消费该键，仅媒体服务在照片出域门禁开启时读取）。
        """
        if not isinstance(payload, Mapping):
            return
        raw_images = payload.get("images")
        if not isinstance(raw_images, list):
            return
        images: list[dict[str, str]] = []
        for item in raw_images[:4]:
            if not isinstance(item, Mapping) or set(item) != {"photo_id", "object_key", "mime"}:
                continue
            photo_id, object_key, mime = item.get("photo_id"), item.get("object_key"), item.get("mime")
            if (
                not isinstance(photo_id, str) or not photo_id
                or not isinstance(object_key, str) or not object_key
                or not isinstance(mime, str) or mime not in MEDIA_IMAGE_MIME_TYPES
            ):
                continue
            images.append({"photo_id": photo_id, "object_key": object_key, "mime": mime})
        if images:
            entry["images"] = images

    @staticmethod
    def _sanitize_material_summary(content: str, limit: int = 80) -> str:
        """替换敏感标识与可识别昵称，并将素材摘要限制在给定字数内。

        默认 80 字（legacy 元数据摘要）；text_digest 路径放宽到 200：
        digest 本身在业务端已截断，二次放宽只为避免标题+正文被再次切半。
        """
        redacted = _MATERIAL_SENSITIVE_TEXT.sub("[REDACTED]", content)
        return _MATERIAL_SELF_NICKNAME.sub("我", redacted).strip()[:limit]

    @staticmethod
    def _safe_material_refs(
        sanitized_material: object, agent_version: object = "",
    ) -> list[str]:
        """返回非敏感稳定引用；仅 1.0.5 放开旧版八条上限。"""
        if not isinstance(sanitized_material, Mapping):
            return []
        materials = sanitized_material.get("materials")
        if not isinstance(materials, list):
            return []
        refs = list(dict.fromkeys(
            item["source_ref"]
            for item in materials
            if isinstance(item, Mapping)
            and item.get("sensitive") is False
            and isinstance(item.get("source_ref"), str)
        ))
        return refs if _version_at_least(agent_version, "1.0.5") else refs[:8]

    @staticmethod
    def _safe_material_texts(
        sanitized_material: object,
        allowed_refs: list[str],
        agent_version: object = "",
    ) -> list[dict[str, str]]:
        """提取授权摘要；仅 1.0.5 放开旧版八条上限。

        只取 sensitive=False 且 text 非空且 source_ref 在 allowed_refs 内的条目：
        与引用白名单双保险，模型只能看到当前节点已授权引用的素材文本。
        text 由 sanitize 阶段统一脱敏并截断，此处不再复制或改写。
        """
        if not isinstance(sanitized_material, Mapping):
            return []
        materials = sanitized_material.get("materials")
        if not isinstance(materials, list):
            return []
        allowlist = set(allowed_refs)
        texts: list[dict[str, str]] = []
        for item in materials:
            if not isinstance(item, Mapping):
                continue
            ref, text = item.get("source_ref"), item.get("text")
            if (
                item.get("sensitive") is False
                and isinstance(ref, str)
                and ref in allowlist
                and isinstance(text, str)
                and text.strip()
            ):
                texts.append({"source_ref": ref, "text": text})
        return texts if _version_at_least(agent_version, "1.0.5") else texts[:8]

    @staticmethod
    def _playback_source_refs(scenes: object) -> set[str]:
        """仅供无脱敏状态的独立节点测试使用；生产发布总是采用冻结素材引用。"""
        if not isinstance(scenes, list):
            return set()
        return {
            ref for scene in scenes if isinstance(scene, Mapping)
            for ref in scene.get("source_refs", [])
            if isinstance(ref, str)
        }

    @staticmethod
    def _template_scene(index: int, source_refs: list[str]) -> dict[str, object]:
        """构造模板兜底场景：引用外的全部字段固定，正文按序取安全文案，保证卡片不空白。"""
        return {
            "scene_id": f"scene-{index}",
            "scene_type": "summary",
            "source_refs": source_refs,
            "body": _TEMPLATE_SCENE_BODIES[(index - 1) % len(_TEMPLATE_SCENE_BODIES)],
        }

    @staticmethod
    def _base_scenes_actions() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """生成三张无素材引用的基础卡，作为唯一发布前安全回退文档。"""
        scenes = [MemoirNodeRunner._template_scene(index, []) for index in range(1, 4)]
        actions = [
            {"action_id": f"action-{index}", "scene_id": f"scene-{index}", "action_type": "show_card", "duration_ms": 3000}
            for index in range(1, 4)
        ]
        return scenes, actions

    @staticmethod
    def _rule_actions(scenes: object) -> list[dict[str, object]]:
        """按 _SCENE_ACTION_RULES 从场景表确定性推导动作列表。

        generate_actions 与媒体节点（图片场景降级后需要重建动作表）共用：
        动作是调度结构而非内容，映射冻结、幂等、不接模型。
        """
        rule_actions: list[dict[str, object]] = []
        for index, scene in enumerate(scenes if isinstance(scenes, list) else [], start=1):
            if not (isinstance(scene, dict) and isinstance(scene.get("scene_id"), str)):
                continue
            action_type = _SCENE_ACTION_RULES.get(
                str(scene.get("scene_type")), "show_card",
            )
            if action_type == "type_text":
                # 打字机停留时长随正文长度自适应：前端 xxt-text-type 冻结
                # 75ms/字，先让全文打完（len*75）再留 1500ms 阅读停留；
                # 不设上限——body 已不限字数，cap 会让长文案打字被截断。
                body = scene.get("body")
                body_len = len(body) if isinstance(body, str) else 0
                duration_ms = max(3000, body_len * 75 + 1500)
            else:
                # show_card：带配图（payload.image_url）的场景停留 5000ms 留出
                # 看图时间；纯文字卡维持 3000ms。
                payload = scene.get("payload")
                has_image = (
                    isinstance(payload, dict)
                    and isinstance(payload.get("image_url"), str)
                    and bool(payload.get("image_url"))
                )
                duration_ms = 5000 if has_image else 3000
            rule_actions.append({
                "action_id": f"action-{index}",
                "scene_id": scene["scene_id"],
                "action_type": action_type,
                "duration_ms": duration_ms,
            })
        return rule_actions

    @staticmethod
    def _scene_content_contract_valid(scenes: list[object], agent_version: object) -> bool:
        """按 Agent 版本校验冻结的场景数量与正文长度合同。"""
        if _per_scene_media_enabled(agent_version):
            return True
        return len(scenes) <= 8 and all(
            not isinstance(scene, Mapping)
            or not isinstance(scene.get("body"), str)
            or len(scene["body"]) <= 80
            for scene in scenes
        )

    @staticmethod
    def _is_safe_playback(
        scenes: object,
        actions: object,
        *,
        media_tasks: object = None,
        scene_types: tuple[str, ...] = _SCENE_TYPES,
        per_scene_media: bool = False,
        media_pending: bool = False,
        agent_version: object = "",
    ) -> bool:
        """校验播放结构及动作引用；只允许当前模板链定义的安全字段组合。

        M6 起同时校验 media_manifest 与 image 场景的 D1 冻结契约：
        六键条目、前缀+UUID object_key、https+域白名单 URL、mime 白名单、
        场景 1:1 引用、payload 白名单 {image_url, title_word}、
        image_url 与 manifest url 逐场景一致、非 image 场景不得携带 payload。
        1.0.4 起（per_scene_media=True）契约放宽：任意场景可携带配图
        payload（白名单不变、配对规则不变）。
        1.0.4+ 场景数量只保下限（>=3）、body 不限长度：素材量随用户数据增长
        （最少日记 7 + 赌约 7），设上限会把真实数据误杀成全盘兜底；1.0.3-
        仍执行冻结的最多 8 场景、每场最多 80 字合同，防止恢复状态绕过生成入口。
        """
        if (
            not isinstance(scenes, list)
            or len(scenes) < 3
            or not MemoirNodeRunner._scene_content_contract_valid(scenes, agent_version)
        ):
            return False
        if not all(
            isinstance(scene, dict)
            and isinstance(scene.get("scene_id"), str)
            and scene.get("scene_type") in scene_types
            and isinstance(scene.get("source_refs"), list)
            and all(isinstance(ref, str) for ref in scene["source_refs"])
            and ("body" not in scene or isinstance(scene["body"], str))
            and not MemoirGuardrails.violations(scene.get("body"))
            for scene in scenes
        ):
            return False
        scene_ids = {scene["scene_id"] for scene in scenes}
        if len(scene_ids) != len(scenes):
            return False
        # ---- M6 媒体契约校验（media_tasks 为 None 视为空清单，兼容旧调用方）----
        manifest = media_tasks if isinstance(media_tasks, list) else []
        if media_pending and manifest:
            return False
        if not MemoirNodeRunner._media_manifest_valid(manifest, scene_ids):
            return False
        url_by_scene = {
            entry["scene_id"]: entry["url"]
            for entry in manifest
            if isinstance(entry, dict) and isinstance(entry.get("scene_id"), str) and isinstance(entry.get("url"), str)
        }
        for scene in scenes:
            payload = scene.get("payload")
            if scene.get("scene_type") == "image":
                if media_pending:
                    if payload not in (None, {}):
                        return False
                    continue
                # image 场景必须有 manifest 条目，payload 白名单仅两键且
                # image_url 必须与该场景 manifest 条目 url 完全一致。
                if scene.get("scene_id") not in url_by_scene:
                    return False
                if not isinstance(payload, dict):
                    return False
                if set(payload) - {"image_url", "title_word"}:
                    return False
                if payload.get("image_url") != url_by_scene.get(scene.get("scene_id")):
                    return False
                title_word = payload.get("title_word")
                if title_word is not None and (
                    not isinstance(title_word, str) or not title_word or len(title_word) > 6
                ):
                    return False
            elif per_scene_media:
                if media_pending:
                    if payload not in (None, {}):
                        return False
                    continue
                # 1.0.4+ 每场景配图：非 image 场景也可携带配图 payload——白名单
                # 仍仅 {image_url, title_word}，image_url 必须命中本场景 manifest
                # 条目 url（配对规则不变）；无 payload/空 payload 的纯文字卡放行
                # （单张生成失败的场景原样保留为文字卡）。
                if isinstance(payload, dict) and payload:
                    if set(payload) - {"image_url", "title_word"}:
                        return False
                    if payload.get("image_url") != url_by_scene.get(scene.get("scene_id")):
                        return False
                    title_word = payload.get("title_word")
                    if title_word is not None and (
                        not isinstance(title_word, str) or not title_word or len(title_word) > 6
                    ):
                        return False
                elif payload not in (None, {}):
                    return False
            elif payload not in (None, {}):
                # 非 image 场景 payload 必须保持无/空（D1 冻结，1.0.3-）。
                return False
        return (
            isinstance(actions, list)
            and len(actions) == len(scenes)
            and all(
                isinstance(action, dict)
                and isinstance(action.get("action_id"), str)
                and action.get("scene_id") in scene_ids
                and action.get("action_type") in _ACTION_TYPES
                and isinstance(action.get("duration_ms"), int)
                and action["duration_ms"] >= 1
                for action in actions
            )
            and {action["scene_id"] for action in actions if isinstance(action, dict)} == scene_ids
        )

    @staticmethod
    def _media_manifest_valid(manifest: list[object], scene_ids: set[str]) -> bool:
        """校验 media_manifest 条目与 image 场景 payload 的冻结契约。"""
        manifest_scenes: set[str] = set()
        for entry in manifest:
            if not isinstance(entry, dict) or set(entry) != MEDIA_MANIFEST_KEYS:
                return False
            if entry.get("kind") != MEDIA_KIND_IMAGE:
                return False
            object_key, url = entry.get("object_key"), entry.get("url")
            mime, scene_id = entry.get("mime"), entry.get("scene_id")
            if not isinstance(object_key, str) or not object_key.startswith(MEDIA_IMAGE_PREFIX):
                return False
            # object_key 前缀后必须是不可猜测的 UUID（拒绝可预测命名）。
            stem = object_key[len(MEDIA_IMAGE_PREFIX):]
            try:
                uuid_module.UUID(stem.rsplit(".", 1)[0] if "." in stem else stem)
            except (ValueError, AttributeError):
                return False
            if not isinstance(url, str) or not url.startswith("https://"):
                return False
            # URL path 必须落在冻结前缀下；域名后缀白名单由媒体服务上传侧
            # _require_contract_url 按部署配置校验（runner 保持配置无关）。
            if not urlparse(url).path.startswith(f"/{MEDIA_IMAGE_PREFIX}"):
                return False
            if not isinstance(mime, str) or mime not in MEDIA_IMAGE_MIME_TYPES:
                return False
            if scene_id not in scene_ids or scene_id in manifest_scenes:
                # scene_id 必须指向当前文档且 1:1 不重复。
                return False
            manifest_scenes.add(scene_id)
        return True

    def _model_data(
        self, run_id: str, node_id: str, request: dict[str, object], agent_version: str,
        materials: list[dict[str, str]] | None = None,
    ) -> object | None:
        """只接受成功 Gateway 的 data；状态和异常一律由确定性模板安全降级。"""
        if self._model_gateway is None:
            return None
        try:
            safe_request = self._safe_model_request(
                node_id, request, agent_version, materials=materials,
            )
            if safe_request is None:
                return None
            result = self._model_gateway.call(run_id, node_id, safe_request)
        except Exception:  # Gateway 已处理 usage；Runner 不记录请求或异常正文。
            logging.warning("MemoirAgent 模型能力不可用 node_id=%s", node_id)
            return None
        if getattr(result, "status", None) != "succeeded":
            logging.info("MemoirAgent 模型能力不可用 node_id=%s", node_id)
            return None
        return getattr(result, "data", None)

    def _repair_model_data(
        self,
        run_id: str,
        node_id: str,
        request: dict[str, object],
        invalid_output: object,
        agent_version: str,
        materials: list[dict[str, str]] | None = None,
    ) -> object | None:
        """最多调用一次受信任 repair 边界；原输出只沿当前调用栈短暂传递。"""
        if self._model_gateway is None:
            return None
        repair = getattr(self._model_gateway, "repair", None)
        if not callable(repair):
            return None
        try:
            safe_request = self._safe_model_request(
                node_id, request, agent_version, materials=materials,
            )
            if safe_request is None:
                return None
            result = repair(run_id, node_id, safe_request, invalid_output)
        except Exception:
            logging.warning("MemoirAgent 结构化修复能力不可用 node_id=%s", node_id)
            return None
        if getattr(result, "status", None) != "succeeded":
            logging.info("MemoirAgent 结构化修复未成功 node_id=%s", node_id)
            return None
        return getattr(result, "data", None)

    def _safe_model_request(
        self, node_id: str, request: dict[str, object], agent_version: str,
        materials: list[dict[str, str]] | None = None,
    ) -> dict[str, object] | None:
        """构造进入网关的执行请求（含素材脱敏文本通道）。

        隐私边界：materials 携带 text_digest 派生的脱敏文本，只在内存中经
        ContextManager 进入 Provider 请求；可观测 context 摘要仍保持占位符
        口径（source_ref_count / redaction 计数），素材正文绝不进入 audit 视图。
        """
        prompt_id = {
            "extract_highlights": "highlight-extract",
            "plan_chapters": "chapter-plan",
            "generate_scenes": "scene-generate",
            # M7 循环体：1.0.5 bounded_loop 的单批场景生成 prompt。
            "generate_scene_batch": "scene-batch-generate",
            # M7 覆盖修复：1.0.5 循环后唯一一次 repair 模型调用的 prompt。
            "repair_coverage_gaps": "coverage-repair",
        }.get(node_id)
        if prompt_id is None:
            return None
        # audit 摘要的 prompt 身份按当前 Run 绑定的 agent_version 加载，与执行路径
        # (memoir_model_gateway) 保持一致，不再硬编码版本；1.0.0/1.0.1 的 prompts 内容
        # 相同，此处 DTO 字段值不变，仅消除“共享 runner 却钉死版本”的特殊情况。
        prompt = self._prompts.load("memoir_agent", agent_version, prompt_id, "v1")
        refs = self._request_source_refs(request)
        context = self._contexts.build_node_context(
            trusted_instructions=prompt.template,
            materials=[{"source_ref": ref, "text": "[SOURCE_REF]"} for ref in refs],
            tool_results=[], token_budget=256,
        )
        # 仅传 Prompt 身份、策略和无正文上下文摘要；模板正文绝不进入可观测 DTO。
        safe_request: dict[str, object] = {
            "prompt_id": prompt.prompt_id,
            "prompt_version": prompt.version,
            "model_policy": prompt.model_policy,
            "context": context.safe_summary(),
            "input": request,
        }
        # 素材文本通道：非空才挂键（旧快照无 text_digest 时形状与历史完全一致，
        # 现有网关与测试夹具零破坏）。
        if materials:
            safe_request["materials"] = materials
        return safe_request

    @staticmethod
    def _request_source_refs(request: Mapping[str, object]) -> list[str]:
        """从节点安全输入抽取来源引用，禁止把日记文本转入模型上下文。"""
        refs = request.get("source_refs")
        if isinstance(refs, list):
            return [item for item in refs if isinstance(item, str)]
        chapters = request.get("chapters")
        if not isinstance(chapters, list):
            return []
        return [ref for chapter in chapters if isinstance(chapter, Mapping)
                for ref in chapter.get("source_refs", []) if isinstance(ref, str)]

    def _valid_highlights(self, data: object | None, allowed_refs: list[str]) -> list[str] | None:
        output = self._parse_structured_output(data, _HighlightOutput, allowed_refs, "extract_highlights")
        if not isinstance(output, _HighlightOutput):
            return None
        return list(dict.fromkeys(output.source_refs))[:8]

    @staticmethod
    def _safe_chapters(chapters: object) -> list[dict[str, object]]:
        if not isinstance(chapters, list):
            return []
        safe: list[dict[str, object]] = []
        for chapter in chapters[:3]:
            if not isinstance(chapter, Mapping) or not isinstance(chapter.get("chapter_id"), str):
                continue
            refs = chapter.get("source_refs")
            if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
                continue
            safe.append({"chapter_id": chapter["chapter_id"], "source_refs": refs[:8], "kind": "memory_overview"})
        return safe

    def _valid_chapters(self, data: object | None, allowed_refs: list[str]) -> list[dict[str, object]] | None:
        output = self._parse_structured_output(data, _ChapterPlanOutput, allowed_refs, "plan_chapters")
        if not isinstance(output, _ChapterPlanOutput):
            return None
        raw_chapters = output.model_dump()["chapters"]
        if not 1 <= len(raw_chapters) <= 3:
            return None
        chapters = MemoirNodeRunner._safe_chapters(raw_chapters)
        if len(chapters) != len(raw_chapters):
            return None
        if len({chapter["chapter_id"] for chapter in chapters}) != len(chapters):
            return None
        if not all(ref in allowed_refs for ref in MemoirNodeRunner._source_refs(chapters)):
            return None
        return chapters

    @staticmethod
    def _source_refs(chapters: object) -> list[str]:
        return [ref for chapter in MemoirNodeRunner._safe_chapters(chapters) for ref in chapter["source_refs"] if isinstance(ref, str)]

    def _valid_scenes(
        self, data: object | None, allowed_refs: list[str], agent_version: str = "",
    ) -> list[dict[str, object]] | None:
        output = self._parse_structured_output(data, _ScenePlanOutput, allowed_refs, "generate_scenes")
        if not isinstance(output, _ScenePlanOutput):
            return None
        scenes = output.model_dump()["scenes"]
        if len(scenes) < 3:
            return None
        # 1.0.4+ 的产品合同不设场景数和正文字数上限；旧包的提示词已冻结为
        # 至多 8 张、每张至多 80 字，生成入口与最终发布审核共用同一版本合同。
        if not self._scene_content_contract_valid(scenes, agent_version):
            return None
        # 版本门控：旧版仅识别六类非媒体场景；1.0.3+ 额外放行 image（M6 媒体通道）。
        allowed_types = _MEDIA_SCENE_TYPES if _media_version_enabled(agent_version) else _SCENE_TYPES
        if not all(isinstance(scene, Mapping) and isinstance(scene.get("scene_id"), str) and scene.get("scene_type") in allowed_types and isinstance(scene.get("source_refs"), list) and all(isinstance(ref, str) and ref in allowed_refs for ref in scene["source_refs"]) for scene in scenes):
            return None
        if len({scene["scene_id"] for scene in scenes}) != len(scenes):
            return None
        validated: list[dict[str, object]] = []
        for scene in scenes:
            entry: dict[str, object] = {
                "scene_id": scene["scene_id"],
                # 场景类型透传（冻结类型集合），前端按类型渲染差异化卡片。
                "scene_type": scene["scene_type"],
                "source_refs": list(dict.fromkeys(scene["source_refs"])),
                **({"body": scene["body"]} if isinstance(scene.get("body"), str) else {}),
            }
            title_word = scene.get("title_word")
            # title_word ≤6 字且非空；1.0.4+ 任意场景可携带（每场景配图的
            # 标题词，发布前由媒体节点收进 payload），旧版本仅 image 场景
            # 允许（M6 契约），其余场景出现即拒整批。
            if title_word is not None:
                if not isinstance(title_word, str) or not title_word or len(title_word) > 6:
                    return None
                if scene["scene_type"] != "image" and not _per_scene_media_enabled(agent_version):
                    return None
                entry["title_word"] = title_word
            validated.append(entry)
        return validated

    def _parse_structured_output(
        self,
        data: object | None,
        schema: type[BaseModel],
        trusted_refs: list[str],
        node_id: str,
    ) -> BaseModel | None:
        """模型结果不论字符串或 Mapping 均必须穿过同一个受控解析器。"""
        if isinstance(data, str):
            raw = data
        elif isinstance(data, Mapping):
            try:
                raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                logging.info("MemoirAgent 结构化模型输出被拒绝 reason=%s", "JSON_SERIALIZATION_FAILED")
                return None
        else:
            logging.info("MemoirAgent 结构化模型输出被拒绝 reason=%s", "MODEL_OUTPUT_TYPE_INVALID")
            return None
        parsed = self._structured_output.parse_and_validate(
            raw, schema, trusted_source_refs=set(trusted_refs),
        )
        if parsed.validated_value is None:
            recorder = getattr(self._model_gateway, "record_validation_rejection", None)
            if callable(recorder) and parsed.safety_status == "semantic_validation_failed":
                recorder(node_id, parsed.error_codes)
            # 仅记录受控状态码，禁止把模型输出或可能含正文的异常写进日志。
            logging.info(
                "MemoirAgent 结构化模型输出被拒绝 parse_status=%s safety_status=%s error_codes=%s",
                parsed.parse_status, parsed.safety_status, parsed.error_codes,
            )
            return None
        return parsed.validated_value
