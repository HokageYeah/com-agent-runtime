"""公开轨迹只能来自 Package 的冻结 ui-trace 配置。"""

from app.schemas.agent_package import UiTraceConfig
from app.services.public_trace_service import PublicTraceService


def test_public_trace_service_uses_only_safe_label_from_public_summary_config() -> None:
    trace = PublicTraceService(UiTraceConfig(mode="public_summary", step_labels={"generate": "生成中"})).render(
        [{"step": "generate", "status": "succeeded", "prompt": "private"}]
    )

    assert trace == [{"step": "generate", "status": "succeeded", "label": "生成中"}]


def test_public_trace_service_hides_all_trace_for_none_mode() -> None:
    assert PublicTraceService(UiTraceConfig(mode="none")).render([{"step": "generate", "status": "succeeded"}]) == []
