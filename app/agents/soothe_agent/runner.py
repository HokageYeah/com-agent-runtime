"""Soothe 的四节点静态执行器；所有业务正文仅在本实例的内存态流转。"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Protocol

from app.agents.soothe_agent.state import (
    ReplyContext,
    ReplyDraft,
    SimulatorContext,
    SimulatorDraft,
    SootheSubmitResult,
    SootheWorkingState,
)
from app.agents.soothe_agent.tools import (
    SOOTHE_HTTP_TOOLS,
    SOOTHE_TOOL_OUTPUT_FIELDS,
    submit_turn_result,
)
from app.agents.soothe_agent.validation import validate_draft
from app.models import AgentRun
from app.runtime.executor import WorkflowNodeError, WorkflowNodeErrorCode
from app.runtime.interfaces import LeaseContext
from app.runtime.model_gateway import ModelGatewayResult
from app.runtime.state import AgentState, apply_safe_output
from app.runtime.structured_output import StructuredOutputParser
from app.runtime.tool_gateway import ToolGateway
from app.schemas.agent_package import ToolManifest
from app.services.tool_call_audit_service import ToolCallAuditService


class SootheModelGateway(Protocol):
    """Runner 仅传私密数据，权威上下文和可信 prompt 由模型适配器装配。"""

    def call(self, run_id: str, node_id: str, request: dict[str, object]) -> ModelGatewayResult: ...


def _manifest(name: str) -> ToolManifest:
    """从代码固定档案构造工具声明，不采信包中的调用坐标。"""
    connector, method, path, source, side_effect, cancellation = SOOTHE_HTTP_TOOLS[name]
    return ToolManifest(
        name=name, version="1.0.0", connector_id=connector, method=method,
        relative_path=path, input_from=source,
        output_to="turn_context" if name == "soothe.get_turn_context" else "submit_result",
        side_effect=side_effect, cancellation_behavior=cancellation,
    )


class SootheNodeRunner:
    """每次 Worker 派发新建实例；公共 AgentState 仅维护执行器路由元数据。"""

    def __init__(
        self, tool_gateway: ToolGateway, audit_service: ToolCallAuditService | None = None,
        model_gateway: SootheModelGateway | None = None,
    ) -> None:
        self._gateway = tool_gateway
        self._audit = audit_service
        self._model = model_gateway
        self._lease: LeaseContext | None = None
        self._working = SootheWorkingState()
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
            if node_id == "load_turn_context":
                self._working = SootheWorkingState()
                self._validated = False
                output = self._gateway.call(_manifest("soothe.get_turn_context"), self._context(run, str(node_id)))
                # 业务 wire 外层是 turn_context，只将其内容写入严格的私密判别联合。
                if set(output) != {"turn_context"}:
                    raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_CALL_FAILED)
                apply_safe_output(self._working, "turn_context", output["turn_context"], SOOTHE_TOOL_OUTPUT_FIELDS)
                expected = {"soothe_reply": "reply", "soothe_simulator": "simulator"}.get(run.business_type)
                if self._working.turn_context is None or self._working.turn_context.mode != expected:
                    raise WorkflowNodeError(WorkflowNodeErrorCode.ANSWER_VALIDATION_FAILED)
            elif node_id == "compose_turn":
                self._compose(run)
            elif node_id == "validate_turn":
                self._validate(run)
                self._validated = True
            elif node_id == "submit_turn_result":
                if not self._validated:
                    raise WorkflowNodeError(WorkflowNodeErrorCode.ANSWER_VALIDATION_FAILED)
                self._validate(run)  # 提交前再校验，内存草稿变化也不能绕过约束。
                self._submit(run)
            else:
                raise WorkflowNodeError(WorkflowNodeErrorCode.STRUCTURED_OUTPUT_INVALID)
        except WorkflowNodeError:
            raise
        except Exception:
            # HTTP/Pydantic/Provider 异常均可能附带正文，严禁透传或记录 repr。
            logging.warning("Soothe 节点失败 run_id=%s code=TOOL_CALL_FAILED", run.run_id)
            raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_CALL_FAILED) from None
        logging.info("Soothe 节点完成 run_id=%s node_id=%s", run.run_id, node_id)
        return {"node_id": str(node_id), "completed": True}

    def _context(self, run: AgentRun, node_id: str) -> dict[str, object]:
        # 身份来自 Run，业务引用来自其已验证输入；调用前仍由 Gateway 校验匹配关系。
        if run.business_connector_id != "couple_diary_backend":
            raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_CALL_FAILED)
        return {
            "session_id": run.input_json.get("session_id"),
            "turn_id": run.input_json.get("turn_id"), "run_id": run.run_id,
            "tool_context": ToolGateway.build_tool_context(run, node_id),
        }

    def _compose(self, run: AgentRun) -> None:
        context = self._working.turn_context
        self._validated = False
        self._working.turn_draft = None
        if self._model is None or context is None:
            raise WorkflowNodeError(WorkflowNodeErrorCode.MODEL_UNAVAILABLE)
        try:
            result = self._model.call(run.run_id, "compose_turn", {"turn_context": context.model_dump()})
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
            raw, ReplyDraft if run.business_type == "soothe_reply" else SimulatorDraft,
            trusted_source_refs=set(),
        )
        if parsed.validated_value is None:
            raise WorkflowNodeError(WorkflowNodeErrorCode.STRUCTURED_OUTPUT_INVALID)
        apply_safe_output(self._working, "turn_draft", parsed.validated_value.model_dump(), {"turn_draft"})
        self._validate(run)

    def _validate(self, run: AgentRun) -> None:
        """每次校验都重新解析内存数据，防止提交前长度或控制字段约束被绕过。"""
        context, draft = self._working.turn_context, self._working.turn_draft
        if context is None or draft is None:
            raise WorkflowNodeError(WorkflowNodeErrorCode.ANSWER_VALIDATION_FAILED)
        try:
            if run.business_type == "soothe_reply":
                checked_context = ReplyContext.model_validate(context.model_dump())
                checked_draft = ReplyDraft.model_validate(draft.model_dump())
                validate_draft(run.business_type, checked_context, checked_draft)
            elif run.business_type == "soothe_simulator":
                sim_context = SimulatorContext.model_validate(context.model_dump())
                sim_draft = SimulatorDraft.model_validate(draft.model_dump())
                validate_draft(run.business_type, sim_context, sim_draft)
            else:
                raise ValueError("SOOTHE_BUSINESS_TYPE_INVALID")
        except WorkflowNodeError:
            raise
        except ValueError:
            raise WorkflowNodeError(WorkflowNodeErrorCode.ANSWER_VALIDATION_FAILED) from None

    def _submit(self, run: AgentRun) -> None:
        """先按稳定业务键查询；未知 attempt 未对账前绝不盲目重发。"""
        context = self._context(run, "submit_turn_result")
        logical_key = hashlib.sha256(json.dumps(
            ["soothe.submit_turn_result", run.run_id, run.input_json.get("turn_id")],
            separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        observed = self._gateway.call(_manifest("soothe.get_turn_result"), context, idempotency_key=logical_key)
        result = SootheSubmitResult.model_validate(observed)
        previous = self._audit.find_publish_attempt(run.run_id, logical_key) if self._audit else None
        if result.submitted:
            if result.revision is None:
                raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_CALL_FAILED)
            if previous is not None and self._audit is not None:
                if not self._audit.succeed(previous, result.model_dump(), lease_context=self._lease):
                    raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_OUTCOME_UNKNOWN)
            apply_safe_output(self._working, "submit_result", observed, SOOTHE_TOOL_OUTPUT_FIELDS)
            return
        if previous is not None:
            raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_OUTCOME_UNKNOWN)
        if self._audit is None or self._lease is None or self._working.turn_draft is None:
            raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_CALL_FAILED)
        document = self._working.turn_draft.model_dump()
        digest = hashlib.sha256(json.dumps(
            document, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()
        try:
            audit = self._audit.begin_side_effect(
                run_id=run.run_id, execution_attempt=self._lease.execution_attempt,
                step_id="submit_turn_result", tool_name="soothe.submit_turn_result", tool_version="1.0.0",
                transport="http_business_tool", logical_key=logical_key, idempotency_key=logical_key,
                request_digest=digest, input_summary={"operation": "soothe.submit_turn_result"},
                lease_context=self._lease,
            )
        except ValueError as exc:
            if str(exc) == "TOOL_CALL_LIMIT_EXCEEDED":
                raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_CALL_LIMIT_EXCEEDED) from None
            raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_CALL_FAILED) from None
        try:
            output = submit_turn_result(self._gateway, {**context, "turn_result": document}, logical_key, audit)
            result = SootheSubmitResult.model_validate(output)
            if not result.submitted or result.revision is None:
                raise ValueError("SOOTHE_SUBMIT_RESULT_INVALID")
        except Exception:
            # 已进入提交阶段，任何未确认结果都保守记为未知，恢复时只查询原幂等键。
            self._audit.unknown(audit, "TOOL_OUTCOME_UNKNOWN", lease_context=self._lease)
            raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_OUTCOME_UNKNOWN) from None
        if not self._audit.succeed(audit, result.model_dump(), lease_context=self._lease):
            raise WorkflowNodeError(WorkflowNodeErrorCode.TOOL_OUTCOME_UNKNOWN)
        apply_safe_output(self._working, "submit_result", output, SOOTHE_TOOL_OUTPUT_FIELDS)
