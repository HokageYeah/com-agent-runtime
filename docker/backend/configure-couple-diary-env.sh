#!/bin/bash
set -euo pipefail

readonly DEFAULT_ENV_DIR="/usr/HokageYeah/服务端系统/env"

usage() {
  cat <<'EOF'
用法：
  ./agent-runtime.sh configure-couple-diary test
  ./agent-runtime.sh configure-couple-diary production
  ./agent-runtime.sh configure-couple-diary <test|production> --activate
  ./agent-runtime.sh configure-couple-diary <test|production> \
    --runtime-env /absolute/path/runtime.env \
    --output /absolute/path/couple-diary.env

说明：
  - 默认从同目录 runtime-<environment>.env 读取 Runtime 共享身份。
  - 默认追加到 couple-diary-<environment>.env，追加前创建 0600 备份。
  - 默认保持 Runtime Worker/Package 门禁关闭；只有 --activate 才开启。
  - test 缺失业务 Snapshot key/pepper 时自动生成；production 必须隐藏输入。
  - 不执行（source）env 文件，不回显任何密钥。
EOF
}

fail() {
  printf '错误：%s\n' "$1" >&2
  exit 1
}

last_env_value() {
  local file="$1" key="$2"
  awk -v key="${key}" '
    index($0, key "=") == 1 { value = substr($0, length(key) + 2) }
    END { sub(/\r$/, "", value); printf "%s", value }
  ' "${file}"
}

required_runtime_value() {
  local variable_name="$1" key="$2" value
  value="$(last_env_value "${runtime_env_file}" "${key}")"
  [[ -n "${value}" ]] || fail "Runtime env 缺少非空 ${key}"
  [[ "${value}" != *'${'* && "${value}" != *change_me* ]] \
    || fail "Runtime env 中 ${key} 仍是未展开值或占位符"
  [[ "${value}" != *$'\r'* && "${value}" != *$'\n'* ]] \
    || fail "Runtime env 中 ${key} 不能包含换行"
  printf -v "${variable_name}" '%s' "${value}"
}

prompt_required_hidden() {
  local variable_name="$1" label="$2" input_value
  read -r -s -p "${label}（必填，输入不回显）: " input_value || true
  printf '\n'
  [[ -n "${input_value}" ]] || fail "${label} 不能为空"
  printf -v "${variable_name}" '%s' "${input_value}"
}

validate_safe_token() {
  local label="$1" value="$2"
  [[ "${value}" =~ ^[A-Za-z0-9._~@%+=:,/-]+$ ]] \
    || fail "${label} 只能包含安全 token 字符"
}

validate_hex_key() {
  local label="$1" value="$2"
  [[ "${value}" =~ ^[0-9a-fA-F]{64}$ ]] || fail "${label} 必须是 32 字节 hex（64 字符）"
}

environment="${1:-}"
case "${environment}" in
  test|production) ;;
  *) usage; fail "环境只能是 test 或 production" ;;
esac
shift

runtime_env_file="${DEFAULT_ENV_DIR}/runtime-${environment}.env"
output_file="${DEFAULT_ENV_DIR}/couple-diary-${environment}.env"
activate="false"
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --runtime-env)
      [[ "$#" -ge 2 ]] || fail "--runtime-env 缺少绝对路径"
      runtime_env_file="$2"
      shift 2
      ;;
    --output)
      [[ "$#" -ge 2 ]] || fail "--output 缺少绝对路径"
      output_file="$2"
      shift 2
      ;;
    --activate)
      activate="true"
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage; fail "未知参数: $1" ;;
  esac
done

