"""Docker 部署编排的静态合同回归。"""

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def _compose(name: str) -> dict[str, object]:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def _workflow() -> str:
    return (ROOT / ".github/workflows/com-agent-runtime.yml").read_text(
        encoding="utf-8"
    )


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


def test_runtime_production_overlay_gates_launcher_behind_legacy_profile() -> None:
    """production 默认不启动 legacy launcher，只保留应急 profile 入口。"""

    launcher = _compose("docker-compose.production.yml")["services"]["launcher"]
    assert launcher["profiles"] == ["legacy-launcher"]


def test_runtime_deploy_workflow_removes_stale_production_launcher() -> None:
    """production 更新前必须清理上一版本遗留的 launcher 容器。"""

    assert "rm --stop --force launcher" in _workflow()


def test_runtime_deploy_workflow_serializes_and_verifies_complete_runtime() -> None:
    workflow = _workflow()

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
    # api/worker/reconciler 是两环境公共长期 workload，workflow 无条件要求 running。
    for service_name in ("api", "worker", "reconciler"):
        assert f'grep -qx "{service_name}"' in workflow
    # launcher 运行断言按环境分化：test 分支要求 running，production 出现即报错。
    assert '[ "${APP_ENV}" = "test" ]; then' in workflow
    assert 'grep -qx "launcher"' in workflow
    assert "production 不应运行 legacy launcher" in workflow


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
    assert "AGENT_PACKAGE_VERSION=1.0.8" in test_template
    assert "COMPOSE_PROJECT_NAME=com-agent-runtime-production" in production_template
    assert "MEMOIR_INTEGRATION_NETWORK=memoir-integration-production" in production_template
    assert "RUNTIME_API_HOST_PORT=18003" in production_template
    assert "AGENT_PACKAGE_VERSION=1.0.8" in production_template
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


def test_dockerfile_installs_ffmpeg_before_non_root_user() -> None:
    """M8 转码依赖合同：ffmpeg/ffprobe 在切换非 root 前以 apt 安装并清索引。

    依赖必须在镜像构建期安装（Debian ffmpeg 包同时提供 ffmpeg 与 ffprobe，
    与 MEMOIR_AUDIO_FFMPEG_PATH/FFPROBE_PATH 默认值一致），不得运行时联网
    安装，也不得经 ARG/ENV 传入任何密钥。
    """
    dockerfile = (ROOT / "docker/backend/Dockerfile").read_text(encoding="utf-8")

    apt_install = dockerfile.index("apt-get install -y --no-install-recommends ffmpeg")
    apt_cleanup = dockerfile.index("rm -rf /var/lib/apt/lists/*")
    user_runtime = dockerfile.index("USER runtime")
    assert apt_install < apt_cleanup < user_runtime
    # 密钥不进镜像层：构建指令中不出现音频 Key 变量。
    for secret_name in ("MEMOIR_TTS_API_KEY", "MEMORY_AUDIO_SCOPE_HMAC_KEY",
                        "MEMORY_AUDIO_OSS_ACCESS_KEY_SECRET"):
        assert secret_name not in dockerfile


