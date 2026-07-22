"""为业务 HTTP Tool 记录真实 TCP 对端地址，抵御 DNS rebinding。"""

from __future__ import annotations

from threading import Lock
from typing import Any

import httpcore
import httpx


class _PeerTrackingNetworkBackend(httpcore.NetworkBackend):
    """复用 httpcore 同步网络后端，仅在建连成功后记录 socket 对端 IP。"""

    def __init__(self, delegate: httpcore.NetworkBackend) -> None:
        self._delegate = delegate
        self._lock = Lock()
        self._peer_ip: str | None = None

    def reset_peer_ip(self) -> None:
        """每次逻辑 Tool 请求发送前清空上一次连接记录。"""
        with self._lock:
            self._peer_ip = None

    def peer_ip(self) -> str | None:
        """返回本次已建立 TCP 连接的对端 IP，不返回端口或响应内容。"""
        with self._lock:
            return self._peer_ip

    def connect_tcp(self, **kwargs: Any) -> httpcore.NetworkStream:
        """委托实际建连，并从 socket 的 server_addr 读取不可伪造的对端地址。"""
        stream = self._delegate.connect_tcp(**kwargs)
        address = stream.get_extra_info("server_addr")
        peer_ip = address[0] if isinstance(address, tuple) and address else None
        with self._lock:
            self._peer_ip = peer_ip if isinstance(peer_ip, str) else None
        return stream

    def connect_unix_socket(self, **kwargs: Any) -> httpcore.NetworkStream:
        """Tool connector 不使用 Unix socket；保留委托以满足 httpcore 后端协议。"""
        return self._delegate.connect_unix_socket(**kwargs)

    def sleep(self, seconds: float) -> None:
        """保持 httpcore 原有退避行为。"""
        self._delegate.sleep(seconds)


class PeerTrackingHTTPTransport(httpx.HTTPTransport):
    """单连接、无代理的 HTTP Transport，向 ToolGateway 暴露实际对端 IP。"""

    def __init__(self) -> None:
        # 每个 ToolGateway 随 Worker Run 创建；不复用 keep-alive 可确保每次请求都有
        # 对应的 socket 对端记录，避免把上一请求的 peer 错配给当前请求。
        super().__init__(
            trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            retries=0,
        )
        backend = _PeerTrackingNetworkBackend(self._pool._network_backend)
        self._pool._network_backend = backend
        self._backend = backend

    def reset_peer_ip(self) -> None:
        """供 ToolGateway 在发送前清除旧连接记录。"""
        self._backend.reset_peer_ip()

    def peer_ip(self) -> str | None:
        """供 ToolGateway 比对预检 DNS 地址与真实 TCP 对端。"""
        return self._backend.peer_ip()
