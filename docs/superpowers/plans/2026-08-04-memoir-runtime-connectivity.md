# 回忆录 Runtime 联通测试实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在真实 `couple-diary-f`、`couple-diary-b` 和 AgentRuntime 之间建立只验证连接与协议的回忆录 Runtime 测试入口。

**Architecture:** 前端只调用 `/memory/runtime-connectivity`；业务后端验证用户 JWT、生成 Runtime Service HMAC、调用 Runtime capabilities 并过滤安全摘要；AgentRuntime 复用现有 capabilities 接口，不创建 Agent Run、Archive、Snapshot 或 Published Revision。

**Tech Stack:** UniApp + Vue 3 + TypeScript；FastAPI + Pydantic + httpx；AgentRuntime FastAPI；Node `node:test`；pytest + pytest-asyncio；现有 HMAC 签名合同。

## Global Constraints

- 前端只能调用 `couple-diary-b`，不能出现 Runtime URL、HMAC、Prompt、模型原文或工具 Payload。
- 前端 API wrapper 使用 `/memory/runtime-connectivity`，不能重复拼接 `/api/v1`。
- development/test 显示测试入口，production 不显示且不能从“我的”页进入。
- 后端 Runtime HMAC canonical string 固定为 `METHOD + PATH + TIMESTAMP + SHA256(body)`。
- Runtime 使用独立 test 数据库和独立密钥，不复用情侣日记业务库或用户凭据。
- 错误响应只暴露受控错误码：`RUNTIME_NOT_CONFIGURED`、`RUNTIME_TIMEOUT`、`RUNTIME_UNAVAILABLE`、`RUNTIME_AUTH_FAILED`、`RUNTIME_CONTRACT_INVALID`。
- 日志只记录接口、状态、耗时、错误码和布尔摘要，不记录 Secret、Token、完整响应或业务内容。
- 新增或修改的页面、hooks、types、API 字段和关键状态补充中文业务注释。
- 不修改 `src/uni-module-common/` 公共源码；前端新 API 放在当前页面模块 hooks 中。
- 不创建 Git commit；由主会话统一保留工作区变更。

---

### Task 1: 冻结业务后端联通合同并先写失败测试

**Files:**
- Create: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/tests/test_memory_runtime_connectivity.py`
- Create: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/app/schemas/memory_runtime.py`
- Create: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/app/services/memory_runtime_client.py`
- Create: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/app/services/memory_runtime_connectivity_service.py`
- Create: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/app/api/endpoints/memory_api.py`
- Modify: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/app/api/api.py`

**Interfaces:**
- Produces `GET /api/v1/memory/runtime-connectivity`。
- Produces `RuntimeConnectivityData`，字段为 `runtime_reachable: bool`、`contract_version: str`、`capabilities` 安全摘要。
- Produces `MemoryRuntimeClient.get_capabilities() -> dict[str, object]` 和 `MemoryRuntimeConnectivityService.check() -> RuntimeConnectivityData`。

- [ ] **Step 1: Write failing route and client-contract tests**

```python
# tests/test_memory_runtime_connectivity.py

def test_runtime_connectivity_requires_login(client):
    response = client.get("/api/v1/memory/runtime-connectivity")
    assert response.status_code == 401
    assert response.json()["ret"][0].startswith("ERROR::")


def test_runtime_connectivity_returns_safe_summary(client, app):
    app.dependency_overrides[get_current_user_from_token] = override_current_user
    try:
        with patch(
            "app.services.memory_runtime_connectivity_service.MemoryRuntimeClient.get_capabilities",
            new=AsyncMock(return_value={
                "contract_version": "1.0.0",
                "package_digest": "private-digest",
                "agents": [{"agent_id": "memoir_agent", "version": "1.0.0"}],
                "model_policies": [],
                "capabilities": {
                    "workflow_agent": True,
                    "native_sse": False,
                    "media": False,
                    "model_enhancement_available": False,
                },
            }),
        ):
            response = client.get("/api/v1/memory/runtime-connectivity")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["runtime_reachable"] is True
    assert data["contract_version"] == "1.0.0"
    assert "package_digest" not in str(response.json())
    assert "private-digest" not in str(response.json())


def test_runtime_connectivity_maps_upstream_errors_to_controlled_code(client, app):
    app.dependency_overrides[get_current_user_from_token] = override_current_user
    try:
        with patch(
            "app.services.memory_runtime_connectivity_service.MemoryRuntimeClient.get_capabilities",
            new=AsyncMock(side_effect=MemoryRuntimeClientError("RUNTIME_TIMEOUT")),
        ):
            response = client.get("/api/v1/memory/runtime-connectivity")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["ret"] == ["ERROR::RUNTIME_TIMEOUT"]
```

