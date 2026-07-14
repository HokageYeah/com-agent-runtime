# Couple Diary Backend

这是 `couple-diary` 项目的后端服务工程。

当前仓库里既包含“项目级”的后端基础设施，也保留了一组新的示例接口用于沉淀工程规范。因此后续做重构或让 LLM 参与开发时，需要特别注意：

- `Couple Diary Backend` / `情侣日记项目后端`：表示整个后端工程
- `demo_api`：表示当前保留下来的“示例规范接口模块”，对外统一挂载到 `/api/v1/demo/...`
- `diary_api`：表示当前补好的“正式业务模块骨架”，对外统一挂载到 `/api/v1/diary/...`
- 不要把“项目级基础设施”和“示例接口代码”混为一谈

## 阅读导航

如果你是第一次接手这个项目，推荐按下面顺序阅读：

1. `快速开始`：先把项目跑起来
2. `开发与协作约定`：理解接口返回规范、命名边界、LLM 开发约束
3. `开发工具链`：知道提交前要跑哪些检查
4. `环境配置`、`数据库操作`、`运行应用`：掌握日常开发流程
5. `日志系统`、`项目结构`、`集成框架`：补齐工程细节

如果你是让 LLM 接入开发，至少先让它理解：

- `开发与协作约定`
- `开发工具链`
- `环境配置`
- `数据库操作`
- `demo_api.py` 是示例规范，当前对外路由归属在 `/api/v1/demo/...`
- `diary_api.py` 是正式业务模块骨架，后续真实业务优先参考它扩展

## 模板工程使用手册目录

如果你后面要把这个项目当成“个人项目模板”“团队协作模板”或“LLM 接入模板”使用，推荐按下面方式查阅。

### 按角色阅读

- 个人开发者：先看 `快速开始`、`环境配置`、`数据库操作`、`运行应用`
- 新接手的开发者：先看 `阅读导航`、`开发与协作约定`、`项目结构`
- 让 LLM 接入开发：先看 `开发与协作约定`、`开发工具链`、`环境配置`、`项目结构`
- 需要排查线上 / 本地问题：先看 `运行应用`、`日志系统`、`healthz / readyz` 相关说明

### 按场景阅读

- 想先把项目跑起来：看 `快速开始`
- 想切换开发 / 测试 / 生产环境：看 `环境配置`
- 想初始化数据库或跑迁移：看 `数据库操作`
- 想新增正式业务模块：看 `如何基于模板新增正式业务模块`
- 想确认接口返回规范：看 `统一响应格式` 与 `接口开发约束`
- 想知道哪些代码是示例、哪些是正式业务骨架：看 `示例模块与历史模块`
- 想知道提交前要跑什么：看 `开发工具链`
- 想知道当前模板目录该怎么看：看 `项目结构`

### 常用入口文件

- `app/main.py`：FastAPI 应用入口
- `app/api/api.py`：全量路由注册入口
- `app/api/endpoints/demo_api.py`：示例规范接口入口
- `app/api/endpoints/diary_api.py`：正式业务骨架接口入口
- `app/core/api_response.py`：统一响应结构构造入口
- `app/core/config.py`：环境识别、配置加载、配置分组入口
- `app/middleware/request_logging.py`：请求日志与 `request_id` 入口
- `app/middleware/exception_handlers.py`：统一错误返回入口
- `app/scripts/set_env.py`：环境切换与命令转发入口
- `app/scripts/manage_db.py`：数据库迁移与重置入口

### 最常用命令速查

```bash
# 安装依赖
poetry install

# 首次初始化开发环境数据库
poetry run python -m app.scripts.set_env development bootstrap

# 启动开发环境
./run.sh development

# 启动测试环境
./run.sh test

# 查看当前环境会加载哪套配置
poetry run python -m app.scripts.set_env development

# 运行测试
poetry run pytest

# 运行静态检查
poetry run ruff check app tests
```

### 当前模板边界

- `demo_*` 相关代码：保留作示例规范，不承载长期正式业务
- `diary_*` 相关代码：作为正式业务模块骨架样板，后续真实功能优先沿这条线扩展
- `diary_entries`：当前模板默认的正式业务持久化模型样板
- `requirements.txt`：仅保留作历史参考，依赖管理以 `pyproject.toml` 为准

## LLM 接入提示词模板

下面这两份模板是给后续 ChatGPT / Codex / Claude 这类 LLM 接手本项目时使用的。

### 最短实用版

