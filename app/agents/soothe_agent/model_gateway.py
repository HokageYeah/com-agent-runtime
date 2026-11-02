"""SOOTHE 模型节点到公共 ModelGateway 的受信任、无模板回退适配。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.soothe_agent.state import ReplyContext, SimulatorContext
from app.models import AgentRun, AgentStep
from app.runtime.context_manager import ContextManager
from app.runtime.interfaces import LeaseContext
from app.runtime.langchain_components import render_model_messages
from app.runtime.model_gateway import ModelCallContext, ModelGateway, ModelGatewayResult
from app.runtime.prompt_registry import PromptRegistry


class SootheModelGatewayAdapter:
    """仅从运行中的 Step、有效 lease 和部署配置派生 SOOTHE 模型调用。"""

    def __init__(
        self,
        session: Session,
        model_gateway: ModelGateway,
        route_ids: dict[str, str],
        lease_context: LeaseContext | None = None,
    ) -> None:
        self._session = session  # 仅用于读取 Run/Step 的权威执行身份。
        self._model_gateway = (
            model_gateway  # 公共调用治理边界，负责实际 Provider 发送。
        )
        self._route_ids = dict(route_ids)  # 仅接受工厂传入的部署节点映射。
        self._lease_context = lease_context  # 当前 Worker 的 fencing/ownership 凭据。
        self._prompts = PromptRegistry(Path(__file__).parents[1])  # 精确版本包内指令。
        self._contexts = ContextManager(
            node_token_caps={"compose_turn": 4096}
        )  # 只能收紧路由预算。

    def bind_lease(self, lease_context: LeaseContext) -> None:
        """Executor 每次执行或恢复时绑定最新 lease，不接受请求自报身份。"""
        self._lease_context = lease_context

    def call(
        self,
        run_id: str,
        node_id: str,
        request: dict[str, object],
    ) -> ModelGatewayResult:
        """私密正文仅在调用栈流转；用户内容固定进入 data 槽，失败由 Runner 收敛。"""
        if (
            node_id != "compose_turn"
            or set(self._route_ids) != {"compose_turn"}
            or not self._route_ids.get(node_id)
            or self._lease_context is None
        ):
            return ModelGatewayResult(
                "route_not_allowed", error_code="SOOTHE_MODEL_ROUTE_UNAVAILABLE"
            )
        try:
            run = self._session.scalar(
                select(AgentRun).where(AgentRun.run_id == run_id)
            )
            steps = self._session.scalars(
                select(AgentStep).where(
                    AgentStep.run_id == run_id,
                    AgentStep.step_name == node_id,
                    AgentStep.status == "running",
                    AgentStep.execution_attempt
                    == self._lease_context.execution_attempt,
                )
            ).all()
            if (
                run is None
                or (run.agent_id, run.agent_version) != ("soothe_agent", "1.0.0")
                or run.business_type not in {"soothe_reply", "soothe_simulator"}
                or len(steps) != 1
            ):
                return ModelGatewayResult(
                    "aborted_before_send", error_code="SOOTHE_MODEL_CONTEXT_UNTRUSTED"
                )
            context = ModelCallContext.from_authoritative(
                self._session,
                run_id,
                steps[0].step_id,
                self._lease_context,
            )
            try:
                if set(request) != {"turn_context"}:
                    raise ValueError("invalid input")
                private_context = (
                    ReplyContext.model_validate(request.get("turn_context"))
                    if run.business_type == "soothe_reply"
                    else SimulatorContext.model_validate(request.get("turn_context"))
                )
            except (ValidationError, ValueError):
                return ModelGatewayResult(
                    "aborted_before_send", error_code="SOOTHE_MODEL_INPUT_INVALID"
                )
            prompt_id = (
                "compose-reply"
                if isinstance(private_context, ReplyContext)
                else "compose-simulator"
            )
            prompt = self._prompts.load("soothe_agent", "1.0.0", prompt_id, "v1")
            # 仅对本地复制的模板做单次占位符替换；JSON 花括号保持原样，
            # 场景文字里即使出现占位符也不会二次解释。用户正文不进入此映射。
            if isinstance(private_context, ReplyContext):
                variables: dict[str, str] = {"tone": private_context.tone}
                materials = [
                    {"source_ref": "soothe:chat", "text": private_context.chat_text}
                ]
                if private_context.background:
                    materials.append(
                        {
                            "source_ref": "soothe:background",
                            "text": private_context.background,
                        }
                    )
                if private_context.previous_replies:
                    materials.append(
                        {
                            "source_ref": "soothe:previous_replies",
                            "text": "\n".join(private_context.previous_replies),
                        }
                    )
            else:
                variables = {
                    "partner_role": {"male": "男朋友", "female": "女朋友"}[
                        private_context.partner_gender
                    ],
                    "scenario_title": private_context.scenario.title,
                    "anger_reason": private_context.scenario.anger_reason,
                    "hidden_need": private_context.scenario.hidden_need,
                    "forgiveness": str(private_context.forgiveness),
                    "round_count": str(private_context.round_count),
                }
                materials = [
                    {
                        "source_ref": "soothe:history",
                        "text": "\n".join(
                            f"我：{item.user_message}\nTA：{item.reply}"
                            for item in private_context.history
                        ),
                    },
                    {
                        "source_ref": "soothe:user_message",
                        "text": private_context.user_message,
                    },
                    {
                        "source_ref": "soothe:partner_name",
                        "text": private_context.partner_name,
                    },
                ]
            prompt = replace(
                prompt,
                template=re.sub(
                    r"\{\{([a-z_]+)\}\}",
                    lambda match: variables[match.group(1)],
                    prompt.template,
                ),
            )
            route_id = self._route_ids[node_id]
            if not self._model_gateway.capability_available(
                route_id, prompt, context.estimated_input_tokens
            ):
                return ModelGatewayResult(
                    "capability_disabled", error_code="MODEL_CAPABILITY_UNAVAILABLE"
                )
            budget = self._contexts.node_token_budget(
                node_id,
                self._model_gateway.context_token_budget(route_id, prompt),
            )
            node_context = self._contexts.build_node_context(
                trusted_instructions=prompt.template,
                materials=materials,
                tool_results=[],
                token_budget=budget,
            )
            # 只传脱敏且有界的素材；可信场景和隐藏诉求仅在本轮调用栈流转。
            provider_request = {
                "messages": render_model_messages(prompt, node_context, {}),
                "response_format": {
                    "type": "json_object"
                },  # 要求结构化候选供 Runner 再校验。
            }
            # 静态工作流的 Step 默认没有 token 估算；按本次完整请求提高预留下限，
            # 避免以 0 token 穿透 Run 预算和共享 TPM。正文仅在内存编码，不写入摘要。
            encoded_request = json.dumps(
                provider_request, ensure_ascii=False, separators=(",", ":")
            )
            context = ModelCallContext.with_minimum_estimated_input_tokens(
                context,
                max(1, (len(encoded_request) + 3) // 4),
            )
            return self._model_gateway.call(
                context, route_id, provider_request, prompt=prompt
            )
        except ValueError:
            # Prompt、路由、预算及权威身份错误均不携带底层异常正文。
            return ModelGatewayResult(
                "aborted_before_send", error_code="SOOTHE_MODEL_CALL_REJECTED"
            )
        except Exception:
            logging.warning("SOOTHE 模型适配失败 run_id=%s node_id=%s", run_id, node_id)
            return ModelGatewayResult(
                "outcome_unknown", error_code="SOOTHE_MODEL_OUTCOME_UNKNOWN"
            )
