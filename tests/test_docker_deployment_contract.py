"""Docker 部署编排的静态合同回归。"""

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def _compose(name: str) -> dict[str, object]:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def test_runtime_compose_gates_long_lived_workloads_on_package_registration() -> None:
    """Package 未幂等注册时，任何长期进程都不得启动。"""

    services = _compose("docker-compose.yml")["services"]
    assert services["register"]["depends_on"]["prepare"] == {
        "condition": "service_completed_successfully"
    }
    for service_name in ("api", "launcher", "worker", "reconciler"):
        assert services[service_name]["depends_on"]["register"] == {
            "condition": "service_completed_successfully"
        }


def test_runtime_compose_requires_environment_isolation_and_private_integration_network() -> None:
    """test/production 不共用 Compose project，业务仓只走私有网络别名。"""

    compose = _compose("docker-compose.yml")
    api = compose["services"]["api"]
    labels = api["labels"]
    assert "COMPOSE_PROJECT_NAME" in labels["com.agent-runtime.compose-project"]
    assert api["ports"][0].startswith("127.0.0.1:")
    assert api["networks"]["memoir-integration"]["aliases"] == ["runtime-api"]
    assert compose["networks"]["memoir-integration"] == {
        "external": True,
        "name": "${MEMOIR_INTEGRATION_NETWORK:?set MEMOIR_INTEGRATION_NETWORK}",
    }


def test_runtime_compose_rotates_container_logs() -> None:
    """所有 Runtime 容器使用有限大小的 Docker 日志轮转，禁止无限增长。"""

    services = _compose("docker-compose.yml")["services"]
    expected_logging = {
        "driver": "json-file",
        "options": {"max-size": "20m", "max-file": "5", "compress": "true"},
    }
    for service_name in ("prepare", "register", "api", "launcher", "worker", "reconciler"):
        assert services[service_name]["logging"] == expected_logging


def test_runtime_overlays_cover_register_service() -> None:
    test_services = _compose("docker-compose.test.yml")["services"]
    production_services = _compose("docker-compose.production.yml")["services"]

    assert test_services["register"]["environment"]["DB_HOST"] == "mysql"
    assert "networks" not in test_services["prepare"]
    assert "networks" not in test_services["register"]
    assert "register" in production_services
    assert production_services["register"]["environment"]["DB_AUTO_CREATE"] == "false"
    for service_name in ("prepare", "register"):
        assert production_services[service_name]["networks"] == [
            "memoir-integration"
        ]


def test_runtime_deploy_workflow_serializes_and_verifies_complete_runtime() -> None:
    workflow = (ROOT / ".github/workflows/com-agent-runtime.yml").read_text(
        encoding="utf-8"
    )

    assert "concurrency:" in workflow
    assert "flock" in workflow
    assert "docker image prune -f" not in workflow
    assert "/api/v1/runtime/health/live" in workflow
    assert "/api/v1/runtime/health/ready" in workflow
    assert 'export RUNTIME_IMAGE_TAG="${DEPLOY_TAG}"' in workflow
    assert '--env-file "${ENV_FILE}" build api' in workflow
    assert '--env-file "${ENV_FILE}" up -d --no-build' in workflow
    assert 'ps -a prepare register' in workflow
    assert 'logs --tail=200 prepare register' in workflow
    for service_name in ("api", "launcher", "worker", "reconciler"):
        assert f'grep -qx "{service_name}"' in workflow


def test_runtime_server_env_templates_freeze_distinct_test_and_production_identity() -> None:
    test_template = (
        ROOT / "docker/backend/test.env.example"
    ).read_text(encoding="utf-8")
    production_template = (
        ROOT / "docker/backend/production.env.example"
    ).read_text(encoding="utf-8")

    assert "COMPOSE_PROJECT_NAME=com-agent-runtime-test" in test_template
    assert "MEMOIR_INTEGRATION_NETWORK=memoir-integration-test" in test_template
    assert "RUNTIME_API_HOST_PORT=18002" in test_template
    assert "BACKEND_CORS_ORIGINS=http://127.0.0.1:18002" in test_template
    assert "AGENT_PACKAGE_VERSION=1.0.5" in test_template
    assert "COMPOSE_PROJECT_NAME=com-agent-runtime-production" in production_template
    assert "MEMOIR_INTEGRATION_NETWORK=memoir-integration-production" in production_template
    assert "RUNTIME_API_HOST_PORT=18003" in production_template
    assert "AGENT_PACKAGE_VERSION=1.0.5" in production_template
    assert "DB_HOST=couple-diary-mysql" in production_template
    assert (
        "RUNTIME_REDIS_URL=redis://couple-diary-redis:6379/15"
        in production_template
    )
    for template in (test_template, production_template):
        assert "MEMOIR_MEDIA_PROVIDER=" in template
        assert "BUCKET_NAME=" in template
        assert "ENDPOINT=" in template


def test_runtime_host_ports_do_not_change_private_container_contract() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    test_template = (ROOT / "docker/backend/test.env.example").read_text(
        encoding="utf-8"
    )

    assert '127.0.0.1:${RUNTIME_API_HOST_PORT:-18002}:8002' in compose
    assert "MEMORY_RUNTIME_BASE_URL=http://runtime-api:8002" in test_template
