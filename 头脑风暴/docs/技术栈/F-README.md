# couple-diary-f UniApp 前端工程说明

`couple-diary-f` 是一个基于 UniApp、Vue 3、TypeScript、Vite 的多端前端工程。当前工程定位是可复用的业务脚手架：公共层提供启动、配置、请求、鉴权、登录态、路由、组件库和基础上传能力；具体业务项目只应该在业务模块中接入自己的登录协议、权限协议、接口协议和页面实现。

本文档同时面向两类读者：

- 人类开发者：快速理解工程结构、运行方式、公共能力和开发规范。
- LLM / AI 编程工具：在编码前读取本文档，并严格遵守 `.claude/rules` 中的工程规则，避免把旧业务逻辑重新塞回脚手架。

## 1. 快速开始

### 1.1 环境要求

- Node.js：建议 18+
- 包管理器：当前仓库使用 `package-lock.json`，优先使用 `npm install`
- 主要目标平台：微信小程序；同时保留 H5、App、多小程序平台构建脚本

### 1.2 安装与运行

```bash
npm install
npm run dev:mp-weixin
```

常用命令：

| 命令 | 作用 |
| --- | --- |
| `npm run dev:mp-weixin` | 运行微信小程序开发环境 |
| `npm run dev:h5` | 运行 H5 开发环境；本地联调通常需要 `.xxt.cn` 二级域名访问 |
| `npm run build:mp-weixin` | 构建微信小程序产物 |
| `npm run build:h5` | 构建 H5 产物 |
| `npm run type-check` | 执行 TypeScript / Vue 类型检查 |
| `npm run eslint` | 对 `src` 下 `.vue`、`.ts` 执行 ESLint 自动修复 |
| `npm run stylelint` | 对样式文件执行 Stylelint 自动修复 |
| `npm run lint:lint-staged` | 执行暂存文件检查 |
| `npm run init:submodule` | 初始化子模块；只应在项目首次初始化且子模块异常时使用 |

`npm run init:submodule` 会重新生成新的 `.git` 目录并重置远程仓库地址。不要在正常开发过程中反复执行。

## 2. 技术栈

| 类型 | 技术 |
| --- | --- |
| 跨端框架 | UniApp `@dcloudio/* 3.0.0-5000720260410001` |
| 前端框架 | Vue `3.4.21` |
| 编程语言 | TypeScript `4.9.4` |
| 构建工具 | Vite `5.2.8` + `@dcloudio/vite-plugin-uni` |
| 状态管理 | Pinia `2.0.33` |
| 状态持久化 | `pinia-plugin-persist-uni` |
| Store 自动引用 | `pinia-auto-refs` |
| 路由 | `uni-mini-router` + `uni-read-pages-vite` |
| 请求 | `uni-ajax` 二次封装 |
| 样式 | SCSS、UnoCSS、`postcss-px2rpx` |
| 代码质量 | ESLint、Prettier、Stylelint、husky、lint-staged |
| 组件库 | ThorUI、XXT 公共组件、UniApp easycom |

## 3. Vite 插件与构建机制

核心配置在 `vite.config.ts`。

| 插件 / 机制 | 作用 | 注意事项 |
| --- | --- | --- |
| `@dcloudio/vite-plugin-uni` | UniApp Vite 构建入口 | 必须保留在插件链中 |
| `uni-read-pages-vite` | 读取 `src/pages.json` 并注入 `ROUTES` | `src/uni-module-common/router/index.ts` 使用 `ROUTES` 创建路由 |
| `unplugin-auto-import` | 自动导入 Vue、UniApp、Pinia、`useRouter`、`useRoute`、`useStore` | 生成 `src/auto-imports.d.ts` 和 ESLint 自动导入配置 |
| `auto-import-types` | 自动挂载类型声明 | 配合 `.eslintrc-auto-import-types.json` 使用 |
| `pinia-auto-refs` | 为 `src/uni-module-common/store` 下的 Store 生成 `useStore` 辅助引用 | Store 文件必须保持扁平化，不要随意在 store 内建子目录 |
| `unocss` | 原子化 CSS | 当前版本固定为 `0.50.4`，不要盲目升级，微信端兼容风险高 |
| `postcss-px2rpx` | 将样式中的 `px` 转成小程序 `rpx` | 设计稿宽度 `750`，样式代码统一写 `px` |
| `loadEnv` + `wrapperEnv` | 读取和格式化 `.env.*` | 环境变量经 `src/uni-module-common/config/index.ts` 统一导出 |
| `createProxy` | H5 开发代理 | 读取 `VITE_PROXY` |

