"""QA 模型节点到公共 ModelGateway 的受信任、无模板回退适配。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentRun, AgentStep
from app.runtime.context_manager import ContextManager
from app.runtime.interfaces import LeaseContext
from app.runtime.langchain_components import render_model_messages
from app.runtime.model_gateway import ModelCallContext, ModelGateway, ModelGatewayResult
from app.runtime.prompt_registry import PromptRegistry


class QaModelGatewayAdapter:
    """仅从运行中的 Step、有效 lease 和部署配置派生 QA 模型调用。"""

    def __init__(
        self, session: Session, model_gateway: ModelGateway,
        route_ids: dict[str, str], lease_context: LeaseContext | None = None,
    ) -> None:
        self._session = session  # 仅用于读取 Run/Step 的权威执行身份。
        self._model_gateway = model_gateway  # 公共调用治理边界，负责实际 Provider 发送。
        self._route_ids = dict(route_ids)  # 仅接受工厂传入的部署节点映射。
        self._lease_context = lease_context  # 当前 Worker 的 fencing/ownership 凭据。
        self._prompts = PromptRegistry(Path(__file__).parents[1])  # 精确版本包内指令。
        self._contexts = ContextManager(node_token_caps={"compose_answer": 1024})  # 只能收紧路由预算。

    def bind_lease(self, lease_context: LeaseContext) -> None:
        """Executor 每次执行或恢复时绑定最新 lease，不接受请求自报身份。"""
        self._lease_context = lease_context

    def call(
        self, run_id: str, node_id: str, request: dict[str, object],
    ) -> ModelGatewayResult:
        """正文只存在当前调用栈的 data 槽；任何失败均交 Runner 终态收敛。"""
        if (
            node_id != "compose_answer"
            or set(self._route_ids) != {"compose_answer"}
            or not self._route_ids.get(node_id)
            or self._lease_context is None
        ):
            return ModelGatewayResult("route_not_allowed", error_code="QA_MODEL_ROUTE_UNAVAILABLE")
        try:
            run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
            steps = self._session.scalars(select(AgentStep).where(
                AgentStep.run_id == run_id,
                AgentStep.step_name == node_id,
                AgentStep.status == "running",
                AgentStep.execution_attempt == self._lease_context.execution_attempt,
            )).all()
            if run is None or (run.agent_id, run.agent_version) != ("qa_agent", "1.0.0") or len(steps) != 1:
                return ModelGatewayResult("aborted_before_send", error_code="QA_MODEL_CONTEXT_UNTRUSTED")
            context = ModelCallContext.from_authoritative(
                self._session, run_id, steps[0].step_id, self._lease_context,
            )
            prompt = self._prompts.load("qa_agent", "1.0.0", "compose-answer", "v1")
            route_id = self._route_ids[node_id]
            if not self._model_gateway.capability_available(route_id, prompt, context.estimated_input_tokens):
                return ModelGatewayResult("capability_disabled", error_code="MODEL_CAPABILITY_UNAVAILABLE")
            question_context = request.get("question_context")
            if not isinstance(question_context, dict) or set(question_context) != {"question", "evidence"}:
                return ModelGatewayResult("aborted_before_send", error_code="QA_MODEL_INPUT_INVALID")
            question, evidence = question_context["question"], question_context["evidence"]
            if not isinstance(question, str) or not isinstance(evidence, list) or any(
                not isinstance(item, dict)
                or set(item) != {"source_ref", "text"}
                or not isinstance(item["source_ref"], str)
                or not item["source_ref"]
                or not isinstance(item["text"], str)
                for item in evidence
            ):
                return ModelGatewayResult("aborted_before_send", error_code="QA_MODEL_INPUT_INVALID")
            refs = list(dict.fromkeys(item["source_ref"] for item in evidence))
            # 问题也占用 data 槽预算，但它的内部标签不能成为可引用的证据。
            question_ref = "qa:question"
            while question_ref in refs:
                question_ref += ":question"
            budget = self._contexts.node_token_budget(
                node_id, self._model_gateway.context_token_budget(route_id, prompt),
            )
            node_context = self._contexts.build_node_context(
                trusted_instructions=prompt.template,
                materials=[{"source_ref": question_ref, "text": question}, *evidence],
                tool_results=[], token_budget=budget,
            )
            # 只传脱敏且有界的内容；引用列表仅包括实际进入本轮上下文的证据。
            provider_request = {
                "messages": render_model_messages(prompt, node_context, {
                    "question_ref": question_ref,  # 内部问题槽标识，不授予证据资格。
                    "allowed_source_refs": [ref for ref in refs if ref in node_context.source_refs],
                }),
                "response_format": {"type": "json_object"},  # 要求结构化候选供 Runner 再校验。
            }
            # 静态工作流的 Step 默认没有 token 估算；按本次完整请求提高预留下限，
            # 避免以 0 token 穿透 Run 预算和共享 TPM。正文仅在内存编码，不写入摘要。
            encoded_request = json.dumps(provider_request, ensure_ascii=False, separators=(",", ":"))
            context = ModelCallContext.with_minimum_estimated_input_tokens(
                context, max(1, (len(encoded_request) + 3) // 4),
            )
            return self._model_gateway.call(context, route_id, provider_request, prompt=prompt)
        except ValueError:
            # Prompt、路由、预算及权威身份错误均不携带底层异常正文。
            return ModelGatewayResult("aborted_before_send", error_code="QA_MODEL_CALL_REJECTED")
        except Exception:
            logging.warning("QA 模型适配失败 run_id=%s node_id=%s", run_id, node_id)
            return ModelGatewayResult("outcome_unknown", error_code="QA_MODEL_OUTCOME_UNKNOWN")
