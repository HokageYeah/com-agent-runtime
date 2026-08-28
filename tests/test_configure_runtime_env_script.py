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


def _last_value(content: str, key: str) -> str:
    matches = re.findall(rf"^{re.escape(key)}=(.*)$", content, flags=re.MULTILINE)
    assert matches, key
    return matches[-1]


def _run_configure(
    output_file: Path, environment: str = "test"
) -> subprocess.CompletedProcess[str]:
    # 普通字段和密钥全部留空，覆盖默认值与安全随机生成分支。
    if environment == "test":
        prompt_answers = "\n" * 14
    else:
        prompt_answers = "\n".join(
            (
                "",  # API port
                "",  # Worker ID
                "",  # Package version
                "",  # Single-server MySQL alias
                "",  # DB port
                "",  # DB user
                "d" * 64,
                "",  # Single-server Redis logical DB
                "h" * 64,
                "A" * 43 + "=",
                "j" * 64,
                "https://runtime.example.com",
                "https://business.example.com",
                "https://runtime.example.com",
                "",  # Model routes
                "",  # Memoir node routes
                "",  # Provider keys
                "",  # Media disabled
            )
        ) + "\n"
    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "configure-docker",
            environment,
            "--output",
            str(output_file),
        ],
        input=prompt_answers,
        text=True,
        capture_output=True,
        check=False,
    )


def test_configure_runtime_env_creates_private_test_block(tmp_path: Path) -> None:
    output_file = tmp_path / "runtime-test.env"

    result = _run_configure(output_file)

    assert result.returncode == 0, result.stderr
    content = output_file.read_text(encoding="utf-8")
    assert "#########自动化test创建#########" in content
    assert "COMPOSE_PROJECT_NAME=com-agent-runtime-test" in content
    assert f"RUNTIME_ENV_FILE={output_file}" in content
    assert "RUNTIME_API_HOST_PORT=18002" in content
    assert "DB_NAME=couple_diary_agent_runtime_test" in content
    assert "RUNTIME_REDIS_URL=redis://redis:6379/14" in content
    assert "MEMORY_RUNTIME_BASE_URL=http://runtime-api:8002" in content
    assert set(_last_value(content, "NO_PROXY").split(",")) == REQUIRED_NO_PROXY_HOSTS
    assert _last_value(content, "no_proxy") == _last_value(content, "NO_PROXY")
    assert "change_me" not in content
    assert stat.S_IMODE(output_file.stat().st_mode) == 0o600
    assert "MEMORY_RUNTIME_SECRET=" not in result.stdout


def test_configure_runtime_env_appends_and_backs_up_existing_file(
    tmp_path: Path,
) -> None:
    output_file = tmp_path / "runtime-test.env"
    original = (
        "EXISTING_VALUE=keep-me\n"
        "NO_PROXY=metadata.internal,runtime-api\n"
        "no_proxy=legacy.internal\n"
    )
    output_file.write_text(original, encoding="utf-8")
    os.chmod(output_file, 0o644)

    result = _run_configure(output_file)

    assert result.returncode == 0, result.stderr
    assert output_file.read_text(encoding="utf-8").startswith(original)
    merged_no_proxy = _last_value(
        output_file.read_text(encoding="utf-8"), "NO_PROXY"
    ).split(",")
    assert set(merged_no_proxy) == REQUIRED_NO_PROXY_HOSTS | {
        "metadata.internal",
        "legacy.internal",
    }
    assert len(merged_no_proxy) == len(set(merged_no_proxy))
    backups = list(tmp_path.glob("runtime-test.env.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(output_file.stat().st_mode) == 0o600


def test_configure_runtime_env_creates_fail_closed_production_block(
    tmp_path: Path,
) -> None:
    output_file = tmp_path / "runtime-production.env"

    result = _run_configure(output_file, "production")

    assert result.returncode == 0, result.stderr
    content = output_file.read_text(encoding="utf-8")
    assert "#########自动化production创建#########" in content
    assert "COMPOSE_PROJECT_NAME=com-agent-runtime-production" in content
    assert "RUNTIME_API_HOST_PORT=18003" in content
    assert "DB_AUTO_CREATE=false" in content
    assert "DB_HOST=couple-diary-mysql" in content
    assert "DB_NAME=couple_diary_agent_runtime_prod" in content
    assert "RUNTIME_REDIS_URL=redis://couple-diary-redis:6379/15" in content
    assert "RUNTIME_MYSQL_ROOT_PASSWORD=" not in content
    assert "RUNTIME_TOOL_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS=false" in content
    assert "MEMORY_RUNTIME_KEY_ID=production-v1" in content
    assert "MEMORY_RUNTIME_BASE_URL=https://runtime.example.com" in content
    assert set(_last_value(content, "NO_PROXY").split(",")) == REQUIRED_NO_PROXY_HOSTS
    assert _last_value(content, "no_proxy") == _last_value(content, "NO_PROXY")
    assert "change_me" not in content
    assert stat.S_IMODE(output_file.stat().st_mode) == 0o600
    assert "MODEL_PROVIDER_API_KEYS_JSON=" not in result.stdout


def test_configure_runtime_env_rejects_empty_production_database_password(
    tmp_path: Path,
) -> None:
    output_file = tmp_path / "runtime-production.env"
    # 单机网络地址使用默认值，production 数据库密码仍必须显式输入。
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "configure-docker",
            "production",
            "--output",
            str(output_file),
        ],
        input="\n" * 7,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert not output_file.exists()
    assert "Runtime production 数据库密码" in result.stderr
    assert "不能为空" in result.stderr


def test_configure_runtime_env_writes_enabled_media_oss_config(
    tmp_path: Path,
) -> None:
    output_file = tmp_path / "runtime-test.env"
    answers = [
        "",  # API port
        "",  # Worker ID
        "",  # Package version
        "",  # DB user
        "",  # DB password
        "",  # MySQL root password
        "",  # HMAC
        "",  # Fernet
        "",  # JWT
        "",  # Business origin
        "",  # Model routes
        "",  # Memoir node routes
        "",  # Provider keys
        "true",
        "",  # Media provider=mock
        "",  # Bucket=com-agent-runtime
        "",  # Region=cn-beijing
        "",  # Endpoint=oss-cn-beijing.aliyuncs.com
        "test-oss-access-key",
        "test-oss-secret-key",
    ]
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "configure-docker",
            "test",
            "--output",
            str(output_file),
        ],
        input="\n".join(answers) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    content = output_file.read_text(encoding="utf-8")
    assert _last_value(content, "MEMOIR_MEDIA_ENABLED") == "true"
    assert _last_value(content, "MEMOIR_MEDIA_PROVIDER") == "mock"
    assert _last_value(content, "BUCKET_NAME") == "com-agent-runtime"
    assert _last_value(content, "REGION") == "cn-beijing"
    assert _last_value(content, "ENDPOINT") == "oss-cn-beijing.aliyuncs.com"
    assert _last_value(content, "ACCESS_KEY_ID") == "test-oss-access-key"
    assert _last_value(content, "ACCESS_KEY_SECRET") == "test-oss-secret-key"
    assert "test-oss-access-key" not in result.stdout + result.stderr
    assert "test-oss-secret-key" not in result.stdout + result.stderr
