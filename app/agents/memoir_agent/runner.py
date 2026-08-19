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
from app.runtime.interfaces import LeaseContext
from app.runtime.material_schema import (
    detect_envelope_mixing,
)
from app.runtime.prompt_registry import PromptRegistry
from app.runtime.state import AgentState
from app.runtime.structured_output import StructuredOutputParser
from app.runtime.tool_gateway import ToolErrorRejected, ToolGateway
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
        self._lease_context: LeaseContext | None = None

    def bind_lease_context(self, lease_context: LeaseContext) -> None:
        """Executor 每个节点前绑定有效写上下文，拒绝迟到工具结果落库。"""
        self._lease_context = lease_context

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
            chapter_request: dict[str, object] = {"source_refs": safe_refs}
            model_data = self._model_data(
                run.run_id, "plan_chapters", chapter_request, run.agent_version,
            )
            validated_chapters = self._valid_chapters(model_data, safe_refs)
            if validated_chapters is None and model_data is not None:
                repaired = self._repair_model_data(
                    run.run_id, "plan_chapters", chapter_request, model_data,
                    run.agent_version,
                )
                validated_chapters = self._valid_chapters(repaired, safe_refs)
            if validated_chapters is not None:
                state.apply_tool_output(
                    "chapter_plan",
                    {"chapters": validated_chapters},
                )
                logging.info(
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
            model_data = self._model_data(
                run.run_id, "generate_scenes", scene_request, run.agent_version,
            )
            validated_scenes = self._valid_scenes(
                model_data,
                self._source_refs(safe_chapters),
            )
            if validated_scenes is None and model_data is not None:
                repaired = self._repair_model_data(
                    run.run_id, "generate_scenes", scene_request, model_data,
                    run.agent_version,
                )
                validated_scenes = self._valid_scenes(
                    repaired,
                    self._source_refs(safe_chapters),
                )
            if validated_scenes is not None:
                state.apply_tool_output("scenes", validated_scenes)
                logging.info(
                    "MemoirAgent 模型场景完成 run_id=%s scene_count=%s",
                    run.run_id,
                    len(validated_scenes),
                )
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
            highlight_request: dict[str, object] = {"source_refs": refs}
            model_data = self._model_data(
                run.run_id, "extract_highlights", highlight_request, run.agent_version,
            )
            validated_highlights = self._valid_highlights(model_data, refs)
            if validated_highlights is None and model_data is not None:
                repaired = self._repair_model_data(
                    run.run_id,
                    "extract_highlights",
                    highlight_request,
                    model_data,
                    run.agent_version,
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
                logging.info(
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
            else:
                diaries = snapshot.get("diary_items", snapshot.get("diaries", []))
                bets = snapshot.get(
                    "completed_bet_items",
                    snapshot.get("completed_bets", snapshot.get("bet_items", snapshot.get("bets", []))),
                )
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
        if node.get("node_id") == "enqueue_media_tasks":
            # 第一版媒体能力关闭。该节点必须是确定性无副作用跳过点：
            # 不解析作品正文、不调用预留 Tool、不创建媒体任务。
            state.media_tasks = []
            logging.info(
                "MemoirAgent 媒体能力已关闭 run_id=%s code=%s",
                run.run_id,
                "CAPABILITY_DISABLED",
            )
            return {
                "node_id": "enqueue_media_tasks",
                "skipped": True,
                "reason_code": "CAPABILITY_DISABLED",
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
        logging.info("MemoirAgent 已加载快照 run_id=%s archive_id=%s", run.run_id, archive_id)
        return {"node_id": "load_snapshot", "snapshot_loaded": True}

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
            if material_type in summary_types and isinstance(payload, Mapping):
                # 元数据摘要：白名单元数据紧凑 JSON 后复用统一截断/脱敏，
                # 与 legacy content 摘要走同一条 _sanitize_material_summary 管线。
                summary = MemoirNodeRunner._sanitize_material_summary(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                )
                materials.append(
                    {
                        "source_ref": source_ref,
                        "type": material_type,
                        "sensitive": False,
                        "summary": summary,
                    }
                )
                continue
            materials.append(
                {"source_ref": source_ref, "type": material_type, "sensitive": True}
            )
            sensitive_count += 1
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

    def _model_data(
        self, run_id: str, node_id: str, request: dict[str, object], agent_version: str,
    ) -> object | None:
        """只接受成功 Gateway 的 data；状态和异常一律由确定性模板安全降级。"""
        if self._model_gateway is None:
            return None
        try:
            safe_request = self._safe_model_request(node_id, request, agent_version)
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
    ) -> object | None:
        """最多调用一次受信任 repair 边界；原输出只沿当前调用栈短暂传递。"""
        if self._model_gateway is None:
            return None
        repair = getattr(self._model_gateway, "repair", None)
        if not callable(repair):
            return None
        try:
            safe_request = self._safe_model_request(node_id, request, agent_version)
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
    ) -> dict[str, object] | None:
        """构造不含模板正文与素材正文的可观测请求摘要。"""
        prompt_id = {
            "extract_highlights": "highlight-extract",
            "plan_chapters": "chapter-plan",
            "generate_scenes": "scene-generate",
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
        return {
            "prompt_id": prompt.prompt_id,
            "prompt_version": prompt.version,
            "model_policy": prompt.model_policy,
            "context": context.safe_summary(),
            "input": request,
        }

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

    def _valid_scenes(self, data: object | None, allowed_refs: list[str]) -> list[dict[str, object]] | None:
        output = self._parse_structured_output(data, _ScenePlanOutput, allowed_refs, "generate_scenes")
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