[[ "${runtime_env_file}" == /* ]] || fail "--runtime-env 必须是绝对路径"
[[ "${output_file}" == /* ]] || fail "--output 必须是绝对路径"
[[ "${runtime_env_file}" != "${output_file}" ]] || fail "Runtime env 与目标文件不能相同"
[[ -f "${runtime_env_file}" && ! -L "${runtime_env_file}" ]] \
  || fail "Runtime env 不存在、不是普通文件或是符号链接: ${runtime_env_file}"
[[ ! -L "${output_file}" ]] || fail "拒绝写入符号链接: ${output_file}"
if [[ -e "${output_file}" && ! -f "${output_file}" ]]; then
  fail "目标已存在但不是普通文件: ${output_file}"
fi
command -v openssl >/dev/null 2>&1 || fail "服务器缺少 openssl"

required_runtime_value runtime_environment "ENVIRONMENT"
[[ "${runtime_environment}" == "${environment}" ]] \
  || fail "Runtime env 环境 ${runtime_environment} 与目标 ${environment} 不一致"
required_runtime_value integration_network "MEMOIR_INTEGRATION_NETWORK"
[[ "${integration_network}" == "memoir-integration-${environment}" ]] \
  || fail "MEMOIR_INTEGRATION_NETWORK 必须是 memoir-integration-${environment}"
required_runtime_value agent_package_version "AGENT_PACKAGE_VERSION"
[[ "${agent_package_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || fail "AGENT_PACKAGE_VERSION 格式不合法"
required_runtime_value runtime_id "RUNTIME_ID"
[[ "${runtime_id}" == "agent-runtime-${environment}" ]] \
  || fail "RUNTIME_ID 必须是 agent-runtime-${environment}"
required_runtime_value runtime_client_id "MEMORY_RUNTIME_CLIENT_ID"
[[ "${runtime_client_id}" == "couple-diary" ]] \
  || fail "MEMORY_RUNTIME_CLIENT_ID 必须是 couple-diary"
required_runtime_value runtime_key_id "MEMORY_RUNTIME_KEY_ID"
expected_key_id="${environment}-v1"
[[ "${runtime_key_id}" == "${expected_key_id}" ]] \
  || fail "MEMORY_RUNTIME_KEY_ID 必须是 ${expected_key_id}"
required_runtime_value runtime_hmac_secret "MEMORY_RUNTIME_SECRET"
validate_safe_token "MEMORY_RUNTIME_SECRET" "${runtime_hmac_secret}"
(( ${#runtime_hmac_secret} >= 32 )) || fail "MEMORY_RUNTIME_SECRET 长度不足 32 字符"

snapshot_master_key=""
password_pepper=""
if [[ -f "${output_file}" ]]; then
  snapshot_master_key="$(last_env_value "${output_file}" "CD_MEMORY_SNAPSHOT_MASTER_KEY")"
  password_pepper="$(last_env_value "${output_file}" "CD_MEMORY_ACCESS_PASSWORD_PEPPER")"
fi

if [[ -z "${snapshot_master_key}" ]]; then
  if [[ "${environment}" == "test" ]]; then
    snapshot_master_key="$(openssl rand -hex 32)"
  else
    prompt_required_hidden snapshot_master_key "Couple Diary production Snapshot master key"
  fi
fi
if [[ -z "${password_pepper}" ]]; then
  if [[ "${environment}" == "test" ]]; then
    password_pepper="$(openssl rand -hex 32)"
  else
    prompt_required_hidden password_pepper "Couple Diary production password pepper"
  fi
fi
validate_hex_key "CD_MEMORY_SNAPSHOT_MASTER_KEY" "${snapshot_master_key}"
validate_hex_key "CD_MEMORY_ACCESS_PASSWORD_PEPPER" "${password_pepper}"

umask 077
mkdir -p "$(dirname "${output_file}")"
timestamp="$(date '+%Y%m%d-%H%M%S')"
backup_file=""
if [[ -f "${output_file}" ]]; then
  backup_file="${output_file}.${timestamp}.$$.bak"
  cp -p "${output_file}" "${backup_file}"
  chmod 600 "${backup_file}"
fi

block_file="$(mktemp "${output_file}.block.XXXXXX")"
cleanup() { rm -f "${block_file}"; }
trap cleanup EXIT

cat >"${block_file}" <<EOF

#########Runtime联动自动化${environment}创建#########
# 创建时间: ${timestamp}
# 由 ./agent-runtime.sh configure-couple-diary ${environment} 追加。
# Runtime 来源 AgentPackage: ${agent_package_version}；Runtime 身份: ${runtime_id}。
# 同名变量以本文件中最后一次出现为准。
MEMOIR_INTEGRATION_NETWORK=${integration_network}
CD_MEMORY_RUNTIME_WORKER_ENABLED=${activate}
CD_MEMORY_RUNTIME_BASE_URL=http://runtime-api:8002
CD_MEMORY_RUNTIME_CLIENT_ID=${runtime_client_id}
CD_MEMORY_RUNTIME_KEY_ID=${runtime_key_id}
CD_MEMORY_RUNTIME_SECRET=${runtime_hmac_secret}
CD_MEMORY_RUNTIME_TIMEOUT_SECONDS=5.0
CD_MEMORY_RUNTIME_PACKAGE_ENABLED=${activate}
CD_MEMORY_SNAPSHOT_MASTER_KEY=${snapshot_master_key}
CD_MEMORY_SNAPSHOT_KEY_ID=1
CD_MEMORY_ACCESS_PASSWORD_PEPPER=${password_pepper}
#########Runtime联动自动化${environment}创建结束#########
EOF

grep -q 'change_me' "${block_file}" && fail "新配置块仍包含 change_me 占位符"
cat "${block_file}" >>"${output_file}"
chmod 600 "${output_file}"

printf '\n[OK] Couple Diary %s Runtime 联动配置已追加。\n' "${environment}"
printf 'Runtime 来源文件: %s\n' "${runtime_env_file}"
printf '目标文件: %s\n' "${output_file}"
if [[ -n "${backup_file}" ]]; then
  printf '原文件备份: %s\n' "${backup_file}"
else
  printf '目标原本不存在，已新建文件。\n'
fi
printf '文件和备份权限均为 0600，密钥未输出到终端。\n'
if [[ "${activate}" == "true" ]]; then
  printf '启用状态: worker/package 已显式开启，下次 Couple Diary 部署会启动 Runtime Worker。\n'
else
  printf '启用状态: worker/package 仍关闭；联通验证后用 --activate 重新执行。\n'
fi
