#!/bin/bash
set -e

APP_ENV="${1:-development}"

echo "使用环境: ${APP_ENV}"
echo "如果是首次运行，请先执行: poetry run python -m app.scripts.set_env ${APP_ENV} bootstrap"
echo "如果看到 your_mysql_user / your_mysql_password 相关报错，请检查 .env.${APP_ENV}.local 或 .env.local"

ENVIRONMENT="$APP_ENV" poetry run python run_app.py
