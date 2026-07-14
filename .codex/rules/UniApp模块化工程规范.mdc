---
description: UniApp 模块开发规范与 AI 智能体工作区配置
alwaysApply: true
---

# UniApp 模块开发规范智能体配置工作区 (UniApp Workspace Rules)

这份文档是专门为你（AI 智能体 / 编程助手）整理的当前项目全局上下文与开发规范。在处理本项目任务前，请自动读取并遵循本文件，严格按照本规范输出代码，避免对项目组件库、Lint 规则、架构分层产生误判。

---

## 0. AI 通用编码与行为准则

**权衡：** 这些指南偏向谨慎而非速度。对于琐碎任务，请自行判断。

### 0.1 先思考再编码
**不要假设。不要隐藏困惑。暴露权衡。**
在实现之前：
- 明确陈述你的假设。如果不确定，请提问。
- 如果存在多种解释，请列出来——不要默默选择一种。
- 如果有更简单的方法，请说出来。在合理时提出反对意见。
- 如果有不清楚的地方，停下来。指出困惑之处。提问。

### 0.2 简单至上
**解决所需问题的最少代码。不做推测性工作。**
- 不要超出要求范围的功能。
- 不要为一次性使用的代码创建抽象。
- 不要添加未经要求的"灵活性"或"可配置性"。
- 不要为不可能发生的场景编写错误处理。
- 如果你写了 200 行代码而本可以只写 50 行，请重写。
- 问自己："资深工程师会觉得这过度复杂了吗？" 如果是，就简化。

### 0.3 手术式修改
**只触及你必须改的地方。只清理你自己造成的混乱。**
在编辑现有代码时：
- 不要"改进"相邻的代码、注释或格式。
- 不要重构没有问题的东西。
- 匹配现有风格，即使你个人会有不同做法。
- 如果你注意到不相关的死代码，可以提一下——但不要删除它。
当你的修改产生了孤立代码：
- 删除因**你的修改**而变得未使用的导入/变量/函数。
- 除非被要求，否则不要删除预先存在的死代码。
- **检验标准**：每一行修改都应该直接追溯到用户的需求。

### 0.4 目标驱动执行
**定义成功标准。循环执行直到验证通过。**
将任务转化为可验证的目标：
- "添加校验" → "为无效输入编写测试，然后让测试通过"
- "修复这个 bug" → "编写一个能复现它的测试，然后让测试通过"
- "重构 X" → "确保重构前后测试都能通过"
对于多步骤任务，陈述一个简要计划：
1. [步骤] → 验证: [检查点]
2. [步骤] → 验证: [检查点]
3. [步骤] → 验证: [检查点]

---

## 一、模块概述

本规范适用于智慧悦读微信小程序项目的所有业务模块开发。项目基于 **UniApp + Vue 3 + TypeScript** 构建，支持多平台编译（主要面向微信小程序）。

### 注释规范

#### 强制要求
- 所有新增或修改的模块页面、hooks、types、api 定义，必须补充清晰的中文注释，并且必须严格执行
- 注释内容不能只写“做了什么”，还必须说明“字段是什么意思”“状态变量的用途”“关键逻辑为什么这样处理”，确保后续联调和维护时可快速理解
- 接口相关的 `type`、`interface`、请求参数、返回字段，必须补充字段含义注释；页面中的关键计算属性、状态变量、分支渲染、分页逻辑、事件方法，也必须补充必要的中文注释
- 对于通用性较强、业务含义不直观、容易误用或容易在联调时产生歧义的字段，必须优先补充注释，不允许省略
- 注释应简洁、准确、贴近业务语义，禁止编写无意义注释，如“定义变量”“设置值”等低质量描述

#### 适用说明
- 本规则适用于新功能开发，也适用于对已有模块进行二次修改、重构、联调修复
- 如果修改了已有逻辑但原文件缺少必要注释，需要一并补齐，不可只改逻辑不补说明

### 样式规范

#### 颜色函数写法
- 所有新增或修改的 `css`、`scss`、`less` 样式代码，涉及颜色函数时必须使用项目当前 lint 兼容的旧式函数写法
- 例如透明色必须优先写成 `rgba(255, 255, 255, 0.25)`，不要写成 `rgb(255 255 255 / 25%)`
- 渐变、阴影、边框、背景色等场景中的颜色函数，同样必须遵守该规则
- 编写样式时要主动规避 `color-function-notation` 相关报错，保证生成代码可直接通过项目规范校验

#### 选择器复用与合并
- 所有新增或修改的 `css`、`scss`、`less` 样式代码，必须主动规避 `no-duplicate-selectors` 相关报错
- 同一个样式文件内，如果已经定义过某个选择器（如 `.card`、`.page-header`、`.personalized-recommend`），后续新增样式时必须优先合并到原选择器块中，不允许在同级作用域重复声明同名选择器
- 即使是 `scoped` 样式，也不能因为“局部生效”而重复定义同名选择器；应统一收口到一个选择器块内管理
- 修改已有页面样式时，先搜索该选择器是否已经存在，再决定是在原块内追加，还是抽取为子元素 / 修饰符类，避免 LLM 直接补一段新的同名样式
- 如需扩展同一模块的不同状态样式，优先使用嵌套写法、`&--modifier`、子元素类或工具类，不要复制一段同名根选择器

### 弹层挂载规范

#### 微信小程序弹层位置要求
- 所有新增或修改的 `tui-actionsheet`、`tui-modal`、`xxt-common-modal`、`tui-bottom-popup` 等弹层组件，在微信小程序页面中必须优先挂载到**页面层**或页面直属容器层，不允许直接挂在局部列表项组件、卡片组件、区块组件内部
- 原因是微信小程序下局部组件内的弹层容易受到父级滚动容器、层级和裁切范围影响，导致弹窗只显示在局部区域，不能覆盖整个页面
- 推荐做法是：业务子组件只负责抛出“打开弹窗”“确认操作”所需事件和数据，由页面容器统一接收后渲染弹层，并在页面层执行显隐控制
- 如果弹层交互只服务于某个区块，也仍然应把**展示节点**放在页面层，不能因为“作用范围小”就把弹层直接写进区块组件模板中

## 二、组件库

项目使用以下公共组件库，均位于 `src/uni-module-common/components/` 目录下：

### 2.1 ThorUI 组件库

项目中使用频率较高的 ThorUI 组件：

