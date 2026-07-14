# OpenMAIC 项目整体说明

本文档基于当前仓库代码结构整理，用于快速理解 OpenMAIC 的技术栈、架构设计、课堂材料生产流程、页面/互动内容生产流程、语音生成机制和主要扩展点。

## 1. 项目定位

OpenMAIC（Open Multi-Agent Interactive Classroom）是一个 AI 互动课堂生成与播放平台。用户输入一个主题，或上传 PDF 学习材料后，系统可以生成完整课堂：

- 课程大纲与场景列表
- 幻灯片、测验、互动 HTML、PBL 项目式学习场景
- AI 教师/学生智能体配置
- 讲课动作脚本，包括语音、白板、激光笔、聚光灯、讨论、互动组件操作
- 图片、视频、TTS 音频等媒体资源
- 可播放课堂页面、可编辑幻灯片、可导出的 PPTX 或课堂 ZIP

它不是单纯的 PPT 生成器，而是一个“课堂运行时 + AI 生成流水线 + 多智能体教学脚本”的组合系统。

## 2. 技术栈

### 前端与应用框架

- Next.js 16 App Router：`app/`
- React 19：页面、课堂播放器、编辑器、设置面板
- TypeScript：全项目主语言
- Tailwind CSS 4：全局样式与组件样式
- shadcn/radix/base-ui/lucide-react：基础 UI、弹窗、图标
- motion：页面与课堂模式切换动画
- Zustand：客户端状态管理，核心 store 在 `lib/store/`
- Dexie/IndexedDB：客户端课堂、音频、媒体、智能体等持久化

### AI 与生成

- Vercel AI SDK `ai`：统一 LLM 文本生成与流式生成
- `@ai-sdk/openai`、`@ai-sdk/anthropic`、`@ai-sdk/google`：原生模型适配
- 自定义 provider 层：`lib/ai/providers.ts`
- 统一 LLM 调用层：`lib/ai/llm.ts`
- 分阶段模型路由：`lib/server/model-routes.ts`、`lib/server/resolve-model.ts`
- LangGraph/LangChain：用于课堂讨论 Director 图等编排；编辑态 AI Agent 则使用 `@earendil-works/pi-agent-core`
- assistant-ui：编辑态 Agent 对话、推理块、工具调用卡片的 UI Runtime
- 文件化 Prompt 系统：`lib/prompts/`

### 内容与媒体

- PDF 解析：`unpdf`、`sharp`，可选 MinerU/MinerU Cloud
- TTS：OpenAI、Azure、GLM、Qwen、VoxCPM、MiniMax、豆包、ElevenLabs、Lemonade、浏览器原生 TTS
- ASR：OpenAI Whisper、浏览器原生、Qwen、Azure、Lemonade
- 图片/视频生成：OpenAI、Qwen、MiniMax、Grok、Kling、Veo、Seedream、Seedance 等 provider adapter
- PPTX 导出：workspace 内 fork/封装的 `pptxgenjs`
- 公式：KaTeX、MathML/OMML 转换

### Monorepo/SDK

项目是 pnpm workspace：

- 主应用：根目录 Next.js 应用
- `packages/@openmaic/dsl`：纯类型契约层，定义 Stage/Scene/Slide/Action
- `packages/@openmaic/renderer`：基于 DSL 的幻灯片渲染器
- `packages/@openmaic/importer`：PPTX 导入相关能力
- `packages/pptxgenjs`：PPTX 导出依赖
- `packages/mathml2omml`：公式导出到 Office 格式
- `packages/docs`：文档站

## 3. 整体架构设计思想

### 3.1 契约层与业务层分离

`@openmaic/dsl` 是无运行时依赖的契约核心，负责定义：

- `Stage`：一堂课
- `Scene`：一页/一个场景
- `SlideContent`、`QuizContent`
- `Action`：课堂播放动作，如 speech、whiteboard、spotlight、laser、discussion、widget 操作

主应用在这个基础上扩展业务内容：

- interactive HTML 场景
- PBL 场景
- 多智能体配置
- 生成、编辑、导入导出、播放控制

这种设计让渲染器、导入器、导出器和运行时共享同一份课堂结构和动作协议。

### 3.2 两阶段内容生成

核心生成管线是“两阶段”：

1. Requirements/PDF/Web Search -> Scene Outlines
2. Scene Outline -> Scene Content -> Scene Actions

大纲阶段只决定“这堂课拆成哪些场景，每个场景是什么类型、讲什么、需要什么媒体”。内容阶段再分别生成 slide、quiz、interactive、PBL 的实际内容。动作阶段根据内容生成讲课脚本。

这样做的好处是：

- 先有课程结构，便于用户审阅
- 不同场景类型可以走不同 prompt 和模型路由
- 页面内容与讲课动作解耦
- 生成失败可以按场景重试
- 可在内容生成前并行启动媒体生成

### 3.3 统一 Action 播放协议

课堂不是静态页面。每个 `Scene` 都带 `actions`，播放引擎按顺序执行：

- `speech`：讲解语音
- `spotlight` / `laser`：强调幻灯片元素
- `wb_*`：白板绘制文本、图形、公式、表格、代码等
- `play_video`：播放视频元素
- `discussion`：触发实时讨论
- `widget_*`：控制互动 iframe 内部组件

运行时核心在：

- `lib/playback/engine.ts`
- `lib/action/engine.ts`
- `components/stage.tsx`

### 3.4 生成管线容错与降级

代码里有明显的“尽量生成出可用课堂”的容错策略：

- 大纲缺字段时补 ID、order、languageDirective
- 普通 interactive 同时缺少 legacy `interactiveConfig` 和 `widgetType + widgetOutline` 时降级为 slide；未获准启用的 `procedural-skill` 会被清理专用字段并降级为 diagram
- PBL 配置不完整或模型不可用时降级
- Web Search 失败不阻断课堂生成
- 图片/视频后台生成失败通常只记录 warn，不阻断页面与动作生成
- 客户端非浏览器 TTS 属于场景提交门槛：TTS 失败会把场景标记失败并暂停，等待重试；服务端批量媒体/TTS 则采用尽力而为策略
- 场景 content/actions 生成支持 retry
- 客户端生成支持暂停、续跑、单场景重试

### 3.5 服务端一键生成与客户端渐进生成并存

项目里有两条主要生产路径：

- 服务端一键生成课堂：`POST /api/generate-classroom`
- 客户端预览/渐进生成：`/generation-preview` + `/api/generate/scene-outlines-stream` + `/api/generate/scene-content` + `/api/generate/scene-actions`

服务端路径适合 API、托管生成、直接得到可分享链接。客户端路径适合更好的交互体验：大纲流式展示、用户审阅、智能体揭示、逐场景进入课堂。两条路径共享 outline/content/action 生成内核，但模型配置来源、持久化位置、失败边界和 TTS 处理方式并不完全相同，不能把它们理解成同一流程的两个 UI。

## 4. 主要目录结构

```text
app/
  page.tsx                         首页输入与生成入口
  generation-preview/              生成预览、大纲 SSE、审阅、进入课堂
  classroom/[id]/page.tsx          课堂播放/编辑页面
  api/                             Next.js API routes

components/
  stage.tsx                        课堂运行容器，切换 playback/edit
  stage/                           场景侧栏、渲染器、控制区
  slide-renderer/                  主应用内幻灯片编辑/渲染组件
  scene-renderers/                 互动 iframe 等场景渲染
  generation/                      生成工具栏、大纲编辑等
  settings/                        模型、TTS、ASR、PDF、媒体 provider 设置
  audio/                           语音按钮和 TTS 配置 UI

lib/
  ai/                              统一 LLM/provider/thinking 配置
  generation/                      大纲、场景内容、动作生成管线
  prompts/                         文件化 prompt 模板与 snippets
  server/                          服务端课堂生成、持久化、provider 配置
  audio/                           TTS/ASR provider、声音注册、VoxCPM
  media/                           图片/视频生成 provider 与编排
  pdf/, document/                  PDF/文档解析
  playback/, action/               播放状态机和 Action 执行器
  store/                           Zustand stores
  export/, import/                 PPTX/课堂 ZIP 导入导出
  pbl/                             PBL v1/v2 项目式学习引擎
  orchestration/                   多智能体运行时、director、上下文摘要

packages/
  @openmaic/dsl                    Stage/Scene/Action/Slide 契约
  @openmaic/renderer               独立幻灯片渲染器
  @openmaic/importer               PPTX 导入能力
  docs                             文档站
```

## 5. 课堂材料生产流程

### 5.1 首页输入

入口在 `app/page.tsx`。用户可以：

- 输入自由文本需求
- 上传 PDF
- 开启 Web Search
- 开启 Interactive Mode
- 使用语音输入
- 配置模型、PDF、TTS、ASR、图片、视频 provider

点击生成后，首页会把 `generationSession` 写入 `sessionStorage`，然后跳转到 `/generation-preview`。

### 5.2 PDF/文档解析

PDF 解析接口是 `app/api/parse-pdf/route.ts`。

流程：

1. 接收 multipart PDF
2. 根据设置选择 PDF provider，默认 `unpdf`
3. 进入文档抽取边界 `lib/document/extract.ts`
4. PDF provider 在 `lib/pdf/pdf-providers.ts`
5. 输出统一结构：文本、图片、页数、图片映射等

默认 unpdf 路径会：

- 提取全文
- 提取每页图片
- 用 sharp 转成 PNG base64
- 生成 `img_1`、`img_2` 这样的图片 ID
- 返回 `imageMapping` 与 `pdfImages`

MinerU 路径用于更强的 OCR、表格、公式、版面解析。

#### 5.2.1 PDF 为什么先存 IndexedDB

首页选择 PDF 时不会立即把整份文件转成 base64 塞进 `sessionStorage`。`storePdfBlob` 先把原始 File 作为 Blob 写入 IndexedDB `imageFiles`，生成 `pdf_<id>` storage key；`generationSession` 只保存这个 key、文件名和当时选择的 PDF provider 配置，然后跳转 generation-preview。

预览页恢复 Blob，重新包装成带文件名和 `application/pdf` MIME 的 File，再提交 `/api/parse-pdf`。解析成功后会清除 `pdfStorageKey`，避免页面刷新重复解析收费服务。这个设计解决了两个问题：

- sessionStorage 容量通常只有约 5 MiB，不适合 PDF 和页面图片。
- 首页与 generation-preview 是两个路由，原始 File 对象不能直接序列化跨页面传递。

#### 5.2.2 Document Extractor 边界

`/api/parse-pdf` 不直接依赖具体解析器，而是：

```text
multipart File
  -> DocumentExtractorInput(buffer/fileName/fileSize/mimeType/config)
  -> selectDocumentExtractorProvider
  -> PDF-backed extractor
  -> ParsedPdfContent
  -> DocumentArtifact
  -> documentArtifactToParsedPdfContent（兼容现有调用方）
```

Extractor registry 根据 MIME、preferred provider 和可选 required capabilities 选择实现。每个 provider 声明 text/images/tables/formulas/layout/ocr/async 能力；指定 provider 不存在或不满足能力时直接报错，而不是静默换成另一个解析器。

