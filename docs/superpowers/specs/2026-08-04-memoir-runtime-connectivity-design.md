# 回忆录 Runtime 联通测试设计

## 目标

为真实“心约手帐”建立一条只验证连接和协议的安全测试链路：

```text
couple-diary-f
  -> couple-diary-b
  -> AgentRuntime capabilities
  -> 安全摘要返回前端
```

本阶段不创建 Agent Run，不生成 Archive/Snapshot，不启动 Worker，也不切换任何回忆录发布版本。

## 工程边界

- `couple-diary-f` 是唯一前端工程，前端只调用 `couple-diary-b`。
- `couple-diary-b` 是唯一业务 API 边界，负责用户 JWT、Runtime 配置和 Runtime Service HMAC。
- AgentRuntime 只提供已存在的 capabilities 探针，不向小程序暴露内部凭据或内部业务状态。
- development/test 才显示测试入口；production 不注册、不渲染测试入口。
- Runtime 使用独立的 development/test 数据库和独立密钥，不能复用情侣日记业务库或用户密钥。

## 前端设计

在真实前端的“我的 -> 回忆录”入口增加 Runtime 联通测试页。页面通过现有 `request<T>()` 和 `custom.auth: true` 调用：

```text
GET /memory/runtime-connectivity
```

环境 API base 已包含 `/api/v1`，因此 wrapper 不重复添加 `/api/v1`。

页面只展示以下受控字段：

- `runtime_reachable`
- `contract_version`
- capabilities summary
- 受控错误码

前端禁止持有或展示 Runtime URL、HMAC、Agent ID、Prompt、模型原文、工具 Payload 和完整 Runtime 响应。

## 业务后端设计

新增受保护业务接口：

```text
GET /api/v1/memory/runtime-connectivity
```

处理顺序：

1. 通过现有 Bearer JWT 解析当前用户。
2. 读取后端 Runtime 配置。
3. 按 Runtime 的 `METHOD + PATH + TIMESTAMP + SHA256(body)` 规则签名。
4. 调用 `GET /api/v1/runtime/capabilities`。
5. 校验合同版本、MemoirAgent 摘要和必要能力字段。
6. 过滤为前端安全摘要。
7. 通过项目统一 API response 返回。

后端向前端只暴露稳定错误码：

- `RUNTIME_NOT_CONFIGURED`
- `RUNTIME_TIMEOUT`
- `RUNTIME_UNAVAILABLE`
- `RUNTIME_AUTH_FAILED`
- `RUNTIME_CONTRACT_INVALID`

Runtime 原始 URL、请求头、签名、密钥、完整响应和异常堆栈不得进入响应或普通日志。

## Runtime 设计

- 复用现有 `GET /api/v1/runtime/capabilities`。
- 保持 service HMAC 验证。
- 不新增业务表和 Agent Run。
- 增加后端兼容性测试所需的稳定响应合同。
- 继续保留 capability 摘要中的模型增强关闭状态和媒体关闭状态。

## 测试设计

### 前端

- 测试 API wrapper 的路径、方法和 `custom.auth`。
- 测试测试入口只在 development/test 可见。
- 测试 production 不出现 Runtime 直连地址、HMAC 或内部字段。
- 使用现有 Node `node:test` 契约测试方式，不新增测试框架。

### 业务后端

- 使用 `httpx.MockTransport` 验证 HMAC 请求方法、路径、时间戳和 body 摘要。
- 使用 FastAPI dependency override 验证 JWT 保护和统一响应。
- 覆盖 Runtime 超时、401、503、合同不完整等错误映射。
- 验证失败时不创建 Archive、Snapshot 或 Agent Run。

### AgentRuntime

- 验证有效 service HMAC 可以访问 capabilities。
- 验证无效、过期或缺失签名返回 401。
- 验证响应只包含允许的 capabilities 摘要。
- 使用 Runtime 独立 test 数据库执行真实探针测试。

### 三端冒烟

在 development/test 环境启动三个工程后，执行真实链路并确认：

```text
前端测试页
  -> couple-diary-b 受保护接口
  -> AgentRuntime capabilities
  -> 前端显示可达状态
```

该冒烟不代表回忆录生成完成；完整生成闭环属于后续 B1 阶段。

## 验收标准

1. 未登录访问业务后端接口返回 401。
2. 前端只请求 `couple-diary-b`，不包含 Runtime 直连逻辑。
3. 有效配置下后端可以通过 HMAC 读取 Runtime capabilities。
4. Runtime 不可达、超时、验签失败和合同异常均返回稳定错误码。
5. 测试请求不创建 Agent Run、Archive、Snapshot 或 Published Revision。
6. 日志和响应不包含 Secret、Token、Prompt、模型正文或完整工具 Payload。
7. production 不显示测试入口。
8. 三端真实本地冒烟通过后，才能在验证文档中记录联通结果。

## 非目标

- 回忆录密码设置和解锁。
- 归档列表、详情、删除、置顶。
- Snapshot 冻结和素材过滤。
- MemoirAgent 生成、Worker、business tool、callback。
- 媒体、TTS、图片或视频生成。
