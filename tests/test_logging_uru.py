from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

from app.core import logging_uru
from app.core.config import settings


def test_resolve_log_level_returns_debug_when_debug_enabled() -> None:
    assert logging_uru.resolve_log_level("INFO", debug=True) == "DEBUG"


def test_resolve_log_level_uses_configured_level_when_debug_disabled() -> None:
    assert logging_uru.resolve_log_level("warning", debug=False) == "WARNING"


def test_should_enable_diagnose_only_in_debug_mode() -> None:
    assert logging_uru.should_enable_diagnose(debug=True) is True
    assert logging_uru.should_enable_diagnose(debug=False) is False


def test_build_log_file_paths_returns_expected_daily_filenames() -> None:
    run_log_path, error_log_path = logging_uru.build_log_file_paths(
        project_root=Path("/tmp/demo-project"),
        date_text="2026-06-15",
    )

    assert run_log_path == Path("/tmp/demo-project/logs/app_run/app_run_2026-06-15.log")
    assert error_log_path == Path(
        "/tmp/demo-project/logs/app_error/app_error_2026-06-15.log"
    )


def test_setup_logging_reads_level_from_settings_logging_group() -> None:
    """日志初始化应优先从主 settings 的 logging 分组读取日志级别。"""

    with (
        patch.object(
            type(settings),
            "logging",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(logging_level="warning"),
        ),
        patch.object(logging_uru.logger, "remove"),
        patch.object(logging_uru.logger, "add") as mock_add,
        patch.object(logging_uru, "ensure_log_directories"),
        patch.object(logging_uru.logging, "basicConfig"),
        patch.object(logging_uru, "configure_third_party_loggers"),
    ):
        logging_uru.setup_logging()

    console_handler_kwargs = mock_add.call_args_list[0].kwargs
    assert console_handler_kwargs["level"] == "WARNING"
