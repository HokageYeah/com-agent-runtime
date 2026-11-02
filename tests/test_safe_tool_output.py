import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.runtime import state as state_module
from app.runtime.state import AgentState


class WorkingState(BaseModel):
    """独立状态模型验证公共写入函数不依赖 Memoir 字段。"""

    model_config = ConfigDict(extra="forbid")
    # 仅内存保存的业务计数，便于验证 Pydantic 类型约束及原子更新。
    result: dict[str, int] = Field(default_factory=lambda: {"count": 1})
    # 非目标字段在成功或失败路径中都应保持原样。
    markers: list[str] = Field(default_factory=lambda: ["ready"])


def test_safe_output_validates_and_updates_a_separate_state_model() -> None:
    model = WorkingState()

    state_module.apply_safe_output(model, "result", {"count": "2"}, frozenset({"result"}))

    assert model.result == {"count": 2}
    assert model.markers == ["ready"]


@pytest.mark.parametrize(
    ("target", "output", "error_code"),
    [
        ("markers", [], "TOOL_OUTPUT_TARGET_FORBIDDEN"),
        ("authorization_version", {"count": 2}, "TOOL_OUTPUT_TARGET_FORBIDDEN"),
        ("result", {"nested": {"items": [{"Access_Token": "synthetic"}]}}, "TOOL_OUTPUT_SENSITIVE_FIELD"),
        ("result", [], "dict_type"),
        ("result", {"count": []}, "int_type"),
    ],
)
def test_safe_output_failure_preserves_the_entire_model(
    target: str, output: object, error_code: str,
) -> None:
    model = WorkingState()
    before = model.model_dump()

    with pytest.raises(ValueError, match=error_code):
        state_module.apply_safe_output(model, target, output, frozenset({"result"}))

    assert model.model_dump() == before


def test_agent_state_type_error_preserves_original_pydantic_error_and_state() -> None:
    """原入口也须先校验再写入，类型错误沿用原 Pydantic 错误。"""
    model = AgentState(snapshot={"source_refs": []}, completed_node_ids=["load_snapshot"])
    before = model.model_dump()

    with pytest.raises(ValidationError) as error:
        model.apply_tool_output("snapshot", [])

    assert error.value.errors()[0]["type"] == "dict_type"
    assert model.model_dump() == before
