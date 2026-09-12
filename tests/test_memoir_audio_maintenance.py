"""M8 R5 音频孤儿维护测试：publish_probe 三态分类 + 真实网关 wire 镜像。

R5 核心断言：
1. 孤儿清理不得查 Runtime 本地发布投影表，发布状态一律由探测口
   （Business memory.get_publish_result）三态判定；run_maintenance 层
   未注入探测口时 fail-safe 只报告不删除。
2. 探测调用逐字镜像生产发布节点（runner.py publish_document）：身份取自
   权威 AgentRun.input_json、logical_key 与 runner 完全一致、4 字段 wire、
   orphan-maintenance 的冻结 tool context。
3. CLI main() 生产装配真实 ToolGateway：测试只在传输层注入
   httpx.MockTransport 拦截 HTTP——签名、网关校验、分类全部真实执行，
   不注入任何测试专用 probe。
全部使用 SQLite 内存库与假删除口；不触真实库、不发真实网络请求、
不删真实对象。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.core.tool_security import tool_signature
from app.models import AgentRun
from app.models.memoir_audio_job import (
    NO_SEGMENT_INDEX,
    WORK_SCENE_ID,
    MemoirAudioJob,
)
from app.runtime.tool_gateway import BusinessConnector, ToolGateway
from app.scripts.memoir_audio_maintenance import (
    PublishStateUnknownError,
    build_production_publish_probe,
    build_publish_probe,
    run_maintenance,
)
from app.services.memoir.memoir_audio_jobs import (
    ROLE_BACKGROUND_MUSIC,
    ROLE_NARRATION,
    STATE_FAILED,
    STATE_RESERVED,
    STATE_SUBMISSION_UNKNOWN,
    STATE_UPLOADED,
)

# 测试 connector 身份：与 MockTransport 配套，绝不指向真实 endpoint。
_CONNECTOR_ID = "couple_diary_backend"
_RUNTIME_ID = "runtime-test"
_KEY_ID = "key-test"
_SECRET = "runtime-tool-test-secret"

# Handler 类型：MockTransport 拦截器，按 Business 端点契约回包。
_Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    """SQLite 内存库：只建 Runtime 元数据表，不连任何真实数据库。"""
    import app.models  # noqa: F401 确保全部模型注册进 Base.metadata
    from app.db.sqlalchemy_db import Base

    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_agent_run(
    session: Session,
    *,
    run_id: str = "run-1",
    archive_id: str = "archive-1",
    snapshot_id: str = "snap-1",
    epoch: int = 3,
    input_json: dict[str, Any] | None = None,
) -> None:
    """播种权威 AgentRun：身份字段与生产 Run 一致。

    business_id 必须等于 input_json.archive_id——这是 gateway
    _validated_tool_context 的冻结校验（context.business_id == archive_id），
    生产 Run 天然满足（发布节点同样依赖该不变量）。
    """
    if input_json is None:
        input_json = {
            "archive_id": archive_id,
            "snapshot_id": snapshot_id,
            "generation_epoch": epoch,
        }
    session.add(
        AgentRun(
            run_id=run_id,
            agent_id="memoir_agent",
            agent_version="1.0.8",
            package_digest="digest-test",
            contract_version="1.1.0",
            business_type="couple_memory",
            business_id=archive_id,
            input_json=input_json,
            authorization_version=1,
            caller_id="caller-1",
            tenant_id="couple-diary",
            create_idempotency_key=f"idem-{run_id}",
            callback_target_id="memory_callback",
            business_connector_id=_CONNECTOR_ID,
            trace_id=f"trace-{run_id}",
            run_deadline_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    session.commit()


def _seed_orphan(
    session: Session,
    *,
    scene_id: str,
    key: str,
    run_id: str = "run-1",
    state: str = STATE_UPLOADED,
    age_hours: int = 30,
    lease_active: bool = False,
) -> MemoirAudioJob:
    """直接造出维护命令要扫描的孤儿作业行（绕过服务以构造终态）。

    state 构造时直接给终值：避免 Core UPDATE 触发 updated_at onupdate
    把回拨的年龄重置。
    """
    job = MemoirAudioJob(
        job_id=f"job-m-{scene_id}",
        business_id="biz-1",
        run_id=run_id,
        generation_epoch=3,
        package_version="1.0.8",
        role=ROLE_NARRATION,
        scene_id=scene_id,
        segment_index=NO_SEGMENT_INDEX,
        input_hmac=f"hmac-{scene_id}",
        attempt=1,
        state=state,
        object_key=key,
        mime="audio/mpeg",
        reserved_cost=Decimal("0.15"),
        currency="CNY",
        lease_owner="worker-a",
        lease_token=1,
        expires_at=(
            datetime.now(UTC) + timedelta(hours=1) if lease_active else None
        ),
        updated_at=datetime.now(UTC) - timedelta(hours=age_hours),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


class _FakeDeleter:
    """假删除口：记录被要求删除的键，绝不触网。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_object(self, object_key: str) -> bool:
        self.deleted.append(object_key)
        return True