构建时还会读取命令行最后一个参数作为项目配置 key。当前允许的配置 key 是 `couple-diary`，并写入 `src/uni-module-common/config/config.json` 的 `loginConfigKey`。如果后续扩展新业务项目，需要同步维护：

- `vite.config.ts` 中的 `projectConfigKeyAry`
- `src/uni-module-common/config/project-json-config/<project-key>/`
- `src/uni-module-common/config/project-modules-config/<project-key>-config.ts`

### 3.1 自动导入机制

工程已通过 `unplugin-auto-import` 配置基础 API 自动导入，生成文件为 `src/auto-imports.d.ts`。开发页面、组件、hooks 时，以下基础能力通常不需要手动 import：

| 来源 | 自动导入内容 |
| --- | --- |
| `vue` | `ref`、`computed`、`watch`、`reactive`、`nextTick` 等 Vue 组合式 API |
| `uni-app` | `onLoad`、`onShow`、`onHide`、`onPullDownRefresh` 等 UniApp 生命周期 API |
| `pinia` | Pinia 基础 API |
| `uni-mini-router` | `createRouter`、`useRouter`、`useRoute` |
| `@/uni-module-common/helper/pinia-auto-refs` | `useStore` |

正确写法：

```ts
const title = ref('首页');
const router = useRouter();
const { userInfo, setLoginState } = useStore('user');

onLoad((options) => {
  console.info('[页面] 页面加载参数', options);
});
```

不推荐再写这类重复导入：

```ts
import { ref } from 'vue';
import { onLoad } from '@dcloudio/uni-app';
import { useRouter } from 'uni-mini-router';
```

业务 API、业务类型、公共工具、非自动导入的模块仍然需要显式 import，例如 `request`、`logoutUser`、`setPermissionChecker` 等。

## 4. 工程目录

```text
frontend/couple-diary-f
├── build/                                  # Vite 工具、代理等构建辅助
├── script/                                 # 初始化子模块、App wgt 构建等脚本
├── src/
│   ├── @types/                             # 全局类型定义
│   ├── auto-imports.d.ts                   # unplugin-auto-import 自动生成
│   ├── App.vue                             # 应用生命周期、原生桥接、运行时入口
│   ├── main.ts                             # 创建 Vue app，注册 Pinia / Router / 全局属性
│   ├── manifest.json                       # UniApp 应用与各端平台配置
│   ├── pages.json                          # 页面、分包、easycom、tabBar 配置
│   ├── pages/                              # 主包页面
│   ├── static/                             # 静态资源
│   ├── style/                              # 全局样式、ThorUI 扩展样式
│   ├── uni.scss                            # UniApp 全局样式变量
│   ├── wxmp-global-config/                 # 微信端路由拦截、tabbar 分包映射
│   ├── uni_modules/                        # 业务分包和三方 uni_modules
│   │   ├── uni-module-public/              # 公共分包：登录页、微信文件选择等
│   │   ├── pages-diary/                    # 日记业务分包
│   │   ├── pages-bets/                     # 赌局业务分包
│   │   └── pages-mine/                     # 我的分包
│   └── uni-module-common/                  # 公共基础模块
│       ├── auth/                           # 鉴权、登录跳转、登出、启动登录桥接
│       ├── components/                     # ThorUI、XXT、Uni 公共组件
│       ├── config/                         # 环境配置、项目配置、模块配置
│       ├── helper/                         # pinia-auto-refs 等辅助文件
│       ├── hooks/                          # 公共 hooks、权限适配、上传 hooks
│       ├── http/                           # 请求实例、请求适配、Cookie 兼容、上传
│       ├── pages/                          # 公共页面辅助与契约测试
│       ├── router/                         # uni-mini-router 注册
│       ├── rules/                          # 公共模块规则镜像
│       ├── store/                          # Pinia store，必须扁平化
│       └── utils/                          # 通用工具、事件总线、原生桥接
├── .claude/rules/                          # 当前工程 AI / LLM 必读规则
├── .env                                    # 默认环境变量
├── .env.development                        # 开发环境变量
├── .env.test                               # 测试环境变量
├── .env.production                         # 生产环境变量
├── vite.config.ts                          # Vite / UniApp 构建配置
├── package.json                            # 脚本与依赖
└── README.md                               # 当前说明文档
```

