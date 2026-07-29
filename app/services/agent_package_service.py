from __future__ import annotations

import ast
import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from app.contracts.api import CONTRACT_VERSION
from app.schemas.agent_package import (
    AgentPackage,
    CallbackConfig,
    PackagePolicy,
    ToolManifest,
    UiTraceConfig,
    WorkflowNodeDefinition,
)

if TYPE_CHECKING:
    from app.models.runtime import AgentDefinition
    from app.services.audit_service import AuditService


class AgentPackageValidationError(ValueError):
    """Package 不满足运行时信任边界时抛出的安全校验异常。"""


class AgentPackageService:
    """只从部署内置目录读取经 CI/管理员注册的 AgentPackage。"""

    _REQUIRED_FILES = {
        "agent.yaml",
        "input.schema.json",
        "output.schema.json",
        "workflow.graph.py",
        "tools.manifest.json",
        "guardrails.yaml",
        "callbacks.yaml",
        "ui-trace.yaml",
    }
    _EXCLUDED_DIGEST_FILES = {"package.sig", "package.digest", "build-metadata.json"}

    def __init__(
        self, package_root: Path, audit_service: AuditService | None = None
    ) -> None:
        self._package_root = package_root.resolve()
        self._registered_digests: dict[tuple[str, str], str] = {}
        self._audit_service = audit_service

    def load(self, agent_id: str, version: str) -> AgentPackage:
        """精确加载指定版本，拒绝 latest 或调用方提供的任意 Python 文件路径。"""
        if not agent_id or not version or version == "latest":
            raise AgentPackageValidationError(
                "必须精确指定 agent_id 与 version，禁止 latest"
            )
        package_dir = (self._package_root / agent_id / version).resolve()
        if self._package_root not in package_dir.parents:
            raise AgentPackageValidationError("Package 路径越界")
        self._validate_required_files(package_dir)
        digest = self._digest(package_dir)
        definition = self._read_yaml(package_dir / "agent.yaml")
        self._validate_identity(definition, agent_id, version)
        if definition.get("contract_version") != CONTRACT_VERSION:
            raise AgentPackageValidationError("Package contract_version 不兼容")
        self.validate_lifecycle(definition.get("status", "active"), operation="load")

        workflow_nodes = self._load_workflow_nodes(package_dir / "workflow.graph.py")
        tools = [
            ToolManifest.model_validate(item)
            for item in self._read_json(package_dir / "tools.manifest.json")
        ]
        callbacks = CallbackConfig.model_validate(
            self._read_yaml(package_dir / "callbacks.yaml")
        )
        policy = PackagePolicy.model_validate(definition.get("policy", {}))
        self._validate_workflow_policy(workflow_nodes, callbacks, policy)
        prompts = self._validate_prompts(package_dir, definition.get("prompts", []))
        self._validate_node_prompt_refs(workflow_nodes, prompts)
        evals = self._load_evals(package_dir / "evals" / "minimal.jsonl")
        if len(evals) < 5:
            raise AgentPackageValidationError("最小 eval 用例不得少于 5 条")

        package = AgentPackage(
            agent_id=agent_id,
            version=version,
            contract_version=definition["contract_version"],
            status=definition.get("status", "active"),
            allowed_business_types=definition.get("allowed_business_types", []),
            policy=policy,
            workflow_nodes=workflow_nodes,
            tools=tools,
            callbacks=callbacks,
            ui_trace=UiTraceConfig.model_validate(
                self._read_yaml(package_dir / "ui-trace.yaml")
            ),
            input_schema=self._read_json(package_dir / "input.schema.json"),
            output_schema=self._read_json(package_dir / "output.schema.json"),
            prompts=prompts,
            guardrails=self._read_yaml(package_dir / "guardrails.yaml"),
            evals=evals,
            package_digest=digest,
        )
        self._remember_digest(package)
        logging.info(
            "加载 AgentPackage 成功 agent_id=%s version=%s digest=%s status=%s",
            agent_id,
            version,
            digest,
            package.status,
        )
        return package

    @staticmethod
    def validate_lifecycle(status: str, operation: str) -> None:
        """统一生命周期判定，后续 create/start/retry 都必须复用此边界。"""
        if status not in {"active", "deprecated", "revoked"}:
            raise AgentPackageValidationError("Package 生命周期状态非法")
        if status == "revoked" and operation != "status_change":
            raise AgentPackageValidationError("已 revoked 的 Package 不可执行")
        if status == "deprecated" and operation == "create":
            raise AgentPackageValidationError("deprecated Package 不接受新的 Run")

    def change_definition_status(
        self,
        definition: AgentDefinition,
        new_status: str,
        actor_id: str,
        reason: str,
        trace_id: str | None = None,
    ) -> None:
        """更新已注册定义的生命周期并写安全审计；提交事务仍由调用方负责。"""
        self.validate_lifecycle(new_status, operation="status_change")
        if not actor_id or not reason:
            raise AgentPackageValidationError("生命周期变更必须填写操作者和原因")
        from datetime import UTC, datetime
        from uuid import uuid4

        now = datetime.now(UTC)
        definition.status = new_status
        definition.status_changed_at = now
        definition.status_changed_by = actor_id
        definition.status_change_reason = reason
        if new_status == "revoked":
            definition.revoked_at = now
            definition.revocation_reason = reason
        logging.info(
            "变更 AgentPackage 生命周期 agent_id=%s version=%s status=%s actor=%s",
            definition.agent_id,
            definition.version,
            new_status,
            actor_id,
        )
        if self._audit_service is not None:
            from app.schemas.audit import RuntimeAuditEvent

            self._audit_service.append(
                RuntimeAuditEvent(
                    audit_id=str(uuid4()),
                    actor_type="administrator",
                    actor_id=actor_id,
                    action="agent_package_status_changed",
                    resource_type="agent_definition",
                    resource_id=f"{definition.agent_id}@{definition.version}",
                    reason_code=reason,
                    outcome="accepted",
                    occurred_at=now,
                    trace_id=trace_id,
                    metadata_summary={"status": new_status},
                )
            )

    def _validate_required_files(self, package_dir: Path) -> None:
        if not package_dir.is_dir():
            raise AgentPackageValidationError("AgentPackage 目录不存在")
        missing = sorted(
            name for name in self._REQUIRED_FILES if not (package_dir / name).is_file()
        )
        if missing:
            raise AgentPackageValidationError(
                f"AgentPackage 缺少文件: {', '.join(missing)}"
            )

    def _validate_identity(
        self, definition: dict[str, Any], agent_id: str, version: str
    ) -> None:
        if (
            definition.get("agent_id") != agent_id
            or definition.get("version") != version
        ):
            raise AgentPackageValidationError(
                "agent.yaml 的 agent_id/version 必须与目录精确匹配"
            )

    def _validate_workflow_policy(
        self,
        nodes: list[WorkflowNodeDefinition],
        callbacks: CallbackConfig,
        policy: PackagePolicy,
    ) -> None:
        node_ids = {node.node_id for node in nodes}
        if (
            any(node.can_wait_for_human for node in nodes)
            and not callbacks.waiting_human_enabled
        ):
            raise AgentPackageValidationError(
                "人工等待节点必须启用 waiting_human callback"
            )
        if (
            policy.waiting_human_timeout_action == "fallback"
            and policy.waiting_human_fallback_node not in node_ids
        ):
            raise AgentPackageValidationError("waiting_human fallback 节点不存在")
        optional_positions = [index for index, node in enumerate(nodes) if node.optional]
        if optional_positions:
            publish_positions = [
                index for index, node in enumerate(nodes)
                if node.node_id == "publish_document"
            ]
            if len(publish_positions) != 1 or min(optional_positions) <= publish_positions[0]:
                raise AgentPackageValidationError("可选节点必须位于 publish_document 之后")

    def _validate_prompts(self, package_dir: Path, prompts: list[str]) -> list[str]:
        for prompt_ref in prompts:
            if (
                not prompt_ref.endswith(".md")
                or not (package_dir / "prompts" / prompt_ref).is_file()
            ):
                raise AgentPackageValidationError(
                    f"prompt 不存在或未版本化: {prompt_ref}"
                )
        return prompts

    @staticmethod
    def _validate_node_prompt_refs(
        nodes: list[WorkflowNodeDefinition], prompts: list[str]
    ) -> None:
        """模型/护栏节点只能引用 package 中已声明的版本化 Prompt。"""
        declared = set(prompts)
        for node in nodes:
            if (
                node.node_type in {"model", "guardrail"}
                and node.prompt_ref not in declared
            ):
                raise AgentPackageValidationError(
                    f"节点 {node.node_id} 引用了未声明的 prompt"
                )

    def _remember_digest(self, package: AgentPackage) -> None:
        key = (package.agent_id, package.version)
        previous = self._registered_digests.setdefault(key, package.package_digest)
        if previous != package.package_digest:
            raise AgentPackageValidationError(
                "同一 agent_id/version 的 package digest 不可变"
            )

    def _digest(self, package_dir: Path) -> str:
        hasher = hashlib.sha256()
        for path in sorted(path for path in package_dir.rglob("*") if path.is_file()):
            if path.name in self._EXCLUDED_DIGEST_FILES:
                continue
            relative_path = path.relative_to(package_dir).as_posix()
            hasher.update(relative_path.encode())
            hasher.update(b"\0")
            hasher.update(path.read_bytes())
            hasher.update(b"\0")
        return f"sha256:{hasher.hexdigest()}"

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentPackageValidationError(f"JSON 读取失败: {path.name}") from exc

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise AgentPackageValidationError(f"YAML 读取失败: {path.name}") from exc
        if not isinstance(data, dict):
            raise AgentPackageValidationError(f"YAML 根节点必须是对象: {path.name}")
        return data

    @staticmethod
    def _load_workflow_nodes(path: Path) -> list[WorkflowNodeDefinition]:
        """只解析字面量 WORKFLOW_NODES，加载期绝不执行 Package 中的任意 Python。"""
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            raise AgentPackageValidationError("workflow.graph.py 格式无效") from exc
        raw_nodes: Any | None = None
        for statement in tree.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and statement.targets[0].id == "WORKFLOW_NODES"
            ):
                try:
                    raw_nodes = ast.literal_eval(statement.value)
                except ValueError as exc:
                    raise AgentPackageValidationError(
                        "WORKFLOW_NODES 只能使用静态字面量"
                    ) from exc
                break
        if not isinstance(raw_nodes, list):
            raise AgentPackageValidationError(
                "workflow.graph.py 必须导出 WORKFLOW_NODES"
            )
        return [WorkflowNodeDefinition.model_validate(item) for item in raw_nodes]

    @staticmethod
    def _load_evals(path: Path) -> list[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            return [json.loads(line) for line in lines if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentPackageValidationError("evals/minimal.jsonl 格式无效") from exc
