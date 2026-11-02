"""QA 的四节点静态执行器；所有业务正文仅在本实例的内存态流转。"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Protocol

from app.agents.qa_agent.state import QaAnswerDraft, QaSubmitResult, QaWorkingState
from app.agents.qa_agent.tools import (
    QA_HTTP_TOOLS,
    QA_TOOL_OUTPUT_FIELDS,
    submit_answer,
)
from app.models import AgentRun
from app.runtime.executor import WorkflowNodeError, WorkflowNodeErrorCode
from app.runtime.interfaces import LeaseContext
from app.runtime.model_gateway import ModelGatewayResult
from app.runtime.state import AgentState, apply_safe_output
from app.runtime.structured_output import StructuredOutputParser
from app.runtime.tool_gateway import ToolGateway
from app.schemas.agent_package import ToolManifest
from app.services.tool_call_audit_service import ToolCallAuditService


class QaModelGateway(Protocol):
    """Runner 仅传私密数据，权威上下文和可信 prompt 由模型适配器装配。"""

    def call(self, run_id: str, node_id: str, request: dict[str, object]) -> ModelGatewayResult: ...


def _manifest(name: str) -> ToolManifest:
    """从代码固定档案构造工具声明，不采信包中的调用坐标。"""
    connector, method, path, source, side_effect, cancellation = QA_HTTP_TOOLS[name]
    return ToolManifest(
        name=name, version="1.0.0", connector_id=connector, method=method,
        relative_path=path, input_from=source,
        output_to="question_context" if name == "qa.get_question_context" else "submit_result",
        side_effect=side_effect, cancellation_behavior=cancellation,
    )


class QaNodeRunner:
    """每次 Worker 派发新建实例；公共 AgentState 仅维护执行器路由元数据。"""

    def __init__(
        self, tool_gateway: ToolGateway, audit_service: ToolCallAuditService | None = None,
        model_gateway: QaModelGateway | None = None,
    ) -> None:
        self._gateway = tool_gateway
        self._audit = audit_service
        self._model = model_gateway
        self._lease: LeaseContext | None = None
        self._working = QaWorkingState()
        self._validated = False

    def bind_lease_context(self, lease_context: LeaseContext) -> None:
        """由公共执行器在节点前绑定 lease，工具审计和模型均使用同一权威代际。"""
        self._lease = lease_context
        bind = getattr(self._model, "bind_lease", None)
        if callable(bind):
            bind(lease_context)

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState,
    ) -> dict[str, object]:
        """只返回固定节点名和安全完成事实；所有异常在此截断正文传播。"""
        del state  # 私密工作态不附着公共状态，不新增任何 checkpoint 键。
        node_id = node.get("node_id")
        try:
            if node_id == "load_question_context":
                self._working = QaWorkingState()
                self._validated = False
                output = self._gateway.call(_manifest("qa.get_question_context"), self._context(run, str(node_id)))
                apply_safe_output(self._working, "question_context", output, QA_TOOL_OUTPUT_FIELDS)
            elif node_id == "compose_answer":
                self._compose(run)
            elif node_id == "validate_answer":
                self._validate()
                self._validated = True
            elif node_id == "submit_answer":
                if not self._validated:
                    raise WorkflowNodeError(WorkflowNodeErrorCode.ANSWER_VALIDATION_FAILED)
                self._validate()  # 提交前再校验，内存草稿变化也不能绕过约束。
                self._submit(run)
            else:
                raise WorkflowNodeError(WorkflowNodeErrorCode.STRUCTURED_OUTPUT_INVALID)
        except WorkflowNodeError:
            raise
        except Exception:
            # HTTP/Pydantic/Provider 异常均可能附带正文，严禁透传或记录 repr。
            logging.warning("QA 节点失败 run_id=%s code=TOOL_CALL_FAILED", run.run_id)
            raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_CALL_FAILED) from None
        logging.info("QA 节点完成 run_id=%s node_id=%s", run.run_id, node_id)
        return {"node_id": str(node_id), "completed": True}

    def _context(self, run: AgentRun, node_id: str) -> dict[str, object]:
        # 身份来自 Run，业务引用来自其已验证输入；调用前仍由 Gateway 校验匹配关系。
        if run.business_connector_id != "qa_business_backend":
            raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_CALL_FAILED)
        return {
            "conversation_id": run.input_json.get("conversation_id"),
            "turn_id": run.input_json.get("turn_id"), "run_id": run.run_id,
            "tool_context": ToolGateway.build_tool_context(run, node_id),
        }

    def _compose(self, run: AgentRun) -> None:
        context = self._working.question_context
        self._validated = False
        self._working.answer_draft = None
        if self._model is None or context is None:
            raise WorkflowNodeError(WorkflowNodeErrorCode.MODEL_UNAVAILABLE)
        try:
            result = self._model.call(run.run_id, "compose_answer", {"question_context": context.model_dump()})
        except Exception:
            raise WorkflowNodeError(WorkflowNodeErrorCode.MODEL_UNAVAILABLE) from None
        if result.status != "succeeded":
            budget_codes = {"MODEL_CALL_LIMIT_EXCEEDED", "MODEL_COST_LIMIT_EXCEEDED", "MODEL_TOKEN_LIMIT_EXCEEDED"}
            code = (
                WorkflowNodeErrorCode.MODEL_BUDGET_EXCEEDED
                if result.status == "policy_denied" and result.error_code in budget_codes
                else WorkflowNodeErrorCode.MODEL_UNAVAILABLE
            )
            raise WorkflowNodeError(code)
        raw = result.data if isinstance(result.data, str) else json.dumps(result.data, ensure_ascii=False)
        parsed = StructuredOutputParser().parse_and_validate(
            raw, QaAnswerDraft, trusted_source_refs={item.source_ref for item in context.evidence},
        )
        if parsed.validated_value is None:
            raise WorkflowNodeError(WorkflowNodeErrorCode.STRUCTURED_OUTPUT_INVALID)
        apply_safe_output(self._working, "answer_draft", parsed.validated_value.model_dump(), {"answer_draft"})
        self._validate()

    def _validate(self) -> None:
        """无证据路径仅允许显式无证据且无引用；有证据必须引用本次来源。"""
        context, draft = self._working.question_context, self._working.answer_draft
        if context is None or draft is None:
            raise WorkflowNodeError(WorkflowNodeErrorCode.ANSWER_VALIDATION_FAILED)
        checked = StructuredOutputParser().parse_and_validate(
            draft.model_dump_json(), QaAnswerDraft,
            trusted_source_refs={item.source_ref for item in context.evidence},
        )
        if (
            checked.validated_value is None or not draft.answer.strip()
            or (draft.no_evidence and (context.evidence or draft.source_refs))
            or (not draft.no_evidence and not draft.source_refs)
        ):
            raise WorkflowNodeError(WorkflowNodeErrorCode.ANSWER_VALIDATION_FAILED)

    def _submit(self, run: AgentRun) -> None:
        """先按稳定业务键查询；未知 attempt 未对账前绝不盲目重发。"""
        context = self._context(run, "submit_answer")
        logical_key = hashlib.sha256(json.dumps(
            ["qa.submit_answer", run.run_id, run.input_json.get("turn_id")],
            separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        observed = self._gateway.call(_manifest("qa.get_answer_result"), context, idempotency_key=logical_key)
        result = QaSubmitResult.model_validate(observed)
        previous = self._audit.find_publish_attempt(run.run_id, logical_key) if self._audit else None
        if result.submitted:
            if result.revision is None:
                raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_CALL_FAILED)
            if previous is not None and self._audit is not None:
                if not self._audit.succeed(previous, result.model_dump(), lease_context=self._lease):
                    raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_OUTCOME_UNKNOWN)
            apply_safe_output(self._working, "submit_result", observed, QA_TOOL_OUTPUT_FIELDS)
            return
        if previous is not None:
            raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_OUTCOME_UNKNOWN)
        if self._audit is None or self._lease is None or self._working.answer_draft is None:
            raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_CALL_FAILED)
        document = self._working.answer_draft.model_dump()
        digest = hashlib.sha256(json.dumps(
            document, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()
        try:
            audit = self._audit.begin_side_effect(
                run_id=run.run_id, execution_attempt=self._lease.execution_attempt,
                step_id="submit_answer", tool_name="qa.submit_answer", tool_version="1.0.0",
                transport="http_business_tool", logical_key=logical_key, idempotency_key=logical_key,
                request_digest=digest, input_summary={"operation": "qa.submit_answer"},
                lease_context=self._lease,
            )
        except ValueError as exc:
            if str(exc) == "TOOL_CALL_LIMIT_EXCEEDED":
                raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_CALL_LIMIT_EXCEEDED) from None
            raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_CALL_FAILED) from None
        try:
            output = submit_answer(self._gateway, {**context, "answer_draft": document}, logical_key, audit)
            result = QaSubmitResult.model_validate(output)
            if not result.submitted or result.revision is None:
                raise ValueError("QA_SUBMIT_RESULT_INVALID")
        except Exception:
            # 已进入提交阶段，任何未确认结果都保守记为未知，恢复时只查询原幂等键。
            self._audit.unknown(audit, "TOOL_OUTCOME_UNKNOWN", lease_context=self._lease)
            raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_OUTCOME_UNKNOWN) from None
        if not self._audit.succeed(audit, result.model_dump(), lease_context=self._lease):
            raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_OUTCOME_UNKNOWN)
        apply_safe_output(self._working, "submit_result", output, QA_TOOL_OUTPUT_FIELDS)