| 组件                 | 使用次数 | 说明                               |
| -------------------- | -------- | ---------------------------------- |
| `tui-text`           | 402      | 文本显示，支持多种样式、对齐、行高 |
| `tui-icon`           | 256      | 图标，支持 ThorUI 图标和 iconfont  |
| `tui-form-button`    | 140      | 表单按钮，支持加载状态、禁用等     |
| `tui-bottom-popup`   | 64       | 底部弹出层                         |
| `tui-col`            | 58       | 栅格布局列                         |
| `tui-input`          | 46       | 输入框                             |
| `tui-modal`          | 39       | 模态框                             |
| `tui-lazyload-img`   | 39       | 图片懒加载，支持占位图、淡入动画   |
| `tui-textarea`       | 25       | 文本域                             |
| `tui-row`            | 25       | 栅格布局行                         |
| `tui-actionsheet`    | 25       | 操作菜单                           |
| `tui-dialog`         | 17       | 对话框                             |
| `tui-checkbox`       | 15       | 复选框                             |
| `tui-tabs`           | 12       | 标签页                             |
| `tui-switch`         | 9        | 开关                               |
| `tui-navigation-bar` | 9        | 自定义导航栏                       |
| `tui-button`         | 9        | 按钮                               |
| `tui-radio`          | 8        | 单选框                             |
| `tui-datetime`       | 8        | 日期时间选择                       |
| `tui-searchbar`      | 5        | 搜索栏                             |
| `tui-tag`            | 5        | 标签                               |

> 更多组件可在 `src/uni-module-common/components/thorui/` 目录下查看

### 2.2 XXT 组件库

项目封装了多个业务组件，位于 `xxt-components/` 目录下，常见组件包括：

| 组件                 | 说明                |
| -------------------- | ------------------- |
| `xxt-common-unlogin` | 登录/加班级拦截容器 |
| `xxt-common-title`   | 模块标题组件        |
| `xxt-empty`          | 空状态显示          |
| `xxt-file-submit`    | 文件提交组件        |
| `xxt-img-sign`       | 图片签章            |
| `xxt-notice-card`    | 通知卡片            |
| `xxt-tabs-bottom`    | 底部标签页          |
| `xxt-task-card`      | 任务卡片            |
| `xxt-text-overflow`  | 文本折叠/展开组件   |

> 更多组件可在 `src/uni-module-common/components/` 目录下查看

#### 2.2.1 常用 XXT 业务组件说明

以下组件在业务页面中使用频率较高。

> **重要原则：如果页面需求与下列组件能力匹配，必须优先考虑使用 XXT 业务组件库，不要重复自建同类业务组件。**
>
> 只有在现有业务组件无法满足需求时，才允许在当前模块内继续封装新的页面级/模块级组件。

**`xxt-common-unlogin`**
- **组件作用**：用于页面级登录态拦截、未加入班级拦截，以及统一承载登录后的页面内容
- **适用场景**：
  - 需要先登录才能访问的页面
  - 学生未加入班级时需要展示“加入班级”引导的页面
  - 页面最外层需要统一包裹登录态判断时
- **常用属性**：
  - `login-url`：登录成功后的回跳地址
  - `is-show-add-class`：是否展示“加入班级”引导
  - `stu-add-class-h5-url`：加入班级 H5 地址
  - `class-style-name`：内容容器样式类名
- **参考写法**：
```vue
<template>
  <xxt-common-unlogin :login-url="loginUrl" :is-show-add-class="true">
    <view class="page-container">
      页面内容
    </view>
  </xxt-common-unlogin>
</template>

<script setup lang="ts">
const loginUrl = '/pages/index/index';
</script>
```

**`xxt-common-title`**
- **组件作用**：用于页面区块标题展示，支持左侧标记图、标题文案和右侧扩展插槽
- **适用场景**：
  - 模块标题
  - 卡片区块标题
  - 页面内带“查看更多/操作按钮”的标题行
- **常用属性**：
  - `title`：标题文本
  - `title-font-size`：标题字号
  - `title-color`：标题颜色
  - `is-show-mark`：是否显示左侧标记
  - `mark-src`：左侧标记图地址
- **插槽说明**：
  - `left-extra`：标题后追加内容
  - `right`：标题行右侧内容
- **参考写法**：
```vue
<template>
  <xxt-common-title title="同学畅聊" :title-font-size="16">
    <template #right>
      <view class="more-btn">更多</view>
    </template>
  </xxt-common-title>
</template>
```

**`xxt-text-overflow`**
- **组件作用**：用于长文本折叠、省略和展开收起展示，适合推荐语、简介、评论内容等文本场景
- **适用场景**：
  - 推荐语
  - 图书简介
  - 评论/动态正文
  - 多行文本需要“展开/收起”交互时
- **常用属性**：
  - `text`：文本内容
  - `clamp`：默认折叠显示的行数
  - `show-hidden-btn`：是否显示“展开详情/收起详情”按钮
  - `show-expand-btn`：是否显示“展开/收起”按钮
  - `default-hide`：是否默认展开
- **组件特点**：
  - 内部已处理文本是否超出的判断
  - 支持插槽扩展首部内容和自定义底部展示
  - 适合替代手写多行省略和展开逻辑
- **参考写法**：
```vue
<template>
  <xxt-text-overflow
    :text="desc"
    :clamp="3"
    :show-hidden-btn="true"
  ></xxt-text-overflow>
</template>

<script setup lang="ts">
const desc = ref('这里是需要折叠展示的长文本内容');
</script>
```

**`xxt-empty`**
- **组件作用**：用于页面空状态展示，支持默认文案、默认空态图片、自定义提示内容和插槽扩展按钮
- **适用场景**：
  - 列表为空
  - 页面暂无数据
  - 搜索无结果
  - 模块数据为空时的占位展示
- **适用建议**：
  - 只要页面是“暂无数据 / 暂无资源 / 暂无内容”的空态展示，优先使用该组件
  - 不要在业务页面里重复手写空图片 + 提示文案结构

**`xxt-file-submit`**
- **组件作用**：用于附件选择、附件展示、附件删除和附件上传，支持图片、语音、视频、文件、链接等类型
- **适用场景**：
  - 发评论
  - 发动态
  - 提交通知附件
  - 提交作业附件
  - 任意需要“选择附件 + 上传文件”的页面
- **适用建议**：
  - 页面只要涉及文件上传、图片上传、音视频上传，优先使用该组件
  - 不要在业务页面里重复实现附件选择、预览、删除和上传链路

