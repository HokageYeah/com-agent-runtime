"""M7：memoir Runner bounded_loop 三段接口（begin/iteration/finalize）测试。

mock ModelGateway（不真调模型），用合成 sanitized_material / AgentState 驱动；
三段接口契约见 app/runtime/bounded_loop.py 与 executor 的 bounded_loop 分支。
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from pytest import LogCaptureFixture, raises

from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.runtime.bounded_loop import InheritedLoopBudget
from app.runtime.state import AgentState

# 与 1.0.5 workflow.graph.py 冻结的 bounded_loop 节点保持一致。
LOOP_NODE: dict[str, object] = {
    "node_id": "generate_scene_batches",
    "node_type": "bounded_loop",
    "safe_to_rerun": True,
    "loop_policy": {
        "budget_strategy": "inherit_run_limits_v1",
        "merge_strategy": "append_unique_by_key",
        "merge_key": "scene_id",
        "on_iteration_error": "continue",
        "on_budget_exhausted": "partial",
        "body_node_ids": ["generate_scene_batch"],
    },
}

# Run 级冻结限额导出的循环预算快照（合成值，只关心 remaining_tokens）。
BUDGET = InheritedLoopBudget(
    max_iterations=6,
    remaining_model_calls=6,
    remaining_tokens=100_000,
    remaining_cost=2.0,
    remaining_ms=300_000,
)


class FakeModelGateway:
    """记录调用并按脚本返回模型输出；可选暴露 route 级 context_token_budget。"""

    def __init__(
        self, outputs: list[object] | None = None, token_budget: int | None = None,
    ) -> None:
        self._outputs = list(outputs or [])
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._token_budget = token_budget

    def context_token_budget(self, node_id: str) -> int:
        if self._token_budget is None:
            raise ValueError("MODEL_CONTEXT_BUDGET_UNAVAILABLE")
        return self._token_budget

    def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
        self.calls.append((node_id, request))
        output = self._outputs.pop(0) if self._outputs else None
        if output is None:
            return SimpleNamespace(status="failed", data=None)
        return SimpleNamespace(status="succeeded", data=output)


def _material(ref: str, text: str) -> dict[str, object]:
    """合成脱敏素材：text 为 sanitize 后的 text_digest 派生文本。"""
    return {"source_ref": ref, "type": "diary", "sensitive": False, "text": text}


def _sanitized(*materials: dict[str, object]) -> dict[str, object]:
    return {"materials": list(materials)}


def _run(version: str = "1.0.5") -> object:
    return type("Run", (), {
        "run_id": "loop-run", "agent_id": "memoir_agent", "agent_version": version,
    })()


def _run_106() -> object:
    """1.0.6 Run 替身：批次候选游标 / 首末批硬校验 / required_scene_type 修复。"""
    return _run("1.0.6")


def _scene(
    scene_id: str, scene_type: str, refs: list[str], body: str = "我们在江边散步的具体画面。",
) -> dict[str, object]:
    return {
        "scene_id": scene_id, "scene_type": scene_type,
        "source_refs": list(refs), "body": body,
    }


def _payload(*scenes: dict[str, object]) -> str:
    return json.dumps({"scenes": list(scenes)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 1. begin_loop：正常初始化 / 素材缺失 fail closed
# ---------------------------------------------------------------------------

def test_begin_loop_initializes_without_model_calls() -> None:
    """begin_loop 只初始化循环状态（素材清单 + 游标归零），不产生模型调用。"""
    gateway = FakeModelGateway()
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(
        _material("diary:d1", "第一条素材"), _material("diary:d2", "第二条素材"),
    ))

    assert runner.begin_loop(LOOP_NODE, _run(), state, BUDGET) is None
    assert gateway.calls == []


def test_begin_loop_fails_closed_without_loopable_materials() -> None:
    """素材视图缺失 / 无任何带安全 text 的素材 / 网关不可用 → fail closed。"""
    runner = MemoirNodeRunner(object(), model_gateway=FakeModelGateway())
    # 脱敏视图整体缺失（上游 sanitize 未执行或被跳过）。
    with raises(ValueError, match="LOOP_MATERIALS_MISSING"):
        runner.begin_loop(LOOP_NODE, _run(), AgentState(), BUDGET)
    # 只有 ref-only 敏感素材（无 text_digest），循环没有可驱动素材。
    ref_only = _sanitized({"source_ref": "diary:d1", "type": "diary", "sensitive": True})
    with raises(ValueError, match="LOOP_MATERIALS_MISSING"):
        runner.begin_loop(LOOP_NODE, _run(), AgentState(sanitized_material=ref_only), BUDGET)
    # 无模型网关：循环体每轮都需要一次模型调用，直接 fail closed 不空转。
    with raises(ValueError, match="LOOP_MODEL_GATEWAY_UNAVAILABLE"):
        MemoirNodeRunner(object(), model_gateway=None).begin_loop(
            LOOP_NODE, _run(), AgentState(sanitized_material=_sanitized(
                _material("diary:d1", "第一条素材"),
            )), BUDGET,
        )


# ---------------------------------------------------------------------------
# 2. 单轮一次模型调用 + 批次切分不超 min(context_token_budget, remaining_tokens)
# ---------------------------------------------------------------------------

def _token_materials(count: int, chars: int) -> tuple[dict[str, object], ...]:
    return tuple(
        _material(f"diary:m{index}", "字" * chars) for index in range(1, count + 1)
    )


def _batch_tokens(request: dict[str, object]) -> int:
    """按 Runner 的字符近似口径（4 字符 ≈ 1 token）累加本批素材 token。"""
    return sum(
        (len(str(item["text"])) + 3) // 4
        for item in request.get("materials", [])  # type: ignore[union-attr]
    )


def test_iteration_batches_within_route_budget_and_calls_model_once() -> None:
    """route 窗口 30 token：每轮恰一次调用；批 1 装 m1-m3，批 2 收尾 m4。"""
    gateway = FakeModelGateway(
        outputs=[
            _payload(_scene("s1-1", "cover", ["diary:m1"])),
            _payload(_scene("s2-1", "summary", ["diary:m4"])),
        ],
        token_budget=30,
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(4, 40)))
    run = _run()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    first = runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)

    assert first.outcome == "continue"
    assert len(gateway.calls) == 1  # 单轮至多一次模型调用（usage 差分契约）
    node_id, request = gateway.calls[0]
    assert node_id == "generate_scene_batch"
    assert request["prompt_id"] == "scene-batch-generate"
    assert [item["source_ref"] for item in request["materials"]] == [  # type: ignore[union-attr]
        "diary:m1", "diary:m2", "diary:m3",
    ]
    assert _batch_tokens(request) <= 30
    candidate_input = request["input"]
    assert candidate_input["batch_index"] == 1  # type: ignore[index]
    assert candidate_input["is_first_batch"] is True  # type: ignore[index]
    assert candidate_input["is_final_batch"] is False  # type: ignore[index]

    second = runner.run_loop_iteration(LOOP_NODE, run, state, 2, BUDGET)

    assert second.outcome == "complete"
    assert len(gateway.calls) == 2
    _, second_request = gateway.calls[1]
    assert [item["source_ref"] for item in second_request["materials"]] == ["diary:m4"]  # type: ignore[union-attr]
    assert second_request["input"]["is_final_batch"] is True  # type: ignore[index]


def test_iteration_batches_within_run_remaining_tokens() -> None:
    """Run 剩余 token 更小时以 remaining_tokens 为准（二者取小）。"""
    small = InheritedLoopBudget(
        max_iterations=6, remaining_model_calls=6, remaining_tokens=10,
        remaining_cost=2.0, remaining_ms=300_000,
    )
    gateway = FakeModelGateway(
        outputs=[_payload(_scene("s1-1", "cover", ["diary:m1"]))], token_budget=30,
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(2, 40)))
    run = _run()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    runner.run_loop_iteration(LOOP_NODE, run, state, 1, small)

    _, request = gateway.calls[0]
    # route 窗口 30，但 Run 剩余只有 10 token → 本批只装得下第一条（10 token）。
    assert [item["source_ref"] for item in request["materials"]] == ["diary:m1"]  # type: ignore[union-attr]
    assert _batch_tokens(request) <= 10


def test_iteration_batch_caps_at_eight_materials() -> None:
    """批内素材条数与网关素材通道的 8 条上限对齐（超出会被网关丢弃）。"""
    gateway = FakeModelGateway(outputs=[_payload()], token_budget=4096)
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(9, 20)))
    run = _run()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    result = runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)

    _, request = gateway.calls[0]
    materials = request["materials"]
    assert materials is not None and len(materials) == 8
    assert result.outcome == "continue"


# ---------------------------------------------------------------------------
# 3. 单条素材超限：整条剔除不截断、安全计数、批空时游标推进不死循环
# ---------------------------------------------------------------------------

def test_over_limit_material_dropped_whole_without_truncation() -> None:
    """单条超限整条剔除：绝不出现截断后的 digest 片段，也不进入任何请求。"""
    secret = "超限素材正文哨兵" * 30  # 240 字 ≈ 60 token，远超 20 token 上限
    gateway = FakeModelGateway(
        outputs=[
            _payload(_scene("s1-1", "cover", ["diary:m1"])),
            _payload(_scene("s2-1", "summary", ["diary:m3"])),
        ],
        token_budget=20,
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(
        _material("diary:huge", secret), *_token_materials(3, 40),
    ))
    run = _run()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    first = runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)

    # huge 被整条剔除，本批装 m1+m2（各 10 token 恰好填满 20）；m3 留给下一轮。
    _, request = gateway.calls[0]
    assert [item["source_ref"] for item in request["materials"]] == [  # type: ignore[union-attr]
        "diary:m1", "diary:m2",
    ]
    assert first.outcome == "continue"
    second = runner.run_loop_iteration(LOOP_NODE, run, state, 2, BUDGET)
    assert second.outcome == "complete"
    # 剔除是整条的：超限素材正文从未以任何截断片段进入两次请求。
    for _, captured in gateway.calls:
        assert "diary:huge" not in json.dumps(captured, ensure_ascii=False)
        assert "超限素材正文哨兵" not in json.dumps(captured, ensure_ascii=False)


def test_all_materials_over_limit_advances_cursor_without_deadlock() -> None:
    """剔除后本批为空：该轮不调模型直接 continue，下一轮游标耗尽收敛。"""
    gateway = FakeModelGateway(token_budget=20)
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(
        _material("diary:h1", "长" * 200), _material("diary:h2", "长" * 200),
    ))
    run = _run()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    first = runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)
    assert first.outcome == "continue"
    assert first.reason_code == "LOOP_BATCH_ALL_OVER_LIMIT"
    assert gateway.calls == []  # 空批不产生模型调用

    second = runner.run_loop_iteration(LOOP_NODE, run, state, 2, BUDGET)
    # 游标已推进到耗尽：直接收敛，不会回头重试超限素材（无死循环）。
    assert second.outcome == "complete"
    assert second.reason_code == "LOOP_MATERIALS_EXHAUSTED"
    assert gateway.calls == []


# ---------------------------------------------------------------------------
# 4. 解析失败 → 安全失败迭代结果，正文/原始输出不泄漏
# ---------------------------------------------------------------------------

def test_invalid_output_raises_safe_code_and_advances_cursor(
    caplog: LogCaptureFixture,
) -> None:
    """非 JSON / 结构非法输出：抛受控原因码，正文不进异常与日志，游标前进。"""
    caplog.set_level(logging.INFO)
    gateway = FakeModelGateway(
        outputs=[
            "SENTINEL-RAW-OUTPUT 不是 JSON {{{",  # 第一批解析失败
            _payload(_scene("s2-1", "summary", ["diary:m3"])),
        ],
        token_budget=10,  # 批 1 只装 m1+m2，m3 留给第二轮
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(3, 20)))
    run = _run()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    with raises(RuntimeError, match="LOOP_BATCH_OUTPUT_INVALID"):
        runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)

    # 模型原始输出哨兵不进入异常消息与任何日志行。
    assert "SENTINEL-RAW-OUTPUT" not in caplog.text
    # 该批失败不写场景。
    assert state.scenes is None
    # 游标已推进：第二轮拿到的是第二批素材（m3），不是重试失败批。
    second = runner.run_loop_iteration(LOOP_NODE, run, state, 2, BUDGET)
    _, request = gateway.calls[1]
    assert [item["source_ref"] for item in request["materials"]] == ["diary:m3"]  # type: ignore[union-attr]
    assert second.outcome == "complete"


def test_model_unavailable_iteration_raises_safe_code() -> None:
    """网关不可用（status 非 succeeded / 异常）同样按单轮失败跳过语义处理。"""
    gateway = FakeModelGateway()  # 无脚本输出 → status=failed
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(1, 20)))
    run = _run()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    with raises(RuntimeError, match="LOOP_BATCH_MODEL_UNAVAILABLE"):
        runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)


def test_output_with_fabricated_ref_is_rejected() -> None:
    """模型编造本批之外的 source_ref → 结构非法，整批安全失败。"""
    gateway = FakeModelGateway(
        outputs=[_payload(_scene("s1-1", "cover", ["diary:not-in-batch"]))],
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(1, 20)))
    run = _run()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    with raises(RuntimeError, match="LOOP_BATCH_OUTPUT_INVALID"):
        runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)
    assert state.scenes is None


# ---------------------------------------------------------------------------
# 5. cover/summary 批次语义：首批 cover、末批 summary、中批不得夹带
# ---------------------------------------------------------------------------

def test_middle_batch_with_cover_or_summary_is_rejected() -> None:
    """非首批出现 cover / 非末批出现 summary 都是结构违约，整批拒绝。"""
    runner = MemoirNodeRunner(object(), model_gateway=FakeModelGateway(
        outputs=[_payload(_scene("s2-1", "cover", ["diary:m1"]))],
    ))
    state = AgentState(
        sanitized_material=_sanitized(*_token_materials(2, 20)),
        scenes=[_scene("s1-1", "cover", ["diary:m0"])],  # 已有产出 → 本批非首批
    )
    run = _run()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    with raises(RuntimeError, match="LOOP_BATCH_OUTPUT_INVALID"):
        runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)


def test_first_batch_failure_keeps_first_flag_for_next_batch() -> None:
    """首批解析失败被跳过后，下一批仍可补 cover（is_first 看实际产出）。"""
    gateway = FakeModelGateway(
        outputs=[
            "INVALID{{{",
            _payload(_scene("s2-1", "cover", ["diary:m3"])),
        ],
        token_budget=10,  # 批 1 只装 m1+m2，批 2 收尾 m3
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(3, 20)))
    run = _run()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    with raises(RuntimeError, match="LOOP_BATCH_OUTPUT_INVALID"):
        runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)
    runner.run_loop_iteration(LOOP_NODE, run, state, 2, BUDGET)

    _, request = gateway.calls[1]
    assert request["input"]["is_first_batch"] is True  # type: ignore[index]
    assert request["input"]["is_final_batch"] is True  # type: ignore[index]


def test_valid_batch_scenes_are_appended_with_counts() -> None:
    """合法批次场景按序追加进 state.scenes，返回安全计数。"""
    gateway = FakeModelGateway(outputs=[
        _payload(
            _scene("s1-1", "cover", ["diary:m1"], body="开场封面画面。"),
            _scene("s1-2", "diary_highlight", ["diary:m1", "diary:m1", "diary:m2"]),
        ),
    ], token_budget=10)  # 批 1 只装 m1+m2（m2 留给下一轮），场景未引用不存在的素材
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(3, 20)))
    run = _run()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    result = runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)

    assert result.outcome == "continue"
    assert result.output_count == 2
    assert result.coverage_count == 2  # m1 + m2（引用去重后的真实覆盖）
    assert [scene["scene_id"] for scene in state.scenes or []] == ["s1-1", "s1-2"]
    # source_refs 语义校验通过后去重写入。
    assert state.scenes is not None and state.scenes[1]["source_refs"] == ["diary:m1", "diary:m2"]


# ---------------------------------------------------------------------------
# 6. finalize_loop：结构完整性判定
# ---------------------------------------------------------------------------

def test_finalize_loop_complete_for_valid_structure() -> None:
    """>=3 场景、首 cover、末 summary → complete。"""
    runner = MemoirNodeRunner(object(), model_gateway=FakeModelGateway())
    state = AgentState(scenes=[
        _scene("s1-1", "cover", ["diary:m1"]),
        _scene("s1-2", "diary_highlight", ["diary:m2"]),
        _scene("s2-1", "summary", ["diary:m3"]),
    ])

    result = runner.finalize_loop(LOOP_NODE, _run(), state)

    assert result.outcome == "complete"
    assert result.reason_code == "LOOP_STRUCTURE_COMPLETE"
    assert result.output_count == 3


def test_finalize_loop_failed_for_insufficient_or_misplaced_scenes() -> None:
    """不足 3 / 缺 cover / 缺 summary → failed（带原因码，不补写 Scene）。"""
    runner = MemoirNodeRunner(object(), model_gateway=FakeModelGateway())
    run = _run()

    two = AgentState(scenes=[
        _scene("s1-1", "cover", ["diary:m1"]),
        _scene("s2-1", "summary", ["diary:m2"]),
    ])
    result = runner.finalize_loop(LOOP_NODE, run, two)
    assert result.outcome == "failed"
    assert result.reason_code == "LOOP_SCENE_COUNT_INSUFFICIENT"
    assert len(two.scenes or []) == 2  # finalize 不补写任何 Scene

    no_cover = AgentState(scenes=[
        _scene("s1-1", "diary_highlight", ["diary:m1"]),
        _scene("s1-2", "diary_highlight", ["diary:m2"]),
        _scene("s2-1", "summary", ["diary:m3"]),
    ])
    assert runner.finalize_loop(LOOP_NODE, run, no_cover).reason_code == "LOOP_COVER_MISSING"

    no_summary = AgentState(scenes=[
        _scene("s1-1", "cover", ["diary:m1"]),
        _scene("s1-2", "diary_highlight", ["diary:m2"]),
        _scene("s1-3", "milestone", ["diary:m3"]),
    ])
    assert runner.finalize_loop(LOOP_NODE, run, no_summary).reason_code == "LOOP_SUMMARY_MISSING"

    assert runner.finalize_loop(LOOP_NODE, run, AgentState()).reason_code == (
        "LOOP_SCENE_COUNT_INSUFFICIENT"
    )


# ---------------------------------------------------------------------------
# 6.1 finalize_loop 定向结构修复：缺 cover/summary 时对首批/末批素材再调一次
# ---------------------------------------------------------------------------
# 通用驱动形态：3 条素材 + route 窗口 10 token → 批 1 装 m1+m2、批 2 收尾 m3；
# 末批/首批刻意漏发 cover 或 summary，复现线上"批次全部成功但收尾结构缺口"。

def _drive_two_batches_and_finalize(
    gateway: FakeModelGateway,
) -> tuple[object, AgentState]:
    """跑满两批迭代后执行 finalize，返回 (finalize 结果, state)。"""
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(3, 20)))
    run = _run()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)
    runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)
    runner.run_loop_iteration(LOOP_NODE, run, state, 2, BUDGET)
    return runner.finalize_loop(LOOP_NODE, run, state), state


def test_finalize_repairs_missing_summary_from_final_batch_stash() -> None:
    """末批漏发 summary（线上 run 9a4a79fa 形态）：末批素材定向修复一次后 complete。"""
    gateway = FakeModelGateway(
        outputs=[
            _payload(
                _scene("s1-1", "cover", ["diary:m1"]),
                _scene("s1-2", "diary_highlight", ["diary:m2"]),
            ),
            # 末批只出 body 卡不收尾：解析合法（闸门只管 summary 位置不管在场），
            # 结构缺口留给 finalize 判定。
            _payload(_scene("s2-1", "milestone", ["diary:m3"])),
            # 定向修复输出：末批素材重新生成的收尾卡。
            _payload(_scene("s3-1", "summary", ["diary:m3"])),
        ],
        token_budget=10,
    )

    result, state = _drive_two_batches_and_finalize(gateway)

    assert result.outcome == "complete"
    # 修复调用形状：复用 generate_scene_batch 路由与 prompt；末批素材；
    # is_final_batch=True 触发 prompt 的 summary 居末强制；批次号取既有
    # 最大批次号 +1（s3 命名空间不与 s1/s2 冲突）。
    assert len(gateway.calls) == 3
    node_id, request = gateway.calls[2]
    assert node_id == "generate_scene_batch"
    assert request["prompt_id"] == "scene-batch-generate"
    assert request["input"]["is_final_batch"] is True  # type: ignore[index]
    assert request["input"]["is_first_batch"] is False  # type: ignore[index]
    assert request["input"]["batch_index"] == 3  # type: ignore[index]
    assert [item["source_ref"] for item in request["materials"]] == ["diary:m3"]  # type: ignore[union-attr]
    # 修复只追加收尾卡：原 3 张顺序不动，summary 居末。
    assert [scene["scene_id"] for scene in state.scenes or []] == [
        "s1-1", "s1-2", "s2-1", "s3-1",
    ]
    assert (state.scenes or [])[-1]["scene_type"] == "summary"


def test_finalize_repair_without_target_card_fails_closed() -> None:
    """修复输出仍无 summary → 维持 failed（每缺口至多修复一次，不重试）。"""
    gateway = FakeModelGateway(
        outputs=[
            _payload(
                _scene("s1-1", "cover", ["diary:m1"]),
                _scene("s1-2", "diary_highlight", ["diary:m2"]),
            ),
            _payload(_scene("s2-1", "milestone", ["diary:m3"])),
            # 修复仍只出 body 卡：无目标卡可提取。
            _payload(_scene("s3-1", "milestone", ["diary:m3"])),
        ],
        token_budget=10,
    )

    result, state = _drive_two_batches_and_finalize(gateway)

    assert result.outcome == "failed"
    assert result.reason_code == "LOOP_SUMMARY_MISSING"
    assert len(gateway.calls) == 3  # 修复只调一次
    assert [scene["scene_id"] for scene in state.scenes or []] == [
        "s1-1", "s1-2", "s2-1",
    ]


def test_finalize_repair_takes_only_summary_card_and_drops_extras() -> None:
    """修复输出多卡时只提取 summary 单卡，多余 body 卡丢弃不进文档。"""
    gateway = FakeModelGateway(
        outputs=[
            _payload(
                _scene("s1-1", "cover", ["diary:m1"]),
                _scene("s1-2", "diary_highlight", ["diary:m2"]),
            ),
            _payload(_scene("s2-1", "milestone", ["diary:m3"])),
            # 修复输出 body + summary：闸门保证 summary 居末，只取收尾卡。
            _payload(
                _scene("s3-1", "diary_highlight", ["diary:m3"]),
                _scene("s3-2", "summary", ["diary:m3"]),
            ),
        ],
        token_budget=10,
    )

    result, state = _drive_two_batches_and_finalize(gateway)

    assert result.outcome == "complete"
    assert [scene["scene_id"] for scene in state.scenes or []] == [
        "s1-1", "s1-2", "s2-1", "s3-2",
    ]


def test_finalize_repairs_missing_cover_from_first_batch_stash() -> None:
    """首批漏发 cover：首批素材定向修复一次，cover 前插到文档首位。"""
    gateway = FakeModelGateway(
        outputs=[
            # 首批只出 body 卡（闸门只管 cover 位置不管在场）。
            _payload(
                _scene("s1-1", "diary_highlight", ["diary:m1"]),
                _scene("s1-2", "diary_highlight", ["diary:m2"]),
            ),
            _payload(
                _scene("s2-1", "milestone", ["diary:m3"]),
                _scene("s2-2", "summary", ["diary:m3"]),
            ),
            # 定向修复输出：开场 cover 卡。
            _payload(_scene("s3-1", "cover", ["diary:m1"])),
        ],
        token_budget=10,
    )

    result, state = _drive_two_batches_and_finalize(gateway)

    assert result.outcome == "complete"
    node_id, request = gateway.calls[2]
    assert node_id == "generate_scene_batch"
    assert request["input"]["is_first_batch"] is True  # type: ignore[index]
    assert request["input"]["is_final_batch"] is False  # type: ignore[index]
    assert [item["source_ref"] for item in request["materials"]] == [  # type: ignore[union-attr]
        "diary:m1", "diary:m2",
    ]
    # cover 前插到首位，原有场景顺序整体后移。
    assert [scene["scene_id"] for scene in state.scenes or []] == [
        "s3-1", "s1-1", "s1-2", "s2-1", "s2-2",
    ]


def test_finalize_repair_model_unavailable_fails_closed() -> None:
    """修复调用网关不可用（status=failed）→ 维持 failed 原因码。"""
    gateway = FakeModelGateway(
        outputs=[
            _payload(
                _scene("s1-1", "cover", ["diary:m1"]),
                _scene("s1-2", "diary_highlight", ["diary:m2"]),
            ),
            _payload(_scene("s2-1", "milestone", ["diary:m3"])),
            # 第三次调用无脚本输出 → status=failed：修复不可用。
        ],
        token_budget=10,
    )

    result, _ = _drive_two_batches_and_finalize(gateway)

    assert result.outcome == "failed"
    assert result.reason_code == "LOOP_SUMMARY_MISSING"


def test_finalize_insufficient_scene_count_skips_repair() -> None:
    """场景数不足下限不属于结构缺口：不触发修复模型调用，直接 failed。"""
    gateway = FakeModelGateway()  # 任何调用都会被断言捕获
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(scenes=[
        _scene("s1-1", "cover", ["diary:m1"]),
        _scene("s2-1", "summary", ["diary:m2"]),
    ])

    result = runner.finalize_loop(LOOP_NODE, _run(), state)

    assert result.outcome == "failed"
    assert result.reason_code == "LOOP_SCENE_COUNT_INSUFFICIENT"
    assert gateway.calls == []  # 数量缺口不做修复（素材不足，补卡无据）


# ---------------------------------------------------------------------------
# 7. 旧版本节点分发零影响：新分支只增不改
# ---------------------------------------------------------------------------

def test_run_node_dispatch_unchanged_for_old_and_new_nodes() -> None:
    """未知节点仍 fail closed；循环体节点线性到达时为无副作用透传。"""
    gateway = FakeModelGateway()
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    run = _run()

    # 既有分发语义不变：未实现节点仍抛 MEMOIR_NODE_NOT_IMPLEMENTED。
    with raises(ValueError, match="MEMOIR_NODE_NOT_IMPLEMENTED"):
        runner.run_node({"node_id": "not_a_node"}, run, AgentState())

    # 循环体节点：模型调用已在 bounded_loop 三段内完成，线性遍历到达时透传。
    state = AgentState(scenes=[_scene("s1-1", "cover", ["diary:m1"])])
    assert runner.run_node({"node_id": "generate_scene_batch"}, run, state) == {
        "node_id": "generate_scene_batch", "loop_body": True,
    }
    assert gateway.calls == []  # 透传绝不再次调用模型（单轮一次调用契约）
    assert state.scenes == [_scene("s1-1", "cover", ["diary:m1"])]


def test_iteration_without_begin_loop_fails_closed() -> None:
    """未 begin_loop 直接迭代是契约违约，fail closed 而不是空转。"""
    runner = MemoirNodeRunner(object(), model_gateway=FakeModelGateway())
    with raises(ValueError, match="LOOP_NOT_INITIALIZED"):
        runner.run_loop_iteration(LOOP_NODE, _run(), AgentState(), 1, BUDGET)


# ---------------------------------------------------------------------------
# 8. 1.0.6 批次候选游标：失败批次不消费素材，下一轮同批重试
# ---------------------------------------------------------------------------
# 语义锚点（方案 §2.2）：1.0.6 起 run_loop_iteration 装批用局部候选游标，
# 只有模型调用成功且 _parse_batch_output 通过（含首末批在场硬校验）后才
# 提交 self._loop_cursor；瞬时失败下一轮对同一批素材重试，不再跳批。
# 1.0.5 旧行为（失败即跳批、由 repair 兜底）由第 4/5 节既有测试锁定。


class FlakyModelGateway(FakeModelGateway):
    """前 fail_first 次调用返回 status=failed（网关不可用），其余按脚本输出。"""

    def __init__(
        self, outputs: list[object] | None, fail_first: int,
        token_budget: int | None = None,
    ) -> None:
        super().__init__(outputs=outputs, token_budget=token_budget)
        self._fail_first = fail_first

    def call(self, run_id: str, node_id: str, request: dict[str, object]) -> object:
        if len(self.calls) < self._fail_first:
            self.calls.append((node_id, request))
            return SimpleNamespace(status="failed", data=None)
        return super().call(run_id, node_id, request)


def test_106_invalid_output_keeps_cursor_and_retries_same_batch() -> None:
    """1.0.6：非法 JSON 不消费批次素材，下一轮同批重试成功（游标未前移）。"""
    gateway = FakeModelGateway(
        outputs=[
            "SENTINEL-RAW-OUTPUT 不是 JSON {{{",  # 批 1 首次尝试解析失败
            # 同批重试（batch_index=2 → s2 命名空间）成功提交首批场景。
            _payload(
                _scene("s2-1", "cover", ["diary:m1"]),
                _scene("s2-2", "diary_highlight", ["diary:m2"]),
            ),
            _payload(_scene("s3-1", "summary", ["diary:m3"])),
        ],
        token_budget=10,  # 批 1 只装 m1+m2，m3 留给收尾批
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(3, 20)))
    run = _run_106()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    with raises(RuntimeError, match="LOOP_BATCH_OUTPUT_INVALID"):
        runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)
    # 失败批次不写场景：首批资格保留给重试轮。
    assert state.scenes is None

    second = runner.run_loop_iteration(LOOP_NODE, run, state, 2, BUDGET)
    # 重试拿到的是同一批素材（m1+m2），而不是跳到 m3（1.0.5 旧行为）。
    _, retry_request = gateway.calls[1]
    assert [item["source_ref"] for item in retry_request["materials"]] == [  # type: ignore[union-attr]
        "diary:m1", "diary:m2",
    ]
    assert retry_request["input"]["is_first_batch"] is True  # type: ignore[index]
    assert retry_request["input"]["is_final_batch"] is False  # type: ignore[index]
    assert second.outcome == "continue"

    third = runner.run_loop_iteration(LOOP_NODE, run, state, 3, BUDGET)
    assert third.outcome == "complete"
    _, final_request = gateway.calls[2]
    assert [item["source_ref"] for item in final_request["materials"]] == ["diary:m3"]  # type: ignore[union-attr]
    assert [scene["scene_id"] for scene in state.scenes or []] == [
        "s2-1", "s2-2", "s3-1",
    ]


def test_106_model_unavailable_keeps_cursor_and_retries_same_batch() -> None:
    """1.0.6：网关不可用（status=failed → data None）游标不前移，恢复后同批成功。"""
    gateway = FlakyModelGateway(
        outputs=[_payload(
            _scene("s2-1", "cover", ["diary:m1"]),
            _scene("s2-2", "summary", ["diary:m2"]),
        )],
        fail_first=1,
        token_budget=10,  # m1+m2 同批且即收尾批
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(2, 20)))
    run = _run_106()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    with raises(RuntimeError, match="LOOP_BATCH_MODEL_UNAVAILABLE"):
        runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)
    assert state.scenes is None

    second = runner.run_loop_iteration(LOOP_NODE, run, state, 2, BUDGET)
    # 恢复后仍是同一批素材（m1+m2），一次调用收尾。
    _, retry_request = gateway.calls[1]
    assert [item["source_ref"] for item in retry_request["materials"]] == [  # type: ignore[union-attr]
        "diary:m1", "diary:m2",
    ]
    assert retry_request["input"]["is_final_batch"] is True  # type: ignore[index]
    assert second.outcome == "complete"


def test_106_final_batch_missing_summary_rejected_then_retried() -> None:
    """1.0.6 首末批在场硬校验：收尾批缺 summary 整批拒绝，预算内同批重试成功。"""
    gateway = FakeModelGateway(
        outputs=[
            _payload(
                _scene("s1-1", "cover", ["diary:m1"]),
                _scene("s1-2", "diary_highlight", ["diary:m2"]),
            ),
            # 收尾批只出 body 卡：1.0.5 闸门只管位置不管在场（可提交），
            # 1.0.6 整批拒绝（BATCH_SUMMARY_MISSING）。
            _payload(_scene("s2-1", "milestone", ["diary:m3"])),
            # 同批重试（batch_index=3）带 summary。
            _payload(_scene("s3-1", "summary", ["diary:m3"])),
        ],
        token_budget=10,
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(3, 20)))
    run = _run_106()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    assert runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET).outcome == "continue"
    with raises(RuntimeError, match="LOOP_BATCH_OUTPUT_INVALID"):
        runner.run_loop_iteration(LOOP_NODE, run, state, 2, BUDGET)
    # 缺 summary 的收尾批不提交：场景仍只有首批两张。
    assert [scene["scene_id"] for scene in state.scenes or []] == ["s1-1", "s1-2"]

    third = runner.run_loop_iteration(LOOP_NODE, run, state, 3, BUDGET)
    assert third.outcome == "complete"
    _, retry_request = gateway.calls[2]
    # 重试仍是收尾批素材 m3。
    assert [item["source_ref"] for item in retry_request["materials"]] == ["diary:m3"]  # type: ignore[union-attr]
    assert [scene["scene_id"] for scene in state.scenes or []] == [
        "s1-1", "s1-2", "s3-1",
    ]
    assert (state.scenes or [])[-1]["scene_type"] == "summary"


def test_106_first_batch_missing_cover_rejected_then_retried() -> None:
    """1.0.6 在场硬校验（cover 侧）：首批缺 cover 整批拒绝，重试补 cover。"""
    gateway = FakeModelGateway(
        outputs=[
            # 首批只出 body 卡：1.0.6 整批拒绝（BATCH_COVER_MISSING）。
            _payload(_scene("s1-1", "diary_highlight", ["diary:m1"])),
            _payload(
                _scene("s2-1", "cover", ["diary:m1"]),
                _scene("s2-2", "summary", ["diary:m2"]),
            ),
        ],
        token_budget=10,  # m1+m2 同批且即收尾批
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(2, 20)))
    run = _run_106()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    with raises(RuntimeError, match="LOOP_BATCH_OUTPUT_INVALID"):
        runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)
    assert state.scenes is None

    second = runner.run_loop_iteration(LOOP_NODE, run, state, 2, BUDGET)
    assert second.outcome == "complete"
    _, retry_request = gateway.calls[1]
    assert retry_request["input"]["is_first_batch"] is True  # type: ignore[index]
    assert [scene["scene_id"] for scene in state.scenes or []] == ["s2-1", "s2-2"]


def test_106_all_over_limit_still_advances_cursor_deterministically() -> None:
    """1.0.6 确定性例外：纯剔除空批不调模型，游标照常推进（无重试死循环）。"""
    gateway = FakeModelGateway(token_budget=20)
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(
        _material("diary:h1", "长" * 200), _material("diary:h2", "长" * 200),
    ))
    run = _run_106()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    first = runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)
    assert first.outcome == "continue"
    assert first.reason_code == "LOOP_BATCH_ALL_OVER_LIMIT"
    assert gateway.calls == []
    second = runner.run_loop_iteration(LOOP_NODE, run, state, 2, BUDGET)
    assert second.outcome == "complete"
    assert second.reason_code == "LOOP_MATERIALS_EXHAUSTED"


def test_106_over_limit_drop_rederived_deterministically_on_retry() -> None:
    """候选游标下剔除不落账：失败重试轮重新推导剔除，超限素材绝不进请求。"""
    secret = "超限素材正文哨兵" * 30  # 240 字 ≈ 60 token，远超 20 token 上限
    gateway = FakeModelGateway(
        outputs=[
            "INVALID{{{",  # 批 1 首次尝试失败
            _payload(
                _scene("s2-1", "cover", ["diary:m1"]),
                _scene("s2-2", "diary_highlight", ["diary:m2"]),
            ),
            _payload(_scene("s3-1", "summary", ["diary:m3"])),
        ],
        token_budget=20,
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(
        _material("diary:huge", secret), *_token_materials(3, 40),
    ))
    run = _run_106()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    with raises(RuntimeError, match="LOOP_BATCH_OUTPUT_INVALID"):
        runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)
    runner.run_loop_iteration(LOOP_NODE, run, state, 2, BUDGET)
    runner.run_loop_iteration(LOOP_NODE, run, state, 3, BUDGET)

    # 失败轮与重试轮装批结果一致（huge 剔除被确定性重推导，m1+m2 同批）。
    assert [item["source_ref"] for item in gateway.calls[0][1]["materials"]] == [  # type: ignore[union-attr]
        "diary:m1", "diary:m2",
    ]
    assert [item["source_ref"] for item in gateway.calls[1][1]["materials"]] == [  # type: ignore[union-attr]
        "diary:m1", "diary:m2",
    ]
    for _, captured in gateway.calls:
        assert "diary:huge" not in json.dumps(captured, ensure_ascii=False)
        assert "超限素材正文哨兵" not in json.dumps(captured, ensure_ascii=False)


def test_106_persistent_failures_converge_without_busy_loop() -> None:
    """持续失败网关：每轮同批重试计入迭代上限，收敛到 finalize fail closed。"""
    gateway = FakeModelGateway(outputs=["INVALID{{{"] * 6, token_budget=10)
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(3, 20)))
    run = _run_106()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    # 按 executor 冻结语义复刻驱动：失败轮同样计入迭代上限后跳过继续。
    iterations = 0
    while iterations < BUDGET.max_iterations:
        try:
            runner.run_loop_iteration(LOOP_NODE, run, state, iterations + 1, BUDGET)
            break
        except RuntimeError:
            iterations += 1

    # 6 轮全部失败后循环自然终止（迭代上限兜底，无 busy loop），
    # 每轮都是同一批素材（候选游标从未提交），场景零提交。
    assert iterations == BUDGET.max_iterations
    assert len(gateway.calls) == BUDGET.max_iterations
    assert [item["source_ref"] for item in gateway.calls[0][1]["materials"]] == [  # type: ignore[union-attr]
        "diary:m1", "diary:m2",
    ]
    assert state.scenes is None
    final = runner.finalize_loop(LOOP_NODE, run, state)
    assert final.outcome == "failed"
    assert final.reason_code == "LOOP_SCENE_COUNT_INSUFFICIENT"


# ---------------------------------------------------------------------------
# 8b. 1.0.7 语义继承：预算扩容不改变 1.0.6 批次候选游标行为
# ---------------------------------------------------------------------------
# 1.0.7 只调 agent.yaml 额度（max_model_calls 8→12 等），runner 循环语义按
# agent_version >= 1.0.6 门控对 1.0.7 天然成立；本节锁住这一继承关系，
# 防止后续版本升级时误把 1.0.7 排除在批次重试语义之外。


def test_107_inherits_106_candidate_cursor_retry_semantics() -> None:
    """1.0.7：瞬时失败不消费批次素材，下一轮同批重试成功（与 1.0.6 同形）。

    该场景正是 1.0.6 生产失败（run ab6fcbfc）的缩影：一次瞬时失败烧掉一轮
    迭代；区别在于 1.0.7 的 max_model_calls=12 为这种重试留出了额度。
    """
    gateway = FlakyModelGateway(
        outputs=[
            _payload(
                _scene("s2-1", "cover", ["diary:m1"]),
                _scene("s2-2", "summary", ["diary:m2"]),
            )
        ],
        fail_first=1,
        token_budget=10,  # m1+m2 同批且即收尾批
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(2, 20)))
    run = _run("1.0.7")
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    with raises(RuntimeError, match="LOOP_BATCH_MODEL_UNAVAILABLE"):
        runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)
    assert state.scenes is None

    second = runner.run_loop_iteration(LOOP_NODE, run, state, 2, BUDGET)
    # 恢复后仍是同一批素材（m1+m2），候选游标语义与 1.0.6 完全一致。
    _, retry_request = gateway.calls[1]
    assert [item["source_ref"] for item in retry_request["materials"]] == [  # type: ignore[union-attr]
        "diary:m1", "diary:m2",
    ]
    assert retry_request["input"]["is_final_batch"] is True  # type: ignore[index]
    assert second.outcome == "complete"


# ---------------------------------------------------------------------------
# 9. 1.0.6 定向结构修复：受信任字段 required_scene_type（1.0.5 请求形状不变）
# ---------------------------------------------------------------------------


def test_106_repair_summary_request_carries_required_scene_type() -> None:
    """缺 summary 修复（1.0.6）：请求携带 required_scene_type=summary，单卡收尾。

    驱动形态：5 条素材 + route 窗口 20 token → 批 1（m1+m2）、批 2（m3+m4）、
    批 3（m5 收尾）；收尾批两次瞬时失败耗尽迭代余量后 finalize 触发修复。
    """
    gateway = FakeModelGateway(
        outputs=[
            _payload(
                _scene("s1-1", "cover", ["diary:m1"]),
                _scene("s1-2", "diary_highlight", ["diary:m2"]),
            ),
            _payload(_scene("s2-1", "milestone", ["diary:m3"])),
            "INVALID{{{",  # 收尾批首次失败
            "INVALID{{{",  # 收尾批重试再失败（迭代余量耗尽）
            # 定向修复输出：收尾 summary 单卡（batch_index=3 → s3 命名空间）。
            _payload(_scene("s3-1", "summary", ["diary:m5"])),
        ],
        token_budget=20,
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(5, 40)))
    run = _run_106()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)

    runner.run_loop_iteration(LOOP_NODE, run, state, 1, BUDGET)
    runner.run_loop_iteration(LOOP_NODE, run, state, 2, BUDGET)
    with raises(RuntimeError, match="LOOP_BATCH_OUTPUT_INVALID"):
        runner.run_loop_iteration(LOOP_NODE, run, state, 3, BUDGET)
    with raises(RuntimeError, match="LOOP_BATCH_OUTPUT_INVALID"):
        runner.run_loop_iteration(LOOP_NODE, run, state, 4, BUDGET)

    result = runner.finalize_loop(LOOP_NODE, run, state)

    assert result.outcome == "complete"
    # 修复请求契约：复用 generate_scene_batch 路由 + 受信任 required_scene_type。
    assert len(gateway.calls) == 5
    node_id, request = gateway.calls[4]
    assert node_id == "generate_scene_batch"
    assert request["input"]["required_scene_type"] == "summary"  # type: ignore[index]
    assert request["input"]["is_final_batch"] is True  # type: ignore[index]
    assert request["input"]["batch_index"] == 3  # type: ignore[index]
    assert [item["source_ref"] for item in request["materials"]] == ["diary:m5"]  # type: ignore[union-attr]
    # 修复只追加收尾卡：原 3 张顺序不动，summary 居末。
    assert [scene["scene_id"] for scene in state.scenes or []] == [
        "s1-1", "s1-2", "s2-1", "s3-1",
    ]


def test_106_repair_cover_request_carries_required_scene_type() -> None:
    """缺 cover 修复（1.0.6）：请求携带 required_scene_type=cover，单卡前插。

    1.0.6 在场硬校验下首批缺 cover 无法经正常循环提交（会被整批拒绝），
    该缺口仅在边界路径可达；这里直接置首批快照验证修复请求契约。
    """
    gateway = FakeModelGateway(
        outputs=[_payload(_scene("s3-1", "cover", ["diary:m1"]))],
        token_budget=10,
    )
    runner = MemoirNodeRunner(object(), model_gateway=gateway)
    state = AgentState(sanitized_material=_sanitized(*_token_materials(2, 20)))
    run = _run_106()
    runner.begin_loop(LOOP_NODE, run, state, BUDGET)
    # 手工置首批快照（begin_loop 会重置，须在其后）：修复段唯一消费方。
    runner._loop_first_batch = (
        1,
        [{"source_ref": "diary:m1", "text": "m1"}, {"source_ref": "diary:m2", "text": "m2"}],
        ["diary:m1", "diary:m2"],
    )
    state.scenes = [
        _scene("s1-1", "diary_highlight", ["diary:m1"]),
        _scene("s1-2", "milestone", ["diary:m2"]),
        _scene("s2-1", "summary", ["diary:m2"]),
    ]

    result = runner.finalize_loop(LOOP_NODE, run, state)

    assert result.outcome == "complete"
    node_id, request = gateway.calls[0]
    assert node_id == "generate_scene_batch"
    assert request["input"]["required_scene_type"] == "cover"  # type: ignore[index]
    assert request["input"]["is_first_batch"] is True  # type: ignore[index]
    assert request["input"]["is_final_batch"] is False  # type: ignore[index]
    # 修复只前插开场卡：cover 居首，原 3 张顺序后移。
    assert [scene["scene_id"] for scene in state.scenes or []] == [
        "s3-1", "s1-1", "s1-2", "s2-1",
    ]


def test_repair_request_105_shape_unchanged_without_required_scene_type() -> None:
    """1.0.5 修复请求形状冻结：不携带 required_scene_type（新字段仅 >=1.0.6）。"""
    gateway = FakeModelGateway(
        outputs=[
            _payload(
                _scene("s1-1", "cover", ["diary:m1"]),
                _scene("s1-2", "diary_highlight", ["diary:m2"]),
            ),
            _payload(_scene("s2-1", "milestone", ["diary:m3"])),
            _payload(_scene("s3-1", "summary", ["diary:m3"])),
        ],
        token_budget=10,
    )

    result, _ = _drive_two_batches_and_finalize(gateway)

    assert result.outcome == "complete"
    _, request = gateway.calls[2]
    assert "required_scene_type" not in request["input"]  # type: ignore[index]
