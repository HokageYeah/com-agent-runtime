from fastapi import APIRouter

from app.api.endpoints import (
    agent_runs_api,
    capabilities_api,
    demo_api,
    diary_api,
    health_api,
    memory_callbacks_api,
    memory_status_api,
    memory_tools_api,
)

api_router = APIRouter()

# 当前只保留工程示例接口。
# 这里统一挂到 /demo 下，明确告诉后续开发者：
# 这组接口是“示例规范接口”，不是正式业务域。
# diary 则是当前模板工程里的“正式业务域样板”，
# 用来演示后续真实模块应该怎样独立注册、独立演进。
# 后续新增真实业务模块时，继续通过这里集中注册新 router，
# 保持路由入口统一，方便团队协作和 LLM 快速建立上下文。
api_router.include_router(demo_api.router, prefix="/demo", tags=["工程示例接口"])
api_router.include_router(diary_api.router, prefix="/diary", tags=["日记业务模块"])
api_router.include_router(health_api.router, prefix="/runtime", tags=["AgentRuntime"])
api_router.include_router(
    capabilities_api.router, prefix="/runtime", tags=["AgentRuntime"]
)
api_router.include_router(
    agent_runs_api.router, prefix="/runtime", tags=["AgentRuntime"]
)
api_router.include_router(memory_tools_api.router)
api_router.include_router(memory_callbacks_api.router)
api_router.include_router(memory_status_api.router)
