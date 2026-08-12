"""跨仓 memory ToolError fixture 的版本协商守护。"""

import json
from hashlib import sha256
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _resolve(name: str) -> dict[str, object]:
    fixture = _read(name)
    parent = fixture.pop("extends", None)
    if parent is None:
        return fixture
    assert parent == "memory-runtime-contract-v1.0.0.json"
    base = _read(parent)
    tool_contract = {**base["tool_contract"], **fixture.pop("tool_contract")}
    return {**base, **fixture, "tool_contract": tool_contract}


def test_v1_0_fixture_is_frozen_head_baseline_and_v1_1_is_explicit_extension() -> None:
    legacy = FIXTURES / "memory-runtime-contract-v1.0.0.json"
    assert sha256(legacy.read_bytes()).hexdigest() == (
        "04a0c12594e0ee1ca062b40842d1d4140aaad52d7f63b9a6c8dc03f9cba1b929"
    )
    assert _resolve(legacy.name)["contract_version"] == "1.0.0"
    v11 = _resolve("memory-runtime-contract-v1.1.0.json")
    assert v11["contract_version"] == "1.1.0"
    assert len(v11["tool_contract"]["tool_error_matrix"]) == 9


def test_only_known_fixture_versions_are_selectable() -> None:
    selectable = {
        "1.0.0": "memory-runtime-contract-v1.0.0.json",
        "1.1.0": "memory-runtime-contract-v1.1.0.json",
    }
    assert _resolve(selectable["1.0.0"])["contract_version"] == "1.0.0"
    assert _resolve(selectable["1.1.0"])["contract_version"] == "1.1.0"
    assert "1.2.0" not in selectable


def test_v1_1_freezes_tool_contract_negotiation_metadata() -> None:
    fixture = _read("memory-runtime-contract-v1.1.0.json")
    assert fixture["negotiation"] == {
        "request_header": "X-Agent-Tool-Contract-Version",
        "supported_versions": ["1.0.0", "1.1.0"],
        "default_version": "1.0.0",
        "unsupported_version_behavior": "fail_closed",
    }
    assert fixture["identity_headers"] == {
        "business_to_runtime": "X-Agent-Client-Id",
        "runtime_to_business_tool_and_callback": "X-Agent-Runtime-Id",
        "mixed_or_missing_identity_headers": "fail_closed",
    }
