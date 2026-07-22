"""回忆录播放文档的确定性 grounding 与领域评价器。"""

from __future__ import annotations

from collections.abc import Mapping

from app.runtime.guardrails import MemoirGuardrails
from app.runtime.semantic_validation import SemanticValidator
from app.schemas.evaluation import EvaluationDecisionDTO


class MemoirPlaybackEvaluator:
    """发布前统一校验场景、动作、素材引用与文本安全，失败统一请求安全回退。"""

    _SCENE_TYPES = frozenset({"cover", "stats", "diary_highlight", "bet_highlight", "image", "milestone", "summary"})
    _SAFETY_LEVELS = frozenset({"normal", "sensitive", "fallback"})
    _ACTION_TYPES = frozenset({"show_card", "focus_image", "type_text", "hold", "play_tts", "transition"})
    _CAPABILITY_ACTIONS = {"focus_image": "image", "play_tts": "tts"}

    def __init__(self) -> None:
        self._semantic = SemanticValidator()

    def evaluate(
        self,
        scenes: object,
        actions: object,
        *,
        trusted_source_refs: set[str],
        enabled_capabilities: set[str],
    ) -> EvaluationDecisionDTO:
        """评价候选文档；只基于冻结引用集合与显式能力开关，不做外部读取。"""
        scene_items = scenes if isinstance(scenes, list) else []
        action_items = actions if isinstance(actions, list) else []
        reasons: list[str] = []
        ref_count = 0
        scene_ids: set[str] = set()
        if not 3 <= len(scene_items) <= 16:
            reasons.append("SCENE_COUNT_INVALID")
        for scene in scene_items:
            if not isinstance(scene, Mapping):
                reasons.append("SCENE_INVALID")
                continue
            semantic = self._semantic.validate(scene, trusted_refs=trusted_source_refs)
            reasons.extend(semantic.error_codes)
            source_refs = scene.get("source_refs")
            ref_count += len(source_refs) if isinstance(source_refs, list) else 0
            scene_id = scene.get("scene_id")
            if not isinstance(scene_id, str) or not scene_id or scene_id in scene_ids:
                reasons.append("SCENE_ID_INVALID")
            elif isinstance(scene_id, str):
                scene_ids.add(scene_id)
            if scene.get("scene_type") not in self._SCENE_TYPES:
                reasons.append("SCENE_TYPE_INVALID")
            if scene.get("safety_level", "normal") not in self._SAFETY_LEVELS:
                reasons.append("SCENE_SAFETY_LEVEL_INVALID")
            body = scene.get("body")
            if body is not None and (not isinstance(body, str) or len(body) > 80):
                reasons.append("SCENE_BODY_INVALID")
            reasons.extend(MemoirGuardrails.violations(body))
        action_ids: set[str] = set()
        covered_scene_ids: set[str] = set()
        for expected_order, action in enumerate(action_items, start=1):
            if not isinstance(action, Mapping):
                reasons.append("ACTION_INVALID")
                continue
            semantic = self._semantic.validate(action, trusted_refs=trusted_source_refs)
            reasons.extend(semantic.error_codes)
            action_id = action.get("action_id")
            if not isinstance(action_id, str) or not action_id or action_id in action_ids:
                reasons.append("ACTION_ID_INVALID")
            elif isinstance(action_id, str):
                action_ids.add(action_id)
            action_type = action.get("action_type")
            if action_type not in self._ACTION_TYPES:
                reasons.append("ACTION_TYPE_INVALID")
            elif action_type in self._CAPABILITY_ACTIONS and self._CAPABILITY_ACTIONS[action_type] not in enabled_capabilities:
                reasons.append("ACTION_CAPABILITY_DISABLED")
            if action.get("action_order", expected_order) != expected_order:
                reasons.append("ACTION_ORDER_INVALID")
            scene_id = action.get("scene_id")
            if not isinstance(scene_id, str) or scene_id not in scene_ids:
                reasons.append("ACTION_SCENE_REF_INVALID")
            else:
                covered_scene_ids.add(scene_id)
        if len(action_items) != len(scene_items) or covered_scene_ids != scene_ids:
            reasons.append("ACTION_COMPLETENESS_INVALID")
        unique_reasons = tuple(dict.fromkeys(reasons))
        return EvaluationDecisionDTO(
            decision="pass" if not unique_reasons else "fallback",
            reasons=unique_reasons,
            scores={"grounding": 1 if "UNKNOWN_SOURCE_REF" not in unique_reasons else 0, "structure": 1 if not unique_reasons else 0},
            next_node="publish_document" if not unique_reasons else "safety_fallback",
            safe_summary={"scene_count": len(scene_items), "action_count": len(action_items), "source_ref_count": ref_count},
        )
