from __future__ import annotations

from pathlib import Path
from shutil import copytree, rmtree

import pytest

from app.schemas.agent_package import WorkflowNodeDefinition
from app.services.agent_package_service import (
    AgentPackageService,
    AgentPackageValidationError,
)

# M7 bounded_loop 冻结契约：唯一预算策略 inherit_run_limits_v1（继承 Run 级额度，
# 缺失/零值 fail closed）；合并策略 append_unique_by_key + merge_key；迭代级错误
# 只允许 continue；额度耗尽允许 partial（部分发布）或 failed；循环体节点由
# body_node_ids 显式引用且只允许 deterministic/model 两类。
FROZEN_LOOP_POLICY = {
    "budget_strategy": "inherit_run_limits_v1",
    "merge_strategy": "append_unique_by_key",
    "merge_key": "scene_id",
    "on_iteration_error": "continue",
    "on_budget_exhausted": "partial",
    "body_node_ids": ["generate_scene_batch"],
}


def test_loads_frozen_memoir_agent_package() -> None:
    package_root = Path(__file__).parents[1] / "app" / "agents"

    package = AgentPackageService(package_root).load("memoir_agent", "1.0.0")

    assert package.agent_id == "memoir_agent"
    assert package.version == "1.0.0"
    assert package.contract_version == "1.0.0"
    assert package.package_digest.startswith("sha256:")
    assert len(package.evals) >= 5
    assert package.tools[0].relative_path.startswith("/")
    enqueue_tts = next(
        tool for tool in package.tools if tool.name == "memory.enqueue_tts"
    )
    assert enqueue_tts.enabled is False
    assert enqueue_tts.side_effect is True
    assert enqueue_tts.cancellation_behavior == "query_after_commit"


def test_rejects_implicit_latest_version() -> None:
    package_root = Path(__file__).parents[1] / "app" / "agents"
    service = AgentPackageService(package_root)

    with pytest.raises(AgentPackageValidationError, match="必须精确指定"):
        service.load("memoir_agent", "latest")


def test_rejects_digest_change_for_same_registered_version(tmp_path: Path) -> None:
    source_root = Path(__file__).parents[1] / "app" / "agents"
    package_root = tmp_path / "agents"
    copytree(source_root, package_root)
    service = AgentPackageService(package_root)

    first = service.load("memoir_agent", "1.0.0")
    prompt = package_root / "memoir_agent" / "1.0.0" / "prompts" / "chapter-plan.v1.md"
    prompt.write_text("版本内容变化", encoding="utf-8")

    with pytest.raises(AgentPackageValidationError, match="digest 不可变"):
        service.load("memoir_agent", "1.0.0")
    assert first.package_digest.startswith("sha256:")


def test_digest_ignores_python_compilation_artifacts(tmp_path: Path) -> None:
    """Python 编译缓存（__pycache__/*.pyc）不得计入 package digest。

    根因回归覆盖：_digest 曾用 rglob("*") 拾取 __pycache__ 编译产物，导致
    同一份源文件在 CI（无 pyc）与本地（有 pyc）算出不同 digest，被误判为
    "同版本 digest 漂移"。编译缓存不是 package 内容，必须稳定排除。
    """
    source_root = Path(__file__).parents[1] / "app" / "agents"
    package_root = tmp_path / "agents"
    copytree(source_root, package_root)
    # 清理 copytree 带过来的编译缓存，建立"无 pyc"干净基线
    for cache in package_root.rglob("__pycache__"):
        rmtree(cache)

    service = AgentPackageService(package_root)
    baseline = service.load("memoir_agent", "1.0.0").package_digest

    # 模拟测试运行后在 package 目录里生成的 .pyc（内容随意，关键是被忽略）
    cache_dir = package_root / "memoir_agent" / "1.0.0" / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "workflow.graph.cpython-313.pyc").write_bytes(b"\x00\x01compiled")

    # 新实例：避开 _remember_digest 的进程内缓存，纯测 _digest 是否稳定排除 pyc
    after = AgentPackageService(package_root).load("memoir_agent", "1.0.0").package_digest
    assert after == baseline, "编译缓存污染了 package digest"


def test_rejects_missing_required_package_file(tmp_path: Path) -> None:
    source_root = Path(__file__).parents[1] / "app" / "agents"
    package_root = tmp_path / "agents"
    copytree(source_root, package_root)
    (package_root / "memoir_agent" / "1.0.0" / "callbacks.yaml").unlink()

    with pytest.raises(AgentPackageValidationError, match="缺少文件"):
        AgentPackageService(package_root).load("memoir_agent", "1.0.0")


def test_package_lifecycle_blocks_revoked_and_new_deprecated_runs() -> None:
    with pytest.raises(AgentPackageValidationError, match="revoked"):
        AgentPackageService.validate_lifecycle("revoked", operation="start")
    with pytest.raises(AgentPackageValidationError, match="deprecated"):
        AgentPackageService.validate_lifecycle("deprecated", operation="create")


