# AgentRuntime 根工程化迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `services/agent-runtime` 迁入当前根工程，使根工程成为唯一的 AgentRuntime 与情侣日记后端工程。

**Architecture:** 根 `app` 承载业务与 Runtime 领域模块；Runtime 复用根配置、数据库 Base、Alembic、日志和测试入口，并使用 `/api/v1/runtime` 作为路由前缀。

**Tech Stack:** Python 3.13、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、PyYAML、pytest、ruff、Poetry。

## Global Constraints

- 当前根目录是唯一工程，禁止新建 `services/agent-runtime`、第二份 `pyproject.toml`、第二份 Alembic 或同名 `app` 包。
- Runtime 仅经业务工具读写情侣日记和回忆录数据。
- ORM 模型、字段和新增逻辑使用中文注释；日志只记录标识、状态、计数和脱敏摘要。
- 根 `pyproject.toml`、`alembic/`、`tests/` 是唯一依赖、迁移和测试入口。
- 每个生产代码任务先写失败测试，再实现最小变更。

### Task 1: 修订工程入口与两份既有开发计划

**Files:** Modify `pyproject.toml`, `README.md`, `头脑风暴/docs/AgentRuntime/plans/2026-07-07-AgentRuntime-总控开发计划.md`, `头脑风暴/docs/AgentRuntime/backend/2026-07-07-AgentRuntime-后端开发计划.md`, `tests/test_project_structure_script.py`.

**Interfaces:** 产出根工程唯一约束；两份计划不再出现 `services/agent-runtime/`。

- [ ] 写测试：

```python
def test_agent_runtime_plans_forbid_nested_project() -> None:
    for path in [MASTER_PLAN, BACKEND_PLAN]:
        text = path.read_text(encoding="utf-8")
        assert "services/agent-runtime/" not in text
        assert "当前 com-agent-runtime 根工程" in text
```

- [ ] 运行 `poetry run pytest tests/test_project_structure_script.py::test_agent_runtime_plans_forbid_nested_project -v`，确认因旧路径存在而失败。
- [ ] 在根依赖加入 `PyYAML>=6.0.0`；更新 README；将两份计划的所有嵌套路径替换为根路径，删除独立工程任务，加入禁止嵌套工程的全局约束和验收项。
- [ ] 运行 `poetry run pytest tests/test_project_structure_script.py::test_agent_runtime_plans_forbid_nested_project -v && poetry check`，确认通过。
- [ ] 提交信息：`docs: make root project the runtime authority`。

### Task 2: 迁入 Runtime 契约、AgentPackage 与运行核心

**Files:** Create `app/contracts/`, `app/runtime/`, `app/schemas/{agent_package,agent_run,audit,plan}.py`, `app/agents/memoir_agent/1.0.0/`, `tests/test_runtime_contract_compatibility.py`, `tests/test_runtime_agent_package_loader.py`, `tests/fixtures/runtime-contract-v1.0.0.json`.

**Interfaces:** 产出 `CONTRACT_VERSION`、`CreateAgentRunRequest`、`AgentPackageService.load()` 和 `StaticPlanner.build()`。

- [ ] 写测试：

```python
def test_root_contract_exports_versioned_schema() -> None:
    from app.contracts.schema_export import export_contract_schema
    assert export_contract_schema()["contract_version"] == "1.0.0"

def test_root_package_loader_reads_memoir_agent() -> None:
    from app.services.agent_package_service import AgentPackageService
    assert AgentPackageService().load("memoir_agent", "1.0.0").agent_id == "memoir_agent"
```

- [ ] 运行两个测试，确认根 Runtime 模块不存在导致失败。
- [ ] 从嵌套工程迁入契约、schema、runtime、MemoirAgent 文件和 package loader；移除嵌套 Settings、Base、Session、FastAPI app 引用，并补中文注释及安全日志。
- [ ] 运行 `poetry run pytest tests/test_runtime_contract_compatibility.py tests/test_runtime_agent_package_loader.py -v`，确认通过。
- [ ] 提交信息：`feat: move runtime contracts and agent package to root`。