当前 registry 只有 PDF-backed providers，但 Document Artifact 层把上游“文档抽取”与下游“大纲/页面生成”隔开，未来增加 DOCX、网页或其他文档格式时，下游不必理解每种解析 API。

#### 5.2.3 三种 PDF Provider

- `unpdf`：本地 Node 解析，无 API key；一次 DocumentProxy 同时提取合并文本和逐页图片。每张原始图用 sharp 转 PNG，单图转换或单页提取失败只记录日志并继续。
- `mineru`：连接自托管 `/file_parse`，使用 multipart 上传，开启 markdown、content list、images、公式和表格，并选择 `hybrid-auto-engine`；可选 Bearer key。
- `mineru-cloud`：调用 MinerU Cloud v4。先创建 batch 获取预签名上传 URL，再 PUT PDF，每 2.5 秒轮询 batch，完成后下载 ZIP，读取 `full.md`、`content_list.json` 和 images。总轮询上限 15 分钟，batch/upload/poll/ZIP 各有独立超时，对网络超时类错误做有限重试。

MinerU self-hosted 与 cloud 最终共享 `extractMinerUResult`：把 Markdown 作为正文，从 content list 恢复 page index、bbox 和 caption，把不同文件名统一成 `img_N`，生成 `imageMapping` 与 `pdfImages`。表格和公式主要保留在 Markdown/结构化文本中，不是单独进入 Slide 的专用表格对象。

#### 5.2.4 Managed Provider 与安全

PDF route 的 providerId 默认是 `unpdf`。如果 provider 由服务端托管，route 忽略表单里的 apiKey/baseUrl，使用服务器 registry；非 managed provider 才接受客户端配置。生产环境客户端自定义 baseUrl 需要通过 SSRF 校验。

解析错误统一返回 `PARSE_FAILED`，不会自动换用 unpdf。这样可以避免用户明确选择 OCR/公式解析后，远程服务失败却悄悄生成一份质量较低、看似成功的材料。

#### 5.2.5 文本与图片如何进入生成模型

解析后的正文在客户端先截断到 50,000 字符，并显示截断提示。图片不会继续以 base64 留在 session：

1. `storeImages` 把每张 data URL 转 Blob，以 `session_<sessionId>_<imgId>` 写入 IndexedDB；
2. `pdfImages[]` 只保存 id、页码、描述、宽高和 storageId，`src` 清空；
3. 请求 outline/content 前由 `loadImageMapping` 临时恢复 `img_N -> data URL`；
4. 场景生成续跑也能从 storageId 重建同一映射。

大纲模型支持 vision 时，最多前 20 张有真实 src 的图片作为视觉 content parts，其余图片以及没有 src 的图片只以页码、尺寸、caption 等文本描述进入 prompt；非视觉模型全部使用文本描述。这个上限控制请求体和视觉 token 成本，但所有图片元数据仍可用于 outline 选择 `suggestedImageIds`。

生成 Slide 时，模型只引用 `img_N`。后处理再用 imageMapping 替换成真实 data URL；因此模型不会在 JSON 中复制大段 base64，也不会把 PDF 内图与 `gen_img_*` 生成图片混淆。

### 5.3 大纲生成

大纲生成有两种入口：

- 非流式函数：`lib/generation/outline-generator.ts`
- SSE 接口：`app/api/generate/scene-outlines-stream/route.ts`

输入包括：

- 用户需求
- PDF 文本
- PDF 图片描述/视觉图片
- Web Search 结果
- 用户画像
- 是否启用图片/视频生成
- 是否启用 Interactive Mode 或 Task Engine Mode

使用的 prompt：

- 普通课堂：`requirements-to-outlines`
- 互动优先：`interactive-outlines`
- 职业/任务引擎：`task-engine-outlines`

大纲输出包含：

- `languageDirective`
- `courseTitle`
- `outlines[]`

每个 outline 可能是：

- `slide`
- `quiz`
- `interactive`
- `pbl`

并可能带：

- `keyPoints`
- `suggestedImageIds`
- `mediaGenerations`
- `quizConfig`
- `widgetType/widgetOutline`
- `pblConfig`

### 5.4 大纲审阅

`app/generation-preview/page.tsx` 会流式接收 outline，并支持进入大纲编辑器 `components/generation/outlines-editor.tsx`。如果用户没有开启审阅，默认会短暂展示后自动继续。

这个环节的意义是：先确认课程结构，再进入成本更高的页面、动作、媒体、语音生成。

### 5.5 智能体生成

智能体有两种模式：

- 默认 preset agents：`lib/orchestration/registry/store.ts`
- 自动生成 agents：`app/api/generate/agent-profiles/route.ts`

自动生成会根据课程标题、大纲、语言、可用头像和可用声音，生成教师/助教/学生角色，并保存到 IndexedDB 或嵌入服务端课堂 JSON。

## 6. 页面生产流程

页面生产主要在 `lib/generation/scene-generator.ts`。

### 6.1 Slide 页面

函数：`generateSlideContent`

使用 prompt：

- `slide-content/system.md`
- `slide-content/user.md`

输入：

- outline 标题、描述、知识点
- PDF 图片或生成媒体占位符
- 画布尺寸 1000 x 562.5
- 教师 persona
- 语言指令

输出是 PPTist/OpenMAIC DSL 风格的 slide elements：

- text
- image
- video
- shape
- chart
- latex
- line
- table 等

后处理包括：

- 修复缺省字段
- 修正图片宽高比
- KaTeX 渲染 latex
- 把 `img_1` 等 PDF 图片 ID 替换成真实 base64
- 保留 `gen_img_*` / `gen_vid_*` 生成媒体占位符
- 给元素补唯一 ID

### 6.2 Quiz 页面

函数：`generateQuizContent`

使用 prompt：

- `quiz-content`

输出：

- single/multiple/short_answer 题目
- options
- answer
- analysis
- points

后处理会规范化选项和答案格式。

### 6.3 Interactive HTML 页面

函数：`generateWidgetContent`

根据 `widgetType` 分发到不同 prompt：

- simulation：`simulation-content`
- diagram：`diagram-content`
- code：`code-content`
- game：`game-content`
- visualization3d：`visualization3d-content`
- procedural-skill：`procedural-skill-content`

LLM 输出完整 HTML。系统通过 `extractHtml` 提取 HTML，再经过 `postProcessInteractiveHtml`：

- 保护 script 标签
- 转换 LaTeX 分隔符
- 注入 KaTeX CSS/JS/auto-render
- 添加 MutationObserver，保证动态内容也能渲染公式

互动场景最终作为 iframe 运行，课堂动作可以通过 `widget_highlight`、`widget_setState`、`widget_annotation`、`widget_reveal` 给 iframe 发消息。

### 6.4 PBL 页面

函数：`generatePBLSceneContent`

PBL v2 优先，入口在：

- `lib/pbl/v2/agents/planner-single-call.ts`
- `lib/pbl/v2/agents/planner.ts`

设计思想：

- 先由 outline 阶段决定是否需要 PBL
- PBL planner 生成完整项目结构
- 包含 milestones、microtasks、roles、proficiency、scenario 等
- 返回 v2 payload，同时转换成 legacy `projectConfig` 兼容旧渲染路径

如果 v2 失败，普通 PBL 可降级到 v1；场景角色扮演类 PBL 不降级，因为 v1 表达不了该结构。

### 6.5 动作脚本生成

页面内容生成后，系统再调用 `generateSceneActions`。

使用 prompt：

- slide：`slide-actions`
- quiz：`quiz-actions`
- interactive：`interactive-actions`
- pbl：`pbl-actions`

动作脚本根据实际内容生成，例如：

- 先 speech 讲解
- spotlight 某个元素
- laser 指向公式
- 打开白板推导
- 播放视频
- 触发讨论
- 操控互动组件

如果 LLM 没生成有效动作，会走默认动作兜底。

## 7. 语音生成实现

### 7.1 TTS provider 架构

核心文件：

- `lib/audio/types.ts`
- `lib/audio/constants.ts`
- `lib/audio/tts-providers.ts`

统一入口：

```ts
generateTTS(config, text)
```

支持 provider：

- OpenAI TTS
- Azure TTS
- GLM TTS
- Qwen TTS
- VoxCPM TTS
- MiniMax TTS
- Doubao TTS
- ElevenLabs TTS
- Lemonade TTS
- Browser Native TTS
- custom OpenAI-compatible TTS

每个 provider 都有：

- provider id
- 是否需要 API key
- baseUrl
- model 列表
- voice 列表
- format
- speedRange

### 7.2 客户端 TTS 路径

客户端渐进生成路径在 `lib/hooks/use-scene-generator.ts`：

1. 场景 actions 生成完成
2. 找出 speech actions
3. `splitLongSpeechActions` 拆分长文本
4. 为每个 speech action 生成 `tts_s{sceneOrder}_{action.id}`
5. 调用 `POST /api/generate/tts`
6. API 返回 base64 音频
7. 客户端转 Blob
8. 存入 IndexedDB 的 `audioFiles`

播放时 `ActionEngine.executeSpeech` 通过 `AudioPlayer` 播放 `audioId` 或 `audioUrl`。

### 7.3 服务端批量 TTS 路径

服务端一键生成课堂路径在 `lib/server/classroom-media-generation.ts`：

1. `generateTTSForClassroom(scenes, classroomId, baseUrl)`
2. 遍历所有 scene actions
3. 拆分长 speech
4. 调用共享 `generateTTS`
5. 写入 `data/classrooms/{classroomId}/audio`
6. 给 speech action 写回 `audioId` 和 `audioUrl`

服务端批量 TTS 会跳过 `browser-native-tts`，因为浏览器原生语音只能在客户端执行。

### 7.4 VoxCPM 音色

VoxCPM 支持多种后端：

- vLLM-Omni
- Python API
- Nano-vLLM

代码在 `lib/audio/voxcpm.ts`、`lib/audio/voice-registration.ts`、`lib/audio/voice-resolver.ts` 等文件。自动音色需要智能体上下文；如果服务端批量生成没有足够上下文，会跳过 VoxCPM Auto Voice。

### 7.5 智能体音色选择

语音系统不是只读取一个全局 `ttsVoice`。讲课旁白和实时讨论会先确定“谁在说话”，再通过 `resolveAgentVoice` 选择 provider/model/voice：

```text
用户为该 agent 保存的 voice override
  -> agent profile 自带的 voiceConfig
  -> 在当前已启用 provider 中按 agentIndex 确定性分配 voice
  -> 没有任何可用 provider 时返回 null，跳过 TTS
```

候选 voice 必须属于仍处于 enabled/configured 状态的 provider。浏览器原生 voice 来自运行时 `speechSynthesis.getVoices()`，不能使用服务端静态 voice registry；它只在客户端可选列表中追加。讲课旁白优先选择带 `voiceDesign` 的 teacher，避免默认 teacher 抢先匹配导致生成教师的音色描述失效。

所有语音入口应共享同一套 agent voice 解析：课堂旁白传 teacher，讨论传当前发言 agent，预览和设置测试也使用相同 provider options，避免“预览一种声音、正式讲课另一种声音”。

### 7.6 VoiceDesign 与自动音色注册

自动音色不是每句话临时随机生成 prompt。Agent profile 可以携带 provider-neutral 的 `VoiceDesign`：