适合你临时给模型下发任务时直接粘贴，目标是先保证它不要跑偏。

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

### 标准协作版

适合长期复用，信息更完整，能明显降低 LLM 把示例模块和正式业务模块混写的概率。

```text
你现在在维护 backend/couple-diary-b 这个 FastAPI 后端模板工程。

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

如果你想直接复制完整文本版本，也可以看仓库里的 `LLM_PROMPTS.md`。

## 快速开始

如果你是第一次把项目跑起来，推荐直接按下面顺序执行：

```bash
# 1. 安装依赖
poetry install

# 2. 首次初始化开发环境数据库
poetry run python -m app.scripts.set_env development bootstrap

# 3. 启动开发环境服务
./run.sh development
```

如果你要切到测试环境：

```bash
poetry run python -m app.scripts.set_env test bootstrap
./run.sh test
```

如果你只想确认当前命令会使用哪套配置：

```bash
poetry run python -m app.scripts.set_env development
```

如果启动时报错里出现 `your_mysql_user` 或 `your_mysql_password`，说明你还没有配置本地覆盖文件，请先补 `.env.development.local`、`.env.test.local` 或 `.env.local`。

如果你想快速确认服务进程是否启动成功，可以访问：

```bash
curl http://127.0.0.1:8002/healthz
```

如果你想进一步确认“服务依赖是否已经就绪”，可以再访问：

```bash
curl http://127.0.0.1:8002/readyz
```

## 开发与协作约定

这一章是给人工开发者和 LLM 都看的核心约束，建议在改代码前先读完。

### 统一响应格式

当前项目约定所有 `/api/v1/...` 接口都返回统一响应结构：

```json
{
  "platform": "DEMO",
  "api": "api/v1/demo/hot-topics",
  "data": {},
  "ret": ["SUCCESS::请求成功"],
  "v": 1
}
```

- `platform`：根据接口路径自动识别，例如 `/api/v1/demo/...` 会自动识别为 `DEMO`
- `api`：接口完整路径，方便前端、日志系统和排错统一定位
- `data`：业务数据主体
- `ret`：结果描述数组，成功以 `SUCCESS::` 开头，失败以 `ERROR::` 开头
- `v`：接口响应版本号，当前统一由后端配置生成

请求参数校验失败、业务异常、响应模型异常，也会尽量返回同一套结构，方便前端统一处理。

### 命名约束

为了避免后续 LLM 或新同事在维护时误判代码性质，项目约定如下：

- `PROJECT_NAME`、`PROJECT_DESCRIPTION`、README 标题、OpenAPI 标题：统一使用整个项目级命名，例如 `Couple Diary Backend`
- `demo_api`：保留为“示例规范模块”，并且对外接口统一归属到 `demo`
- 如果未来新增新的业务域，例如 `diary`、`auth`、`feed`，继续按“项目名通用、模块名准确”的原则扩展

换句话说：

- 可以改项目标题、文档标题、服务描述
- 新功能优先参考 `demo_api.py` 的组织方式
- 如果是示例、教学、联调用接口，优先归到 `/api/v1/demo/...`

### 接口开发约束

- 成功响应必须在“路由层”显式返回标准结构
- 推荐统一使用 `app/core/api_response.py` 中的 `build_api_response_from_request()`
- 不要再用中间件对普通业务响应做“事后补齐”或“自动包装”
- 如果新增接口，优先让路由函数直接 `return build_api_response_from_request(...)`
- 如果是错误响应，优先抛出 `HTTPException` 或交给现有异常处理器统一格式化

推荐写法示例：

```python
from fastapi import APIRouter, Request

from app.core.api_response import build_api_response_from_request
from app.schemas.common_data import ApiResponseData

router = APIRouter()


@router.get("/demo", response_model=ApiResponseData)
async def demo(request: Request):
    result = {"hello": "world"}
    return build_api_response_from_request(
        request,
        data=result,
        ret=["SUCCESS::获取示例数据成功"],
    )