class _FakeProbe:
    """假探测口（三态）：published 命中返回 dict；unpublished 命中返回
    None；其余抛异常模拟网关未知。仅供 run_maintenance 单元测试注入。"""

    def __init__(
        self,
        published: frozenset[str] = frozenset(),
        unpublished: frozenset[str] = frozenset(),
    ) -> None:
        self.published = published
        self.unpublished = unpublished

    def __call__(self, job: MemoirAudioJob) -> dict[str, Any] | None:
        key = job.object_key or ""
        if key in self.published:
            # C1：keep_published 只认成员关系，必须显式带上该键。
            return {"document_id": "doc-1", "audio_object_keys": [key]}
        if key in self.unpublished:
            return None
        raise RuntimeError("probe unavailable")


# ---------------------------------------------------------------------------
# 真实网关 + MockTransport 基础设施：传输层拦截，代码路径全真实
# ---------------------------------------------------------------------------


def _mock_client(handler: _Handler) -> tuple[httpx.Client, list[httpx.Request]]:
    """构造 MockTransport 客户端并记录发出的请求（供 wire 形状断言）。"""
    requests: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return (
        httpx.Client(transport=httpx.MockTransport(_record), trust_env=False),
        requests,
    )


def _real_gateway(client: httpx.Client) -> ToolGateway:
    """真实 ToolGateway：真实签名/校验/发包逻辑，仅 HTTP 被拦截。"""
    return ToolGateway(
        {
            _CONNECTOR_ID: BusinessConnector(
                base_url="http://business.test",
                runtime_id=_RUNTIME_ID,
                key_id=_KEY_ID,
                secret=_SECRET,
            )
        },
        client,
        # 测试主机名非公网：放行私网 endpoint，跳过 DNS/对端复核。
        # 传输层由 MockTransport 拦截，全程不出网。
        allow_private_endpoints=True,
    )


def _published_handler(request: httpx.Request) -> httpx.Response:
    """Business 原键权威命中：冻结摘要 + C1 audio_object_keys。"""
    return httpx.Response(
        200,
        json={
            "output": {
                "revision": 2,
                "content_digest": "ab" * 32,
                "audio_object_keys": ["memoir-test/rg-pub.mp3"],
            },
            "schema_version": "1.0.0",
        },
    )


def _not_yet_observed_handler(request: httpx.Request) -> httpx.Response:
    """Business 原键未命中：404 + PUBLISH_NOT_YET_OBSERVED（v1.1.0 冻结规格，
    镜像 Business memory_agent_tools_api 的 get_publish_result 端点）。"""
    return httpx.Response(
        404,
        json={
            "error_code": "PUBLISH_NOT_YET_OBSERVED",
            "error_type": "publish_not_observed",
            "retryable": False,
            "safe_message": "尚未观察到发布结果",
            "details_visible_to_model": False,
        },
    )


# ---------------------------------------------------------------------------
# run_maintenance 三态分类（探测口注入点；契约与生产一致）
# ---------------------------------------------------------------------------


def test_probe_published_keeps_object(session_factory: sessionmaker[Session]) -> None:
    """①探测返回 dict → 已发布：keep_published 不删（execute 也不删）。"""
    with session_factory() as session:
        _seed_orphan(session, scene_id="s-pub", key="memoir-test/published.mp3")
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,  # 即使 execute 也不得删已发布对象
            publish_probe=_FakeProbe(published=frozenset({"memoir-test/published.mp3"})),
        )
        assert report.keep_published == 1
        assert report.deleted == 0
        assert deleter.deleted == []


def test_probe_unpublished_past_retention_deletes(
    session_factory: sessionmaker[Session],
) -> None:
    """②探测返回 None 且超保留窗 → 明确未发布：进删除候选并真删。"""
    with session_factory() as session:
        job = _seed_orphan(session, scene_id="s-old", key="memoir-test/old.mp3")
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=_FakeProbe(unpublished=frozenset({"memoir-test/old.mp3"})),
        )
        assert report.delete_candidates == 1
        assert report.deleted == 1
        assert deleter.deleted == ["memoir-test/old.mp3"]
        # mark_cleaned 改写的是同一 Session 身份映射中的实例（未 flush），
        # 直接断言内存态即可；refresh 反而会读回未落库的旧值。
        assert job.state == "cleaned"


def test_probe_unpublished_within_retention_kept(
    session_factory: sessionmaker[Session],
) -> None:
    """②补：明确未发布但仍在保留窗内 → keep_within_retention 不删。"""
    with session_factory() as session:
        _seed_orphan(session, scene_id="s-fresh", key="memoir-test/fresh.mp3",
                     age_hours=1)
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=_FakeProbe(unpublished=frozenset({"memoir-test/fresh.mp3"})),
        )
        assert report.keep_within_retention == 1
        assert report.deleted == 0


def test_probe_error_keeps_unknown(session_factory: sessionmaker[Session]) -> None:
    """③探测抛异常 → 状态未知：keep_unknown 不删（fail-safe）。"""
    with session_factory() as session:
        _seed_orphan(session, scene_id="s-unk", key="memoir-test/unknown.mp3")
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,  # 即使 execute，未知态也绝不删
            publish_probe=_FakeProbe(),  # 未命中两个集合 → 抛异常
        )
        assert report.keep_unknown == 1
        assert report.deleted == 0
        assert deleter.deleted == []