### 4.1 页面与分包

主包页面在 `src/pages`，当前底部 tabBar 以 `src/pages.json` 为唯一事实源：

- `pages/diary/index`：日记入口，也是当前应用默认入口
- `pages/bets/bets`：赌局入口
- `pages/mine/mine`：我的入口

分包在 `src/pages.json` 的 `subPackages` 中注册：

- `uni_modules/uni-module-public`：公共登录页、绑定页、微信文件选择页
- `uni_modules/pages-diary`：日记模块，当前注册页为 `diary-main/diary-main`
- `uni_modules/pages-bets`：赌局模块，当前注册页为 `pages/home/index`
- `uni_modules/pages-mine`：我的模块，当前注册页为 `mine-main/mine-main`

当前 tabBar 配置：

| 页面路径 | tab 文案 | 说明 |
| --- | --- | --- |
| `pages/diary/index` | 日记 | 默认首页与日记主入口 |
| `pages/bets/bets` | 赌局 | 赌局系统主入口 |
| `pages/mine/mine` | 我的 | 个人中心入口 |

### 4.2 easycom 自动组件

`src/pages.json` 已配置 easycom：

```json
{
  "^uni-(.*)": "@/uni-module-common/components/uni-$1/uni-$1.vue",
  "^xxt-(.*)": "@/uni-module-common/components/xxt-components/xxt-$1/xxt-$1.vue",
  "^tui-(.*)": "@/uni-module-common/components/thorui/tui-$1/tui-$1.vue"
}
```

页面中可以直接使用：

```vue
<template>
  <xxt-empty tip-message="暂无数据" />
  <tui-icon name="arrowright" :size="16" />
</template>
```

## 5. 配置说明

### 5.1 环境变量

| 变量 | 作用 | 使用位置 |
| --- | --- | --- |
| `VITE_GLOB_API_URL` | API 域名 | `src/uni-module-common/config/index.ts` |
| `VITE_GLOB_API_URL_PREFIX` | API 前缀，H5 常用 `/api` 走代理 | `resolveRequestUrl()` |
| `VITE_GLOB_CLICK_URL` | 点击日志域名 | 点击日志请求分流 |
| `VITE_GLOB_CLICK_URL_PREFIX` | 点击日志前缀 | `resolveRequestUrl()` |
| `VITE_GLOB_AI_URL` | AI 接口域名，如环境未配置则为空 | AI 请求分流 |
| `VITE_GLOB_AI_URL_PREFIX` | AI 接口前缀，如环境未配置则为空 | AI 请求分流 |
| `VITE_GLOB_UPLOAD_URL` | 上传接口域名 | 上传模块 |
| `VITE_GLOB_IMG_CDN_URL` | 静态资源 CDN 域名 | `$cdn` 全局属性与配置导出 |
| `VITE_PORT` | H5 开发端口 | Vite dev server |
| `VITE_PROXY` | H5 开发代理，JSON 数组字符串 | `createProxy()` |
| `VITE_PUBLIC_PATH` | Vite base path | `vite.config.ts` |
| `VITE_DROP_CONSOLE` | 生产构建是否移除 console/debugger | terser compress |

平台差异：

