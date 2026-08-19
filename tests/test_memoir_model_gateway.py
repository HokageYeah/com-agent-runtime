"""Memoir 模型节点的安全降级回归。"""

from __future__ import annotations

from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.runtime.state import AgentState


def _run() -> object:
    # run.agent_version 由执行链路读取（_safe_model_request 据此加载 Prompt 身份）；
    # 这里给一个 PromptRegistry 可解析的 memoir_agent 版本，避免属性缺失。
    return type("Run", (), {"run_id": "run-1", "agent_version": "1.0.0"})()


def test_model_capability_unavailable_uses_template_without_snapshot_body(caplog: object) -> None:
    class ModelGateway:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            self.requests.append(request)
            return type("Result", (), {"status": "route_not_allowed", "data": None})()

    gateway = ModelGateway()
    state = AgentState(
        snapshot={"diaries": [{"id": "diary-1", "content": "绝不能泄露的日记正文"}]},
        sanitized_material={"materials": [
            {"source_ref": "diary:diary-1", "type": "diary", "sensitive": False, "summary": "摘要"},
        ]},
    )

    result = MemoirNodeRunner(object(), model_gateway=gateway).run_node(
        {"node_id": "extract_highlights"}, _run(), state
    )

    assert result == {"node_id": "extract_highlights", "fallback": True}
    assert state.highlights == {"source_refs": ["diary:diary-1"], "mode": "template"}
    assert state.fallback_flags == ["model_unavailable_highlights", "template_highlights"]
    assert gateway.requests == [{
        "prompt_id": "highlight-extract", "prompt_version": "v1",
        "model_policy": "strict",
        "context": {"token_budget": 256, "source_ref_count": 1,
                    "redaction_summary": {"redacted_fields": 0, "item_count": 1}},
        "input": {"source_refs": ["diary:diary-1"]},
    }]
    assert "绝不能泄露的日记正文" not in str(gateway.requests)
    assert "绝不能泄露的日记正文" not in caplog.text  # type: ignore[attr-defined]


def test_invalid_model_structure_uses_safe_scene_template() -> None:
    class ModelGateway:
        def __init__(self) -> None:
            self.rejections: list[tuple[str, tuple[str, ...]]] = []

        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            assert node_id == "generate_scenes"
            assert request["input"] == {"chapters": [{"chapter_id": "chapter-1", "source_refs": ["diary:diary-1"], "kind": "memory_overview"}]}
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

        def record_validation_rejection(self, node_id: str, error_codes: tuple[str, ...]) -> None:
            self.rejections.append((node_id, error_codes))

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

    gateway = ModelGateway()
    result = MemoirNodeRunner(object(), model_gateway=gateway).run_node(
        {"node_id": "generate_scenes"}, _run(), state
    )

    assert result == {"node_id": "generate_scenes", "fallback": True}
    # 模板兜底场景带固定安全正文，保证模型输出被拒时发布卡片不空白。
    assert state.scenes == [
        {"scene_id": "scene-1", "scene_type": "summary", "source_refs": ["diary:diary-1"], "body": "这一路的小事，都被好好收藏在这本回忆里。"},
        {"scene_id": "scene-2", "scene_type": "summary", "source_refs": [], "body": "每一次并肩与交心，都是我们最珍贵的默契。"},
        {"scene_id": "scene-3", "scene_type": "summary", "source_refs": [], "body": "往后的日子，也一起慢慢写下新的故事吧。"},
    ]
    assert state.fallback_flags == ["model_invalid_scenes", "template_scenes"]
    assert gateway.rejections == [("generate_scenes", ("UNKNOWN_SOURCE_REF", "SCENE_COUNT_INVALID"))]


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


def test_repaired_json_model_highlights_are_accepted_without_exposing_raw_text(caplog: object) -> None:
    class ModelGateway:
        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            assert node_id == "extract_highlights"
            return type(
                "Result",
                (),
                {"status": "succeeded", "data": "```json\n{'source_refs': ['diary:diary-1']}\n```"},
            )()

    state = AgentState(
        snapshot={"diaries": [{"id": "diary-1", "content": "私密正文"}]},
        sanitized_material={"materials": [
            {"source_ref": "diary:diary-1", "type": "diary", "sensitive": False, "summary": "摘要"},
        ]},
    )

    result = MemoirNodeRunner(object(), model_gateway=ModelGateway()).run_node(
        {"node_id": "extract_highlights"}, _run(), state
    )

    assert result == {"node_id": "extract_highlights", "fallback": False}
    assert state.highlights == {"source_refs": ["diary:diary-1"], "mode": "model"}
    assert "私密正文" not in str(result)
    assert "私密正文" not in caplog.text  # type: ignore[attr-defined]


