# 这是受信任的静态工作流声明，真正的 LangGraph 执行器将在后续任务读取它。
# 禁止在此文件访问网络、文件系统或业务数据库；这里只描述节点边界。
#
# safe_to_rerun 语义（R2 分类恢复）：memoir 的读取/内容/发布节点都声明 True，
# 因为 R2 checkpoint 不存正文——resume 时 state 为空，必须从 load_snapshot 重新
# 读 Snapshot、内容节点重算、publish_document 走 query-after-commit（logical_key）
# 幂等。这些节点要么无副作用（deterministic/model/guardrail 内容计算），要么由
# 真实 MemoirNodeRunner + ToolCallAuditService 保证发布不双发。enqueue_media_tasks
# 是 optional 后处理，保持默认 False：已完成则 resume 跳过（不重复提交媒体任务），
# 未完成（partial 失败）才执行，符合 partial 只重做未完成 optional 的语义。
WORKFLOW_NODES = [
    {
        "node_id": "load_snapshot",
        "node_type": "tool",
        "next_nodes": ["sanitize_materials"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "sanitize_materials",
        "node_type": "deterministic",
        "next_nodes": ["compute_stats"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "compute_stats",
        "node_type": "deterministic",
        "next_nodes": ["extract_highlights"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "extract_highlights",
        "node_type": "model",
        "prompt_ref": "highlight-extract.v1.md",
        "next_nodes": ["plan_chapters"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "plan_chapters",
        "node_type": "model",
        "prompt_ref": "chapter-plan.v1.md",
        "next_nodes": ["generate_scenes"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "generate_scenes",
        "node_type": "model",
        "prompt_ref": "scene-generate.v1.md",
        "next_nodes": ["generate_actions"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "generate_actions",
        "node_type": "deterministic",
        "next_nodes": ["safety_review"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "safety_review",
        "node_type": "guardrail",
        "prompt_ref": "safety-review.v1.md",
        "next_nodes": ["publish_document"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "publish_document",
        "node_type": "tool",
        "next_nodes": ["enqueue_media_tasks"],
        # 发布节点声明 safe_to_rerun=True：resume 时重访，但真实 runner 先按稳定
        # logical_key 查是否已提交（find_publish_attempt），已提交则 get_publish_result
        # 对账、不重发 publish_playback_document 写请求——即使模型重算让文档 digest 漂移。
        "safe_to_rerun": True,
    },
    {
        "node_id": "enqueue_media_tasks",
        "node_type": "deterministic",
        "next_nodes": [],
        "optional": True,
    },
]
