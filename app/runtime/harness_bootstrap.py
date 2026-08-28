"""测试子进程的最小 bootstrap；导入失败时只报告安全元数据。"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Literal

_Role = Literal["api", "worker", "reconciler"]
_ENTRY_MODULE = "app.runtime.harness_entry"


def _failed(role: _Role, error: BaseException) -> None:
    """Bootstrap 只输出固定元数据，不输出异常消息或环境。"""
    error_type = type(error).__name__
    if not error_type.isidentifier() or len(error_type) > 80:
        error_type = "UnknownError"
    print(
        json.dumps(
            {
                "event": "harness_failed",
                "role": role,
                "stage": "bootstrap",
                "error_type": error_type,
            },
            separators=(",", ":"),
        ),
        file=sys.stderr,
        flush=True,
    )


def bootstrap_entry(role: _Role, config_path: Path) -> None:
    """在导入 Runtime 重依赖前建立最小可诊断边界。"""
    try:
        entry = importlib.import_module(_ENTRY_MODULE)
        config = entry.HarnessProcessConfig.from_path(config_path)
        if config.role != role:
            raise ValueError("TEST_HARNESS_ROLE_MISMATCH")
    except BaseException as exc:
        _failed(role, exc)
        raise SystemExit(1) from None
    entry.run(config)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AgentRuntime test harness bootstrap entry"
    )
    parser.add_argument("--role", required=True, choices=("api", "worker", "reconciler"))
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    bootstrap_entry(args.role, args.config)


if __name__ == "__main__":
    main()
