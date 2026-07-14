# 《情侣日记》前端 UI 设计规范与 AI 提示词指南

> **版本：** v1.0
> **日期：** 2026-06-12
> **适用范围：** 《情侣日记》小程序全部页面、组件、模块级设计文档、AI 生成页面与前端实现
> **文档定位：** 全局 UI 设计基线文档，后续所有模块设计均需以此为基础展开

---

## 一、文档目标

本指南用于为《情侣日记》小程序提供统一的前端视觉与交互基准，确保不同模块、不同页面、不同设计阶段始终维持一致的品牌氛围与组件语言。

本指南同时服务于两类场景：

- 前端开发落地时的视觉与交互规范参考
- AI 辅助生成页面、组件、交互方案时的统一约束前置词

---

## 二、核心设计理念

### 2.1 情感化连接

- 通过圆润线条、柔和色彩和带有呼吸感的动效，营造情侣间私密、温柔、亲密的氛围。
- 页面不追求工具型产品的冷静效率感，而更强调陪伴感、仪式感和情绪价值。

### 2.2 双向对称逻辑

- 核心页面，尤其是日记与赌局，采用中轴线布局。
- 中轴线左侧归属 `TA`，右侧归属 `我`，强化情侣之间对等、双向、互动的视觉关系。
- 这种左右分栏逻辑是产品的核心识别特征之一，后续页面设计不应轻易破坏。

### 2.3 微光微动交互

- 状态变化优先通过细腻的呼吸灯、柔和阴影、轻微位移和卡片悬浮感表达。
- 尽量避免生硬的闪烁、突兀的颜色切换和强烈震动式提醒。
- 产品整体动效应表现为“轻提醒、轻反馈、轻浮动”。

---

## 三、主题色系统

### 3.1 核心色值 Tokens

| Token | 色值 | 说明 |
|---|---|---|
| Primary | `#ff8fa3` | 樱花粉，品牌主色，用于高亮按钮、图标、关键状态 |
| Surface | `#fff8f7` | 暖肤白，全局主背景色 |
| Surface-Dim | `#e7d6d7` | 淡烟灰粉，用于输入框、分割线、时间线 |
| Surface-Container | `#ffffff` | 纯白，用于浮层、卡片、容器背景 |
| Text-Primary | `#4a3a3b` | 暖深棕，主文本色，避免纯黑带来的生硬感 |
| Text-Variant | `#8c7b7c` | 柔和灰，用于日期、时间戳、辅助文案 |
| Action-Rose | `#9c4b57` | 豆沙红，用于主要 CTA 按钮和强调动作 |

### 3.2 状态色

| 状态 | 色彩策略 | 说明 |
|---|---|---|
| Success / Completed | `#ffcad4` | 淡粉渐变或柔和完成态背景 |
| Pending / Active | `#ff8fa3` | 主色高亮，可搭配轻呼吸发光 |
| Warning / Redeeming | `#ff8fa3` 阴影扩散 | 用于待兑现等需要柔性提醒的状态 |

### 3.3 颜色使用约束

- 全局主背景优先使用 `#fff8f7`，避免大面积纯白导致页面失去温度。
- 不使用蓝色、绿色或纯黑作为主视觉重心色。
- 高风险、高提醒状态也尽量先通过粉调深浅、阴影和动效表达，而不是改用刺眼警示色。

---

## 四、布局与组件规范

### 4.1 中轴时间线 Central Timeline

#### 4.1.1 布局规则

- 页面水平居中一条 `1px` 虚线，颜色使用 `#e7d6d7`。
- 左侧卡片与中轴线之间使用 `margin-right: 12px`。
- 右侧卡片与中轴线之间使用 `margin-left: 12px`。
- 日期显示在中轴椭圆胶囊内，作为时间分组节点。
- 时间点使用 `8px` 实心圆点，通过细连接线指向左右卡片。

#### 4.1.2 归属规则

- 左侧默认归属 `TA / 对方`
- 右侧默认归属 `我 / 自己`
- 归属规则一旦在该产品中确立，不允许在某些页面反转，避免认知负担

### 4.2 卡片系统 Card System

#### 4.2.1 基础规范

- 圆角：`24px`
- 阴影：`0 8px 24px rgba(255, 143, 163, 0.08)`
- 内边距：`16px`
- 容器背景：优先使用纯白或极浅暖粉背景

#### 4.2.2 动效规范

- `hover: scale(1.02)`
- `active: scale(0.98)`
- 过渡时间：`0.3s cubic-bezier(0.4, 0, 0.2, 1)`

#### 4.2.3 视觉原则

- 卡片强调“软边界、轻阴影、低压迫感”
- 不使用厚重黑色投影
- 状态强化优先通过边框、角标、微发光和局部背景变化表达

### 4.3 导航栏 Navigation

- BottomNavBar 图标采用线性与面性切换
- 激活状态带 `bg-primary-container` 圆形底衬
- 建议搭配 `backdrop-blur-md` 和 `bg-surface/80`
- 导航栏整体应轻、透、柔和，不使用厚重深色底

### 4.4 组件复用原则

- 统一复用 `TopAppBar`
- 统一复用 `BottomNavBar`
- 列表、浮层、状态卡片、按钮等基础组件应基于同一套卡片与色彩系统扩展
- 模块页面不应各自发明新的按钮样式、阴影体系或边框语言

---

## 五、字体与排版规范

### 5.1 字体策略

- 优先使用 `"Plus Jakarta Sans"` 或系统无衬线字体
- 文本颜色必须以 `#4a3a3b` 为主，不使用纯黑

### 5.2 排版气质

