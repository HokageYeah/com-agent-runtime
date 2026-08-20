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


def test_extract_highlights_forwards_material_texts_to_gateway() -> None:
    """Phase A：text_digest 素材文本必须随请求进入网关 materials 通道。

    - request["materials"] 携带 {source_ref, text}（真实脱敏细节）；
    - 可观测 context 摘要保持占位符口径，素材正文绝不进入 audit 视图；
    - input 仍是纯 source_refs，形状不变（零契约破坏）。
    """

    class ModelGateway:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            self.requests.append(request)
            return type("Result", (), {"status": "route_not_allowed", "data": None})()

    gateway = ModelGateway()
    state = AgentState(
        snapshot={
            "materials": [
                {
                    "material_type": "diary",
                    "source_ref": "diary:d1",
                    "sanitized_payload": {"id": "d1", "text_digest": "火锅之夜：今晚吃了火锅"},
                },
                {
                    "material_type": "handbook_note",
                    "source_ref": "handbook_note:h1",
                    "sanitized_payload": {"id": "h1", "text_digest": "记得周五一起去看电影"},
                },
            ]
        }
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    runner.run_node({"node_id": "sanitize_materials"}, _run(), state)
    runner.run_node({"node_id": "extract_highlights"}, _run(), state)

    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    # 素材文本进入 materials 通道（模型引用真实细节的唯一来源）。
    assert request["materials"] == [
        {"source_ref": "diary:d1", "text": "火锅之夜：今晚吃了火锅"},
        {"source_ref": "handbook_note:h1", "text": "记得周五一起去看电影"},
    ]
    # input 形状不变：仍是纯 source_refs。
    assert request["input"] == {"source_refs": ["diary:d1", "handbook_note:h1"]}
    # 红线：可观测 context 摘要不含素材正文（占位符口径）。
    assert "火锅" not in str(request["context"])
    assert "看电影" not in str(request["context"])


def test_scene_generation_filters_material_texts_by_chapter_refs() -> None:
    """generate_scenes 只携带章节选中 refs 对应的素材文本，省 token 且不越权。"""

    class ModelGateway:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            self.requests.append(request)
            return type("Result", (), {"status": "route_not_allowed", "data": None})()

    gateway = ModelGateway()
    state = AgentState(
        snapshot={
            "materials": [
                {
                    "material_type": "diary",
                    "source_ref": "diary:d1",
                    "sanitized_payload": {"id": "d1", "text_digest": "火锅之夜"},
                },
                {
                    "material_type": "diary",
                    "source_ref": "diary:d2",
                    "sanitized_payload": {"id": "d2", "text_digest": "未被章节选中的日记"},
                },
                {
                    "material_type": "matured_wish",
                    "source_ref": "matured_wish:w1",
                    "sanitized_payload": {"id": "w1", "text_digest": "一起去看海"},
                },
            ]
        },
        chapter_plan={
            "chapters": [
                {
                    "chapter_id": "chapter-1",
                    "source_refs": ["diary:d1", "matured_wish:w1"],
                    "kind": "memory_overview",
                }
            ]
        },
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    # 先跑 sanitize 让 text_digest 进入脱敏视图，再跑场景节点。
    runner.run_node({"node_id": "sanitize_materials"}, _run(), state)
    runner.run_node({"node_id": "generate_scenes"}, _run(), state)

    assert len(gateway.requests) == 1
    # 未选中的 diary:d2 不进 materials；选中 refs 的文本全量携带。
    assert gateway.requests[0]["materials"] == [
        {"source_ref": "diary:d1", "text": "火锅之夜"},
        {"source_ref": "matured_wish:w1", "text": "一起去看海"},
    ]


def test_repair_receives_material_texts_alongside_candidate() -> None:
    """repair 也必须携带素材文本：否则修复时模型看不到素材，无法纠正引用。"""

    class ModelGateway:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.repairs: list[dict[str, object]] = []

        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            self.calls.append(request)
            # 返回结构化输出但引用未在 allowlist → 触发一次 repair。
            return type(
                "Result",
                (),
                {"status": "succeeded", "data": {"scenes": [
                    {"scene_id": "scene-1", "scene_type": "summary",
                     "source_refs": ["diary:forged"], "body": "x"},
                ]}},
            )()

        def repair(
            self, run_id: str, node_id: str, request: dict[str, object],
            invalid_output: object,
        ) -> object:
            self.repairs.append(request)
            return type("Result", (), {"status": "route_not_allowed", "data": None})()

    gateway = ModelGateway()
    state = AgentState(
        snapshot={
            "materials": [
                {
                    "material_type": "diary",
                    "source_ref": "diary:d1",
                    "sanitized_payload": {"id": "d1", "text_digest": "火锅之夜"},
                },
            ]
        },
        chapter_plan={
            "chapters": [
                {"chapter_id": "chapter-1", "source_refs": ["diary:d1"],
                 "kind": "memory_overview"},
            ]
        },
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    # 先跑 sanitize 让 text_digest 进入脱敏视图，再跑场景节点触发 repair。
    runner.run_node({"node_id": "sanitize_materials"}, _run(), state)
    runner.run_node({"node_id": "generate_scenes"}, _run(), state)

    assert len(gateway.repairs) == 1
    # repair 请求同样携带素材文本（修复时模型需要看到真实素材）。
    assert gateway.repairs[0]["materials"] == [
        {"source_ref": "diary:d1", "text": "火锅之夜"},
    ]


def test_model_scenes_with_rich_scene_types_are_accepted() -> None:
    """B1：六种冻结场景类型（cover/stats/diary_highlight/bet_highlight/milestone/summary）
    的模型输出必须原样通过校验，不再被重写为 summary。词表与业务端发布白名单、
    前端 adapter 白名单三端对齐；image 依赖 Phase D 媒体暂不生成。"""

    class ModelGateway:
        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            return type("Result", (), {"status": "succeeded", "data": {
                "scenes": [
                    {"scene_id": "scene-1", "scene_type": "cover", "source_refs": ["diary:diary-1"]},
                    {"scene_id": "scene-2", "scene_type": "diary_highlight", "source_refs": ["diary:diary-1"]},
                    {"scene_id": "scene-3", "scene_type": "bet_highlight", "source_refs": ["diary:diary-1"]},
                ]
            }})()

    state = AgentState(
        chapter_plan={"chapters": [
            {"chapter_id": "chapter-1", "source_refs": ["diary:diary-1"], "kind": "memory_overview"},
        ]}
    )

    result = MemoirNodeRunner(object(), model_gateway=ModelGateway()).run_node(
        {"node_id": "generate_scenes"}, _run(), state
    )

    assert result == {"node_id": "generate_scenes", "fallback": False}
    # 场景类型必须透传（重写为 summary 会让前端拿到千篇一律的摘要卡）。
    assert state.scenes == [
        {"scene_id": "scene-1", "scene_type": "cover", "source_refs": ["diary:diary-1"]},
        {"scene_id": "scene-2", "scene_type": "diary_highlight", "source_refs": ["diary:diary-1"]},
        {"scene_id": "scene-3", "scene_type": "bet_highlight", "source_refs": ["diary:diary-1"]},
    ]


def test_model_scenes_with_unknown_scene_type_use_template() -> None:
    """词表外的 scene_type 必须整体拒绝并走模板兜底：前端对未知场景类型会把
    整份文档降级成占位卡，比模板兜底损失更大，所以生成端必须守住词表。"""

    class ModelGateway:
        def repair(
            self, run_id: str, node_id: str, request: dict[str, object],
            invalid_output: object,
        ) -> object:
            return type("Result", (), {"status": "route_not_allowed", "data": None})()

        def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
            return type("Result", (), {"status": "succeeded", "data": {
                "scenes": [
                    {"scene_id": "scene-1", "scene_type": "diary_quote", "source_refs": ["diary:diary-1"]},
                    {"scene_id": "scene-2", "scene_type": "summary", "source_refs": []},
                    {"scene_id": "scene-3", "scene_type": "summary", "source_refs": []},
                ]
            }})()

    state = AgentState(
        chapter_plan={"chapters": [
            {"chapter_id": "chapter-1", "source_refs": ["diary:diary-1"], "kind": "memory_overview"},
        ]}
    )

    result = MemoirNodeRunner(object(), model_gateway=ModelGateway()).run_node(
        {"node_id": "generate_scenes"}, _run(), state
    )

    assert result == {"node_id": "generate_scenes", "fallback": True}
    assert state.fallback_flags == ["model_invalid_scenes", "template_scenes"]
    # 模板兜底全部为 summary 安全文案。
    assert [scene["scene_type"] for scene in state.scenes] == ["summary", "summary", "summary"]


def test_generate_actions_maps_scene_types_to_action_types() -> None:
    """B1：动作按 scene_type 确定性映射——日记/赌约精选卡正文用打字机呈现
    （type_text），其余用 show_card；映射冻结在 Runner，不接模型。
    type_text 停留时长按正文长度自适应：len(body)*75+1500 夹在 [3000, 9000]，
    对齐前端 75ms/字打字机节奏（40 字 → 4500ms），短正文不再固定空等 6000ms。"""

    state = AgentState()
    state.apply_tool_output("scenes", [
        {"scene_id": "scene-1", "scene_type": "cover", "source_refs": [], "body": "封面主题"},
        # 40 字正文：40*75+1500=4500ms，落在区间内验证公式本体。
        {"scene_id": "scene-2", "scene_type": "diary_highlight", "source_refs": [], "body": "字" * 40},
        # 无 body 的异常场景：长度按 0 计，命中 3000ms 下限兜底。
        {"scene_id": "scene-3", "scene_type": "bet_highlight", "source_refs": []},
        {"scene_id": "scene-4", "scene_type": "milestone", "source_refs": [], "body": "里程碑"},
    ])

    result = MemoirNodeRunner(object()).run_node({"node_id": "generate_actions"}, _run(), state)

    assert result == {"node_id": "generate_actions", "fallback": True}
    assert state.actions == [
        {"action_id": "action-1", "scene_id": "scene-1", "action_type": "show_card", "duration_ms": 3000},
        {"action_id": "action-2", "scene_id": "scene-2", "action_type": "type_text", "duration_ms": 4500},
        {"action_id": "action-3", "scene_id": "scene-3", "action_type": "type_text", "duration_ms": 3000},
        {"action_id": "action-4", "scene_id": "scene-4", "action_type": "show_card", "duration_ms": 3000},
    ]


def test_is_safe_playback_accepts_frozen_vocab_and_rejects_unknown() -> None:
    """发布边界：六种场景类型与四种动作类型放行；词表外（image/focus_image 等）
    一律拒绝，保证 Runtime 发布的文档永远落在三端冻结契约内。"""

    scenes = [
        {"scene_id": "scene-1", "scene_type": "cover", "source_refs": [], "body": "开场"},
        {"scene_id": "scene-2", "scene_type": "diary_highlight", "source_refs": [], "body": "日记精选"},
        {"scene_id": "scene-3", "scene_type": "bet_highlight", "source_refs": [], "body": "赌约精选"},
    ]
    actions = [
        {"action_id": "action-1", "scene_id": "scene-1", "action_type": "show_card", "duration_ms": 3000},
        {"action_id": "action-2", "scene_id": "scene-2", "action_type": "type_text", "duration_ms": 6000},
        {"action_id": "action-3", "scene_id": "scene-3", "action_type": "type_text", "duration_ms": 6000},
    ]
    assert MemoirNodeRunner._is_safe_playback(scenes, actions) is True

    # 词表外场景类型（image 依赖 Phase D 媒体，不在生成集）。
    bad_scenes = [dict(scenes[0], scene_type="image"), scenes[1], scenes[2]]
    assert MemoirNodeRunner._is_safe_playback(bad_scenes, actions) is False

    # 词表外动作类型（focus_image 为 M4 恒关动作，不在生成集）。
    bad_actions = [dict(actions[0], action_type="focus_image"), actions[1], actions[2]]
    assert MemoirNodeRunner._is_safe_playback(scenes, bad_actions) is False
