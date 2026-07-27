"""LangChain Tool 适配必须保留 ToolGateway 作为唯一业务执行边界。"""

from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool

from app.runtime.langchain_tools import build_langchain_tool
from app.schemas.agent_package import ToolManifest


class _RecordingGateway:
    """只记录安全结构，不模拟 connector 或 HTTP。"""

    def __init__(self) -> None:
        self.calls: list[tuple[ToolManifest, dict[str, object], str | None]] = []

    def call(
        self,
        manifest: ToolManifest,
        runtime_context: dict[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        self.calls.append((manifest, runtime_context, idempotency_key))
        return {"revision": 3, "content_digest": "safe-digest"}


def test_langchain_structured_tool_converts_schema_and_routes_through_gateway() -> None:
    """LangChain 调用只转换输入后回流 Gateway，不能持有 connector 或 HTTP 客户端。"""
    manifest = ToolManifest(
        name="memory.publish_playback_document",
        version="1.0.0",
        connector_id="couple_diary_backend",
        method="POST",
        relative_path="/api/v1/internal/agent-tools/memory.publish_playback_document",
        input_from="playback_document",
        output_to="publish_result",
        side_effect=True,
        cancellation_behavior="query_after_commit",
        input_schema={
            "type": "object",
            "properties": {"schema_version": {"type": "string"}},
            "required": ["schema_version"],
            "additionalProperties": False,
        },
    )
    gateway = _RecordingGateway()
    runtime_context: dict[str, object] = {
        "archive_id": "archive-1",
        "snapshot_id": "snapshot-1",
        "run_id": "run-1",
        "generation_epoch": 7,
    }

    tool = build_langchain_tool(
        manifest,
        gateway,
        runtime_context,
        idempotency_key="tool-operation-1",
    )

    assert isinstance(tool, StructuredTool)
    assert isinstance(tool, BaseTool)
    assert tool.invoke({"schema_version": "1.0.0"}) == {
        "revision": 3,
        "content_digest": "safe-digest",
    }
    assert gateway.calls == [
        (
            manifest,
            {
                **runtime_context,
                "playback_document": {"schema_version": "1.0.0"},
            },
            "tool-operation-1",
        )
    ]


def test_langchain_tool_rejects_schema_that_would_admit_control_fields() -> None:
    """适配器拒绝可信运行上下文控制字段，模型不能借 schema 覆盖其值。"""
    manifest = ToolManifest(
        name="memory.get_snapshot",
        version="1.0.0",
        connector_id="couple_diary_backend",
        method="POST",
        relative_path="/api/v1/internal/agent-tools/memory.get_snapshot",
        input_from="input",
        output_to="snapshot",
        cancellation_behavior="cancellable",
        input_schema={
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
        },
    )

    try:
        build_langchain_tool(
            manifest,
            _RecordingGateway(),
            {
                "archive_id": "archive-1",
                "snapshot_id": "snapshot-1",
                "run_id": "run-1",
                "generation_epoch": 7,
            },
        )
    except ValueError as exc:
        assert str(exc) == "LANGCHAIN_TOOL_INPUT_SCHEMA_UNSAFE"
    else:
        raise AssertionError("控制字段必须在适配期被拒绝")
