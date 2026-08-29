from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "agent-runtime.sh"
REQUIRED_NO_PROXY_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "mysql",
    "redis",
    "runtime-api",
    "couple-diary-backend",
}


def _write_runtime_env(path: Path, environment: str = "test") -> None:
    key_id = "test-v1" if environment == "test" else "production-v1"
    runtime_id = (
        "agent-runtime-test"
        if environment == "test"
        else "agent-runtime-production"
    )
    path.write_text(
        "\n".join(
            (
                f"ENVIRONMENT={environment}",
                f"MEMOIR_INTEGRATION_NETWORK=memoir-integration-{environment}",
                "AGENT_PACKAGE_VERSION=1.0.4",
                f"RUNTIME_ID={runtime_id}",
                "MEMORY_RUNTIME_CLIENT_ID=couple-diary",
                f"MEMORY_RUNTIME_KEY_ID={key_id}",
                f"MEMORY_RUNTIME_SECRET={environment}-shared-hmac-secret-0123456789",
                "MEMOIR_MEDIA_ENABLED=true",
                "BUCKET_NAME=com-agent-runtime",
                "ENDPOINT=https://oss-cn-beijing.aliyuncs.com/",
                "",
            )
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _run_configure(
    runtime_env: Path,
    output_file: Path,
    environment: str = "test",
    *extra_args: str,
    input_text: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "configure-couple-diary",
            environment,
            "--runtime-env",
            str(runtime_env),
            "--output",
            str(output_file),
            *extra_args,
        ],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def _last_value(content: str, key: str) -> str:
    matches = re.findall(rf"^{re.escape(key)}=(.*)$", content, flags=re.MULTILINE)
    assert matches, key
    return matches[-1]


def test_configure_couple_diary_env_creates_private_test_block(
    tmp_path: Path,
) -> None:
    runtime_env = tmp_path / "runtime-test.env"
    output_file = tmp_path / "couple-diary-test.env"
    _write_runtime_env(runtime_env)

    result = _run_configure(runtime_env, output_file)

    assert result.returncode == 0, result.stderr
    content = output_file.read_text(encoding="utf-8")
    assert "#########Runtime联动自动化test创建#########" in content
    assert "MEMOIR_INTEGRATION_NETWORK=memoir-integration-test" in content
    assert "CD_MEMORY_RUNTIME_WORKER_ENABLED=false" in content
    assert "CD_MEMORY_RUNTIME_BASE_URL=http://runtime-api:8002" in content
    assert "CD_MEMORY_RUNTIME_CLIENT_ID=couple-diary" in content
    assert "CD_MEMORY_RUNTIME_KEY_ID=test-v1" in content
    assert (
        "CD_MEMORY_RUNTIME_SECRET=test-shared-hmac-secret-0123456789" in content
    )
    assert "CD_MEMORY_RUNTIME_PACKAGE_ENABLED=false" in content
    assert (
        _last_value(content, "CD_MEMORY_MEDIA_URL_ALLOWED_SUFFIXES")
        == '["com-agent-runtime.oss-cn-beijing.aliyuncs.com"]'
    )
    assert (
        set(_last_value(content, "CD_DOCKER_NO_PROXY").split(","))
        == REQUIRED_NO_PROXY_HOSTS
    )
    assert re.fullmatch(
        r"[0-9a-f]{64}", _last_value(content, "CD_MEMORY_SNAPSHOT_MASTER_KEY")
    )
    assert re.fullmatch(
        r"[0-9a-f]{64}", _last_value(content, "CD_MEMORY_ACCESS_PASSWORD_PEPPER")
    )
    assert stat.S_IMODE(output_file.stat().st_mode) == 0o600
    assert "test-shared-hmac-secret" not in result.stdout + result.stderr


def test_configure_couple_diary_env_reuses_business_secrets_and_backs_up(
    tmp_path: Path,
) -> None:
    runtime_env = tmp_path / "runtime-test.env"
    output_file = tmp_path / "couple-diary-test.env"
    _write_runtime_env(runtime_env)
    original = "\n".join(
        (
            "EXISTING_VALUE=keep-me",
            "CD_DOCKER_NO_PROXY=metadata.internal,runtime-api",
            f"CD_MEMORY_SNAPSHOT_MASTER_KEY={'a' * 64}",
            f"CD_MEMORY_ACCESS_PASSWORD_PEPPER={'b' * 64}",
            "",
        )
    )
    output_file.write_text(original, encoding="utf-8")
    os.chmod(output_file, 0o644)

    result = _run_configure(runtime_env, output_file)

    assert result.returncode == 0, result.stderr
    content = output_file.read_text(encoding="utf-8")
    assert content.startswith(original)
    assert _last_value(content, "CD_MEMORY_SNAPSHOT_MASTER_KEY") == "a" * 64
    assert _last_value(content, "CD_MEMORY_ACCESS_PASSWORD_PEPPER") == "b" * 64
    merged_no_proxy = _last_value(content, "CD_DOCKER_NO_PROXY").split(",")
    assert set(merged_no_proxy) == REQUIRED_NO_PROXY_HOSTS | {"metadata.internal"}
    assert len(merged_no_proxy) == len(set(merged_no_proxy))
    backups = list(tmp_path.glob("couple-diary-test.env.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(output_file.stat().st_mode) == 0o600


def test_configure_couple_diary_env_requires_explicit_activation(
    tmp_path: Path,
) -> None:
    runtime_env = tmp_path / "runtime-test.env"
    output_file = tmp_path / "couple-diary-test.env"
    _write_runtime_env(runtime_env)

    result = _run_configure(runtime_env, output_file, "test", "--activate")

    assert result.returncode == 0, result.stderr
    content = output_file.read_text(encoding="utf-8")
    assert _last_value(content, "CD_MEMORY_RUNTIME_WORKER_ENABLED") == "true"
    assert _last_value(content, "CD_MEMORY_RUNTIME_PACKAGE_ENABLED") == "true"


def test_configure_couple_diary_env_generates_production_business_secrets(
    tmp_path: Path,
) -> None:
    runtime_env = tmp_path / "runtime-production.env"
    output_file = tmp_path / "couple-diary-production.env"
    _write_runtime_env(runtime_env, "production")

    result = _run_configure(runtime_env, output_file, "production")
    assert result.returncode == 0, result.stderr
    content = output_file.read_text(encoding="utf-8")
    snapshot_key = _last_value(content, "CD_MEMORY_SNAPSHOT_MASTER_KEY")
    password_pepper = _last_value(content, "CD_MEMORY_ACCESS_PASSWORD_PEPPER")
    assert re.fullmatch(r"[0-9a-f]{64}", snapshot_key)
    assert re.fullmatch(r"[0-9a-f]{64}", password_pepper)
    assert snapshot_key != password_pepper
    assert (
        set(_last_value(content, "CD_DOCKER_NO_PROXY").split(","))
        == REQUIRED_NO_PROXY_HOSTS
    )
    assert snapshot_key not in result.stdout + result.stderr
    assert password_pepper not in result.stdout + result.stderr


def test_configure_couple_diary_env_rejects_missing_runtime_identity(
    tmp_path: Path,
) -> None:
    runtime_env = tmp_path / "runtime-test.env"
    output_file = tmp_path / "couple-diary-test.env"
    runtime_env.write_text(
        "\n".join(
            (
                "ENVIRONMENT=test",
                "MEMOIR_INTEGRATION_NETWORK=memoir-integration-test",
                "AGENT_PACKAGE_VERSION=1.0.4",
                "RUNTIME_ID=agent-runtime-test",
                "",
            )
        ),
        encoding="utf-8",
    )

    result = _run_configure(runtime_env, output_file)

    assert result.returncode != 0
    assert "MEMORY_RUNTIME_CLIENT_ID" in result.stderr
    assert not output_file.exists()


def test_configure_couple_diary_env_rejects_invalid_oss_endpoint(
    tmp_path: Path,
) -> None:
    runtime_env = tmp_path / "runtime-test.env"
    output_file = tmp_path / "couple-diary-test.env"
    _write_runtime_env(runtime_env)
    content = runtime_env.read_text(encoding="utf-8").replace(
        "ENDPOINT=https://oss-cn-beijing.aliyuncs.com/",
        "ENDPOINT=https://oss-cn-beijing.aliyuncs.com/private-path",
    )
    runtime_env.write_text(content, encoding="utf-8")

    result = _run_configure(runtime_env, output_file)

    assert result.returncode != 0
    assert "ENDPOINT" in result.stderr
    assert not output_file.exists()


def test_configure_couple_diary_env_allows_media_disabled_without_oss(
    tmp_path: Path,
) -> None:
    runtime_env = tmp_path / "runtime-test.env"
    output_file = tmp_path / "couple-diary-test.env"
    _write_runtime_env(runtime_env)
    content = runtime_env.read_text(encoding="utf-8")
    content = content.replace("MEMOIR_MEDIA_ENABLED=true", "MEMOIR_MEDIA_ENABLED=false")
    content = re.sub(r"^(BUCKET_NAME|ENDPOINT)=.*\n", "", content, flags=re.MULTILINE)
    runtime_env.write_text(content, encoding="utf-8")

    result = _run_configure(runtime_env, output_file)

    assert result.returncode == 0, result.stderr
    generated = output_file.read_text(encoding="utf-8")
    assert _last_value(generated, "CD_MEMORY_MEDIA_URL_ALLOWED_SUFFIXES") == "[]"