- `identity`：性别、年龄、角色等身份特征。
- `texture`：音高、音质和声音质感。
- `delivery`：情绪、速度和表达方式。

`getDeterministicVoiceId` 使用 provider、三段设计和 model 计算 SHA-256，并取稳定前缀作为 `auto-*` voiceId。同一个 agent/设计/provider/model 会得到同一个 ID，使不同场景和多次生成可以复用同一音色。

注册链路为：

```text
resolveAgentVoiceOptions
  -> 检查 provider 是否支持 VoiceRegistrationAdapter
  -> POST /api/generate/voice
     -> voiceExists(voiceId)
        -> 已存在：直接复用
        -> 后端丢失但客户端有缓存 reference clip：重新注册
        -> 首次使用：按 VoiceDesign 合成参考片段 -> 注册 -> 返回片段
  -> 后续 /api/generate/tts 只引用稳定 voiceId
```

这套注册接口是 provider-neutral seam，但当前真正注册的 adapter 只有 VoxCPM。ElevenLabs、MiniMax、豆包等未来可以实现相同 adapter，不能把接口的可扩展性误写成这些 provider 已支持自动注册。

生成 agent 保存后会 fire-and-forget 预热 narrator/teacher 的自动音色，减少第一句旁白等待；其他讨论 agent 按首次使用再注册，避免为可能永远不发言的角色提前产生音频成本。预热失败不影响正确性，因为正式 TTS 路径仍会执行幂等 ensure。

### 7.7 VoxCPM Prompt、Clone 与参考音频

VoxCPM voice profile 支持：

- Auto：使用 agent `VoiceDesign`，没有设计时可用 persona 作为稳定但较泛化的种子。
- Prompt：用户保存自然语言音色描述。
- Clone：用户上传或录制参考音频。

Profile 和参考音频保存在浏览器 IndexedDB；可用 voice 列表会把 profile 转成 `voxcpm:profile:<id>` voiceId。只有支持 reference audio 的 VoxCPM backend 才显示 Clone profile。调用 TTS 时，`getVoxCPMProviderOptions` 根据 backend、profile、agent 上下文决定传 inline prompt、reference audio，还是已注册的 voiceId。

服务端一键生成只有课堂 JSON 中的 agent 配置，没有浏览器 IndexedDB 里的 profile/reference clip，因此不能等价复现所有客户端 Clone/Auto 行为。这也是服务端批量 TTS 遇到 VoxCPM Auto Voice 时选择跳过的原因之一。

### 7.8 预览、编辑和音频缓存失效

TTS 设置预览分两条路径：browser-native 直接调用 SpeechSynthesis；API provider 调用 `/api/generate/tts`，将 base64 转成 Blob/Object URL 后播放。新的预览会取消旧请求、停止旧 Audio 并 revoke URL，组件卸载也会清理资源。

编辑 Actions timeline 中的 speech text 时，旧音频不能继续使用，因为 audio key 基于 `sceneOrder + actionId`，不包含文本内容。`discardSpeechAudio` 会删除 action 上已有 `audioId` 和规范派生 key 对应的 IndexedDB Blob；用户重新生成后，`regenerateSpeechAudio` 复用课堂生成管线的 `generateAndStoreTTS`，仍写回 `tts_s<sceneOrder>_<actionId>`。

因此完整语音生命周期是：

```text
Agent/Teacher 身份
  -> provider + voice 解析
  -> 可选 VoiceDesign 注册/参考音频
  -> speech 拆分
  -> TTS 生成
  -> IndexedDB 或服务端文件缓存
  -> AudioPlayer / Browser SpeechSynthesis 播放
  -> 文本修改时主动失效旧缓存并按需重生成
```

## 8. 媒体生成实现

媒体生成请求来自 outline 的 `mediaGenerations`。

### 客户端媒体生成

入口：

- `lib/media/media-orchestrator.ts`
- `lib/store/media-generation.ts`

特点：

- 与 scene content/actions 生成并行启动
- 图片/视频以 `gen_img_*`、`gen_vid_*` 占位
- 完成后写入 IndexedDB
- 播放或渲染时由 media store 替换/解析

### 服务端媒体生成

入口：

- `generateMediaForClassroom`
- `replaceMediaPlaceholders`

流程：

1. 收集 outlines 中所有 `mediaGenerations`
2. 根据服务端配置选择 image/video provider
3. 生成或下载图片/视频
4. 写入 `data/classrooms/{id}/media`
5. 替换 slide elements 中的占位符 URL

### 8.1 媒体请求与页面占位符

Outline prompt 在需要视觉素材时生成 `mediaGenerations[]`，每项包含 type、prompt、elementId、aspectRatio/style 等参数。Slide content prompt 不等待真实资源，而是把同一个 elementId 写成：

- 图片：`gen_img_*`
- 视频：`gen_vid_*`，可位于 `src` 或显式 `mediaRef`

因此“LLM 生成页面布局”和“媒体 Provider 生成二进制资源”可以解耦。页面先以 skeleton/placeholder 渲染，媒体完成后由 elementId 关联结果，不需要重新请求 LLM 修改 Slide JSON。

Stage 还保存从 outlines 构建的 `videoManifest`，以 `gen_vid_*` 为键保留原始 prompt/aspectRatio。Manifest 是生成意图与视频元素之间的稳定映射，供视频元素在只有 `mediaRef`、尚无最终 `src` 时恢复任务信息；它不是视频文件本身。

### 8.2 客户端调度与持久化

“媒体与页面生成并行”指 `generateMediaForOutlines` 整批任务在 scene content/actions 流水线旁边独立启动；媒体 orchestrator 内部会把图片和视频请求放进同一队列逐项串行处理，避免生成 API 并发限额。它不是把所有媒体请求同时 `Promise.all`。

任务状态机为：

```text
pending -> generating -> done
                      -> failed -> 手动 retry -> pending
```

成功后流程是：

1. Provider route 返回 URL 或 base64；
2. base64 直接转 Blob，远程 URL 通过 `/api/proxy-media` 下载以绕过浏览器 CORS；
3. Blob、mimeType、poster、prompt、params 写入 IndexedDB `mediaFiles`；
4. 创建 Object URL 写入 Zustand task，renderer 随即显示真实媒体；
5. 课堂重新打开时 `restoreFromDB(stageId)` 重建 task 与 Object URL。

结构化错误（例如 `CONTENT_SENSITIVE`）会以空 Blob + error/errorCode 持久化，刷新后仍显示失败状态而不会自动重复调用收费 API；用户显式重试时先删除该失败记录。普通未结构化网络错误只保留在当前 store，可在后续流程重新尝试。

Object URL 不是永久地址。切换/离开课堂和首页重新载入列表时会主动 revoke，防止长时间使用后积累 Blob 内存；持久数据始终是 IndexedDB Blob。

### 8.3 Image Provider 层

图片统一入口 `generateImage(config, options)` 按 providerId 分发到 Seedream、OpenAI Image、Qwen Image、Nano Banana、MiniMax Image、Grok Image、Lemonade adapter。Provider metadata 声明 models、支持宽高比、是否需要 key 和可选最大分辨率。

`/api/generate/image` 会：

- 对 managed provider 使用服务端 key/baseUrl，忽略客户端凭据；
- 对生产环境客户端 baseUrl 做 SSRF 校验；
- 在只有 aspectRatio 时换算 width/height；
- 将不同 adapter 的 URL/base64 结果归一成统一响应；
- 将已识别的安全过滤拒绝映射为 `CONTENT_SENSITIVE`。

尺寸换算是请求适配，不改变 Slide 画布中的元素几何；生成图片最终仍按元素自己的 left/top/width/height 和裁剪规则渲染。

### 8.4 Video Provider 与异步任务

可工作的 video adapter 包括 Seedance、Kling、Veo、MiniMax Video、Grok Video、HappyHorse。多数视频平台采用 submit task -> poll status -> download URL，adapter 把各家的任务状态、轮询间隔和结果字段收敛成 `VideoGenerationResult`；API route 因此允许最长 300 秒。

提交前 `normalizeVideoOptions` 根据 provider capabilities 校正 duration、aspectRatio、resolution：请求值不受支持时取该 provider 的第一个支持值，而不是把无效参数原样发给上游。

当前 `VIDEO_PROVIDERS` metadata 中存在 `sora` 条目和环境变量映射，但 `generateVideo`/connectivity switch 没有 Sora adapter case，models 也是空数组。因此 Sora 目前不是可工作的生成 Provider；这是注册表脚手架，不能仅凭设置类型或 `.env.example` 判断功能已经接通。

### 8.5 媒体代理安全边界

`/api/proxy-media` 不是任意开放代理：

- 初始 URL 和最多 5 次 redirect 的每一跳都会重新执行 SSRF 校验；
- 禁止跳转到内网/不允许地址；
- 单资源上限 25 MiB，同时检查 Content-Length 与实际 Blob 大小；
- 上游 4xx 保留为客户端错误，5xx 折叠为 502；
- 响应仅短时 private cache。

服务端整课生成不经过浏览器代理，而是后端直接下载；它使用 120 秒超时和 100 MiB 上限。两条下载路径的限制不同，原因是客户端代理面向通用浏览器请求，攻击面更大。

### 8.6 服务端批量媒体的并发边界

服务端整课生成选择各类别第一个已配置 provider 和该 provider 的第一个 model。图片队列内部串行、视频队列内部串行，但 image 与 video 两条队列通过 `Promise.all` 并行。这与客户端“所有媒体共用一个串行队列”不同。

每个资源单独 try/catch：某一图片或视频失败只留下未替换的 placeholder，不阻止其他资源和课堂 JSON 持久化。成功资源写入课堂 media 目录，再把 slide image/video 的 placeholder 或 `mediaRef` 替换成 `/api/classroom-media/{classroomId}/media/...` URL。

## 9. 课堂数据与持久化

### 客户端 IndexedDB

主要由 `lib/utils/database.ts` 和各 store 使用。保存：

- stages
- scenes
- stageOutlines
- audioFiles
- mediaFiles
- generatedAgents
- chats 等

`lib/store/stage.ts` 负责 stage/scenes/outlines/generation 状态，并带自动保存、续跑标记、失败 outline 重试等逻辑。

### 服务端文件持久化

服务端课堂生成使用文件系统：

- 课堂 JSON：`data/classrooms/{id}.json`
- 媒体：`data/classrooms/{id}/media`
- 音频：`data/classrooms/{id}/audio`
- job 状态：`data/classroom-jobs`

核心文件：

- `lib/server/classroom-storage.ts`
- `lib/server/classroom-job-store.ts`
- `lib/server/classroom-job-runner.ts`

### 课堂加载

课堂页 `app/classroom/[id]/page.tsx` 会先尝试从 IndexedDB 加载；如果没有，再请求服务端 `GET /api/classroom?id=...`。这使本地生成课堂和服务端 API 生成课堂都能进入同一个播放页面。

### 9.1 Dexie 数据模型与版本迁移

本地数据库名为 `MAIC-Database`，当前 schema version 为 12。核心表不是一个大 Stage JSON，而是按生命周期拆分：

