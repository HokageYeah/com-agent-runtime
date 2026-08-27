FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VERSION=2.1.4 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root --no-ansi

RUN groupadd --system --gid 10001 runtime \
    && useradd --system --uid 10001 --gid runtime --home-dir /app --shell /usr/sbin/nologin runtime \
    && mkdir -p /app/logs/app_run /app/logs/app_error \
    && chown -R runtime:runtime /app/logs

# 运行用户只需要写日志；源码与入口保持 root 所有，避免进程被劫持后篡改自身代码。
COPY . .
COPY docker/backend/docker-entrypoint.sh /usr/local/bin/runtime-entrypoint
RUN chmod 0555 /usr/local/bin/runtime-entrypoint

USER runtime

ENTRYPOINT ["/usr/local/bin/runtime-entrypoint"]
CMD ["python", "run_app.py"]
