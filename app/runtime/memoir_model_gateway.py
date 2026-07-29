"""Memoir Runner 与受信任 ModelGateway 的执行边界适配。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentRun, AgentStep
from app.runtime.context_manager import ContextManager
from app.runtime.interfaces import LeaseContext
from app.runtime.langchain_components import render_model_messages
from app.runtime.model_gateway import ModelCallContext, ModelGateway, ModelGatewayResult
from app.runtime.prompt_registry import PromptRegistry, PromptRegistryError

_PROMPT_REFS = {
    "extract_highlights": ("highlight-extract", "v1"),
    "plan_chapters": ("chapter-plan", "v1"),
    "generate_scenes": ("scene-generate", "v1"),
}
_REPAIR_PROMPT_REF = ("structured-output-repair", "v1")
_MAX_REPAIR_CANDIDATE_CHARS = 2048


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
        self._contexts = ContextManager()

    def bind_lease(self, lease_context: LeaseContext) -> None:
        self._lease_context = lease_context

    def call(
        self, run_id: str, node_id: str, request: dict[str, object]
    ) -> ModelGatewayResult:
        prompt_ref = _PROMPT_REFS.get(node_id)
        if prompt_ref is None or self._lease_context is None:
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
        # 先冻结 Agent 的 logical policy，再读取部署 route 映射；请求、Package
        # 输入与 prompt 正文均没有 provider/model/URL/key 的覆盖入口。
        route_id = self._route_ids.get(node_id)
        if route_id is None:
            return ModelGatewayResult("route_not_allowed")
        # 在渲染消息前使用与 Gateway 相同的判定，避免已知不兼容配置产生任何 Provider 调用。
        if not self._model_gateway.capability_available(
            route_id, prompt, context.estimated_input_tokens
        ):
            return ModelGatewayResult("capability_disabled", error_code="MODEL_CAPABILITY_UNAVAILABLE")
        # Prompt 身份只认部署注册表。候选输入只可作为不可信 data 槽进入消息，
        # 绝不允许 request 自报 provider、route、授权或运行控制字段。
        candidate_input = request.get("input", {})
        if not isinstance(candidate_input, Mapping):
            return ModelGatewayResult("aborted_before_send")
        refs = _candidate_source_refs(candidate_input)
        try:
            token_budget = self._contexts.node_token_budget(
                node_id, self._model_gateway.context_token_budget(route_id, prompt)
            )
            node_context = self._contexts.build_node_context(
                trusted_instructions=prompt.template,
                materials=[{"source_ref": ref, "text": "[SOURCE_REF]"} for ref in refs],
                tool_results=[],
                token_budget=token_budget,
            )
            provider_request = {
                "messages": render_model_messages(
                    prompt, node_context, candidate_input
                )
            }
        except ValueError:
            return ModelGatewayResult("aborted_before_send")
        try:
            return self._model_gateway.call(
                context, route_id, provider_request, prompt=prompt,
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

    def repair(
        self,
        run_id: str,
        node_id: str,
        request: dict[str, object],
        invalid_output: object,
    ) -> ModelGatewayResult:
        """使用精确版本 repair prompt 发起一次新的受治理物理模型调用。

        原始候选只在本调用栈内转成有界 untrusted data，既不写入 usage，也不进入
        日志、Artifact、Checkpoint 或 callback。调用方负责保证本方法最多执行一次。
        """
        prompt_ref = _PROMPT_REFS.get(node_id)
        if prompt_ref is None or self._lease_context is None:
            return ModelGatewayResult("route_not_allowed")
        try:
            run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
            steps = self._session.scalars(
                select(AgentStep).where(
                    AgentStep.run_id == run_id,
                    AgentStep.step_name == node_id,
                    AgentStep.status == "running",
                    AgentStep.execution_attempt
                    == self._lease_context.execution_attempt,
                )
            ).all()
        except Exception:
            logging.warning(
                "Memoir repair 权威上下文查询失败 run_id=%s node_id=%s",
                run_id,
                node_id,
            )
            return ModelGatewayResult("aborted_before_send")
        if run is None or len(steps) != 1:
            return ModelGatewayResult("aborted_before_send")
        step = steps[0]
        try:
            context = ModelCallContext.from_authoritative(
                self._session, run_id, step.step_id, self._lease_context,
            )
            original_prompt = self._prompts.load(
                run.agent_id,
                run.agent_version,
                prompt_ref[0],
                prompt_ref[1],
            )
            repair_prompt = self._prompts.load(
                run.agent_id,
                run.agent_version,
                _REPAIR_PROMPT_REF[0],
                _REPAIR_PROMPT_REF[1],
            )
            raw_candidate = self._serialize_invalid_output(invalid_output)
        except (PromptRegistryError, ValueError):
            return ModelGatewayResult("aborted_before_send")
        route_id = self._route_ids.get(node_id)
        if route_id is None:
            return ModelGatewayResult("route_not_allowed")
        if not self._model_gateway.capability_available(
            route_id, repair_prompt, context.estimated_input_tokens,
        ):
            return ModelGatewayResult(
                "capability_disabled",
                error_code="MODEL_CAPABILITY_UNAVAILABLE",
            )
        safe_input = request.get("input")
        if not isinstance(safe_input, Mapping):
            return ModelGatewayResult("aborted_before_send")
        refs = _candidate_source_refs(safe_input)
        try:
            token_budget = self._contexts.node_token_budget(
                node_id,
                self._model_gateway.context_token_budget(route_id, repair_prompt),
            )
            repair_context = self._contexts.build_node_context(
                trusted_instructions=repair_prompt.template,
                materials=[
                    {
                        "source_ref": "model_candidate",
                        "text": raw_candidate,
                    },
                ],
                tool_results=[],
                token_budget=token_budget,
            )
            provider_request = {
                "messages": render_model_messages(
                    repair_prompt,
                    repair_context,
                    {
                        "original_prompt_id": original_prompt.prompt_id,
                        "original_prompt_version": original_prompt.version,
                        "output_schema": original_prompt.output_schema,
                        "allowed_source_refs": refs,
                        "validation_code": "MODEL_OUTPUT_INVALID",
                    },
                ),
            }
            encoded_request = json.dumps(
                provider_request,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            context = ModelCallContext.with_minimum_estimated_input_tokens(
                context,
                max(1, (len(encoded_request) + 3) // 4),
            )
        except ValueError:
            return ModelGatewayResult("aborted_before_send")
        if not self._model_gateway.capability_available(
            route_id,
            repair_prompt,
            context.estimated_input_tokens,
        ):
            return ModelGatewayResult(
                "capability_disabled",
                error_code="MODEL_CAPABILITY_UNAVAILABLE",
            )
        try:
            # 同一 Gateway 会重新执行 route 治理、预算预留、Redis permit、usage
            # 预写以及发送前后的 lease/privacy/authorization/deadline 复核。
            return self._model_gateway.call(
                context,
                route_id,
                provider_request,
                prompt=repair_prompt,
            )
        except ValueError:
            return ModelGatewayResult("route_not_allowed")
        except Exception:
            logging.warning(
                "Memoir repair 模型适配失败 run_id=%s step_id=%s node_id=%s",
                run_id,
                step.step_id,
                node_id,
            )
            return ModelGatewayResult("outcome_unknown")

    def record_validation_rejection(self, node_id: str, error_codes: tuple[str, ...]) -> None:
        """Runner 仅传节点与受控码，route 仍只从部署映射取得。"""
        route_id = self._route_ids.get(node_id)
        if route_id is not None:
            self._model_gateway.record_validation_rejection(route_id, error_codes)

    @staticmethod
    def _serialize_invalid_output(value: object) -> str:
        """只在内存中生成有界 repair 候选；不可序列化类型直接拒绝。"""
        if isinstance(value, str):
            return value[:_MAX_REPAIR_CANDIDATE_CHARS]
        if isinstance(value, Mapping):
            try:
                serialized = json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("MODEL_REPAIR_INPUT_INVALID") from exc
            return serialized[:_MAX_REPAIR_CANDIDATE_CHARS]
        raise ValueError("MODEL_REPAIR_INPUT_INVALID")


def _candidate_source_refs(candidate_input: Mapping[str, object]) -> list[str]:
    """仅提取引用 ID 作为 ContextManager 的占位数据，不透传任何素材正文。"""
    source_refs = candidate_input.get("source_refs")
    if isinstance(source_refs, list):
        return [ref for ref in source_refs if isinstance(ref, str)]
    chapters = candidate_input.get("chapters")
    if not isinstance(chapters, list):
        return []
    return [
        ref
        for chapter in chapters
        if isinstance(chapter, Mapping)
        for ref in chapter.get("source_refs", [])
        if isinstance(ref, str)
    ]