- H5：`apiBaseUrl = ''`，请求走 `VITE_GLOB_API_URL_PREFIX`，通常由 Vite 代理或部署网关转发。
- 非 H5：`apiUrlPrefix = ''`，请求会拼接 `VITE_GLOB_API_URL`。

### 5.2 项目模块配置

当前项目配置位于 `src/uni-module-common/config/project-modules-config/couple-diary-config.ts`，主要字段：

| 字段 | 说明 |
| --- | --- |
| `entry` | 当前应用入口标识，启动时同步到全局运行时 |
| `hostId` | 当前项目 host 标识 |
| `defalutAppConfig` | 默认 tabBar 与模块配置 |
| `publicSubPackgePath` | 公共分包路径 |
| `subPackagesRoot` | 当前业务分包根路径 |
| `loginPhoneCode` / `bindPhoneCode` | 短信登录 / 绑定验证码类型；脚手架默认 `0` |
| `noLoginApiWhiteList` | 接口返回未登录时不触发跳登录的白名单 |

新增业务项目时，不要把项目专属逻辑写死在公共请求、公共 Store 或公共登录 hook 中；应该新增自己的 project config，并在业务模块中接入真实协议。

## 6. 启动入口与全局注册

### 6.1 `main.ts`

`src/main.ts` 做三件事：

1. 创建 Vue SSR App。
2. 注册 Pinia：`setupPinia(app)`。
3. 注册路由：`setupRouter(app)`。
4. 挂载全局属性：
   - `$uniAjax`：公共请求实例
   - `$cdn`：静态资源 CDN
   - `$eventBus`：事件总线

### 6.2 `App.vue`

`src/App.vue` 负责应用级生命周期：

- `onLaunch`：隐藏原生 tabBar、写入微信端最小 `userAgent` / `clientInfo`、注册 App 原生桥接事件、执行路由拦截初始化、同步当前项目 entry。
- `onShow`：消费 App 原生启动透传登录数据，处理前后台切换。
- `onHide`：通知原生端生命周期变化。

微信端开发默认会生成最小运行时标识：

```ts
const userAgent4Dev = `${appModuleConfig.entry};hostId:${appModuleConfig.hostId};appversion:1.0.0;appversioncode:100`;
```

这只是运行时兼容信息，不是业务登录协议。

## 7. 请求层使用说明

公共请求入口在 `src/uni-module-common/http/index.ts`。

### 7.1 推荐入口：`request<T>()`

新增代码优先使用命名导出的 `request<T>()`。它复用旧 ajax 实例、拦截器、URL 分流、鉴权和 Cookie 兼容逻辑，但把返回值收敛成稳定结构：

```ts
import { request } from '@/uni-module-common/http';

interface ProfileInfo {
  id: string;
  nickname: string;
  avatar: string;
}

export function getProfileInfo() {
  return request<ProfileInfo>({
    url: '/profile/info',
    method: 'GET',
    data: {},
    custom: {
      auth: true,
      showLoading: false
    }
  });
}
```

调用方处理：

```ts
const result = await getProfileInfo();

if (!result.success) {
  console.warn('[个人资料] 获取失败', result.error);
  return;
}

const profile = result.data;
```

`request<T>()` 返回结构：

```ts
interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: string;
  raw: unknown;
}
```

### 7.2 兼容入口：默认 `ajax`

历史代码仍可使用默认导出的 `ajax`。不要删除或改名：上传、旧页面、旧兼容链路仍依赖它。

```ts
import ajax from '@/uni-module-common/http';

export function submitForm(data: Record<string, unknown>) {
  return ajax({
    url: '/form/submit',
    method: 'POST',
    data,
    custom: {
      auth: true,
      showLoading: true,
      loadingMsg: '提交中'
    }
  });
}
```

