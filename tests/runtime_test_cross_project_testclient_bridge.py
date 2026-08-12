"""Runtime ToolGateway 到 couple-diary provider 的无监听 TestClient 集成证据。"""

from __future__ import annotations

import base64
import json
import subprocess
import textwrap
from pathlib import Path

import httpx

from app.runtime.test_harness import LoopbackTestTransport, RuntimeHarnessConfig
from app.runtime.tool_gateway import BusinessConnector, ToolGateway

_BUSINESS_ROOT = Path("/Users/yuye/YeahWork/Python项目/couple-diary-doc/backend/couple-diary-b")
_BUSINESS_PYTHON = _BUSINESS_ROOT / ".venv" / "bin" / "python"

# 该子进程只拥有 business 的 ``app`` import namespace，避开同一解释器中两个仓库
# 都叫 app 的 Python package 冲突。它接受 Runtime 已签名的原始 HTTP 请求，并在真实
# FastAPI TestClient 中处理；没有伪造 provider JSON、也不绑定 loopback 端口。
_PROVIDER_TESTCLIENT_SCRIPT = textwrap.dedent(
    """
    import base64, importlib.util, json
    from pathlib import Path
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from pytest import MonkeyPatch

    root = Path.cwd()
    spec = importlib.util.spec_from_file_location(
        "provider_test_helpers", root / "tests" / "test_memory_agent_tools_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from app.core.config import settings
    from app.db.sqlalchemy_db import get_sqlalchemy_db
    from app.main import app

    request = json.loads(input())
    monkeypatch = MonkeyPatch()
    session = module._create_test_session()
    monkeypatch.setattr(settings, "MEMORY_RUNTIME_CLIENT_ID", module._TEST_CLIENT_ID)
    monkeypatch.setattr(settings, "MEMORY_RUNTIME_KEY_ID", module._TEST_KEY_ID)
    monkeypatch.setattr(settings, "MEMORY_RUNTIME_SECRET", module._TEST_SECRET)
    def override_db():
        yield session
    app.dependency_overrides[get_sqlalchemy_db] = override_db
    try:
        archive, snapshot, runref = module._seed_archive(session)
        # Runtime must provide this trusted scope; a provider-created response alone
        # cannot make the test pass because the incoming signature/context are retained.
        assert request["path"].endswith("memory.get_snapshot")
        with patch("app.main.setup_logging"), patch("app.main.database.connect"), patch("app.main.database.close"):
            with TestClient(app) as client:
                response = client.request(
                    request["method"], request["path"],
                    content=base64.b64decode(request["body"]), headers=request["headers"],
                )
        print(json.dumps({"status": response.status_code, "body": base64.b64encode(response.content).decode()}))
    finally:
        app.dependency_overrides.pop(get_sqlalchemy_db, None)
        session.close()
        monkeypatch.undo()
    """
)


class _BusinessTestClientBridge(httpx.BaseTransport):
    """把真实 Runtime HTTP request 转交给独立解释器中的 business TestClient。"""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        payload = {
            "method": request.method,
            "path": request.url.raw_path.decode(),
            "headers": dict(request.headers),
            "body": base64.b64encode(request.content).decode(),
        }
        result = subprocess.run(
            [_BUSINESS_PYTHON, "-c", _PROVIDER_TESTCLIENT_SCRIPT],
            cwd=_BUSINESS_ROOT,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
        )
        response = json.loads(result.stdout)
        return httpx.Response(
            int(response["status"]),
            content=base64.b64decode(response["body"]),
            request=request,
        )


def test_tool_gateway_reaches_real_business_testclient_with_runtime_signature() -> None:
    """真实网关签名/envelope 经 provider TestClient 验证并返回真实快照摘要。"""
    assert _BUSINESS_PYTHON.is_file(), "跨仓 TestClient bridge 需要本地隔离 business venv"
    harness = RuntimeHarnessConfig(
        session_factory=object(),
        trusted_clients={"test": {"keys": {"test": "test-agent-tool-secret"}}},
        runtime_id="couple-diary-test",
        mock_base_url="http://127.0.0.1:8765",
    )
    gateway = ToolGateway(
        {
            "couple_diary_backend": BusinessConnector(
                "http://127.0.0.1:8765",
                "couple-diary-test",
                "test-key-1",
                "test-agent-tool-secret",
            )
        },
        httpx.Client(transport=_BusinessTestClientBridge()),
        test_transport=LoopbackTestTransport(harness),
    )

    output = gateway.get_snapshot(
        "couple_diary_backend",
        "01J00000000000000000000001",
        "01J00000000000000000000002",
        "run_test_0001",
        1,
        {
            "agent_id": "memoir_agent",
            "agent_version": "1.0.1",
            "run_id": "run_test_0001",
            "step_id": "cross_project_snapshot",
            "business_type": "couple_memory",
            "business_id": "01J00000000000000000000001",
            "trace_id": "cross-project-trace",
        },
    )

    assert output["snapshot_id"] == "01J00000000000000000000002"
    assert output["schema_version"] == "1.0.0"