**`xxt-img-sign`**
- **组件作用**：用于图片签章/签名区域展示与拖拽定位，支持多签名、缩放、移动和签章区域数据记录
- **适用场景**：
  - 作业签名
  - 图片签章
  - 需要在图片指定区域进行签字或盖章的页面
- **适用建议**：
  - 只要业务中涉及“图片上签名/盖章/拖动签章位置”，优先使用该组件
  - 不要在页面中自行实现 `movable-view` 签名区域逻辑

**`xxt-notice-card`**
- **组件作用**：用于通知卡片展示，封装了通知类型、标题、发送对象、截止时间、正文摘要、附件信息和操作按钮
- **适用场景**：
  - 通知列表
  - 收到的通知
  - 已发送通知
  - 通知详情前的卡片概览区
- **适用建议**：
  - 只要页面是通知类卡片列表或通知概览展示，优先使用该组件
  - 不要重复手写通知标题、截止时间、反馈情况、去完成按钮等业务结构

**`xxt-tabs-bottom`**
- **组件作用**：用于底部标签导航展示，支持图标、文字、选中态、禁用态和底部安全区适配
- **适用场景**：
  - 底部 tab 页面
  - 底部功能切换栏
  - 页面底部固定导航入口
- **适用建议**：
  - 如果页面是底部 tab 导航结构，优先使用该组件
  - 不要重复自定义底部导航的选中态、安全区高度和点击切换逻辑

**`xxt-task-card`**
- **组件作用**：用于任务/作业卡片展示，封装了任务类型、标题、截止时间、正文摘要、附件信息、完成状态和学生操作按钮
- **适用场景**：
  - 作业列表
  - 任务中心列表
  - 待完成任务卡片
  - 教师/学生任务概览
- **适用建议**：
  - 只要业务是任务、作业、待办类卡片列表，优先使用该组件
  - 不要重复实现任务状态、截止时间、完成情况、开始作业按钮等结构

**`xxt-common-title`、`xxt-text-overflow`、`xxt-empty` 的优先级补充说明**
- 模块标题、区块标题：优先使用 `xxt-common-title`
- 长文本折叠、多行省略：优先使用 `xxt-text-overflow`
- 空状态页面、空列表占位：优先使用 `xxt-empty`

**常用组件优先使用原则总结**
- 登录拦截页面：优先 `xxt-common-unlogin`
- 模块标题区：优先 `xxt-common-title`
- 空状态：优先 `xxt-empty`
- 上传附件：优先 `xxt-file-submit`
- 图片签章：优先 `xxt-img-sign`
- 通知卡片：优先 `xxt-notice-card`
- 底部 tab：优先 `xxt-tabs-bottom`
- 任务卡片：优先 `xxt-task-card`
- 长文本折叠：优先 `xxt-text-overflow`

## 三、开发规范

### 3.1 组件化开发原则

> **核心原则：优先使用公共组件库，遵循组件化开发思想**

> **强制要求：必须组件化开发，做到 UI 与业务逻辑分离**
>
> - 页面 `.vue` 文件只负责页面编排、路由参数承接、顶层容器样式和组件组合，不允许把大段业务 UI、列表项、卡片、弹窗、表单等结构直接堆在页面文件中
> - 页面中的独立 UI 区块必须拆到当前页面或当前模块的 `components` 文件夹中，例如个人信息卡片、排行榜表格、图书卡片、筛选栏、弹窗、空状态区块等
> - 组件文件必须专注 UI 展示和必要的组件交互，通过 props 接收数据，通过 emit 通知父级，不允许在组件中直接散落页面级路由参数解析、复杂请求编排或跨模块业务状态
> - 页面或组件中出现可复用的状态管理、接口请求、数据格式化、分页、筛选、跳转编排、事件处理等业务逻辑时，必须抽离到当前页面或当前模块的 `hooks` 文件夹中
> - hooks 必须以 `use-xxx.ts` 或清晰业务名命名，导出状态、计算属性和方法；页面通过调用 hooks 获取数据与事件方法，再传给组件展示
> - 新增 API 请求必须放在当前模块的 `hooks` 目录内，禁止直接写在页面或组件 `.vue` 文件中
> - 禁止为了图省事把“请求接口 + 数据处理 + 大段模板 + 大段样式”全部写在同一个页面文件里；后续维护时如发现页面承担过多职责，应优先拆分为 `components + hooks`

推荐目录结构：

```text
pages/example-page/
├── components/
│   ├── example-filter-bar.vue
│   ├── example-list-card.vue
│   └── example-confirm-popup.vue
├── hooks/
│   ├── example-api.ts
│   └── use-example-page.ts
└── example-page.vue
```

推荐职责划分：

```text
example-page.vue              页面容器：读取路由参数、调用 hooks、组合组件
components/*.vue              UI 组件：接收 props、渲染视图、emit 用户操作
hooks/example-api.ts          接口定义：请求入参、返回类型、ajax 方法
hooks/use-example-page.ts     业务逻辑：状态、分页、筛选、请求编排、跳转方法
```

开发时应具有组件化开发意识，遵循以下优先级：

1. **优先使用公共组件库**：首先在 `src/uni-module-common/components/` 下查找是否有可复用的组件（ThorUI 或 XXT 组件）
2. **封装通用组件**：如果现有组件无法满足需求，应在当前模块的 `components` 文件夹中封装可复用的组件
3. **页面级组件**：放置在当前页面目录下的 `components` 文件夹中
4. **模块级组件**：放置在模块根目录的 `components` 文件夹中
5. **页面级逻辑 Hooks**：页面相关的请求编排、状态、计算属性和事件方法，放置在当前页面目录下的 `hooks` 文件夹中
6. **模块级逻辑 Hooks**：多个页面复用的业务逻辑，放置在当前模块根目录的 `hooks` 文件夹中

> **⚠️ 重要警告：禁止向公共模块提交代码**
>
> - **禁止修改** `src/uni-module-common/` 目录下的任何代码，该公共模块在多个项目中使用，修改可能影响其他业务
> - 如果组件或逻辑需要公用，应在自己的模块根目录下新建 `hooks` 或 `components` 目录来存放
> - 业务模块应保持独立性，避免对公共模块产生依赖

> 注意：
> - 如果在 `components` 目录中进行开发，则无需再创建 `components` 子目录
> - 封装新组件时，应考虑该组件是否可以在其他模块中复用
> - 如果在 `hooks` 目录中进行开发，则无需再创建 `hooks` 子目录；业务逻辑应按页面或业务域拆分，避免形成超大 hooks 文件