### 7.3 `custom` 配置

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `auth` | `false` | 是否要求登录；受保护接口必须显式设置 `true` |
| `showLoading` | `false` | 是否展示 loading |
| `loadingMsg` | `加载中` | loading 文案 |
| `showError` | `true` | 是否由请求层统一展示失败 toast |
| `errorMsg` | 空 | 自定义失败文案 |
| `showSuccess` | `false` | 是否展示成功 toast |
| `successMsg` | 空 | 自定义成功文案 |
| `isCancelHideToast` | `false` | 是否取消请求前自动隐藏 toast |
| `isCancelHideLoading` | `false` | 是否取消请求前自动隐藏 loading |

### 7.4 URL 分流规则

请求地址由 `src/uni-module-common/http/core/resolve-request-url.ts` 统一处理：

- 完整 `http` / `https` 地址直接放行。
- 以 `clickUrlPrefix` 开头的请求走点击日志域名。
- 以 `aiUrlPrefix` 开头的请求走 AI 域名。
- 普通相对路径走 API 域名和 API 前缀。

因此业务 API 文件只写业务路径，不要手动拼接环境域名：

```ts
request({
  url: '/diary/list',
  method: 'GET',
  custom: { auth: true }
});
```

## 8. 鉴权、登录、回跳与登出

公共鉴权入口在 `src/uni-module-common/auth/`，请求层通过 `src/uni-module-common/http/auth/http-auth-adapter.ts` 适配到 user store。

### 8.1 登录页路径

统一登录页：

```text
/uni_modules/uni-module-public/login/login
```

`redirectToLoginPage()` 会使用历史兼容参数 `redictUrl` 拼接回跳地址：

```ts
import { redirectToLoginPage } from '@/uni-module-common/auth';

void redirectToLoginPage('/pages/diary/index');
```

### 8.2 受保护请求

请求级鉴权只看 `custom.auth === true`：

```ts
request({
  url: '/diary/private-list',
  method: 'GET',
  custom: {
    auth: true
  }
});
```

如果未登录：

1. 请求在发出前被阻断。
2. 当前页面地址被记录到 `user.loginRedirectUrl`。
3. 跳转到统一登录页。
4. 请求抛出 `AUTH_REDIRECT_REQUIRED`，避免被普通网络错误重复提示。

### 8.3 登录成功写入状态

真实业务登录成功后，只应该写最小登录态：

```ts
const { setLoginState, consumeLoginRedirect } = useStore('user');

setLoginState({
  token: loginResult.token,
  userInfo: {
    id: loginResult.user.id,
    nickname: loginResult.user.nickname,
    avatar: loginResult.user.avatar,
    mobile: loginResult.user.mobile
  }
});

const redirectUrl = consumeLoginRedirect();
uni.redirectTo({ url: redirectUrl });
```

不要把多身份、学校、年级学期、旧 Cookie 登录保持逻辑写回 `user` store 或 `useLoginHooks.ts`。这些是业务项目自己的协议。

### 8.4 登出

默认登出能力在 `src/uni-module-common/auth/logout.ts`：

```ts
import { logoutUser, setLogoutRequest } from '@/uni-module-common/auth/logout';

setLogoutRequest(() => {
  return request({
    url: '/logout',
    method: 'POST',
    custom: { auth: true, showLoading: true }
  });
});

await logoutUser();
```

`logoutUser()` 会先尝试调用业务登出接口，最终一定执行本地登录态清理：

- `user.clearLoginState()`
- `requestCookies`
- `saveStateCookie`
- `isTempLogin`

## 9. Cookie 与旧登录兼容

当前推荐会话模型是最小 token 模型：

- `token`：登录凭证
- `userInfo`：最小用户信息
- `loginRedirectUrl`：登录后回跳地址

旧 Cookie 兼容桥在 `src/uni-module-common/http/legacy/cookie-bridge.ts`，只服务历史 Cookie 登录、扫码登录和旧上传链路。新业务不要新增依赖。

旧兼容缓存：

| 缓存 key | 用途 | 新业务是否应直接使用 |
| --- | --- | --- |
| `requestCookies` | 暂存登录响应原始 Cookie 数组 | 否 |
| `saveStateCookie` | 旧“保持登录”凭证缓存 | 否 |
| `token` 对象形式 | 旧 Cookie Map 过渡会话 | 否 |