```

不推荐的写法：

- 路由直接返回裸 `dict`，指望别的中间件自动补 `platform/api/v`
- 在服务层直接拼整套 HTTP 响应格式
- 在多个接口里复制粘贴一套手写响应结构，导致字段慢慢不一致

### 错误返回自检接口

如果你想快速验证错误返回格式，可以访问：

```bash
GET /api/v1/demo/error-demo
```

它会主动抛出一个 `418` 错误，并返回统一错误结构，适合前端联调和 LLM 开发时自检。

### 示例模块与历史模块

当前仓库里最值得参考的示例代码是：

- `app/api/endpoints/demo_api.py`
- `app/services/demo_service.py`
- `app/api/endpoints/diary_api.py`
- `app/services/diary_service.py`

- `demo_*` 这组文件主要用于演示推荐的接口结构、日志写法、缓存装饰器使用方式和统一错误处理方式。
- `diary_*` 这组文件则是当前模板工程里的“正式业务域最小骨架”，用于告诉后续开发者和 LLM：真实业务模块应该怎样独立落目录、独立注册路由、独立组织 service 与 schema。
- `demo_*` 对外统一归在 `/api/v1/demo/...`，避免和正式业务域混淆。
- `diary_*` 对外统一归在 `/api/v1/diary/...`，作为正式业务模块样板。

### 如何基于模板新增正式业务模块

如果你后面要从这个模板继续开发正式业务模块，推荐按下面顺序做，而不是直接在 `demo_api.py` 里不断追加真实业务逻辑。

推荐步骤：

1. 先确定业务域名称
   例如：`diary`、`auth`、`feed`
2. 新增路由文件
   例如：`app/api/endpoints/diary_api.py`
3. 新增服务层文件
   例如：`app/services/diary_service.py`
4. 如果需要请求/响应模型，再新增 schema 文件
   例如：`app/schemas/diary.py`
5. 在 `app/api/api.py` 里统一注册新路由
   例如挂到 `/api/v1/diary/...`
6. 路由层继续显式返回 `build_api_response_from_request(...)`
7. 如果这个业务域需要独立平台标识，再去 `app/core/api_response.py` 的 `detect_platform_from_path()` 里补平台识别规则

推荐目录落点示例：

```text
app/
├── api/
│   └── endpoints/
│       ├── demo_api.py
│       └── diary_api.py
├── services/
│   ├── demo_service.py
│   └── diary_service.py
└── schemas/
    ├── common_data.py
    └── diary.py
```

推荐开发原则：

- `demo_api.py / demo_service.py` 继续保留为示例规范，不要把正式业务长期混写进去
- 正式业务模块优先按“一个业务域，一套路由层 + 服务层 + schema”的方式扩展
- 统一响应结构继续由路由层调用 `build_api_response_from_request()` 构造
- 错误处理优先继续复用现有异常处理器，不要每个业务模块自己拼错误响应

如果你是让 LLM 来新增模块，建议直接给它下面这类指令：

```text
请参考 demo_api.py / demo_service.py 的结构，
新增一个 diary 业务模块，
路由挂载到 /api/v1/diary，
并继续使用 build_api_response_from_request() 返回统一结构。
```

## 开发工具链

为了方便个人开发、后续团队协作，以及 LLM 按统一规范接手，本项目推荐使用下面这套开发工具链：

- `pytest`：当前基础回归测试的主要执行入口
- `pytest-asyncio`：后续扩展异步测试时可直接复用
- `ruff`：统一静态检查与格式化
- `pre-commit`：在提交前自动执行基础检查
- `GitHub Actions`：在远端自动执行基础 CI 校验

如果你刚拉下最新代码，推荐执行：

```bash
poetry install
poetry run pre-commit install
```

如果你是在受限环境、沙箱环境，或者不希望污染全局缓存目录，也可以这样运行：

```bash
PRE_COMMIT_HOME=.pre-commit-cache poetry run pre-commit run --all-files
```

第一次执行 `pre-commit` 时需要联网下载 hook 依赖；如果当前机器不能访问 GitHub，这一步会失败，这属于环境限制，不是项目代码本身报错。

常用命令：

```bash
# 运行全部测试
poetry run pytest

# 只跑单个测试文件
poetry run pytest tests/test_api_responses.py

# 执行静态检查
poetry run ruff check .

# 自动格式化
poetry run ruff format .

