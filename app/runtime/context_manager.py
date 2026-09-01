"""构造模型上下文时隔离可信指令和不可信业务素材。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

_SENSITIVE = re.compile(r"\b(?:1[3-9]\d{9}|\d{6,})\b")
_NODE_TOKEN_CAPS = {
    "extract_highlights": 256,
    "plan_chapters": 384,
    "generate_scenes": 512,
    # M7 bounded_loop 循环体节点：单批场景卡生成，与 generate_scenes 同族，
    # cap 取一致值（节点 cap 只会收紧 route/policy 计算出的可信输入窗口）。
    "generate_scene_batch": 512,
    # M7 覆盖修复节点：输入仅为缺失类型素材摘要，与循环体同族，cap 一致。
    "repair_coverage_gaps": 512,
}


@dataclass(frozen=True)
class NodeContext:
    """跨层只传递受控上下文；摘要不得包含日记、prompt 或播放文档正文。"""

    trusted_instructions: str
    untrusted_items: tuple[dict[str, str], ...]
    token_budget: int
    source_refs: tuple[str, ...]
    redaction_summary: dict[str, int]

    def safe_summary(self) -> dict[str, object]:
        """供 checkpoint/日志使用的无正文摘要。"""
        return {
            "token_budget": self.token_budget,
            "source_ref_count": len(self.source_refs),
            "redaction_summary": dict(self.redaction_summary),
        }


class ContextManager:
    """不可信内容只能进入 data 槽，不能覆盖 Runtime/Package 指令。"""

    def node_token_budget(self, node_id: str, model_token_budget: int) -> int:
        """节点 cap 只能收紧已由 route/policy 计算的可信输入窗口。"""
        if (
            not isinstance(node_id, str)
            or node_id not in _NODE_TOKEN_CAPS
            or isinstance(model_token_budget, bool)
            or not isinstance(model_token_budget, int)
            or model_token_budget <= 0
        ):
            raise ValueError("MODEL_NODE_BUDGET_UNAVAILABLE")
        return min(_NODE_TOKEN_CAPS[node_id], model_token_budget)

    def build_node_context(
        self, *, trusted_instructions: str, materials: Iterable[object],
        tool_results: Iterable[object], token_budget: int,
    ) -> NodeContext:
        if not isinstance(trusted_instructions, str) or not trusted_instructions:
            raise ValueError("trusted_instructions 不能为空")
        if isinstance(token_budget, bool) or not isinstance(token_budget, int) or token_budget <= 0:
            raise ValueError("token_budget 必须为正整数")
        items: list[dict[str, str]] = []
        refs: list[str] = []
        redacted = 0
        # token 预算属于整个节点，不是每条素材各自的上限；否则多条长素材会
        # 线性突破 route 的上下文窗口。第一版用字符近似，模型侧仍以策略/route 复核。
        remaining_chars = token_budget * 4
        for item in materials:
            if not isinstance(item, Mapping):
                continue
            ref, text = item.get("source_ref"), item.get("text")
            if not isinstance(ref, str) or not ref or not isinstance(text, str):
                continue
            clean, count = _SENSITIVE.subn("[REDACTED]", text)
            redacted += count
            if remaining_chars <= 0:
                break
            # 以字符上限近似 token 预算；分段按原顺序截取，绝不拼接或记录全文。
            content = clean[:remaining_chars]
            items.append({"source_ref": ref, "content": content})
            refs.append(ref)
            remaining_chars -= len(content)
        for item in tool_results:
            if not isinstance(item, Mapping):
                continue
            ref = item.get("source_ref")
            if not isinstance(ref, str) or not ref:
                continue
            text = item.get("text")
            if isinstance(text, str):
                # 即使 payload 只生成摘要，也统计被识别的敏感标识数量，便于安全观测。
                _, count = _SENSITIVE.subn("[REDACTED]", text)
                redacted += count
            # 工具 payload 不进入模型上下文；仅暴露稳定键名和容器规模供后续节点决策。
            keys = sorted(key for key in item if isinstance(key, str))[:8]
            if remaining_chars <= 0:
                break
            summary = f"keys:{','.join(keys)};items:{len(item)}"
            items.append(
                {"source_ref": ref, "content": summary[:remaining_chars]}
            )
            refs.append(ref)
            remaining_chars -= len(items[-1]["content"])
        return NodeContext(
            trusted_instructions=trusted_instructions,
            untrusted_items=tuple(items), token_budget=token_budget,
            source_refs=tuple(dict.fromkeys(refs)),
            redaction_summary={"redacted_fields": redacted, "item_count": len(items)},
        )
