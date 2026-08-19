"""Runtime Worker 入口：通过持久 outbox 唤醒，再由数据库 lease 决定唯一执行者。"""

from __future__ import annotations

import argparse
import inspect
import logging
import signal
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Event
from time import sleep
from typing import cast
from uuid import uuid4

try:
    from redis import Redis
except ImportError:  # 部署未安装 Redis 客户端时模型能力必须显式关闭。
    Redis = None  # type: ignore[assignment,misc]

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.core.authorization import AuthorizationError, AuthorizationService
from app.core.config import settings
from app.core.logging_uru import setup_logging, shutdown_logging
from app.db.sqlalchemy_db import database
from app.dispatcher import Dispatcher
from app.models import AgentDefinition, AgentRun
from app.runtime.artifact import ArtifactStore
from app.runtime.callback_gateway import CallbackGateway, CallbackTarget
from app.runtime.checkpoint import CheckpointStore, FernetCheckpointCipher
from app.runtime.executor import WorkflowExecutor
from app.runtime.interfaces import AgentRunResult, LeaseContext, RunExecutor
from app.runtime.memoir_model_gateway import MemoirModelGatewayAdapter
from app.runtime.model_gateway import (
    HttpProviderAdapter,
    ModelCallContext,
    ModelCallGuard,
    ModelCapabilityEvaluator,
    ModelGateway,
    ModelPolicyRegistry,
    ModelRouteRegistry,
    ProviderTrafficController,
)
from app.runtime.peer_tracking_transport import PeerTrackingHTTPTransport
from app.runtime.policy_engine import PolicyEngine
from app.runtime.test_harness import LoopbackTestTransport, RuntimeDependencies
from app.runtime.tool_gateway import BusinessConnector, ToolGateway
from app.schemas.audit import (
    AUTHORIZATION_REVOKED,
    AUTHORIZATION_VERSION_CHANGED,
    CALLBACK_TARGET_MISSING,
    CONNECTOR_DISABLED,
    RuntimeAuditEvent,
)
from app.services.audit_service import AuditService
from app.services.callback_delivery_service import (
    CallbackDeliveryService,
    CallbackSender,
)
from app.services.evaluation_service import EvaluationService
from app.services.lease_service import LeaseService
from app.services.model_usage_service import ModelUsageService
from app.services.run_queue_service import RunQueueService
from app.services.tool_call_audit_service import ToolCallAuditService
from app.services.traffic_event_service import SqlAlchemyTrafficEventRecorder


class WorkerDrainController:
    """将进程终止信号转为安全 draining，不中断正在返回的同步节点。"""

    def __init__(self) -> None:
        self._requested = Event()

    def request(self) -> None:
        self._requested.set()

    def is_draining(self) -> bool:
        return self._requested.is_set()


def install_drain_signal_handlers(controller: WorkerDrainController) -> None:
    """SIGTERM/SIGINT 只请求 draining；当前节点仍会完成 heartbeat/checkpoint 边界。"""

    def request_drain(signal_number: int, _frame: object) -> None:
        controller.request()
        logging.warning("Worker 收到终止信号，开始 draining signal=%s", signal_number)

    signal.signal(signal.SIGTERM, request_drain)
    signal.signal(signal.SIGINT, request_drain)