同时写 HMAC client 的单元测试，固定 `GET`、`/api/v1/runtime/capabilities`、空 body SHA-256、四个 `X-Agent-*` 请求头，并覆盖配置缺失、超时、401 和非 JSON 响应。

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
cd /Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b
poetry run pytest tests/test_memory_runtime_connectivity.py -q
```

Expected: FAIL because the memory route, schemas, client and service do not exist yet。

- [ ] **Step 3: Define the typed safe response and error contracts**

`app/schemas/memory_runtime.py` 定义 Pydantic 模型：

```python
class RuntimeConnectivityCapabilities(BaseModel):
    workflow_agent: bool
    native_sse: bool
    media: bool
    model_enhancement_available: bool


class RuntimeConnectivityData(BaseModel):
    runtime_reachable: bool
    contract_version: str
    capabilities: RuntimeConnectivityCapabilities
```

`MemoryRuntimeClientError` 只接受上述五个错误码，拒绝把 httpx 异常文本直接作为业务错误码。

- [ ] **Step 4: Run tests again and verify only implementation failures remain**

Run the same pytest command. Expected: imports resolve, assertions still fail until the client and route are implemented。

---

### Task 2: 实现业务后端 Runtime façade

**Files:**
- Modify: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/app/core/config.py`
- Modify: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/app/api/api.py`
- Modify: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/app/api/endpoints/memory_api.py`
- Modify: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/app/services/memory_runtime_client.py`
- Modify: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/app/services/memory_runtime_connectivity_service.py`
- Test: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/tests/test_memory_runtime_connectivity.py`

**Interfaces:**
- Consumes existing `get_current_user_from_token`, `build_api_response_from_request` and `settings`。
- Produces `MemoryRuntimeConfig` with `base_url`, `client_id`, `key_id`, `secret`, `timeout_seconds`。
- Produces safe `RuntimeConnectivityData`; no package digest, agent ID, URL or raw Runtime body crosses the route boundary。

- [ ] **Step 1: Add backend Runtime configuration with safe defaults**

在 `Settings` 增加：

```python
MEMORY_RUNTIME_BASE_URL: str = ""
MEMORY_RUNTIME_CLIENT_ID: str = ""
MEMORY_RUNTIME_KEY_ID: str = ""
MEMORY_RUNTIME_SECRET: str = ""
MEMORY_RUNTIME_TIMEOUT_SECONDS: float = 5.0
```

增加 `MemoryRuntimeConfig` 分组属性。配置缺失不阻止普通业务后端启动；联通接口在调用时返回 `RUNTIME_NOT_CONFIGURED`。只允许 `http://` 或 `https://` origin，不接受 query、fragment 或用户输入路径。

- [ ] **Step 2: Implement exact Runtime HMAC request**

`MemoryRuntimeClient.get_capabilities()` 使用：

```python
path = "/api/v1/runtime/capabilities"
body = b""
timestamp = str(int(time.time()))
canonical = "\\n".join(("GET", path, timestamp, hashlib.sha256(body).hexdigest()))
signature = hmac.new(
    config.secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
).hexdigest()
headers = {
    "X-Agent-Client-Id": config.client_id,
    "X-Agent-Key-Id": config.key_id,
    "X-Agent-Timestamp": timestamp,
    "X-Agent-Signature": signature,
}
```

使用 `httpx.AsyncClient(timeout=config.timeout_seconds)`，将异常映射为固定错误码：

