#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# Docker 服务器首次配置不应依赖宿主机已安装 Poetry/Python 依赖。
# 保留统一入口，但把服务器 Docker env 配置交给独立 Bash 脚本。
if [[ "${1:-}" == "configure-docker" ]]; then
  shift
  exec "${SCRIPT_DIR}/docker/backend/configure-runtime-env.sh" "$@"
fi

exec poetry run python -m app.scripts.agent_runtime_cli "$@"
