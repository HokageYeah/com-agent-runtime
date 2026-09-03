"""M7：1.0.5 repair_coverage_gaps 覆盖补齐节点测试（C1 回归）。

mock ModelGateway（不真调模型），用合成 stats / sanitized_material / scenes 直接
驱动 run_node；冻结语义见 1.0.5 workflow.graph.py 节点注释与设计说明 §3.3：
只允许一次 ModelGateway repair、输入仅缺失类型的安全 text_digest 与真实
source_ref、修复后仍缺失或无模型许可/预算即 Run failed、禁止模板补写。
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

from pytest import raises

import app.worker as worker
from app.agents.memoir_agent import model_gateway as memoir_model_gateway
from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.runtime import harness_entry
from app.runtime.context_manager import _NODE_TOKEN_CAPS
from app.runtime.state import AgentState

# 与 1.0.5 workflow.graph.py 冻结的模型节点声明保持一致。
REPAIR_NODE: dict[str, object] = {
    "node_id": "repair_coverage_gaps",
    "node_type": "model",
    "prompt_ref": "coverage-repair.v1.md",
    "safe_to_rerun": True,
}


class FakeModelGateway:
    """记录调用并按脚本返回模型输出；无 route 预算入口（走保守回退）。"""

    def __init__(self, outputs: list[object] | None = None) -> None:
        self._outputs = list(outputs or [])
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
        self.calls.append((node_id, request))
        output = self._outputs.pop(0) if self._outputs else None
        if output is None:
            return SimpleNamespace(status="failed", data=None)
        return SimpleNamespace(status="succeeded", data=output)


def _material(ref: str, text: str) -> dict[str, object]:
    """合成脱敏素材：type 从 source_ref 前缀推导，text 为 digest 派生文本。"""
    material_type = ref.split(":", 1)[0]
    return {"source_ref": ref, "type": material_type, "sensitive": False, "text": text}


def _ref_only(ref: str) -> dict[str, object]:
    """无 text_digest 的 ref-only 素材（敏感视图，模型拿不到正文）。"""
    return {"source_ref": ref, "type": ref.split(":", 1)[0], "sensitive": True}


def _sanitized(*materials: dict[str, object]) -> dict[str, object]:
    return {"materials": list(materials)}


def _run() -> object:
    return type("Run", (), {
        "run_id": "repair-run", "agent_id": "memoir_agent", "agent_version": "1.0.5",
    })()


def _scene(
    scene_id: str, scene_type: str, refs: list[str], body: str = "我们在江边散步的具体画面。",
) -> dict[str, object]:
    return {
        "scene_id": scene_id, "scene_type": scene_type,
        "source_refs": list(refs), "body": body,
    }


def _state(
    stats_types: list[str],
    scenes: list[dict[str, object]],
    sanitized: dict[str, object],
) -> AgentState:
    """合成 repair 节点输入：stats.available_material_types + 已生成场景 + 脱敏视图。"""
    return AgentState(
        stats={
            "diary_count": 0, "bet_count": 0, "has_material": True,
            "available_material_types": list(stats_types),
        },
        scenes=list(scenes),
        sanitized_material=sanitized,
    )


# 覆盖完整的典型三场结构：首 cover、末 summary（finalize_loop 已保证）。
_FULL_COVER_SCENES = [
    _scene("s1-1", "cover", ["diary:d1"]),
    _scene("s1-2", "diary_highlight", ["diary:d1"]),
    _scene("s1-3", "summary", ["completed_bet:b1"]),
]


# ---------------------------------------------------------------------------
# 1. 覆盖完整：不调模型直接透传，链路继续 generate_actions
# ---------------------------------------------------------------------------

def test_fully_covered_passes_through_without_model_call() -> None:
    """available_material_types 全部被引用：零模型调用，节点直通完成。"""
    gateway = FakeModelGateway()
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = _state(
        ["diary", "completed_bet"], _FULL_COVER_SCENES,
        _sanitized(_material("diary:d1", "日记文本"), _material("completed_bet:b1", "赌约文本")),
    )

    result = runner.run_node(REPAIR_NODE, _run(), state)

    assert result["node_id"] == "repair_coverage_gaps"  # type: ignore[index]
    assert result["repaired"] is False  # type: ignore[index]
    assert gateway.calls == []
    # 透传不改动已生成场景（含首 cover / 末 summary 结构）。
    assert [scene["scene_id"] for scene in state.scenes] == ["s1-1", "s1-2", "s1-3"]


def test_repair_scene_inserted_before_trailing_summary() -> None:
    """修复场景插在末尾 summary 之前，保持"修复只补中间场景"的播放结构。"""
    gateway = FakeModelGateway(
        outputs=[json.dumps({"scenes": [
            _scene("r1-1", "milestone", ["matured_wish:w1"], "阳台上那盆薄荷真的长起来了。"),
        ]}, ensure_ascii=False)],
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    scenes = [
        _scene("s1-1", "cover", ["diary:d1"]),
        _scene("s1-2", "diary_highlight", ["diary:d1"]),
        _scene("s1-3", "summary", ["diary:d1"]),
    ]
    state = _state(
        ["diary", "matured_wish"], scenes,
        _sanitized(_material("diary:d1", "日记文本"), _material("matured_wish:w1", "心愿文本")),
    )

    result = runner.run_node(REPAIR_NODE, _run(), state)

    assert result["repaired"] is True  # type: ignore[index]
    assert [scene["scene_id"] for scene in state.scenes] == ["s1-1", "s1-2", "r1-1", "s1-3"]
    assert state.scenes[-1]["scene_type"] == "summary"


# ---------------------------------------------------------------------------
# 2. 覆盖缺失：一次受治理模型调用 + 请求契约（只带缺失类型素材）
# ---------------------------------------------------------------------------

def test_repair_calls_model_once_with_missing_type_materials_only() -> None:
    """请求契约：prompt_id=coverage-repair，input 只含缺失类型与对应 refs/materials。"""
    gateway = FakeModelGateway(
        outputs=[json.dumps({"scenes": [
            _scene("r1-1", "bet_highlight", ["completed_bet:b1"], "赌约是谁先跑完五公里。"),
        ]}, ensure_ascii=False)],
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = _state(
        ["diary", "completed_bet"],
        [
            _scene("s1-1", "cover", ["diary:d1"]),
            _scene("s1-2", "diary_highlight", ["diary:d1"]),
            _scene("s1-3", "summary", ["diary:d1"]),
        ],
        _sanitized(
            _material("diary:d1", "日记文本"), _material("diary:d2", "日记文本二"),
            _material("completed_bet:b1", "赌约文本"),
        ),
    )

    result = runner.run_node(REPAIR_NODE, _run(), state)

    assert result["repaired"] is True  # type: ignore[index]
    # 恰一次模型调用（"只允许一次 repair"冻结语义）。
    assert len(gateway.calls) == 1
    node_id, request = gateway.calls[0]
    assert node_id == "repair_coverage_gaps"
    assert request["prompt_id"] == "coverage-repair"  # type: ignore[index]
    candidate_input = request["input"]  # type: ignore[index]
    assert candidate_input["missing_material_types"] == ["completed_bet"]
    # 已覆盖类型（diary）的素材绝不进入修复请求：输入仅为缺失类型的安全文本。
    assert candidate_input["source_refs"] == ["completed_bet:b1"]
    assert [item["source_ref"] for item in request["materials"]] == ["completed_bet:b1"]  # type: ignore[index]
    # 合并后 completed_bet 被真实引用，全类型覆盖。
    covered_types = {ref.split(":", 1)[0] for scene in state.scenes for ref in scene["source_refs"]}
    assert {"diary", "completed_bet"} <= covered_types


def test_repair_materials_round_robin_across_missing_types_capped_at_eight() -> None:
    """多缺失类型交错装填（每类至少一条进入上下文），总量对齐网关 8 条上限。"""
    materials = [
        *(_material(f"diary:d{index}", "日记文本") for index in range(1, 6)),
        *(_material(f"matured_wish:w{index}", "心愿文本") for index in range(1, 6)),
    ]
    gateway = FakeModelGateway(outputs=[json.dumps({"scenes": [
        _scene("r1-1", "diary_highlight", ["diary:d1"], "日记里的具体画面。"),
        _scene("r1-2", "milestone", ["matured_wish:w1"], "心愿实现的具体画面。"),
    ]}, ensure_ascii=False)])
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = _state(
        ["diary", "matured_wish", "completed_bet"],
        [
            _scene("s1-1", "cover", ["completed_bet:b0"]),
            _scene("s1-2", "stats", ["completed_bet:b0"]),
            _scene("s1-3", "summary", ["completed_bet:b0"]),
        ],
        _sanitized(_material("completed_bet:b0", "赌约文本"), *materials),
    )
    # 循环只引用了 completed_bet：diary 与 matured_wish 都是缺失类型。

    runner.run_node(REPAIR_NODE, _run(), state)

    _, request = gateway.calls[0]
    refs = [item["source_ref"] for item in request["materials"]]  # type: ignore[index]
    assert len(refs) == 8
    # 交错装填保证两类都进入模型上下文，而不是被单一类型前 8 条挤占。
    assert any(ref.startswith("diary:") for ref in refs)
    assert any(ref.startswith("matured_wish:") for ref in refs)
    assert set(refs) == set(request["input"]["source_refs"])  # type: ignore[index]


# ---------------------------------------------------------------------------
# 3. fail closed：缺 text_digest 投影 / 无模型许可 / 输出非法 / 修复后仍缺失
# ---------------------------------------------------------------------------

def test_missing_type_without_text_digest_fails_closed() -> None:
    """缺失类型无安全 text_digest 投影：契约错误 fail closed，不调模型不编造。"""
    gateway = FakeModelGateway()
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    # handbook_note 实际存在但只有 ref-only 视图（无 digest），且未被任何场景引用。
    state = _state(
        ["diary", "handbook_note"],
        [
            _scene("s1-1", "cover", ["diary:d1"]),
            _scene("s1-2", "diary_highlight", ["diary:d1"]),
            _scene("s1-3", "summary", ["diary:d1"]),
        ],
        _sanitized(_material("diary:d1", "日记文本"), _ref_only("handbook_note:n1")),
    )

    with raises(ValueError, match="COVERAGE_TEXT_DIGEST_MISSING"):
        runner.run_node(REPAIR_NODE, _run(), state)
    assert gateway.calls == []


def test_model_unavailable_fails_run_instead_of_template() -> None:
    """网关不可用/无剩余许可预算：Run failed，禁止 deterministic 模板补写。"""
    gateway = FakeModelGateway()  # 脚本为空 → status=failed
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = _state(
        ["diary", "completed_bet"],
        [_scene("s1-1", "cover", ["diary:d1"]), _scene("s1-2", "stats", ["diary:d1"]),
         _scene("s1-3", "summary", ["diary:d1"])],
        _sanitized(_material("diary:d1", "日记文本"), _material("completed_bet:b1", "赌约文本")),
    )

    with raises(ValueError, match="COVERAGE_REPAIR_MODEL_UNAVAILABLE"):
        runner.run_node(REPAIR_NODE, _run(), state)
    # 场景保持原样：模板补写被禁止（fail closed 优于编造）。
    assert [scene["scene_id"] for scene in state.scenes] == ["s1-1", "s1-2", "s1-3"]


def test_invalid_repair_output_fails_closed() -> None:
    """输出违反 JSON 契约（非 JSON / scene_id 前缀 / 未知引用 / 类型枚举）→ failed。"""
    runner = MemoirNodeRunner(
        object(), model_gateway=FakeModelGateway(outputs=["SENTINEL-RAW 不是 JSON {{{"]),
    )
    state = _state(
        ["diary", "completed_bet"],
        [_scene("s1-1", "cover", ["diary:d1"]), _scene("s1-2", "stats", ["diary:d1"]),
         _scene("s1-3", "summary", ["diary:d1"])],
        _sanitized(_material("diary:d1", "日记文本"), _material("completed_bet:b1", "赌约文本")),
    )
    # 非 JSON 输出。
    with raises(ValueError, match="COVERAGE_REPAIR_OUTPUT_INVALID"):
        runner.run_node(REPAIR_NODE, _run(), state)


def test_repair_scene_contract_violations_fail_closed() -> None:
    """逐字段契约：r1- 前缀、封闭类型枚举、引用白名单、空数组禁令逐项校验。"""
    base_scenes = [
        _scene("s1-1", "cover", ["diary:d1"]), _scene("s1-2", "stats", ["diary:d1"]),
        _scene("s1-3", "summary", ["diary:d1"]),
    ]
    sanitized = _sanitized(_material("diary:d1", "日记文本"), _material("completed_bet:b1", "赌约文本"))
    cases: list[str] = [
        # scene_id 不带 r1- 前缀（与生成批次 s{batch}- 命名冲突）。
        json.dumps({"scenes": [_scene("x-1", "bet_highlight", ["completed_bet:b1"])]}, ensure_ascii=False),
        # scene_type 越出封闭枚举（cover/summary 由生成批次固定，修复不得生成）。
        json.dumps({"scenes": [_scene("r1-1", "cover", ["completed_bet:b1"])]}, ensure_ascii=False),
        # source_refs 引用白名单外的素材。
        json.dumps({"scenes": [_scene("r1-1", "bet_highlight", ["diary:d999"])]}, ensure_ascii=False),
        # source_refs 空数组。
        json.dumps({"scenes": [_scene("r1-1", "bet_highlight", [])]}, ensure_ascii=False),
        # body 缺失（场景卡不能发布空白正文）。
        json.dumps({"scenes": [{
            "scene_id": "r1-1", "scene_type": "bet_highlight", "source_refs": ["completed_bet:b1"],
        }]}, ensure_ascii=False),
        # title_word 超 6 汉字。
        json.dumps({"scenes": [{
            "scene_id": "r1-1", "scene_type": "bet_highlight", "source_refs": ["completed_bet:b1"],
            "body": "赌约画面正文。", "title_word": "一二三四五六七",
        }]}, ensure_ascii=False),
        # 与已生成场景 scene_id 冲突（覆盖式合并被禁止）。
        json.dumps({"scenes": [_scene("s1-2", "bet_highlight", ["completed_bet:b1"])]}, ensure_ascii=False),
    ]
    for payload in cases:
        runner = MemoirNodeRunner(
            object(), model_gateway=FakeModelGateway(outputs=[payload]),
        )
        state = _state(["diary", "completed_bet"], list(base_scenes), sanitized)
        with raises(ValueError, match="COVERAGE_REPAIR_OUTPUT_INVALID"):
            runner.run_node(REPAIR_NODE, _run(), state)


def test_repair_incomplete_coverage_fails_run() -> None:
    """修复输出合法但未补齐缺失类型（如素材不足以成卡返回空数组）→ Run failed。"""
    gateway = FakeModelGateway(outputs=[json.dumps({"scenes": []}, ensure_ascii=False)])
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = _state(
        ["diary", "completed_bet"],
        [_scene("s1-1", "cover", ["diary:d1"]), _scene("s1-2", "stats", ["diary:d1"]),
         _scene("s1-3", "summary", ["diary:d1"])],
        _sanitized(_material("diary:d1", "日记文本"), _material("completed_bet:b1", "赌约文本")),
    )

    with raises(ValueError, match="COVERAGE_REPAIR_INCOMPLETE"):
        runner.run_node(REPAIR_NODE, _run(), state)
    # 空数组合并后场景表不变。
    assert len(state.scenes) == 3


# ---------------------------------------------------------------------------
# 4. 网关注册面契约：repair_coverage_gaps 与 generate_scene_batch 同级登记
# ---------------------------------------------------------------------------

def test_repair_node_registered_on_all_gateway_surfaces() -> None:
    """四处注册面（prompt 表 / worker 门禁 / harness 路由 / token cap）缺一即失败。"""
    # 1) 模型网关适配器 prompt 表：缺键时 call() 直接 route_not_allowed。
    assert "repair_coverage_gaps" in memoir_model_gateway._PROMPT_REFS
    # 2) Worker 精确门禁基准：键集合不一致会整体禁用模型网关（模板降级）。
    assert "repair_coverage_gaps" in worker._MEMOIR_MODEL_NODES
    # 3) harness_entry 默认路由：provider 模式下与 worker 基准同步列全。
    assert '"repair_coverage_gaps"' in inspect.getsource(harness_entry)
    # 4) ContextManager 节点 token cap：模型节点必须有同级 cap（512）。
    assert _NODE_TOKEN_CAPS.get("repair_coverage_gaps") == 512


# ---------------------------------------------------------------------------
# 5. 1.0.6 兼容：coverage repair 请求契约与 1.0.5 逐字段同形
# ---------------------------------------------------------------------------

def test_repair_request_contract_unchanged_for_1_0_6_run() -> None:
    """1.0.6 Run 的 repair_coverage_gaps 请求契约不变（不携带 required_scene_type）。

    required_scene_type 只属于 generate_scene_batch 的定向结构修复请求
    （_repair_loop_structure，1.0.6 起）；coverage repair（缺失类型补齐）
    的输入契约 missing_material_types / source_refs / materials 在 1.0.6
    保持 1.0.5 原形，修复场景仍插在末尾 summary 之前。
    """
    gateway = FakeModelGateway(
        outputs=[json.dumps({"scenes": [
            _scene("r1-1", "bet_highlight", ["completed_bet:b1"], "赌约是谁先跑完五公里。"),
        ]}, ensure_ascii=False)],
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    run_106 = type("Run", (), {
        "run_id": "repair-run-106", "agent_id": "memoir_agent", "agent_version": "1.0.6",
    })()
    state = _state(
        ["diary", "completed_bet"],
        [
            _scene("s1-1", "cover", ["diary:d1"]),
            _scene("s1-2", "diary_highlight", ["diary:d1"]),
            _scene("s1-3", "summary", ["diary:d1"]),
        ],
        _sanitized(
            _material("diary:d1", "日记文本"),
            _material("completed_bet:b1", "赌约文本"),
        ),
    )

    result = runner.run_node(REPAIR_NODE, run_106, state)

    assert result["repaired"] is True  # type: ignore[index]
    # 恰一次模型调用，请求契约与 1.0.5 逐字段同形。
    assert len(gateway.calls) == 1
    node_id, request = gateway.calls[0]
    assert node_id == "repair_coverage_gaps"
    assert request["prompt_id"] == "coverage-repair"  # type: ignore[index]
    candidate_input = request["input"]  # type: ignore[index]
    assert candidate_input["missing_material_types"] == ["completed_bet"]
    assert candidate_input["source_refs"] == ["completed_bet:b1"]
    assert [item["source_ref"] for item in request["materials"]] == ["completed_bet:b1"]  # type: ignore[index]
    # coverage repair 绝不携带 required_scene_type（该字段只在结构修复请求出现）。
    assert "required_scene_type" not in candidate_input
    # 修复场景插在末尾 summary 之前，1.0.6 合并结构不变。
    assert [scene["scene_id"] for scene in state.scenes] == ["s1-1", "s1-2", "r1-1", "s1-3"]
    assert state.scenes[-1]["scene_type"] == "summary"
