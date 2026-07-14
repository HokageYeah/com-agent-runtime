# 情侣日记小程序 MVP 实施计划

> **给 agentic workers：** 必须使用子技能：推荐 `superpowers:subagent-driven-development`，也可以使用 `superpowers:executing-plans`，按任务逐项实施本计划。所有步骤使用复选框 `- [ ]` 追踪进度。

**目标：** 实现「情侣日记」微信小程序 MVP，包括微信登录、情侣绑定、共享日记时间线、打赌闭环、解绑后的回忆录归档，以及后端通知能力。

**架构：** 使用 monorepo 组织 FastAPI 后端与 uni-app Vue 3 TypeScript 前端。后端负责领域规则、持久化、状态流转、定时任务和通知记录；前端负责小程序页面、交互流程、状态管理和类型化 API 调用。

**技术栈：** FastAPI、SQLAlchemy 2、Alembic、MySQL、Redis、pytest、httpx、uni-app、Vue 3、TypeScript、Pinia、Vitest。

---

## 文件结构

创建以下顶层目录：

- `backend/`：FastAPI 应用、领域服务、数据库模型、迁移、测试和定时任务。
- `frontend/`：uni-app Vue 3 小程序、类型化 API、Pinia stores、页面和组件测试。
- `docs/superpowers/plans/`：Superpowers 实施计划。

后端文件职责：

- `backend/pyproject.toml`：Python 依赖和 pytest 配置。
- `backend/alembic.ini`：Alembic 配置。
- `backend/app/main.py`：FastAPI app 工厂和 router 注册。
- `backend/app/core/config.py`：环境变量配置。
- `backend/app/core/security.py`：密码哈希、token 签发和认证辅助函数。
- `backend/app/db/session.py`：异步 SQLAlchemy engine 与 session 依赖。
- `backend/app/db/base.py`：SQLAlchemy declarative base 和模型导入。
- `backend/app/models/*.py`：用户、情侣关系、日记、赌注、回忆录、拉黑、关键词、通知等模型。
- `backend/app/schemas/*.py`：Pydantic 请求和响应协议。
- `backend/app/services/*.py`：登录绑定、日记、赌局、回忆录、通知等业务规则。
- `backend/app/api/routes/*.py`：FastAPI 路由。
- `backend/app/jobs/bet_jobs.py`：赌注过期、自动作废和兑现提醒任务。
- `backend/app/jobs/request_expiry_jobs.py`：日记删除请求、解绑请求过期任务。
- `backend/tests/`：后端单元测试和 API 测试。

前端文件职责：

- `frontend/package.json`：Node 依赖和脚本。
- `frontend/src/main.ts`：uni-app 启动入口。
- `frontend/src/pages.json`：页面和 tabBar 注册。
- `frontend/src/api/*.ts`：类型化 API 客户端。
- `frontend/src/stores/*.ts`：认证、情侣、日记、赌局、回忆录 stores。
- `frontend/src/pages/login/index.vue`：微信登录和角色选择。
- `frontend/src/pages/binding/index.vue`：邀请码、二维码展示和绑定码输入。
- `frontend/src/pages/diary/index.vue`：日记时间线和日历模式切换。
- `frontend/src/pages/diary/edit.vue`：写日记，最多 9 张图片。
- `frontend/src/pages/diary/detail.vue`：日记详情和删除请求流程。
- `frontend/src/pages/bets/index.vue`：赌局时间线和战绩统计。
- `frontend/src/pages/bets/create.vue`：发起赌注和奖励提醒。
- `frontend/src/pages/bets/detail.vue`：接受、结算、协商、兑现和确认奖励。
- `frontend/src/pages/me/index.vue`：个人资料、情侣状态、回忆录入口、设置入口。
- `frontend/src/pages/memory/index.vue`：回忆录密码门禁和列表。
- `frontend/src/pages/memory/detail.vue`：归档日记和赌局时间线。
- `frontend/src/pages/unlink/index.vue`：和平解绑和拉黑流程。
- `frontend/src/components/*.vue`：通用时间线卡片、表单控件、空状态和弹窗。
- `frontend/src/styles/tokens.scss`：温暖甜蜜风格设计变量。
- `frontend/tests/`：Vitest store 测试和纯 UI 逻辑测试。

## 任务 1：后端项目脚手架

**文件：**
- 创建：`backend/pyproject.toml`
- 创建：`backend/app/main.py`
- 创建：`backend/app/core/config.py`
- 创建：`backend/app/db/session.py`
- 创建：`backend/app/db/base.py`
- 创建：`backend/tests/test_health.py`

- [ ] **步骤 1：创建后端包结构**

运行：

