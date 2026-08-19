#!/usr/bin/env python3
"""把部署目录内的 AgentPackage 注册进 agent_definitions 表（幂等 upsert）。

背景：正式服务链路只读数据库里的 AgentDefinition 行，从不自动同步磁盘包
（capabilities API 仅在线读包做探测；测试 harness 只 seed 自己的临时库）。
新环境首次联调时 agent_definitions 为空，create Run 会 409
"AgentPackage 不可用于创建 Run"。本脚本用 AgentPackageService.load()
复用全部包校验与真实 package_digest 后落库，注册结果与磁盘包严格一致。

用法（推荐走 CLI 入口，doctor 校验 + 按环境加载 .env）::

    ./agent-runtime.sh register development --agent-id memoir_agent --version 1.0.2
    ./agent-runtime.sh register development --version 1.0.2 --dry-run

也可直接调用本模块（依赖进程已加载目标环境的 ENVIRONMENT/.env）::

    .venv/bin/python -m app.scripts.register_agent_package --version 1.0.2 --dry-run
    .venv/bin/python -m app.scripts.register_agent_package --agent-id X --version Y
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config.database_config import get_database_url
from app.models import AgentDefinition
from app.services.agent_package_service import AgentPackageService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# 与 capabilities_api 同款目标：app/agents 是部署内置的受管包目录
# （本脚本在 app/scripts/ 下，比 api/endpoints/ 浅一层，取 parents[1]）。
PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "agents"


def _build_definition_json(package: object) -> dict:
    """把磁盘包的受管字段组装成 definition_json（create/planner 消费的子集）。

    消费方：input_schema / allowed_business_types / policy（create 校验）、
    workflow_nodes（StaticPlanner 建 plan）、ui_trace（trace 模式降级）。
    """

    return {
        "allowed_business_types": package.allowed_business_types,
        "input_schema": package.input_schema,
        "policy": package.policy.model_dump(mode="json"),
        "workflow_nodes": [node.model_dump(mode="json") for node in package.workflow_nodes],
        "ui_trace": package.ui_trace.model_dump(mode="json"),
    }


def register(agent_id: str, version: str, *, dry_run: bool) -> int:
    """加载磁盘包并幂等 upsert 到 agent_definitions；dry-run 只打印不落库。"""

    package = AgentPackageService(PACKAGE_ROOT).load(agent_id, version)
    definition_json = _build_definition_json(package)
    logger.info(
        "磁盘包加载成功 agent_id=%s version=%s digest=%s status=%s nodes=%s",
        agent_id,
        version,
        package.package_digest,
        package.status,
        len(package.workflow_nodes),
    )

    engine = create_engine(get_database_url())
    now = datetime.now(UTC)
    try:
        with Session(engine) as session:
            existing = session.scalar(
                select(AgentDefinition).where(
                    AgentDefinition.agent_id == agent_id,
                    AgentDefinition.version == version,
                )
            )
            if dry_run:
                logger.info(
                    "[dry-run] 库内现有记录：%s；将写入 status=%s digest=%s",
                    "无（将插入新行）" if existing is None else f"id={existing.id} "
                    f"status={existing.status} digest={existing.package_digest}",
                    package.status,
                    package.package_digest,
                )
                return 0

            if (
                existing is not None
                and existing.package_digest == package.package_digest
                and existing.status == package.status
            ):
                logger.info("库内 definition 与磁盘包一致，无需注册，退出")
                return 0

            if existing is None:
                session.add(
                    AgentDefinition(
                        agent_id=agent_id,
                        version=version,
                        runtime_type="workflow",
                        definition_json=definition_json,
                        package_digest=package.package_digest,
                        contract_version=package.contract_version,
                        status=package.status,
                        status_changed_at=now,
                        status_changed_by="register_agent_package",
                        status_change_reason="register from deployed package files",
                    )
                )
                logger.info("插入新 definition agent_id=%s version=%s", agent_id, version)
            else:
                # 同 (agent_id, version) 重新注册：包文件内容或生命周期状态变化，
                # 以磁盘为准更新；已有 Run 冻结的旧 digest 不受影响。
                existing.definition_json = definition_json
                existing.package_digest = package.package_digest
                existing.contract_version = package.contract_version
                existing.status = package.status
                existing.status_changed_at = now
                existing.status_changed_by = "register_agent_package"
                existing.status_change_reason = "re-register sync from package files"
                logger.info(
                    "更新已存在 definition id=%s agent_id=%s version=%s",
                    existing.id,
                    agent_id,
                    version,
                )
            session.commit()
            logger.info("注册提交完成 status=%s", package.status)
    finally:
        engine.dispose()

    if package.status != "active":
        # create 只认 active；注册了也建不了 Run，必须显式提醒而不是静默通过。
        logger.warning(
            "包 status=%s 非 active，create Run 仍会被拒绝；请检查 agent.yaml",
            package.status,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="register_agent_package",
        description="把部署目录内的 AgentPackage 幂等注册进 agent_definitions 表",
    )
    parser.add_argument("--agent-id", default="memoir_agent", help="默认 memoir_agent")
    # version 必填：包版本随每次升级变化，静默默认值会注册过期版本（曾默认
    # 1.0.1 在 1.0.2 发布后成为陷阱）；注册动作必须显式声明目标版本。
    parser.add_argument("--version", required=True, help="要注册的包版本，如 1.0.2")
    parser.add_argument(
        "--dry-run", action="store_true", help="只加载并打印，不写数据库"
    )
    args = parser.parse_args()
    return register(args.agent_id, args.version, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
