"""构造模型上下文时隔离可信指令和不可信业务素材。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

_SENSITIVE = re.compile(r"\b(?:1[3-9]\d{9}|\d{6,})\b")


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
        for item in [*materials, *tool_results]:
            if not isinstance(item, Mapping):
                continue
            ref, text = item.get("source_ref"), item.get("text")
            if not isinstance(ref, str) or not ref or not isinstance(text, str):
                continue
            clean, count = _SENSITIVE.subn("[REDACTED]", text)
            redacted += count
            # 以字符上限近似 token 预算，至少保留来源而不将超长私密正文带入上下文。
            items.append({"source_ref": ref, "content": clean[: token_budget * 4]})
            refs.append(ref)
        return NodeContext(
            trusted_instructions=trusted_instructions,
            untrusted_items=tuple(items), token_budget=token_budget,
            source_refs=tuple(dict.fromkeys(refs)),
            redaction_summary={"redacted_fields": redacted, "item_count": len(items)},
        )