公共请求响应会调用 `saveResponseCookiesForLegacyLogin(response.cookies)`，只记录凭证数量，不打印明文 Cookie。

新业务只需要使用 `setLoginState({ token, userInfo })`；不要手动拼 Cookie 头，不要在业务页面里读写 `requestCookies` / `saveStateCookie`。

## 10. 用户状态与数据存储

用户 store 在 `src/uni-module-common/store/user.ts`。当前核心是最小会话模型。

### 10.1 核心状态

| 状态 | 说明 |
| --- | --- |
| `isLogin` | 是否已登录，由 token 是否有效推导 |
| `token` | 当前登录凭证 |
| `useToken` | 兼容字段，保持和 `token` 同步 |
| `userInfo` | 当前用户最小信息 |
| `loginRedirectUrl` | 未登录跳转前记录的回跳地址 |
| `userAgent` / `clientInfo` | 兼容字段，承接旧运行时信息 |
| `accountList` / `deafultGradeTerm` | 兼容空壳，不再承载脚手架核心业务 |

### 10.2 最小用户信息

```ts
interface TemplateUserInfo {
  id: string | number;
  nickname: string;
  avatar: string;
  mobile?: string;
  wid?: string | number;
  avatorUrl?: string;
  avatarUrl?: string;
  [key: string]: any;
}
```

`wid`、`avatorUrl`、`avatarUrl` 是历史兼容字段。新代码优先使用 `id`、`nickname`、`avatar`、`mobile`。

### 10.3 常用操作

```ts
const {
  isLogin,
  token,
  userInfo,
  setLoginState,
  setUserInfo,
  clearLoginState,
  setLoginRedirectUrl,
  consumeLoginRedirect
} = useStore('user');

setLoginState({
  token: 'real-token',
  userInfo: {
    id: '10001',
    nickname: '用户昵称',
    avatar: 'https://example.com/avatar.png'
  }
});

setUserInfo({ nickname: '新昵称' });
clearLoginState();
setLoginRedirectUrl('/pages/diary/index');
const redirectUrl = consumeLoginRedirect();
```

### 10.4 Store 组织规则

- Store 文件放在 `src/uni-module-common/store` 根目录，保持扁平化。
- 通过 `useStore('user')` 获取状态和 actions。
- `pinia-plugin-persist-uni` 已注册，必要状态会持久化到 UniApp storage。
- 不要直接修改 state 对象；通过 action 写入。

## 11. 路由与页面跳转

项目使用 `uni-mini-router`，路由表由 `uni-read-pages-vite` 从 `src/pages.json` 读取后注入 `ROUTES`。

注册位置：`src/uni-module-common/router/index.ts`

```ts
const router = createRouter({
  routes: [...ROUTES]
});
```

页面或组件内使用：

```ts
const router = useRouter();

router.push({
  path: '/pages/diary/index',
  query: {
    id: '123'
  }
});
```

规则：

- `useRouter()` 必须在 `.vue` 的 `script setup` 或组件上下文里调用。
- 禁止在普通 `hooks/*.ts`、工具文件、类型文件中直接调用 `useRouter()`。
- hooks 如果需要跳转能力，应由页面传入 `router` 或传入跳转方法。
- `path` 搭配 `query` 使用；不要混用不清晰的跳转协议。

## 12. 权限检查适配

公共权限 hook 在 `src/uni-module-common/hooks/usePermission.ts`。它只是适配层，不内置任何业务权限接口。

业务项目需要在初始化时注册自己的权限检查器：

