"""Block outbound network and drop exchange credentials during pytest.

Tests inject their own fake HTTP clients. Anything that still tries to open
a socket to a host other than loopback fails here, before a packet is sent.
"""
from __future__ import annotations

import socket

EXCHANGE_ENV_KEYS = (
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "DELTA_INDIA_API_KEY",
    "DELTA_INDIA_API_SECRET",
    "DELTA_INDIA_TESTNET_API_KEY",
    "DELTA_INDIA_TESTNET_API_SECRET",
)

_INSTALLED = False


class OfflineNetworkError(RuntimeError):
    """A test tried to use the network."""


def _allowed(address: object) -> bool:
    if isinstance(address, str):
        return True  # AF_UNIX path
    if not isinstance(address, tuple) or not address:
        return True
    host = address[0]
    if host is None or host == "":
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    text = str(host).strip("[]")
    return text in {"localhost", "::1"} or text.startswith("127.")


def install() -> None:
    """Install the socket block once per process. Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_create = socket.create_connection
    orig_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: object) -> None:
        if _allowed(address):
            orig_connect(self, address)  # type: ignore[arg-type]
            return
        raise OfflineNetworkError(f"offline tests blocked connect to {address!r}")

    def connect_ex(self: socket.socket, address: object) -> int:
        if _allowed(address):
            return orig_connect_ex(self, address)  # type: ignore[arg-type]
        raise OfflineNetworkError(f"offline tests blocked connect_ex to {address!r}")

    def create_connection(address: object, *args: object, **kwargs: object) -> socket.socket:
        if _allowed(address):
            return orig_create(address, *args, **kwargs)  # type: ignore[arg-type]
        raise OfflineNetworkError(f"offline tests blocked create_connection to {address!r}")

    def getaddrinfo(host: object, port: object, *args: object, **kwargs: object) -> list:
        probe: object = host
        if isinstance(host, bytes):
            probe = host.decode("ascii", "replace")
        if _allowed((probe, port)):
            return orig_getaddrinfo(host, port, *args, **kwargs)  # type: ignore[arg-type]
        raise OfflineNetworkError(f"offline tests blocked DNS for {host!r}")

    socket.socket.connect = connect  # type: ignore[assignment]
    socket.socket.connect_ex = connect_ex  # type: ignore[assignment]
    socket.create_connection = create_connection  # type: ignore[assignment]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    _INSTALLED = True
