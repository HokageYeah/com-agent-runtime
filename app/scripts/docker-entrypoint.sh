#!/bin/bash
set -e

export ENVIRONMENT=production

echo "等待 MySQL 准备就绪..."
until python -c "import mysql.connector; mysql.connector.connect(host='mysql', user='root', password='aa123456')" &> /dev/null
do
  echo "MySQL 尚未准备就绪 - 等待..."
  sleep 3
done
echo "MySQL 已准备就绪"

python -m app.scripts.create_database || true

echo "执行数据库迁移..."
alembic upgrade head || python -m app.scripts.init_database

exec "$@"
