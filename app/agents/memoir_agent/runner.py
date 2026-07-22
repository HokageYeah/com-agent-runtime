"""MemoirAgent 已实现节点的受信任 Runner。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import AgentRun
from app.runtime.context_manager import ContextManager
from app.runtime.evaluator import MemoirPlaybackEvaluator
from app.runtime.guardrails import MemoirGuardrails
from app.runtime.prompt_registry import PromptRegistry
from app.runtime.state import AgentState
from app.runtime.structured_output import StructuredOutputParser
from app.runtime.tool_gateway import ToolGateway
from app.services.evaluation_service import EvaluationService
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
    scene_type: Literal["summary"]
    source_refs: list[str]
    # 正文为可选字段，最终仍由 safety_review 统一限制长度与情绪风险表达。
    body: str | None = None


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


class MemoirNodeRunner:
    """回忆录 MVP 节点执行器，只输出不含日记正文的结构化播放文档。"""

    def __init__(
        self,
        gateway: ToolGateway,
        audit: ToolCallAuditService | None = None,
        model_gateway: MemoirModelGateway | None = None,
        evaluation_service: EvaluationService | None = None,
    ) -> None:
        self._gateway, self._audit = gateway, audit
        self._model_gateway = model_gateway
        # 审计服务由 Worker 注入同一事务 Session；单元测试可省略持久化依赖。
        self._evaluation_service = evaluation_service
        self._playback_evaluator = MemoirPlaybackEvaluator()
        # Prompt 只从内置 package 精确读取；调用方无法指定 latest 或模板路径。
        self._prompts = PromptRegistry(Path(__file__).parents[1])
        self._contexts = ContextManager()
        self._structured_output = StructuredOutputParser()

    def run_node(self, node: dict[str, object], run: AgentRun, state: AgentState) -> dict[str, object]:
        if node.get("node_id") == "safety_review":
            # 使用脱敏节点冻结的引用集合做 grounding；兼容独立节点单测时，
            # 仅把已在内存中的引用视为测试夹具，正式工作流一定会带 sanitized_material。
            trusted_refs = set(self._safe_material_refs(state.sanitized_material))
            if not isinstance(state.sanitized_material, Mapping):
                trusted_refs = self._playback_source_refs(state.scenes)
            evaluation = self._playback_evaluator.evaluate(
                state.scenes, state.actions,
                trusted_source_refs=trusted_refs,
                enabled_capabilities=set(),
            )
            if evaluation.decision == "pass" and self._is_safe_playback(state.scenes, state.actions):
                decision = "passed"
            else:
                # 不安全或不完整时回退到无素材引用的基础卡片，保证发布端不会收到畸形文档。
                state.scenes, state.actions = self._base_scenes_actions()
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
            # 媒体能力尚未启用，仍显式提交空清单以固定发布文档与摘要的契约。
            state.playback_document = {"schema_version": "1.0.0", "scenes": state.scenes, "actions": state.actions, "media_manifest": []}
            logging.info("MemoirAgent 安全审核完成 run_id=%s decision=%s scene_count=%s", run.run_id, decision, len(state.scenes))
            return {"node_id": "safety_review", "safe": decision == "passed"}
        if node.get("node_id") == "plan_chapters":
            highlights = state.highlights if isinstance(state.highlights, dict) else {}
            refs = highlights.get("source_refs", [])
            safe_refs = [ref for ref in refs if isinstance(ref, str)][:8] if isinstance(refs, list) else []
            model_data = self._model_data(run.run_id, "plan_chapters", {"source_refs": safe_refs})
            chapters = self._valid_chapters(model_data, safe_refs)
            if chapters is not None:
                state.apply_tool_output("chapter_plan", {"chapters": chapters})
                logging.info("MemoirAgent 模型章节完成 run_id=%s chapter_count=%s", run.run_id, len(chapters))
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
            model_data = self._model_data(run.run_id, "generate_scenes", {"chapters": safe_chapters})
            scenes = self._valid_scenes(model_data, self._source_refs(safe_chapters))
            if scenes is not None:
                state.apply_tool_output("scenes", scenes)
                logging.info("MemoirAgent 模型场景完成 run_id=%s scene_count=%s", run.run_id, len(scenes))
                return {"node_id": "generate_scenes", "fallback": False}
            if self._model_gateway is not None:
                state.fallback_flags.append("model_invalid_scenes")
            scenes: list[dict[str, object]] = []
            for index, chapter in enumerate(safe_chapters[:3], start=1):
                refs = chapter["source_refs"]
                scenes.append({"scene_id": f"scene-{index}", "scene_type": "summary", "source_refs": refs})
            # 素材不足时仅补无引用基础卡，避免为凑数量重复引用用户素材。
            while len(scenes) < 3:
                scene_index = len(scenes) + 1
                scenes.append({"scene_id": f"scene-{scene_index}", "scene_type": "summary", "source_refs": []})
            state.apply_tool_output("scenes", scenes)
            state.fallback_flags.append("template_scenes")
            logging.info("MemoirAgent 模板场景完成 run_id=%s scene_count=%s", run.run_id, len(state.scenes))
            return {"node_id": "generate_scenes", "fallback": True}
        if node.get("node_id") == "generate_actions":
            scenes = state.scenes if isinstance(state.scenes, list) else []
            state.actions = [{"action_id": f"action-{index}", "scene_id": scene.get("scene_id"), "action_type": "show_card", "duration_ms": 3000} for index, scene in enumerate(scenes, start=1) if isinstance(scene, dict) and isinstance(scene.get("scene_id"), str)]
            state.fallback_flags.append("template_actions")
            logging.info("MemoirAgent 规则动作完成 run_id=%s action_count=%s", run.run_id, len(state.actions))
            return {"node_id": "generate_actions", "fallback": True}
        if node.get("node_id") == "extract_highlights":
            # 原始 snapshot 不得在此节点回读，高光只可消费脱敏视图中的非敏感引用。
            refs = self._safe_material_refs(state.sanitized_material)
            model_data = self._model_data(run.run_id, "extract_highlights", {"source_refs": refs})
            highlights = self._valid_highlights(model_data, refs)
            if highlights is not None:
                state.apply_tool_output("highlights", {"source_refs": highlights, "mode": "model"})
                logging.info("MemoirAgent 模型高光完成 run_id=%s ref_count=%s", run.run_id, len(highlights))
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
            diaries = snapshot.get("diaries", [])
            bets = snapshot.get("bets", [])
            diary_count = len(diaries) if isinstance(diaries, list) else 0
            bet_count = len(bets) if isinstance(bets, list) else 0
            state.stats = {"diary_count": diary_count, "bet_count": bet_count, "has_material": bool(diary_count or bet_count)}
            logging.info("MemoirAgent 统计素材 run_id=%s diaries=%s bets=%s", run.run_id, diary_count, bet_count)
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
                logging.info(
                    "MemoirAgent 素材脱敏完成 run_id=%s material_count=%s sensitive_count=%s",
                    run.run_id,
                    len(materials),
                    sensitive_count,
                )
            return {"node_id": "sanitize_materials", "sanitized": True}
        if node.get("node_id") == "publish_document":
            if not isinstance(state.playback_document, dict):
                raise ValueError("PLAYBACK_DOCUMENT_MISSING")
            archive_id, snapshot_id, epoch = run.input_json.get("archive_id"), run.input_json.get("snapshot_id"), run.input_json.get("generation_epoch")
            if not isinstance(archive_id, str) or not isinstance(snapshot_id, str) or not isinstance(epoch, int):
                raise ValueError("MEMORY_PUBLISH_REFERENCE_INVALID")
            logical_key = f"{run.run_id}:publish_document:memory.publish_playback_document:{epoch}"
            digest = hashlib.sha256(json.dumps(state.playback_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            committed = (
                self._audit.latest_committed(
                    run.run_id, logical_key, logical_key, digest
                )
                if self._audit
                else None
            )
            if committed is not None:
                reconciled = self._gateway.get_publish_result(run.business_connector_id, archive_id, run.run_id, committed.idempotency_key)
                if reconciled is not None:
                    state.publish_result = reconciled
                    self._audit.succeed(committed, int(reconciled["revision"]), str(reconciled["content_digest"]))
                    logging.info("MemoirAgent 对账恢复发布结果 run_id=%s", run.run_id)
                    return {"node_id": "publish_document", "published": True}
                logging.warning("MemoirAgent 发布未知结果尚未可对账 run_id=%s", run.run_id)
                raise RuntimeError("PUBLISH_OUTCOME_UNKNOWN")
            audit = self._audit.begin_publish(run.run_id, run.execution_attempt, logical_key, logical_key, digest) if self._audit else None
            try:
                state.publish_result = self._gateway.publish_playback_document(run.business_connector_id, archive_id, run.run_id, snapshot_id, epoch, state.playback_document, logical_key)
            except httpx.TimeoutException:
                if audit is not None:
                    self._audit.unknown(audit, "HTTP_TIMEOUT")
                logging.warning("MemoirAgent 发布结果未知 run_id=%s", run.run_id)
                raise
            except httpx.HTTPStatusError as exc:
                if audit is not None:
                    self._audit.fail(audit, f"HTTP_{exc.response.status_code}", retryable=exc.response.status_code >= 500)
                logging.warning("MemoirAgent 发布被业务端拒绝 run_id=%s status=%s", run.run_id, exc.response.status_code)
                raise
            except Exception:
                if audit is not None:
                    self._audit.fail(audit, "TOOL_CALL_FAILED", retryable=True)
                # 异常消息可能携带 HTTP 请求体；只记录受控码，不记录异常正文。
                logging.warning(
                    "MemoirAgent 发布调用异常 run_id=%s code=%s",
                    run.run_id,
                    "TOOL_CALL_FAILED",
                )
                raise
            if audit is not None:
                self._audit.succeed(audit, int(state.publish_result["revision"]), str(state.publish_result["content_digest"]))
            logging.info("MemoirAgent 已发布作品 run_id=%s archive_id=%s", run.run_id, archive_id)
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
        state.snapshot = self._gateway.get_snapshot(
            run.business_connector_id, archive_id, snapshot_id,
            run.run_id, generation_epoch,
        )
        logging.info("MemoirAgent 已加载快照 run_id=%s archive_id=%s", run.run_id, archive_id)
        return {"node_id": "load_snapshot", "snapshot_loaded": True}

    @staticmethod
    def _sanitize_materials(snapshot: object) -> tuple[list[dict[str, object]], int, int]:
        """将快照转换为无正文泄漏的最小素材列表，并返回安全计数。"""
        if not isinstance(snapshot, Mapping):
            return [], 0, 0
        materials: list[dict[str, object]] = []
        sensitive_count = invalid_count = 0
        for field, material_type in (("diaries", "diary"), ("bets", "bet")):
            raw_items = snapshot.get(field)
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
        return materials, sensitive_count, invalid_count

    @staticmethod
    def _sanitize_material_summary(content: str) -> str:
        """替换敏感标识与可识别昵称，并将普通素材摘要限制在 80 字内。"""
        redacted = _MATERIAL_SENSITIVE_TEXT.sub("[REDACTED]", content)
        return _MATERIAL_SELF_NICKNAME.sub("我", redacted).strip()[:80]

    @staticmethod
    def _safe_material_refs(sanitized_material: object) -> list[str]:
        """返回脱敏材料中可供模型使用的非敏感稳定引用，最多八条。"""
        if not isinstance(sanitized_material, Mapping):
            return []
        materials = sanitized_material.get("materials")
        if not isinstance(materials, list):
            return []
        return list(dict.fromkeys(
            item["source_ref"]
            for item in materials
            if isinstance(item, Mapping)
            and item.get("sensitive") is False
            and isinstance(item.get("source_ref"), str)
        ))[:8]

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
    def _base_scenes_actions() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """生成三张无素材引用的基础卡，作为唯一发布前安全回退文档。"""
        scenes = [
            {"scene_id": f"scene-{index}", "scene_type": "summary", "source_refs": []}
            for index in range(1, 4)
        ]
        actions = [
            {"action_id": f"action-{index}", "scene_id": f"scene-{index}", "action_type": "show_card", "duration_ms": 3000}
            for index in range(1, 4)
        ]
        return scenes, actions

    @staticmethod
    def _is_safe_playback(scenes: object, actions: object) -> bool:
        """校验播放结构及动作引用；只允许当前模板链定义的安全字段组合。"""
        if not isinstance(scenes, list) or not 3 <= len(scenes) <= 16:
            return False
        if not all(
            isinstance(scene, dict)
            and isinstance(scene.get("scene_id"), str)
            and scene.get("scene_type") == "summary"
            and isinstance(scene.get("source_refs"), list)
            and all(isinstance(ref, str) for ref in scene["source_refs"])
            and ("body" not in scene or isinstance(scene["body"], str))
            and (not isinstance(scene.get("body"), str) or len(scene["body"]) <= 80)
            and not MemoirGuardrails.violations(scene.get("body"))
            for scene in scenes
        ):
            return False
        scene_ids = {scene["scene_id"] for scene in scenes}
        if len(scene_ids) != len(scenes):
            return False
        return (
            isinstance(actions, list)
            and len(actions) == len(scenes)
            and all(
                isinstance(action, dict)
                and isinstance(action.get("action_id"), str)
                and action.get("scene_id") in scene_ids
                and action.get("action_type") == "show_card"
                and isinstance(action.get("duration_ms"), int)
                and 1 <= action["duration_ms"] <= 30000
                for action in actions
            )
            and {action["scene_id"] for action in actions if isinstance(action, dict)} == scene_ids
        )

    def _model_data(self, run_id: str, node_id: str, request: dict[str, object]) -> object | None:
        """只接受成功 Gateway 的 data；状态和异常一律由确定性模板安全降级。"""
        if self._model_gateway is None:
            return None
        try:
            prompt_id = {
                "extract_highlights": "highlight-extract",
                "plan_chapters": "chapter-plan",
                "generate_scenes": "scene-generate",
            }.get(node_id)
            if prompt_id is None:
                return None
            prompt = self._prompts.load("memoir_agent", "1.0.0", prompt_id, "v1")
            refs = self._request_source_refs(request)
            context = self._contexts.build_node_context(
                trusted_instructions=prompt.template,
                materials=[{"source_ref": ref, "text": "[SOURCE_REF]"} for ref in refs],
                tool_results=[], token_budget=256,
            )
            # 仅传 Prompt 身份、策略和无正文上下文摘要；模板正文绝不进入可观测请求 DTO。
            safe_request = {
                "prompt_id": prompt.prompt_id,
                "prompt_version": prompt.version,
                "model_policy": prompt.model_policy,
                "context": context.safe_summary(),
                "input": request,
            }
            result = self._model_gateway.call(run_id, node_id, safe_request)
        except Exception:  # Gateway 已处理 usage；Runner 不记录请求或异常正文。
            logging.warning("MemoirAgent 模型能力不可用 node_id=%s", node_id)
            return None
        if getattr(result, "status", None) != "succeeded":
            logging.info("MemoirAgent 模型能力不可用 node_id=%s", node_id)
            return None
        return getattr(result, "data", None)


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
        output = self._parse_structured_output(data, _HighlightOutput, allowed_refs)
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
        output = self._parse_structured_output(data, _ChapterPlanOutput, allowed_refs)
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

    def _valid_scenes(self, data: object | None, allowed_refs: list[str]) -> list[dict[str, object]] | None:
        output = self._parse_structured_output(data, _ScenePlanOutput, allowed_refs)
        if not isinstance(output, _ScenePlanOutput):
            return None
        scenes = output.model_dump()["scenes"]
        if not 3 <= len(scenes) <= 8:
            return None
        if not all(isinstance(scene, Mapping) and isinstance(scene.get("scene_id"), str) and scene.get("scene_type") == "summary" and isinstance(scene.get("source_refs"), list) and all(isinstance(ref, str) and ref in allowed_refs for ref in scene["source_refs"]) for scene in scenes):
            return None
        if len({scene["scene_id"] for scene in scenes}) != len(scenes):
            return None
        return [
            {
                "scene_id": scene["scene_id"],
                "scene_type": "summary",
                "source_refs": list(dict.fromkeys(scene["source_refs"])),
                **({"body": scene["body"]} if isinstance(scene.get("body"), str) else {}),
            }
            for scene in scenes
        ]

    def _parse_structured_output(
        self,
        data: object | None,
        schema: type[BaseModel],
        trusted_refs: list[str],
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
            # 仅记录受控状态码，禁止把模型输出或可能含正文的异常写进日志。
            logging.info(
                "MemoirAgent 结构化模型输出被拒绝 parse_status=%s safety_status=%s error_codes=%s",
                parsed.parse_status, parsed.safety_status, parsed.error_codes,
            )
            return None
        return parsed.validated_value