- 配置空值：`RUNTIME_NOT_CONFIGURED`
- `httpx.TimeoutException`：`RUNTIME_TIMEOUT`
- 401：`RUNTIME_AUTH_FAILED`
- 其他 HTTP/网络错误：`RUNTIME_UNAVAILABLE`
- JSON 缺失或类型错误：`RUNTIME_CONTRACT_INVALID`

日志只记录错误码和耗时，不记录 headers、URL query 或 response body。

- [ ] **Step 3: Validate and project the Runtime contract**

`MemoryRuntimeConnectivityService` 校验：

- `contract_version` 为字符串且 major 为 `1`
- `agents` 包含 `memoir_agent` / `1.0.0`
- `capabilities` 四个布尔字段全部存在
- `package_digest` 只参与内部类型校验，不进入返回值

通过校验后只构造 `RuntimeConnectivityData(runtime_reachable=True, ...)`。不把 Runtime 的 `model_policies`、`package_digest` 或 agent 元数据返回前端。

- [ ] **Step 4: Add authenticated route and register it**

`memory_api.py` 使用现有依赖：

```python
@router.get("/runtime-connectivity", response_model=ApiResponseData)
async def get_runtime_connectivity(
    request: Request,
    current_user: CoupleUser = Depends(get_current_user_from_token),
):
    data = await service.check()
    return build_api_response_from_request(
        request,
        data=data.model_dump(),
        ret=["SUCCESS::回忆录 Runtime 联通检查成功"],
    )
```

服务错误统一抛出 `HTTPException(status_code=503, detail=error_code)`，让现有异常处理器返回 `ERROR::<code>`。日志只记录当前用户 ID、结果状态和错误码，不记录用户内容。

在 `app/api/api.py` 集中注册：

```python
api_router.include_router(memory_api.router, prefix="/memory", tags=["回忆录模块"])
```

- [ ] **Step 5: Run backend tests and static checks**

Run:

```bash
cd /Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b
poetry run pytest tests/test_memory_runtime_connectivity.py tests/test_auth_api.py -q
poetry run ruff check app tests/test_memory_runtime_connectivity.py
```

Expected: focused tests PASS; unrelated historical failures must be reported without changing them。

---

### Task 3: 冻结 AgentRuntime capabilities 兼容测试

**Files:**
- Modify: `/Users/yuye/YeahWork/AIAgent项目/com-agent-runtime/tests/test_runtime_capabilities.py`
- Modify: `/Users/yuye/YeahWork/AIAgent项目/com-agent-runtime/VERIFICATION.md`

**Interfaces:**
- Consumes existing `GET /api/v1/runtime/capabilities` and current test fixture HMAC。
- Produces a regression contract for `couple-diary-b` without changing Runtime endpoint behavior。

- [ ] **Step 1: Add a failing-safe-contract assertion**

扩展 Runtime capabilities 测试，断言：

```python
payload = response.json()
assert payload["contract_version"] == "1.0.0"
assert payload["agents"] == [{"agent_id": "memoir_agent", "version": "1.0.0"}]
assert set(payload["capabilities"]) == {
    "workflow_agent",
    "native_sse",
    "media",
    "model_enhancement_available",
}
serialized = str(payload)
assert all(secret not in serialized for secret in ("development-secret", "runtime-tool-development-secret"))
```

同时断言未签名、错误 key 和过期 timestamp 仍返回 401。

- [ ] **Step 2: Run Runtime capability tests**

Run:

```bash
cd /Users/yuye/YeahWork/AIAgent项目/com-agent-runtime
poetry run pytest tests/test_runtime_capabilities.py -q
```

Expected: PASS without修改 Runtime capabilities 业务逻辑；若合同断言失败，先修正合同兼容性再继续前端。

- [ ] **Step 3: Update Runtime verification documentation**

在 `VERIFICATION.md` 增加方案 A 验收段，明确真实前端路径为 `couple-diary-f`，真实后端路径为 `couple-diary-b`，并写清该探针不代表 Agent Run 或回忆录生成闭环。删除或标注旧 `uni-com-project-template` 路径，避免历史模板被当成验收证据。

---