### 3.2 文件命名规范

| 类型     | 命名规范   | 示例                                                  |
| -------- | ---------- | ----------------------------------------------------- |
| 页面     | kebab-case | `act-ranking.vue`、`index-evaluation.vue`             |
| 组件     | kebab-case | `home-detail-item.vue`、`read-index-canvas-model.vue` |
| Hooks    | kebab-case | `evaluation-api.ts`、`use-evaluation.ts`              |
| 类型文件 | kebab-case | `evaluation-types.ts`、`activity-types.ts`            |

补充要求：

- 本工程是可直接复用的脚手架工程，新增或改造代码时，文件名、类型名、方法名、变量名、组件文案、注释和日志中不要使用 `template`、`Template`、`模板` 等“模板示例”表达；除 Vue 固定语法 `<template>` 或历史外部路径无法修改的情况外，应使用正常项目命名，例如 `request`、`auth`、`login`、`page`、`profile`、`currentUser` 等。
- 不要把页面、接口、登录、请求、启动等能力命名成“模板能力”或“模板示例”；这些能力都应被视为当前工程的正式基础能力，后续业务前端工程会直接沿用。

### 3.2.1 组件 Props 传参规范

页面与组件之间的传参，**必须使用 `script setup + TypeScript` 的显式类型声明方式**，禁止无类型、弱类型或默认值不完整的 props 写法。

- **必须使用 `defineProps` 声明 props 类型**
- **props 必须配合 `withDefaults` 使用，给默认值**
- **对象、数组类型的默认值必须使用工厂函数返回**
- **复杂业务数据优先收敛为单个对象 prop，并为对象内部字段补齐默认值**
- **布尔值、数字、字符串等基础类型，也应提供清晰默认值，避免模板中出现大量空值判断**
- **props 命名在脚本中使用 camelCase，在父组件模板中传参使用 kebab-case**
- **禁止直接修改 props，若需变更应通过 emit 通知父组件**

推荐写法：

```ts
const props = withDefaults(
  defineProps<{
    recommendInfo: {
      id: string;
      libraryId?: string;
      userName: string;
      userSubName?: string;
      userAvatar: string;
      isHot: boolean;
      bookName: string;
      bookAuthor: string;
      bookImage: string;
      bookDescription: string;
      createTime: string;
      fileList?: any[];
      isLiked: boolean;
      praiseCount: number;
      isAiCreate?: boolean;
      canAccess?: boolean;
      isbn?: string;
      libraryBookId?: string;
      canAccessBook?: boolean;
      isMyLibrary?: boolean;
      status?: number;
    };
    isShowMore?: boolean;
    isShowBookTag?: boolean;
    imgUrls: any[];
    currentIndex: number;
    imgShow: boolean;
    auditReason?: string;
  }>(),
  {
    recommendInfo: () => ({
      id: '',
      libraryId: '',
      userName: '',
      userSubName: '',
      userAvatar: '',
      isHot: false,
      bookName: '',
      bookAuthor: '',
      bookImage: '',
      bookDescription: '',
      createTime: '',
      fileList: [],
      isLiked: false,
      praiseCount: 0,
      isAiCreate: false,
      canAccess: false,
      isbn: '',
      libraryBookId: '',
      canAccessBook: false,
      isMyLibrary: false,
      status: 0
    }),
    isShowMore: false,
    isShowBookTag: false,
    imgUrls: () => [],
    currentIndex: 0,
    imgShow: false,
    auditReason: ''
  }
);
```

父组件模板传参示例：

```vue
<recommend-card
  :recommend-info="item"
  :is-show-more="true"
  :img-urls="previewUrls"
  :current-index="currentIndex"
  :img-show="imgShow"
  @click-image="handleClickImage"
  @update:img-urls="handleUpdateImgUrls"
/>
```

补充要求：

1. 如果 prop 字段较多，优先在当前模块 `types/` 中单独定义类型，再在组件中引入
2. 如果 prop 仅服务当前页面，可直接在组件内联声明类型，但必须保留字段注释
3. 能明确类型时不要使用 `any`；仅在三方数据结构未稳定前，才允许临时使用 `any`

### 3.3 API 请求规范

所有模块的 API 接口必须统一写在当前模块的 `hooks` 目录中，禁止把业务接口散落在页面 `.vue` 文件里。

- **推荐命名方式**：
  - 单一接口文件可命名为 `api-hooks.ts`
  - 按业务域拆分时，可命名为 `xxx-api.ts`、`xxx-request-api.ts`
- **推荐原则**：
  - 小模块可集中写在一个 API 文件中
  - 复杂模块可按业务域拆成多个 API 文件
  - 但都必须放在当前模块自己的 `hooks/` 目录下

#### 3.3.1 API 文件结构

```typescript
import ajax from '@/uni-module-common/http';

/**
 * ====== 类型定义 ======
 */

/**
 * 接口返回数据示例
 */
export interface ApiResponse<T = any> {
  code: number;
  data: T;
  message: string;
}

/**
 * 用户信息
 */
export interface UserInfo {
  id: string;
  name: string;
  avatar: string;
}

/**
 * ====== 接口方法 ======
 */

/**
 * 获取用户信息
 * 接口ID: XXXXX
 *
 * @param data 请求参数
 * @param data.id 用户ID
 * @returns Promise<UserInfo> 用户信息
 */
export function getUserInfoApi(data: { id: string }): Promise<UserInfo> {
  return ajax({
    url: '/api/user/info',
    method: 'GET',
    data,
    custom: {
      showLoading: false
    }
  });
}

/**
 * 提交表单数据
 * 接口ID: XXXXX
 *
 * @param data 表单数据
 * @returns Promise<{ success: boolean }> 提交结果
 */
export function submitFormApi(data: any): Promise<{ success: boolean }> {
  return ajax({
    url: '/api/form/submit',
    method: 'POST',
    data,
    custom: {
      showLoading: true
    }
  });
}
```

#### 3.3.2 API 规范要点

1. **类型定义**：所有接口数据类型必须在文件顶部单独定义区域声明
2. **注释规范**：
   - 每个接口必须包含接口ID（如果有）
   - 使用 JSDoc 格式注释
   - 说明请求参数和返回值
