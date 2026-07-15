"""业务 connector 注册表；第一版只校验固定 ID，不暴露 endpoint/secret。"""

from __future__ import annotations

from typing import Any


class ConnectorValidationError(ValueError):
    """connector 不存在、已禁用或配置不满足最小安全要求。"""


class ConnectorRegistry:
    def __init__(self, connectors: dict[str, dict[str, Any]]) -> None:
        self._connectors = connectors

    def require_enabled(self, connector_id: str) -> None:
        connector = self._connectors.get(connector_id)
        if connector is None or not bool(connector.get("enabled", False)):
            raise ConnectorValidationError("business connector 不存在或未启用")
