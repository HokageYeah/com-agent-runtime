# LLM Prompts

这份文档用于给后续 ChatGPT / Codex / Claude 等 LLM 接手 `com-agent-runtime` 时直接复制使用。

## 最短实用版

```text
你现在在维护一个 FastAPI 后端模板工程：Couple Diary Backend。

请先理解这些核心约束再开始改代码：
1. 这是“项目级后端工程”，不是单一 demo 项目。
2. `demo_api.py / demo_service.py` 只用于示例规范，对外路由归属在 `/api/v1/demo/...`。
3. `diary_api.py / diary_service.py / diary_entry.py` 是正式业务骨架样板，真实业务优先沿这条线扩展。
4. 所有 `/api/v1/...` 接口必须在路由层显式返回统一结构，优先使用 `build_api_response_from_request()`。
5. 不要依赖中间件去给普通业务响应“自动补齐” `platform / api / v`。
6. 新增正式业务模块时，按“路由层 + 服务层 + schema + 必要模型”独立落目录，不要长期堆进 demo 模块。
7. 新代码优先使用 `settings.application / server / cors / request_logging / database` 这套配置分组视图。
8. 环境切换、建库、迁移、启动，优先复用 `app/scripts/set_env.py`、`app/scripts/manage_db.py`、`run.sh`，不要自己发明新入口。
9. 提交前至少运行 `poetry run ruff check app tests` 和 `poetry run pytest`。
10. 修改前优先阅读 README 的 `开发与协作约定`、`环境配置`、`数据库操作`、`项目结构`。

如果你要新增业务模块，请优先参考：
- `app/api/endpoints/diary_api.py`
- `app/services/diary_service.py`
- `app/core/api_response.py`
- `app/core/config.py`
```

## 标准协作版

```text
你现在在维护 com-agent-runtime 这个 FastAPI 后端模板工程。

在开始任何改动前，请先遵守以下工程约束：

一、项目定位
- 项目名是 `Couple Diary Backend`，这是整个后端工程名，不是某一个子模块名。
- 当前仓库里同时存在“项目级基础设施”和“示例规范模块”，不要混淆。
- `demo_*` 代码只用于保留示例规范。
- `diary_*` 代码是当前正式业务骨架样板。

二、模块边界
- `app/api/endpoints/demo_api.py`、`app/services/demo_service.py`：示例规范模块
- `app/api/endpoints/diary_api.py`、`app/services/diary_service.py`、`app/schemas/diary.py`：正式业务骨架
- `app/models/diary_entry.py`：当前模板默认的正式业务持久化模型样板
- 不要把真实长期业务持续追加到 `demo_*` 里
- 如果新增正式业务域，继续按独立目录和独立 router 注册方式扩展

三、接口返回规范
- 所有 `/api/v1/...` 接口都必须返回统一结构：
  - `platform`
  - `api`
  - `data`
  - `ret`
  - `v`
- 成功响应必须在路由层显式构造
- 推荐统一使用 `app/core/api_response.py` 中的 `build_api_response_from_request()`
- 不要让服务层直接拼整套 HTTP 响应格式
- 不要依赖中间件为普通成功响应做自动包装

四、错误处理规范
- 优先抛出 `HTTPException` 或复用现有异常处理器
- 当前项目已经有统一异常格式化逻辑
- 调试错误返回格式时，可参考 `/api/v1/demo/error-demo`

五、配置与环境规范
- 配置入口在 `app/core/config.py`
- 新代码优先使用配置分组视图：
  - `settings.application`
  - `settings.server`
  - `settings.cors`
  - `settings.request_logging`
  - `settings.database`
- 环境文件按 `development / test / production` 分开管理
- 本地真实密码优先放在 `.env.development.local`、`.env.test.local`、`.env.local`

六、数据库与脚本规范
- 环境切换命令入口：`app/scripts/set_env.py`
- 数据库管理入口：`app/scripts/manage_db.py`
- 推荐启动入口：`./run.sh <env>`
- 首次初始化某个环境时，优先使用：
  - `poetry run python -m app.scripts.set_env development bootstrap`
- 如果是数据库结构演进，优先走 Alembic，而不是只用 `create_all()`

七、日志与排障规范
- 请求日志与 `request_id` 入口：`app/middleware/request_logging.py`
- 统一异常格式：`app/middleware/exception_handlers.py`
- 基础健康检查：`/healthz`
- 依赖就绪检查：`/readyz`

八、开发前推荐先读
- `README.md` 的以下章节：
  - `开发与协作约定`
  - `开发工具链`
  - `环境配置`
  - `数据库操作`
  - `项目结构`
- 关键文件：
  - `app/main.py`
  - `app/api/api.py`
  - `app/core/api_response.py`
  - `app/core/config.py`
  - `app/api/endpoints/demo_api.py`
  - `app/api/endpoints/diary_api.py`

九、开发行为约束
- 改代码前先判断当前需求属于“示例规范”还是“正式业务模块”
- 新增正式业务优先参考 `diary_*`，不要默认参考 `demo_*`
- 新增接口时，优先补测试
- 变更完成后至少执行：
  - `poetry run ruff check app tests`
  - `poetry run pytest`

十、如果要新增正式业务模块
- 推荐步骤：
  1. 新增 `app/api/endpoints/<module>_api.py`
  2. 新增 `app/services/<module>_service.py`
  3. 需要时新增 `app/schemas/<module>.py`
  4. 需要持久化时新增模型
  5. 在 `app/api/api.py` 里注册路由
  6. 路由层继续显式返回 `build_api_response_from_request()`

如果不确定应该参考哪部分代码，请优先参考正式业务骨架 `diary_*`，而不是示例模块 `demo_*`。
```
