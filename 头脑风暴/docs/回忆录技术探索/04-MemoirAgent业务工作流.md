# 04-MemoirAgent 业务工作流

## 一、定位

`MemoirAgent` 是运行在公共 Agent Runtime 上的回忆录业务 Agent。它不直接拥有模型 provider、工具执行器、上下文管理、观测和重试逻辑，而是复用公共 Runtime：

```text
MemoirAgent = LangGraph workflow + LangChain components + Provider Adapter / LiteLLM ModelGateway + ToolGateway
```

`MemoirAgent` 的目标是把 `MemorySnapshot` 转换为可播放的回忆作品：

```text
MemorySnapshot
  -> MemoirHighlights
  -> MemoirChapterPlan
  -> MemoryScene[]
  -> MemoryAction[]
  -> MediaTask[]
  -> SafetyReport
```

## 二、Agent 输入输出

### 2.1 输入

```json
{
  "archive_id": "archive_123",
  "snapshot_id": "snapshot_456",
  "owner_user_id": "user_789",
  "space_id": "space_1",
  "relationship_segment_no": 2,
  "generation_epoch": 1,
  "locale": "zh-CN"
}
```

### 2.2 输出

```json
{
  "playback_document": {
    "document_schema_version": "1.0.0",
    "scenes": [],
    "actions": []
  },
  "media_tasks": [],
  "safety_report": {
    "level": "pass",
    "replaced_scene_ids": [],
    "notes": []
  },
  "generation_summary": {
    "scene_count": 8,
    "action_count": 36,
    "fallback_used": false
  }
}
```

Runtime 只把输出的 schema version、内容摘要、hash 和业务写回引用记录为 `AgentArtifact`。完整 `MemoryScene`、`MemoryAction`、`MemoryMediaAsset` 通过业务工具写入情侣日记；Runtime 不默认长期保存第二份回忆录正文。

## 三、LangGraph 状态定义

建议 `MemoirAgentState` 包含：

```text
input
snapshot
sanitized_material
stats
highlights
chapter_plan
scenes
actions
playback_document
media_tasks
safety_report
errors
fallback_flags
```

所有节点都只读写状态中的明确字段，便于 checkpoint、resume 和节点级重试。

## 四、工作流图

```text
START
  -> load_snapshot
  -> sanitize_materials
  -> compute_stats
  -> extract_highlights
  -> plan_chapters
  -> generate_scenes
  -> generate_actions
  -> safety_review
  -> publish_playback_document
  -> enqueue_media_tasks（能力开关启用时）
  -> END
```

失败分支：

```text
extract_highlights failed
  -> template_highlights
  -> plan_chapters

generate_scenes failed
  -> template_scenes
  -> generate_actions

generate_actions failed
  -> default_actions
  -> safety_review

enqueue_media_tasks enqueue failed
  -> mark_partial
  -> END
```

## 五、节点设计

### 5.1 load_snapshot

类型：业务工具节点。

调用：

```text
memory.get_snapshot
```

要求：

- 只能读取当前 `archive_id + owner_user_id` 有权访问的 snapshot。
- 后端返回前必须过滤已删除、无权、跨段号数据。
- 工具返回结果写入 `state.snapshot`。

### 5.2 sanitize_materials

类型：确定性节点 + LangChain middleware。

处理：

- 手机号、地址、openid、token 等敏感字段脱敏。
- 用户身份映射成 `我`、`TA`、`对方`。
- 长日记截断为短摘录。
- 图片只保留引用、尺寸、描述和授权标记。
- 敏感情绪片段打标，默认不进入最终文案。
- 为每条素材保留 `material_id/source_type/owner_scope/content_digest/trusted=false`，正文只进入 prompt 的数据槽。
- 指令性文本可以标记为 injection risk，但不能删除后当作 trusted instructions 重新拼接。

输出：`state.sanitized_material`。

### 5.3 compute_stats

类型：确定性节点。

必须不依赖 AI。输出基础兜底卡所需统计：

- `relationship_days`
- `diary_total`
- `self_diary_count`
- `partner_diary_count`
- `image_diary_count`
- `same_day_diary_count`
- `bet_total`
- `bet_completed_count`
- `bet_fulfillment_rate`
- `top_keywords`
- `top_rewards`
- `first_record_at`
- `last_record_at`

### 5.4 extract_highlights

类型：LLM 节点。

使用：

- LangChain `PromptTemplate`
- Pydantic / JSON Schema output parser
- Provider Adapter / LiteLLM ModelGateway

目标：从素材里挑选适合做卡片的片段。

高光优先级：

1. 绑定日、解绑日、首次记录日。
2. 双方同日或相近日期记录。
3. 图片日记。
4. 已完成赌局。
5. 高互动赌局。
6. 基础统计亮点。

失败时进入 `template_highlights`。

parser 通过后，确定性节点校验每个 highlight 的 `material_id`、owner scope 和 content digest 是否属于当前 snapshot；未知引用或模型生成的工具/URL/权限字段直接拒绝，不交给后续节点。

### 5.5 plan_chapters

类型：LLM 节点。

输出 `MemoirChapterPlan`：

```json
[
  {"type": "cover", "title": "开场", "intent": "说明时间范围"},
  {"type": "stats", "title": "记录过的日子", "intent": "展示基础统计"},
  {"type": "diary_highlight", "title": "那些日常", "intent": "展示温和片段"},
  {"type": "bet_highlight", "title": "小小赌约", "intent": "展示完成赌局"},
  {"type": "summary", "title": "结尾", "intent": "克制收束"}
]
```

