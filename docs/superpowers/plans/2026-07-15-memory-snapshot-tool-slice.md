# 回忆录快照工具纵向切片 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使 `memoir_agent` 能通过受签名保护的 `memory.get_snapshot` HTTP Business Tool 读取已冻结的回忆录快照。

**Architecture:** 业务侧 endpoint 只接受 Runtime 服务身份签名，并通过 `MemorySnapshotService` 读取同 archive 的加密快照；Runtime `ToolGateway` 只允许已注册 connector 与 manifest 相对路径。结果只进入执行内存态，日志和 Artifact 不保留正文。

**Tech Stack:** FastAPI、httpx、SQLAlchemy、cryptography Fernet、pytest。

## Global Constraints

- Runtime 不直连业务数据库，业务数据只通过 HTTP Business Tool 获取。
- 日记正文、工具原始入参和工具原始结果不得写入日志、checkpoint 摘要或 Artifact。
- connector 地址只能由服务端配置提供；AgentPackage 只提供相对路径。
- 所有读取前验证 Runtime 身份、时间戳、HMAC 与 archive/snapshot 归属关系。

---

### Task 1: 业务快照读取与内部接口

**Files:**
- Create: `app/services/memory_snapshot_service.py`
- Create: `app/api/endpoints/memory_tools_api.py`
- Modify: `app/api/api.py`
- Test: `tests/test_memory_snapshot_tool.py`

- [ ] 写入失败测试：不存在、跨 archive 或签名错误的请求均被拒绝；合法请求只返回解密后的冻结 payload。
- [ ] 运行：`poetry run pytest tests/test_memory_snapshot_tool.py -q`，预期失败。
- [ ] 实现服务端身份验签、snapshot 归属校验和 JSON response。
- [ ] 再次运行同一测试，预期通过。

### Task 2: Runtime ToolGateway 与 load_snapshot Runner

**Files:**
- Create: `app/runtime/tool_gateway.py`
- Create: `app/agents/memoir_agent/runner.py`
- Test: `tests/test_runtime_snapshot_tool_gateway.py`

- [ ] 写入失败测试：Gateway 拒绝未注册 connector/完整 URL，并将合法快照结果写入 `state.snapshot`，不产生包含正文的 Artifact 摘要。
- [ ] 运行：`poetry run pytest tests/test_runtime_snapshot_tool_gateway.py -q`，预期失败。
- [ ] 实现固定 connector 请求、HMAC headers、超时与安全日志；实现 `load_snapshot` 节点 Runner。
- [ ] 再次运行同一测试，预期通过。

### Task 3: 回归与计划状态

- [ ] 运行 `poetry run pytest -q` 与 `poetry run ruff check app tests`。
- [ ] 在 AgentRuntime 总控计划中仅标记已实现的 `memory.get_snapshot` 相关条目；保留 publish、重试、写工具幂等与模型节点为未完成。
