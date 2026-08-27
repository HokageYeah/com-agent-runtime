#!/bin/bash
set -euo pipefail

readonly DEFAULT_ENV_DIR="/usr/HokageYeah/服务端系统/env"

usage() {
  cat <<'EOF'
用法：
  ./agent-runtime.sh configure-docker test
  ./agent-runtime.sh configure-docker production
  ./agent-runtime.sh configure-docker <test|production> --output /absolute/path/runtime.env

说明：
  - test 默认追加到 /usr/HokageYeah/服务端系统/env/runtime-test.env。
  - production 默认追加到 /usr/HokageYeah/服务端系统/env/runtime-production.env。
  - 已有文件不会被覆盖；追加前会先生成时间戳备份。
  - 密码、密钥、私有地址和 Provider JSON 输入都不回显。
  - test 密钥留空时可由 OpenSSL 生成；production 密钥必须从受控密钥管理边界输入。
EOF
}

fail() {
  printf '错误：%s\n' "$1" >&2
  exit 1
}

prompt_value() {
  local variable_name="$1" label="$2" default_value="$3" input_value
  read -r -p "${label} [${default_value}]: " input_value
  printf -v "${variable_name}" '%s' "${input_value:-${default_value}}"
}

prompt_secret() {
  local variable_name="$1" label="$2" generated_value="$3" input_value
  read -r -s -p "${label}（留空自动生成）: " input_value
  printf '\n'
  printf -v "${variable_name}" '%s' "${input_value:-${generated_value}}"
}

prompt_required_hidden() {
  local variable_name="$1" label="$2" input_value
  read -r -s -p "${label}（必填，输入不回显）: " input_value
  printf '\n'
  [[ -n "${input_value}" ]] || fail "${label} 不能为空"
  printf -v "${variable_name}" '%s' "${input_value}"
}

prompt_hidden_value() {
  local variable_name="$1" label="$2" default_value="$3" input_value
  # 模型路由可能包含私有 endpoint，Provider JSON 包含 API Key，统一禁止终端回显。
  read -r -s -p "${label}（留空使用 ${default_value}）: " input_value
  printf '\n'
  printf -v "${variable_name}" '%s' "${input_value:-${default_value}}"
}

validate_token() {
  local label="$1" value="$2"
  [[ "${value}" =~ ^[A-Za-z0-9._~@%+=:,/-]+$ ]] \
    || fail "${label} 只能包含字母、数字和 ._~@%+=:,/- 这些安全字符"
}

validate_json_shape() {
  local label="$1" value="$2" expected_prefix="$3"
  [[ "${value}" == "${expected_prefix}"* ]] || fail "${label} 必须以 ${expected_prefix} 开头"
  [[ "${value}" != *$'\r'* && "${value}" != *$'\n'* ]] || fail "${label} 不能包含换行"
}

environment="${1:-}"
case "${environment}" in
  test|production) ;;
  *) usage; fail "环境只能是 test 或 production" ;;
esac
shift

output_file="${DEFAULT_ENV_DIR}/runtime-${environment}.env"
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --output)
      [[ "$#" -ge 2 ]] || fail "--output 缺少绝对路径"
      output_file="$2"
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage; fail "未知参数: $1" ;;
  esac
done