```bash
mkdir -p backend/app/{api/routes,core,db,jobs,models,schemas,services} backend/tests
touch backend/app/__init__.py backend/app/api/__init__.py backend/app/api/routes/__init__.py backend/app/core/__init__.py backend/app/db/__init__.py backend/app/jobs/__init__.py backend/app/models/__init__.py backend/app/schemas/__init__.py backend/app/services/__init__.py
```

预期：`backend/` 下出现对应目录和空包文件。

- [ ] **步骤 2：添加后端依赖和测试配置**

创建 `backend/pyproject.toml`：

```toml
[project]
name = "couple-diary-backend"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115.0",
  "uvicorn[standard]>=0.30.0",
  "sqlalchemy[asyncio]>=2.0.30",
  "aiomysql>=0.2.0",
  "alembic>=1.13.0",
  "pydantic-settings>=2.3.0",
  "python-jose[cryptography]>=3.3.0",
  "passlib[bcrypt]>=1.7.4",
  "redis>=5.0.0",
  "httpx>=0.27.0",
  "apscheduler>=3.10.4"
]

[project.optional-dependencies]
test = [
  "pytest>=8.2.0",
  "pytest-asyncio>=0.23.0",
  "aiosqlite>=0.20.0"
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **步骤 3：添加配置模块**

创建 `backend/app/core/config.py`：

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Couple Diary"
    environment: str = "local"
    database_url: str = "sqlite+aiosqlite:///./test.db"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "local-dev-secret-change-before-deploy"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60 * 24 * 30
    wechat_appid: str = "local-appid"
    wechat_secret: str = "local-secret"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **步骤 4：添加数据库 session**

创建 `backend/app/db/session.py`：

```python
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
```

创建 `backend/app/db/base.py`：

```python
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
```

- [ ] **步骤 5：添加健康检查接口**

创建 `backend/app/main.py`：

```python
from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Couple Diary API")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

- [ ] **步骤 6：编写并运行健康检查测试**

创建 `backend/tests/test_health.py`：

```python
from httpx import ASGITransport, AsyncClient

from app.main import app


async def test_health_returns_ok():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

运行：

```bash
cd backend && pytest tests/test_health.py -q
```

预期：`1 passed`。

- [ ] **步骤 7：提交**

```bash
git add backend
git commit -m "chore: scaffold backend api"
```

## 任务 2：核心数据模型和迁移

**文件：**
- 创建：`backend/alembic.ini`
- 创建：`backend/alembic/env.py`
- 创建：`backend/alembic/versions/20260611_0001_initial_schema.py`
- 创建：`backend/app/models/user.py`
- 创建：`backend/app/models/couple.py`
- 创建：`backend/app/models/diary.py`
- 创建：`backend/app/models/bet.py`
- 创建：`backend/app/models/memory.py`
- 创建：`backend/app/models/notification.py`
- 修改：`backend/app/db/base.py`
- 测试：`backend/tests/test_models.py`

- [ ] **步骤 1：先写模型测试**

创建 `backend/tests/test_models.py`：

```python
from app.models.bet import BetStatus
from app.models.couple import CoupleStatus
from app.models.diary import Diary
from app.models.memory import MemoryPassword


def test_bet_status_values_match_product_flow():
    assert [status.value for status in BetStatus] == [
        "pending",
        "rejected",
        "active",
        "settling",
        "expired",
        "redeeming",
        "completed",
    ]


def test_couple_status_values_cover_binding_and_archive():
    assert [status.value for status in CoupleStatus] == ["active", "unlinked", "blocked"]


def test_diary_supports_content_images_and_delete_state():
    columns = Diary.__table__.columns
    assert "content" in columns
    assert "image_urls" in columns
    assert "delete_requested_at" in columns


def test_memory_password_uses_hash_not_plaintext():
    columns = MemoryPassword.__table__.columns
    assert "password_hash" in columns
    assert "password" not in columns
```

运行：

```bash
cd backend && pytest tests/test_models.py -q
```

预期：因模型尚不存在而失败。

- [ ] **步骤 2：实现用户和情侣关系模型**

创建 `backend/app/models/user.py`：

```python
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    openid: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    nickname: Mapped[str] = mapped_column(String(80))
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    role_label: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

创建 `backend/app/models/couple.py`：

