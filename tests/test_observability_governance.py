from app.api.endpoints.health_api import RuntimeHealth
from app.core.config import Settings
from app.runtime.observability import ExternalExporterPolicy


def test_external_exporter_is_disabled_without_complete_governance() -> None:
    policy = ExternalExporterPolicy(enabled=True, data_classification="internal")

    assert policy.allows_export({"run_id": "run-1"}) is False


def test_external_exporter_rejects_sensitive_payload_even_when_governed() -> None:
    policy = ExternalExporterPolicy(
        enabled=True, data_classification="internal", sampled_fields=("run_id",),
        region="cn", retention_days=7, audit_permission="observability_audit",
        privacy_purge_supported=True,
    )

    assert policy.allows_export({"run_id": "run-1", "prompt": "private"}) is False
    assert policy.allows_export({"run_id": "run-1"}) is True


def test_readiness_rejects_enabled_exporter_with_incomplete_governance() -> None:
    """部署若误开启 exporter，readiness 必须阻止其带病上线。"""
    health = RuntimeHealth(
        Settings(RUNTIME_EXTERNAL_EXPORTER_ENABLED=True),
        database_ready=lambda: (True, {}),
    )

    ready, checks = health.check_ready()

    assert ready is False
    assert checks["external_exporter"] == "invalid"


def test_readiness_accepts_governed_exporter_and_strips_sample_field_spaces() -> None:
    """只有显式治理且仅采样安全字段时才宣告 exporter 可用。"""
    health = RuntimeHealth(
        Settings(
            RUNTIME_EXTERNAL_EXPORTER_ENABLED=True,
            RUNTIME_EXTERNAL_EXPORTER_DATA_CLASSIFICATION="internal",
            RUNTIME_EXTERNAL_EXPORTER_SAMPLED_FIELDS=" run_id ",
            RUNTIME_EXTERNAL_EXPORTER_REGION="cn",
            RUNTIME_EXTERNAL_EXPORTER_RETENTION_DAYS=7,
            RUNTIME_EXTERNAL_EXPORTER_AUDIT_PERMISSION="observability_audit",
            RUNTIME_EXTERNAL_EXPORTER_PRIVACY_PURGE_SUPPORTED=True,
        ),
        database_ready=lambda: (True, {}),
    )

    ready, checks = health.check_ready()

    assert ready is True
    assert checks["external_exporter"] == "governed"


def test_readiness_rejects_missing_persistent_audit_sink() -> None:
    health = RuntimeHealth(Settings(RUNTIME_AUDIT_SINK_CONFIGURED=False), database_ready=lambda: (True, {}))

    ready, checks = health.check_ready()

    assert ready is False
    assert checks["audit_sink"] == "missing"