def test_env_templates_carry_m8_audio_placeholders_per_environment() -> None:
    """M8 音频配置落点：两环境模板列全部音频占位，前缀按环境固定且默认关闭。"""
    test_template = (
        ROOT / "docker/backend/test.env.example"
    ).read_text(encoding="utf-8")
    production_template = (
        ROOT / "docker/backend/production.env.example"
    ).read_text(encoding="utf-8")

    for template in (test_template, production_template):
        assert "MEMOIR_AUDIO_ENABLED=false" in template
        for key in (
            "MEMOIR_TTS_API_KEY=",
            "MEMOIR_MUSIC_ACTION=",
            "MEMOIR_AUDIO_COST_CURRENCY=",
            "MEMOIR_TTS_PRICE_PER_1000_TEXT_WORDS=",
            "MEMOIR_MUSIC_PRICE_PER_SECOND=",
            "MEMOIR_AUDIO_MAX_COST_PER_RUN=",
            "MEMORY_AUDIO_OSS_ENDPOINT=",
            "MEMORY_AUDIO_OSS_BUCKET=",
            "MEMORY_AUDIO_OSS_ACCESS_KEY_ID=",
            "MEMORY_AUDIO_OSS_ACCESS_KEY_SECRET=",
            "MEMORY_AUDIO_SCOPE_HMAC_KEY=",
            "MEMOIR_MUSIC_DOWNLOAD_ALLOWED_HOSTS_JSON=",
        ):
            assert key in template, key
    # 两环境四前缀与 ENV_CONFIG 冻结表一致（末尾保留 /）。
    assert "MEMORY_AUDIO_NARRATOR_PREFIX=memoir-test/audios/narrator/" in test_template
    assert "MEMORY_AUDIO_BACKGROUND_PREFIX=memoir-test/audios/background/" in test_template
    assert "MEMORY_AUDIO_NARRATOR_PREFIX=memoir/audios/narrator/" in production_template
    assert "MEMORY_AUDIO_BACKGROUND_PREFIX=memoir/audios/background/" in production_template
    # M8 默认配乐：生成子开关两环境默认关闭；默认源 key 按环境固定且互不混用。
    assert "MEMOIR_MUSIC_GENERATION_ENABLED=false" in test_template
    assert "MEMOIR_MUSIC_GENERATION_ENABLED=false" in production_template
    assert (
        "MEMOIR_DEFAULT_BGM_OBJECT_KEY=memoir-test/audios/default/memoirs.mp3"
        in test_template
    )
    assert (
        "MEMOIR_DEFAULT_BGM_OBJECT_KEY=memoir/audios/default/memoirs.mp3"
        in production_template
    )
    # 生产模板不得使用测试前缀。
    assert "memoir-test/" not in production_template


def test_runtime_compose_keeps_env_file_chain_without_audio_secrets() -> None:
    """音频 Key 只经既有 x-runtime-env-file 注入，不进 Compose build args。"""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "x-runtime-env-file:" in compose
    assert "env_file: *runtime-env-file" in compose
    for secret_name in ("MEMOIR_TTS_API_KEY", "MEMORY_AUDIO_SCOPE_HMAC_KEY",
                        "MEMORY_AUDIO_OSS_ACCESS_KEY_ID",
                        "MEMORY_AUDIO_OSS_ACCESS_KEY_SECRET"):
        assert secret_name not in compose


def test_ffmpeg_build_mirror_and_download_limits() -> None:
    """国内构建源可覆盖，网络失败有界且安装层不依赖业务源码。"""
    dockerfile = (ROOT / "docker/backend/Dockerfile").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "${RUNTIME_APT_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn}" in compose
    assert "ARG APT_MIRROR=https://deb.debian.org" in dockerfile
    assert "/etc/apt/sources.list.d/debian.sources" in dockerfile
    assert 'Acquire::Retries "3"' in dockerfile
    assert 'Acquire::https::Timeout "30"' in dockerfile
    assert "APT::Update::Error-Mode=any" in dockerfile
    assert "ffmpeg -version" in dockerfile and "ffprobe -version" in dockerfile
    assert dockerfile.index("apt-get install") < dockerfile.index("RUN pip install")
    assert dockerfile.index("apt-get install") < dockerfile.index("COPY pyproject.toml")
    assert "--allow-unauthenticated" not in dockerfile


def test_apt_mirror_sed_expression_executes_and_preserves_security_fields() -> None:
    """实际执行 Dockerfile 的 sed 表达式，避免分隔符与正则或运算符冲突。"""
    import shlex
    import subprocess

    dockerfile = (ROOT / "docker/backend/Dockerfile").read_text()
    command = next(line for line in dockerfile.splitlines() if line.startswith("RUN sed "))
    expression = shlex.split(command.removesuffix("\\"))[4]
    source = (
        "URIs: http://deb.debian.org/debian\n"
        "Suites: trixie trixie-updates\n"
        "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
        "URIs: https://security.debian.org/debian-security\n"
        "URIs: http://deb.debian.org/debian-security\n"
        "Suites: trixie-security\n"
    )
    for mirror in ("https://mirrors.tuna.tsinghua.edu.cn", "https://deb.debian.org"):
        result = subprocess.run(
            ["sed", "-E", expression.replace("${APT_MIRROR}", mirror)],
            input=source, text=True, capture_output=True, check=True,
        )
        expected = source.replace("http://deb.debian.org", mirror).replace(
            "https://security.debian.org", mirror
        )
        assert result.stdout == expected