def test_no_probe_classifies_all_unknown(session_factory: sessionmaker[Session]) -> None:
    """④run_maintenance 未注入探测口 → 全部 keep_unknown 只报告不删除。"""
    with session_factory() as session:
        # 三种历史形态：uploaded / failed / submission_unknown 各一。
        _seed_orphan(session, scene_id="s-up", key="memoir-test/up.mp3")
        _seed_orphan(session, scene_id="s-fail", key="memoir-test/fail.mp3",
                     state="failed")
        _seed_orphan(session, scene_id="s-led",
                     key="memoir-test/pending.mp3",
                     state=STATE_SUBMISSION_UNKNOWN)
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
        )
        # 账本未清优先拦截；其余未注入探测口全部落入未知。
        assert report.keep_ledger_pending == 1
        assert report.keep_unknown == 2
        assert report.deleted == 0
        assert deleter.deleted == []


def test_lease_alive_keeps_in_flight(session_factory: sessionmaker[Session]) -> None:
    """⑤回归：lease 未过期仍 keep_in_flight，探测口不被调用。"""
    with session_factory() as session:
        _seed_orphan(session, scene_id="s-fly", key="memoir-test/inflight.mp3",
                     lease_active=True)
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=_FakeProbe(unpublished=frozenset({"memoir-test/inflight.mp3"})),
        )
        assert report.keep_in_flight == 1
        assert report.deleted == 0
        assert deleter.deleted == []


# ---------------------------------------------------------------------------
# R5 新契约：真实 ToolGateway 代码路径（MockTransport 只拦传输层）
# ---------------------------------------------------------------------------


def test_probe_mirrors_publish_wire_shape(
    session_factory: sessionmaker[Session],
) -> None:
    """探测 wire 逐字镜像生产发布节点：路径、四字段 payload、
    orphan-maintenance context、真实签名头、原发布幂等键派生。"""
    with session_factory() as session:
        _seed_agent_run(session)
        job = _seed_orphan(session, scene_id="s-wire", key="memoir-test/wire.mp3")
        client, requests = _mock_client(_published_handler)
        probe = build_publish_probe(session, _real_gateway(client))

        assert probe(job) == {
            "revision": 2,
            "content_digest": "ab" * 32,
            "audio_object_keys": ["memoir-test/rg-pub.mp3"],
        }
        (request,) = requests
        # 请求形状按 gateway 实现事实断言（先读实现再断言，不猜）。
        assert request.method == "POST"
        assert request.url.path == (
            "/api/v1/internal/agent-tools/memory.get_publish_result"
        )
        body = json.loads(request.read())
        assert set(body) == {"input", "context"}  # ToolRequest 信封
        # 4 字段 wire（非 legacy 2 字段），身份全部来自权威 Run.input_json。
        assert body["input"] == {
            "archive_id": "archive-1",
            "snapshot_id": "snap-1",
            "run_id": "run-1",
            "generation_epoch": 3,
        }
        # 冻结 envelope context 7 字段，step_id 固定 orphan-maintenance。
        assert body["context"] == {
            "agent_id": "memoir_agent",
            "agent_version": "1.0.8",
            "run_id": "run-1",
            "step_id": "orphan-maintenance",
            "business_type": "couple_memory",
            "business_id": "archive-1",
            "trace_id": "trace-run-1",
        }
        headers = request.headers
        assert headers["X-Agent-Runtime-Id"] == _RUNTIME_ID
        assert headers["X-Agent-Key-Id"] == _KEY_ID
        assert headers["X-Agent-Tool-Name"] == "memory.get_publish_result"
        assert headers["X-Agent-Run-Id"] == "run-1"
        assert headers["X-Agent-Tool-Contract-Version"] == "1.1.0"
        # 签名真实计算：与 tool_security 用同 METHOD/path/timestamp/body 重算一致。
        assert headers["X-Agent-Signature"] == tool_signature(
            "POST",
            request.url.path,
            headers["X-Agent-Timestamp"],
            request.read(),
            _SECRET,
        )
        # 幂等键 = 原发布 logical_key 的 wire 派生（含冒号不合规 → sha256 hex），
        # 绝不是旧实现自造的 orphan-maintenance:{job_id} 键。
        logical_key = (
            "run-1:publish_document:memory.publish_playback_document:3"
        )
        assert headers["Idempotency-Key"] == hashlib.sha256(
            logical_key.encode()
        ).hexdigest()


def _legacy_two_field_handler(request: httpx.Request) -> httpx.Response:
    """旧 Business 包：只返回 revision/content_digest，缺 audio_object_keys。"""
    return httpx.Response(
        200,
        json={
            "output": {
                "revision": 2,
                "content_digest": "ab" * 32,
            },
            "schema_version": "1.0.0",
        },
    )


def _sensitive_keys_handler(request: httpx.Request) -> httpx.Response:
    """新三字段但对象键命中敏感标识符：网关必须拒绝，探测归未知。"""
    return httpx.Response(
        200,
        json={
            "output": {
                "revision": 2,
                "content_digest": "ab" * 32,
                "audio_object_keys": ["13800138000"],
            },
            "schema_version": "1.0.0",
        },
    )