- `stages`：课程元数据、当前 scene、语言、模式标记和 video manifest。
- `scenes`：按 stageId/order 索引的页面内容、actions、whiteboard。
- `stageOutlines`：生成大纲与 `generationComplete`，用于中断续跑。
- `chatSessions`、`playbackState`：课堂讨论和播放断点。
- `audioFiles`、`imageFiles`、`mediaFiles`：二进制资源；media 使用 `${stageId}:${elementId}` 避免不同课程都出现 `gen_img_1` 时冲突。
- `generatedAgents`：课程生成的 agent profile。
- `voiceProfiles`、`autoVoiceCache`：浏览器音色与自动音色参考片段。
- `agentEditSessions`：按 stage 保存的 AI 编辑多会话。
- `snapshots`：遗留 undo/redo 表，新 Slide 编辑历史不以它作为主要机制。

Dexie migration 处理过：删除废弃表、增加 chat/playback/outlines/media/agents、修复 media compound key、把旧 `language` locale 迁移为 `languageDirective`，以及新增 voice/Agent session 表。数据库初始化后调用 `navigator.storage.persist()` 请求持久存储，减少大媒体 Blob 在浏览器存储压力下被回收的概率；浏览器可以拒绝该请求，因此它不是永久保存保证。

### 9.2 Stage 保存与生成续跑

`stage-storage.ts` 保存 Stage 元数据后，会删除该 stage 的旧 scenes 再 bulkPut 当前 scene 集合；chat session 使用独立表。Stage store 通过 debounce 自动保存，scene 进入 store 时先做 schema migration。

生成过程另存 outlines。课堂重新打开时：

1. 载入 Stage/scenes 与 outlines；
2. 用 scene.order 与 outline.order 计算尚未生成项；
3. 若 persisted `generationComplete` 为 true，不再把用户后来删除的 Slide 当成“中断生成”重新补回；
4. 对旧版本没有完成标记的课堂，如果每个 outline order 都已经有 scene，则自愈为 complete 并回写标记；
5. 未完成课堂才恢复 pending outlines 并继续 content -> actions -> TTS。

这里使用 `generationEpoch` 防止异步旧请求跨 Stage 落库，使用 `generationComplete` 区分“生成中缺页”和“用户编辑后主动删页”。两者解决的是不同竞争条件。

### 9.3 播放断点是未接线的保留能力

数据库仍有 `playbackState` 表，`playback-storage.ts` 也提供 save/load/clear；`PlaybackEngine` 可以导出/恢复 sceneIndex、actionIndex、已消费 discussion IDs 和 sceneId 的 snapshot。这些构成了一套断点续播底层接口。

但当前 `PlaybackChromeRoot` 明确移除了播放状态持久化，仓库没有业务代码调用 `savePlaybackState` 或 `loadPlaybackState`，刷新会从头开始。现在只有删除课堂路径会清理可能存在的旧 playback record。

因此“PlaybackEngine 支持 snapshot”是底层能力，“应用已经支持刷新断点续播”则不成立。未来重新接入时还需要用 sceneId 校验页面身份，避免编辑/重排后把旧 actionIndex 恢复到错误场景。

### 9.4 服务端课堂与 Job 文件

服务端 JSON 使用“写临时文件 -> rename”的原子替换方式，降低进程中断留下半截 JSON 的风险。Classroom ID 和 Job ID 只允许字母、数字、下划线和连字符，避免路径穿越。

整课生成 API 的 Job 状态机是：

```text
POST /api/generate-classroom
  -> 写 queued job JSON
  -> 202 返回 jobId/pollUrl
  -> Next after() 启动 runner
  -> running + step/progress/scenesGenerated
  -> succeeded(result) 或 failed(error)
```

同一 Node 进程的 `runningJobs` Map 避免同一 job runner 重复启动；同一 job 文件的 read-modify-write 由进程内 Promise mutex 串行。它们不是跨进程分布式锁，多实例仍需要外部队列/数据库才能获得严格的 exactly-once 语义。

读取 running job 时，如果 30 分钟没有任何 progress update，会在响应中视为 failed/stale，提示进程可能在生成中重启。这个判定不能恢复任务，只避免客户端无限轮询一个已经失去 runner 的 job。

### 9.5 本地课堂删除的当前边界

仓库存在 `deleteStageWithRelatedData`，可以事务删除 stages/scenes/chat/playback/outlines/media/generatedAgents/agentEditSessions；但首页当前实际调用的是 `stage-storage.ts` 的 `deleteStageData`，它只删除 stage、scenes、chat、playback 和每个 scene 的 Quiz localStorage。

因此从首页删除课堂后，`stageOutlines`、`mediaFiles`、generated agent、Agent edit session 等关联记录可能仍留在 IndexedDB。`audioFiles` 又没有 stageId 索引，连通用完整删除 helper 也不能按课程直接清理。这是当前存储清理缺口，不应把“课堂卡片消失”理解成所有二进制和关联数据已被级联删除。

### 9.6 本地与服务端数据不是自动双向同步

客户端生成课堂以 IndexedDB 为主，服务端一键生成课堂以 `data/classrooms` 为主。课堂页的加载顺序只是“本地没有时回退服务端”，不是同步协议：

- 客户端编辑本地课堂不会自动回写服务端 JSON。
- 服务端课堂载入客户端后，后续持久化可能形成浏览器本地副本。
- 同一 ID 两边都存在时优先本地版本。
- 课堂 ZIP 是显式迁移/备份机制，而不是后台同步。

这解释了为什么本地多设备共享、多人协作或服务端编辑一致性不属于当前架构能力。

## 10. 播放与编辑工作流

### 播放模式

`components/stage.tsx` 根据 `useStageStore.mode` 切换：

- playback/autonomous：`PlaybackChromeRoot`
- edit：`EditChromeRoot`

播放引擎 `PlaybackEngine` 是状态机：

- idle
- playing
- paused
- live

它处理：

- start/pause/resume/stop
- speech 音频播放
- discussion 进入 live 模式
- 引擎本身可导出/恢复 snapshot，但当前应用没有接入持久化，刷新后从头播放
- browser native TTS 暂停/恢复

### 编辑模式

编辑相关在：

- `components/edit/`
- `lib/edit/`
- `lib/agent/tools/`

设计重点：

- Pro/Edit 模式与 Playback 模式分离
- 编辑模式有跨标签页 lock，避免多个 tab 同时编辑
- AI 编辑工具可以读场景、重生成 scene、编辑 interactive HTML
- slide schema 有 migration，避免旧数据不兼容

## 11. 导入导出

### PPTX 导出

入口：`lib/export/use-export-pptx.ts`

负责把 slide scene 转成 pptxgenjs 对象，包括：

- 文本 HTML 转 PPT text runs
- shape/path/line
- image/video
- chart
- latex -> OMML
- shadow/outline/color

PPTX 导出主要覆盖幻灯片，不等价于完整互动课堂导出。

### 课堂 ZIP 导出

入口：`lib/export/use-export-classroom.ts`

导出内容：

- `manifest.json`
- stage 元信息
- scenes
- actions
- agents
- audio
- generated media
- interactive HTML 内联资源

这是完整课堂包，更适合迁移、离线保存或再次导入。

### 课堂 ZIP 导入

入口：`lib/import/use-import-classroom.ts`

导入时会：

- 解析 manifest
- 生成新的 stage/scene/agent/media/audio ID
- 重写 audio/media/agent 引用
- 写入 IndexedDB

### PPTX 导入的当前状态

仓库包含完整的 `@openmaic/importer` 解析包和 `lib/import/use-import-pptx.ts` 客户端桥接，但主应用目前尚未把解析结果接入课堂创建，因此不能把它描述成已经完成的用户功能。

Importer 内部管线是：

```text
.pptx zip
  -> parser：解压、XML/rels、单位转换
  -> model：theme/master/layout/slide 与节点几何
  -> serializer：样式级联、元素/媒体/公式转换
  -> adapter：输出 OpenMAIC Slide[]
```

主应用为规避 `pdfjs-dist` 动态 require 与 Turbopack 冲突，不把 importer runtime 静态打进页面，而是在 postinstall 时由 `scripts/sync-maic-importer.mjs` 把预构建产物复制到 `public/vendor/maic-importer/index.js`，使用时再通过 URL 动态加载。

但首页调用 `useImportPptx()` 时没有提供 `onImported`，解析出的 slides 目前只记录日志；入口也默认由 `NEXT_PUBLIC_ENABLE_PPTX_IMPORT` 隐藏。也就是说：SDK 解析能力已存在、主应用桥接已搭好、端到端落库和创建 Stage 尚未完成。

## 12. API 工作流总览

### 客户端渐进生成

```text
首页 app/page.tsx
  -> sessionStorage.generationSession
  -> /generation-preview
  -> /api/parse-pdf               可选
  -> /api/generate/scene-outlines-stream
  -> 用户审阅/编辑 outlines
  -> /api/generate/agent-profiles 可选
  -> 创建 Stage，保存 outlines
  -> /classroom/{stageId}
  -> useSceneGenerator.generateRemaining
     -> /api/generate/scene-content
     -> /api/generate/scene-actions
     -> /api/generate/tts         可选
     -> IndexedDB 保存 scene/audio/media
```

### 服务端一键生成

```text
POST /api/generate-classroom
  -> create job
  -> after() runClassroomGenerationJob
  -> generateClassroom
     -> resolve model
     -> optional web search
     -> generate outlines
     -> generate/default agents
     -> loop outlines:
        -> generate scene content
        -> generate scene actions
        -> create scene
     -> optional media generation
     -> optional TTS generation
     -> persist data/classrooms/{id}.json
  -> GET /api/generate-classroom/{jobId} polling
  -> /classroom/{id}
```

## 13. Prompt 系统

Prompt 统一放在 `lib/prompts/templates/`，通过 `buildPrompt` 加载。

模板支持：

- `{{variable}}`
- `{{snippet:name}}`
- `{{#if condition}}...{{/if}}`

主要 prompt 类型：

- 大纲：`requirements-to-outlines`、`interactive-outlines`、`task-engine-outlines`
- 页面：`slide-content`、`quiz-content`、`simulation-content`、`diagram-content`、`code-content`、`game-content`、`visualization3d-content`、`procedural-skill-content`
- 动作：`slide-actions`、`quiz-actions`、`interactive-actions`、`pbl-actions`
- 多智能体：`agent-system`、`director`
- Web Search：`web-search-query-rewrite`

Prompt 与业务逻辑的边界比较清晰：prompt 决定模型输出结构，TypeScript 后处理负责校验、修复、降级、补 ID、替换媒体。

## 14. 多智能体与课堂讨论

多智能体相关代码主要在：

- `lib/orchestration/`
- `lib/chat/`
- `lib/agent/`
- `components/agent/`

生成出来的智能体用于：

- 讲课动作中的 agent attribution
- discussion action 中指定发起者
- TTS voice 选择
- AgentBar 中选择课堂角色
- live discussion 时的 director/agent loop

运行时会维护上下文摘要、白板 ledger、同伴上下文、消息转换等，避免长对话直接塞满上下文。

## 15. 重要扩展点

### 新增 LLM provider

主要看：

- `lib/ai/providers.ts`
- `lib/server/provider-config.ts`
- `lib/types/provider.ts`
- 设置 UI 与 i18n