### Task 4: 前端 API、状态 hook 和页面测试先行

**Files:**
- Create: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/frontend/couple-diary-f/script/tests/memoir-runtime-connectivity-contract.test.cjs`
- Create: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/frontend/couple-diary-f/src/uni_modules/pages-mine/pages/memoir-runtime-connectivity/hooks/memoir-runtime-api.ts`
- Create: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/frontend/couple-diary-f/src/uni_modules/pages-mine/pages/memoir-runtime-connectivity/hooks/use-memoir-runtime-connectivity.ts`
- Create: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/frontend/couple-diary-f/src/uni_modules/pages-mine/pages/memoir-runtime-connectivity/index.vue`
- Modify: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/frontend/couple-diary-f/package.json`

**Interfaces:**
- Produces `getMemoirRuntimeConnectivityApi(): Promise<ApiResponse<MemoirRuntimeConnectivityData>>`。
- Produces hook state `status: 'idle' | 'loading' | 'success' | 'error'`、`data`、`error`、`checkConnectivity()`。
- Page only consumes hook and renders safe summary。

- [ ] **Step 1: Write failing frontend contract tests**

测试 wrapper 和页面静态合同：

```javascript
const captured = []
const api = loadTypeScriptModule(
  'src/uni_modules/pages-mine/pages/memoir-runtime-connectivity/hooks/memoir-runtime-api.ts',
  { request: (config) => { captured.push(config); return Promise.resolve({ success: true, data: {} }) } }
)

await api.getMemoirRuntimeConnectivityApi()
assert.equal(captured[0].url, '/memory/runtime-connectivity')
assert.equal(captured[0].method, 'GET')
assert.equal(captured[0].custom.auth, true)
assert.equal(captured[0].custom.showLoading, false)
assert.doesNotMatch(JSON.stringify(captured[0]), /127\\.0\\.0\\.1:8010|X-Agent-Signature|prompt|model_policies/)
```

另外断言：

- `pages.json` 注册测试页路径。
- `use-couple-mine.ts` 仅 development/test 把 memories 卡片跳转到测试页。
- production 分支不调用 `uni.navigateTo` 进入该页。
- 页面模板包含“检查 Runtime 联通”按钮和成功/失败状态渲染。
- API wrapper 使用 `request<T>()`，不使用旧 `http` 和完整 Runtime URL。

- [ ] **Step 2: Run the focused frontend test and verify it fails**

Run:

```bash
cd /Users/yuye/YeahWork/Python项目/couple-diary-doc/frontend/couple-diary-f
node --test script/tests/memoir-runtime-connectivity-contract.test.cjs
```

Expected: FAIL because the new page, hook and API files do not exist。

- [ ] **Step 3: Define typed frontend API and hook**

API 类型字段补充中文注释：

```typescript
export interface MemoirRuntimeConnectivityData {
  /** 后端是否完成 Runtime capabilities 探测。 */
  runtimeReachable: boolean;
  /** 经过后端过滤的 Runtime 合同版本。 */
  contractVersion: string;
  /** 只包含可展示的能力布尔值。 */
  capabilities: {
    workflowAgent: boolean;
    nativeSse: boolean;
    media: boolean;
    modelEnhancementAvailable: boolean;
  };
}
```

hook 只保存上述安全字段和错误摘要。请求失败时恢复 `error` 状态，不重复弹出通用失败 toast；公共请求层已经处理失败提示。

- [ ] **Step 4: Implement page-level UI**

页面使用 `script setup + TypeScript`，只组合 hook 和页面级展示：

- 首次进入显示“尚未检查”。
- 点击按钮调用 `checkConnectivity()`。
- loading 时禁用按钮。
- success 显示 `runtimeReachable`、`contractVersion` 和四项能力状态。
- error 只显示后端返回的安全错误摘要和“重新检查”。
- 使用 `scoped scss`，颜色函数使用 `rgba(...)`，不重复声明同名选择器。

- [ ] **Step 5: Run frontend tests and type check**

Run:

```bash
node --test script/tests/memoir-runtime-connectivity-contract.test.cjs
npm run type-check
```

Expected: contract tests PASS；若 `type-check` 暴露已有历史错误，只记录文件和错误，不修改无关模块。

---

### Task 5: 接通“我的页”回忆录测试入口并注册路由

**Files:**
- Modify: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/frontend/couple-diary-f/src/uni_modules/pages-mine/mine-main/use-couple-mine.ts`
- Modify: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/frontend/couple-diary-f/src/pages.json`
- Modify: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/frontend/couple-diary-f/script/tests/memoir-runtime-connectivity-contract.test.cjs`

