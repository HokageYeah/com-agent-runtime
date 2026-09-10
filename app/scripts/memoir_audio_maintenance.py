"""M8 R7 Memoir 音频孤儿维护命令：扫描、分类、（可选）删除私有对象。

用法（仓根执行）：
    python -m app.scripts.memoir_audio_maintenance --environment test --dry-run
    python -m app.scripts.memoir_audio_maintenance --environment production --execute --limit 100

安全边界：
1. 只连 Runtime 库（require_runtime_database 校验库名含 agent_runtime，
   绝不触达业务库 couple_diary_dev/test/prod）。
2. 删除只针对"明确未发布且超保留窗"的对象；已发布/账本未清/在途/
   发布状态未知一律保留。
3. 404/NoSuchKey 视为清理成功（对象本就不存在）；其他删除失败计数并
   继续本批，不中断整轮。
4. 日志只携带计数、安全枚举与短 ID；对象键不进日志。

注入模式（测试/演练）：main(..., session_factory=..., oss_deleter=..., tool_client=...)
跳过真实引擎与 OSS SDK；发布探测口没有测试专用注入——未显式传入
publish_probe 时，main 一律走 build_production_publish_probe 从 settings
装配真实 ToolGateway（tool_client 仅在传输层替换为 MockTransport，签名、
网关、分类全部保持真实代码路径）。
"""
from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import AgentRun
from app.models.memoir_audio_job import MemoirAudioJob
from app.runtime.tool_gateway import ToolGateway
from app.services.memoir.memoir_audio_jobs import (
    STATE_SUBMISSION_UNKNOWN,
    MemoirAudioJobsError,
    MemoirAudioJobsService,
    _as_aware,
)

logger = logging.getLogger(__name__)

# 删除口协议：delete_object(object_key) -> bool。
# True=已删除；False=404/NoSuchKey（对象不存在，同样算清理成功）；
# 抛异常=删除失败（计入 delete_failed，不中断本批）。
# 用 Protocol 而非 Callable 别名：所有调用方都按对象方法使用
# （oss_deleter.delete_object(...)），真实实现与测试假删除口均结构化匹配。
class OssDeleter(Protocol):
    """OSS 删除口协议：任何提供 delete_object(object_key) -> bool 的对象。"""

    def delete_object(self, object_key: str) -> bool: ...


# 发布探测口协议（R5 三态语义，替代 Runtime 本地 publish 表对账）：
# - 返回 dict → 整 Run 已发布；再按 audio_object_keys 成员关系判定该对象
#   是否已被引用（缺字段按空列表，兼容旧 Business 包）；
# - 返回 None → 业务明确未发布（超过保留窗才可进删除候选）；
# - 抛异常 → 发布状态未知（keep_unknown，fail-safe 保留人工复核）。
class PublishProbe(Protocol):
    """发布探测口协议：可调用对象，入参为账本作业行。"""

    def __call__(self, job: MemoirAudioJob) -> dict[str, Any] | None: ...


# 探测异常 / 未注入探测口的哨兵。绝不能用 None：None = 明确未发布 404。
_PROBE_UNKNOWN = object()


