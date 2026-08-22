# 这是受信任的静态工作流声明，真正的 LangGraph 执行器在后续任务读取它。
# 禁止在此文件访问网络、文件系统或业务数据库；这里只描述节点边界。
#
# 1.0.3 图重排（M6 媒体通道）：enqueue_media_tasks 从"发布后 optional 后处理"
# 前移到 generate_actions 与 safety_review 之间、publish 前同步执行——图片必须
# 先于发布生成（manifest 进 playback_document 一起过安全审核与发布）。
# 包策略禁止 optional 节点出现在唯一 publish_document 之前，故该节点改为非
# optional 的确定性节点；节点内部永不抛异常（单张失败降级文本卡），不会造成
# partial。safe_to_rerun=True 与其余内容节点一致：R2 checkpoint 不存正文，
# resume 时整链重算；图片按张配额（SUM(image_count)）会拦住重复计费，resume
# 中配额已满的 image 场景按约定降级为文本卡，publish 幂等保证不双发。
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
        "next_nodes": ["enqueue_media_tasks"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "enqueue_media_tasks",
        # M6 媒体节点：逐张生成 image 场景配图（文生图/受门禁的图生图）、上传
        # OSS 公共读、产出六键 media_manifest；失败场景降级 summary 文本卡。
        # node_type 用 tool：节点真实副作用是外部图像 API + OSS 上传，不属于
        # 纯确定性计算，也不接模型 prompt。
        "node_type": "tool",
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
        "next_nodes": [],
        # 发布节点声明 safe_to_rerun=True：resume 时重访，但真实 runner 先按稳定
        # logical_key 查是否已提交（find_publish_attempt），已提交则 get_publish_result
        # 对账、不重发 publish_playback_document 写请求——即使模型重算让文档 digest 漂移。
        "safe_to_rerun": True,
    },
]
