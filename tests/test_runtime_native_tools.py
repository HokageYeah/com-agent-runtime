"""Native Tool 的无正文安全适配测试。"""

from __future__ import annotations

from app.runtime.native_tools import (
    contains_sensitive_identifier,
    repair_json_once,
    summarize_keys,
)


def test_native_json_repair_accepts_one_fenced_json_value() -> None:
    """只接受一段 fenced JSON，去除围栏后返回解析结构。"""
    assert repair_json_once('```json\n{"scene_id":"s"}\n```') == {"scene_id": "s"}


def test_native_json_repair_rejects_non_json_or_non_string_value() -> None:
    """一次解析失败即交由上层 fallback，不能猜测或修补业务正文。"""
    assert repair_json_once("not-json") is None
    assert repair_json_once({"scene_id": "s"}) is None


def test_native_summary_never_copies_sensitive_string() -> None:
    """摘要只包含稳定键名与数量，绝不复制工具返回的私密正文。"""
    secret = "私密正文"

    summary = summarize_keys({"diary": secret, "count": 1}, max_items=2)

    assert summary == {"keys": ["count", "diary"], "item_count": 2}
    assert secret not in str(summary)


def test_native_summary_limits_keys_and_reports_container_size() -> None:
    """键名输出遵守上限，但 item_count 保留真实容器大小用于安全观测。"""
    assert summarize_keys({"z": 1, "a": 2, "b": 3}, max_items=2) == {
        "keys": ["a", "b"],
        "item_count": 3,
    }


def test_native_sensitive_identifier_scans_nested_values_without_returning_them() -> None:
    """嵌套手机号或身份证号只返回风险布尔值。"""
    assert contains_sensitive_identifier({"items": ["13800138000"]}) is True
    assert contains_sensitive_identifier({"items": ["safe-reference"]}) is False


def test_native_sensitive_identifier_detects_identifiers_adjacent_to_chinese_text() -> None:
    """手机号和身份证号紧邻中文时仍应被识别，且结果不能返回原始正文。"""
    phone_text = "用户手机号13800138000已脱敏"
    identity_text = "证件号11010519491231002X需要保护"

    phone_result = contains_sensitive_identifier(phone_text)
    identity_result = contains_sensitive_identifier(identity_text)

    assert phone_result is True
    assert identity_result is True
    assert phone_text not in str(phone_result)
    assert identity_text not in str(identity_result)
