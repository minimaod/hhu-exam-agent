"""F010 B 离线评测的 pytest 外连封锁插件。

用法::

    pytest -p tests.f010b_egress_guard ...

插件在整个会话内禁用 DNS、TCP 连接、TLS 包装与 ``urllib``，并把任何外连
尝试写入 ``$F010B_EVIDENCE/egress-pytest.json``。只要有一次尝试，会话退出码
即被改为 1：离线证据不能靠“恰好没有执行到联网分支”来通过。
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import time
from pathlib import Path
from typing import Any

_ATTEMPTS: list[dict[str, Any]] = []
_STARTED = time.time()
_PATCHED: list[tuple[Any, str, Any]] = []


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


def _loopback(address: Any) -> bool:
    """本机回环（含 asyncio 自管道）不算外连；评测证据只统计真正的外部目标。"""

    if isinstance(address, tuple) and address:
        return str(address[0]) in _LOOPBACK_HOSTS
    return str(address) in _LOOPBACK_HOSTS


def _record(kind: str, target: str) -> None:
    _ATTEMPTS.append(
        {
            "kind": kind,
            "target": str(target),
            "at": round(time.time() - _STARTED, 3),
        }
    )


def _patch(owner: Any, name: str, replacement: Any) -> None:
    original = getattr(owner, name)
    _PATCHED.append((owner, name, original))
    setattr(owner, name, replacement)


def _install() -> None:
    if _PATCHED:
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def blocked_connect(self, address):
        if _loopback(address):
            return real_connect(self, address)
        _record("connect", address)
        raise OSError("F010B 离线评测：禁止网络连接。")

    def blocked_connect_ex(self, address):
        if _loopback(address):
            return real_connect_ex(self, address)
        _record("connect_ex", address)
        return 111

    def blocked_getaddrinfo(host, *args, **kwargs):
        if _loopback(host):
            return real_getaddrinfo(host, *args, **kwargs)
        _record("getaddrinfo", host)
        raise socket.gaierror("F010B 离线评测：禁止 DNS 查询。")

    def blocked_create_connection(address, *args, **kwargs):
        if _loopback(address):
            return real_create_connection(address, *args, **kwargs)
        _record("create_connection", address)
        raise OSError("F010B 离线评测：禁止网络连接。")

    _patch(socket.socket, "connect", blocked_connect)
    _patch(socket.socket, "connect_ex", blocked_connect_ex)
    _patch(socket, "getaddrinfo", blocked_getaddrinfo)
    _patch(socket, "create_connection", blocked_create_connection)

    real_wrap_socket = ssl.SSLContext.wrap_socket

    def blocked_wrap_socket(self, sock, *args, **kwargs):
        _record("wrap_socket", getattr(sock, "getpeername", lambda: "unknown")())
        raise ssl.SSLError("F010B 离线评测：禁止 TLS 连接。")

    _patch(ssl.SSLContext, "wrap_socket", blocked_wrap_socket)

    import urllib.request

    real_urlopen = urllib.request.urlopen

    def blocked_urlopen(url, *args, **kwargs):
        _record("urlopen", url)
        raise OSError("F010B 离线评测：禁止 HTTP 请求。")

    _patch(urllib.request, "urlopen", blocked_urlopen)

    # 保留引用，避免被垃圾回收（也便于调试时确认补丁来源）。
    _PATCHED.extend(
        [
            (socket.socket, "_f010b_real_connect", real_connect),
            (socket.socket, "_f010b_real_connect_ex", real_connect_ex),
            (socket, "_f010b_real_getaddrinfo", real_getaddrinfo),
            (socket, "_f010b_real_create_connection", real_create_connection),
            (ssl.SSLContext, "_f010b_real_wrap_socket", real_wrap_socket),
            (urllib.request, "_f010b_real_urlopen", real_urlopen),
        ]
    )


def pytest_configure(config: Any) -> None:
    _install()


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    evidence_dir = os.environ.get("F010B_EVIDENCE")
    payload = {
        "version": 1,
        "attempts": len(_ATTEMPTS),
        "blocked": _ATTEMPTS,
        "seconds": round(time.time() - _STARTED, 3),
        "exitstatus": exitstatus,
    }
    if evidence_dir:
        target = Path(evidence_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "egress-pytest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if _ATTEMPTS:
        session.exitstatus = 1