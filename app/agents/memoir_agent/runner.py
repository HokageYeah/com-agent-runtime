"""MemoirAgent 已实现节点的受信任 Runner。"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Protocol

import httpx

from app.models import AgentRun
from app.runtime.state import AgentState
from app.runtime.tool_gateway import ToolGateway
from app.services.tool_call_audit_service import ToolCallAuditService


class MemoirModelGateway(Protocol):
    """Memoir 模型节点到受信任 Gateway 的最小适配边界。

    调用方负责在此边界之下构造权威 ModelCallContext；Runner 只传递经过
    allowlist 裁剪的结构化摘要，绝不传递快照正文或 prompt 正文。
    """

    def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object: ...


class MemoirNodeRunner:
    """回忆录 MVP 节点执行器，只输出不含日记正文的结构化播放文档。"""

    def __init__(
        self,
        gateway: ToolGateway,
        audit: ToolCallAuditService | None = None,
        model_gateway: MemoirModelGateway | None = None,
    ) -> None:
        self._gateway, self._audit = gateway, audit
        self._model_gateway = model_gateway

    def run_node(self, node: dict[str, object], run: AgentRun, state: AgentState) -> dict[str, object]:
        if node.get("node_id") == "safety_review":
            # 审核只接受第一版规则节点生产的最小结构，避免未校验字段进入发布载荷。
            if self._is_safe_playback(state.scenes, state.actions):
                decision = "passed"
            else:
                # 不安全或不完整时回退到无素材引用的基础卡片，保证发布端不会收到畸形文档。
                state.scenes = [{"scene_id": "scene-1", "scene_type": "summary", "source_refs": []}]
                state.actions = [{"action_id": "action-1", "scene_id": "scene-1", "action_type": "show_card", "duration_ms": 3000}]
                state.fallback_flags.append("safety_fallback")
                decision = "fallback"
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
                state.chapter_plan = {"chapters": chapters}
                logging.info("MemoirAgent 模型章节完成 run_id=%s chapter_count=%s", run.run_id, len(chapters))
                return {"node_id": "plan_chapters", "fallback": False}
            if self._model_gateway is not None:
                state.fallback_flags.append("model_invalid_chapters")
            state.chapter_plan = {"chapters": [{"chapter_id": "chapter-1", "source_refs": safe_refs, "kind": "memory_overview"}]}
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
                state.scenes = scenes
                logging.info("MemoirAgent 模型场景完成 run_id=%s scene_count=%s", run.run_id, len(scenes))
                return {"node_id": "generate_scenes", "fallback": False}
            if self._model_gateway is not None:
                state.fallback_flags.append("model_invalid_scenes")
            scenes: list[dict[str, object]] = []
            for index, chapter in enumerate(safe_chapters[:3], start=1):
                refs = chapter["source_refs"]
                scenes.append({"scene_id": f"scene-{index}", "scene_type": "summary", "source_refs": refs})
            state.scenes = scenes or [{"scene_id": "scene-1", "scene_type": "summary", "source_refs": []}]
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
            # 第一版模型能力未接入，模板 fallback 只能保留素材稳定 ID，不能复制正文。
            snapshot = state.snapshot if isinstance(state.snapshot, dict) else {}
            refs: list[str] = []
            for field, prefix in (("diaries", "diary"), ("bets", "bet")):
                items = snapshot.get(field, [])
                if isinstance(items, list):
                    for item in items[:8]:
                        if isinstance(item, dict) and isinstance(item.get("id"), str):
                            refs.append(f"{prefix}:{item['id']}")
            model_data = self._model_data(run.run_id, "extract_highlights", {"source_refs": refs})
            highlights = self._valid_highlights(model_data, refs)
            if highlights is not None:
                state.highlights = {"source_refs": highlights, "mode": "model"}
                logging.info("MemoirAgent 模型高光完成 run_id=%s ref_count=%s", run.run_id, len(highlights))
                return {"node_id": "extract_highlights", "fallback": False}
            if self._model_gateway is not None:
                state.fallback_flags.append("model_unavailable_highlights")
            state.highlights = {"source_refs": refs, "mode": "template"}
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
        if node.get("node_id") == "publish_document":
            if not isinstance(state.playback_document, dict):
                raise ValueError("PLAYBACK_DOCUMENT_MISSING")
            archive_id, snapshot_id, epoch = run.input_json.get("archive_id"), run.input_json.get("snapshot_id"), run.input_json.get("generation_epoch")
            if not isinstance(archive_id, str) or not isinstance(snapshot_id, str) or not isinstance(epoch, int):
                raise ValueError("MEMORY_PUBLISH_REFERENCE_INVALID")
            logical_key = f"{run.run_id}:publish_document:memory.publish_playback_document:{epoch}"
            digest = hashlib.sha256(json.dumps(state.playback_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            unknown = self._audit.latest_unknown(run.run_id, logical_key) if self._audit else None
            if unknown is not None and unknown.idempotency_key:
                reconciled = self._gateway.get_publish_result(run.business_connector_id, archive_id, run.run_id, unknown.idempotency_key)
                if reconciled is not None:
                    state.publish_result = reconciled
                    self._audit.succeed(unknown, int(reconciled["revision"]), str(reconciled["content_digest"]))
                    logging.info("MemoirAgent 对账恢复发布结果 run_id=%s", run.run_id)
                    return {"node_id": "publish_document", "published": True}
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
                logging.exception("MemoirAgent 发布调用异常 run_id=%s", run.run_id)
                raise
            if audit is not None:
                self._audit.succeed(audit, int(state.publish_result["revision"]), str(state.publish_result["content_digest"]))
            logging.info("MemoirAgent 已发布作品 run_id=%s archive_id=%s", run.run_id, archive_id)
            return {"node_id": "publish_document", "published": True}
        if node.get("node_id") != "load_snapshot":
            raise ValueError("MEMOIR_NODE_NOT_IMPLEMENTED")
        archive_id = run.input_json.get("archive_id")
        snapshot_id = run.input_json.get("snapshot_id")
        if not isinstance(archive_id, str) or not isinstance(snapshot_id, str):
            raise ValueError("MEMORY_SNAPSHOT_REFERENCE_INVALID")
        state.snapshot = self._gateway.get_snapshot(run.business_connector_id, archive_id, snapshot_id)
        logging.info("MemoirAgent 已加载快照 run_id=%s archive_id=%s", run.run_id, archive_id)
        return {"node_id": "load_snapshot", "snapshot_loaded": True}

    @staticmethod
    def _is_safe_playback(scenes: object, actions: object) -> bool:
        """校验播放结构及动作引用；只允许当前模板链定义的安全字段组合。"""
        if not isinstance(scenes, list) or not 1 <= len(scenes) <= 16:
            return False
        if not all(isinstance(scene, dict) and isinstance(scene.get("scene_id"), str) and scene.get("scene_type") == "summary" and isinstance(scene.get("source_refs"), list) and all(isinstance(ref, str) for ref in scene["source_refs"]) for scene in scenes):
            return False
        scene_ids = {scene["scene_id"] for scene in scenes}
        if len(scene_ids) != len(scenes):
            return False
        return isinstance(actions, list) and bool(actions) and all(isinstance(action, dict) and isinstance(action.get("action_id"), str) and action.get("scene_id") in scene_ids and action.get("action_type") == "show_card" and isinstance(action.get("duration_ms"), int) and 1 <= action["duration_ms"] <= 30000 for action in actions)

    def _model_data(self, run_id: str, node_id: str, request: dict[str, object]) -> object | None:
        """只接受成功 Gateway 的 data；状态和异常一律由确定性模板安全降级。"""
        if self._model_gateway is None:
            return None
        try:
            result = self._model_gateway.call(run_id, node_id, request)
        except Exception:  # Gateway 已处理 usage；Runner 不记录请求或异常正文。
            logging.warning("MemoirAgent 模型能力不可用 node_id=%s", node_id)
            return None
        if getattr(result, "status", None) != "succeeded":
            logging.info("MemoirAgent 模型能力不可用 node_id=%s", node_id)
            return None
        return getattr(result, "data", None)

    @staticmethod
    def _valid_highlights(data: object | None, allowed_refs: list[str]) -> list[str] | None:
        if not isinstance(data, Mapping) or not isinstance(data.get("source_refs"), list):
            return None
        refs = data["source_refs"]
        if not all(isinstance(ref, str) and ref in allowed_refs for ref in refs):
            return None
        return list(dict.fromkeys(refs))[:8]

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

    @staticmethod
    def _valid_chapters(data: object | None, allowed_refs: list[str]) -> list[dict[str, object]] | None:
        if not isinstance(data, Mapping) or not isinstance(data.get("chapters"), list):
            return None
        raw_chapters = data["chapters"]
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

    @staticmethod
    def _valid_scenes(data: object | None, allowed_refs: list[str]) -> list[dict[str, object]] | None:
        if not isinstance(data, Mapping) or not isinstance(data.get("scenes"), list):
            return None
        scenes = data["scenes"]
        if not 1 <= len(scenes) <= 3:
            return None
        if not all(isinstance(scene, Mapping) and isinstance(scene.get("scene_id"), str) and scene.get("scene_type") == "summary" and isinstance(scene.get("source_refs"), list) and all(isinstance(ref, str) and ref in allowed_refs for ref in scene["source_refs"]) for scene in scenes):
            return None
        if len({scene["scene_id"] for scene in scenes}) != len(scenes):
            return None
        return [{"scene_id": scene["scene_id"], "scene_type": "summary", "source_refs": list(dict.fromkeys(scene["source_refs"]))} for scene in scenes]
