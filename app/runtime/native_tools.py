"""Runtime 内置的无副作用工具适配，禁止记录或转存输入正文。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

# 与 ToolGateway 一致，仅识别当前 Runtime 明确需要阻断的手机号和身份证号格式。
# 中文属于 Unicode 单词字符，不能用 \b 作为边界；仅排除相邻数字，避免漏检中文紧邻的标识符。
_SENSITIVE_IDENTIFIER = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|\d{17}[\dXx])(?!\d)")


def repair_json_once(value: object) -> object | None:
    """最多移除一次 JSON 围栏并解析；失败返回 None 交由节点 fallback。"""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.startswith("```json"):
        if not candidate.endswith("```"):
            return None
        candidate = candidate.removeprefix("```json").removesuffix("```").strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def summarize_keys(value: object, max_items: int) -> dict[str, list[str] | int]:
    """生成可进入上下文的结构摘要，只保留键名和容器元素数量。"""
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 0:
        raise ValueError("max_items 必须为非负整数")
    if isinstance(value, Mapping):
        return {
            "keys": sorted(key for key in value if isinstance(key, str))[:max_items],
            "item_count": len(value),
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {"keys": [], "item_count": len(value)}
    return {"keys": [], "item_count": 0}


def contains_sensitive_identifier(value: object) -> bool:
    """递归检测工具结果中的敏感标识符，只返回布尔风险信号。"""
    if isinstance(value, str):
        return _SENSITIVE_IDENTIFIER.search(value) is not None
    if isinstance(value, Mapping):
        return any(contains_sensitive_identifier(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(contains_sensitive_identifier(item) for item in value)
    return False