3. **请求配置**：
   - `showLoading`: 查询类接口设为 `false`，提交类接口设为 `true`
   - 使用 `POST` 请求时通过 `data` 传递参数
   - 使用 `GET` 请求时同样通过 `data` 传递参数

#### 3.3.3 接口失败提示规范

- 项目公共请求层 `src/uni-module-common/http/index.ts` 已统一处理业务失败提示；当接口返回业务失败结果时，会在请求层通过 `uni.showToast` 优先展示后端返回的 `res.message`
- 因此模块页面、业务 hooks、当前模块 API 文件中，**禁止再重复编写失败提示弹窗、失败 toast、失败对话框或额外的 message 透传逻辑**
- 业务 hooks 在接口失败时，应优先做状态复原、流程中断、错误日志记录等必要处理；不要再额外返回“提交失败”“兑换失败”“请稍后重试”等通用失败提示给页面层二次弹出
- 页面层在调用接口或业务 hooks 后，失败分支应避免再次手写统一失败文案，防止与请求层提示重复，造成双重弹窗或提示不一致
- 只有在产品明确要求“特殊失败交互”时，才允许单独自定义失败提示；如果确实需要自定义失败提示，必须先关闭请求层默认失败提示能力（如 `showError: false`），并在代码注释中说明原因，避免重复提示

### 3.4 业务逻辑抽离

除 API 外的其他业务逻辑，需要单独抽离到 hooks 文件夹中：

```
hooks/
├── api-hooks.ts / xxx-api.ts  # API 接口
├── use-xxx.ts             # 组合式逻辑
├── use-form.ts            # 表单处理
└── use-list.ts            # 列表处理
```

```typescript
// hooks/use-evaluation.ts 示例
import { ref, computed } from 'vue';

export function useEvaluation() {
  const score = ref(0);
  const maxScore = ref(100);

  const percentage = computed(() => {
    return Math.round((score.value / maxScore.value) * 100);
  });

  function reset() {
    score.value = 0;
  }

  return {
    score,
    percentage,
    reset
  };
}
```

#### 3.4.1 路由对象使用限制

- `const router = useRouter();` 必须声明在 `.vue` 文件的 `script setup` 或组件脚本上下文中，禁止在当前模块的 `hooks`、普通 `ts` 工具文件、类型文件或其他非 Vue 组件上下文中直接调用
- 原因：`useRouter()` 依赖当前 Vue 组件实例上下文；如果在非 Vue 上下文中执行，容易报错：
  `Uncaught Error: useRouter 只可以在 Vue 上下文中使用，请确保你已经正确地注册了 "uni-mini-router" 并且当前正处于 Vue 上下文中`
- 推荐做法：
  - 在页面或组件 `.vue` 文件中先声明 `const router = useRouter();`
  - 如需在 hooks 中使用跳转能力，应由 `.vue` 文件把 `router` 或跳转方法作为参数传入 hooks，而不是在 hooks 内部自行调用 `useRouter()`
- 适用范围：
  - 页面组件
  - 页面级子组件
  - 模块级业务组件
  - 不适用于独立 `hooks/*.ts`、`api` 文件、`types` 文件

### 3.5 确认弹窗规范

凡是页面中的**二次确认弹窗**（如删除、退出、解绑、确认提交等），**必须统一使用公共组件 `xxt-common-modal`**，禁止直接使用 `uni.showModal` 作为业务确认弹窗。

- **组件路径**：`src/uni-module-common/components/xxt-components/xxt-common-modal/xxt-common-modal.vue`
- **适用场景**：退出确认、删除确认、解绑确认、操作前二次提醒
- **推荐做法**：
  - 页面中使用 `ref(false)` 控制弹窗显示状态
  - `@confirm-click` 中执行确认逻辑
  - `@close-click` 中关闭弹窗
  - 弹窗文案通过 `modal-title` 和 `modalContent` 插槽传入

参考写法：

```vue
<template>
  <xxt-common-modal
    :show-modal="showConfirmModal"
    modal-title="提示"
    :is-show-cancel-btn="true"
    confirm-text="确定"
    btn-width="240rpx"
    @confirm-click="handleConfirm"
    @close-click="showConfirmModal = false"
  >
    <template #modalContent>
      <view class="modal-content">确定执行当前操作吗？</view>
    </template>
  </xxt-common-modal>
</template>

<script setup lang="ts">
const showConfirmModal = ref(false);

const handleConfirm = async () => {
  showConfirmModal.value = false;
  // 执行确认逻辑
};
</script>
```

### 3.6 Lint 规范

所有新增或修改代码，**必须先满足项目现有 ESLint / Stylelint / TypeScript 规则**，禁止提交“功能能跑但 lint 报错”的代码。

#### 3.6.1 Vue 自定义事件命名规范

在 `script setup` 中使用 `defineEmits` 和 `emit` 时，**自定义事件名必须使用 camelCase**。

- 正确：`wantRead`、`moreAction`、`wholeBookTest`、`exitGroup`、`selectBook`
- 错误：`want-read`、`more-action`、`whole-book-test`、`exit-group`、`select-book`

参考写法：

```vue
<template>
  <child-comp
    @want-read="handleWantRead"
    @more-action="handleMoreAction"
  />
</template>

<script setup lang="ts">
const emit = defineEmits<{
  (e: 'wantRead', value: string): void;
  (e: 'moreAction'): void;
}>();

const handleClick = () => {
  emit('wantRead', '1');
  emit('moreAction');
};
</script>
```

#### 3.6.2 模板事件监听规范

在 `.vue` 模板中，`v-on` / `@` 监听事件时，**事件名必须使用 kebab-case**，即使其在 `defineEmits` 中声明为 camelCase。

- 正确：`@want-read="..."`、`@more-action="..."`、`@whole-book-test="..."`
- 错误：`@wantRead="..."`、`@moreAction="..."`、`@wholeBookTest="..."`

> 约定总结：
> - `defineEmits` / `emit`：使用 **camelCase**
> - 模板 `@事件名`：使用 **kebab-case**

#### 3.6.3 组件事件声明与双向绑定规范

子组件事件必须使用 `defineEmits` 显式声明，**事件名、参数类型、双向绑定事件都必须清晰可追踪**。