**Interfaces:**
- Consumes `mode` from existing config and existing `MineFeatureCard` click flow。
- Produces navigation to `/uni_modules/pages-mine/pages/memoir-runtime-connectivity/index` only for logged-in bound users in `development` or `test`。

- [ ] **Step 1: Add explicit environment-gating assertions**

测试应验证：

```javascript
assert.match(mineSource, /mode/)
assert.match(mineSource, /development/)
assert.match(mineSource, /test/)
assert.match(mineSource, /memoir-runtime-connectivity/)
assert.doesNotMatch(mineSource, /MEMORY_RUNTIME_BASE_URL|X-Agent-Signature|RUNTIME_SECRET/)
```

同时检查 `pages.json` 的页面路径和 `package.json` 的 `test:memoir-runtime-connectivity-contract` 脚本。

- [ ] **Step 2: Run the entry-contract test and verify the new assertions fail**

Run:

```bash
node --test script/tests/memoir-runtime-connectivity-contract.test.cjs
```

Expected: FAIL until the Mine hook and route are wired。

- [ ] **Step 3: Implement the minimal bound-user entry**

在 `use-couple-mine.ts`：

- `development` / `test` 的已绑定 memories 卡片文案改为 Runtime 联通测试。
- 生产环境保持“回忆录功能即将开放”，不跳转测试页。
- 未登录和未绑定态继续保留现有锁定行为。
- 进入页面使用 `uni.navigateTo`，不在 hook 中调用 `useRouter()`。
- 事件日志只记录卡片标识、环境和关系状态，不记录 token 或私有地址。

在 `pages.json` 的 `pages-mine` 子包注册页面，保持现有 navigation style。

- [ ] **Step 4: Run frontend contract tests**

Run:

```bash
node --test script/tests/memoir-runtime-connectivity-contract.test.cjs
npm run test:bucket-list-contract
npm run test:daily-care-contract
```

Expected: 新增合同和相邻入口测试 PASS；历史无关失败单独记录。

---

### Task 6: 同步三个工程及产品文档

**Files:**
- Modify: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/frontend/couple-diary-f/README.md`
- Modify: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/README.md`
- Modify: `/Users/yuye/YeahWork/AIAgent项目/com-agent-runtime/VERIFICATION.md`
- Modify: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/头脑风暴/docs/superpowers/回忆录/需求设计文档.md`

**Interfaces:**
- Documentation must describe the same endpoint, environment gate, HMAC ownership and non-goals as the code。

- [ ] **Step 1: Document frontend entry and command**

在真实前端 README 增加：

```text
开发/测试环境：我的 -> 回忆录档案 -> Runtime 联通测试。
该页面只请求 couple-diary-b，不直连 AgentRuntime。
```

记录 `node --test script/tests/memoir-runtime-connectivity-contract.test.cjs` 和 `npm run type-check`。

- [ ] **Step 2: Document backend façade and configuration**

在后端 README 增加配置项名称、接口路径和错误码，但不写任何真实 secret：

```text
MEMORY_RUNTIME_BASE_URL
MEMORY_RUNTIME_CLIENT_ID
MEMORY_RUNTIME_KEY_ID
MEMORY_RUNTIME_SECRET
MEMORY_RUNTIME_TIMEOUT_SECONDS
```

明确后端才持有 Runtime HMAC，联通接口不创建回忆录业务状态。

- [ ] **Step 3: Correct Runtime verification references**

在 Runtime `VERIFICATION.md` 中把旧模板前端路径标为历史参考，改为真实 `couple-diary-f` 和 `couple-diary-b` 的三端验证顺序，并明确 capabilities 绿色不代表完整生成闭环。

- [ ] **Step 4: Add product-document architecture note**

在回忆录需求文档的核心流程前增加当前实施说明：前端只对接 `couple-diary-b`，业务后端再调用 AgentRuntime；本阶段为连接级验证，Archive/Snapshot/Worker/Callback 属于后续 B1/B2。

- [ ] **Step 5: Check documentation consistency**

Run:

```bash
rg -n "uni-com-project-template|couple-diary-f|couple-diary-b|runtime-connectivity|RUNTIME_SECRET" \
  /Users/yuye/YeahWork/AIAgent项目/com-agent-runtime/VERIFICATION.md \
  /Users/yuye/YeahWork/Python项目/couple-diary-doc/frontend/couple-diary-f/README.md \
  /Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/README.md \
  /Users/yuye/YeahWork/Python项目/couple-diary-doc/头脑风暴/docs/superpowers/回忆录/需求设计文档.md
