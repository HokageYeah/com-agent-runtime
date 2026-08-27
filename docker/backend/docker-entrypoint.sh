#!/bin/sh
set -eu

# 容器只负责执行一个明确的长期进程；数据库建库和迁移由独立 prepare job 完成。
# 这样 API、Worker、launcher、Reconciler 不会并发修改 schema，也不会吞掉迁移失败。
: "${ENVIRONMENT:?ENVIRONMENT must be injected at runtime}"

case "${ENVIRONMENT}" in
  test|production) ;;
  *)
    printf '%s\n' "Unsupported ENVIRONMENT: ${ENVIRONMENT}" >&2
    exit 64
    ;;
esac

if [ "$#" -eq 0 ]; then
  printf '%s\n' 'Container command is required' >&2
  exit 64
fi

exec "$@"