### 新增 TTS provider

按 `lib/audio/tts-providers.ts` 文件头部说明：

1. 在 `lib/audio/types.ts` 增加 provider id
2. 在 `lib/audio/constants.ts` 增加 provider metadata
3. 在 `lib/audio/tts-providers.ts` 实现请求逻辑
4. 在 `generateTTS` switch 中接入
5. 补 i18n/设置 UI

### 新增 PDF provider

按 `lib/pdf/pdf-providers.ts` 文件头部说明：

1. 增加 `PDFProviderId`
2. 增加 provider constants
3. 实现 parse 函数
4. 在 `parsePDF` switch 中接入
5. 标注 capabilities

### 新增互动组件类型

需要改：

- `lib/types/widgets.ts`
- `SceneOutline.widgetType`
- `generateWidgetContent`
- 新增 prompt template
- `interactive-actions` 是否需要支持新动作
- iframe 端消息处理

### 新增 Action 类型

需要改：

- `packages/@openmaic/dsl/src/action.ts`
- `lib/action/engine.ts`
- action prompt snippets
- parser/validator/tests
- 播放 UI 或 canvas store

## 16. 当前实际生成调度与并发边界

现有代码不只是抽象的“两阶段”，客户端实际采用的是“媒体并行 + 内容可选并发预热 + 动作/TTS 有序提交”的流水线，核心在 `lib/hooks/use-scene-generator.ts`：

```text
所有 outlines
  ├─ 后台并行：generateMediaForOutlines
  └─ 场景流水线（按 order 提交）
       ├─ content：默认串行；配置 PARALLEL_SCENE_CONCURRENCY > 1 后有界并发预取
       ├─ actions：严格按场景顺序生成
       ├─ TTS：严格按场景顺序生成（browser-native 除外）
       └─ addScene：该场景全部成功后才写入 store
```

这里保持 actions 串行，是因为下一页动作 prompt 会接收上一页的 `previousSpeeches`，用于减少跨页重复和保持讲解连贯。并发 content 只提前计算无跨页依赖的内容，消费和提交仍按 outline order 进行，因此不会打乱课堂顺序。

失败语义也分两种：

- 串行 content、actions 或非浏览器 TTS 失败：暂停整批，保留失败 outline 供单页重试。
- 并发 content 模式中的某页 content 失败：记录该页后继续消费已经在途的其他 content，最终状态仍为 paused，提示用户处理失败项。
- `stop()` 会同时提升 `generationEpoch` 并 abort fetch/media；晚到的旧请求结果因为 epoch 不匹配而被丢弃，避免切课后污染新课堂。

### 16.1 第一张可见页面的前台生成门槛

generation-preview 不会在只有 outlines 时立刻跳进一个全是 skeleton 的课堂。它先在前台完成第一条 outline 的 content、actions 和可选 TTS，成功写入第一个 Scene 后才：

1. 把剩余 outlines 设为 generating placeholders；
2. 把继续生成所需的 PDF 图片、agents、用户画像和 languageDirective 写入 `generationParams`；
3. 保存 Stage 并跳转 `/classroom/{id}`；
4. 由课堂页的 `useSceneGenerator` 继续剩余页面。

第一张页面对 content、actions 和每条 TTS 使用统一 `FOREGROUND_SCENE_RETRY_OPTIONS.maxRetries = 2`，因为首屏失败会让用户无法进入课堂；进入课堂后的后台生成则使用批处理失败/暂停/单页重试语义。第一张 Scene 只有在 TTS 全部成功（或TTS未启用/使用browser-native）后才提交，避免展示一张看似完成但缺少所需旁白音频的首屏。

## 17. 编辑态 MAIC Agent Runtime

旧文档只把 `lib/agent/` 描述成“AI 编辑工具”，当前实现已经是一套独立的、带服务端工具执行和客户端线程状态的 Agent Runtime，并且由 `NEXT_PUBLIC_MAIC_EDITOR_ENABLED` 默认关闭、显式开启。

### 17.1 请求与执行链

```text
EditChromeRoot / AgentPanel
  -> useAgentRuntime（assistant-ui ExternalStore）
  -> POST /api/agent/edit（SSE）
  -> 为 maic-agent 阶段解析对话模型
  -> pi-agent-core Agent（工具串行执行）
  -> 工具按各自 LLM stage 再解析模型
  -> 工具结果经 SSE 返回
  -> 客户端校验并应用到 Dexie-backed stage store
```

它不是让模型直接改客户端状态。客户端把当前 stage 的可信 `sceneContextMap` 随请求发送，模型只给出 `sceneId` 与自然语言指令；服务端工具从可信 context 取 outline/content，再执行生成。结果回到客户端后还会经过空结果保护、同步应用和快照管理。

### 17.2 当前能力边界

工具注册表与 allowlist 当前只开放四项：

- `read_scene_content`：读取场景实际内容，要求先读后改。
- `regenerate_scene`：仅 slide，重建页面内容和动作。
- `regenerate_scene_actions`：只重建旁白与播放动作。
- `edit_interactive_html`：对 interactive HTML 做精确 `oldText -> newText` 局部替换。

当前不能通过 Agent 新增、删除、排序、复制页面，不能直接编辑 quiz/PBL/白板，也不能保证整页重生成后保留指定元素或 cue。`beforeToolCall` allowlist 是服务端能力边界；`afterToolCall` 已留出 quota hook，但当前额度源是无限值 stub，尚未接入真实计费/额度系统。

### 17.3 模型路由与会话持久化

Agent 对话模型使用 `maic-agent` 路由；重生成工具再按 `scene-content:slide`、`scene-content:interactive`、`scene-actions` 等阶段独立路由，因此可以把对话与重生成交给不同模型。

客户端保存每门课的多会话历史：

- 消息正文存 `db.agentEditSessions`（IndexedDB/Dexie），避免 localStorage 容量上限。
- localStorage 只保存每个 stage 当前活动 session 的短 ID。
- 每个 stage 的会话按更新时间排序并设置软上限；旧版单线程 localStorage 数据会一次性迁移。
- 推理块耗时、工具卡片状态和可恢复快照会被序列化；删除使用 tombstone 防止并发中的迟到保存把会话“复活”。

### 17.4 编辑/播放隔离

Pro/Edit 模式进入前会：

1. 检查当前 scene 是否已生成且可编辑；
2. 获取跨标签页 edit lock；
3. 并行停止播放 SSE、播放引擎、TTS，并预加载编辑器 chunk；
4. 完成后再切换 mode。

这体现了架构中的一个重要原则：生成、播放、编辑不是三个松散页面，而是共享同一 Stage 数据、但有明确生命周期边界的三种运行状态。

## 18. Interactive 页面运行时与安全边界

Interactive HTML 来自 LLM 或导入课堂，不能按可信业务组件运行。当前实现采用：

- `srcDoc` sandbox iframe，允许 scripts/forms/popups，但刻意不加 `allow-same-origin`，让文档处于 null origin，阻止其读取宿主 cookie、localStorage 和 DOM。
- 宿主与 iframe 只通过 `postMessage` 传递 widget action。
- 注入 storage shim，使 sandbox 内访问不到真实 Web Storage 时退化到内存存储。
- 注入 runtime-error 捕获与重放协议，把页面初始化阶段的错误回传给宿主；编辑 Agent 修复页面时可带上这些运行时错误。
- `InteractiveIframeHost` 固定挂在 Stage 根部，实际 iframe 不跟随 scene renderer 或 playback/edit 子树卸载。
- `interactive-iframe-pool` 以 sceneId 缓存最多 3 个 iframe，LRU 淘汰；切场景或切 Pro 模式只切可见性，内容真正变化时才重载。

因此互动页同时解决了两类问题：sandbox 提供“不信任生成代码”的隔离边界，keep-alive pool 提供“切换后不丢模拟器/游戏内部状态”的体验边界。

## 19. 配置、部署与功能开关

### 19.1 模型与 Provider 配置

浏览器交互生成主要从 settings store 读取 provider；服务端一键生成从环境变量/服务端 provider 配置解析。`lib/server/model-routes.ts` 支持按生成阶段路由模型，例如大纲、不同 scene content、actions、Agent 对话可以分配不同模型。这样既能控制成本，也能避免用同一个模型兼顾规划、长 HTML、结构化动作和工具调用。

### 19.2 功能开关

- `NEXT_PUBLIC_MAIC_EDITOR_ENABLED`：开启 Pro/Edit 与 MAIC Agent，默认关闭。
- `OPENMAIC_ENABLE_VOCATIONAL`：服务端权威控制职业教育 Task Engine；客户端传参不能绕过。
- `NEXT_PUBLIC_SHOW_VOCATIONAL_TEST_UI`：只控制实验入口可见性，不是安全边界。

职业 Task Engine 与普通 Interactive Mode 是两个维度：task-engine 课堂属于互动课堂，但普通互动课堂不一定是职业任务课堂。

### 19.3 持久化的三层含义

项目中的“storage”需要区分：

1. 浏览器持久化：Dexie/IndexedDB 保存课堂、场景、音频、媒体、PDF、Agent 会话；settings 与少量指针使用 localStorage。
2. 服务端课堂文件：`data/classrooms` 与 `data/classroom-jobs` 保存 API 生成课堂及 job，媒体通过受控 route 提供。
3. 对象存储抽象：`lib/storage/types.ts` 中的 `StorageProvider` 定义 upload/exists/getUrl/batchExists，但当前仓库注册的是 `NoopStorageProvider`。这是一条扩展接口，不应误解为项目已经内置某个可用 OSS/S3 后端；PBL 上传在未配置对象存储时会按调用点策略降级。

### 19.4 访问与网络安全

- 可选 access-code guard 用于部署入口保护。
- 媒体代理和服务端 fetch 使用 SSRF guard/受控代理逻辑，避免任意内网地址访问。
- interactive iframe 的 sandbox 是前端不可信代码边界。
- Provider key 应保留在服务端配置；客户端配置适用于本地/自托管使用，但不等于服务端密钥托管。

## 20. PBL v2 运行流程补充

PBL 不只是在生成阶段产出一个配置。需要先纠正 v1/v2 的界面边界：role selection、Guide、传统 issue board 和多角色 MCP 主要属于 PBL v1；PBL v2 明确用“单 Instructor 引导的 Hero -> Workspace -> Completion”替代该流程。两套数据同时保留是为了兼容旧课堂，renderer 根据 `projectV2` 是否存在选择实现。

### 20.1 PBL v2 数据模型

`PBLProjectV2` 是一个可序列化的完整项目状态，主要包含：

- 项目生命周期：designing/review/active/completed/archived。
- UI phase：hero/generating/workspace/completed。
- roles：当前产品只有一个 Instructor；learner 是隐含 user，不建立 role record。
- milestones：locked/active/completed。
- microtasks：todo/in_progress/completed/skipped，当前全部由 learner 负责。
- chat threads、submissions、evaluations、engagementEvents。
- proficiency 与内部 assessment。
- 可选 scenario：角色扮演人物、场景、旁白和逐 beat 目标。