class WorkerLoop:
    """可注入执行器的 Worker 循环；Task 6 只需替换 RunExecutor 实现。"""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        executor: RunExecutor | Callable[[Session], RunExecutor],
        *,
        worker_id: str,
        is_draining: Callable[[], bool] = lambda: False,
        callback_gateway: CallbackSender | None = None,
        trusted_clients: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._executor = executor
        self._worker_id = worker_id
        self._is_draining = is_draining
        self._callback_gateway = callback_gateway
        self._trusted_clients = trusted_clients

    def run_once(self) -> int:
        """派发持久 outbox、消费本批通知并回收超时 lease。"""
        run_ids: list[str] = []
        dispatcher_session = self._session_factory()
        try:
            callback_sender = (
                CallbackDeliveryService(
                    dispatcher_session,
                    self._callback_gateway,
                    authorize_target=self._callback_target_authorized,
                ).send
                if self._callback_gateway is not None
                else None
            )
            Dispatcher(
                dispatcher_session,
                owner=f"dispatcher:{self._worker_id}",
                notify_run=run_ids.append,
                callback_sender=callback_sender,
            ).dispatch_pending()
            LeaseService(dispatcher_session).reap_expired()
        finally:
            dispatcher_session.close()

        consumed = 0
        for run_id in run_ids:
            session = self._session_factory()
            try:
                executor = self._executor
                if callable(executor):
                    parameters = inspect.signature(executor).parameters
                    executor_factory = cast(Callable[..., RunExecutor], executor)
                    executor = (
                        executor_factory(session, is_draining=self._is_draining)
                        if "is_draining" in parameters
                        else executor_factory(session)
                    )
                queue = RunQueueService(
                    session,
                    executor,
                    self._worker_id,
                    is_draining=self._is_draining,
                )
                consumed += int(queue.consume(run_id))
            finally:
                session.close()
        logging.info(
            "Worker 本轮完成 worker_id=%s claimed=%s", self._worker_id, consumed
        )
        return consumed

    def _callback_target_authorized(self, run: AgentRun) -> str | None:
        """每次发送前以当前部署配置复核 target；不记录 callback body。"""
        if not isinstance(self._callback_gateway, CallbackGateway):
            return None
        if not self._callback_gateway.has_target(run.callback_target_id):
            return CALLBACK_TARGET_MISSING
        try:
            authorization = AuthorizationService(
                self._trusted_clients or settings.trusted_clients
            )
            authorization.authorize_callback_target(
                run.caller_id, run.callback_target_id
            )
            if (
                authorization.authorization_version(run.caller_id)
                != run.authorization_version
            ):
                return AUTHORIZATION_VERSION_CHANGED
        except AuthorizationError:
            return AUTHORIZATION_REVOKED
        return None