```ts
import {
  setPermissionChecker,
  checkPermissions,
  checkPermission
} from '@/uni-module-common/hooks/usePermission';

setPermissionChecker(async ({ resourceCodeList, context }) => {
  const result = await request<Array<{ code: string; allowed: boolean }>>({
    url: '/permission/check',
    method: 'POST',
    data: {
      resourceCodeList,
      context
    },
    custom: {
      auth: true,
      showLoading: false
    }
  });

  if (!result.success || !result.data) {
    return {
      allAllowed: false,
      items: resourceCodeList.map((code) => ({
        code,
        allowed: false,
        reason: result.error || '权限接口失败'
      })),
      raw: result.raw
    };
  }

  const items = resourceCodeList.map((code) => {
    const matchedItem = result.data?.find((item) => item.code === code);
    return {
      code,
      allowed: Boolean(matchedItem?.allowed),
      raw: matchedItem
    };
  });

  return {
    allAllowed: items.every((item) => item.allowed),
    items,
    raw: result.raw
  };
});

const canReadDiary = await checkPermission('diary:read');
const permissionResult = await checkPermissions(['diary:read', 'diary:write']);
```

默认策略：

- 未注册 checker：拒绝。
- checker 抛错：拒绝。
- checker 返回结构不合法：拒绝。
- checker 缺少某个权限编码结果：该编码拒绝。

这不是“保守”，这是权限系统的底线。默认放行就是安全坑。

## 13. 上传与文件选择

公共上传能力分布在：

- `src/uni-module-common/http/upload/`
- `src/uni-module-common/http/legacy/legacy-upload.ts`
- `src/uni-module-common/hooks/oss-upload/`
- `src/uni_modules/uni-module-public/weChatFileSelect/`

新页面如果只是业务附件上传，优先使用公共组件 `xxt-file-submit`。不要在页面中重复实现文件选择、预览、删除和上传链路。

旧微信文件选择页仍通过 `dealLoginOutToken()` 清理登录态；该函数现在只是通用登录态清理兼容别名，不再承载旧业务登录协议。

## 14. 组件化开发规范

组件化、样式、弹层、Props、事件、API 目录、路由对象使用限制等规则，以 `.claude/rules/UniApp模块化工程规范.md` 为唯一规范源。

人类开发者和 LLM 在修改页面、组件、hooks、types、api、样式前，必须先读取并遵守：

```text
frontend/couple-diary-f/.claude/rules/UniApp模块化工程规范.md
```

README 不重复维护这份规范的细节，避免两处内容漂移。执行时按该文件为准，尤其注意：

- 页面必须按模块拆分为页面容器、components、hooks，不要把请求、数据处理、大段 UI 和样式堆在同一个 `.vue` 文件里。
- 优先复用 `src/uni-module-common/components/` 下的 ThorUI / XXT 公共组件，不要重复造轮子。
- 微信小程序弹层必须挂到页面层或页面直属容器层。
- Props、emit、双向绑定、样式写法、选择器合并、路由对象使用限制，都以规范文件为准。
- 如果规范文件和 README 描述不一致，以规范文件为准。

## 15. API 文件规范

业务 API 必须放在当前模块的 `hooks` 目录，不要散落在 `.vue` 中。

```ts
import { request } from '@/uni-module-common/http';

/**
 * 日记列表项。
 */
export interface DiaryItem {
  /** 日记 ID */
  id: string;
  /** 日记标题 */
  title: string;
  /** 创建时间 */
  createTime: string;
}

/**
 * 获取日记列表。
 * @param data 查询参数
 * @param data.page 页码
 * @param data.pageSize 每页数量
 */
export function getDiaryListApi(data: { page: number; pageSize: number }) {
  return request<DiaryItem[]>({
    url: '/diary/list',
    method: 'GET',
    data,
    custom: {
      auth: true,
      showLoading: false
    }
  });
}
```

接口失败提示规则：

- 公共请求层默认会展示后端失败文案。
- 页面和业务 hooks 不要重复弹失败 toast。
- 业务 hooks 失败时做状态复原、流程中断和必要日志即可。
- 如果产品要求特殊失败交互，必须关闭请求层默认提示，并在注释中说明原因。

### 15.1 用户文本输入安全清洗

凡是接收用户自由文本的输入控件（`textarea` / `input` / 弹窗输入框），在把文本回写给父级双向绑定、或拼进接口请求体之前，**必须统一调用公共纯函数 `sanitizeTextInput`**（位于 `src/uni-module-common/utils/sanitize-text.ts`），不得各自实现清洗逻辑。

