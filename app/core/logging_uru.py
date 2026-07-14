from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

from app.core.config import PROJECT_ROOT, settings

THIRD_PARTY_WARNING_LOGGERS = (
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.proxy",
)

LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | " "{name}:{function}:{line} - {message}"
)


def resolve_log_level(configured_level: str, *, debug: bool) -> str:
    """解析当前运行应使用的日志级别。"""
    if debug:
        return "DEBUG"
    return configured_level.upper()


def should_enable_diagnose(*, debug: bool) -> bool:
    """是否开启 loguru diagnose。

    diagnose=True 会把局部变量和更详细的上下文打进异常日志里。
    这在本地调试很方便，但在线上或共享环境里有泄漏敏感信息的风险，
    因此这里只允许在 DEBUG 模式下开启。
    """
    return debug


def build_log_file_paths(
    *,
    project_root: Path,
    date_text: str | None = None,
) -> tuple[Path, Path]:
    """构建当天运行日志和错误日志的目标路径。"""
    resolved_date_text = date_text or datetime.now().strftime("%Y-%m-%d")
    run_log_path = (
        project_root / "logs" / "app_run" / f"app_run_{resolved_date_text}.log"
    )
    error_log_path = (
        project_root / "logs" / "app_error" / f"app_error_{resolved_date_text}.log"
    )
    return run_log_path, error_log_path


def ensure_log_directories(run_log_path: Path, error_log_path: Path) -> None:
    """确保日志目录存在。"""
    run_log_path.parent.mkdir(parents=True, exist_ok=True)
    error_log_path.parent.mkdir(parents=True, exist_ok=True)


def configure_third_party_loggers(level: int = logging.WARNING) -> None:
    """收敛第三方库的默认日志级别。"""
    for logger_name in THIRD_PARTY_WARNING_LOGGERS:
        logging.getLogger(logger_name).setLevel(level)


class InterceptHandler(logging.Handler):
    """把标准 logging 的日志桥接到 loguru。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame = logging.currentframe()
        depth = 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging() -> None:
    """初始化项目日志系统。

    当前目标：
    1. 统一标准 logging 与 loguru 输出
    2. DEBUG 环境保留更强诊断能力
    3. 非 DEBUG 环境默认关闭 diagnose，降低敏感信息泄漏风险
    4. 运行日志与错误日志分目录落盘，方便排查
    """
    logger.remove()

    logging_config = settings.logging
    application_config = settings.application
    log_level = resolve_log_level(
        logging_config.logging_level,
        debug=application_config.debug,
    )
    enable_diagnose = should_enable_diagnose(debug=application_config.debug)
    run_log_path, error_log_path = build_log_file_paths(project_root=PROJECT_ROOT)
    ensure_log_directories(run_log_path, error_log_path)

    logger.add(
        sink=sys.stderr,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        level=log_level,
        colorize=True,
        enqueue=True,
        diagnose=enable_diagnose,
    )

    logger.add(
        sink=str(run_log_path),
        format=LOG_FORMAT,
        level=log_level,
        filter=lambda record: record["level"].no < logging.ERROR,
        rotation="00:00",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
        diagnose=enable_diagnose,
    )

    logger.add(
        sink=str(error_log_path),
        format=LOG_FORMAT,
        level="ERROR",
        rotation="00:00",
        retention="90 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
        diagnose=enable_diagnose,
    )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    for logger_name in list(logging.root.manager.loggerDict.keys()):
        logging.getLogger(logger_name).handlers = []
        logging.getLogger(logger_name).propagate = True

    configure_third_party_loggers()

    logger.info(
        "日志系统初始化完成，level=%s，diagnose=%s，run_log=%s，error_log=%s",
        log_level,
        enable_diagnose,
        run_log_path,
        error_log_path,
    )


def get_logger(name: str | None = None):
    """获取命名日志记录器。"""
    return logger.bind(name=name)