def test_real_gateway_legacy_two_field_keeps_unknown(
    session_factory: sessionmaker[Session],
) -> None:
    """旧 Business 两字段响应：真实网关接受摘要，维护按未知保留，零删除。"""
    with session_factory() as session:
        _seed_agent_run(session)
        job = _seed_orphan(
            session, scene_id="s-rg-legacy", key="memoir-test/rg-legacy.mp3",
        )
        deleter = _FakeDeleter()
        client, _ = _mock_client(_legacy_two_field_handler)
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=build_publish_probe(session, _real_gateway(client)),
        )
        assert report.keep_unknown == 1
        assert report.deleted == 0
        assert deleter.deleted == []
        assert job.state == STATE_UPLOADED


def test_real_gateway_sensitive_keys_keep_unknown(
    session_factory: sessionmaker[Session],
) -> None:
    """新增字段含敏感标识符：真实网关拒绝 → 探测异常 → keep_unknown。"""
    with session_factory() as session:
        _seed_agent_run(session)
        job = _seed_orphan(
            session, scene_id="s-rg-sens", key="memoir-test/rg-sens.mp3",
        )
        deleter = _FakeDeleter()
        client, _ = _mock_client(_sensitive_keys_handler)
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=build_publish_probe(session, _real_gateway(client)),
        )
        assert report.keep_unknown == 1
        assert report.deleted == 0
        assert deleter.deleted == []
        assert job.state == STATE_UPLOADED


def test_real_gateway_published_keeps_object(
    session_factory: sessionmaker[Session],
) -> None:
    """①Business 原键命中（dict）→ keep_published 零删除，对象 state 不变。"""
    with session_factory() as session:
        _seed_agent_run(session)
        job = _seed_orphan(session, scene_id="s-rg-pub",
                           key="memoir-test/rg-pub.mp3")
        deleter = _FakeDeleter()
        client, _ = _mock_client(_published_handler)
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=build_publish_probe(session, _real_gateway(client)),
        )
        assert report.keep_published == 1
        assert report.deleted == 0
        assert deleter.deleted == []
        assert job.state == STATE_UPLOADED  # 已发布对象原样保留


def test_real_gateway_unpublished_past_retention_deletes(
    session_factory: sessionmaker[Session],
) -> None:
    """③404 PUBLISH_NOT_YET_OBSERVED → None（原键权威未命中）且超窗 → 删除。"""
    with session_factory() as session:
        _seed_agent_run(session)
        job = _seed_orphan(session, scene_id="s-rg-old",
                           key="memoir-test/rg-old.mp3")
        deleter = _FakeDeleter()
        client, _ = _mock_client(_not_yet_observed_handler)
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=build_publish_probe(session, _real_gateway(client)),
        )
        assert report.delete_candidates == 1
        assert report.deleted == 1
        assert deleter.deleted == ["memoir-test/rg-old.mp3"]
        assert job.state == "cleaned"


def test_real_gateway_unpublished_within_retention_kept(
    session_factory: sessionmaker[Session],
) -> None:
    """④404 → None 但仍在保留窗内 → keep_within_retention 不删。"""
    with session_factory() as session:
        _seed_agent_run(session)
        _seed_orphan(session, scene_id="s-rg-fresh",
                     key="memoir-test/rg-fresh.mp3", age_hours=1)
        deleter = _FakeDeleter()
        client, _ = _mock_client(_not_yet_observed_handler)
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=build_publish_probe(session, _real_gateway(client)),
        )
        assert report.keep_within_retention == 1
        assert report.deleted == 0
        assert deleter.deleted == []


def test_real_gateway_5xx_keeps_unknown(
    session_factory: sessionmaker[Session],
) -> None:
    """②Business 5xx（错误响应形状不合法）→ 探测失败 → keep_unknown 零删除。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"unexpected": "shape"})

    with session_factory() as session:
        _seed_agent_run(session)
        job = _seed_orphan(session, scene_id="s-rg-5xx",
                           key="memoir-test/rg-5xx.mp3")
        deleter = _FakeDeleter()
        client, _ = _mock_client(_handler)
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=build_publish_probe(session, _real_gateway(client)),
        )
        assert report.keep_unknown == 1
        assert report.deleted == 0
        assert deleter.deleted == []
        assert job.state == STATE_UPLOADED


def test_real_gateway_connect_error_keeps_unknown(
    session_factory: sessionmaker[Session],
) -> None:
    """②传输层连接失败 → 探测失败 → keep_unknown 零删除。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with session_factory() as session:
        _seed_agent_run(session)
        _seed_orphan(session, scene_id="s-rg-conn",
                     key="memoir-test/rg-conn.mp3")
        deleter = _FakeDeleter()
        client, _ = _mock_client(_handler)
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=build_publish_probe(session, _real_gateway(client)),
        )
        assert report.keep_unknown == 1
        assert report.deleted == 0
        assert deleter.deleted == []


