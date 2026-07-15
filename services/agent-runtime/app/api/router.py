from __future__ import annotations

from fastapi import APIRouter

from app.api.endpoints.agent_runs_api import router as agent_runs_router
from app.api.endpoints.capabilities_api import router as capabilities_router
from app.api.endpoints.health_api import router as health_router

router = APIRouter()
router.include_router(health_router)
router.include_router(capabilities_router)
router.include_router(agent_runs_router)