# 手动执行 pre-commit
poetry run pre-commit run --all-files
```

### 本地检查 vs 远端 CI

为了让这个项目既适合个人高效开发，也适合后续团队协作和 LLM 接入，可以把质量校验理解成两层：

#### 本地检查

本地检查主要解决“你在提交前，自己先快速发现问题”：

- `pre-commit`：适合拦截格式、尾部空格、基础 lint 这类低成本问题
- `ruff check`：适合做静态检查
- `pytest`：适合在本机先确认核心链路没有被改坏

本地检查的特点是：

- 反馈快
- 适合边改边跑
- 适合个人开发阶段快速自检

#### 远端 CI

远端 CI 主要解决“代码推上去之后，仓库还能不能稳定通过统一校验”。

- `.github/workflows/backend-couple-diary-b-ci.yml` 是当前后端模板工程的 GitHub Actions 持续集成配置文件
- `backend/couple-diary-b/.github/workflows/backend-couple-diary-b-ci.yml` 是给“后端工程单独上传到 GitHub 作为模板仓库”时使用的同等 CI 模板
- 当 `backend/couple-diary-b/` 下的代码，或者这个 workflow 文件本身发生 `push` / `pull_request` 变更时，它会自动触发
- 它会在 GitHub 提供的 Linux 环境里执行统一检查，避免不同人本地环境差异导致的误判

它当前主要负责四件事：

- 安装 Python `3.13`
- 安装 Poetry 和项目依赖
- 执行 `poetry run ruff check app tests`
- 执行 `poetry run pytest`

它的核心价值是：

- 避免“我本地能跑，推上去就坏了”
- 让团队成员和 LLM 改代码后，都有一套统一的远端兜底校验
- 让模板工程具备最基础的持续集成能力

两份 workflow 的分工如下：

- 仓库根目录 `.github/workflows/backend-couple-diary-b-ci.yml`
  用于当前这个 monorepo，只在 `backend/couple-diary-b/**` 发生变更时触发
- 子工程目录 `backend/couple-diary-b/.github/workflows/backend-couple-diary-b-ci.yml`
  用于你后续把这个后端工程单独上传到 GitHub 后直接复用，不再依赖 monorepo 的路径前缀或工作目录

#### 推荐协作方式

推荐把这两层配合起来使用：

1. 本地改代码时，优先跑 `pre-commit`、`ruff`、`pytest`
2. 提交代码后，让 `backend-couple-diary-b-ci.yml` 再做一次远端自动校验
3. 如果远端 CI 失败，以 CI 结果为准继续修正

### 当前测试覆盖

当前仓库已经补了几组基础回归测试，主要覆盖这些高价值链路：

- `tests/test_config.py`：环境别名归一化、`.env` 文件映射、本地覆盖文件顺序、占位账号密码识别、配置分组视图稳定性
- `tests/test_sqlalchemy_db.py`：数据库依赖生成器、数据库连接初始化时动态读取最新配置
- `tests/test_alembic_metadata.py`：Alembic 是否能正确发现模型元数据
- `tests/test_api_responses.py`：统一成功响应、请求参数校验失败、业务异常、响应模型校验失败
- `tests/test_logging_uru.py`：日志级别、`diagnose` 开关、日志文件路径规则
- `tests/test_set_env.py`：环境脚本命令分发、`bootstrap` 迁移失败回退行为
- `tests/test_main.py`：主应用 `healthz / readyz` 基础探针接口

推荐在提交前至少执行：

```bash
poetry run ruff check app tests
poetry run pytest
```

### 仓库清洁约束

为了降低协作噪音，下面这些文件不应该进入版本库：

- `__pycache__/`
- `*.pyc`
- `.DS_Store`
- 本地日志文件
- 本地 `.env.local` / `*.env.local`
- 本地虚拟环境目录，例如 `venv/`、`.venv/`
- 工具缓存目录，例如 `.ruff_cache/`、`.pytest_cache/`、`.pre-commit-cache/`

如果你发现这些文件重新出现在 `git status` 里，优先判断是不是误提交的缓存产物，而不是业务代码变更。这条规则对人工开发和 LLM 开发都适用。

## 环境配置

### 环境文件规则

本项目支持多环境配置，包括：

- `.env.development`：开发环境配置
- `.env.test`：测试环境配置
- `.env.production`：生产环境配置

应用启动时会读取 `ENVIRONMENT` 环境变量，并按下面的规则加载对应文件：

- `development` -> `.env.development`
- `test` -> `.env.test`
- `production` -> `.env.production`

脚本只负责传递 `ENVIRONMENT`，不会再改写 `.env` 文件。

同时还支持两层本地覆盖文件，方便把真实密码留在本机，不提交到仓库：

- `.env.development.local` / `.env.test.local` / `.env.production.local`
- `.env.local`

推荐做法：

- 把仓库里的 `.env.development / .env.test / .env.production` 当作团队共享模板
- 把你自己的真实账号密码写进本地 `.local` 文件
- `.local` 文件已经加入忽略规则，不会进入 git
- 跨域白名单通过 `BACKEND_CORS_ORIGINS` 配置，不再写死在代码里
- 请求链路日志也支持基础配置，例如 `REQUEST_ID_HEADER` 和 `SLOW_REQUEST_THRESHOLD_MS`

编辑相应的环境配置文件，设置数据库连接信息和其他参数：

```env
# 数据库配置
DB_DRIVER=mysql+mysqlconnector
DB_USER=root
DB_PASSWORD=password
DB_HOST=localhost
DB_PORT=3306
DB_NAME=couple_diary_dev
DB_CHARSET=utf8mb4

# API配置
API_PREFIX=/api/v1
DEBUG=True
ENVIRONMENT=development

# CORS 配置（支持逗号分隔，或 JSON 数组字符串）
BACKEND_CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000

# 请求日志配置
REQUEST_ID_HEADER=X-Request-ID
SLOW_REQUEST_THRESHOLD_MS=800
```

### 配置分组约定

为了兼顾“老代码稳定”和“新代码更容易理解”，当前项目的配置读取采用两层设计：

- 扁平字段：例如 `PROJECT_NAME`、`DB_HOST`、`REQUEST_ID_HEADER`
- 分组视图：例如 `settings.application`、`settings.server`、`settings.database`

现在的约定是：

- 老代码可以继续使用原来的扁平字段，不强制一次性重构
- 新代码优先使用分组视图，表达“当前逻辑依赖的是哪一类配置”

当前分组含义如下：

- `settings.application`：应用身份信息，例如项目名、描述、版本、接口前缀、响应版本、环境
- `settings.server`：服务监听信息，例如 `host`、`port`、`reload`
- `settings.cors`：跨域配置，例如 `allow_origins`
- `settings.logging`：日志系统基础配置，例如 `logging_level`
- `settings.request_logging`：请求链路日志配置，例如 `request_id_header`、`slow_request_threshold_ms`
- `settings.database`：数据库运行时配置，例如驱动、主机、库名、连接池参数

推荐示例：

```python
from app.core.config import settings

application_config = settings.application
database_config = settings.database

print(application_config.project_name)
print(database_config.database)
```

不推荐在新增代码里继续到处散落读取一长串 `settings.DB_*`、`settings.PROJECT_*`、`settings.REQUEST_*`，这样后面很难快速看出某段逻辑到底依赖了哪类配置。

如果你要在本机放真实开发库密码，推荐新建：

```bash
backend/couple-diary-b/.env.development.local
```

示例：

```env
DB_USER=root
DB_PASSWORD=你的真实密码
MYSQL_ROOT_PASSWORD=你的真实密码
MYSQL_USER=你的真实用户名
MYSQL_PASSWORD=你的真实密码
```

如果你没有配置这些本地覆盖文件，应用启动时会直接提示“当前数据库账号或密码仍然是模板占位值”。

## 数据库操作

这里的数据库脚本分成三类：

- `bootstrap`：首次初始化环境时使用，最省心
- `upgrade/downgrade/migrate`：后续日常迁移维护时使用
- `init_database` / `create_all()`：仅作为迁移异常时的兜底方案

### 创建数据库

推荐通过环境脚本执行：

```bash
poetry run python -m app.scripts.set_env development create_db
```

或者直接执行底层脚本：

```bash
poetry run python -m app.scripts.create_database
```

如果你是首次初始化某个环境，推荐直接执行一键准备命令：

```bash
poetry run python -m app.scripts.set_env development bootstrap
```

这个命令会按当前环境依次执行：

- 创建数据库
- 执行 Alembic 迁移
- 如果迁移不可用，则回退到 `create_all()` 初始化表结构

如果第一步建库本身就失败，`bootstrap` 会直接中断，不会再继续执行后续迁移。这样日志里的首个报错，就是最接近真实根因的报错。

### 常用数据库命令

```bash
# 开发环境首次初始化
poetry run python -m app.scripts.set_env development bootstrap

# 测试环境首次初始化
poetry run python -m app.scripts.set_env test bootstrap

# 生产环境首次初始化
poetry run python -m app.scripts.set_env production bootstrap

# 查看当前迁移版本
poetry run python -m app.scripts.manage_db current

# 查看迁移历史
poetry run python -m app.scripts.manage_db history

# 重置当前环境数据库
poetry run python -m app.scripts.manage_db reset
```

### 初始化数据库表

方式一：使用 SQLAlchemy 直接创建表

```bash
poetry run python -m app.scripts.init_database
```

方式二：使用 Alembic 进行数据库迁移（推荐）

```bash
# 创建迁移脚本
poetry run alembic revision --autogenerate -m "创建初始表结构"

# 应用迁移
poetry run alembic upgrade head
```

当前仓库已经内置了首个正式迁移版本。首次初始化环境时，优先建议使用 `bootstrap` 或 `alembic upgrade head`，把 Alembic 作为正式的表结构演进入口。

也可以通过 `set_env.py` 管理迁移流程：

```bash
# 创建迁移脚本
poetry run python -m app.scripts.set_env development migrate revision --autogenerate -m "pro_table"

# 应用迁移
poetry run python -m app.scripts.set_env development upgrade

# 回滚迁移
poetry run python -m app.scripts.set_env development downgrade
```

### 数据库字段更新

当模型定义发生变化时，例如添加、修改或删除字段，使用 Alembic 进行数据库迁移：

```bash
# 生成迁移脚本
poetry run alembic revision --autogenerate -m "更新字段描述"

# 应用迁移
poetry run alembic upgrade head

# 回滚迁移
poetry run alembic downgrade -1
```

或者继续使用环境脚本：

```bash
# 创建迁移脚本
poetry run python -m app.scripts.set_env development migrate revision --autogenerate -m "pro_table"

# 应用迁移
poetry run python -m app.scripts.set_env development upgrade

# 回滚迁移
poetry run python -m app.scripts.set_env development downgrade
```

## 运行应用

有三种常用方式运行应用：

### 方式一：使用 run.sh 脚本（推荐）

```bash
./run.sh development
```

该脚本会把 `ENVIRONMENT` 传给应用，再通过 Poetry 启动服务。

如果当前环境数据库还没有创建，应用会在启动阶段直接报错。首次运行请先执行：

```bash
poetry run python -m app.scripts.set_env development bootstrap
```

脚本内容如下：

```bash
#!/bin/bash
set -e

APP_ENV="${1:-development}"
echo "使用环境: ${APP_ENV}"
echo "如果是首次运行，请先执行: poetry run python -m app.scripts.set_env ${APP_ENV} bootstrap"
echo "如果看到 your_mysql_user / your_mysql_password 相关报错，请检查 .env.${APP_ENV}.local 或 .env.local"
ENVIRONMENT="$APP_ENV" poetry run python run_app.py
```

### 方式二：直接运行 Python 脚本

```bash
ENVIRONMENT=development poetry run python run_app.py
```

### 方式三：使用环境脚本启动

```bash
poetry run python -m app.scripts.set_env development runserver
```

`run_app.py` 会启动 Uvicorn 服务并加载 `app.main:app`。FastAPI 会在 `lifespan` 阶段初始化日志和数据库连接。

应用将在 http://localhost:8002 运行，API 文档可在 http://localhost:8002/docs 访问。

基础健康检查接口：

```http
GET /healthz
```

示例返回：

```json
{
  "status": "ok",
  "service": "Couple Diary Backend",
  "environment": "development",
  "version": "0.1.0"
}
```

依赖就绪检查接口：

```http
GET /readyz
```

示例返回：

```json
{
  "status": "ready",
  "service": "Couple Diary Backend",
  "environment": "development",
  "version": "0.1.0",
  "checks": [
    {
      "dependency": "database",
      "status": "ready"
    }
  ]
}
```

### 推荐使用顺序

首次启动某个环境：

1. `poetry install`
2. `poetry run python -m app.scripts.set_env <env> bootstrap`
3. `./run.sh <env>`

已经初始化过数据库后的日常启动：

1. `./run.sh development`
2. 或 `poetry run python -m app.scripts.set_env development runserver`

## 日志系统

当前日志系统已经做了两点工程化约束：

- DEBUG 环境下会开启更强的诊断信息，方便本地调试
- 非 DEBUG 环境默认关闭 `diagnose`，避免异常日志把过多局部变量和上下文暴露出来

当前请求链路日志还额外做了三件事：

- 每个请求都会分配或透传一个 `request_id`
- 响应头会回写 `X-Request-ID`（可通过 `REQUEST_ID_HEADER` 配置修改）
- 超过 `SLOW_REQUEST_THRESHOLD_MS` 的请求会单独打慢请求告警日志

日志文件默认写入：

- `logs/app_run/`：普通运行日志
- `logs/app_error/`：错误日志

你在本地排查接口时，推荐优先看这些信息是否能串起来：

- 请求入口日志：方法、路径、客户端 IP、`request_id`
- 请求完成日志：状态码、耗时、`request_id`
- 异常日志：异常类型、路径、`request_id`
- 接口响应头：`X-Request-ID`

如果后续要继续扩展日志能力，优先修改：

- `app/core/logging_uru.py`
- `app/middleware/request_logging.py`
- `app/middleware/exception_handlers.py`

不要在多个业务模块里各自重新初始化一套日志系统。

## API 接口

### 示例接口：获取热门话题

```http
GET /api/v1/demo/hot-topics?count=10
```

这个接口来自 `demo_api.py`，主要用于演示当前项目推荐的接口组织方式、统一响应结构、缓存装饰器使用方式和日志打印方式。
现在文件名、服务层命名和对外路由都已经统一归到 `demo` 语义下，方便区分“示例接口”和“正式业务接口”。

示例成功响应：

```json
{
  "platform": "DEMO",
  "api": "api/v1/demo/hot-topics",
  "data": {
    "topics": [
      {
        "id": 1,
        "title": "热门话题 1",
        "views": 1000
      }
    ],
    "timestamp": 0.0
  },
  "ret": [
    "SUCCESS::获取热门话题成功"
  ],
  "v": 1
}
```

### 正式业务模块骨架接口

```http
GET /api/v1/diary/ping
```

这个接口来自 `diary_api.py`，它的目标不是返回真实日记数据，而是提供一个最小正式业务域样板，告诉后续开发者：

- 正式业务模块应该独立于 `demo` 模块存在
- 正式业务模块也继续通过路由层显式返回统一响应
- 正式业务逻辑仍然进入 `service` 层，而不是直接堆在接口函数里

示例成功响应：

```json
{
  "platform": "DIARY",
  "api": "api/v1/diary/ping",
  "data": {
    "module": "diary",
    "status": "skeleton_ready",
    "message": "这是正式业务模块样板，可在此基础上继续扩展真实日记能力"
  },
  "ret": [
    "SUCCESS::获取 diary 模块骨架信息成功"
  ],
  "v": 1
}
```

### 基础健康检查接口

```http
GET /healthz
```

这个接口不走 `/api/v1/...` 统一业务响应结构，而是作为基础设施探针使用。它的职责是告诉你：

- 服务进程是否已经启动
- 当前运行的是哪个环境
- 当前服务版本是什么
- 它适合给容器探针、反向代理、部署平台做快速存活检查

### 依赖就绪检查接口

```http
GET /readyz
```

这个接口和 `/healthz` 的区别是：

- `/healthz` 只回答“进程是否活着”
- `/readyz` 进一步回答“数据库等关键依赖是否已经可用”

当前版本里，`/readyz` 会检查数据库依赖是否可正常执行一次轻量探测；如果依赖未就绪，会返回 `503`。

## 项目结构

```text
.
├── alembic/                         # 数据库迁移相关目录
│   ├── env.py                       # Alembic 环境配置与 metadata 入口
│   ├── script.py.mako               # 迁移脚本模板
│   └── versions/                    # 具体迁移版本文件
├── app/                             # 应用主代码目录
│   ├── api/                         # API 路由注册与 endpoints
│   ├── config/                      # 数据库等基础配置封装
│   ├── core/                        # 核心能力：统一响应、全局配置、日志
│   ├── db/                          # SQLAlchemy 连接、Base、metadata
│   ├── decorators/                  # 缓存等通用装饰器
│   ├── middleware/                  # 异常处理等中间层逻辑
│   ├── models/                      # 数据库模型定义
│   ├── schemas/                     # Pydantic 请求/响应模型
│   ├── scripts/                     # 环境切换、建库、迁移、初始化脚本
│   ├── services/                    # 业务逻辑服务层
│   └── main.py                      # FastAPI 应用入口
├── logs/                            # 运行日志与错误日志目录
├── tests/                           # 基础回归测试
├── .env.development                 # 开发环境基础模板
├── .env.test                        # 测试环境基础模板
├── .env.production                  # 生产环境基础模板
├── .env.example                     # 环境变量示例文件
├── .env.local                       # 通用本地覆盖文件（可选，本地使用，不提交）
├── .env.development.local           # 开发环境本地覆盖文件（可选，本地使用，不提交）
├── .env.test.local                  # 测试环境本地覆盖文件（可选，本地使用，不提交）
├── .env.production.local            # 生产环境本地覆盖文件（可选，本地使用，不提交）
├── .github/
│   └── workflows/
│       └── backend-couple-diary-b-ci.yml  # 后端工程独立发布到 GitHub 时使用的 CI 模板
├── .gitignore                       # 项目级忽略规则，防止缓存、日志、虚拟环境误提交
├── alembic.ini                      # Alembic 主配置文件
├── pyproject.toml                   # Poetry 项目配置与工具链配置
├── poetry.lock                      # Poetry 锁定依赖
├── poetry.toml                      # Poetry 本地行为配置
├── .pre-commit-config.yaml          # pre-commit 钩子配置
├── requirements.txt                 # 已废弃，保留作历史参考
├── run.sh                           # 推荐启动脚本
├── run_app.py                       # 应用启动脚本
└── project_structure.sh             # 输出当前模板推荐结构说明的辅助脚本
```

### 项目结构说明

- `app/core/` 是后端工程约束最集中的目录，后续如果要改统一响应、日志策略、环境配置，优先看这里。
- `app/core/api_response.py` 负责统一成功/失败响应的构造；改接口返回规范时，优先看这里。
- `app/core/config.py` 负责环境识别、`.env` 加载顺序、全局配置读取；改环境切换行为时，优先看这里。
- `app/core/config.py` 现在同时保留“扁平字段 + 分组视图”两套访问方式；新增代码优先使用 `settings.application / server / cors / logging / request_logging / database`。
- `app/core/logging_uru.py` 负责日志初始化、日志级别策略、日志文件路径规则；改日志行为时，优先看这里。
- `app/scripts/` 是数据库和环境切换的标准入口，个人开发、团队协作、LLM 自动化都尽量通过这里操作，而不是手敲零散命令。
- `app/scripts/set_env.py` 是最常用的开发脚本入口，负责把当前环境显式传给子命令。
- `app/scripts/manage_db.py` 负责数据库迁移、重置、历史查看等数据库管理命令。
- `app/api/endpoints/demo_api.py` 和 `app/services/demo_service.py` 是当前推荐参考的示例模块。
- `app/api/endpoints/diary_api.py`、`app/services/diary_service.py`、`app/schemas/diary.py` 是当前正式业务模块骨架样板。
- `.env.*` 文件是团队共享模板，`.env*.local` 是你本机私有覆盖，不应该提交到仓库。
- `requirements.txt` 现在只保留作历史参考，新的依赖管理、开发依赖、工具链配置都以 `pyproject.toml` 为准。
- `tests/` 目前以“关键链路回归测试”为主，后续继续扩展时，建议优先补接口、脚本、异常分支测试。

## 集成框架

本项目集成了以下框架和库：

1. **FastAPI**: 现代、快速的 Web 框架，用于构建 API。它基于标准的 Python 类型提示，提供自动文档生成和高性能。
2. **SQLAlchemy**: Python 的 SQL 工具包和 ORM 框架，提供 SQL 抽象层，使数据库操作更加简单和灵活。
3. **Alembic**: SQLAlchemy 的数据库迁移工具，用于管理数据库模式的变更。
4. **MySQL**: 用于数据存储的关系型数据库。
5. **python-dotenv**: 用于从 `.env` 文件加载环境变量，方便配置管理。
6. **pydantic-settings**: 基于 pydantic 的配置管理工具，提供类型安全的配置验证。
7. **pydantic**: 数据验证和设置管理库，使用 Python 类型注解。
8. **httpx**: 现代化的 HTTP 客户端，支持异步请求。
9. **uvicorn**: 现代的 ASGI 服务器，用于运行 FastAPI 应用。
10. **cachetools**: 缓存工具库，可减少重复计算或重复请求，提高接口响应速度。
11. **loguru**: 现代化日志库，支持多种日志级别和格式。
12. **click**: 命令行工具开发库，当前项目的数据库管理脚本和环境脚本会用它来组织命令入口。
13. **pytest**: 主流 Python 测试框架，适合统一执行项目测试。
14. **pytest-asyncio**: `pytest` 的异步测试扩展，后续编写异步接口测试和服务测试时会依赖它。
15. **ruff**: Python 静态检查与格式化工具，用于统一代码风格、导入顺序和基础质量检查。
16. **pre-commit**: Git 提交前检查工具，用于把 `ruff`、基础文件检查等动作串起来。