def test_invalid_model_output_uses_one_versioned_repair_before_template_fallback() -> None:
    class ModelGateway:
        def __init__(self) -> None:
            self.calls = 0
            self.repairs = 0

        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            self.calls += 1
            return type("Result", (), {"status": "succeeded", "data": "not-json"})()

        def repair(
            self,
            run_id: str,
            node_id: str,
            request: dict[str, object],
            invalid_output: object,
        ) -> object:
            self.repairs += 1
            assert request["prompt_id"] == "highlight-extract"
            return type(
                "Result",
                (),
                {
                    "status": "succeeded",
                    "data": {"source_refs": ["diary:diary-1"]},
                },
            )()

    gateway = ModelGateway()
    state = AgentState(
        sanitized_material={
            "materials": [
                {
                    "source_ref": "diary:diary-1",
                    "type": "diary",
                    "sensitive": False,
                    "summary": "摘要",
                },
            ],
        },
    )

    result = MemoirNodeRunner(object(), model_gateway=gateway).run_node(
        {"node_id": "extract_highlights"}, _run(), state
    )

    assert result == {"node_id": "extract_highlights", "fallback": False}
    assert state.highlights == {
        "source_refs": ["diary:diary-1"],
        "mode": "model",
    }
    assert gateway.calls == 1
    assert gateway.repairs == 1


def test_invalid_repair_output_is_not_repaired_recursively() -> None:
    class ModelGateway:
        def __init__(self) -> None:
            self.repairs = 0

        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            return type("Result", (), {"status": "succeeded", "data": "not-json"})()

        def repair(
            self,
            run_id: str,
            node_id: str,
            request: dict[str, object],
            invalid_output: object,
        ) -> object:
            self.repairs += 1
            return type("Result", (), {"status": "succeeded", "data": "still-not-json"})()

    gateway = ModelGateway()
    state = AgentState(
        sanitized_material={
            "materials": [
                {
                    "source_ref": "diary:diary-1",
                    "type": "diary",
                    "sensitive": False,
                    "summary": "摘要",
                },
            ],
        },
    )

    result = MemoirNodeRunner(object(), model_gateway=gateway).run_node(
        {"node_id": "extract_highlights"}, _run(), state
    )

    assert result == {"node_id": "extract_highlights", "fallback": True}
    assert state.highlights == {
        "source_refs": ["diary:diary-1"],
        "mode": "template",
    }
    assert gateway.repairs == 1


def test_model_chapters_with_control_field_fall_back_without_writing_model_data() -> None:
    class ModelGateway:
        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            assert node_id == "plan_chapters"
            return type(
                "Result",
                (),
                {
                    "status": "succeeded",
                    "data": {
                        "chapters": [
                            {
                                "chapter_id": "chapter-unsafe",
                                "source_refs": ["diary:diary-1"],
                                "connector_id": "untrusted-connector",
                            }
                        ]
                    },
                },
            )()

    state = AgentState(highlights={"source_refs": ["diary:diary-1"]})

    result = MemoirNodeRunner(object(), model_gateway=ModelGateway()).run_node(
        {"node_id": "plan_chapters"}, _run(), state
    )

    assert result == {"node_id": "plan_chapters", "fallback": True}
    assert state.chapter_plan == {
        "chapters": [
            {"chapter_id": "chapter-1", "source_refs": ["diary:diary-1"], "kind": "memory_overview"}
        ]
    }
    assert state.fallback_flags == ["model_invalid_chapters", "template_chapters"]


def test_json_string_model_scenes_are_parsed_before_state_write() -> None:
    class ModelGateway:
        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            assert node_id == "generate_scenes"
            return type(
                "Result",
                (),
                {
                    "status": "succeeded",
                    "data": '{"scenes":[{"scene_id":"scene-1","scene_type":"summary","source_refs":["diary:diary-1"]},{"scene_id":"scene-2","scene_type":"summary","source_refs":[]},{"scene_id":"scene-3","scene_type":"summary","source_refs":[]}]}',
                },
            )()

    state = AgentState(
        chapter_plan={
            "chapters": [
                {"chapter_id": "chapter-1", "source_refs": ["diary:diary-1"], "kind": "memory_overview"}
            ]
        }
    )

    result = MemoirNodeRunner(object(), model_gateway=ModelGateway()).run_node(
        {"node_id": "generate_scenes"}, _run(), state
    )

    assert result == {"node_id": "generate_scenes", "fallback": False}
    assert state.scenes == [
        {"scene_id": "scene-1", "scene_type": "summary", "source_refs": ["diary:diary-1"]},
        {"scene_id": "scene-2", "scene_type": "summary", "source_refs": []},
        {"scene_id": "scene-3", "scene_type": "summary", "source_refs": []},
    ]