```python
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CoupleStatus(str, Enum):
    active = "active"
    unlinked = "unlinked"
    blocked = "blocked"


class Couple(Base):
    __tablename__ = "couple"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_a_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    user_b_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default=CoupleStatus.active.value)
    invite_code: Mapped[str | None] = mapped_column(String(12), unique=True, nullable=True)
    bound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    unlinked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UnlinkRequest(Base):
    __tablename__ = "unlink_request"

    id: Mapped[int] = mapped_column(primary_key=True)
    couple_id: Mapped[int] = mapped_column(ForeignKey("couple.id"), index=True)
    requester_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Blacklist(Base):
    __tablename__ = "blacklist"

    id: Mapped[int] = mapped_column(primary_key=True)
    blocker_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    blocked_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    cooldown_until: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

- [ ] **步骤 3：实现日记、赌注、回忆录和通知模型**

创建以下模型文件，并覆盖需求文档 5.1 中的所有表：

- `backend/app/models/diary.py`：`Diary`、`DiaryDeleteRequest`，包含 `content`、`image_urls`、`author_id`、`couple_id`、`diary_date`、删除请求状态和过期时间。
- `backend/app/models/bet.py`：`BetStatus`、`Bet`、`BetMessage`、`RewardBlacklist`，状态值必须为 `pending`、`rejected`、`active`、`settling`、`expired`、`redeeming`、`completed`。
- `backend/app/models/memory.py`：`Memory`、`MemoryPassword`，密码只保存 `password_hash`。
- `backend/app/models/notification.py`：`Notification`，包含类型、内容、目标类型、目标 ID、已读状态。

- [ ] **步骤 4：导入模型到 metadata**

修改 `backend/app/db/base.py`：

```python
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


from app.models import bet, couple, diary, memory, notification, user  # noqa: E402,F401
```

- [ ] **步骤 5：添加 Alembic 初始迁移**

创建 `backend/alembic.ini`、`backend/alembic/env.py`、`backend/alembic/versions/20260611_0001_initial_schema.py`。迁移必须创建需求文档 5.1 中列出的所有表。

运行：

```bash
cd backend && alembic upgrade head
```

预期：迁移成功，所有表创建完成。

- [ ] **步骤 6：运行模型测试并提交**

```bash
cd backend && pytest tests/test_models.py -q
git add backend
git commit -m "feat: add core data model"
```

预期：测试全部通过并完成提交。

## 任务 3：登录和情侣绑定 API

**文件：**
- 创建：`backend/app/schemas/auth.py`
- 创建：`backend/app/schemas/couple.py`
- 创建：`backend/app/services/auth_service.py`
- 创建：`backend/app/services/couple_service.py`
- 创建：`backend/app/api/routes/auth.py`
- 创建：`backend/app/api/routes/couples.py`
- 修改：`backend/app/main.py`
- 测试：`backend/tests/test_auth_and_binding.py`

- [ ] **步骤 1：编写登录、角色、邀请和绑定测试**

创建 `backend/tests/test_auth_and_binding.py`，覆盖：

- `POST /api/auth/wechat-login` 用 code 创建用户并返回 token。
- `POST /api/auth/role` 只能设置一次「他」或「她」。
- `POST /api/couples/invites` 为未绑定用户生成绑定码。
- `POST /api/couples/bind` 第二个用户输入绑定码后完成绑定。
- 已绑定用户不能再创建邀请。
- 用户不能绑定自己的邀请码。
- 拉黑冷却期内不能重新绑定。

运行：

```bash
cd backend && pytest tests/test_auth_and_binding.py -q
```

预期：接口尚不存在，测试失败。

- [ ] **步骤 2：实现认证服务**

创建 `backend/app/services/auth_service.py`：

```python
from datetime import datetime, timedelta, timezone

from jose import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.user import User