Planner 为 milestone 生成 briefing、completionCriteria、debrief，为 microtask 生成 hints、可交付目标和顺序；核心知识阶段还可带 `synthesisCheck`，要求在封闭 milestone 前进行一次综合反向提问。普通 PBL 生成 v2 失败时可先尝试 single-call、再尝试 loop planner，最后回退 v1；scenario role-play 不回退，因为 v1 无法表达角色、场景 beat 和模拟器语义。

v2 payload 同时转换为 legacy `projectConfig`，是旧渲染/存储兼容层，不代表 v2 仍以 issue board 为核心。

### 20.2 Hero 到 Workspace

初始 `uiPhase` 是 hero。学习者进入项目时，`prepareWorkspaceLaunchProject` 切到 workspace，并可携带前序 Quiz 快照。第一次进入调用 `/api/pbl/v2/open-task` 的 greeting phase；后续新 microtask 激活时调用 setup phase，保证 Instructor 主动开场，而不是让空白工作区等待用户先发消息。

前序 Quiz 中可确定评分的题目会折算成 pre-play proficiency signal；无法评分的开放题记为 unscored，不当作错误惩罚。

### 20.3 Stateless SSE 与 Project Patch

PBL v2 API 不在服务端保存会话。客户端每次发送完整 `PBLProjectV2` clone，服务端在请求内执行 Instructor/Simulator/Evaluator，再通过 SSE 返回：

- `token`：当前回复的文本增量。
- `tool_call`：Instructor 使用的结构化工具。
- `project_patch`：message、advance、engagement event、evaluation、handover、proficiency 等权威状态变更。
- `sim_phase`：scenario 中的 narration/character 阶段。
- `reset_draft`：任务推进时丢弃可能提前泄漏下一任务的临时文本。
- `error` 与唯一终止 `done`。

SSE 每 15 秒发送 heartbeat，并把 request abort 传播给生成器。客户端应用 patch 到 `scene.content.projectV2`，再由 Stage 持久化；服务器不会返回整份新 Project 覆盖客户端，避免长项目反复传回两份大 JSON。

### 20.4 Instructor、任务推进与证据门槛

`/api/pbl/v2/instructor` 根据当前 milestone/microtask 和 phase 暴露有限工具。Instructor 可以记录 observation、closing check、内部 assessment、难度调整和 advance 意图，但任务推进不是只靠模型说“完成了”。

任务完成采用两阶段门槛：

1. Instructor 通过工具建立 `pendingTaskCompletion`，保存 reason、assessment 和可选 evidence；
2. UI 提示学习者点击当前任务的 Done；
3. 如果任务有 submission，task evaluation 必须达到 60/100 才能完成，未通过可修改后再次提交；
4. 核心 milestone 的最后一个任务还需要 synthesis check 证据；
5. 通过后才把 microtask、milestone、next task 和 project status 作为 advance patch 提交。

这种设计把“教学判断”与“状态提交”分开，避免 LLM 在一段自然语言中越过 UI 确认、评分或阶段门槛。

### 20.5 Engagement 与适应性难度

项目维护 append-only engagement ledger，记录 learner turn、错误、重复错误、struggle、question、concept unlocked、closing check、task open/complete/skip 和 proficiency change。Ledger 最多保留 500 条，旧事件从头淘汰；每个 microtask 在完成时缓存 engagement summary，保留持续时间、回合数、错误签名、概念、问题和 closing quality。

Proficiency 不是让 LLM 每轮随意猜 beginner/intermediate/advanced，而是三阶段证据引擎：

1. Planner-time：outline、用户 bio、前序场景难度等静态信号。
2. Hero-time：前序 Quiz accuracy。
3. Runtime：observation、closing check、force advance、任务速度、submission score。

信号用纯代码做权重限制、EWMA、confidence、hysteresis 和 cooldown；至少积累一定动态证据后才允许换 tier，降低难度来回抖动。学习者明确自报水平或要求升降难度优先级最高，可以直接重设 tier。Proficiency 默认 intermediate，通常只作为 Instructor guidance 和开发调试信息，不直接向学习者展示分数。

### 20.6 普通项目与 Scenario Role-play

普通项目由 Instructor 在 Workspace 中讲解、追问、接收产物并推进任务。Scenario 项目额外带 prep -> 一个或多个 roleplay -> wrapup 阶段：

- prep：Instructor 说明情境、人物和规则。
- roleplay：`/api/pbl/v2/simulator` 以场景角色身份回应，必要时先输出 neutral system narration；Simulator 不是 roles[] 中的通用助教。
- 每个 beat 可定义隐藏的 `successWhen`、characterObjective、skillFocus、narration 和 learnerBrief。
- wrapup：Instructor 做轻量回顾，详细评价留给完成页。

Simulator route 只允许 scenario 的 roleplay milestone。Character persona、situation 和 boundaries 在 Planner 阶段固化，保证角色扮演有可复现的身份和安全边界，而不是每轮重新发明人物。

### 20.7 Submission 与评估链

Submission 可以是文本、文件或链接，并绑定 microtask。文本文件采用扩展名与 MIME 双重白名单；PDF 走现有 PDF 解析路径；图片保留为视觉输入。对象存储可用时保存原文件 URL，未配置时按 UI 的大小限制选择 base64/文本降级，不能把任意大二进制塞进 Project JSON。

Task evaluator 只评分最新 submission，旧版本保留为历史而不重复污染当前分数；没有 submission 的任务跳过 task evaluation。`/api/pbl/v2/evaluate` 统一处理：

- task：绑定 milestoneId + microtaskId，可使用视觉模型评估图片。
- milestone：汇总阶段完成证据与近期对话。
- final：汇总整个项目，生成 strengths、improvements、whatYouBuilt、whatYouLearned、下一步和可选星级/分数。

Advance patch 以声明式 flags 指示客户端在 Instructor stream 结束后按 task -> milestone -> final 顺序串联评价，避免把两条 LLM stream 交错在同一个响应里。Evaluation 作为 project patch 写回客户端项目，任务卡、handover 卡和 Completion 页面消费同一份记录。

### 20.8 PBL v2 模型路由

Planner 使用 scene-content:pbl 所属生成模型；运行时分别支持 `pbl-v2-runtime:instructor`、`open-task`、`evaluate`、`simulator` 组合路由，未配置时逐级回退到 `pbl-v2-runtime`。每个 route 接收 ThinkingConfig 并支持 300 秒 SSE 请求。

这允许 Planner 使用擅长长结构输出的模型、Instructor 使用低延迟对话模型、Evaluator 使用更强推理/视觉模型，同时保持 Project 数据协议不变。

## 21. 质量保障与可观测性

项目的验证不只包含单元测试：

- Vitest：generation、store、media、export、agent、PBL、audio、orchestration、API 等模块测试。
- Playwright：首页到生成、outline 审阅、课堂交互、slide/quiz surface、iframe keep-alive、托管 provider 等关键链路。
- Eval：`eval/pbl-v2-planner`、`eval/whiteboard-layout`、`eval/outline-language`、`eval/orchestration` 对非确定性 LLM 输出做场景化评测，并带 judge/report/compare 工具。
- `lib/logger.ts` 提供模块化日志；生成 job 暴露 step/progress/scenesGenerated/error，客户端也维护 generation status、failed outlines 和 runtime errors。

这套测试结构反映了项目的风险分层：纯数据修复用单测，页面/模式切换用 E2E，无法用固定断言覆盖的 LLM 质量用 eval。

## 22. 架构取舍与当前边界

### 22.1 主要优点

- 以 Stage/Scene/Action 为稳定契约，把生成器、渲染器、播放器、导入导出连接起来。
- outline 先审阅、content/action 后生成，降低高成本生成返工。
- prompt 输出与 TypeScript 后处理分工明确：模型负责语义与布局建议，代码负责 ID、结构、校验、修复、降级和引用替换。
- 支持按阶段模型路由、按 provider 扩展、客户端/服务端双路径，适合本地、托管和 API 三种使用方式。
- 播放与编辑有显式状态机、锁、abort/epoch 与恢复机制，减少长任务和多标签页导致的数据竞争。

### 22.2 需要注意的边界

- `@openmaic/dsl` 只原生拥有通用课堂骨架、slide/quiz 和标准 Action；interactive/PBL 的富内容仍由主应用通过泛型扩展，并非全部 SDK 化。
- DSL migration registry 目前还是空脚手架；真正的兼容迁移更多存在于主应用 slide schema/import 路径。
- 服务端课堂持久化默认是本地文件，横向扩容或 serverless 多实例部署需要外接共享存储方案。
- 对象存储 provider 当前为 noop；相关接口是扩展准备，不是开箱即用的云存储实现。
- Agent quota hook 尚未接真实额度；编辑 Agent 的能力仍是四个 allowlisted 工具。
- Interactive HTML 虽有 iframe sandbox，但仍应把网络访问、第三方脚本可用性和 CSP 视为部署侧需要继续约束的面。
- PPTX 只表达 slide 视觉内容，不能保真承载 discussion、PBL、互动 iframe 和完整 Action runtime；完整语义应使用课堂 ZIP。

## 23. Web Search 与外部知识注入

Web Search 是大纲生成前的可选知识增强层，而不是 Agent 在讲课过程中任意浏览网页。客户端通过 `/api/web-search` 发起请求，服务端流程为：

```text
用户 requirement + 可选 PDF 摘要
  -> buildSearchQuery
     -> 短需求直接使用
     -> 长需求或带 PDF 时调用 web-search-query-rewrite 模型压缩查询
     -> 改写失败则回退原始 requirement
  -> searchWeb(provider)
  -> 统一格式化 answer/sources/context
  -> context 注入 outline prompt
```

支持 Baidu、Bocha、Brave、MiniMax、Tavily。Provider 可以由客户端配置，也可以被服务端标记为 managed；managed 模式下服务端忽略客户端提交的 key/baseUrl，以管理员配置为准。搜索结果失败不会阻断课堂生成，代价是该次大纲只使用原始需求与文档上下文。

`web-search-query-rewrite` 是独立模型路由，输入 PDF 摘要会在路由边界截断，最终查询也会做长度控制。这体现了“用便宜模型做检索表达，用主模型做课程规划”的分层思路。

## 24. ASR 语音输入实现

TTS 负责“课堂说出来”，ASR 负责“用户说进去”。语音输入链路由 `components/audio/speech-button.tsx`、`lib/hooks/use-audio-recorder.ts`、`lib/hooks/use-browser-asr.ts` 和 `/api/transcription` 组成。

两条执行路径：

- Browser Native：直接在客户端使用 Web Speech API，不经过服务端转写接口。
- 服务端 ASR：浏览器录音后提交 multipart audio、provider/model/language 到 `/api/transcription`，由 `transcribeAudio` 分发到 OpenAI Whisper、Qwen、Azure、Lemonade 或 custom OpenAI-compatible provider。

服务端会区分普通客户端配置和 managed provider：managed provider 忽略客户端 key/baseUrl；生产环境对客户端自定义 baseUrl 做 SSRF 校验。不同 provider 的音频格式要求并不相同，例如 Lemonade 路径要求 WAV，录音与转换逻辑需要服从 provider capability，而不是假设所有后端都能接受 WebM。

ASR 的输出最终只是文本输入，后续仍走普通需求生成或课堂问答链路，因此语音交互没有复制一套课程生成逻辑。

