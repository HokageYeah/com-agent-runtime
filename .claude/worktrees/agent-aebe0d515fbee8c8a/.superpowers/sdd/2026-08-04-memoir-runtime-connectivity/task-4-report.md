# Task 4 修复报告

## 目标与范围

修复 `couple-diary-f` 回忆录 Runtime 联通页的审查问题。仅修改 Task 4 前端 API wrapper、状态 hook、页面合同测试、页面样式和 `package.json` 定向测试脚本；未修改 Mine 入口、`pages.json` 或 `src/uni-module-common`，未提交代码。

## 审查问题与修复

### High：后端 snake_case 到前端 camelCase 的协议边界

- 在 `src/uni_modules/pages-mine/pages/memoir-runtime-connectivity/hooks/memoir-runtime-api.ts` 新增模块私有 DTO：
  - `RawMemoirRuntimeConnectivityData.runtime_reachable`
  - `RawMemoirRuntimeConnectivityData.contract_version`
  - `RawMemoirRuntimeConnectivityData.capabilities.workflow_agent`
  - `RawMemoirRuntimeConnectivityData.capabilities.native_sse`
  - `RawMemoirRuntimeConnectivityData.capabilities.media`
  - `RawMemoirRuntimeConnectivityData.capabilities.model_enhancement_available`
- wrapper 现在调用 `request<RawMemoirRuntimeConnectivityData>()`，仅在 API 层通过 `mapMemoirRuntimeConnectivityData()` 白名单映射为既有 `MemoirRuntimeConnectivityData` camelCase 结构。
- 业务失败或缺失数据时原样返回请求层响应；该分支不读取、不记录 `raw`。
- hook 删除了重复映射逻辑，只消费 API wrapper 输出的数据模型。

### Medium：测试覆盖协议与并发行为

- 合同测试 mock 改为实际后端蛇形字段，断言 wrapper 导出的结果为 camelCase。
- 新增 wrapper 失败响应原样返回测试。
- hook 测试独立 mock camelCase API 输出，断言 hook 不包含 Raw DTO 或 mapping 函数。
- 新增 deferred 请求并发测试：加载中连续两次调用 `checkConnectivity()` 时仅产生一次 API 请求；完成后状态为 `success`。

### Low：范围化质量修复

- 修复 ESLint 的 Promise 参数命名、函数签名缩进和 Vue 模板格式。
- 修复 Stylelint 的 SCSS 属性顺序、规则间空行和可缩短的十六进制色值。
- 保留 `scoped` SCSS，透明色继续使用旧式 `rgba(...)` 语法。

## TDD 证据

1. 修订后的合同测试先运行：失败。
   - API 直接返回蛇形字段，未映射为 camelCase。
   - hook 包含 `createSafeConnectivityData`，违反“hook 不做映射”。
2. 最小实现后重新运行：通过。

## 最终验证

| 命令 | 结果 |
| --- | --- |
| `node --test script/tests/memoir-runtime-connectivity-contract.test.cjs` | 通过，6/6 子测试通过。 |
| 范围化 `eslint`（测试、API、hook、页面） | 通过，无错误。 |
| 范围化 `stylelint`（页面） | 通过，无错误；仅输出仓库配置中五项废弃规则警告。 |
| `npm run type-check` | 失败，退出码 2；最终输出未包含 `memoir-runtime-connectivity` 路径。 |
| `git diff --check` | 通过，无空白错误。 |

## 类型检查阻断项

`vue-tsc --noEmit` 仍由无关历史错误阻断，主要位于以下范围：

- `src/App.vue` 的 Uni 原生事件类型缺失。
- `src/uni-module-common/**` 的 `wx` 全局、上传类型、旧 `.ts` 导入、HTTP 类型与存量 store 字段错误。
- `src/uni_modules/pages-diary/**`、`pages-handbook/**`、`pages-mine/mine-main/**` 和 `uni-module-public/**` 的既有页面类型错误。

本次最初引入的 API 失败响应泛型转换错误已修复；最终类型检查不再报告本任务新增的 API、hook 或页面文件。

## 限制与风险

- 项目未配置 coverage 工具；按任务要求未新增测试依赖，因此没有覆盖率百分比。
- 目标工作树还包含后端 Runtime 联通相关修改，这些文件不属于本 Task 4 前端范围，未触碰。

## 二次审查修复：请求层原始响应隔离

### 问题

上一版成功分支通过展开请求层响应、失败分支通过类型转换返回请求层响应，可能把 `ApiResponse.raw` 带到 hook 或页面，违反前端数据边界。

### 修复

- 移除 API wrapper 对 `ApiResponse` 的对外类型依赖。
- 在 API 文件内定义私有 `ConnectivityResponse` 联合类型，只允许：
  - `{ success: true, data: MemoirRuntimeConnectivityData }`
  - `{ success: false, error?: string }`
- 成功时创建新对象并白名单映射 snake_case 数据。
- 失败时创建仅含 `success` 与安全 `error` 摘要的新对象；不读取、不记录请求层扩展字段。
- hook 无需导入该私有类型；由函数返回值自动完成安全分支收窄。

### 回归测试与验证

1. 更新合同测试后先运行失败：成功响应含 `raw`，失败响应仍与请求层响应同一对象。
2. 修复后运行：
   - `node --test script/tests/memoir-runtime-connectivity-contract.test.cjs`：通过，6/6。
   - 成功响应断言不含 `raw`。
   - 失败响应断言为全新对象，仅含 `success: false` 和安全 `error`，不含 `data` 或 `raw`。
   - 保留后端 snake_case 映射与 hook 加载中单请求并发测试。
3. 范围化 `eslint`（测试、API、hook、页面）：通过，无错误。
4. 范围化 `stylelint`（页面）：通过，无错误；仅输出仓库既有废弃规则警告。
5. 最终 `npm run type-check`：新增 API、hook、页面路径均不再出现在输出；命令仍由既有 `src/App.vue`、`src/uni-module-common/**`、日记/手帐/Mine 存量类型错误以退出码 2 结束。

### 联合类型收窄修复

- 全量类型检查曾发现 hook 在仓库非严格配置下无法从 `else` 分支自动收窄可选 `error` 字段。
- hook 现在按 `result.success` 处理成功数据，并使用 `'error' in result` 读取失败摘要；读取对象仍是 API wrapper 的安全联合结果，不会接触请求层扩展字段。
- 修复后再次通过 6/6 合同测试、范围化 ESLint、范围化 Stylelint 和全量类型检查的新增路径验证。