async def login_with_wechat_code(db: AsyncSession, code: str) -> User:
    openid = f"local_{code}"
    existing = await db.scalar(select(User).where(User.openid == openid))
    if existing:
        return existing
    user = User(openid=openid, nickname="微信用户", avatar_url=None)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def create_access_token(user_id: int) -> str:
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_minutes)
    return jwt.encode({"sub": str(user_id), "exp": expires}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
```

- [ ] **步骤 3：实现情侣绑定服务**

在 `backend/app/services/couple_service.py` 中实现：

- 已有 active 情侣关系的用户不能创建新邀请。
- 邀请码为 8 位大写字母数字。
- 自己不能绑定自己的邀请码。
- 任一方处于拉黑冷却期时绑定失败。
- 绑定成功后写入 `user_b_id`、`bound_at`，状态为 `active`。

- [ ] **步骤 4：注册路由并运行测试**

在 `backend/app/main.py` 注册 `/api/auth` 和 `/api/couples`。

```bash
cd backend && pytest tests/test_auth_and_binding.py -q
git add backend
git commit -m "feat: add auth and couple binding"
```

预期：测试全部通过并完成提交。

## 任务 4：日记时间线和删除审批

**文件：**
- 创建：`backend/app/schemas/diary.py`
- 创建：`backend/app/services/diary_service.py`
- 创建：`backend/app/api/routes/diaries.py`
- 修改：`backend/app/main.py`
- 测试：`backend/tests/test_diaries.py`

- [ ] **步骤 1：编写日记行为测试**

创建 `backend/tests/test_diaries.py`，覆盖：

- 内容超过 500 字返回 HTTP 422。
- 图片超过 9 张返回 HTTP 422。
- 时间线按 `diary_date` 分组，并在当天内按时间倒序。
- 任意一方可以发起删除请求。
- 发起人不能替另一方确认自己的删除请求。
- 另一方同意后，日记从时间线隐藏。
- 删除请求 7 天后由过期任务自动失效。

示例测试：

```python
async def test_partner_confirms_delete_request_hides_diary(couple_clients):
    her, him = couple_clients
    created = await her.post("/api/diaries", json={"content": "今天一起吃火锅", "image_urls": []})
    diary_id = created.json()["id"]

    request = await him.post(f"/api/diaries/{diary_id}/delete-request")
    confirm = await her.post(f"/api/diaries/{diary_id}/delete-confirm", json={"agree": True})
    timeline = await her.get("/api/diaries/timeline")

    assert request.status_code == 200
    assert confirm.status_code == 200
    assert all(item["id"] != diary_id for day in timeline.json()["days"] for item in day["items"])
```

- [ ] **步骤 2：实现日记服务**

在 `backend/app/services/diary_service.py` 中实现：

- `create_diary(db, current_user, content, image_urls, created_at)`：校验 active 情侣空间、500 字限制、9 图限制。
- `list_timeline(db, current_user, cursor_date, limit_days)`：返回按日期分组的时间线。
- `list_calendar(db, current_user, month)`：返回有日记日期和有图片日期。
- `request_delete(db, current_user, diary_id)`：创建 7 天后过期的删除请求。
- `confirm_delete(db, current_user, diary_id, agree)`：只有另一方同意时才隐藏日记。
- `revoke_delete_request(db, current_user, diary_id)`：发起人可在过期前撤回。

- [ ] **步骤 3：添加日记路由**

暴露：

- `POST /api/diaries`
- `GET /api/diaries/timeline`
- `GET /api/diaries/calendar`
- `GET /api/diaries/{diary_id}`
- `POST /api/diaries/{diary_id}/delete-request`
- `POST /api/diaries/{diary_id}/delete-confirm`
- `POST /api/diaries/{diary_id}/delete-revoke`

- [ ] **步骤 4：运行测试并提交**

```bash
cd backend && pytest tests/test_diaries.py -q
git add backend
git commit -m "feat: add shared diary timeline"
```

预期：测试全部通过并完成提交。

## 任务 5：赌注生命周期 API 和定时任务

**文件：**
- 创建：`backend/app/schemas/bet.py`
- 创建：`backend/app/services/bet_service.py`
- 创建：`backend/app/api/routes/bets.py`
- 创建：`backend/app/jobs/bet_jobs.py`
- 修改：`backend/app/main.py`
- 测试：`backend/tests/test_bets.py`
- 测试：`backend/tests/test_bet_jobs.py`

- [ ] **步骤 1：编写生命周期测试**

创建测试覆盖：

- 创建赌注后状态为 `pending`。
- 奖励包含 `钻戒` 时返回温暖提醒，但提交仍成功。
- 对方接受后状态为 `active`。
- 对方拒绝后状态为 `rejected`。
- active 赌注到截止时间后变为 `settling`。
- settling 后 3 天双方无操作则变为 `expired`。
- 一方提交结果，另一方确认后状态变为 `redeeming`。
- 双方意见不一致时进入协商态，并允许发送简短消息。
- 输方标记已兑现，赢方确认后状态为 `completed`。
- 待兑现赌注每 12 小时为输方生成一次提醒通知。

示例：

```python
async def test_sensitive_reward_returns_warm_warning(couple_clients, seeded_reward_keywords):
    her, _ = couple_clients
    response = await her.post("/api/bets", json={
        "title": "今天会下雨",
        "win_condition": "如果下雨她赢",
        "reward": "钻戒",
        "deadline_at": "2026-06-11T22:00:00"
    })
    body = response.json()
    assert response.status_code == 201
    assert body["reward_warning"]["title"] == "这个奖励也太重啦！"
```

- [ ] **步骤 2：实现赌注服务**

在 `backend/app/services/bet_service.py` 中实现：

- `create_bet`
- `respond_to_bet`
- `list_timeline`
- `submit_result`
- `confirm_result`
- `add_message`
- `mark_reward_delivered`
- `confirm_reward_received`
- `get_stats`

状态值必须与需求文档 5.2 一致。

- [ ] **步骤 3：实现奖励关键词提醒**

初始化 `reward_blacklist`：

```text
车
房
钻石
钻戒
别墅
轿车
豪宅
```

命中后返回：

```json
{
  "title": "这个奖励也太重啦！",
  "message": "打赌是生活里的小情趣，不需要用太贵重的东西来下注哦。试试一顿亲手做的饭、一次肩膀按摩、一张手写情书、一起看场日落。真正珍贵的从来不是价格，而是用心。"
}
```

- [ ] **步骤 4：实现定时任务**

创建 `backend/app/jobs/bet_jobs.py`：

- `move_due_active_bets_to_settling(now)`：将 `deadline_at <= now` 的 active 赌注改为 `settling`。
- `expire_idle_settling_bets(now)`：将 settling 且 3 天未操作的赌注改为 `expired`。
- `create_redemption_reminders(now)`：为 redeeming 赌注的输方每 12 小时创建一次通知。

- [ ] **步骤 5：添加赌注路由**

暴露：

- `POST /api/bets`
- `GET /api/bets/timeline`
- `GET /api/bets/stats`
- `GET /api/bets/{bet_id}`
- `POST /api/bets/{bet_id}/respond`
- `POST /api/bets/{bet_id}/result`
- `POST /api/bets/{bet_id}/result-confirm`
- `POST /api/bets/{bet_id}/messages`
- `POST /api/bets/{bet_id}/reward-delivered`
- `POST /api/bets/{bet_id}/reward-confirm`

- [ ] **步骤 6：运行测试并提交**

```bash
cd backend && pytest tests/test_bets.py tests/test_bet_jobs.py -q
git add backend
git commit -m "feat: add betting lifecycle"
```

预期：测试全部通过并完成提交。

## 任务 6：解绑、拉黑和回忆录归档

**文件：**
- 创建：`backend/app/schemas/memory.py`
- 创建：`backend/app/services/memory_service.py`
- 创建：`backend/app/api/routes/memories.py`
- 修改：`backend/app/services/couple_service.py`
- 修改：`backend/app/api/routes/couples.py`
- 创建：`backend/app/jobs/request_expiry_jobs.py`
- 测试：`backend/tests/test_unlink_and_memory.py`

- [ ] **步骤 1：编写解绑和回忆录测试**

覆盖：

- 和平解绑请求 7 天后过期。
- 对方同意解绑后，情侣关系变为 `unlinked`，并为双方创建回忆录。
- 对方拒绝解绑后，情侣关系保持 active。
- 拉黑必须输入确认文字 `确定拉黑`。
- 拉黑立即解绑、创建双向拉黑记录、创建回忆录，并在 7 天冷却期内阻止重新绑定。
- 第一次进入回忆录时要求设置密码。
- 密码只接受 4-6 位数字。
- 正确密码可解锁，错误密码不能解锁。

- [ ] **步骤 2：实现密码哈希**

在 `backend/app/core/security.py` 中实现：

```python
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(raw_password: str) -> str:
    return pwd_context.hash(raw_password)


def verify_password(raw_password: str, password_hash: str) -> bool:
    return pwd_context.verify(raw_password, password_hash)
```

- [ ] **步骤 3：实现回忆录归档**

实现 `create_memory_from_couple(db, couple)`，保存：

- 原 `couple_id`
- 双方用户 ID
- 绑定时间
- 解绑时间
- 日记数量
- 打赌数量
- 用于回看归档数据的不可变引用

- [ ] **步骤 4：添加回忆录和解绑路由**

回忆录路由：

- `POST /api/memories/password`
- `POST /api/memories/unlock`
- `GET /api/memories`
- `GET /api/memories/{memory_id}/timeline`
- `GET /api/memories/{memory_id}/calendar`

情侣关系路由：

- `POST /api/couples/unlink-request`
- `POST /api/couples/unlink-confirm`
- `POST /api/couples/block`

- [ ] **步骤 5：运行测试并提交**

```bash
cd backend && pytest tests/test_unlink_and_memory.py -q
git add backend
git commit -m "feat: add unlink and memory archive"
```

预期：测试全部通过并完成提交。

## 任务 7：通知中心

**文件：**
- 创建：`backend/app/schemas/notification.py`
- 创建：`backend/app/services/notification_service.py`
- 创建：`backend/app/api/routes/notifications.py`
- 修改：`backend/app/main.py`
- 测试：`backend/tests/test_notifications.py`

- [ ] **步骤 1：编写通知测试**

覆盖以下通知的创建和读取：

- 绑定成功。
- 解绑请求。
- 日记删除请求。
- 赌注邀请。
- 赌注状态变化。
- 兑现提醒。
- 单条标记已读。
- 全部标记已读。

- [ ] **步骤 2：实现通知服务**

实现：

- `create_notification(db, user_id, type, content, target_type, target_id)`
- `list_notifications(db, user_id, only_unread)`
- `mark_read(db, user_id, notification_id)`
- `mark_all_read(db, user_id)`

通知内容保存为可直接展示的短文案，并附带目标类型和目标 ID 供前端跳转。

- [ ] **步骤 3：添加通知路由并提交**

暴露：

- `GET /api/notifications`
- `POST /api/notifications/{notification_id}/read`
- `POST /api/notifications/read-all`

运行：

```bash
cd backend && pytest tests/test_notifications.py -q
git add backend
git commit -m "feat: add notification center"
```

预期：测试全部通过并完成提交。

## 任务 8：前端脚手架和应用外壳

**文件：**
- 创建：`frontend/package.json`
- 创建：`frontend/src/main.ts`
- 创建：`frontend/src/pages.json`
- 创建：`frontend/src/styles/tokens.scss`
- 创建：`frontend/src/api/http.ts`
- 创建：`frontend/src/stores/auth.ts`
- 创建：`frontend/src/pages/login/index.vue`
- 创建：`frontend/src/pages/binding/index.vue`
- 创建：`frontend/src/pages/diary/index.vue`
- 创建：`frontend/src/pages/bets/index.vue`
- 创建：`frontend/src/pages/me/index.vue`
- 测试：`frontend/tests/auth-store.test.ts`

- [ ] **步骤 1：添加前端依赖**

创建 `frontend/package.json`：

```json
{
  "name": "couple-diary-frontend",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev:h5": "uni -p h5",
    "build:mp-weixin": "uni build -p mp-weixin",
    "test": "vitest run"
  },
  "dependencies": {
    "@dcloudio/uni-app": "latest",
    "pinia": "latest",
    "vue": "^3.5.0"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "latest",
    "typescript": "^5.5.0",
    "vitest": "^2.0.0",
    "vue-tsc": "^2.0.0"
  }
}
```

- [ ] **步骤 2：注册页面和 tabBar**

创建 `frontend/src/pages.json`：

```json
{
  "pages": [
    { "path": "pages/login/index", "style": { "navigationBarTitleText": "登录" } },
    { "path": "pages/binding/index", "style": { "navigationBarTitleText": "情侣绑定" } },
    { "path": "pages/diary/index", "style": { "navigationBarTitleText": "日记" } },
    { "path": "pages/bets/index", "style": { "navigationBarTitleText": "赌局" } },
    { "path": "pages/me/index", "style": { "navigationBarTitleText": "我的" } }
  ],
  "tabBar": {
    "color": "#7A6F68",
    "selectedColor": "#E86F8E",
    "backgroundColor": "#FFF9F5",
    "list": [
      { "pagePath": "pages/diary/index", "text": "日记" },
      { "pagePath": "pages/bets/index", "text": "赌局" },
      { "pagePath": "pages/me/index", "text": "我的" }
    ]
  }
}
```

- [ ] **步骤 3：添加视觉变量**

创建 `frontend/src/styles/tokens.scss`：

```scss
$color-bg: #fff9f5;
$color-surface: #ffffff;
$color-primary: #e86f8e;
$color-accent: #5bb8a8;
$color-warning: #f4b24d;
$color-text: #332d2a;
$color-muted: #7a6f68;
$radius-card: 8px;
$space-page: 24rpx;
```

- [ ] **步骤 4：实现 HTTP 客户端和认证 store**

`frontend/src/api/http.ts` 负责保存并发送 `Authorization: Bearer <token>`。`frontend/src/stores/auth.ts` 暴露 `loginWithWechatCode`、`selectRole`、`restoreSession`。

- [ ] **步骤 5：实现初始页面**

页面要求：

- 登录页在微信环境调用 `wx.login`，H5 开发环境使用 `fake-code`。
- 绑定页支持生成邀请码和输入绑定码。
- 日记、赌局、我的三个 tab 页能展示已登录空状态，并调用对应 store loader。

- [ ] **步骤 6：运行测试并提交**

```bash
cd frontend && npm test
git add frontend
git commit -m "feat: scaffold miniapp shell"
```

预期：认证 store 测试通过并完成提交。

## 任务 9：前端日记体验

**文件：**
- 创建：`frontend/src/api/diaries.ts`
- 创建：`frontend/src/stores/diary.ts`
- 创建：`frontend/src/components/DiaryTimeline.vue`
- 创建：`frontend/src/components/DiaryComposer.vue`
- 创建：`frontend/src/components/CalendarStrip.vue`
- 创建：`frontend/src/pages/diary/edit.vue`
- 创建：`frontend/src/pages/diary/detail.vue`
- 修改：`frontend/src/pages/diary/index.vue`
- 修改：`frontend/src/pages.json`
- 测试：`frontend/tests/diary-store.test.ts`

- [ ] **步骤 1：编写日记 store 测试**

覆盖：

- 时间线按日期分组。
- 日历能区分有日记日期和有照片日期。
- 编辑器拒绝超过 500 字内容。
- 编辑器拒绝超过 9 张图片。
- 删除请求和确认后本地状态更新。

- [ ] **步骤 2：实现日记 API 客户端**

创建：

- `createDiary`
- `getDiaryTimeline`
- `getDiaryCalendar`
- `getDiaryDetail`
- `requestDiaryDelete`
- `confirmDiaryDelete`
- `revokeDiaryDelete`

- [ ] **步骤 3：实现日记页面**

日记 tab 必须支持：

- 默认展示今天时间线。
- 下拉或滚动加载更早日期。
- 日历模式切换。
- 右下角浮动 `+` 按钮写日记。
- 详情页展示删除请求状态。
- 图片选择最多 9 张，并提示单张 5 MB 限制。

- [ ] **步骤 4：运行测试并提交**

```bash
cd frontend && npm test -- diary-store
git add frontend
git commit -m "feat: add diary frontend"
```

预期：日记 store 测试通过并完成提交。

## 任务 10：前端赌局体验

**文件：**
- 创建：`frontend/src/api/bets.ts`
- 创建：`frontend/src/stores/bet.ts`
- 创建：`frontend/src/components/BetTimeline.vue`
- 创建：`frontend/src/components/BetCard.vue`
- 创建：`frontend/src/components/BetStatsBar.vue`
- 创建：`frontend/src/pages/bets/create.vue`
- 创建：`frontend/src/pages/bets/detail.vue`
- 修改：`frontend/src/pages/bets/index.vue`
- 修改：`frontend/src/pages.json`
- 测试：`frontend/tests/bet-store.test.ts`

- [ ] **步骤 1：编写赌局 store 测试**

覆盖：

- `pending`、`active`、`settling`、`redeeming`、`completed`、`rejected`、`expired` 映射为正确展示文案。
- 敏感奖励提醒出现，但不阻止提交。
- 时间线按日期分组。
- 待兑现赌注比已完成赌注更醒目。
- 战绩展示她赢次数、他赢次数、兑现率、最常见奖励。

- [ ] **步骤 2：实现赌局 API 客户端**

创建 Task 5 中所有赌注接口对应的前端函数。

- [ ] **步骤 3：实现发起赌注页面**

字段：

- `赌什么`
- `谁赢了会怎样`
- `奖励是什么`
- `截止时间`

命中奖励关键词时显示温暖提醒，但保留已创建的赌注结果。

- [ ] **步骤 4：实现赌局时间线和详情页**

按状态支持：

- `pending`：对方接受或拒绝。
- `active`：显示倒计时。
- `settling`：提交结果、确认结果、不同意、发送简短协商消息。
- `redeeming`：输方标记已兑现，赢方确认收到。
- `completed`、`rejected`、`expired`：只读详情。

- [ ] **步骤 5：运行测试并提交**

```bash
cd frontend && npm test -- bet-store
git add frontend
git commit -m "feat: add betting frontend"
```

预期：赌局 store 测试通过并完成提交。

## 任务 11：我的页、解绑、回忆录和通知前端

**文件：**
- 创建：`frontend/src/api/memories.ts`
- 创建：`frontend/src/api/notifications.ts`
- 创建：`frontend/src/stores/memory.ts`
- 创建：`frontend/src/stores/notification.ts`
- 创建：`frontend/src/pages/memory/index.vue`
- 创建：`frontend/src/pages/memory/detail.vue`
- 创建：`frontend/src/pages/unlink/index.vue`
- 创建：`frontend/src/pages/notifications/index.vue`
- 修改：`frontend/src/pages/me/index.vue`
- 修改：`frontend/src/pages.json`
- 测试：`frontend/tests/memory-store.test.ts`
- 测试：`frontend/tests/notification-store.test.ts`

- [ ] **步骤 1：编写回忆录和通知测试**

覆盖：

- 回忆录密码只接受 4-6 位数字。
- 解锁失败展示保护隐私的错误文案。
- 回忆录列表展示对方昵称、绑定日期、解绑日期、日记数量、赌局数量。
- 通知列表可筛选未读。
- 标记单条已读和全部已读后，本地未读数更新。

- [ ] **步骤 2：实现 API 和 stores**

创建回忆录、解绑、拉黑、通知接口对应的前端函数和 Pinia stores。

- [ ] **步骤 3：实现我的页**

展示：

- 用户昵称和角色标签。
- active 情侣昵称和绑定日期。
- 未绑定时展示绑定入口。
- 回忆录入口。
- 带未读角标的通知入口。
- 通知开关和密码管理等设置入口。

- [ ] **步骤 4：实现解绑和拉黑页面**

和平解绑：

- 第一次确认说明日记和赌局将进入回忆录。
- 发起解绑请求。
- 展示待确认状态和过期日期。

拉黑：

- 展示严肃提示。
- 要求输入 `确定拉黑`。
- 调用拉黑接口。
- 成功后进入未绑定状态。

- [ ] **步骤 5：实现回忆录页面**

首次进入提示设置 4-6 位数字密码。每次查看回忆录详情前都要求输入密码。

- [ ] **步骤 6：运行测试并提交**

```bash
cd frontend && npm test -- memory-store notification-store
git add frontend
git commit -m "feat: add profile memory and notifications"
```

预期：测试全部通过并完成提交。

## 任务 12：MVP 全链路验收和部署文档

**文件：**
- 创建：`docker-compose.yml`
- 创建：`backend/Dockerfile`
- 创建：`frontend/README.md`
- 创建：`backend/README.md`
- 创建：`docs/MVP验收清单.md`
- 修改：`backend/app/main.py`
- 测试：`backend/tests/test_mvp_contract.py`

- [ ] **步骤 1：添加完整 happy path 合同测试**

创建 `backend/tests/test_mvp_contract.py`：

```python
async def test_mvp_happy_path(couple_clients):
    her, him = couple_clients

    diary = await her.post("/api/diaries", json={"content": "今天开始一起写日记", "image_urls": []})
    assert diary.status_code == 201

    bet = await her.post("/api/bets", json={
        "title": "今天会下雨",
        "win_condition": "如果下雨她赢",
        "reward": "汉堡包",
        "deadline_at": "2026-06-11T22:00:00"
    })
    bet_id = bet.json()["id"]

    accepted = await him.post(f"/api/bets/{bet_id}/respond", json={"accepted": True})
    result = await her.post(f"/api/bets/{bet_id}/result", json={"winner_role": "她"})
    confirmed = await him.post(f"/api/bets/{bet_id}/result-confirm", json={"agree": True})

    assert accepted.json()["status"] == "active"
    assert result.status_code == 200
    assert confirmed.json()["status"] == "redeeming"
