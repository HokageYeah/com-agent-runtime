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
from socket import (
    AF_INET,
    SO_REUSEADDR,
    SOCK_STREAM,
    SOL_SOCKET,
    create_connection,
    socket,
)
from time import monotonic

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.db.sqlalchemy_db import Base
from app.runtime.harness_entry import HarnessProcessConfig
from app.runtime.postgres_harness import PostgresHarnessConfig

_PYTHON_RUNTIME_ENV_ALLOWLIST = (
    "DYLD_LIBRARY_PATH",
    "LD_LIBRARY_PATH",
    "PATH",
    "SYSTEMROOT",
)
_SAFE_FAILURE_ROLES = frozenset({"api", "worker", "reconciler"})
_SAFE_FAILURE_STAGES = frozenset(
    {"bootstrap", "dependencies", "api_app", "api_server"}
)


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
        capture_stderr: bool = False,
        extra_environment: dict[str, str] | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> subprocess.Popen[str]:
        project_root = str(Path(__file__).parents[2])
        # 只继承 Python/动态链接器运行所需的固定变量；业务配置、Provider 与密钥
        # 仍全部隔离。GitHub setup-python 的解释器依赖 LD_LIBRARY_PATH 才能加载扩展。
        environment = {"PYTHONPATH": project_root}
        environment.update(
            {
                key: value
                for key in _PYTHON_RUNTIME_ENV_ALLOWLIST
                if (value := os.environ.get(key))
            }
        )
        if extra_environment is not None:
            environment.update(extra_environment)
        process = subprocess.Popen(
            command,
            cwd=self.path,
            text=True,
            env=environment,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
            pass_fds=pass_fds,
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
        self,
        host: str,
        port: int,
        *,
        timeout_seconds: float | None = None,
        process: subprocess.Popen[str] | None = None,
        fallback_role: str | None = None,
    ) -> None:
        """探测 loopback 端口；子进程提前退出时立即返回固定安全错误码。"""
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("TEST_HARNESS_LOOPBACK_REQUIRED")
        deadline = monotonic() + (
            self._timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        while monotonic() < deadline:
            if process is not None and process.poll() is not None:
                raise RuntimeError(
                    self._safe_process_exit_message(process, fallback_role=fallback_role)
                )
            try:
                with create_connection((host, port), timeout=0.1):
                    return
            except OSError:
                pass
        if process is not None and process.poll() is not None:
            raise RuntimeError(
                self._safe_process_exit_message(process, fallback_role=fallback_role)
            )
        raise TimeoutError("TEST_HARNESS_HEALTH_TIMEOUT")

    @staticmethod
    def _safe_process_exit_message(
        process: subprocess.Popen[str], *, fallback_role: str | None = None
    ) -> str:
        """只从 stderr 提取子进程产生的固定元数据，其余内容全部丢弃。"""
        return_code = process.poll()
        if process.stderr is not None:
            for line in process.stderr.read(8192).splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or set(event) != {
                    "event",
                    "role",
                    "stage",
                    "error_type",
                }:
                    continue
                role = event.get("role")
                stage = event.get("stage")
                error_type = event.get("error_type")
                if (
                    event.get("event") == "harness_failed"
                    and role in _SAFE_FAILURE_ROLES
                    and stage in _SAFE_FAILURE_STAGES
                    and isinstance(error_type, str)
                    and error_type.isidentifier()
                    and len(error_type) <= 80
                ):
                    return (
                        "TEST_HARNESS_PROCESS_EXITED:"
                        f"{role}:{stage}:{error_type}:{return_code}"
                    )
        if fallback_role in _SAFE_FAILURE_ROLES:
            return (
                "TEST_HARNESS_PROCESS_EXITED:"
                f"{fallback_role}:bootstrap:ProcessExit:{return_code}"
            )
        return "TEST_HARNESS_PROCESS_EXITED"

    def start_mock_business(
        self, port: int, *, identity_id: str | None = None
    ) -> subprocess.Popen[str]:
        """只启动本仓库的回环 mock；调用方必须先选择未占用端口。"""
        identity = identity_id or self._identity_id
        process = self.start(
            [
                sys.executable,
                "-c",
                "from app.runtime.mock_business_server import serve; "
                f"serve({port}, {identity!r}, announce_ready=True)",
            ],
            capture_stdout=True,
        )
        self._wait_for_ready(process, "mock_business")
        return process

    def start_mock_provider(self, port: int) -> subprocess.Popen[str]:
        """Provider mock 只允许 loopback 与测试身份控制，不继承生产 Provider 配置。"""
        process = self.start(
            [
                sys.executable, "-c",
                "from app.runtime.mock_provider_server import serve; "
                f"serve({port}, {self._identity_id!r}, announce_ready=True)",
            ],
            capture_stdout=True,
        )
        self._wait_for_ready(process, "mock_provider")
        return process

    @staticmethod
    def _create_api_listener() -> socket:
        """创建已监听的最终 IPv4 socket，端口由内核原子分配。"""
        listener = socket(AF_INET, SOCK_STREAM)
        listener.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            listener.set_inheritable(True)
            return listener
        except BaseException:
            listener.close()
            raise

    def start_api(
        self, *, mock_port: int, provider_port: int | None = None
    ) -> tuple[subprocess.Popen[str], int]:
        """通过父进程最终 listening socket 启动 API，不预选或重新绑定端口。"""
        listener = self._create_api_listener()
        port = int(listener.getsockname()[1])
        try:
            socket_fd = listener.fileno()
            process = self._start_role(
                "api",
                port=port,
                mock_port=mock_port,
                ready=True,
                provider_port=provider_port,
                socket_fd=socket_fd,
            )
        finally:
            # Popen 已把 fd 继承给子进程；父进程必须关闭副本，端口所有权只留给 API。
            listener.close()
        # API 子进程需冷导入完整 Runtime app（FastAPI+SQLAlchemy+Worker 栈），
        # CI 受限 runner 的冷启动可超过调用方给的短超时；与 _wait_for_ready
        # 保持同一 60 秒硬下限，正常启动仍会在端口就绪时立即返回。
        self.wait_for_port(
            "127.0.0.1",
            port,
            timeout_seconds=max(self._timeout_seconds, 60.0),
            process=process,
            fallback_role="api",
        )
        return process, port

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
        socket_fd: int | None = None,
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
            socket_fd=socket_fd,
        )
        config_path = self.path / f"{role}.json"
        config_path.write_text(json.dumps(config.to_payload()), encoding="utf-8")
        config_path.chmod(0o600)
        process = self.start(
            [
                sys.executable,
                "-m",
                "app.runtime.harness_bootstrap",
                "--role",
                role,
                "--config",
                str(config_path),
            ],
            capture_stdout=ready,
            capture_stderr=role == "api",
            extra_environment=(
                {"PGPASSWORD": database_password}
                if database_password is not None
                else None
            ),
            pass_fds=(socket_fd,) if socket_fd is not None else (),
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
                    raise RuntimeError(
                        self._safe_process_exit_message(
                            process,
                            fallback_role=role if role in _SAFE_FAILURE_ROLES else None,
                        )
                    )
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