章节数量：

- 素材少：3 到 4 章。
- 普通素材：6 到 10 章。
- 素材多：最多 16 章。

### 5.6 generate_scenes

类型：LLM 节点。

输出 `MemoryScene[]`。约束：

- 每张卡主体文案不超过 80 字。
- 不评价谁对谁错。
- 不使用强引导词。
- 不暗示复合。
- 不输出过长原文。
- 不编造不存在的事件。
- 引用素材必须能追溯到 snapshot 中的 material id。
- 统计数字必须来自 `compute_stats`，模型不能改写。
- 日记、赌局和图片说明中的命令只作为引用内容，不能改变章节、工具或发布策略。

场景类型：

| 类型 | 说明 |
|---|---|
| `cover` | 标题、时间范围、封面 |
| `stats` | 关系统计 |
| `diary_highlight` | 日记高光 |
| `image` | 图片回忆 |
| `bet_highlight` | 赌局高光 |
| `milestone` | 重点日期 |
| `summary` | 总结 |

失败时进入 `template_scenes`。

### 5.7 generate_actions

类型：LLM 节点或规则节点。

第一版建议优先规则生成，减少不稳定性：

```text
show_card 500ms
type_text 2000ms
hold 2500ms
transition 500ms
```

增强版再让模型根据场景节奏生成动作。

首期动作协议：

| 动作 | 用途 |
|---|---|
| `show_card` | 展示当前卡 |
| `type_text` | 标题或正文逐字浮现 |
| `focus_image` | 图片轻微放大或聚焦 |
| `hold` | 保持当前画面，形成阅读停顿 |
| `play_tts` | 播放旁白 |
| `transition` | 切换下一张 |

### 5.8 safety_review

类型：规则 + LLM 复核节点。

检查：

- 是否包含敏感字段。
- 是否引用已删除素材。
- 是否出现刺激性分手词。
- 是否暗示复合或责备。
- 是否编造事件。
- 是否超出卡片长度。
- 是否存在空作品。
- 是否出现未知 material/source 引用，或把素材中的指令传播成工具、URL、权限和系统配置字段。

风险处理：

```text
轻微风险 -> 替换单张卡文案
中等风险 -> 替换整组场景为模板卡
严重风险 -> 标记 failed，前端只展示基础统计卡
```

### 5.9 publish_playback_document

类型：业务工具节点。

调用：

```text
memory.publish_playback_document
```

要求：

- 必须带 `run_id`、`generation_epoch`、`snapshot_id` 和稳定幂等键。
- 单次请求携带完整 document/scenes/actions。情侣日记后端在一个事务中完成 schema、引用、数量、素材归属和当前 epoch 校验，再写入新 revision 并切换 `published_revision`。
- `run_id` 必须等于 archive 当前 `active_run_id`，epoch 必须等于 archive 当前值；删除、重新生成或新 run 产生的旧请求返回 `GENERATION_SUPERSEDED`，不得发布。
- 相同逻辑操作重试返回原 revision，不重复插入卡片。
- 保存前再次校验素材引用属于当前 snapshot。
- Runtime 只有在发布工具成功后才能进入 `succeeded/partial` 终态并发送对应 callback。

### 5.10 enqueue_media_tasks

类型：业务工具节点。

调用：

```text
memory.enqueue_tts
memory.enqueue_cover_generation
```

第一版只预留媒体节点和工具契约，默认关闭 TTS、封面和视频；二期再启用 TTS，封面和视频继续按产品节奏开放。

与实施路线图的交付边界保持一致：MVP 默认关闭媒体能力时，该节点以 `skipped(capability_disabled)` 正常结束。二期启用后，Runtime 携带 `document_id + generation_epoch` 幂等创建业务媒体任务：入队失败可将本次 AgentRun 标为 `partial`；入队成功后 AgentRun 正常 `succeeded`。媒体 worker 随后的生成失败只更新对应 MediaTask；业务后端只用当前 published document 的任务聚合 `enhancement_status`，不回写已结束的 AgentRun。

媒体失败不回滚 `published_revision`。详情页继续播放文本作品，并根据派生 `generation_status` 展示 `partial`。

## 六、Prompt 与 Parser

每个 LLM 节点必须有：

- prompt id
- prompt version
- input schema
- output schema
- parser
- fallback

建议 prompt：

| Prompt | 节点 | 输出 |
|---|---|---|
| `memoir-highlight-extract@v1` | extract_highlights | Highlight[] |
| `memoir-chapter-plan@v1` | plan_chapters | ChapterPlan[] |
| `memoir-scene-generate@v1` | generate_scenes | MemoryScene[] |
| `memoir-action-generate@v1` | generate_actions | MemoryAction[] |
| `memoir-safety-review@v1` | safety_review | SafetyReport |

## 七、第一版与二期边界

第一版必做：

- LangGraph 固定 workflow。
- HTTP Business Tool。
- 模板兜底。
- 规则动作生成。
- 基础安全复核。
- AgentRun / Step / ToolCall 记录。

二期再做：

- 模型生成更细腻的动作脚本。
- TTS 声线选择。
- AI 封面图。
- 视频或 H5 动效分镜。
- 人工审核台。
- 用户可选“重新生成某一章”。