def test_probe_identity_gaps_keep_unknown(
    session_factory: sessionmaker[Session],
) -> None:
    """②Run 不存在 / archive_id 缺失 / epoch 不匹配 → 一律 keep_unknown。

    三种情况探测都在发包前被身份校验挡下（请求根本不出）——即使
    Business 对探测键会回"未发布"，身份对不上也绝不进入删除路径。
    """
    for case in ("run_missing", "archive_missing", "epoch_mismatch"):
        with session_factory() as session:
            # 同一内存库跑三轮：先清上一轮的 Run/作业，隔离各身份缺陷场景。
            session.execute(sa.delete(MemoirAudioJob))
            session.execute(sa.delete(AgentRun))
            session.commit()
            if case == "archive_missing":
                _seed_agent_run(
                    session,
                    input_json={"snapshot_id": "snap-1", "generation_epoch": 3},
                )
            elif case == "epoch_mismatch":
                _seed_agent_run(session, epoch=4)  # 账本行 epoch=3，跨代
            # run_missing：不播种 AgentRun
            job = _seed_orphan(session, scene_id=f"s-{case}",
                               key=f"memoir-test/{case}.mp3")
            deleter = _FakeDeleter()
            client, requests = _mock_client(_not_yet_observed_handler)
            report = run_maintenance(
                session,
                oss_deleter=deleter,
                retention_hours=24,
                limit=100,
                execute=True,
                publish_probe=build_publish_probe(session, _real_gateway(client)),
            )
            assert requests == [], case  # 身份校验挡在发包前
            assert report.keep_unknown == 1, case
            assert report.deleted == 0, case
            assert deleter.deleted == [], case
            assert job.state == STATE_UPLOADED, case


def test_probe_raises_identity_error_codes(
    session_factory: sessionmaker[Session],
) -> None:
    """身份缺失的探测错误码可断言（安全枚举，无敏感值）。"""
    with session_factory() as session:
        job = _seed_orphan(session, scene_id="s-code",
                           key="memoir-test/code.mp3")
        client, _ = _mock_client(_published_handler)
        probe = build_publish_probe(session, _real_gateway(client))
        with pytest.raises(PublishStateUnknownError) as exc_info:
            probe(job)  # 未播种 AgentRun → RUN_NOT_FOUND
        assert exc_info.value.code == "RUN_NOT_FOUND"


# ---------------------------------------------------------------------------
# CLI main() 生产装配：settings → 真实 ToolGateway（仅传输层可替换）
# ---------------------------------------------------------------------------


def test_cli_assembles_production_probe_via_mock_transport(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⑥不注入 probe 时 main() 走生产装配：settings.business_connectors →
    真实 ToolGateway → 镜像发布形状的探测；MockTransport 只替换传输层。"""
    from app.core.config import settings
    from app.scripts import memoir_audio_maintenance as cli

    monkeypatch.setattr(
        settings,
        "RUNTIME_BUSINESS_CONNECTORS_JSON",
        json.dumps(
            {
                _CONNECTOR_ID: {
                    "enabled": True,
                    "base_url": "http://business.test",
                    "runtime_id": _RUNTIME_ID,
                    "key_id": _KEY_ID,
                    "secret": _SECRET,
                },
                # 配置不完整的 connector 必须被装配跳过，不影响可用项。
                "broken_one": {"enabled": True, "base_url": "http://x.test"},
            }
        ),
    )
    # 放行私网 endpoint：MockTransport 主机名非公网，跳过 DNS（全程不出网）。
    monkeypatch.setattr(
        settings, "RUNTIME_TOOL_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS", True
    )

    with session_factory() as session:
        _seed_agent_run(session)
        _seed_orphan(session, scene_id="s-cli", key="memoir-test/cli.mp3")

    deleter = _FakeDeleter()
    client, requests = _mock_client(_not_yet_observed_handler)
    code = cli.main(
        ["--environment", "test", "--execute"],
        session_factory=session_factory,
        oss_deleter=deleter,
        tool_client=client,  # 仅替换传输层；probe 由生产装配构建
    )
    assert code == 0
    # 生产装配真的发出了探测请求（走真实签名/网关路径），并命中"未发布"
    # → 超窗对象被清理、账本标记 cleaned。
    assert len(requests) == 1
    assert requests[0].url.path == (
        "/api/v1/internal/agent-tools/memory.get_publish_result"
    )
    assert deleter.deleted == ["memoir-test/cli.mp3"]
    with session_factory() as session:
        job = session.scalar(sa.select(MemoirAudioJob))
        assert job is not None
        assert job.state == "cleaned"


def test_build_production_probe_reads_settings_connectors(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产装配工厂直接可测：enabled 过滤 + 四要素校验；全不可用则拒绝。"""
    from app.core.config import settings

    monkeypatch.setattr(
        settings,
        "RUNTIME_BUSINESS_CONNECTORS_JSON",
        json.dumps({_CONNECTOR_ID: {"enabled": False}}),
    )
    with session_factory() as session:
        with pytest.raises(Exception) as exc_info:
            build_production_publish_probe(session)
        assert "MEMOIR_AUDIO_CONNECTOR_UNAVAILABLE" in str(exc_info.value)


# ---------------------------------------------------------------------------
# C1：对象成员关系判定 + reaper 接入维护
# ---------------------------------------------------------------------------


class _FixedProbe:
    """返回固定 dict / None 的探测口；用于成员关系与缺字段用例。"""

    def __init__(self, payload: dict[str, Any] | None) -> None:
        self.payload = payload

    def __call__(self, job: MemoirAudioJob) -> dict[str, Any] | None:
        return self.payload


class _ErrorProbe:
    def __call__(self, job: MemoirAudioJob) -> dict[str, Any] | None:
        raise RuntimeError("probe boom")


def test_probe_dict_with_key_keeps_published(
    session_factory: sessionmaker[Session],
) -> None:
    """probe dict 且 keys 含该键 → keep_published 零删除。"""
    with session_factory() as session:
        job = _seed_orphan(session, scene_id="s-ref", key="memoir-test/ref.mp3")
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=_FixedProbe({"audio_object_keys": [job.object_key]}),
        )
        assert report.keep_published == 1
        assert report.deleted == 0
        assert deleter.deleted == []
        assert job.state == STATE_UPLOADED


