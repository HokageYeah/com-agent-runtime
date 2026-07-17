"""Memoir Runner 与受信任 ModelGateway 的执行边界适配。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentStep
from app.runtime.interfaces import LeaseContext
from app.runtime.model_gateway import ModelCallContext, ModelGateway, ModelGatewayResult


class MemoirModelGatewayAdapter:
    """只从运行中的权威 Step、lease 和部署映射发起 Memoir 模型调用。"""

    def __init__(
        self,
        session: Session,
        model_gateway: ModelGateway,
        route_ids: dict[str, str],
        lease_context: LeaseContext | None = None,
    ) -> None:
        self._session = session
        self._model_gateway = model_gateway
        self._route_ids = dict(route_ids)
        self._lease_context = lease_context

    def bind_lease(self, lease_context: LeaseContext) -> None:
        self._lease_context = lease_context

    def call(
        self, run_id: str, node_id: str, request: dict[str, object]
    ) -> ModelGatewayResult:
        route_id = self._route_ids.get(node_id)
        if route_id is None or self._lease_context is None:
            return ModelGatewayResult("route_not_allowed")
        step = self._session.scalar(
            select(AgentStep).where(
                AgentStep.run_id == run_id,
                AgentStep.step_name == node_id,
                AgentStep.status == "running",
                AgentStep.execution_attempt == self._lease_context.execution_attempt,
            )
        )
        if step is None:
            return ModelGatewayResult("aborted_before_send")
        try:
            context = ModelCallContext.from_authoritative(
                self._session, run_id, step.step_id, self._lease_context
            )
        except ValueError:
            return ModelGatewayResult("aborted_before_send")
        return self._model_gateway.call(context, route_id, request)