```

- [ ] **步骤 2：添加 Docker 配置**

创建 `docker-compose.yml`，包含：

- `mysql`
- `redis`
- `backend`

后端暴露 `8000` 端口，使用：

```text
DATABASE_URL=mysql+aiomysql://couple:couple@mysql:3306/couple_diary
REDIS_URL=redis://redis:6379/0
```

- [ ] **步骤 3：添加部署文档**

创建 `backend/README.md`，包含：

- 环境变量表。
- 本地安装命令。
- 数据库迁移命令。
- 测试命令。
- `uvicorn` 启动命令。

创建 `frontend/README.md`，包含：

- 依赖安装命令。
- H5 开发命令。
- 微信小程序构建命令。
- API base URL 配置位置。

- [ ] **步骤 4：添加 MVP 验收清单**

创建 `docs/MVP验收清单.md`，包含：

- 登录和角色选择。
- 邀请码绑定。
- 共享日记创建、时间线、日历和删除审批。
- 赌注创建、接受、拒绝、过期、结算、协商、兑现、完成。
- 和平解绑和拉黑。
- 回忆录密码和归档查看。
- 通知列表和已读状态。
- 后端测试套件。
- 前端测试套件。
- 微信小程序构建。

- [ ] **步骤 5：运行完整验证**

运行：

```bash
cd backend && pytest -q
```

预期：后端测试全部通过。

运行：

```bash
cd frontend && npm test
```

预期：前端测试全部通过。

运行：

```bash
cd frontend && npm run build:mp-weixin
```

预期：构建完成并输出微信小程序产物。

- [ ] **步骤 6：提交**

```bash
git add backend frontend docs docker-compose.yml
git commit -m "docs: add mvp deployment and acceptance"
```

## 自检

需求覆盖：

- 用户登录和角色选择由任务 3、任务 8 覆盖。
- 情侣绑定、和平解绑、强制拉黑、冷却期和重新绑定限制由任务 3、任务 6、任务 8、任务 11 覆盖。
- 日记内容、图片、时间线、日历、删除审批由任务 4、任务 9 覆盖。
- 赌注创建、奖励提醒、回应、过期、结算、协商、兑现、时间线、战绩由任务 5、任务 10 覆盖。
- 回忆录归档、密码门禁、归档时间线由任务 6、任务 11 覆盖。
- 通知和提醒记录由任务 5、任务 7、任务 11 覆盖。
- 非功能需求通过密码哈希、token 认证、Docker 文档、面向对象存储的图片 URL 模型、测试套件和 MVP 验收清单覆盖。

占位检查：

- 计划中没有待补充占位标记，也没有空实现步骤。
- 较宽的步骤都绑定了明确文件、接口列表、业务规则和测试预期。

类型一致性：

- 赌注状态值在模型测试、服务、前端状态映射和需求文档 5.2 中保持一致。
- 情侣状态值在绑定、解绑、拉黑和回忆录归档任务中保持一致。
- 后端路由任务和前端 API 客户端任务中的接口路径保持一致。