def test_probe_dict_unreferenced_within_retention_kept(
    session_factory: sessionmaker[Session],
) -> None:
    """图文已发布但该对象未引用：窗内 keep，不删。"""
    with session_factory() as session:
        _seed_orphan(
            session, scene_id="s-fresh-unref",
            key="memoir-test/fresh-unref.mp3", age_hours=1,
        )
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=_FixedProbe({"audio_object_keys": ["memoir-test/other.mp3"]}),
        )
        assert report.keep_within_retention == 1
        assert report.deleted == 0
        assert deleter.deleted == []


def test_probe_dict_unreferenced_past_retention_deletes(
    session_factory: sessionmaker[Session],
) -> None:
    """图文已发布但该对象未引用：超窗才是删除候选；execute 才删。"""
    with session_factory() as session:
        job = _seed_orphan(
            session, scene_id="s-old-unref", key="memoir-test/old-unref.mp3",
        )
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=_FixedProbe({"audio_object_keys": ["memoir-test/other.mp3"]}),
        )
        assert report.delete_candidates == 1
        assert report.deleted == 1
        assert deleter.deleted == ["memoir-test/old-unref.mp3"]
        assert job.state == "cleaned"


def test_probe_none_past_retention_still_deletes(
    session_factory: sessionmaker[Session],
) -> None:
    """probe 404/None：明确未发布，超窗删除（回归）。"""
    with session_factory() as session:
        job = _seed_orphan(session, scene_id="s-none", key="memoir-test/none.mp3")
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=_FixedProbe(None),
        )
        assert report.delete_candidates == 1
        assert report.deleted == 1
        assert job.state == "cleaned"


def test_probe_error_keeps_unknown_zero_delete(
    session_factory: sessionmaker[Session],
) -> None:
    """probe 异常 → keep_unknown 零删除。"""
    with session_factory() as session:
        _seed_orphan(session, scene_id="s-err", key="memoir-test/err.mp3")
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=_ErrorProbe(),
        )
        assert report.keep_unknown == 1
        assert report.deleted == 0
        assert deleter.deleted == []


@pytest.mark.parametrize(
    "payload",
    [
        {"revision": 2, "content_digest": "ab" * 32},
        {"revision": 2, "content_digest": "ab" * 32, "audio_object_keys": None},
        {"revision": 2, "content_digest": "ab" * 32, "audio_object_keys": "memoir-test/miss.mp3"},
        {"revision": 2, "content_digest": "ab" * 32, "audio_object_keys": [1, "memoir-test/miss.mp3"]},
        {"revision": 2, "content_digest": "ab" * 32, "audio_object_keys": ["memoir-test/miss.mp3", None]},
    ],
)
def test_illegal_audio_object_keys_keep_unknown(
    session_factory: sessionmaker[Session],
    payload: dict[str, Any],
) -> None:
    """缺字段 / null / 非 list / 非法元素 → 未知保留，零删除。

    旧 Business 两字段响应不能反向证明对象未被引用；显式合法空列表
    才表示已发布但无音频，由 test_dry_run / 端到端未引用路径覆盖。
    """
    with session_factory() as session:
        job = _seed_orphan(session, scene_id="s-miss", key="memoir-test/miss.mp3")
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=_FixedProbe(payload),
        )
        assert report.keep_unknown == 1
        assert report.deleted == 0
        assert report.delete_candidates == 0
        assert deleter.deleted == []
        assert job.state == STATE_UPLOADED


def test_in_flight_keyed_reserved_not_reaped_or_deleted(
    session_factory: sessionmaker[Session],
) -> None:
    """lease 在途的持键 reserved：不 reap、不删。"""
    with session_factory() as session:
        job = _seed_orphan(
            session, scene_id="s-fly-res", key="memoir-test/fly-res.mp3",
            state=STATE_RESERVED, lease_active=True, age_hours=30,
        )
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            now=datetime.now(UTC),
            publish_probe=_FixedProbe(None),
        )
        assert report.scanned == 0
        assert report.deleted == 0
        assert deleter.deleted == []
        session.refresh(job)
        assert job.state == STATE_RESERVED