class PublishStateUnknownError(Exception):
    """探测前置身份不完整：Run 缺失 / 发布引用缺失 / epoch 漂移。

    与网关调用失败一样归入 keep_unknown——没有权威身份就绝不进入删除
    路径。只携带安全枚举码，绝不携带 Run 输入或对象键。
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def build_publish_probe(session: Session, gateway: Any) -> PublishProbe:
    """构建真实发布探测口：逐字镜像生产发布节点的对账查询形状。

    R5 修复：旧实现把 job.business_id 当 archive_id、自造
    ``orphan-maintenance:{job_id}`` 幂等键——Business 按原发布幂等键精确
    查询，自造键永远未命中，不能证明对象未发布。现在：

    1. 按 job.run_id 现读权威 AgentRun（populate_existing 防止身份映射
       吃到本会话早前的旧快照）；Run 不存在 → 未知。
    2. archive_id/snapshot_id/generation_epoch 一律取自 run.input_json，
       与 runner.py publish_document 节点同源；任一缺失或类型不符 → 未知。
    3. job.generation_epoch 必须与 input 中的 epoch 一致（防跨代误删）。
    4. logical_key 与 runner 发布节点逐字一致，Business 才能按原发布
       幂等键精确命中，命中才能证明"已发布"。
    5. tool_context 复用 ToolGateway.build_tool_context 静态生产方法，
       其对 Run 的校验会 fail-closed 抛错 → 未知（正是保守行为）。
    6. 走 4 字段 wire 调 get_publish_result；PUBLISH_NOT_YET_OBSERVED
       由网关归一为 None（原键权威未命中 = 明确未发布），其余异常原样
       上抛 → run_maintenance 归入未知。
    """

    def probe(job: MemoirAudioJob) -> dict[str, Any] | None:
        # 现读权威 Run：探测身份只信 AgentRun，不信账本行自报字段。
        run = session.scalar(
            select(AgentRun)
            .where(AgentRun.run_id == job.run_id)
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise PublishStateUnknownError("RUN_NOT_FOUND")
        archive_id = run.input_json.get("archive_id")
        snapshot_id = run.input_json.get("snapshot_id")
        epoch = run.input_json.get("generation_epoch")
        if (
            not isinstance(archive_id, str)
            or not isinstance(snapshot_id, str)
            or isinstance(epoch, bool)
            or not isinstance(epoch, int)
        ):
            # 没有权威发布引用（archive/snapshot/epoch）就不删。
            raise PublishStateUnknownError("PUBLISH_REFERENCE_INVALID")
        if job.generation_epoch != epoch:
            # 账本行属于旧代：探测的不是这代对象，身份对不上 → 未知。
            raise PublishStateUnknownError("GENERATION_EPOCH_MISMATCH")
        # 与 runner.py 发布节点完全一致的逻辑键：Business 按此键精确查询。
        logical_key = (
            f"{run.run_id}:publish_document:"
            f"memory.publish_playback_document:{epoch}"
        )
        # 复用静态生产方法构造冻结 envelope context；校验失败会抛错，
        # 由上层归入未知（fail-closed）。
        tool_context = ToolGateway.build_tool_context(run, "orphan-maintenance")
        # 注意：get_publish_result 的 tool_context 是 keyword-only 形参
        # （*scope_and_key 之后），必须关键字传递；runner.py:888/926/967
        # 的 7 位置参数写法按当前签名会抛 TOOL_PUBLISH_RESULT_INVALID，
        # 这里以 gateway 代码事实为准。
        return gateway.get_publish_result(
            run.business_connector_id,
            archive_id,
            snapshot_id,
            run.run_id,
            epoch,
            logical_key,
            tool_context=tool_context,
        )

    return probe


def build_production_publish_probe(
    session: Session,
    *,
    tool_client: httpx.Client | None = None,
) -> PublishProbe:
    """CLI 生产装配：从 settings 装配真实 ToolGateway 构建探测口。

    装配镜像 app/worker.py 的生产路径：settings.business_connectors 中
    enabled 且 base_url/runtime_id/key_id/secret 四要素齐全的 connector
    才可用；生产 transport 用 PeerTrackingHTTPTransport（ToolGateway 发包
    前要复核 TCP 对端，缺它会被 BUSINESS_CONNECTOR_PEER_UNVERIFIABLE 拒绝）；
    allow_private_endpoints 取 settings 同名开关。tool_client 仅用于测试/
    演练在传输层注入 MockTransport——网关、签名、分类仍是真实代码路径，
    不存在测试专用 probe。
    """
    from app.core.config import settings
    from app.runtime.peer_tracking_transport import PeerTrackingHTTPTransport
    from app.runtime.tool_gateway import BusinessConnector, ToolGateway

    connectors: dict[str, BusinessConnector] = {}
    for connector_id, config in settings.business_connectors.items():
        if not isinstance(config, dict):
            continue
        required = ("base_url", "runtime_id", "key_id", "secret")
        if not bool(config.get("enabled")) or any(
            not isinstance(config.get(key), str) or not config.get(key)
            for key in required
        ):
            logger.warning(
                "维护命令跳过配置不完整的 connector，"
                "code=MEMOIR_AUDIO_CONNECTOR_SKIPPED"
            )
            continue
        connectors[connector_id] = BusinessConnector(
            base_url=cast(str, config["base_url"]),
            runtime_id=cast(str, config["runtime_id"]),
            key_id=cast(str, config["key_id"]),
            secret=cast(str, config["secret"]),
        )
    if not connectors:
        raise MemoirAudioJobsError(
            "MEMOIR_AUDIO_CONNECTOR_UNAVAILABLE",
            "没有 enabled 且配置完整的业务 connector，无法装配发布探测",
        )
    if tool_client is None:
        # 生产路径：对端复核 transport（无代理、单连接，暴露真实 TCP 对端）。
        peer_transport = PeerTrackingHTTPTransport()
        client = httpx.Client(transport=peer_transport, trust_env=False)
        peer_ip_provider = peer_transport.peer_ip
        reset_peer_ip = peer_transport.reset_peer_ip
    else:
        # 测试/演练：仅替换传输层，peer 复核交给 MockTransport 语义跳过。
        client = tool_client
        peer_ip_provider = None
        reset_peer_ip = None
    gateway = ToolGateway(
        connectors,
        client,
        allow_private_endpoints=(
            settings.RUNTIME_TOOL_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS
        ),
        peer_ip_provider=peer_ip_provider,
        reset_peer_ip=reset_peer_ip,
    )
    return build_publish_probe(session, gateway)



@dataclass(frozen=True)
class MaintenanceReport:
    """一轮维护的计数汇总：只含计数，不含对象键等敏感值。"""

    scanned: int = 0  # 本批扫描到的孤儿候选数（受 --limit 约束）
    keep_in_flight: int = 0  # lease 仍有效：作业在途，保留
    keep_ledger_pending: int = 0  # submission_unknown：账本未清，保留对账
    keep_published: int = 0  # 探测确认已发布：移交 Business 生命周期，保留
    keep_unknown: int = 0  # 探测失败或未注入探测口：发布状态未知，保留人工复核
    keep_within_retention: int = 0  # 保留窗内：尚未到清理时点
    delete_candidates: int = 0  # 判定为可删除的候选数
    deleted: int = 0  # execute 模式下实际完成清理（含 404）的数量
    delete_failed: int = 0  # 删除抛异常的数量（候选保留原状态，下轮重试）


def run_maintenance(
    session: Session,
    *,
    oss_deleter: OssDeleter,
    retention_hours: int,
    limit: int,
    execute: bool,
    now: datetime | None = None,
    publish_probe: PublishProbe | None = None,
) -> MaintenanceReport:
    """扫描一批孤儿候选并分类；execute=True 时删除明确未发布且超窗对象。

    R5：发布状态由 publish_probe 向 Business 权威探测（三态：dict=已发布/
    None=明确未发布/异常=未知），绝不查 Runtime 本地发布投影表。

    分类顺序（先命中先保留，兜底才是删除）：
    1. lease 未过期 → 在途，保留；
    2. submission_unknown → 账本未清（预留未对账），保留；
    3. 探测返回 dict 且 audio_object_keys 含该键 → 已引用，保留；
       dict 但未引用 / 探测返回 None（明确未发布）→ 落入④窗检查；
    4. 仍在保留窗内 → 保留；
    5. 明确未发布或已发布但未引用该对象 → 超窗可删（唯一删除路径）；
    6. 探测失败或未注入探测口 → 状态未知，保留人工复核，绝不自动删。

    execute=True 时先 fail_abandoned_keyed_jobs（grace=保留窗），再扫描，
    本轮收割的 failed 行立刻进入视野。dry-run 零写入（含不 reap）。
    """
    moment = now or datetime.now(UTC)
    service = MemoirAudioJobsService(session)
    # 先收割过窗持键 active，再 list：failed ∈ _ORPHAN_STATES。
    # dry-run 连 reap 也不跑，保证零写入。
    if execute:
        service.fail_abandoned_keyed_jobs(
            now=moment,
            grace_seconds=float(retention_hours) * 3600.0,
        )
    candidates = service.list_orphan_candidates(limit=limit)
    report = MaintenanceReport(scanned=len(candidates))
    retention_edge = moment - timedelta(hours=retention_hours)

    for job in candidates:
        # 1. 在途：lease 未过期，别的 worker 可能正在写。
        if job.expires_at is not None and _as_aware(job.expires_at) > moment:
            report = _bump(report, "keep_in_flight")
            continue
        # 2. 账本未清：submission_unknown 的预留尚未对账，禁止清理。
        if job.state == STATE_SUBMISSION_UNKNOWN:
            report = _bump(report, "keep_ledger_pending")
            continue
        # 3. 发布探测。未注入探测口 ≠ probe 返回 None（明确未发布 404）。
        if publish_probe is None:
            probe: object = _PROBE_UNKNOWN
        else:
            try:
                probe = publish_probe(job)
            except Exception:  # noqa: BLE001 探测任何失败都按未知兜底
                probe = _PROBE_UNKNOWN
        if isinstance(probe, dict):
            # 缺 audio_object_keys 按空列表：兼容旧 Business 包。
            keys = probe.get("audio_object_keys", [])
            if not isinstance(keys, list):
                keys = []
            if job.object_key in keys:
                report = _bump(report, "keep_published")
                continue
            # 图文已发布但该对象未被引用：落入④窗检查。
        # probe is None → 明确未发布，落入④窗检查。
        # probe is _PROBE_UNKNOWN → 先④窗，超窗再⑥，不删。
        # 4. 保留窗内：无论后续判定为何，都未到清理时点。
        if _as_aware(job.updated_at) > retention_edge:
            report = _bump(report, "keep_within_retention")
            continue
        # 5. 唯一删除路径：明确未发布，或已发布但未引用该对象。
        if probe is None or isinstance(probe, dict):
            report = _bump(report, "delete_candidates")
            if not execute:
                continue
            try:
                # False=404/NoSuchKey：对象不存在同样视为清理成功。
                oss_deleter.delete_object(job.object_key or "")
            except Exception:
                logger.warning(
                    "Memoir 音频孤儿删除失败，code=MEMOIR_AUDIO_DELETE_FAILED，"
                    "job_state=%s",
                    job.state,
                )
                report = _bump(report, "delete_failed")
                continue
            service.mark_cleaned(job.job_id)
            report = _bump(report, "deleted")
            continue
        # 6. 探测未注入或失败：状态未知，保守保留。
        report = _bump(report, "keep_unknown")

    logger.info(
        "Memoir 音频维护批次完成，code=MEMOIR_AUDIO_MAINTENANCE_DONE，"
        "scanned=%d，deleted=%d，delete_failed=%d，execute=%s",
        report.scanned,
        report.deleted,
        report.delete_failed,
        execute,
    )
    return report


def _bump(report: MaintenanceReport, field_name: str) -> MaintenanceReport:
    """不可变递增报告计数（dataclass frozen，返回新对象）。"""
    return MaintenanceReport(
        **{**report.__dict__, field_name: getattr(report, field_name) + 1}
    )


def build_parser() -> argparse.ArgumentParser:
    """维护命令参数合同：环境必选二选一、dry-run/execute 互斥、limit 默认 100。"""
    parser = argparse.ArgumentParser(
        prog="memoir-audio-maintenance",
        description="Memoir 音频孤儿资产维护：扫描分类（dry-run）或执行删除（execute）",
    )
    parser.add_argument(
        "--environment",
        required=True,
        choices=("test", "production"),
        help="目标 Runtime 环境（只允许 test/production，开发库不跑维护）",
    )
    mutex = parser.add_mutually_exclusive_group()
    # 互斥组共用 dest=execute：--dry-run 置 False（默认），--execute 置 True。
    mutex.add_argument(
        "--dry-run", dest="execute", action="store_false",
        help="只扫描分类并打印计数，不做任何删除（默认）",
    )
    mutex.add_argument(
        "--execute", dest="execute", action="store_true",
        help="真正执行删除（仅明确未发布且超保留窗的对象）",
    )
    parser.set_defaults(execute=False)
    parser.add_argument(
        "--limit", type=int, default=100,
        help="本批扫描候选上限（默认 100，剩余留给下一批）",
    )
    return parser


def require_runtime_database(url: str) -> None:
    """守卫：目标库必须属于 agent_runtime，禁止把维护指向业务库。"""
    from sqlalchemy.engine import make_url

    database = make_url(url).database or ""
    if "agent_runtime" not in database:
        raise MemoirAudioJobsError(
            "MEMOIR_AUDIO_DB_FORBIDDEN",
            "维护命令只允许连接 agent_runtime 库",
        )


class AliyunAudioOSSDeleter:
    """真实 OSS 删除口：懒加载 SDK；404/NoSuchKey 归一为 False。"""

    def __init__(
        self,
        *,
        access_key_id: str,
        access_key_secret: str,
        bucket: str,
        endpoint: str,
    ) -> None:
        if not access_key_id or not access_key_secret or not bucket or not endpoint:
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_DELETE_CONFIG_INVALID", "OSS 凭据/桶/endpoint 未配置"
            )
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._bucket = bucket
        self._endpoint = endpoint
        # OSS SDK 无类型标注（import-untyped），懒加载句柄统一按 Any 处理，
        # 避免 object 类型导致成员访问被 mypy 拒绝。
        self._client: Any | None = None
        self._oss_module: Any | None = None

    def _ensure_client(self) -> tuple[Any, Any]:
        """懒加载 OSS SDK 并复用客户端；凭据绝不写日志。"""
        if self._client is not None:
            return self._client, self._oss_module
        import alibabacloud_oss_v2 as oss  # type: ignore[import-untyped]

        credentials_provider = oss.credentials.StaticCredentialsProvider(
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
        )
        cfg = oss.config.load_default()
        cfg.credentials_provider = credentials_provider
        cfg.endpoint = self._endpoint
        self._client = oss.Client(cfg)
        self._oss_module = oss
        return self._client, self._oss_module

    def delete_object(self, object_key: str) -> bool:
        """删除对象：True=已删；False=404/NoSuchKey（本就不存在）。"""
        client, oss = self._ensure_client()
        try:
            client.delete_object(
                oss.DeleteObjectRequest(bucket=self._bucket, key=object_key)
            )
            return True
        except Exception as exc:
            message = str(exc)
            if "NoSuchKey" in message or "404" in message:
                return False
            # 异常原文可能含签名/内部细节，绝不进错误文本。
            raise MemoirAudioJobsError(
                "MEMOIR_AUDIO_DELETE_FAILED", "OSS 对象删除失败"
            ) from exc


def main(
    argv: list[str] | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    oss_deleter: OssDeleter | None = None,
    publish_probe: PublishProbe | None = None,
    tool_client: httpx.Client | None = None,
) -> int:
    """CLI 入口：注入模式供测试演练；真实路径先切环境再建引擎。

    R5：未显式传入 publish_probe 时不再留"无探测口"的 fail-open 默认，
    而是走 build_production_publish_probe 从 settings 装配真实
    ToolGateway 探测口——生产 CLI 直接就是这条真实路径。tool_client
    仅替换传输层（测试演练注入 MockTransport），不注入测试专用 probe。
    返回码：0=本轮完成（含部分删除失败，见报告计数）；2=配置/守卫拒绝。
    """
    args = build_parser().parse_args(argv)
    if session_factory is None or oss_deleter is None:
        # 真实路径：先设置 ENVIRONMENT 再导入配置（config 在导入时求值环境）。
        os.environ["ENVIRONMENT"] = args.environment
        from sqlalchemy import create_engine

        from app.config.database_config import get_database_url
        from app.core.config import settings

        try:
            url = get_database_url()
            require_runtime_database(url)
            if oss_deleter is None:
                oss_deleter = AliyunAudioOSSDeleter(
                    access_key_id=settings.MEMORY_AUDIO_OSS_ACCESS_KEY_ID,
                    access_key_secret=settings.MEMORY_AUDIO_OSS_ACCESS_KEY_SECRET,
                    bucket=settings.MEMORY_AUDIO_OSS_BUCKET,
                    endpoint=settings.MEMORY_AUDIO_OSS_ENDPOINT,
                )
        except MemoirAudioJobsError as exc:
            print(f"维护命令拒绝执行：{exc.code}")
            return 2
        session_factory = sessionmaker(bind=create_engine(url))

    # 保留窗从部署配置读；配置不可用时退回默认 24 小时，不阻断扫描。
    try:
        from app.core.config import settings as _settings

        retention_hours = int(_settings.MEMOIR_AUDIO_ORPHAN_RETENTION_HOURS)
    except Exception:  # noqa: BLE001
        retention_hours = 24

    with session_factory() as session:
        if publish_probe is None:
            # R5：CLI 生产装配真实探测口（settings → ToolGateway → 镜像
            # 发布形状的 probe）；装配失败按配置拒绝处理，绝不降级为
            # "无探测口继续跑"——那会把未知态悄悄扩大。
            try:
                publish_probe = build_production_publish_probe(
                    session, tool_client=tool_client
                )
            except MemoirAudioJobsError as exc:
                print(f"维护命令拒绝执行：{exc.code}")
                return 2
        report = run_maintenance(
            session,
            oss_deleter=oss_deleter,
            retention_hours=retention_hours,
            limit=args.limit,
            execute=args.execute,
            publish_probe=publish_probe,
        )
        session.commit()
    print(
        f"扫描 {report.scanned}：在途 {report.keep_in_flight}，账本未清 "
        f"{report.keep_ledger_pending}，已发布 {report.keep_published}，未知 "
        f"{report.keep_unknown}，窗内 {report.keep_within_retention}，删除候选 "
        f"{report.delete_candidates}，已清理 {report.deleted}，删除失败 "
        f"{report.delete_failed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
