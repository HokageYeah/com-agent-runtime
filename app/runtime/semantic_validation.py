"""schema 通过后仍必须执行的确定性业务语义校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SemanticValidationResult:
    valid: bool
    error_codes: tuple[str, ...]
    safe_summary: dict[str, int]
    normalized_value: dict[str, object] | None


class SemanticValidator:
    """只允许受信任来源与有限的播放动作；URL/connector 等控制字段永不放行。"""

    # 结构化模型输出不能携带 Runtime 的控制面或认证材料；业务正文不在此名单内。
    _FORBIDDEN_CONTROL_FIELDS = {
        "url", "endpoint", "connector", "connector_id", "permission", "callback_target",
        "authorization", "authorization_version", "privacy", "privacy_version", "generation",
        "generation_epoch", "fencing_token", "execution_attempt", "run_id", "package_digest",
        "contract_version", "secret", "token", "access_token", "api_key", "password",
        "owner_id", "owner_scope", "tenant_id", "user_id",
    }
    _ACTION_TYPES = frozenset(
        {"show_card", "focus_image", "type_text", "hold", "play_tts", "transition"}
    )
    _STAT_KEYS = frozenset({"diary_count", "bet_count", "has_material"})

    def validate(
        self, value: object, schema: object = None, trusted_refs: set[str] | None = None,
        policy: object = None,
    ) -> SemanticValidationResult:
        del schema, policy  # schema 已在解析层校验；这里仅做确定性领域约束。
        if not isinstance(value, Mapping):
            return SemanticValidationResult(False, ("SEMANTIC_VALUE_INVALID",), {}, None)
        refs = trusted_refs or set()
        errors: list[str] = []
        normalized = dict(value)
        if _contains_forbidden_control_field(value, self._FORBIDDEN_CONTROL_FIELDS):
            errors.append("FORBIDDEN_CONTROL_FIELD")
        # 第一版不允许模型声明或参数化工具；工具只能由静态 workflow 经 ToolGateway 调用。
        if "tool_params" in value:
            errors.append("TOOL_PARAMETERS_FORBIDDEN")
        source_refs = value.get("source_refs", [])
        if not isinstance(source_refs, list) or any(not isinstance(ref, str) or ref not in refs for ref in source_refs):
            errors.append("UNKNOWN_SOURCE_REF")
        duration = value.get("duration_ms")
        if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int) or duration < 1):
            errors.append("INVALID_DURATION")
        self._validate_container(value, refs, errors)
        return SemanticValidationResult(
            not errors, tuple(dict.fromkeys(errors)),
            {"source_ref_count": len(source_refs) if isinstance(source_refs, list) else 0},
            normalized if not errors else None,
        )

    @staticmethod
    def _validate_container(
        value: Mapping[object, object], trusted_refs: set[str], errors: list[str],
    ) -> None:
        """校验模型可能返回的播放容器；只检查结构与引用，不读取正文。"""
        scenes = value.get("scenes")
        actions = value.get("actions")
        if scenes is not None:
            if not isinstance(scenes, list) or len(scenes) < 3:
                errors.append("SCENE_COUNT_INVALID")
                scenes = []
            scene_ids: set[str] = set()
            for scene in scenes:
                if not isinstance(scene, Mapping):
                    errors.append("SCENE_INVALID")
                    continue
                scene_id = scene.get("scene_id")
                if not isinstance(scene_id, str) or not scene_id or scene_id in scene_ids:
                    errors.append("SCENE_ID_INVALID")
                elif isinstance(scene_id, str):
                    scene_ids.add(scene_id)
                scene_refs = scene.get("source_refs", [])
                if not isinstance(scene_refs, list) or any(
                    not isinstance(ref, str) or ref not in trusted_refs for ref in scene_refs
                ):
                    errors.append("UNKNOWN_SOURCE_REF")
            if actions is not None:
                if not isinstance(actions, list) or len(actions) != len(scenes):
                    errors.append("ACTION_COMPLETENESS_INVALID")
                    actions = []
                covered_scene_ids: set[str] = set()
                for action in actions:
                    if not isinstance(action, Mapping):
                        errors.append("ACTION_INVALID")
                        continue
                    scene_id = action.get("scene_id")
                    if not isinstance(scene_id, str) or scene_id not in scene_ids:
                        errors.append("ACTION_SCENE_REF_INVALID")
                    elif isinstance(scene_id, str):
                        covered_scene_ids.add(scene_id)
                    if action.get("action_type") not in SemanticValidator._ACTION_TYPES:
                        errors.append("ACTION_TYPE_INVALID")
                    action_duration = action.get("duration_ms")
                    if (
                        isinstance(action_duration, bool)
                        or not isinstance(action_duration, int)
                        or action_duration < 1
                    ):
                        errors.append("INVALID_DURATION")
                if covered_scene_ids != scene_ids:
                    errors.append("ACTION_COMPLETENESS_INVALID")
        stats = value.get("stats")
        if stats is not None:
            if not isinstance(stats, Mapping) or set(stats) - SemanticValidator._STAT_KEYS:
                errors.append("INVALID_STATS")
            else:
                for key in ("diary_count", "bet_count"):
                    count = stats.get(key)
                    if (
                        isinstance(count, bool)
                        or not isinstance(count, int)
                        or count < 0
                    ):
                        errors.append("INVALID_STAT_COUNT")
                has_material = stats.get("has_material")
                if not isinstance(has_material, bool):
                    errors.append("INVALID_STATS")


def _contains_forbidden_control_field(value: object, forbidden_fields: set[str]) -> bool:
    """递归检查结构化结果键名，不读取也不输出私密字段值。"""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and key.lower() in forbidden_fields:
                return True
            if _contains_forbidden_control_field(nested, forbidden_fields):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_forbidden_control_field(item, forbidden_fields) for item in value)
    return False