## 25. 白板、实时讨论与 Action 执行闭环

课堂运行时存在两个相互衔接但职责不同的执行器：

- `PlaybackEngine` 管理整堂课的 action 游标、playing/paused/live 状态、恢复进度和场景推进。
- `ActionEngine` 执行单个动作，调用注入的 Stage/Canvas/Audio/Widget 能力。

白板 action 覆盖打开/关闭、文本、形状、图表、公式、表格、连线、代码绘制、代码局部编辑、删除和清空。除 open/close 外，白板动作执行前会确保白板已打开；动作是 awaitable 的，播放引擎在动画或写入完成后才进入下一步。

`discussion` action 会把播放状态从脚本播放切到 live 讨论：

```text
discussion action
  -> Roundtable / Chat session
  -> 客户端 runAgentLoop
  -> 每轮 POST /api/chat（完整消息 + 最新 Stage 状态）
  -> LangGraph Director 选择 Agent / USER / END
  -> Agent 流式输出文本与 Action
  -> 客户端执行 Action，下一轮重新读取最新白板/场景状态
  -> Director cue USER 或 END 后退出/等待用户
```

`/api/chat` 本身无服务端会话状态，每次请求都携带完整上下文；取消由客户端 abort 传播到请求 signal。单 Agent 首轮由代码直接派发，避免一次 Director LLM 调用；多 Agent 才由 Director 根据对话摘要、角色、白板 ledger 和用户画像决定下一位。连续两个空 Agent turn 会由客户端保护性终止，避免无限空转。

Widget action 则通过按 scene 注册的 `postMessage` 通道控制 interactive iframe。这样 speech、白板、讨论和互动页面虽然表现不同，仍统一归入 Action 时间线。

## 26. Quiz 作答、评分与结课汇总

Quiz 页面生成后的运行流程分为本地确定性评分和 LLM 开放题评分：

- single/multiple choice：客户端按答案集合比较，顺序不影响多选判定。
- short_answer：提交 `/api/quiz-grade`，使用 `quiz-grade` 阶段模型，按题目、用户答案、满分和可选评分要点返回分数与简评。
- LLM 返回会被限制在 `0..points` 并取整；JSON 无法解析时采用半分与通用评语的降级结果。

作答状态按 sceneId 写入 localStorage，并区分草稿、已提交答案、评分结果三种生命周期。重试会清理提交结果；删除场景会清理全部相关 key；结课页读取已提交答案，若尚未提交则读取草稿参与汇总。因此 Quiz 不只是静态内容类型，也有独立的交互状态和课程完成统计。

## 27. Stage API 与内部能力分层

`lib/api/stage-api.ts` 提供面向 Agent、Action 和业务代码的统一高层接口，将 Zustand store 包装成七组能力：

- `scene`：创建、删除和管理场景。
- `navigation`：场景导航。
- `element`：增删改 slide element。
- `canvas`：highlight、spotlight 等画布操作。
- `whiteboard`：白板写入与清理。
- `mode`：课堂模式切换。
- `stage`：课程元数据。

它使用依赖注入的 `StageStore` 接口，而不是在所有业务代码里直接绑定全局 store。这一层与 `@openmaic/dsl` 的区别是：DSL 描述可序列化数据契约，Stage API 描述对运行中课堂能做什么，Action Engine 再把结构化动作翻译为这些操作。三层关系是：

```text
DSL：课堂是什么
  -> Stage API：课堂可以怎样被操作
     -> Action/Playback Engine：何时、按什么顺序执行操作
```

当前 Stage API 的注释提出幂等性目标，但具体 mutation 是否幂等仍取决于子 API 和调用参数，不能把设计目标等同于所有操作已经具备严格幂等保证。

## 28. 模型路由与推理配置的完整边界

`MODEL_ROUTES` 不只支持生成三阶段，而是覆盖当前所有真实 LLM 调用面：outline、四类 scene content、actions、agent profiles、quiz grade、PBL chat、PBL v2 各运行端点、课堂 discussion、服务端整课生成、搜索改写和 MAIC Agent。

路由值既可以是 `provider:model` 字符串，也可以是 `{ model, thinking }`，其中 thinking 会按模型能力归一化。组合路由采用逐级回退，例如：

```text
scene-content:quiz -> scene-content -> DEFAULT_MODEL
pbl-v2-runtime:evaluate -> pbl-v2-runtime -> DEFAULT_MODEL
```

因此模型选择是服务端的横切基础设施，而不是散落在各 route 中的硬编码。主流程可以用高能力模型生成 slide/interactive，用低延迟模型驱动 discussion，用专门模型评估 PBL，同时保持未配置时回退到统一默认模型。

## 29. 国际化与多语言内容边界

应用 UI 使用 i18next，包含简体中文、繁体中文、英语、日语、韩语、葡萄牙语、俄语和阿拉伯语资源。课程内容语言则由 `languageDirective` 从 outline 阶段贯穿到 scene content、actions、TTS 与评分 prompt。

这里需要区分两种语言状态：

- UI locale：按钮、提示、错误和设置界面的语言。
- course language/languageDirective：模型生成的课程页面、旁白和课堂交互语言。

二者可以不同。`languageDirective` 被持久化到 Stage，续跑和单场景重试继续使用它，避免后续页面因客户端 UI 语言变化而漂移。语音 provider 的 voice/language capability 仍需独立匹配，课程语言正确不代表任意 TTS voice 都支持该语言。

## 30. 运行与部署形态

项目支持的运行方式共享同一 Next.js 应用，但持久化与网络边界不同。

### 30.1 本地开发与生产构建

- 开发：`pnpm dev`，用于热更新和调试。
- 本地生产：`pnpm build && pnpm start`。
- Node.js 要求 `>=20.9.0`，仓库固定 pnpm 10。
- postinstall 会依次构建 mathml2omml、pptxgenjs、DSL、importer、renderer，并同步 importer 浏览器产物；跳过 postinstall 可能导致 PPTX parser vendor 文件不存在。

非 Vercel 构建使用 Next.js standalone output。`@earendil-works/pi-ai` 与 `pi-agent-core` 被标记为 server external package，让其 Node 动态 import 在运行时解析，而不是被 webpack 错误静态打包。

### 30.2 Docker

Dockerfile 采用 deps/builder/runner 多阶段构建：构建阶段安装 sharp、canvas 所需原生工具，运行镜像只保留 standalone 产物与运行库，并使用非 root `nextjs` 用户。Compose 把 `/app/data` 挂载到命名卷，因此服务端 classroom/job/media 文件可以跨容器重启保留；`server-providers.yml` 可选只读挂载。

### 30.3 Vercel

Vercel 使用普通 Next.js output，并把 API function 最大时长配置为 300 秒。需要注意：代码中的本地文件课堂存储适合单机/持久卷；在无共享持久磁盘的 serverless 部署里，不能把它当成跨实例可靠数据库。生产化托管应替换课堂/job/媒体持久化，或保证相关请求落在具有共享存储的后端。

### 30.4 健康检查与能力发现

`GET /api/health` 返回版本以及 webSearch、imageGeneration、videoGeneration、tts capability。这些 capability 来自服务端已配置 provider，不等于前端 UI 是否显示某个开关。外部调用方应先发现能力，再决定是否向整课生成接口提交可选功能字段。

### 30.5 页面嵌入策略

默认响应使用 `frame-ancestors 'self'` 并配合 `X-Frame-Options: SAMEORIGIN`。配置 `ALLOWED_FRAME_ANCESTORS` 后，CSP 会扩展允许来源，同时移除无法表达 allow-list 的 X-Frame-Options。这是宿主页面能否被外部平台 iframe 嵌入的部署边界，与课堂内部 interactive sandbox iframe 是两个不同方向的安全问题。

## 31. ACCESS_CODE 认证边界

设置 `ACCESS_CODE` 后，根布局显示访问码 Guard，验证接口使用恒定时间比较，并设置有效期 7 天的 HttpOnly、SameSite=Lax、生产环境 Secure cookie。Cookie 内容是 `timestamp.HMAC-SHA256`，middleware 会重新验签，而不是只检查 cookie 是否存在。

middleware 的行为需要准确区分：

- `/api/access-code/*` 与 `/api/health` 永远放行。
- 其他 API 没有有效 cookie 时直接返回 401。
- 页面请求仍允许进入，由前端 Guard 弹出验证框；因此它不是把 HTML 路由重定向到独立登录页。
- 静态资源、Next image 和 logos 不进入该 middleware matcher。

这是一层共享部署访问保护，不是用户账户、角色授权或多租户权限系统。Token 当前没有服务器端过期字段校验，7 天生命周期主要由 cookie `maxAge` 控制；修改 `ACCESS_CODE` 会自然让旧 HMAC 失效。

## 32. OpenMAIC Skill / 外部 Agent 集成

仓库的 `skills/openmaic/` 不是课堂运行时代码，而是供 OpenClaw 等外部 Agent 操作 OpenMAIC 的 SOP 集成层。它定义了两种模式：

- 本地模式：确认仓库 -> 选择 dev/production/Docker -> 指导用户配置服务端 provider -> 启动 -> `/api/health` 验证 -> 提交生成 job。
- 托管模式：从外部 Agent 自己的配置中读取 access code，调用 `open.maic.chat`，无需克隆仓库或配置本地 provider。

生成协议复用项目的服务端一键生成 API：

```text
GET /api/health（能力发现）
  -> 可选 POST /api/parse-pdf
  -> POST /api/generate-classroom
  -> 保存 jobId/pollUrl
  -> GET pollUrl，持续跟踪 queued/running
  -> succeeded 后返回 classroomId/url
```

Skill 明确禁止通过请求临时覆盖模型/provider，要求由 OpenMAIC 服务端配置控制；对长 job 采用稀疏轮询且不因单次网络失败重新提交，避免重复生成和重复扣费。

需要区分仓库代码与托管站增强：Skill 文档约定托管站使用 Bearer access code 和每日额度，但当前开源 middleware 实现的是浏览器 cookie `ACCESS_CODE`，没有 Bearer token 或每日配额逻辑。那些属于 `open.maic.chat` 部署侧能力，不能据此推断本仓库开箱即带相同认证/计费功能。

## 33. Slide 页面渲染架构

“页面生产”只解决 Slide JSON 从哪里来，渲染层负责把 DSL 变成屏幕画面。主应用的 `components/slide-renderer/` 与独立包 `@openmaic/renderer` 有不同职责。

### 33.1 主应用只读播放画布

`ScreenCanvas` 从 Scene Context 读取当前 `SlideContent.canvas.elements/background`，根据容器尺寸计算 viewport 和 `canvasScale`，再按元素类型分派到 text/image/shape/line/chart/table/latex/video/code 渲染器。

播放特效不是修改 Slide 数据，而是叠加在画布之上的运行时状态：

- highlight：元素内容层上方的高亮 overlay。
- spotlight：覆盖整页并挖出目标元素区域。
- laser：通过元素几何换算成百分比位置绘制激光指示。
- zoom：改变画布 transform，并把目标元素中心作为 transform origin。

媒体元素通过业务侧 resolver 把 IndexedDB object URL、生成任务占位符或服务端 URL 解析为可显示资源。它与纯 SDK renderer 的默认 `<img>/<video>` 行为不同。

