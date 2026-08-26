"""Task 12 测试子进程生命周期管理；不得用于生产服务守护。"""

from __future__ import annotations

import json
import os
import secrets
import selectors
import subprocess
import sys
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from socket import create_connection
from time import monotonic

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.db.sqlalchemy_db import Base
from app.runtime.harness_entry import HarnessProcessConfig
from app.runtime.postgres_harness import PostgresHarnessConfig


class ProcessHarness(AbstractContextManager["ProcessHarness"]):
    """所有测试子进程均由此对象回收，超时或断言失败也不遗留后台服务。"""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        postgres: PostgresHarnessConfig | None = None,
        redis_url: str | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._temporary = tempfile.TemporaryDirectory(prefix="agent-runtime-harness-")
        self.path = Path(self._temporary.name)
        self._processes: list[subprocess.Popen[str]] = []
        self._sqlite_path = self.path / "runtime.db"
        self._identity_id = f"harness-{secrets.token_hex(12)}"
        self._postgres = postgres
        self._redis_url = redis_url

    @property
    def identity_id(self) -> str:
        """测试调用方身份不是密钥；仅用于生成同一临时受信任调用方的签名。"""
        return self._identity_id

    def sqlite_session_factory(self) -> sessionmaker:
        """只在临时目录创建 SQLite schema，绝不读取环境数据库配置。"""
        if self._postgres is not None:
            raise RuntimeError("TEST_HARNESS_POSTGRES_REQUIRED")
        engine = create_engine(f"sqlite:///{self._sqlite_path}")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)

    def start(
        self,
        command: list[str],
        *,
        capture_stdout: bool = False,
        extra_environment: dict[str, str] | None = None,
    ) -> subprocess.Popen[str]:
        project_root = str(Path(__file__).parents[2])
        # 子进程不继承父环境，防止生产数据库、Provider 或密钥意外进入测试路径。
        environment = {"PYTHONPATH": project_root}
        if extra_environment is not None:
            environment.update(extra_environment)
        process = subprocess.Popen(
            command,
            cwd=self.path,
            text=True,
            env=environment,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._processes.append(process)
        return process

    def wait_for_exit(self, process: subprocess.Popen[str]) -> int:
        return process.wait(timeout=self._timeout_seconds)

    def wait_for_completed(self, process: subprocess.Popen[str], role: str) -> str:
        """读取唯一无内容终态事件，避免测试依赖子进程退出时序。"""
        if process.stdout is None:
            raise RuntimeError("TEST_HARNESS_READY_STREAM_MISSING")
        deadline = monotonic() + max(self._timeout_seconds, 60.0)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while monotonic() < deadline:
                if not selector.select(timeout=min(0.1, deadline - monotonic())):
                    continue
                try:
                    event = json.loads(process.stdout.readline())
                except json.JSONDecodeError as exc:
                    raise RuntimeError("TEST_HARNESS_COMPLETED_INVALID") from exc
                if (
                    isinstance(event, dict)
                    and event.get("event") == "completed"
                    and event.get("role") == role
                    and event.get("result_code") in {"completed", "failed"}
                ):
                    return str(event["result_code"])
        raise TimeoutError("TEST_HARNESS_COMPLETED_TIMEOUT")

    def wait_for_port(
        self, host: str, port: int, *, timeout_seconds: float | None = None
    ) -> None:
        """健康探针只探测 loopback TCP 端口，超时即让测试失败并触发回收。"""
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("TEST_HARNESS_LOOPBACK_REQUIRED")
        deadline = monotonic() + (
            self._timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        while monotonic() < deadline:
            try:
                with create_connection((host, port), timeout=0.1):
                    return
            except OSError:
                pass
        raise TimeoutError("TEST_HARNESS_HEALTH_TIMEOUT")

    def start_mock_business(
        self, port: int, *, identity_id: str | None = None
    ) -> subprocess.Popen[str]:
        """只启动本仓库的回环 mock；调用方必须先选择未占用端口。"""
        identity = identity_id or self._identity_id
        process = self.start(
            [
                sys.executable,
                "-c",
                f"from app.runtime.mock_business_server import serve; serve({port}, {identity!r})",
            ]
        )
        self.wait_for_port("127.0.0.1", port)
        return process

    def start_mock_provider(self, port: int) -> subprocess.Popen[str]:
        """Provider mock 只允许 loopback 与测试身份控制，不继承生产 Provider 配置。"""
        process = self.start(
            [
                sys.executable, "-c",
                f"from app.runtime.mock_provider_server import serve; serve({port}, {self._identity_id!r})",
            ]
        )
        self.wait_for_port("127.0.0.1", port)
        return process

    def start_api(
        self, port: int, *, mock_port: int, provider_port: int | None = None
    ) -> subprocess.Popen[str]:
        """以受限 JSON 启动测试 API；仅模型场景冻结同一回环 route。"""
        process = self._start_role(
            "api", port=port, mock_port=mock_port, provider_port=provider_port
        )
        # API 子进程需冷导入完整 Runtime app（FastAPI+SQLAlchemy+Worker 栈），
        # CI 受限 runner 的冷启动可超过调用方给的短超时；与 _wait_for_ready
        # 保持同一 60 秒硬下限，正常启动仍会在端口就绪时立即返回。
        self.wait_for_port(
            "127.0.0.1", port, timeout_seconds=max(self._timeout_seconds, 60.0)
        )
        return process

    def start_worker(self, *, mock_port: int, provider_port: int | None = None) -> subprocess.Popen[str]:
        """Worker 仅跑一轮并以固定 ready 摘要通知父进程。"""
        return self._start_role(
            "worker", port=mock_port, mock_port=mock_port, ready=True, provider_port=provider_port
        )

    def start_reconciler(self, *, mock_port: int) -> subprocess.Popen[str]:
        """Reconciler 仅跑一轮并以固定 ready 摘要通知父进程。"""
        return self._start_role(
            "reconciler", port=mock_port, mock_port=mock_port, ready=True
        )

    def _start_role(
        self, role: str, *, port: int, mock_port: int, ready: bool = False,
        provider_port: int | None = None,
    ) -> subprocess.Popen[str]:
        if self._postgres is None:
            self.sqlite_session_factory()
        database_url: str | None = None
        database_password: str | None = None
        if self._postgres is not None:
            parsed_database_url = make_url(self._postgres.database_url)
            database_password = parsed_database_url.password or os.environ.get(
                "PGPASSWORD"
            )
            # 子进程配置文件只保存无凭据 loopback DSN；测试密码仅进入该子进程环境。
            database_url = parsed_database_url.set(password=None).render_as_string(
                hide_password=False
            )
        config = HarnessProcessConfig(
            sqlite_path=self._sqlite_path,
            # API 只需要自身的 loopback 端口；worker/reconciler 使用 mock 端口构造 client。
            port=port,
            mock_port=mock_port,
            role=role,  # type: ignore[arg-type]
            identity_id=self._identity_id,
            # PostgreSQL 子进程首次导入 ORM/迁移元数据可能超过 SQLite 的 10 秒；
            # 仍限制在 harness 全局有限时钟内，避免真实故障无限挂起。
            timeout_seconds=min(self._timeout_seconds, 60.0),
            provider_port=provider_port,
            redis_url=self._redis_url if provider_port is not None else None,
            database_url=database_url,
            schema=self._postgres.schema if self._postgres is not None else None,
        )
        config_path = self.path / f"{role}.json"
        config_path.write_text(json.dumps(config.to_payload()), encoding="utf-8")
        config_path.chmod(0o600)
        process = self.start(
            [
                sys.executable,
                "-m",
                "app.runtime.harness_entry",
                "--config",
                str(config_path),
            ],
            capture_stdout=ready,
            extra_environment=(
                {"PGPASSWORD": database_password}
                if database_password is not None
                else None
            ),
        )
        if ready:
            self._wait_for_ready(process, role)
        return process

    def _wait_for_ready(self, process: subprocess.Popen[str], role: str) -> None:
        if process.stdout is None:
            raise RuntimeError("TEST_HARNESS_READY_STREAM_MISSING")
        # 真实 PostgreSQL Worker 的冷启动可超过单轮业务超时；ready 仅等待固定
        # 安全事件，仍有 60 秒硬上限，避免把正常装配误判为 worker 竞争失败。
        deadline = monotonic() + max(self._timeout_seconds, 60.0)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("TEST_HARNESS_PROCESS_EXITED")
                if not selector.select(timeout=min(0.1, deadline - monotonic())):
                    continue
                line = process.stdout.readline()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("TEST_HARNESS_READY_INVALID") from exc
                if event == {"event": "ready", "role": role}:
                    return
                raise RuntimeError("TEST_HARNESS_READY_INVALID")
        raise TimeoutError("TEST_HARNESS_READY_TIMEOUT")

    def __exit__(self, *_: object) -> None:
        deadline = monotonic() + self._timeout_seconds
        for process in self._processes:
            if process.poll() is None:
                process.terminate()
        for process in self._processes:
            remaining = max(0.0, deadline - monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        self._temporary.cleanup()
