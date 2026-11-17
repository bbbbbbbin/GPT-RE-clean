# -*- coding: utf-8 -*-
"""本地 SOCKS5 链式代理：浏览器 -> 本服务 -> xray -> 代理商 -> 目标。

代理商代理端口从本机直连会返回 SOCKS5 0x02（只允许刷新用的 xray 出口访问），
因此本服务把流量先送进 xray，再由 xray 连到代理商，最后以代理商住宅出口访问目标。

配置（项目根目录 .env）：
    PROXY_POOL                    # 第一行即代理商代理 URL
    PROXY_POOL_UPSTREAM_PROXY     # xray，例如 socks5://127.0.0.1:10808
    PROXY_CHAIN_PORT              # 可选，默认 10810
    PROXY_CHAIN_PROVIDER          # 可选，覆盖 PROXY_POOL 第一行
"""
from __future__ import annotations

import logging
import os
import select
import socket
import struct
import sys
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("proxy-chain")

PROVIDER_URL = str(os.environ.get("PROXY_CHAIN_PROVIDER") or "").strip()
if not PROVIDER_URL:
    raw_pool = str(os.environ.get("PROXY_POOL") or "").replace("\\n", "\n")
    for line in raw_pool.splitlines():
        line = line.strip()
        if line:
            PROVIDER_URL = line
            break

UPSTREAM_URL = str(os.environ.get("PROXY_POOL_UPSTREAM_PROXY") or "socks5://127.0.0.1:10808").strip()
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("PROXY_CHAIN_PORT", "10810") or "10810")

_provider = urlparse(PROVIDER_URL)
_upstream = urlparse(UPSTREAM_URL)
PROVIDER_HOST = _provider.hostname or ""
PROVIDER_PORT = int(_provider.port or 0)
PROVIDER_USER = unquote(_provider.username or "").encode()
PROVIDER_PASS = unquote(_provider.password or "").encode()
UPSTREAM_HOST = _upstream.hostname or "127.0.0.1"
UPSTREAM_PORT = int(_upstream.port or 10808)


def _recvn(sock: socket.socket, size: int) -> bytes:
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _read_reply(sock: socket.socket) -> int:
    version, code, _, atyp = _recvn(sock, 4)
    if atyp == 1:
        _recvn(sock, 4)
    elif atyp == 3:
        _recvn(sock, _recvn(sock, 1)[0])
    elif atyp == 4:
        _recvn(sock, 16)
    _recvn(sock, 2)
    return code


def _upstream_tunnel() -> socket.socket:
    sock = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=15)
    sock.sendall(b"\x05\x01\x00")
    if _recvn(sock, 2)[1] != 0x00:
        raise RuntimeError("xray 不接受免认证")
    host = PROVIDER_HOST.encode()
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack(">H", PROVIDER_PORT))
    if _read_reply(sock) != 0:
        raise RuntimeError("xray 无法连接代理商")
    return sock


def _connect_provider() -> socket.socket:
    sock = _upstream_tunnel()
    sock.sendall(b"\x05\x01\x02")
    if _recvn(sock, 2)[1] != 0x02:
        raise RuntimeError("代理商不接受用户名密码")
    sock.sendall(
        b"\x01"
        + bytes([len(PROVIDER_USER)])
        + PROVIDER_USER
        + bytes([len(PROVIDER_PASS)])
        + PROVIDER_PASS
    )
    if _recvn(sock, 2)[1] != 0x00:
        raise RuntimeError("代理商认证失败")
    return sock


def _connect_target(host: str, port: int) -> socket.socket:
    sock = _connect_provider()
    raw = host.encode()
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(raw)]) + raw + struct.pack(">H", port))
    if _read_reply(sock) != 0:
        raise RuntimeError(f"代理商无法连接目标 {host}:{port}")
    return sock


def _relay(client: socket.socket, remote: socket.socket) -> None:
    try:
        while True:
            readable, _, _ = select.select([client, remote], [], [], 120)
            if not readable:
                return
            for sock in readable:
                data = sock.recv(65536)
                if not data:
                    return
                (remote if sock is client else client).sendall(data)
    finally:
        try:
            remote.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass


def _handle(client: socket.socket) -> None:
    try:
        version, methods = _recvn(client, 2)
        if version != 0x05:
            return
        _recvn(client, methods)
        client.sendall(b"\x05\x00")
        version, command, _, atyp = _recvn(client, 4)
        if version != 0x05 or command != 0x01:
            client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            return
        if atyp == 1:
            host = socket.inet_ntoa(_recvn(client, 4))
        elif atyp == 3:
            host = _recvn(client, _recvn(client, 1)[0]).decode("utf-8", "replace")
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, _recvn(client, 16))
        else:
            client.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
            return
        port = struct.unpack(">H", _recvn(client, 2))[0]
        remote = _connect_target(host, port)
        client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        _relay(client, remote)
    except Exception as exc:
        LOG.warning("连接失败：%s", exc)
        try:
            client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass


def main() -> None:
    if not PROVIDER_HOST or not PROVIDER_PORT:
        raise SystemExit("PROXY_POOL/PROXY_CHAIN_PROVIDER 中没有有效的代理商代理")
    LOG.info(
        "链式代理启动：%s:%s -> xray %s:%s -> 代理商 %s:%s",
        LISTEN_HOST,
        LISTEN_PORT,
        UPSTREAM_HOST,
        UPSTREAM_PORT,
        PROVIDER_HOST,
        PROVIDER_PORT,
    )
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((LISTEN_HOST, LISTEN_PORT))
    server.listen(100)
    while True:
        client, _ = server.accept()
        threading.Thread(target=_handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
