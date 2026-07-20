"""无执行能力的 JSON 小修复；不调用模型，也不记录待修复正文。"""

from __future__ import annotations

import ast
import json


def parse_json_once(raw: str) -> tuple[object | None, str]:
    """返回严格 JSON 或 Python 字面量修复结果；失败不泄漏原文。"""
    if not isinstance(raw, str):
        return None, "failed"
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        parts = text.split("\n", 1)
        text = parts[1].rsplit("\n", 1)[0] if len(parts) == 2 else ""
    try:
        return json.loads(text), "parsed"
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(text), "repaired"
        except (SyntaxError, ValueError):
            return None, "failed"