[[ "${output_file}" == /* ]] || fail "--output 必须是绝对路径"
[[ ! -L "${output_file}" ]] || fail "拒绝写入符号链接: ${output_file}"
if [[ -e "${output_file}" && ! -f "${output_file}" ]]; then
  fail "目标已存在但不是普通文件: ${output_file}"
fi
command -v openssl >/dev/null 2>&1 || fail "服务器缺少 openssl"
umask 077
mkdir -p "$(dirname "${output_file}")"

if [[ "${environment}" == "test" ]]; then
  default_api_port="18002"
  default_worker_id="agent-runtime-worker-test"
  compose_project="com-agent-runtime-test"
  integration_network="memoir-integration-test"
  db_auto_create="true"
  db_host="mysql"
  db_port="3306"
  db_name="couple_diary_agent_runtime_test"
  default_db_user="runtime_test"
  runtime_redis_url="redis://redis:6379/14"
  runtime_id="agent-runtime-test"
  key_id="test-v1"
  tool_allow_private_endpoints="true"
  runtime_base_url="http://runtime-api:8002"
else
  default_api_port="18003"
  default_worker_id="agent-runtime-worker-production"
  compose_project="com-agent-runtime-production"
  integration_network="memoir-integration-production"
  db_auto_create="false"
  db_name="couple_diary_agent_runtime_prod"
  default_db_user="runtime_prod"
  runtime_id="agent-runtime-production"
  key_id="production-v1"
  tool_allow_private_endpoints="false"
fi

printf '即将生成 Runtime Docker %s 配置。\n' "${environment}"
printf '目标文件: %s\n' "${output_file}"
if [[ "${environment}" == "test" ]]; then
  printf 'test 普通字段可使用默认值，密钥留空则安全随机生成。\n\n'
else
  printf 'production 不创建数据库/Redis sidecar，也不代你生成正式密钥。\n'
  printf '请准备 Runtime 专用外部 MySQL/Redis、受控密钥以及两个 HTTPS origin。\n\n'
fi

prompt_value runtime_api_host_port "Runtime 宿主回环端口" "${default_api_port}"
[[ "${runtime_api_host_port}" =~ ^[0-9]+$ ]] || fail "Runtime 端口必须是数字"
(( runtime_api_host_port >= 1024 && runtime_api_host_port <= 65535 )) || fail "Runtime 端口必须在 1024-65535 之间"
prompt_value runtime_worker_id "Runtime Worker ID" "${default_worker_id}"
validate_token "Runtime Worker ID" "${runtime_worker_id}"
prompt_value agent_package_version "Memoir AgentPackage 版本" "1.0.4"
[[ "${agent_package_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "AgentPackage 版本必须类似 1.0.4"

if [[ "${environment}" == "production" ]]; then
  prompt_required_hidden db_host "Runtime production MySQL host"
  [[ "${db_host}" =~ ^[A-Za-z0-9._-]+$ ]] || fail "MySQL host 格式不合法"
  prompt_value db_port "Runtime production MySQL port" "3306"
  [[ "${db_port}" =~ ^[0-9]+$ ]] || fail "MySQL port 必须是数字"
fi
prompt_value db_user "Runtime ${environment} 专用库用户" "${default_db_user}"
[[ "${db_user}" =~ ^[A-Za-z0-9_]+$ ]] || fail "数据库用户只能包含字母、数字和下划线"

if [[ "${environment}" == "test" ]]; then
  prompt_secret db_password "Runtime test 数据库密码" "$(openssl rand -hex 32)"
  prompt_secret mysql_root_password "Runtime test MySQL root 密码" "$(openssl rand -hex 32)"
  prompt_secret runtime_hmac_secret "Runtime/Business test 共享 HMAC 密钥" "$(openssl rand -hex 32)"
else
  prompt_required_hidden db_password "Runtime production 数据库密码"
  mysql_root_password=""
  prompt_required_hidden runtime_redis_url "Runtime production 专用 Redis URL"
  [[ "${runtime_redis_url}" == redis://* || "${runtime_redis_url}" == rediss://* ]] || fail "Runtime production Redis URL 必须以 redis:// 或 rediss:// 开头"
  prompt_required_hidden runtime_hmac_secret "Runtime/Business production 共享 HMAC 密钥"
fi
validate_token "Runtime 数据库密码" "${db_password}"
if [[ -n "${mysql_root_password}" ]]; then validate_token "MySQL root 密码" "${mysql_root_password}"; fi
validate_token "HMAC 密钥" "${runtime_hmac_secret}"

generated_fernet_key="$(openssl rand -base64 32 | tr -d '\n' | tr '+/' '-_')"
if [[ "${environment}" == "test" ]]; then
  prompt_secret snapshot_fernet_key "Runtime test Fernet 密钥" "${generated_fernet_key}"
  prompt_secret jwt_secret "Runtime test JWT 密钥" "$(openssl rand -hex 32)"
else
  prompt_required_hidden snapshot_fernet_key "Runtime production Fernet 密钥"
  prompt_required_hidden jwt_secret "Runtime production JWT 密钥"
fi
[[ "${snapshot_fernet_key}" =~ ^[A-Za-z0-9_-]{43}=$ ]] || fail "Runtime Fernet 密钥必须是 URL-safe Base64 编码的 32-byte key"
validate_token "JWT 密钥" "${jwt_secret}"

if [[ "${environment}" == "test" ]]; then
  prompt_value business_base_url "共享 Docker 网络中的业务后端 origin" "http://couple-diary-backend:8008"
  [[ "${business_base_url}" =~ ^http://[A-Za-z0-9._-]+:[0-9]+$ ]] || fail "test 业务后端 origin 必须类似 http://couple-diary-backend:8008，不带路径"
  backend_cors_origins="http://127.0.0.1:${runtime_api_host_port}"
else
  prompt_required_hidden runtime_base_url "Runtime production HTTPS origin"
  prompt_required_hidden business_base_url "业务后端 production HTTPS origin"
  prompt_required_hidden backend_cors_origins "Runtime production CORS origins"
  [[ "${runtime_base_url}" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || fail "Runtime production origin 必须是不带路径的 https:// origin"
  [[ "${business_base_url}" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || fail "业务后端 production origin 必须是不带路径的 https:// origin"
fi

prompt_hidden_value model_routes_json "MODEL_ROUTES_JSON" "[]"
validate_json_shape "MODEL_ROUTES_JSON" "${model_routes_json}" "["
prompt_hidden_value memoir_model_routes_json "MEMOIR_MODEL_NODE_ROUTES_JSON" "{}"
validate_json_shape "MEMOIR_MODEL_NODE_ROUTES_JSON" "${memoir_model_routes_json}" "{"
prompt_hidden_value provider_keys_json "MODEL_PROVIDER_API_KEYS_JSON" "{}"
validate_json_shape "MODEL_PROVIDER_API_KEYS_JSON" "${provider_keys_json}" "{"
prompt_value memoir_media_enabled "是否启用回忆录媒体生成 (true/false)" "false"
[[ "${memoir_media_enabled}" == "true" || "${memoir_media_enabled}" == "false" ]] || fail "MEMOIR_MEDIA_ENABLED 只能是 true 或 false"

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

mysql_root_line=""
if [[ "${environment}" == "test" ]]; then mysql_root_line="RUNTIME_MYSQL_ROOT_PASSWORD=${mysql_root_password}"; fi
separator_title="#########自动化${environment}创建#########"

cat >"${block_file}" <<EOF

${separator_title}
# 创建时间: ${timestamp}
# 由 ./agent-runtime.sh configure-docker ${environment} 追加；同名变量以本文件中最后一次出现为准。
COMPOSE_PROJECT_NAME=${compose_project}
ENVIRONMENT=${environment}
MEMOIR_INTEGRATION_NETWORK=${integration_network}
RUNTIME_ENV_FILE=${output_file}
RUNTIME_API_HOST_PORT=${runtime_api_host_port}
RUNTIME_WORKER_ID=${runtime_worker_id}
AGENT_PACKAGE_VERSION=${agent_package_version}

DB_DRIVER=mysql+mysqlconnector
DB_AUTO_CREATE=${db_auto_create}
DB_HOST=${db_host}
DB_PORT=${db_port}
DB_NAME=${db_name}
DB_USER=${db_user}
DB_PASSWORD=${db_password}
DB_CHARSET=utf8mb4
DB_ECHO=false
${mysql_root_line}
RUNTIME_REDIS_URL=${runtime_redis_url}

RUNTIME_ID=${runtime_id}
RUNTIME_AUDIT_SINK_CONFIGURED=true
RUNTIME_EXTERNAL_EXPORTER_ENABLED=false
RUNTIME_TOOL_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS=${tool_allow_private_endpoints}
BACKEND_CORS_ORIGINS=${backend_cors_origins}
DEBUG=false

RUNTIME_TRUSTED_CLIENTS_JSON={"couple-diary":{"tenant_id":"couple-diary","keys":{"${key_id}":"${runtime_hmac_secret}"},"agent_ids":["memoir_agent"],"business_types":["couple_memory"],"callback_target_ids":["memory_callback"],"connector_ids":["couple_diary_backend"],"data_domains":["couple_memory"],"authorization_version":1,"model_data_residency":"private"}}
RUNTIME_BUSINESS_CONNECTORS_JSON={"couple_diary_backend":{"enabled":true,"base_url":"${business_base_url}","runtime_id":"couple-diary","key_id":"${key_id}","secret":"${runtime_hmac_secret}"}}
RUNTIME_CALLBACK_TARGETS_JSON={"memory_callback":{"enabled":true,"url":"${business_base_url}/api/v1/internal/memory-callbacks","runtime_id":"couple-diary","key_id":"${key_id}","secret":"${runtime_hmac_secret}"}}
MEMORY_TOOL_TRUSTED_RUNTIMES_JSON={"${runtime_id}":{"keys":{"${key_id}":"${runtime_hmac_secret}"}}}
MEMORY_RUNTIME_BASE_URL=${runtime_base_url}
MEMORY_RUNTIME_CLIENT_ID=couple-diary
MEMORY_RUNTIME_KEY_ID=${key_id}
MEMORY_RUNTIME_SECRET=${runtime_hmac_secret}
MEMORY_SNAPSHOT_FERNET_KEY=${snapshot_fernet_key}
USER_AUTH_JWT_SECRET=${jwt_secret}
USER_AUTH_JWT_ISSUER=couple-diary

MODEL_ROUTES_JSON=${model_routes_json}
MEMOIR_MODEL_NODE_ROUTES_JSON=${memoir_model_routes_json}
MODEL_PROVIDER_API_KEYS_JSON=${provider_keys_json}
MEMOIR_MEDIA_ENABLED=${memoir_media_enabled}
#########自动化${environment}创建结束#########
EOF

grep -q 'change_me' "${block_file}" && fail "新配置块仍包含 change_me 占位符"
cat "${block_file}" >>"${output_file}"
chmod 600 "${output_file}"

printf '\n[OK] Runtime Docker %s 配置已追加。\n' "${environment}"
printf '目标文件: %s\n' "${output_file}"
if [[ -n "${backup_file}" ]]; then printf '原文件备份: %s\n' "${backup_file}"; else printf '目标原本不存在，已新建文件。\n'; fi
printf '文件和备份权限均为 0600，密钥未输出到终端。\n'
printf '下一步：只在服务器内用编辑器检查最后一个配置块，不要把密钥粘贴到日志或工单。\n'
