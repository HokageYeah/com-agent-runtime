"""中立的 Agent 注册协议；仅描述可信权限，不加载具体 Agent 或 Worker。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.runtime.interfaces import LeaseContext, RunExecutor
    from app.runtime.test_harness import RuntimeDependencies


class ExecutorFactory(Protocol):
    """每次派发按当前事务和 lease 新建执行器，禁止缓存跨 Run 的实例。"""

    def __call__(
        self,
        session: Session,
        run_id: str,
        lease_context: LeaseContext,
        *,
        is_draining: Callable[[], bool],
        dependencies: RuntimeDependencies | None,
    ) -> RunExecutor | None: ...


@dataclass(frozen=True)
class AgentToolProfile:
    """由 Runtime 代码登记的工具信任边界，不能从 Package 或请求构造。"""

    # 固定 connector、method、path、输入槽、副作用与取消语义，Package 只能匹配声明。
    http_tools: Mapping[str, tuple[str, str, str, str, bool, str]]
    # 当前 Agent 可调用的业务工具命名空间，专用查询方法也必须匹配。
    tool_prefix: str
    # 从 Worker 可信上下文提取并校验业务引用，不能读取 Package 的同名输入。
    extract_references: Callable[[Mapping[str, Any]], dict[str, Any]]
    # envelope.business_id 必须绑定的可信业务引用字段。
    business_id_field: str
    # 当前 Agent 自己的包版本到 Tool wire 的允许表，禁止跨 Agent 按版本兜底。
    wire_versions: Mapping[str, str]


@dataclass(frozen=True)
class AgentRegistration:
    """一个 Agent 的代码内可信登记；包和业务请求不能提供加载路径或扩展权限。"""

    # 固定执行身份，同时用于包注册门禁与 Worker 工厂选择。
    agent_id: str
    # 按业务类型隔离的工具权限；身份与业务类型必须联合命中。
    tool_profiles: Mapping[str, AgentToolProfile]
    # 工具可写入的内存业务字段；包级校验据此收紧独立 manifest 的并集权限。
    tool_output_fields: frozenset[str]
    # 无参延迟加载器：只在 Worker 派发时解析工厂，API 导入登记时不加载执行依赖。
    executor_factory_loader: Callable[[], ExecutorFactory]
