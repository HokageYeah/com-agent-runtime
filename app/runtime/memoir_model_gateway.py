"""Memoir Runner 与受信任 ModelGateway 的执行边界适配。"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentRun, AgentStep
from app.runtime.interfaces import LeaseContext
from app.runtime.model_gateway import ModelCallContext, ModelGateway, ModelGatewayResult
from app.runtime.prompt_registry import PromptRegistry, PromptRegistryError

_PROMPT_REFS = {
    "extract_highlights": ("highlight-extract", "v1"),
    "plan_chapters": ("chapter-plan", "v1"),
    "generate_scenes": ("scene-generate", "v1"),
}


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
        self._prompts = PromptRegistry(Path(__file__).parents[1] / "agents")

    def bind_lease(self, lease_context: LeaseContext) -> None:
        self._lease_context = lease_context

    def call(
        self, run_id: str, node_id: str, request: dict[str, object]
    ) -> ModelGatewayResult:
        route_id = self._route_ids.get(node_id)
        prompt_ref = _PROMPT_REFS.get(node_id)
        if route_id is None or prompt_ref is None or self._lease_context is None:
            return ModelGatewayResult("route_not_allowed")
        try:
            run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
            steps = self._session.scalars(
                select(AgentStep).where(
                    AgentStep.run_id == run_id,
                    AgentStep.step_name == node_id,
                    AgentStep.status == "running",
                    AgentStep.execution_attempt == self._lease_context.execution_attempt,
                )
            ).all()
        except Exception:
            logging.warning(
                "Memoir 模型权威上下文查询失败 run_id=%s node_id=%s", run_id, node_id,
            )
            return ModelGatewayResult("aborted_before_send")
        if run is None or len(steps) != 1:
            return ModelGatewayResult("aborted_before_send")
        step = steps[0]
        try:
            context = ModelCallContext.from_authoritative(
                self._session, run_id, step.step_id, self._lease_context
            )
            prompt = self._prompts.load(
                run.agent_id, run.agent_version, prompt_ref[0], prompt_ref[1],
            )
        except (PromptRegistryError, ValueError):
            return ModelGatewayResult("aborted_before_send")
        # Prompt 身份与策略只认部署内注册表，忽略调用 request 中可伪造的同名字段。
        safe_request = dict(request)
        safe_request.update({
            "prompt_id": prompt.prompt_id,
            "prompt_version": prompt.version,
            "model_policy": prompt.model_policy,
        })
        try:
            return self._model_gateway.call(
                context, route_id, safe_request, prompt=prompt,
            )
        except ValueError:
            # route 配置失配属于能力不可用，交给 Runner 做确定性 fallback。
            return ModelGatewayResult("route_not_allowed")
        except Exception:
            # 禁止记录异常详情，避免 Provider 或业务正文随异常消息进入日志。
            logging.warning(
                "Memoir 模型适配失败 run_id=%s step_id=%s node_id=%s",
                run_id, step.step_id, node_id,
            )
            return ModelGatewayResult("outcome_unknown")