- **必须使用 `defineEmits<...>()` 声明事件签名**
- **事件名在脚本中使用 camelCase**
- **模板监听时统一写成 kebab-case**
- **涉及双向绑定时，统一使用 `update:xxx` 命名**
- **`update:` 事件中，`xxx` 在脚本里保持 camelCase，在模板里监听时改为 kebab-case**
- **事件参数必须声明类型；类型明确时禁止直接写 `any`**
- **业务动作事件语义要清晰，禁止使用 `click`、`change`、`submit` 这类过于泛化且无上下文的名字作为自定义事件名**

推荐写法：

```ts
const emit = defineEmits<{
  (e: 'like', value: any): void;
  (e: 'more', value: string): void;
  (e: 'clickImage', value: number): void;
  (e: 'update:imgUrls', value: any[]): void;
  (e: 'update:currentIndex', value: number): void;
  (e: 'update:imgShow', value: boolean): void;
  (e: 'handleUserInfoClick', value: any): void;
  (e: 'handleBookInfoClick', value: any): void;
}>();
```

模板监听示例：

```vue
<recommend-card
  @like="handleLike"
  @more="handleMore"
  @click-image="handleClickImage"
  @update:img-urls="handleUpdateImgUrls"
  @update:current-index="handleUpdateCurrentIndex"
  @update:img-show="handleUpdateImgShow"
  @handle-user-info-click="handleUserInfoClick"
  @handle-book-info-click="handleBookInfoClick"
/>
```

> 双向绑定补充约定：
> - 子组件内部更新外部状态时，不允许直接改 props
> - 必须通过 `emit('update:xxx', value)` 通知父组件
> - 父组件如需透传多个可变状态，优先保持 `prop + update:prop` 的成对设计

#### 3.6.4 变量与函数声明顺序规范

必须遵循 **先声明，后使用** 原则，避免触发 `@typescript-eslint/no-use-before-define`。

- `ref`、`computed`、`const`、工具函数、hooks 返回值中依赖的变量，都要先定义再传入
- 特别是组合式 hooks 的入参如果依赖某个 `ref`，该 `ref` 必须写在 hook 调用之前

错误示例：

```ts
const { submit } = usePublish({
  getId: () => currentId.value
});

const currentId = ref(0);
```

正确示例：

```ts
const currentId = ref(0);

const { submit } = usePublish({
  getId: () => currentId.value
});
```

#### 3.6.5 提交前自检要求

生成或修改页面、组件、hooks 后，必须自检以下内容：

1. 自定义事件是否符合“脚本 camelCase、模板 kebab-case”
2. 是否存在变量、`ref`、函数先使用后声明的问题
3. 是否新增了明显的 ESLint / TypeScript / Stylelint 报错
4. 如果只是局部修改，优先至少检查本次改动涉及的文件
5. 组件 props 是否使用了显式类型声明，且默认值是否通过 `withDefaults` 完整补齐
6. 对象/数组类型默认值是否使用了工厂函数
7. 组件事件是否使用了 `defineEmits` 显式声明，`update:` 事件是否符合命名规范

> 后续所有代码生成与修改，默认必须遵循本节 lint 规范。

## 四、组件使用规范

### 4.1 列表滑动组件

当页面需要实现**上拉加载**、**下拉刷新**功能时，使用 `scroll-view` 组件，参考如下：

```vue
<template>
  <scroll-view
    class="uni-scroll-view"
    lower-threshold="100"
    scroll-y
    :refresher-enabled="true"
    :refresher-triggered="trigger"
    :show-scrollbar="false"
    :style="{
      paddingBottom: '0px',
      height: scrollHeight
    }"
    @refresherrefresh="getFirstPage"
    @scrolltolower="loadMoreData"
  >
    <item-component
      v-for="item in listData"
      :key="item.id"
      :data="item"
    />

    <view v-show="listData.length > 0 && listData.length >= totalRow" class="loading-more">
      已经到底了
    </view>
  </scroll-view>
</template>

<script lang="ts" setup>
import { ref } from 'vue';

// 下拉刷新状态
const trigger = ref(false);
const loading = ref(true);

// 列表数据
const listData = ref<any[]>([]);
const totalRow = ref(0);
const totalPage = ref(0);

// 分页参数
const params = ref({
  current: 1,
  pageSize: 20
});

// 下拉刷新
function getFirstPage() {
  params.value.current = 1;
  trigger.value = true;
  loadData(params.value, 0);
}

// 上拉加载更多
function loadMoreData() {
  if (totalPage.value >= params.value.current) {
    params.value.current++;
    loadData(params.value, 1);
  }
}

// 动态计算滚动区域高度
const scrollHeight = computed(() => {
  const statusBarHeight = uni.getSystemInfoSync().statusBarHeight;
  return `calc(100vh - ${statusBarHeight + 100}px)`;
});

// 数据加载
async function loadData(params: any, type: number) {
  loading.value = true;
  try {
    const res = await fetchData(params);
    if (type === 0) {
      listData.value = res.resultList;
      totalPage.value = res.totalPage;
      totalRow.value = res.totalRow;
    } else {
      listData.value.push(...res.resultList);
    }
  } finally {
    loading.value = false;
    trigger.value = false;
  }
}
</script>

<style scoped lang="scss">
.loading-more {
  padding: 15px 0 20px;
  text-align: center;
  font-size: 12.5px;
  color: #999;
}
</style>
```

> 关键点说明：
> - `refresher-enabled`: 开启下拉刷新
> - `refresher-triggered`: 控制刷新状态
> - `@refresherrefresh`: 下拉刷新事件
> - `@scrolltolower`: 滚动到底部事件
> - `lower-threshold`: 距底部多少像素触发加载更多

### 4.2 底部弹出层组件

凡是页面中需要实现**从下往上弹出的操作面板、输入弹窗、底部表单、更多操作菜单**等交互，**必须统一使用 `tui-bottom-popup` 组件**，禁止自行用 `view` + `position: fixed` 模拟整套底部弹层交互。

- **适用场景**：
  - 更多操作弹出框
  - 文字输入弹出框
  - 底部表单面板
  - 底部菜单、底部选择器
- **组件路径**：`@/uni-module-common/components/thorui/tui-bottom-popup/tui-bottom-popup.vue`
- **规范要求**：
  - 弹层显隐统一使用 `ref(false)` 控制
  - 必须通过 `@close` 处理关闭逻辑，并同步清理页面状态
  - 输入类弹窗建议将内容区、关闭按钮、提交按钮封装为当前模块下的页面级组件
  - 如果弹层内包含二次确认，确认弹窗仍需遵循 3.5，统一使用 `xxt-common-modal`

参考写法：

