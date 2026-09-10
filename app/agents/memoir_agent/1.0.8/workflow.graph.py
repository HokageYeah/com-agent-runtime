# 这是受信任的静态工作流声明，LangGraph 执行器读取它驱动节点边界。
# 禁止在此文件访问网络、文件系统或业务数据库；这里只描述节点边界。
#
# 1.0.8 图结构（M8 语音与配乐）：在 1.0.6/1.0.7 十节点 DAG 的
# safety_review 与 publish_document 之间插入 enqueue_audio_tasks，形成
# 十一节点 DAG；其余节点与 1.0.7 逐字段一致，循环语义（批次候选游标 /
# 首末批在场硬校验 / required_scene_type 修复）由 runner 按
# agent_version >= 1.0.6 门控继承。音频节点只读审核后的最终正文，
# 生成结果只作为发布文档 audio 条目，不回改任何 Scene。
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
        # 1.0.6 批次重试语义（runner 门控，图结构不变）：单批瞬时失败（网关
        # 不可用/输出非法/首末批缺卡）不消费该批素材，下一轮同批重试；失败轮
        # 同样计入 max_iterations，预算耗尽仍 partial 收尾收敛（无 busy loop）；
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
        # 双重保证）；无任何场景数量上限。1.0.6 输出校验升级为在场硬校验：
        # 首批缺 cover / 末批缺 summary 整批拒绝同批重试；结构修复请求
        # 携带 required_scene_type 直说缺口类型。safe_to_rerun=True 与内容节点一致。
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
        # 1.0.8 起审核通过后先进入音频节点再发布：审核后的 scene.body 即最终
        # 正文（音频只读正文，绝不回改），发布文档 schema_version=2.0.0。
        "node_type": "guardrail",
        "prompt_ref": "safety-review.v1.md",
        "next_nodes": ["enqueue_audio_tasks"],
        "safe_to_rerun": True,
    },
    {
        "node_id": "enqueue_audio_tasks",
        # M8 音频节点（仅 1.0.8 图存在）：safety_review 之后、publish_document
        # 之前，对最终正文逐场景合成旁白 MP3 并为作品提交一首 60s BGM，全部
        # 走 Memoir 专属服务（TTS/音乐 provider + 私有 OSS + 音频作业账本），
        # 不引入公共 Memoir 节点类型、不启用历史 memory.enqueue_tts 工具。
        # 节点绝不抛异常：任一音频失败按场景有界降级（缺旁白/无 BGM 仍发布
        # 完整图文），失败不另起补音 revision。时间预算受
        # min(音频节点 300s, Run 剩余 - 发布预留 30s) 约束，预留耗尽立即停止
        # 新提交、保留已成功资源后进入发布。safe_to_rerun=True：音频资产键与
        # 费用预留均由作业账本按 (run, epoch, 输入 HMAC) 幂等对账，崩溃恢复
        # 重算不会重复扣费；已上传成功资产同 Run 同输入直接复用。
        "node_type": "deterministic",
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