### Task 3: 合并 Runtime ORM 模型与根 Alembic

**Files:** Modify `app/models/__init__.py`; create `app/models/runtime.py`, `alembic/versions/20260715_1600_add_agent_runtime_tables.py`, `tests/test_runtime_models_metadata.py`; modify `tests/test_alembic_metadata.py`.

**Interfaces:** 所有 Runtime 表由 `app.db.sqlalchemy_db.Base` 管理，根 Alembic metadata 可以发现。

- [ ] 写测试：

```python
def test_root_metadata_contains_runtime_tables() -> None:
    from app.db.metadata import get_target_metadata
    assert {"agent_runs", "agent_steps", "runtime_outbox_events"} <= set(get_target_metadata().tables)
```

- [ ] 运行该测试，确认 Runtime 表缺失导致失败。
- [ ] 迁入模型并改为 `from app.db.sqlalchemy_db import Base`；为表和隐私、状态、fencing、审计字段添加中文注释；创建以根迁移最新版本为 `down_revision` 的新迁移。
- [ ] 运行 `poetry run pytest tests/test_runtime_models_metadata.py tests/test_alembic_metadata.py -v && poetry run alembic check`，确认通过。
- [ ] 提交信息：`feat: add runtime models to root alembic`。

### Task 4: 合并 Runtime 服务、路由、Worker 与 Dispatcher

**Files:** Create `app/services/{admission,agent_package,agent_run,audit,idempotency,lease,outbox,run_queue}_service.py`, `app/core/{authorization,connectors,security}.py`, `app/api/endpoints/{runtime_agent_runs,runtime_capabilities,runtime_health}_api.py`, `app/worker.py`, `app/dispatcher.py`; modify `app/api/api.py`; create `tests/test_runtime_*.py`。

**Interfaces:** 产出 `/api/v1/runtime/health/*`、`/api/v1/runtime/capabilities`、`/api/v1/runtime/agent-runs` 和 `python -m app.worker --once`。

- [ ] 写测试：

```python
def test_runtime_capabilities_requires_signed_caller(client) -> None:
    assert client.get("/api/v1/runtime/capabilities").status_code == 401

def test_worker_module_exposes_main() -> None:
    from app.worker import main
    assert callable(main)
```

- [ ] 运行目标测试，确认根路由与 Worker 缺失导致失败。
- [ ] 迁入服务、鉴权、connector、签名、dispatcher 与 worker；服务层复用根 Session，路由注册 `prefix="/runtime"`；为 run 状态、attempt、lease 与 outbox 记录安全日志。
- [ ] 运行 `poetry run pytest tests/test_runtime_*.py -v`，确认 Runtime 测试通过。
- [ ] 提交信息：`feat: run agent runtime from root application`。

### Task 5: 删除旧工程并验证单工程结果

**Files:** Delete all tracked files under `services/agent-runtime/`; modify `README.md`, `头脑风暴/docs/AgentRuntime/需求设计文档.md`, `tests/test_project_structure_script.py`.

**Interfaces:** 产出不含嵌套 Runtime 工程的仓库。

- [ ] 写测试：

```python
def test_nested_agent_runtime_project_is_absent() -> None:
    assert not (PROJECT_ROOT / "services" / "agent-runtime").exists()
```

- [ ] 运行该测试，确认旧目录存在导致失败。
- [ ] 删除受版本控制的旧配置、迁移、实现、测试、lock 文件和 README；文档统一改为根工程 Runtime 模块。
- [ ] 运行 `poetry run pytest && poetry run ruff check app tests && poetry check && git ls-files | rg '^services/agent-runtime/'`，确认前三项成功且最后一项无输出。
- [ ] 提交信息：`refactor: remove nested agent runtime project`。

## 自检

- 覆盖根依赖、README、两份既有计划、Runtime 代码、AgentPackage、模型、迁移、路由、Worker、测试与旧目录删除。
- Runtime API 统一为 `/api/v1/runtime`，Runtime 模型统一复用根 `Base`。