```vue
<template>
  <tui-bottom-popup :show="showPopup" background-color="transparent" @close="handleClose">
    <view class="popup-container">
      <view class="popup-panel">
        <view class="popup-header">
          <text class="popup-title">标题</text>
        </view>

        <view class="popup-content">
          弹层内容
        </view>
      </view>
    </view>
  </tui-bottom-popup>
</template>

<script setup lang="ts">
const showPopup = ref(false);

const handleClose = () => {
  showPopup.value = false;
  // 同步清理弹层相关状态
};
</script>
```

### 4.3 图片懒加载组件

需要懒加载的图片使用 `tui-lazyload-img` 组件：

```vue
<template>
  <tui-lazyload-img
    :src="imageUrl"
    placeholder="/static/images/placeholder.png"
    mode="widthFix"
    radius="8px"
    @click="handleImageClick"
  />
</template>

<script setup lang="ts">
defineProps<{
  imageUrl: string;
}>();

const handleImageClick = () => {
  // 点击处理
};
</script>
```

**组件路径**：`@/uni-module-common/components/thorui/tui-lazyload-img/tui-lazyload-img.vue`

**常用属性**：
| 属性        | 类型    | 默认值   | 说明          |
| ----------- | ------- | -------- | ------------- |
| src         | String  | -        | 图片路径      |
| placeholder | String  | -        | 占位图路径    |
| mode        | String  | widthFix | 裁剪模式      |
| radius      | String  | -        | 圆角          |
| fadeShow    | Boolean | true     | 淡入动画      |
| webp        | Boolean | false    | 是否解析 WebP |

### 4.4 文本显示组件

普通文本显示使用 `tui-text` 组件：

```vue
<template>
  <tui-text
    text="这是文本内容"
    type="primary"
    :size="28"
    unit="px"
    align="left"
  />
</template>

<script setup lang="ts">
// 无需导入，已通过 easycom 自动注册
</script>
```

**组件路径**：`@/uni-module-common/components/thorui/tui-text/tui-text.vue`

**常用属性**：
| 属性       | 类型          | 默认值 | 说明                                                  |
| ---------- | ------------- | ------ | ----------------------------------------------------- |
| text       | String/Number | -      | 显示文本                                              |
| type       | String        | -      | 样式：primary/success/warning/danger/gray/black/white |
| size       | Number        | 0      | 字体大小                                              |
| unit       | String        | -      | 单位：rpx/px                                          |
| color      | String        | -      | 自定义颜色                                            |
| align      | String        | left   | 对齐方式：left/center/right                           |
| lineHeight | Boolean       | false  | 是否开启行高                                          |

### 4.5 Icon 图标组件

图标使用 `tui-icon` 组件：

```vue
<template>
  <!-- 使用 ThorUI 图标 -->
  <tui-icon name="arrowright" :size="16" color="#999999" />

  <!-- 使用 iconfont 图标 -->
  <tui-icon name="iconfont" custom-prefix="iconfont" :size="26" />
</template>

<script setup lang="ts">
// 无需导入，已通过 easycom 自动注册
</script>
```

**组件路径**：`@/uni-module-common/components/thorui/tui-icon/tui-icon.vue`

> 注意：`tui-icon` 是 ThorUI 组件库的一部分，支持多种图标类型。如需使用 iconfont，需要配置自定义图标。

## 五、类型定义规范

### 5.1 类型文件位置

- **页面级类型**：放在当前页面的 `hooks` 文件夹中，如 `evaluation-types.ts`
- **模块级类型**：放在模块根目录的 `types` 文件夹中

### 5.2 类型定义示例

```typescript
/**
 * 问卷选项数据结构
 */
export interface SurveyOption {
  optionCode: string;  // 选项编码
  text: string;        // 选项文本内容
}

/**
 * 问卷题目数据结构
 */
export interface SurveyQuestion {
  questionCode: string;     // 题目编码
  title: string;           // 题目标题
  questionType: string;    // 题目类型：SINGLE 单选 / MULTIPLE 多选
  required: boolean;       // 是否必填
  options: SurveyOption[]; // 选项列表
}

/**
 * 提交问卷请求参数
 */
export interface SurveySubmitParams {
  userExt: {
    provinceId: string;
    areaCode: string;
    gradeCode: string;
    identityType: string;
    identityValue: string;
  };
  surveyCode: string;
  answers: Array<{
    questionCode: string;
    optionCode: string;
  }>;
}
```

## 六、样式规范

### 6.1 CSS 预处理器

项目使用 **SCSS** 作为 CSS 预处理器。

### 6.2 单位使用

项目已配置 `postcss-px2rpx` 进行单位转换，样式中统一使用 **px** 单位：

```scss
.container {
  padding: 20px 30px;
  margin: 10px;

  .title {
    font-size: 32px;
    font-weight: 600;
  }
}
```

### 6.3 组件样式作用域

所有组件样式必须使用 `scoped`：

```vue
<style scoped lang="scss">
// 样式内容
</style>
```

## 七、路由与跳转

### 7.1 页面注册

页面需要在 `pages.json` 中注册：

```json
{
  "path": "pages/activity/act-ranking",
  "style": {
    "navigationBarTitleText": "排行榜",
    "navigationStyle": "custom"
  }
}
```

### 7.2 路由跳转

使用 `uni-mini-router`（项目封装的路由库）：

```typescript
import { useRouter } from 'uni-mini-router';

const router = useRouter();

// 路由跳转
router.push({
  path: '/pages/activity/act-ranking',
  query: {
    id: '123'
  }
});

// 返回上一页
router.back();
```

## 八、状态管理

### 8.1 Pinia Store

项目使用 Pinia 进行状态管理，推荐使用 **auto-import** 方式：

```typescript
// 方式一：函数封装（推荐）
const { userInfo, token } = useStore('user');

// 方式二：传统导入
import userStore from '@/uni-module-common/store/modules/user';
const { userInfo, token } = storeToRefs(userStore);
```

### 8.2 持久化

Pinia 已配置 `pinia-plugin-persist-uni` 插件，数据自动持久化到本地存储。

## 九、开发注意事项

### 9.1 平台兼容

注意使用条件编译处理平台差异：

```typescript
// #ifdef MP-WEIXIN
console.log('微信小程序专用代码');
// #endif

// #ifdef H5
console.log('H5 专用代码');
// #endif
```

### 9.2 安全规范

- 禁止使用 `v-html` 渲染不受信任的内容
- 敏感数据不存储在 localStorage
- 接口参数需进行校验

