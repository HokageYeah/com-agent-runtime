"""Memoir 模型节点的安全降级回归。"""

from __future__ import annotations

from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.runtime.state import AgentState


def _run() -> object:
    return type("Run", (), {"run_id": "run-1"})()


def test_model_capability_unavailable_uses_template_without_snapshot_body() -> None:
    class ModelGateway:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            self.requests.append(request)
            return type("Result", (), {"status": "route_not_allowed", "data": None})()

    gateway = ModelGateway()
    state = AgentState(
        snapshot={"diaries": [{"id": "diary-1", "content": "绝不能泄露的日记正文"}]}
    )

    result = MemoirNodeRunner(object(), model_gateway=gateway).run_node(
        {"node_id": "extract_highlights"}, _run(), state
    )

    assert result == {"node_id": "extract_highlights", "fallback": True}
    assert state.highlights == {"source_refs": ["diary:diary-1"], "mode": "template"}
    assert state.fallback_flags == ["model_unavailable_highlights", "template_highlights"]
    assert gateway.requests == [{"source_refs": ["diary:diary-1"]}]
    assert "绝不能泄露的日记正文" not in str(gateway.requests)


def test_invalid_model_structure_uses_safe_scene_template() -> None:
    class ModelGateway:
        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            assert node_id == "generate_scenes"
            assert request == {
                "chapters": [
                    {
                        "chapter_id": "chapter-1",
                        "source_refs": ["diary:diary-1"],
                        "kind": "memory_overview",
                    }
                ]
            }
            return type(
                "Result",
                (),
                {
                    "status": "succeeded",
                    "data": {
                        "scenes": [
                            {
                                "scene_id": "scene-1",
                                "scene_type": "summary",
                                "source_refs": ["diary:forged"],
                            }
                        ]
                    },
                },
            )()

    state = AgentState(
        chapter_plan={
            "chapters": [
                {
                    "chapter_id": "chapter-1",
                    "source_refs": ["diary:diary-1"],
                    "kind": "memory_overview",
                }
            ]
        }
    )

    result = MemoirNodeRunner(object(), model_gateway=ModelGateway()).run_node(
        {"node_id": "generate_scenes"}, _run(), state
    )

    assert result == {"node_id": "generate_scenes", "fallback": True}
    assert state.scenes == [
        {
            "scene_id": "scene-1",
            "scene_type": "summary",
            "source_refs": ["diary:diary-1"],
        }
    ]
    assert state.fallback_flags == ["model_invalid_scenes", "template_scenes"]


def test_duplicate_model_scene_ids_use_safe_scene_template() -> None:
    class ModelGateway:
        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            return type(
                "Result",
                (),
                {
                    "status": "succeeded",
                    "data": {
                        "scenes": [
                            {
                                "scene_id": "scene-1",
                                "scene_type": "summary",
                                "source_refs": ["diary:diary-1"],
                            },
                            {
                                "scene_id": "scene-1",
                                "scene_type": "summary",
                                "source_refs": ["diary:diary-1"],
                            },
                        ]
                    },
                },
            )()

    state = AgentState(
        chapter_plan={
            "chapters": [
                {
                    "chapter_id": "chapter-1",
                    "source_refs": ["diary:diary-1"],
                    "kind": "memory_overview",
                }
            ]
        }
    )

    result = MemoirNodeRunner(object(), model_gateway=ModelGateway()).run_node(
        {"node_id": "generate_scenes"}, _run(), state
    )

    assert result == {"node_id": "generate_scenes", "fallback": True}
    assert state.fallback_flags == ["model_invalid_scenes", "template_scenes"]


def test_missing_model_chapter_list_uses_template() -> None:
    class ModelGateway:
        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            return type("Result", (), {"status": "succeeded", "data": {}})()

    state = AgentState(highlights={"source_refs": ["diary:diary-1"]})

    result = MemoirNodeRunner(object(), model_gateway=ModelGateway()).run_node(
        {"node_id": "plan_chapters"}, _run(), state
    )

    assert result == {"node_id": "plan_chapters", "fallback": True}
    assert state.chapter_plan == {
        "chapters": [
            {
                "chapter_id": "chapter-1",
                "source_refs": ["diary:diary-1"],
                "kind": "memory_overview",
            }
        ]
    }