def test_dry_run_does_not_reap_or_delete(
    session_factory: sessionmaker[Session],
) -> None:
    """dry-run 零写入：reaper 不执行，超窗未引用也不删。"""
    with session_factory() as session:
        abandoned = _seed_orphan(
            session, scene_id="s-abn", key="memoir-test/abandoned.mp3",
            state=STATE_RESERVED, age_hours=30,
        )
        abandoned.expires_at = datetime.now(UTC) - timedelta(hours=25)
        uploaded = _seed_orphan(
            session, scene_id="s-up", key="memoir-test/stale.mp3", age_hours=30,
        )
        session.commit()
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=False,
            now=datetime.now(UTC),
            publish_probe=_FixedProbe({"audio_object_keys": []}),
        )
        assert report.deleted == 0
        assert deleter.deleted == []
        session.refresh(abandoned)
        session.refresh(uploaded)
        assert abandoned.state == STATE_RESERVED
        assert uploaded.state == STATE_UPLOADED
        assert report.delete_candidates == 1  # 仅已在孤儿集合的 uploaded


def test_execute_reaps_abandoned_then_classifies(
    session_factory: sessionmaker[Session],
) -> None:
    """execute 先 reap 再扫描：过窗持键 reserved 本轮即可删除。"""
    with session_factory() as session:
        job = _seed_orphan(
            session, scene_id="s-reap", key="memoir-test/reap.mp3",
            state=STATE_RESERVED, age_hours=30,
        )
        now = datetime.now(UTC)
        # ORM commit 会触发 updated_at onupdate，把 30h 年龄刷回现在；
        # 过期写入必须走 Core UPDATE 并保留原时间戳，才能用墙钟 now 验证同轮删除。
        session.execute(
            sa.update(MemoirAudioJob)
            .where(MemoirAudioJob.id == job.id)
            .values(
                expires_at=now - timedelta(hours=25),
                updated_at=MemoirAudioJob.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        session.expire(job)
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            now=now,
            publish_probe=_FixedProbe(None),
        )
        assert report.scanned == 1
        assert report.delete_candidates == 1
        assert report.deleted == 1
        assert deleter.deleted == ["memoir-test/reap.mp3"]
        # mark_cleaned 只改身份映射，未 flush；refresh 会读回库里的 failed。
        assert job.state == "cleaned"


def _seed_keyless_default_bgm(
    session: Session,
    *,
    expires_at: datetime,
    state: str = STATE_RESERVED,
    attempt: int = 1,
) -> MemoirAudioJob:
    """直接造默认 BGM 零费无键槽（freeze §11.3 D4 收敛测试专用）。

    形状与生产卡死行一致：reserved、无 object_key、无 provider_task_id、
    reserved_cost=0、lease 过保留窗——fail_abandoned_keyed_jobs 因无键
    不收割、service 恢复因非终态不复活，正是 D4 要收敛的缺口形状
    （确定性转码失败 / 进程中断在 record_object_key 之前崩溃的窗口）。
    """
    job = MemoirAudioJob(
        job_id="job-bgm-keyless",
        business_id="archive-1",
        run_id="run-1",
        generation_epoch=3,
        package_version="1.0.8",
        role=ROLE_BACKGROUND_MUSIC,
        scene_id=WORK_SCENE_ID,
        segment_index=NO_SEGMENT_INDEX,
        input_hmac="hmac-bgm-default",
        attempt=attempt,
        state=state,
        object_key=None,
        reserved_cost=Decimal("0"),
        currency="CNY",
        lease_owner="worker-a",
        lease_token=1,
        expires_at=expires_at,
        updated_at=datetime.now(UTC) - timedelta(hours=30),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def test_execute_converges_keyless_expired_default_bgm_slot(
    session_factory: sessionmaker[Session],
) -> None:
    """D4（freeze §11.3）：无键过期零费默认槽必须有界收敛。

    过保留窗的无键 reserved 默认槽：execute 维护轮收割为 failed 且
    settled_cost=0（终结 + 零费结算）——后续生成请求经 reserve_job
    复活重试（attempt+1、lease fence 旋转，见 service 侧
    test_default_mode_recovers_settled_zero_fee_failed_slot），attempt
    达上限后自然终态。不得永久停留 reserved/待对账。
    """
    with session_factory() as session:
        job = _seed_keyless_default_bgm(
            session, expires_at=datetime.now(UTC) - timedelta(hours=25)
        )
        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            now=datetime.now(UTC),
            publish_probe=_FixedProbe(None),
        )
        # 无键行不进孤儿扫描、无对象可删：唯一断点是状态收敛本身。
        assert report.scanned == 0
        assert deleter.deleted == []
        session.refresh(job)
        assert job.state == STATE_FAILED
        assert job.error_code == "AUDIO_LEASE_ABANDONED"
        assert job.settled_cost == Decimal("0")


def test_keyless_default_bgm_live_lease_not_converged(
    session_factory: sessionmaker[Session],
) -> None:
    """lease 未过期的无键默认槽：worker 可能仍在写，维护轮不得收割。"""
    with session_factory() as session:
        job = _seed_keyless_default_bgm(
            session, expires_at=datetime.now(UTC) + timedelta(hours=1)
        )
        deleter = _FakeDeleter()
        run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            now=datetime.now(UTC),
            publish_probe=_FixedProbe(None),
        )
        session.refresh(job)
        assert job.state == STATE_RESERVED
        assert job.settled_cost is None