### 33.2 元素渲染层

每类元素分成 Base renderer 与主应用 wrapper。Base 层负责 DSL 到视觉的纯映射，例如：

- text：HTML 富文本、字体和行距。
- image：裁剪、滤镜、翻转、轮廓和阴影。
- shape/line：SVG path、渐变、pattern、端点。
- chart：DSL 数据转换为 ECharts option。
- latex：KaTeX/HTML 表达。
- table：单元格、边框、行列尺寸。

主应用 wrapper 再接入选中状态、编辑事件、媒体重试、上下文和业务提示。这种分层让同一元素视觉逻辑可以被播放画布、缩略图和编辑画布复用。

### 33.3 独立 `@openmaic/renderer`

独立 renderer 是只读 React SDK：以 `Slide` props 或 Provider Context 为输入，包含元素渲染与可选特效，允许消费者注入 `renderImage`、`renderVideo` 和 element click。它不依赖主应用 Zustand、Scene Context、i18n、生成任务或 IndexedDB，也不包含 ProseMirror 和编辑操作。

因此它不是主应用编辑器的完整抽包版本，而是“DSL -> 只读画布”的最小可复用闭环。主应用当前仍使用自己的 renderer/editor 子树；两者通过 `@openmaic/dsl` 共享类型，而不是通过运行时相互调用。

## 34. Slide 编辑器与富文本实现

### 34.1 编辑画布状态分层

可编辑 Canvas 同时维护三种状态：

- 课程权威数据：Scene Context 背后的 Stage store。
- 手势中的临时元素快照：组件 `useRef + useState`，供拖拽、缩放、旋转时高频更新，避免每个 pointer move 都写持久化 store。
- UI 交互状态：Canvas store/Keyboard store，保存选择集、当前 handle、创建模式、标尺、网格、快捷键状态等。

单次手势结束后，renderer 会提交完整的新 `SlideContent`。`scene-edit-bridge` 对前后快照做 diff，转换成规范化的 `element.add/delete/update/removeProps` 或 `slide.update` 操作；多元素手势仍合并成一个 undo transaction。这样屏幕交互可以使用高效快照，历史、持久化和导出仍建立在可解释的 edit operations 上。

### 34.2 场景编辑 Surface

`sceneEditorRegistry` 以 `SceneType` 注册编辑 surface，编辑 Chrome 只依赖统一 surface 接口。Slide 和 Quiz 可以提供真正的编辑 session；尚未支持的类型可以注册 noop/read-only surface。这个注册表把“编辑器外壳”与“每种 scene 如何编辑”解耦，未来新增类型不需要重写 StageGrid、导航栏和 Agent Panel。

`StageGrid` 使用 top/left/center/right/bottom 五槽布局：左侧页面导航、中心 scene surface、右侧 Agent、底部 Actions timeline 可以独立增减，而不会改变画布组件层级。

### 34.3 ProseMirror 富文本

文本元素内部使用 ProseMirror，而不是直接把 `contentEditable` 当作数据源。自定义 schema/marks/commands 支持字体、字号、颜色、背景色、粗体、斜体、下划线、删除线、列表、缩进和对齐。

关键同步机制：

- 编辑器输入先在 ProseMirror transaction 中完成，再以 debounce 把 HTML 回写 element content。
- active-editor registry 让浮动格式工具栏可以把命令发送给当前文本元素，或显式定位某个 elementId。
- 编辑文本时临时关闭全局快捷键，避免 Delete/Ctrl+Z 同时被画布与富文本处理。
- selection sync 把当前光标 marks 推到 Canvas store，供工具栏显示实际格式。
- 历史命令会标记 `ignore`，避免 ProseMirror history 与外层 Slide history 重复记录同一操作。

### 34.4 Schema 与兼容

进入 Stage store 边界的 scene 会经过 `migrateScene`：`setScenes`、`addScene`、插页和 IndexedDB 恢复都会归一化内容。当前 Slide schema 为 v1，旧数据补 `schemaVersion`；interactive migration 会移除已经废弃的 `teacherActions` authoring 字段。迁移函数保持纯函数和幂等，并对未知的未来高版本数据选择不降级写回，避免旧客户端破坏新格式。

这与 `@openmaic/dsl` 的空 migration registry 是两层：DSL registry 面向未来 SDK 级破坏性版本，主应用 migrator 处理现在已经发生的业务内容演进。

### 34.5 页面级管理

Pro模式左侧SlideNavRail不只是导航缩略图，还负责整个Deck的页面管理：

- 拖拽Scene重新排序，并重新平衡连续order。
- 在任意两个缩略图之间插入空白Slide。
- 复制Slide或其他Scene。
- 删除Scene，并通过5秒Toast提供单槽撤销。
- 调整导航栏宽度、折叠和切换当前页面。

空白Slide使用1000 × 562.5画布、默认主题和一条空speech action。加入空speech不是占位数据失误，而是保证新页面立即进入可播放/可编辑脚本状态；完全没有actions的页面会在播放中被跳过。

复制Slide时不能只复制Scene ID：实现会重新生成Slide ID、所有element ID和group ID，把spotlight/laser/play_video等cue的elementId映射到新元素，同时重新生成action ID并清除audioId/audioUrl，避免复制页继续播放源页面的缓存语音。嵌套Action数据使用structuredClone，避免两页共享可变对象。

删除时至少保留一个Scene；对于Slide还至少保留一个Slide。回收站只保存最近一次删除及原数组位置，后续删除会覆盖上一条；撤销时检查stageId，避免用户切换课程后把旧Scene插入新课程。

页面创建入口现在由`SCENE_CREATION_ENABLED = true`开启，因为空白页有speech脚本、复制页携带可播放actions。生成尚未完成时不能进入编辑模式，因此order重排不会与outline按order提交发生竞争。

### 34.6 Actions脚本时间线

ActionsBar把`scene.actions[]`直接显示成横向电影剪辑时间线，不创建另一份脚本模型。speech是可编辑片段，spotlight/laser/whiteboard/discussion等是Cue卡片；所有修改最终通过`updateScene(sceneId, { actions })`持久化。

可执行编辑包括：

- 行内修改speech文本、试听、单句TTS重生成和整页音频重生成。
- 从Palette拖入speech、spotlight、laser。
- 拖拽或左右按钮调整Action顺序。
- 删除Action。
- 为spotlight、laser、play_video进入Canvas Pick模式，直接点击Slide元素绑定elementId。
- 编辑discussion的topic、prompt和发起agent。
- 悬停Cue时复用真实spotlight/laser效果做预览，而不是显示假的静态提示。

白板Cue不能从Palette裸添加，因为有效白板脚本需要open -> draw/edit -> close以及位置/内容参数；已有AI生成的白板Actions仍会显示在时间线上。Discussion最多一个且必须位于末尾，插入和移动操作会钳制位置，保持Action Parser与播放引擎的终止Cue约束一致。

时间线操作按actionId而不是易过期的数组index定位，避免拖拽重排和异步TTS完成后修改错卡片。Agent重生成旁白时，正在编辑的draft会采用新的Store状态，防止旧输入在blur时覆盖刚生成的Actions。整页/旁白Agent重生成还保存一次恢复快照，工具卡允许用户撤销本次重生成。

## 35. Provider 配置、同步与优先级

Provider 系统存在“管理员配置”和“浏览器配置”两套来源，不能简单理解为把设置页内容传给所有 API。

### 35.1 服务端配置加载

`lib/server/provider-config.ts` 分别加载 LLM、TTS、ASR、PDF、image、video、web-search：

```text
server-providers.yml 作为基础
  -> 环境变量逐字段覆盖 YAML
  -> 形成进程内 server provider registry
  -> API route 只从 registry 取密钥和真实 baseUrl
```

Ollama、Lemonade 等 keyless provider 可以仅凭 server-side baseUrl 激活。环境变量优先于 YAML；别名如 MIMO/XIAOMI、TENCENT/TENCENT_HUNYUAN 会归一到同一 provider ID。

### 35.2 浏览器同步

根布局中的 `ServerProvidersInit` 请求 `/api/server-providers`，把“哪些 provider 由服务器托管”合并进 Zustand settings。响应只暴露 provider ID、model 等安全元数据和 managed/disabled 状态，不把密钥或真实托管 baseUrl 下发浏览器。

同步后的含义是：

- managed provider 可在没有客户端 key 的情况下使用。
- route 收到 managed provider 时忽略客户端伪造或过期的 key/baseUrl，以服务端 registry 为准。
- 非 managed provider 才使用浏览器本地保存的 key/baseUrl。
- TTS 额外支持管理员 `enabled: false` 或 `TTS_*_ENABLED=false` 强制禁用；当前该“服务端强制关闭”机制只对 TTS 完整实现。

Settings store 每次 rehydrate 都会补齐新版本新增的 built-in provider、迁移旧存储结构并清理不应继续保留的 managed baseUrl。首次 server sync 可以自动选择可用 provider，但用户后续的显式 opt-out 不应在每次刷新时被覆盖。

### 35.3 模型解析优先级

LLM route 的有效选择顺序是：

```text
MODEL_ROUTES 中的 stage route
  -> 请求携带的 x-model/客户端模型（仅未路由阶段）
  -> DEFAULT_MODEL
  -> 无可用模型则报配置错误，不硬编码厂商兜底
```

已路由阶段代表管理员明确决策，模型和可选 thinking 配置优先于浏览器。未路由阶段继续尊重客户端模型与思考配置。该规则解释了为什么同一个浏览器设置在整课 API、Agent 工具和普通交互生成中可能得到不同的最终模型。

## 36. SDK 包边界总览

当前 workspace SDK 不是一个包含全部应用能力的“大包”，而是按稳定性拆分：

- `@openmaic/dsl`：零运行时依赖，提供 Slide/Stage/Scene/Action 类型、guards 和版本脚手架。
- `@openmaic/renderer`：React 只读画布，消费 DSL；不包含编辑、业务存储和生成流程。
- `@openmaic/importer`：PPTX OOXML -> Slide 转换；SDK 能力完成，但主应用落库尚未接通。
- `pptxgenjs`：本仓 fork 的 Office 导出底层。
- `mathml2omml`：公式向 Office OMML 转换。
- `packages/docs`：独立文档站，有自己的 lockfile，已从根 pnpm workspace 排除，避免与主应用依赖生命周期耦合。

依赖方向保持为 DSL 在最底层，renderer/importer 消费 DSL，主应用组合所有包。PPTX exporter 尚未形成对应的 `@openmaic/exporter` 包，当前导出仍在主应用 `lib/export` 中。这种拆分优先抽离稳定的数据契约和只读能力，把变化更快的编辑器、Agent 和课堂运行时留在应用层。

## 37. 一句话总结

OpenMAIC 的核心架构可以概括为：

```text
用户需求/文档
  -> 大纲规划
  -> 多类型页面生成
  -> 讲课动作脚本生成
  -> 媒体/语音补全
  -> Stage/Scene/Action 课堂包
  -> 播放器按 Action 驱动多智能体课堂体验
```

它的关键设计不是“生成一组静态页面”，而是把课堂建模成可播放、可编辑、可导入导出的结构化教学程序。