清洗内容与后端 `app/core/text_sanitize.py` 的 `sanitize_bet_text` 完全一致：剥离 HTML 标签、剔除控制字符与零宽字符，保留 emoji 与中日韩标点。前端这层是「第一道」纵深防御，后端 schema 字段声明为 `SafeText` 是「最后一道」，两层规则必须相同。

接入示例与强制条款详见 `.claude/rules/UniApp模块化工程规范.md` 第 3.7 节「用户文本输入安全清洗」。新增任何 `textarea` / `input` / 弹窗输入框时，必须在其 `input` / `blur` / `confirm` 处理函数里调用 `sanitizeTextInput`，未调用的不得提交。

## 16. LLM / AI 编码协作准则

AI 编码、协作、验证、交付的完整规则，以 `.claude/rules/AI通用编码与协作规范.md` 为唯一规范源。

所有 LLM / AI 编程工具在开始分析、编码、重构、调试、补文档、补测试之前，必须先读取并遵守：

```text
frontend/couple-diary-f/.claude/rules/AI通用编码与协作规范.md
```

README 不复制这份规范的正文，避免两处规则不一致。执行时按该文件为准，最低要求是：

- 先明确目标、范围、非目标、依赖、假设和验证方式。
- 信息不足时先提问，不靠猜测编码。
- 只做当前任务需要的最小改动，不顺手重构无关代码。
- 新增或重写核心逻辑时，按规范补中文注释和关键日志。
- 交付前说明实际验证结果；如果验证被历史问题阻断，写清楚命令、文件和替代验证。
- 如果规范文件和 README 描述不一致，以规范文件为准。

## 17. 新功能开发推荐流程

1. 读本文档和 `.claude/rules`。
2. 明确目标、范围、非目标和验证方式。
3. 查公共组件，能复用就不要造轮子。
4. 页面按 `page.vue + components + hooks` 拆分。
5. API 写在当前模块 hooks 中，优先使用 `request<T>()`。
6. 需要登录的接口显式写 `custom.auth: true`。
7. 登录成功只写 `setLoginState({ token, userInfo })`。
8. 权限逻辑通过 `setPermissionChecker()` 接入业务实现。
9. 修改后执行最小验证。
10. 输出交付说明：改了什么、为什么、验证结果、风险。

## 18. 常见错误

| 错误做法 | 正确做法 |
| --- | --- |
| 在页面 `.vue` 里直接写大段请求和数据处理 | 抽到 `hooks/*-api.ts` 和 `hooks/use-*.ts` |
| 受保护接口忘记 `custom.auth: true` | 所有需要登录的接口显式开启 auth |
| 业务登录逻辑写进 `useLoginHooks.ts` | 在业务登录页或业务模块中调用 `setLoginState()` |
| 权限接口写死在公共 hook | 通过 `setPermissionChecker()` 注册业务 checker |
| 手动拼接 API 域名 | 只写业务路径，让 `resolveRequestUrl()` 分流 |
| 页面重复写空状态、上传、标题组件 | 优先使用 XXT / ThorUI 公共组件 |
| 在 hooks 里直接 `useRouter()` | 页面传入 router 或跳转函数 |
| 接口失败页面再弹一次 toast | 由公共请求层统一提示，页面只做状态复原 |
| 在新业务里读写 `requestCookies` / `saveStateCookie` | 使用最小 token 登录态 |

## 19. 当前已知注意点

- 当前仓库仍保留部分历史兼容字段和方法，例如 `useToken`、`userAgent`、`clientInfo`、`accountList`、`deafultGradeTerm`。新代码不要继续依赖它们。
- 旧 Cookie 兼容桥仍服务历史上传和旧登录链路。新业务不要新增 Cookie Map 依赖。
- 登录页当前是脚手架占位页，真实项目必须替换为真实登录接口和视觉实现。
- `type-check` 可能被历史组件语法问题阻断；遇到阻断时按“验证原则”记录清楚，不要把历史问题算成本次改动。