- 标题应柔和、亲近，不要过于硬朗或商务化
- 时间、日期、状态标签使用较轻字号和辅助色
- 强调信息通过颜色、字重和留白表达，不依赖过度加粗

### 5.3 移动端字号与控件尺度基准

> 以“我的页 - 登录未绑定态”作为小程序移动端首屏字号基准。AI 从原型图或 HTML code 还原页面时，必须先把原型中的超大桌面字号收敛到以下梯度，再进行代码落地。

| 层级 | 推荐字号 | 适用场景 | 说明 |
|---|---:|---|---|
| 页面/品牌标题 | `40rpx - 44rpx` | 登录页品牌名、核心 hero 标题 | 只用于页面最强信息，不允许大面积使用 `56rpx+` |
| 区块标题 | `34rpx - 40rpx` | 身份选择标题、卡片主标题、hero 主标题 | 默认靠近 `34rpx`，情绪化 hero 可到 `40rpx` |
| 列表/按钮主文案 | `28rpx - 30rpx` | 主按钮、功能入口标题、角色标签 | 主 CTA 推荐 `28rpx`，列表标题推荐 `30rpx` |
| 正文说明 | `24rpx - 26rpx` | hero 副标题、卡片说明、协议文字 | 普通说明用 `24rpx`，重要说明可用 `26rpx` |
| 辅助/状态文字 | `22rpx - 24rpx` | chip、错误提示、过期提示、注释说明 | 不要低于可读性，也不要抢主标题层级 |
| 极小辅助文字 | `20rpx - 22rpx` | 小图标内文字、极弱说明 | 仅在空间极小的局部使用 |

控件尺度必须与字号同步：

- 主按钮高度优先使用 `88rpx - 92rpx`，按钮文字使用 `28rpx`。
- 次级按钮高度优先使用 `64rpx - 72rpx`，按钮文字使用 `24rpx`。
- 输入框高度优先使用 `96rpx - 100rpx` 或 `48px - 50px`，输入文字使用 `30rpx - 32rpx`。
- 头像、Logo、Hero 装饰不能为了还原原型而无限放大；移动端首屏主装饰建议控制在 `136rpx - 168rpx` 或 `112px - 148px` 区间。
- 如果原型图 code 中出现 `text-5xl`、`56px`、`64px`、`w-32 h-32` 等桌面化尺度，落地到小程序前必须按本节降级。

---

## 六、全局动效规范

### 6.1 动效原则

- 动效应辅助情绪表达和状态提示，而不是制造干扰
- 所有动效都应保持柔和、轻量、节奏平缓

### 6.2 推荐动效

- 呼吸发光：用于待兑现、待处理等温和提醒状态
- 轻微浮动：用于强化待关注卡片
- 平滑位移：用于页面切换、卡片展开、状态更新
- 轻微缩放：用于按钮按下和卡片交互反馈

### 6.3 禁止项

- 禁止剧烈闪烁
- 禁止高频抖动
- 禁止强烈对比色跳变
- 禁止使用重黑阴影制造压迫感

---

## 七、全局 AI 提示词框架

在向 AI 请求新功能页面、组件实现、视觉方案或前端代码时，应优先附加以下系统级约束：

```text
[UI System Constraint]
- Brand Colors: Use #ff8fa3 as primary, #fff8f7 as global background. Avoid blue, green, or pure black.
- Visual Style: Soft, romantic, intimate. Use rounded-3xl (24px) for cards and buttons.
- Typography: Use "Plus Jakarta Sans" or system-sans. Mobile font scale must follow: page title 40-44rpx, section title 34-40rpx, button/list title 28-30rpx, body 24-26rpx, auxiliary 22-24rpx. Do not directly copy oversized prototype fonts such as 56rpx+ or text-5xl. Text color must be #4a3a3b.
- Layout: Follow the "Central Axis Timeline" logic for lists. Left=Partner, Right=Self.
- Micro-interactions: Use animate-pulse for redeeming states. Buttons must have a soft transition-all.
- Components: Reuse TopAppBar and BottomNavBar with backdrop-blur-md and bg-surface/80.
- Shadows: Only use very soft pink-tinted shadows, no heavy blacks.
```

### 7.1 AI 使用规则

- 所有模块级 AI 设计提示词都应先引入这段全局约束，再补充页面专属约束。
- 如果 AI 产出的页面出现蓝绿主色、纯黑文本、方角卡片、重阴影或破坏左右归属逻辑，应视为不符合全局设计系统。

---

## 八、典型实现参考

```html
<!-- 核心卡片容器示例 -->
<div class="bg-white rounded-[24px] p-4 shadow-[0_8px_24px_rgba(255,143,163,0.08)] border border-[#fff0f1] transition-all active:scale-95">
  <div class="flex items-center justify-between mb-2">
    <span class="text-[12px] font-medium text-[#ff8fa3]">进行中</span>
    <span class="text-[12px] text-[#8c7b7c]">23:30 截止</span>
  </div>
  <p class="text-[16px] text-[#4a3a3b] font-semibold">打赌今晚会下雨</p>
  <div class="mt-3 py-2 px-3 bg-[#fff8f7] rounded-xl flex items-center gap-2">
    <span>🎁</span>
    <span class="text-[14px] text-[#9c4b57]">奖励：一杯超大冰美式</span>
  </div>
</div>
```

---

## 九、后续使用规则

- 后续每个模块单独的 UI 设计文档，只补充该模块专属页面规范，不重复定义主色、全局卡片风格和基础动效系统。
- 当后续模块设计需要新增共性的按钮规范、弹窗规范、输入框规范或页面框架规范时，应先补充到本全局文档，再下发到各模块。
- 各模块的 `designs/` 目录应默认引用本指南作为前置设计依据。
