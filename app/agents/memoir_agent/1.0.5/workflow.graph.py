# 这是受信任的静态工作流声明，LangGraph 执行器读取它驱动节点边界。
# 禁止在此文件访问网络、文件系统或业务数据库；这里只描述节点边界。
#
# 1.0.5 图结构（M7 动态生成）：线性 DAG 十节点。相对 1.0.2 移除单次裁剪链
# extract_highlights（全局前 N 条高光）与 plan_chapters（固定 1~3 章），改为
# bounded_loop generate_scene_batches 分批驱动循环体 generate_scene_batch，
# 每批一次模型调用、由模型按素材丰富度动态决定场景数量与组织方式；
# 覆盖缺失由唯一一次 repair 模型节点 repair_coverage_gaps 补齐。
#
# safe_to_rerun 语义（R2 分类恢复 + M7 循环重算）：memoir 的读取/内容/媒体/发布节点
# 都声明 True——R2 checkpoint 不存正文，resume 时 state 为空，必须从 load_snapshot
# 重新读 Snapshot、内容与媒体节点整链重算、publish_document 走 query-after-commit
# （logical_key）幂等。bounded_loop 首版铁律 safe_to_rerun=True：循环中途不写
# checkpoint，Worker 崩溃/接管/retry/resume 后从节点起点按冻结排序完整重算，
# 整节点重算而非续跑。媒体节点在最终安全审核前完成，失败只降级为纯文字卡，
# 再由 safety_review 组装最终文档并交 publish_document 发布。
WORKFLOW_NODES = [
    {
        "node_id": "load_snapshot",
        "node_type": "tool",
        "next_nodes": ["sanitize_materials"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "sanitize_materials",
        # 五类素材（diary/completed_bet/handbook_note/matured_wish/
        # bucket_list_completion）统一走 Phase A 素材通道：materials ->
        # untrusted_items content 键。任一素材缺安全 text_digest 时该素材
        # fail closed 丢弃，不得虚构摘要进入模型。
        "node_type": "deterministic",
        "next_nodes": ["compute_stats"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "compute_stats",
        # 只统计素材中真实出现的数字，并产出 available_material_types
        # （实际存在的合格素材类型集合，覆盖判定与缺失修复的输入）。
        "node_type": "deterministic",
        "next_nodes": ["generate_scene_batches"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "generate_scene_batches",
        # M7 受控循环：按五类固定类型顺序交错成批（批内保持 Snapshot 冻结顺序），
        # 每批驱动一次循环体 generate_scene_batch 模型调用；批切片由上下文与
        # Run 剩余额度计算，不是总素材上限。预算继承 Run 级冻结限额
        # （inherit_run_limits_v1，缺失/零值由 executor fail closed）；
        # 迭代产物按 scene_id 去重追加（重复 key 拒绝，不允许模型覆盖已验证批次）；
        # 单批错误只跳过该批继续（结构错误最多一次既有 repair）；
        # 额度耗尽进入 partial 收尾判定，不直接伪造成功。
        "node_type": "bounded_loop",
        "next_nodes": ["generate_scene_batch"],
        # 铁律：循环节点必须 safe_to_rerun=True——循环中途无 checkpoint，
        # 崩溃/接管/resume 后从节点起点整节点重算（见文件头注释）。
        "safe_to_rerun": True,
        "loop_policy": {
            "budget_strategy": "inherit_run_limits_v1",
            "merge_strategy": "append_unique_by_key",
            "merge_key": "scene_id",
            "on_iteration_error": "continue",
            "on_budget_exhausted": "partial",
            "body_node_ids": ["generate_scene_batch"],
        },
    },
    {
        "node_id": "generate_scene_batch",
        # 循环体（bounded_loop 唯一 body 引用）：为本批素材生成场景卡。
        # 中间场景按时间/主题/事件动态组织，禁止固定「日记时光/赌约回顾」章节名；
        # 首批必须以 cover 开场、收尾批必须以 summary 收尾（prompt + 输出校验
        # 双重保证）；无任何场景数量上限。safe_to_rerun=True 与内容节点一致。
        "node_type": "model",
        "prompt_ref": "scene-batch-generate.v1.md",
        "next_nodes": ["repair_coverage_gaps"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "repair_coverage_gaps",
        # 覆盖缺失收尾：available_material_types 中存在类型未被任何已生成 Scene
        # 的 source_refs 引用时，只允许这一次 repair 模型调用补齐——经相同
        # ModelGateway、预算与 guardrail 治理，输入仅为缺失类型的安全 text_digest
        # 与真实 source_ref；无剩余模型许可/预算，或修复后仍缺失，Run 即 failed。
        # 禁止 deterministic 模板补写 Scene（fail closed 优于编造内容）。
        "node_type": "model",
        "prompt_ref": "coverage-repair.v1.md",
        "next_nodes": ["generate_actions"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "generate_actions",
        # 播放动作（show_card/type_text/hold/transition）与最终 Scene 一一对应，
        # 修复后新增的场景同样在此收口，保证媒体与 safety_review 时 actions 对齐。
        "node_type": "deterministic",
        "next_nodes": ["enqueue_media_tasks"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "enqueue_media_tasks",
        # 最终安全审核前逐场景尝试配图，单场景失败只降级为文本卡并记
        # media_degraded 安全计数，不阻塞后续 safety_review 或发布。
        "node_type": "deterministic",
        "next_nodes": ["safety_review"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "safety_review",
        # 统一内容安全审核：覆盖循环生成、修复补齐与媒体降级后的全部 Scene。
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
        # 对账、不重发 publish_playback_document 写请求——即使循环重算让文档 digest 漂移。
        "safe_to_rerun": True,
    },
]
