"""Runtime 服务身份授权：只信任部署配置，绝不接受业务请求覆盖控制字段。"""

from __future__ import annotations

import logging
from typing import Any


class AuthorizationError(ValueError):
    """调用方不存在或不具备目标 Agent/数据域权限。"""


class AuthorizationService:
    """从已验签的 client_id 推导租户并校验资源 allowlist。"""

    def __init__(self, clients: dict[str, dict[str, Any]]) -> None:
        self._clients = clients

    def tenant_for(self, client_id: str) -> str:
        client = self._require_client(client_id)
        tenant_id = client.get("tenant_id", client_id)
        if not isinstance(tenant_id, str) or not tenant_id:
            raise AuthorizationError("tenant 配置非法")
        return tenant_id

    def authorize_create(
        self,
        *,
        client_id: str,
        agent_id: str,
        business_type: str,
        callback_target_id: str,
        connector_id: str,
        data_domain: str,
    ) -> None:
        client = self._require_client(client_id)
        self._require_allowed(client, "agent_ids", agent_id, "agent")
        self._require_allowed(client, "business_types", business_type, "business_type")
        self._require_allowed(
            client, "callback_target_ids", callback_target_id, "callback"
        )
        self._require_allowed(client, "connector_ids", connector_id, "connector")
        self._require_allowed(client, "data_domains", data_domain, "data domain")
        logging.info(
            "Runtime 授权通过 agent_id=%s business_type=%s",
            agent_id,
            business_type,
        )

    def can_audit(self, client_id: str) -> bool:
        client = self._require_client(client_id)
        return bool(client.get("internal_auditor", False))

    def authorize_callback_target(self, client_id: str, callback_target_id: str) -> None:
        """投递前复核调用方当前 callback allowlist，撤销后不得出站。"""
        self._require_allowed(
            self._require_client(client_id), "callback_target_ids", callback_target_id, "callback"
        )

    def authorization_version(self, client_id: str) -> int:
        """读取部署受控的授权版本；缺省仅兼容既有配置并冻结为 1。"""
        value = self._require_client(client_id).get("authorization_version", 1)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise AuthorizationError("authorization_version 配置非法")
        return value

    def model_data_residency(self, client_id: str) -> str | None:
        """读取租户部署侧驻留要求；业务请求和 Package 不能声明或覆盖。"""
        value = self._require_client(client_id).get("model_data_residency")
        if value is None:
            return None
        if value not in {"public", "private"}:
            raise AuthorizationError("model_data_residency 配置非法")
        return value

    def _require_client(self, client_id: str) -> dict[str, Any]:
        client = self._clients.get(client_id)
        if client is None:
            raise AuthorizationError("未知 Runtime 调用方")
        return client

    @staticmethod
    def _require_allowed(
        client: dict[str, Any], field: str, value: str, label: str
    ) -> None:
        configured = client.get(field)
        # 未设置 allowlist 的旧本地配置保持兼容；生产部署应固定完整清单。
        if configured is None:
            return
        if not isinstance(configured, list) or value not in configured:
            raise AuthorizationError(f"{label} 未获调用方授权")