### 9.3 性能优化

- 列表使用 `key` 绑定稳定 ID
- 大数据列表考虑虚拟滚动
- 合理使用 `shallowRef` 处理大型数据
- 图片使用懒加载组件

## 十、常用工具

### 10.1 HTTP 请求

```typescript
import ajax from '@/uni-module-common/http';

// 基础请求
const res = await ajax({
  url: '/api/test',
  method: 'POST',
  data: { id: 1 },
  custom: {
    showLoading: true
  }
});
```

### 10.2 事件总线

```typescript
import eventBus from '@/uni-module-common/utils/eventBus';

// 监听事件
eventBus.on('event-name', (data) => {
  console.log('收到事件:', data);
});

// 触发事件
eventBus.emit('event-name', { id: 1 });

// 移除监听
eventBus.off('event-name');
```

### 10.3 通用组件引用

项目已配置 easycom，组件可自动引入：

```vue
<!-- 通用组件使用 -->
<xxt-empty tip-message="暂无数据" />

<tui-icon name="arrowright" :size="16" />
```

## 附录

### API 接口示例

```typescript
// hooks/api-hooks.ts
import ajax from '@/uni-module-common/http';

/**
 * ====== 类型定义 ======
 */

/**
 * 用户信息
 */
export interface UserInfo {
  id: string;
  name: string;
  avatar: string;
}

/**
 * ====== 接口方法 ======
 */

/**
 * 获取用户信息
 * 接口ID: XXXXX
 *
 * @param data 请求参数
 * @returns Promise<UserInfo> 用户信息
 */
export function getUserInfoApi(data: { id: string }): Promise<UserInfo> {
  return ajax({
    url: '/api/user/info',
    method: 'GET',
    data,
    custom: {
      showLoading: false
    }
  });
}
```

### 列表组件 scroll-view 示例

```vue
<template>
  <scroll-view
    class="scroll-view"
    scroll-y
    :refresher-enabled="true"
    :refresher-triggered="trigger"
    @refresherrefresh="getFirstPage"
    @scrolltolower="loadMoreData"
  >
    <item-component
      v-for="item in listData"
      :key="item.id"
      :data="item"
    />
  </scroll-view>
</template>

<script lang="ts" setup>
const trigger = ref(false);
const listData = ref<any[]>([]);

function getFirstPage() {
  trigger.value = true;
  loadData(0);
}

function loadMoreData() {
  loadData(1);
}
</script>
```

### 公共组件目录约定

公共组件统一位于 `src/uni-module-common/components/` 目录下，开发前应先在该目录中查找是否已有可复用组件。

#### ThorUI 组件目录

> **使用原则：以下为项目中使用 10 次以上的常用 ThorUI 组件。若页面需求与这些组件能力匹配，必须优先考虑使用 ThorUI 组件，不要重复封装同类基础组件。**

| 组件类型                   | 目录路径                                                    |
| -------------------------- | ----------------------------------------------------------- |
| 文本组件 `tui-text`        | `src/uni-module-common/components/thorui/tui-text/`         |
| 图标组件 `tui-icon`        | `src/uni-module-common/components/thorui/tui-icon/`         |
| 表单按钮 `tui-form-button` | `src/uni-module-common/components/thorui/tui-form-button/`  |
| 底部弹层                   | `src/uni-module-common/components/thorui/tui-bottom-popup/` |
| 栅格列 `tui-col`           | `src/uni-module-common/components/thorui/tui-col/`          |
| 输入框 `tui-input`         | `src/uni-module-common/components/thorui/tui-input/`        |
| 模态框 `tui-modal`         | `src/uni-module-common/components/thorui/tui-modal/`        |
| 图片懒加载                 | `src/uni-module-common/components/thorui/tui-lazyload-img/` |
| 文本域 `tui-textarea`      | `src/uni-module-common/components/thorui/tui-textarea/`     |
| 栅格行 `tui-row`           | `src/uni-module-common/components/thorui/tui-row/`          |
| 操作菜单 `tui-actionsheet` | `src/uni-module-common/components/thorui/tui-actionsheet/`  |
| 对话框 `tui-dialog`        | `src/uni-module-common/components/thorui/tui-dialog/`       |
| 复选框 `tui-checkbox`      | `src/uni-module-common/components/thorui/tui-checkbox/`     |
| 标签页 `tui-tabs`          | `src/uni-module-common/components/thorui/tui-tabs/`         |

#### XXT 业务组件目录

| 组件类型   | 目录路径                                                              |
| ---------- | --------------------------------------------------------------------- |
| 登录拦截   | `src/uni-module-common/components/xxt-components/xxt-common-unlogin/` |
| 模块标题   | `src/uni-module-common/components/xxt-components/xxt-common-title/`   |
| 空状态     | `src/uni-module-common/components/xxt-components/xxt-empty/`          |
| 文本折叠   | `src/uni-module-common/components/xxt-components/xxt-text-overflow/`  |
| 文件上传   | `src/uni-module-common/components/xxt-components/xxt-file-submit/`    |
| 图片签章   | `src/uni-module-common/components/xxt-components/xxt-img-sign/`       |
| 通知卡片   | `src/uni-module-common/components/xxt-components/xxt-notice-card/`    |
| 底部标签栏 | `src/uni-module-common/components/xxt-components/xxt-tabs-bottom/`    |
| 任务卡片   | `src/uni-module-common/components/xxt-components/xxt-task-card/`      |

#### 使用原则

- 页面中的基础 UI 能力，优先在 ThorUI 组件目录中查找现成组件
- 如果需求与 `tui-text`、`tui-icon`、`tui-input`、`tui-textarea`、`tui-bottom-popup`、`tui-modal`、`tui-tabs` 等组件能力匹配，优先直接使用
- 先查公共组件目录，再决定是否新建业务组件
- 如果已有组件能力匹配，优先复用，不重复造轮子
- 如果公共组件不满足需求，应在当前业务模块内封装，不修改公共模块源码

### 注意

- 关键业务逻辑、复杂字段、接口参数含义应补充中文注释，避免只写“表面代码”不写语义说明
- `hooks` 中对外暴露的方法、状态、计算属性，建议写清楚作用和使用场景
- 调试日志只在排查问题时临时添加；无意义、重复性的 `console.log` 在提交前应清理
- 规范鼓励必要的日志和注释，但不建议为了“多写注释”而写低价值注释
