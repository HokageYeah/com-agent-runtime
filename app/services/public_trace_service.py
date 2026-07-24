"""将内部节点进度投影为 Package 声明的公开轨迹。"""

from __future__ import annotations

from app.schemas.agent_package import UiTraceConfig


class PublicTraceService:
    """不读取模型、工具或业务正文；仅投影节点标识和受控标签。"""

    def __init__(self, config: UiTraceConfig) -> None:
        self._config = config

    @classmethod
    def from_snapshot(cls, snapshot: object) -> PublicTraceService:
        """Run 创建时冻结的 ui-trace 配置缺失时保守退化为 status_only。"""
        try:
            config = UiTraceConfig.model_validate(snapshot)
        except (TypeError, ValueError):
            config = UiTraceConfig()
        return cls(config)

    def render(self, value: object) -> list[dict[str, str]]:
        if self._config.mode == "none" or not isinstance(value, list):
            return []
        trace: list[dict[str, str]] = []
        for item in value[:8]:
            if not isinstance(item, dict):
                continue
            step, status = item.get("step"), item.get("status")
            if not isinstance(step, str) or not isinstance(status, str):
                continue
            public_item = {"step": step, "status": status}
            if self._config.mode == "public_summary":
                label = self._config.step_labels.get(step)
                if isinstance(label, str) and label:
                    public_item["label"] = label
            trace.append(public_item)
        return trace
