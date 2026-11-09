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
    # 部署清单（app/agents/deploy_packages.yaml）全部注册，Compose register 服务即走此入口
    ./agent-runtime.sh register production --all --include memoir_agent@1.0.8 --dry-run

也可直接调用本模块（依赖进程已加载目标环境的 ENVIRONMENT/.env）::

    .venv/bin/python -m app.scripts.register_agent_package --version 1.0.2 --dry-run
    .venv/bin/python -m app.scripts.register_agent_package --agent-id X --version Y
"""

from __future__ import annotations

import argparse
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.registry import is_executable_agent
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
# 部署必须注册的包清单；与包目录同级，随镜像一起发布。
DEPLOY_MANIFEST = PACKAGE_ROOT / "deploy_packages.yaml"
# 清单与 --include 条目统一写成 agent_id@x.y.z，避免拼写成镜像 tag 或任意路径。
_PACKAGE_REF = re.compile(r"^([a-z][a-z0-9_]{0,63})@(\d+\.\d+\.\d+)$")


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
    # active 启用必须已有代码内受信工厂；共享 CLI 与 dry-run 也走同一门禁。
    # 放在数据库访问前拒绝，非 active 包继续沿用既有加载/生命周期规则。
    if package.status == "active" and not is_executable_agent(package.agent_id):
        logger.error(
            "active 注册被拒绝 code=AGENT_EXECUTOR_NOT_REGISTERED agent_id=%s version=%s",
            package.agent_id,
            package.version,
        )
        return 1
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


def parse_package_ref(ref: str) -> tuple[str, str]:
    """把 agent_id@x.y.z 拆成 (agent_id, version)；格式不对直接报错，不猜测。"""

    matched = _PACKAGE_REF.match(ref.strip())
    if matched is None:
        raise ValueError(f"AGENT_PACKAGE_REF_INVALID: {ref!r}，应为 agent_id@x.y.z")
    return matched.group(1), matched.group(2)


def load_deploy_manifest(path: Path | None = None) -> list[tuple[str, str]]:
    """读取部署清单 packages 列表；空清单或格式错误都拒绝，避免静默什么都不注册。"""

    data = yaml.safe_load((path or DEPLOY_MANIFEST).read_text(encoding="utf-8")) or {}
    refs = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(refs, list) or not refs or not all(isinstance(r, str) for r in refs):
        raise ValueError("AGENT_DEPLOY_MANIFEST_INVALID: packages 必须是非空字符串列表")
    return [parse_package_ref(ref) for ref in refs]


def _precheck(agent_id: str, version: str) -> bool:
    """不连数据库的预检：包能通过全部加载校验，且 active 包有受信执行身份。"""

    try:
        package = AgentPackageService(PACKAGE_ROOT).load(agent_id, version)
    except Exception as exc:  # 包缺失、digest/schema 校验失败等一律视为预检失败
        logger.error(
            "预检失败 code=AGENT_PACKAGE_INVALID agent_id=%s version=%s error=%s",
            agent_id,
            version,
            type(exc).__name__,
        )
        return False
    if package.status == "active" and not is_executable_agent(package.agent_id):
        logger.error(
            "预检失败 code=AGENT_EXECUTOR_NOT_REGISTERED agent_id=%s version=%s",
            agent_id,
            version,
        )
        return False
    return True


def register_all(includes: list[str], *, dry_run: bool, manifest: Path | None = None) -> int:
    """注册部署清单 + 额外条目（如 Compose 传入的 AGENT_PACKAGE_VERSION）。

    先对全部条目做不连库预检，任一失败就整体退出、不写任何一行（全有或全无的前置门禁）；
    预检全部通过后再逐条复用 register() 幂等 upsert，任一条失败即停止并返回非零。
    """

    refs = load_deploy_manifest(manifest) + [parse_package_ref(ref) for ref in includes]
    ordered = list(dict.fromkeys(refs))  # 去重且保持清单顺序
    logger.info(
        "部署清单注册开始 count=%s dry_run=%s packages=%s",
        len(ordered),
        dry_run,
        ",".join(f"{agent}@{version}" for agent, version in ordered),
    )
    failed = [f"{a}@{v}" for a, v in ordered if not _precheck(a, v)]
    if failed:
        logger.error("部署清单预检未通过，未写入任何定义 failed=%s", ",".join(failed))
        return 1
    for agent_id, version in ordered:
        code = register(agent_id, version, dry_run=dry_run)
        if code != 0:
            logger.error("部署清单注册中断 agent_id=%s version=%s", agent_id, version)
            return code
    logger.info("部署清单注册完成 count=%s dry_run=%s", len(ordered), dry_run)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="register_agent_package",
        description="把部署目录内的 AgentPackage 幂等注册进 agent_definitions 表",
    )
    parser.add_argument("--agent-id", default="memoir_agent", help="默认 memoir_agent")
    # version 必填（单包模式）：包版本随每次升级变化，静默默认值会注册过期版本
    # （曾默认 1.0.1 在 1.0.2 发布后成为陷阱）；注册动作必须显式声明目标版本。
    parser.add_argument("--version", help="要注册的包版本，如 1.0.2（单包模式必填）")
    parser.add_argument(
        "--all", action="store_true", help="注册 app/agents/deploy_packages.yaml 全部条目"
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="--all 时额外注册的 agent_id@x.y.z，可重复（Compose 传 AGENT_PACKAGE_VERSION）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只加载并打印，不写数据库"
    )
    args = parser.parse_args()
    if args.all:
        return register_all(args.include, dry_run=args.dry_run)
    if not args.version or args.include:
        parser.error("单包模式需要 --version，且 --include 只能与 --all 一起使用")
    return register(args.agent_id, args.version, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
