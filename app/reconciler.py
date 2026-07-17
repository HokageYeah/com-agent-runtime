"""Runtime P0 对账器命令行入口。"""

from __future__ import annotations

import argparse

from app.core.logging_uru import setup_logging
from app.db.sqlalchemy_db import database
from app.services.reconciliation_service import ReconciliationService


def main() -> None:
    """执行一次安全对账；生产调度器可按固定周期调用本入口。"""
    parser = argparse.ArgumentParser(description="Run AgentRuntime reconciliation once")
    parser.add_argument("--once", action="store_true", help="兼容调度器显式单次调用")
    parser.parse_args()
    setup_logging()
    database.connect()
    session = database.get_session_factory()()
    try:
        ReconciliationService(session).run_once()
    finally:
        session.close()
        database.close()


if __name__ == "__main__":
    main()
