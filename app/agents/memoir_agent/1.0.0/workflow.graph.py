# 这是受信任的静态工作流声明，真正的 LangGraph 执行器将在后续任务读取它。
# 禁止在此文件访问网络、文件系统或业务数据库；这里只描述节点边界。
WORKFLOW_NODES = [
    {"node_id": "load_snapshot", "node_type": "tool", "next_nodes": ["sanitize_materials"]},
    {
        "node_id": "sanitize_materials",
        "node_type": "deterministic",
        "next_nodes": ["compute_stats"],
    },
    {
        "node_id": "compute_stats",
        "node_type": "deterministic",
        "next_nodes": ["extract_highlights"],
    },
    {
        "node_id": "extract_highlights",
        "node_type": "model",
        "prompt_ref": "highlight-extract.v1.md",
        "next_nodes": ["plan_chapters"],
    },
    {
        "node_id": "plan_chapters",
        "node_type": "model",
        "prompt_ref": "chapter-plan.v1.md",
        "next_nodes": ["generate_scenes"],
    },
    {
        "node_id": "generate_scenes",
        "node_type": "model",
        "prompt_ref": "scene-generate.v1.md",
        "next_nodes": ["generate_actions"],
    },
    {
        "node_id": "generate_actions",
        "node_type": "deterministic",
        "next_nodes": ["safety_review"],
    },
    {
        "node_id": "safety_review",
        "node_type": "guardrail",
        "prompt_ref": "safety-review.v1.md",
        "next_nodes": ["publish_document"],
    },
    {
        "node_id": "publish_document",
        "node_type": "tool",
        "next_nodes": ["enqueue_media_tasks"],
    },
    {
        "node_id": "enqueue_media_tasks",
        "node_type": "deterministic",
        "next_nodes": [],
        "optional": True,
    },
]