class BootstrapExecutor:
    """Task 6 尚未部署时的安全占位执行器，不读取输入、不调用模型或业务工具。"""

    def run(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        logging.warning(
            "WorkflowExecutor 尚未部署，Worker 仅完成 lease 验证 run_id=%s attempt=%s",
            run_id,
            lease_context.execution_attempt,
        )
        return AgentRunResult(
            run_id=run_id,
            status="pending",
            execution_attempt=lease_context.execution_attempt,
            error_code="WORKFLOW_EXECUTOR_NOT_CONFIGURED",
        )

    def resume(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        return self.run(run_id, lease_context)


_MEMOIR_MODEL_NODES = frozenset(
    {"extract_highlights", "plan_chapters", "generate_scenes"}
)


def configured_model_gateway(
    session: Session,
    *,
    is_draining: Callable[[], bool] = lambda: False,
    dependencies: RuntimeDependencies | None = None,
) -> MemoirModelGatewayAdapter | None:
    """只从受信任 Settings 装配模型边界；任何缺项都禁用模型能力。"""
    try:
        runtime_settings = (
            dependencies.settings if dependencies is not None else settings
        )
        routes = runtime_settings.model_routes
        node_routes = runtime_settings.memoir_model_node_routes
    except ValueError:
        logging.exception("Memoir Worker 模型配置无效，使用模板 fallback")
        return None
    if (
        Redis is None
        or not isinstance(runtime_settings.RUNTIME_REDIS_URL, str)
        or not runtime_settings.RUNTIME_REDIS_URL
        or set(node_routes) != _MEMOIR_MODEL_NODES
        or any(
            route_id not in {route.route_id for route in routes}
            for route_id in node_routes.values()
        )
    ):
        logging.warning("Memoir Worker 模型配置不完整，使用模板 fallback")
        return None
    try:
        redis = Redis.from_url(runtime_settings.RUNTIME_REDIS_URL)
    except Exception:
        logging.exception("Memoir Worker Redis 初始化失败，使用模板 fallback")
        return None

    class _WorkerDrainingGuard(ModelCallGuard):
        """每次检查实时读取 Worker 和权威授权状态，不能把状态写入 Run。"""

        def permits_new_call(self, context: ModelCallContext) -> bool:
            return (
                not is_draining()
                and _authorization_is_current(
                    session,
                    context.run_id,
                    trusted_clients=runtime_settings.trusted_clients,
                )
                and _package_permitted(session, context.run_id)
            )

    # Provider 每次请求都使用无 keep-alive 的真实 socket 对端追踪，防止 DNS rebinding。
    provider_peer_transport = (
        PeerTrackingHTTPTransport() if dependencies is None else None
    )
    model_policies = ModelPolicyRegistry.default()
    traffic_recorder = SqlAlchemyTrafficEventRecorder(
        session, audit_service=AuditService(session=session),
    )
    gateway = ModelGateway(
        ModelRouteRegistry(routes),
        ProviderTrafficController(redis, recorder=traffic_recorder),
        ModelUsageService(session),
        LeaseService(session),
        dependencies.provider_adapter
        if dependencies is not None and dependencies.provider_adapter is not None
        else HttpProviderAdapter(
            httpx.Client(transport=provider_peer_transport, trust_env=False),
            peer_ip_provider=provider_peer_transport.peer_ip
            if provider_peer_transport
            else None,
            reset_peer_ip=provider_peer_transport.reset_peer_ip
            if provider_peer_transport
            else None,
        ),
        PolicyEngine(session),
        call_guard=_WorkerDrainingGuard(),
        model_policies=model_policies,
        capability_evaluator=ModelCapabilityEvaluator(model_policies),
        traffic_event_recorder=traffic_recorder,
    )
    return MemoirModelGatewayAdapter(session, gateway, node_routes)


def configured_executor(
    session: Session,
    *,
    is_draining: Callable[[], bool] = lambda: False,
    dependencies: RuntimeDependencies | None = None,
) -> RunExecutor:
    """按 Worker 当前事务装配 Memoir 执行器，配置不完整时安全退回占位器。"""

    class _Executor:
        @staticmethod
        def _memoir_executor(
            run_id: str, lease_context: LeaseContext
        ) -> WorkflowExecutor | None:
            runtime_settings = (
                dependencies.settings if dependencies is not None else settings
            )
            agent_run = session.scalar(
                select(AgentRun).where(AgentRun.run_id == run_id)
            )
            # Run 的版本由创建时冻结；Worker 只按 Agent 身份选择执行器，绝不把
            # 新版本或历史版本重写为默认版本。Package 可用性由 Executor 和发包守卫复核。
            if agent_run is None or agent_run.agent_id != "memoir_agent":
                return None
            config = runtime_settings.business_connectors.get(
                agent_run.business_connector_id, {}
            )
            required = ("base_url", "runtime_id", "key_id", "secret")
            if not bool(config.get("enabled")) or any(
                not isinstance(config.get(key), str) for key in required
            ):
                logging.error("Memoir Worker connector 配置不完整 run_id=%s", run_id)
                AuditService(session=session).append(
                    RuntimeAuditEvent(
                        audit_id=str(uuid4()), actor_type="system",
                        actor_id="tool_gateway", action="tool_gateway_rejected",
                        resource_type="agent_run", resource_id=run_id,
                        reason_code=CONNECTOR_DISABLED, outcome="rejected",
                        occurred_at=datetime.now(UTC), trace_id=agent_run.trace_id,
                        metadata_summary={"run_id": run_id},
                    )
                )
                return None
            connector = BusinessConnector(
                base_url=cast(str, config["base_url"]),
                runtime_id=cast(str, config["runtime_id"]),
                key_id=cast(str, config["key_id"]),
                secret=cast(str, config["secret"]),
            )
            # ToolGateway 每次发送前读取同一实时 draining 回调，不能在装配时冻结状态。
            peer_transport = (
                PeerTrackingHTTPTransport() if dependencies is None else None
            )
            tool_gateway = ToolGateway(
                {agent_run.business_connector_id: connector},
                dependencies.tool_client
                if dependencies is not None
                else httpx.Client(transport=peer_transport, trust_env=False),
                is_draining=is_draining,
                deadline_at=lambda: agent_run.run_deadline_at,
                lease_expires_at=lambda: lease_context.lease_expires_at,
                authorization_permitted=lambda checked_run_id: (
                    _authorization_rejection_reason(
                        session,
                        checked_run_id,
                        trusted_clients=runtime_settings.trusted_clients,
                    )
                ),
                execution_permitted=lambda checked_run_id: _tool_execution_permitted(
                    session, checked_run_id, lease_context
                ),
                audit_rejection=lambda checked_run_id, code: AuditService(
                    session=session
                ).append(
                    RuntimeAuditEvent(
                        audit_id=str(uuid4()), actor_type="system",
                        actor_id="tool_gateway", action="tool_gateway_rejected",
                        resource_type="agent_run", resource_id=checked_run_id,
                        reason_code=code, outcome="rejected",
                        occurred_at=datetime.now(UTC),
                        metadata_summary={"run_id": checked_run_id},
                    )
                ),
                peer_ip_provider=peer_transport.peer_ip
                if peer_transport is not None
                else None,
                reset_peer_ip=peer_transport.reset_peer_ip
                if peer_transport is not None
                else None,
                test_transport=(
                    dependencies.transport_verifier
                    if dependencies is not None
                    and isinstance(
                        dependencies.transport_verifier, LoopbackTestTransport
                    )
                    else None
                ),
                # 开发联调逃生门：connector 指向本机业务后端（127.0.0.1:8008）时
                # 由运维配置显式放行；生产环境 config validator 强制为 False。
                allow_private_endpoints=(
                    runtime_settings.RUNTIME_TOOL_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS
                ),
            )
            model_gateway = configured_model_gateway(
                session, is_draining=is_draining, dependencies=dependencies
            )
            if model_gateway is not None:
                model_gateway.bind_lease(lease_context)
            return WorkflowExecutor(
                session,
                MemoirNodeRunner(
                    tool_gateway,
                    ToolCallAuditService(session),
                    model_gateway,
                    EvaluationService(session),
                ),
                CheckpointStore(
                    session,
                    FernetCheckpointCipher(
                        runtime_settings.MEMORY_SNAPSHOT_FERNET_KEY.encode()
                    ),
                ),
                ArtifactStore(session),
                authorization_version_resolver=lambda run: AuthorizationService(
                    runtime_settings.trusted_clients
                ).authorization_version(run.caller_id),
                is_draining=is_draining,
            )

        def run(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
            executor = self._memoir_executor(run_id, lease_context)
            return (
                BootstrapExecutor().run(run_id, lease_context)
                if executor is None
                else executor.run(run_id, lease_context)
            )

        def resume(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
            executor = self._memoir_executor(run_id, lease_context)
            return (
                BootstrapExecutor().resume(run_id, lease_context)
                if executor is None
                else executor.resume(run_id, lease_context)
            )

    return _Executor()


def _tool_execution_permitted(
    session: Session, run_id: str, lease_context: LeaseContext
) -> bool:
    """工具发包前的统一无内容边界，拒绝迟到 Worker 继续产生副作用。"""
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    if run is None or not LeaseService(session).can_write(run_id, lease_context):
        return False
    return _package_permitted(session, run_id)


def _package_permitted(session: Session, run_id: str) -> bool:
    """只返回 Package 生命周期结论，供模型与工具发送前复用。"""
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    if run is None:
        return False
    definition = session.scalar(
        select(AgentDefinition).where(
            AgentDefinition.agent_id == run.agent_id,
            AgentDefinition.version == run.agent_version,
        )
    )
    return (
        definition is not None
        and definition.status == "active"
        and definition.package_digest == run.package_digest
    )


def _authorization_is_current(
    session: Session,
    run_id: str,
    *,
    trusted_clients: dict[str, dict[str, object]] | None = None,
) -> bool:
    """只按权威 Run 冻结版本与部署授权配置决定是否允许外部副作用。"""
    return (
        _authorization_rejection_reason(
            session, run_id, trusted_clients=trusted_clients
        )
        is None
    )


def _authorization_rejection_reason(
    session: Session,
    run_id: str,
    *,
    trusted_clients: dict[str, dict[str, object]] | None = None,
) -> str | None:
    """区分授权撤销与版本漂移，同时不向审计暴露配置内容。"""
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    if run is None:
        return AUTHORIZATION_REVOKED
    try:
        current = AuthorizationService(
            trusted_clients or settings.trusted_clients
        ).authorization_version(run.caller_id)
    except AuthorizationError:
        return AUTHORIZATION_REVOKED
    return None if current == run.authorization_version else AUTHORIZATION_VERSION_CHANGED


def configured_callback_gateway(
    dependencies: RuntimeDependencies | None = None,
) -> CallbackGateway | None:
    """按部署白名单装配 callback 网关；非法或关闭配置不启用 callback 消费。"""
    targets: dict[str, CallbackTarget] = {}
    runtime_settings = dependencies.settings if dependencies is not None else settings
    for target_id, config in runtime_settings.callback_targets.items():
        required = ("url", "runtime_id", "key_id", "secret")
        if bool(config.get("enabled")) and all(
            isinstance(config.get(key), str) for key in required
        ):
            targets[target_id] = CallbackTarget(
                url=cast(str, config["url"]),
                runtime_id=cast(str, config["runtime_id"]),
                key_id=cast(str, config["key_id"]),
                secret=cast(str, config["secret"]),
            )
        else:
            logging.warning(
                "callback target 未启用或配置不完整 target_id=%s", target_id
            )
    client = (
        dependencies.callback_client if dependencies is not None else httpx.Client()
    )
    test_transport = (
        dependencies.transport_verifier
        if dependencies is not None
        and isinstance(dependencies.transport_verifier, LoopbackTestTransport)
        else None
    )
    return (
        CallbackGateway(targets, client, test_transport=test_transport)
        if targets
        else None
    )


def main() -> None:
    """命令行启动入口；生产由 Task 6 注入 WorkflowExecutor。"""
    parser = argparse.ArgumentParser(description="Start the AgentRuntime worker")
    parser.add_argument(
        "--once", action="store_true", help="只处理一轮持久 outbox 后退出"
    )
    parser.add_argument("--worker-id", default="agent-runtime-worker")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    setup_logging()
    database.connect()
    try:
        session_factory = database.get_session_factory()
        drain_controller = WorkerDrainController()
        install_drain_signal_handlers(drain_controller)
        loop = WorkerLoop(
            session_factory,
            configured_executor,
            worker_id=args.worker_id,
            is_draining=drain_controller.is_draining,
            callback_gateway=configured_callback_gateway(),
        )
        logging.info(
            "AgentRuntime Worker 启动 runtime_id=%s queue=%s once=%s",
            settings.runtime_id,
            "root-database-outbox",
            args.once,
        )
        if args.once:
            loop.run_once()
            return
        while True:
            loop.run_once()
            # 信号处理器不会抛异常中断当前节点；RunQueue/Executor 已在安全边界
            # heartbeat、checkpoint 后停止后续模型/工具调用并释放 lease 给 reaper。
            if drain_controller.is_draining():
                return
            sleep(max(args.poll_seconds, 0.1))
    finally:
        database.close()
        shutdown_logging()


if __name__ == "__main__":
    main()
