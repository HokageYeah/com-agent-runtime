FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VERSION=2.1.4 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

# M8 音频转码依赖：ffmpeg（含 ffprobe）必须在切换非 root 前以系统包管理器
# 安装并清理 apt 索引；不通过 ARG/ENV 传 Key，运行时不联网安装。
# Debian 的 ffmpeg 包同时提供 /usr/bin/ffmpeg 与 /usr/bin/ffprobe，
# 与 MEMOIR_AUDIO_FFMPEG_PATH / MEMOIR_AUDIO_FFPROBE_PATH 默认值一致。
# 仅替换软件源主机，保留 Debian 发行版、组件和 Signed-By 校验。
# Compose 可覆盖为就近镜像；直接 docker build 默认仍使用官方源。
ARG APT_MIRROR=https://deb.debian.org
RUN sed -i -E "s|https?://(deb.debian.org|security.debian.org)|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && printf 'Acquire::Retries "3";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' > /etc/apt/apt.conf.d/80-runtime-download \
    && apt-get update -o APT::Update::Error-Mode=any \
    && apt-get install -y --no-install-recommends ffmpeg \
    && ffmpeg -version > /dev/null \
    && ffprobe -version > /dev/null \
    && rm -rf /var/lib/apt/lists/*

# 独立缓存系统依赖，Python 依赖变动不应触发重新下载 ffmpeg。
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