def test_memoir_agent_1_0_0_and_1_0_1_are_independent_immutable_packages() -> None:
    """1.0.0 与 1.0.1 必须是两个独立不可变 package（P1 不可变恢复的核心证据）。

    1.0.0 的 workflow.graph.py 在 620f44a 被改动（给 enqueue_media_tasks 补
    safe_to_rerun=False）破坏了不可变性，按不可变 Package 规则不可同 version 改内容，
    故恢复 1.0.0 到缺 safe_to_rerun 的冻结原貌、另发 1.0.1 承载显式分类。两者必须：
    - digest 不同（证明是两个独立 package，非同版本改内容）；
    - 各自能独立 load（证明都是合法 package）；
    - contract_version 都仍是 1.0.0（Tool/Snapshot/Playback 契约不随 Agent 版本升级）。
    """
    package_root = Path(__file__).parents[1] / "app" / "agents"
    service = AgentPackageService(package_root)

    package_100 = service.load("memoir_agent", "1.0.0")
    package_101 = service.load("memoir_agent", "1.0.1")

    assert package_100.version == "1.0.0"
    assert package_101.version == "1.0.1"
    # digest 独立是“另发新版本而非同版本改内容”的硬证据。
    assert package_100.package_digest != package_101.package_digest
    assert package_100.package_digest.startswith("sha256:")
    assert package_101.package_digest.startswith("sha256:")
    # contract_version 不随 Agent 版本升级（P1 第7条铁律）。
    assert package_100.contract_version == "1.0.0"
    assert package_101.contract_version == "1.0.0"


def test_bounded_loop_schema_accepts_frozen_policy() -> None:
    """bounded_loop 节点必须携带冻结 loop_policy，schema 接受该组合。

    M7 铁律：仅 node_type="bounded_loop" 可声明 loop_policy；策略取值全部
    Literal 冻结，不接受任何自由字符串；body_node_ids 非空。
    """
    node = WorkflowNodeDefinition.model_validate(
        {
            "node_id": "generate_scene_batches",
            "node_type": "bounded_loop",
            "safe_to_rerun": True,
            "loop_policy": FROZEN_LOOP_POLICY,
        }
    )

    assert node.node_type == "bounded_loop"
    assert node.loop_policy is not None
    assert node.loop_policy.budget_strategy == "inherit_run_limits_v1"
    assert node.loop_policy.merge_strategy == "append_unique_by_key"
    assert node.loop_policy.merge_key == "scene_id"
    assert node.loop_policy.on_iteration_error == "continue"
    assert node.loop_policy.on_budget_exhausted == "partial"
    assert node.loop_policy.body_node_ids == ["generate_scene_batch"]


@pytest.mark.parametrize(
    "payload",
    [
        # loop_policy 绝不允许出现在非 bounded_loop 节点上。
        {
            "node_id": "generate_scenes",
            "node_type": "model",
            "safe_to_rerun": False,
            "loop_policy": FROZEN_LOOP_POLICY,
        },
        # bounded_loop 缺 loop_policy 必须拒绝：循环额度语义无法静态审计。
        {
            "node_id": "generate_scene_batches",
            "node_type": "bounded_loop",
            "safe_to_rerun": False,
        },
        # 未知预算策略拒绝：不允许 Package 自带任意预算语义。
        {
            "node_id": "generate_scene_batches",
            "node_type": "bounded_loop",
            "safe_to_rerun": False,
            "loop_policy": {**FROZEN_LOOP_POLICY, "budget_strategy": "unlimited"},
        },
        # 循环体引用为空拒绝：空循环体没有可审计语义。
        {
            "node_id": "generate_scene_batches",
            "node_type": "bounded_loop",
            "safe_to_rerun": False,
            "loop_policy": {**FROZEN_LOOP_POLICY, "body_node_ids": []},
        },
        # 非 safe-to-rerun 拒绝：循环中间产物不落 checkpoint，崩溃后必须整节点
        # 重算，False 会让 resume 跳过半途循环，违反"崩溃后重算"契约。
        {
            "node_id": "generate_scene_batches",
            "node_type": "bounded_loop",
            "safe_to_rerun": False,
            "loop_policy": FROZEN_LOOP_POLICY,
        },
    ],
)
def test_bounded_loop_schema_rejects_out_of_contract_policy(payload: dict) -> None:
    """越界 loop_policy 组合一律 fail closed，不接受静默降级。"""
    with pytest.raises(ValueError):
        WorkflowNodeDefinition.model_validate(payload)


def test_loads_memoir_agent_1_0_5_with_frozen_bounded_loop_dag() -> None:
    """1.0.5 是独立不可变包：bounded_loop DAG + 冻结策略 + digest 独立。

    M7 动态生成：1.0.5 引入受控 bounded_loop 节点（场景批量生成），
    必须满足：唯一 bounded_loop 节点、策略逐字段冻结、与 1.0.0–1.0.4
    digest 全部不同、contract_version 仍为 1.0.0。
    """
    package_root = Path(__file__).parents[1] / "app" / "agents"
    service = AgentPackageService(package_root)

    package = service.load("memoir_agent", "1.0.5")

    loop_nodes = [
        node for node in package.workflow_nodes if node.node_type == "bounded_loop"
    ]
    assert len(loop_nodes) == 1
    loop_node = loop_nodes[0]
    assert loop_node.loop_policy is not None
    assert loop_node.loop_policy.budget_strategy == "inherit_run_limits_v1"
    assert loop_node.loop_policy.merge_strategy == "append_unique_by_key"
    assert loop_node.loop_policy.merge_key == "scene_id"
    assert loop_node.loop_policy.on_iteration_error == "continue"
    assert loop_node.loop_policy.on_budget_exhausted in {"partial", "failed"}
    # 循环体只能引用 deterministic/model 节点。
    node_types = {node.node_id: node.node_type for node in package.workflow_nodes}
    for body_id in loop_node.loop_policy.body_node_ids:
        assert node_types[body_id] in {"deterministic", "model"}
    # digest 与历史版本全部不同，且契约版本不随 Agent 版本升级。
    for version in ("1.0.0", "1.0.1", "1.0.2", "1.0.3", "1.0.4"):
        assert package.package_digest != service.load("memoir_agent", version).package_digest
    assert package.contract_version == "1.0.0"