```

Expected: 旧模板路径只保留明确的历史说明；三个工程使用相同接口和边界描述。

---

### Task 7: 执行三端验证并记录真实冒烟结果

**Files:**
- Modify: `/Users/yuye/YeahWork/AIAgent项目/com-agent-runtime/VERIFICATION.md` only after successful real smoke
- Modify: `/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b/README.md` only if command/config corrections are discovered

**Interfaces:**
- Consumes all previous tasks。
- Produces focused test reports and one真实 development/test 冒烟结果。

- [ ] **Step 1: Run Runtime focused tests and lint**

```bash
cd /Users/yuye/YeahWork/AIAgent项目/com-agent-runtime
poetry run pytest tests/test_runtime_capabilities.py -q
poetry run ruff check app tests/test_runtime_capabilities.py
```

- [ ] **Step 2: Run backend focused tests and lint**

```bash
cd /Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b
poetry run pytest tests/test_memory_runtime_connectivity.py tests/test_auth_api.py -q
poetry run ruff check app tests/test_memory_runtime_connectivity.py
```

- [ ] **Step 3: Run frontend contract tests and type check**

```bash
cd /Users/yuye/YeahWork/Python项目/couple-diary-doc/frontend/couple-diary-f
node --test script/tests/memoir-runtime-connectivity-contract.test.cjs
npm run type-check
```

- [ ] **Step 4: Run the real local smoke path**

Use isolated test configuration only:

```bash
# Terminal 1: AgentRuntime test environment
cd /Users/yuye/YeahWork/AIAgent项目/com-agent-runtime
./agent-runtime.sh start test

# Terminal 2: couple-diary-b with MEMORY_RUNTIME_* test values
cd /Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b
poetry run uvicorn app.main:app --host 127.0.0.1 --port 8008

# Terminal 3: real frontend test build
cd /Users/yuye/YeahWork/Python项目/couple-diary-doc/frontend/couple-diary-f
npm run dev:mp-weixin -- --mode test
```

登录测试账号后，从“我的 -> 回忆录档案 -> Runtime 联通测试”点击检查。验收结果必须同时满足：

- 前端页面成功显示 `runtime_reachable=true`。
- 后端日志只出现请求摘要、状态和错误码。
- Runtime 日志无前端 token、业务正文和 Runtime secret。
- Runtime 数据库没有新增 Agent Run、Archive、Snapshot 或 Published Revision。

- [ ] **Step 5: Run whitespace and repository status checks**

```bash
git -C /Users/yuye/YeahWork/AIAgent项目/com-agent-runtime diff --check
git -C /Users/yuye/YeahWork/Python项目/couple-diary-doc diff --check
git -C /Users/yuye/YeahWork/AIAgent项目/com-agent-runtime status --short
git -C /Users/yuye/YeahWork/Python项目/couple-diary-doc status --short
```

Only report files belonging to方案 A；不删除或重置用户已有修改。

- [ ] **Step 6: Record only verified smoke evidence**

只有真实命令和页面冒烟都成功时，才在 `VERIFICATION.md` 写入日期、三个工程路径、测试命令和“连接级通过”；不得写“回忆录生成闭环完成”。
