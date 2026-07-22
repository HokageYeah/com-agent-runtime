"""业务 Tool HTTP 真实对端地址记录的回归测试。"""

from __future__ import annotations

from typing import Any

from app.runtime.peer_tracking_transport import _PeerTrackingNetworkBackend


def test_peer_tracking_backend_records_actual_tcp_server_address() -> None:
    """建连成功后只提取 socket 的对端 IP，绝不依赖请求域名或响应正文。"""

    class Stream:
        def get_extra_info(self, info: str) -> object:
            assert info == "server_addr"
            return ("8.8.8.8", 443)

    class Backend:
        def connect_tcp(self, **kwargs: Any) -> Stream:
            assert kwargs["host"] == "business.local"
            return Stream()

        def connect_unix_socket(self, **kwargs: Any) -> object:
            raise AssertionError("业务 HTTP connector 不应走 Unix socket")

        def sleep(self, seconds: float) -> None:
            raise AssertionError("本测试不应触发退避")

    backend = _PeerTrackingNetworkBackend(Backend())
    backend.reset_peer_ip()

    backend.connect_tcp(host="business.local", port=443, timeout=1.0)

    assert backend.peer_ip() == "8.8.8.8"