def test_end_to_end_timeout_reaper_and_membership(tmp_path: Any) -> None:
    """真实 generate + 真实账本 + reaper + 维护分类（含键/不含键）。

    覆盖旁白超时→迟到成功→保留窗到期→正确收敛。收费/OSS 用替身。
    """
    import time

    from test_memoir_audio_ledger_recovery import (
        RUN_ID,
        FakeMusicClient,
        SlowUploader,
        _build_harness,
        _playback,
    )

    slow = SlowUploader(1.2)
    harness = _build_harness(
        tmp_path,
        music=FakeMusicClient(fail_submit=True),
        uploader=slow,
        config_overrides={
            "node_timeout_seconds": 0.40,
            "publish_reserve_seconds": 0.05,
        },
    )
    try:
        result = harness.service.generate(harness.run, _playback())
        assert result == {"narrations": [], "background_music": None}
        assert slow.finished.wait(timeout=5.0)
        if slow.thread is not None:
            slow.thread.join(timeout=5.0)
        with harness.factory() as probe:
            narr = probe.execute(
                sa.select(MemoirAudioJob).where(
                    MemoirAudioJob.run_id == RUN_ID,
                    MemoirAudioJob.role == ROLE_NARRATION,
                )
            ).scalar_one()
        assert narr.state == STATE_RESERVED
        assert narr.object_key is not None
        object_key = narr.object_key
        reap_now = datetime.now(UTC) + timedelta(hours=25)

        # 含键：已引用零删除。
        with harness.factory() as maint:
            report = run_maintenance(
                maint,
                oss_deleter=_FakeDeleter(),
                retention_hours=24,
                limit=100,
                execute=True,
                now=reap_now,
                publish_probe=_FixedProbe({"audio_object_keys": [object_key]}),
            )
            maint.commit()
        assert report.keep_published == 1
        assert report.deleted == 0
        with harness.factory() as probe:
            kept = probe.execute(
                sa.select(MemoirAudioJob).where(MemoirAudioJob.job_id == narr.job_id)
            ).scalar_one()
        assert kept.state == "failed"  # reap 后因已引用保留
        assert kept.error_code == "AUDIO_LEASE_ABANDONED"

        # 不含键：超窗删除。
        deleter = _FakeDeleter()
        with harness.factory() as maint:
            report = run_maintenance(
                maint,
                oss_deleter=deleter,
                retention_hours=24,
                limit=100,
                execute=True,
                now=reap_now,
                publish_probe=_FixedProbe({"audio_object_keys": []}),
            )
            maint.commit()
        assert report.delete_candidates == 1
        assert report.deleted == 1
        assert deleter.deleted == [object_key]
        with harness.factory() as probe:
            cleaned = probe.execute(
                sa.select(MemoirAudioJob).where(MemoirAudioJob.job_id == narr.job_id)
            ).scalar_one()
        assert cleaned.state == "cleaned"
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not any(
                "memoir-audio-upload" in t.name
                for t in __import__("threading").enumerate()
            ):
                break
            time.sleep(0.02)
    finally:
        harness.session.close()
        harness.engine.dispose()


def test_default_bgm_orphan_cleanup_uses_copy_key_only(
    session_factory: sessionmaker[Session],
) -> None:
    """M8 默认配乐最小断言（freeze 2026-09-11 §6）：清理台账只按副本 key 工作。

    默认 BGM 账本行的 object_key 只能是独立副本键（源 key 永不入账本），
    维护对账与删除因此天然只作用于副本；源 key 绝不出现在删除口。
    """
    source_key = "memoir-test/audios/default/memoirs.mp3"
    copy_key = "memoir-test/audios/background/some-scope/bgm-abc123.mp3"
    with session_factory() as session:
        _seed_agent_run(session, run_id="run-default-bgm")
        job = MemoirAudioJob(
            # 零费默认槽形状：reserved_cost=0 / settled_cost=0 / 无 TaskID / 无秒数。
            job_id="job-default-bgm-1",
            business_id="archive-1",
            run_id="run-default-bgm",
            generation_epoch=3,
            package_version="1.0.8",
            role=ROLE_BACKGROUND_MUSIC,
            scene_id=WORK_SCENE_ID,
            segment_index=NO_SEGMENT_INDEX,
            input_hmac="hmac-default-bgm",
            attempt=1,
            state=STATE_UPLOADED,
            object_key=copy_key,
            mime="audio/mpeg",
            duration_ms=47_777,
            reserved_cost=Decimal("0"),
            settled_cost=Decimal("0"),
            requested_music_seconds=None,
            provider_task_id=None,
            currency="CNY",
            lease_owner="worker-a",
            lease_token=1,
            updated_at=datetime.now(UTC) - timedelta(hours=30),
        )
        session.add(job)
        session.commit()

        deleter = _FakeDeleter()
        report = run_maintenance(
            session,
            oss_deleter=deleter,
            retention_hours=24,
            limit=100,
            execute=True,
            publish_probe=_FakeProbe(unpublished=frozenset({copy_key})),
        )

        assert report.deleted == 1
        # 删除口只收到副本 key；默认源 key 绝不被维护路径触碰。
        assert deleter.deleted == [copy_key]
        assert source_key not in deleter.deleted
