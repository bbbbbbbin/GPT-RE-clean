#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ChatGPT Plus 支付短链生成器（单文件版）

流程：session + 代理 -> 查 plus-1-month-free 优惠 -> 有优惠才建 checkout -> 输出短链
      https://chatgpt.com/checkout/{processor_entity}/{checkout_session_id}

依赖：
    pip install curl_cffi pysocks        # pysocks 只在走链式代理时需要

示例：
    python plus_short_link_standalone.py --session session.json --proxy socks5h://127.0.0.1:10808
    python plus_short_link_standalone.py --session token.txt --proxy 1.2.3.4:1080 --country PH
    python plus_short_link_standalone.py --session session.json --proxy host:10000:user:pass
    python plus_short_link_standalone.py --no-chain-only --session ... --proxy ...

代理写法：
    socks5h://user:pass@host:port / socks5:// / http:// / https://
    host:port                      -> 依次试 http、socks5h、socks5、https
    host:port:user:pass            -> 同上，账号密码自动 URL 编码

代理链（默认开启）：
    直连代理商常被 reset，所以默认先起一个本地中继：
        本进程 -> 上游(默认 http://127.0.0.1:7890) -> 代理商 -> chatgpt.com
    上游地址读环境变量 PROXY_POOL_UPSTREAM_PROXY，没有就用 http://127.0.0.1:7890；
    若该端口没监听，会自动按 7890 → 7891 → 10808 → 1080 找一个能用的本地代理。
    想禁用链式：--no-chain-only（会先直连，直连失败再回退链式）。

sentinel：
    checkout 建单必须带 openai-sentinel-token（flow=chatgpt_checkout），
    否则会被判 400 "Our systems have detected unusual activity"。
    生成方式：core/sentinel.py 造初始 p，node 跑 sentinel/sdk.js 出最终 token，
    所以 exe 里要一并打包 core、config 和 sentinel/*.js，并保证机器上有 Node.js。
    若网关也要求 SO，会同时带上 openai-sentinel-so-token。
"""
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import logging
import os
import re
import select
import shutil
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from curl_cffi import requests

try:
    import socks
except ImportError:  # 链式代理才需要
    socks = None


CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
COUPON_URL = "https://chatgpt.com/backend-api/promo_campaign/check_coupon"
SENTINEL_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
TRACE_URL = "https://chatgpt.com/cdn-cgi/trace"
GEO_URL = "https://ipapi.co/json/"
DEFAULT_IMPERSONATE = "chrome"
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
DEFAULT_PROMO = "plus-1-month-free"
DEFAULT_CHAIN_UPSTREAM = "http://127.0.0.1:7890"
# 本地代理端口按顺序探测：Clash(7890/7891) → 其他客户端(10808/1080)
LOCAL_UPSTREAM_CANDIDATES = (
    ("http", 7890),
    ("http", 7891),
    ("socks5h", 10808),
    ("http", 1080),
)


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.4):
            return True
    except OSError:
        return False


def pick_local_upstream(preferred: str) -> tuple[str, str]:
    """本地代理没监听时按候选端口换一个；返回 (上游, 提示)。"""
    text = (preferred or "").strip()
    if not text:
        return "", ""
    parts = urlsplit(text if "://" in text else "http://" + text)
    host = (parts.hostname or "").lower()
    port = parts.port or (1080 if parts.scheme.lower().startswith("socks") else 7890)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return text, ""
    if _port_open(port):
        return text, ""
    for scheme, candidate_port in LOCAL_UPSTREAM_CANDIDATES:
        if candidate_port == port:
            continue
        if _port_open(candidate_port):
            url = f"{scheme}://127.0.0.1:{candidate_port}"
            return url, f"本地 {port} 未监听，已自动改用 {url}"
    return text, f"本地 {port} 未监听，且 7890 / 7891 / 10808 / 1080 都没有代理"

SESSION_RE = re.compile(r"((?:cs_(?:live|test)_|oaics_)[A-Za-z0-9]+)")
ENTITY_RE = re.compile(r"(?:/checkout/|processor_entity=)([A-Za-z0-9_]+)")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
REGION_RE = re.compile(r"region[-_]([A-Za-z]{2})(?:[-_]|$)")

CURRENCY_BY_COUNTRY = {
    "US": "USD", "GB": "GBP", "JP": "JPY", "SG": "SGD", "DE": "EUR", "FR": "EUR",
    "NL": "EUR", "IE": "EUR", "IT": "EUR", "ES": "EUR", "PT": "EUR", "BE": "EUR",
    "AT": "EUR", "FI": "EUR", "GR": "EUR", "BR": "BRL", "IN": "INR", "ID": "IDR",
    "KR": "KRW", "TH": "THB", "VN": "VND", "HK": "HKD", "TW": "TWD", "CA": "CAD",
    "AU": "AUD", "NZ": "NZD", "MX": "MXN", "TR": "TRY", "AE": "AED", "PL": "PLN",
    "CH": "CHF", "SE": "SEK", "NO": "NOK", "DK": "DKK", "CZ": "CZK", "RO": "RON",
    "MY": "MYR", "PH": "PHP", "SA": "SAR", "ZA": "ZAR", "CL": "CLP", "CO": "COP",
    "PE": "PEN", "EG": "EGP", "NG": "NGN", "PK": "PKR", "BD": "BDT", "IL": "ILS",
}

EU_COUNTRIES = {"DE", "FR", "NL", "IE", "IT", "ES", "PT", "BE", "AT", "FI", "GR",
                "PL", "CZ", "RO", "SE", "DK", "NO", "CH"}

logger = logging.getLogger("plus_short_link")


# --------------------------------------------------------------------------- #
# 代理 URL 解析
# --------------------------------------------------------------------------- #
SUPPORTED_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})


@dataclass(frozen=True)
class ProxySpec:
    raw: str
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""


def parse_proxy_url(value: str, *, allow_bare: bool = False) -> ProxySpec:
    text = str(value or "").strip()
    if not text:
        raise ValueError("代理地址为空")
    text = text.replace("\\@", "@")
    if allow_bare and "://" not in text:
        text = f"socks5://{text}"
    parsed = urlsplit(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError(f"不支持的代理协议: {scheme or '-'}")
    if not parsed.hostname:
        raise ValueError("代理格式缺少 host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口无效") from exc
    if not port or not 1 <= port <= 65535:
        raise ValueError("代理格式缺少有效 port")
    return ProxySpec(
        raw=text,
        scheme=scheme,
        host=parsed.hostname,
        port=port,
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
    )


def mask_proxy_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        spec = parse_proxy_url(text)
    except ValueError:
        return "***"
    auth = "***:***@" if spec.username or spec.password else ""
    return f"{spec.scheme}://{auth}{spec.host}:{spec.port}"


def proxy_candidates(value: str) -> list[str]:
    """把用户输入的代理展开成若干候选 URL。"""
    text = (value or "").strip()
    if not text:
        raise ValueError("代理不能为空")
    if "://" in text:
        scheme = urlsplit(text).scheme.lower()
        if scheme not in SUPPORTED_PROXY_SCHEMES:
            raise ValueError(f"不支持的代理协议: {scheme}")
        return [text]
    parts = text.split(":")
    if len(parts) == 2:
        host, port = parts
        return [
            f"http://{host}:{port}",
            f"socks5h://{host}:{port}",
            f"socks5://{host}:{port}",
            f"https://{host}:{port}",
        ]
    if len(parts) == 4:
        host, port, user, password = parts
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}"
        return [
            f"http://{auth}@{host}:{port}",
            f"socks5h://{auth}@{host}:{port}",
            f"socks5://{auth}@{host}:{port}",
            f"https://{auth}@{host}:{port}",
        ]
    raise ValueError("代理格式应为 URL、host:port 或 host:port:user:pass")


def normalize_proxy(value: str) -> str:
    return proxy_candidates(value)[0]


def proxy_map(proxy: str) -> dict:
    return {"http": proxy, "https": proxy}


def mask_proxy(proxy: str) -> str:
    try:
        return mask_proxy_url(proxy)
    except Exception:
        return re.sub(r"://[^/@]+@", "://***:***@", str(proxy))


def geo_hint(value: str) -> dict:
    """代理商地址里常带 region-US，直接当国家提示，省一次探测请求。"""
    match = REGION_RE.search(str(value or ""))
    if not match:
        return {"ok": False, "ip": "", "country": "", "currency": "", "source": "", "error": ""}
    country = match.group(1).upper()
    return {
        "ok": True,
        "ip": "",
        "country": country,
        "currency": CURRENCY_BY_COUNTRY.get(country, ""),
        "source": "proxy-region",
        "error": "",
    }


# --------------------------------------------------------------------------- #
# 本地链式中继：本进程 <-> 上游 <-> 代理商 <-> 目标
# --------------------------------------------------------------------------- #
def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("SOCKS5 客户端提前断开")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_socks_address(sock: socket.socket, atyp: int) -> tuple[str, int]:
    if atyp == 1:
        host = socket.inet_ntoa(_read_exact(sock, 4))
    elif atyp == 3:
        length = _read_exact(sock, 1)[0]
        host = _read_exact(sock, length).decode("idna")
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, _read_exact(sock, 16))
    else:
        raise ValueError("SOCKS5 地址类型无效")
    port = int.from_bytes(_read_exact(sock, 2), "big")
    return host, port


class ProxyChainError(ConnectionError):
    """链路上游返回的、面向用户的错误。"""


class SocksConnectError(ProxyChainError):
    """SOCKS5 代理已返回的 CONNECT 错误，不代表账号密码错误。"""

    def __init__(self, code: int):
        self.code = code
        reasons = {1: "代理服务器一般性失败", 2: "代理规则拒绝", 3: "网络不可达",
                   4: "目标主机不可达", 5: "目标端口拒绝连接", 6: "连接 TTL 超时",
                   7: "不支持 CONNECT 命令", 8: "不支持目标地址类型"}
        super().__init__(f"动态代理 CONNECT 失败（SOCKS5 code {code}：{reasons.get(code, '未知错误')}）")

    @property
    def retryable(self) -> bool:
        return self.code in {1, 3, 4, 6}


class ProxyChainRelay:
    """在本机随机端口开一个 HTTP CONNECT 代理，流量经上游发给目标代理商。"""

    def __init__(self, target: str, upstream: str = "", *, timeout: float = 15.0):
        self.target = parse_proxy_url(target)
        self.upstream = parse_proxy_url(upstream) if upstream else None
        if self.target.scheme not in {"http", "socks5", "socks5h"}:
            raise ValueError("代理链目标代理必须是 http://、socks5:// 或 socks5h://")
        if self.upstream and self.upstream.scheme == "https":
            raise ValueError("代理链暂不支持 https:// 作为上游代理，请填写 http:// 或 socks5://")
        if self.upstream and socks is None:
            raise RuntimeError("代理链需要 PySocks，请先 pip install pysocks")
        self.timeout = max(1.0, float(timeout or 15.0))
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        self._last_error = ""

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def proxy_url(self) -> str:
        if self._listener is None:
            raise RuntimeError("代理链尚未启动")
        return f"http://127.0.0.1:{self._listener.getsockname()[1]}"

    def start(self) -> "ProxyChainRelay":
        if self._listener is not None:
            return self
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        listener.settimeout(0.25)
        self._listener = listener
        self._accept_thread = threading.Thread(target=self._accept_loop, name="proxy-chain-accept", daemon=True)
        self._accept_thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            try:
                conn.close()
            except OSError:
                pass
        thread, self._accept_thread = self._accept_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def __enter__(self) -> "ProxyChainRelay":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _track(self, conn: socket.socket, add: bool) -> None:
        with self._connections_lock:
            if add:
                self._connections.add(conn)
            else:
                self._connections.discard(conn)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._track(client, True)
            threading.Thread(target=self._handle_client, args=(client,), name="proxy-chain-client", daemon=True).start()

    def _connect_target_through_upstream(self, host: str, port: int) -> socket.socket:
        if self.upstream is None:
            return socket.create_connection((host, port), timeout=self.timeout)
        proxy_type = socks.HTTP if self.upstream.scheme == "http" else socks.SOCKS5
        remote = socks.socksocket()
        remote.set_proxy(
            proxy_type,
            addr=self.upstream.host,
            port=self.upstream.port,
            username=self.upstream.username or None,
            password=self.upstream.password or None,
            rdns=self.upstream.scheme == "socks5h",
        )
        remote.settimeout(self.timeout)
        try:
            remote.connect((host, port))
        except Exception:
            remote.close()
            raise
        return remote

    @staticmethod
    def _pack_socks_address(host: str, port: int) -> bytes:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            encoded = host.encode("idna")
            if len(encoded) > 255:
                raise ValueError("SOCKS5 目标域名过长")
            return b"\x03" + bytes([len(encoded)]) + encoded + int(port).to_bytes(2, "big")
        if address.version == 4:
            return b"\x01" + address.packed + int(port).to_bytes(2, "big")
        return b"\x04" + address.packed + int(port).to_bytes(2, "big")

    @staticmethod
    def _read_socks_reply(sock: socket.socket) -> int:
        header = _read_exact(sock, 4)
        if header[0] != 5:
            raise ValueError("动态代理返回了非 SOCKS5 响应")
        _read_socks_address(sock, header[3])
        return header[1]

    def _target_socks_connect(self, remote: socket.socket, host: str, port: int) -> None:
        methods = b"\x00"
        if self.target.username or self.target.password:
            methods += b"\x02"
        remote.sendall(b"\x05" + bytes([len(methods)]) + methods)
        selected = _read_exact(remote, 2)
        if selected[0] != 5:
            response = selected
            try:
                response += remote.recv(512)
            except OSError:
                pass
            if response.startswith(b"HTTP/"):
                first_line = response.splitlines()[0].decode("latin1", errors="replace")
                raise ProxyChainError(f"动态代理经本地上游返回 {first_line}，不是 SOCKS5 响应")
            raise ProxyChainError("动态代理经本地上游返回了非 SOCKS5 响应")
        if selected[1] == 0xFF:
            raise ProxyChainError("动态代理不接受当前认证方式")
        if selected[1] == 2:
            username = self.target.username.encode("utf-8")
            password = self.target.password.encode("utf-8")
            remote.sendall(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
            auth = _read_exact(remote, 2)
            if auth[0] != 1 or auth[1] != 0:
                raise PermissionError("动态代理账号密码认证失败")
        elif selected[1] != 0:
            raise ConnectionError("动态代理选择了不支持的认证方式")

        remote.sendall(b"\x05\x01\x00" + self._pack_socks_address(host, port))
        reply_code = self._read_socks_reply(remote)
        if reply_code:
            raise SocksConnectError(reply_code)

    def _open_target_tunnel(self, host: str, port: int, method: str) -> socket.socket:
        """只在转发数据前重试握手，绝不重放 HTTP 请求或 TLS 数据。"""
        for attempt in range(1, 4):
            remote = None
            try:
                if self._stop.is_set():
                    raise ConnectionAbortedError("代理中继正在关闭")
                remote = self._connect_target_through_upstream(self.target.host, self.target.port)
                self._track(remote, True)
                if self.target.scheme in {"socks5", "socks5h"}:
                    self._target_socks_connect(remote, host, port)
                elif method == "CONNECT":
                    self._target_http_connect(remote, host, port)
                return remote
            except Exception as exc:
                if remote is not None:
                    self._track(remote, False)
                    try:
                        remote.close()
                    except OSError:
                        pass
                if not isinstance(exc, SocksConnectError) or not exc.retryable or attempt == 3 or self._stop.is_set():
                    raise
                if self._stop.wait(0.25 * attempt):
                    raise
        raise AssertionError("unreachable")

    def _target_http_connect(self, remote: socket.socket, host: str, port: int) -> None:
        """经上游连到 HTTP 目标代理，并在目标代理上建立 CONNECT 隧道。"""
        destination = f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"
        headers = [
            f"CONNECT {destination} HTTP/1.1",
            f"Host: {destination}",
            "Proxy-Connection: Keep-Alive",
        ]
        if self.target.username or self.target.password:
            credentials = f"{self.target.username or ''}:{self.target.password or ''}".encode("utf-8")
            headers.append("Proxy-Authorization: Basic " + base64.b64encode(credentials).decode("ascii"))
        remote.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("latin1"))

        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = remote.recv(4096)
            if not chunk:
                raise ConnectionError("HTTP 目标代理提前断开")
            response.extend(chunk)
            if len(response) > 64 * 1024:
                raise ValueError("HTTP 目标代理响应头过大")
        first_line = bytes(response).split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        parts = first_line.split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            raise ProxyChainError("目标代理返回了无效的 HTTP 响应")
        try:
            status = int(parts[1])
        except ValueError as exc:
            raise ProxyChainError("目标代理返回了无效的 HTTP 状态码") from exc
        if status != 200:
            raise ProxyChainError(f"目标代理 CONNECT 失败（HTTP {status}）")

    @staticmethod
    def _read_http_request(client: socket.socket) -> tuple[str, str, bytes]:
        request = bytearray()
        while b"\r\n\r\n" not in request:
            chunk = client.recv(4096)
            if not chunk:
                raise ConnectionError("HTTP 代理客户端提前断开")
            request.extend(chunk)
            if len(request) > 64 * 1024:
                raise ValueError("HTTP 代理请求头过大")
        first_line = bytes(request).split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        parts = first_line.split(" ", 2)
        if len(parts) != 3:
            raise ValueError("HTTP 代理请求行无效")
        return parts[0].upper(), parts[1], bytes(request)

    @staticmethod
    def _split_host_port(destination: str) -> tuple[str, int]:
        if destination.startswith("["):
            host, port_text = destination.rsplit("]:", 1)
            host = host[1:]
        else:
            host, port_text = destination.rsplit(":", 1)
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError("HTTP 代理目标端口无效")
        return host, port

    @staticmethod
    def _send_http_error(client: socket.socket) -> None:
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass

    def _handle_client(self, client: socket.socket) -> None:
        remote: socket.socket | None = None
        connect_response_sent = False
        destination_key: tuple[str, int] | None = None
        try:
            client.settimeout(self.timeout)
            method, destination, initial = self._read_http_request(client)
            if method == "CONNECT":
                host, port = self._split_host_port(destination)
                parsed = None
            else:
                parsed = urlsplit(destination)
                if parsed.scheme != "http" or not parsed.hostname:
                    raise ValueError("HTTP 代理请求必须使用 http:// 绝对 URL")
                host, port = parsed.hostname, parsed.port or 80
            destination_key = (host, port)

            remote = self._open_target_tunnel(host, port, method)
            remote.settimeout(0.5)
            if method == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\nConnection: keep-alive\r\n\r\n")
                connect_response_sent = True
                # CONNECT 头不走目标隧道；保留同一 recv 里提前到达的 TLS 数据。
                initial = initial.split(b"\r\n\r\n", 1)[1]
            else:
                header, body = initial.split(b"\r\n\r\n", 1)
                lines = header.split(b"\r\n")
                version = lines[0].rsplit(b" ", 1)[1]
                path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
                if self.target.scheme in {"socks5", "socks5h"}:
                    lines[0] = method.encode("ascii") + b" " + path.encode("ascii") + b" " + version
                lines = [line for line in lines
                         if not line.lower().startswith((b"proxy-authorization:", b"proxy-connection:"))]
                if not any(line.lower().startswith(b"upgrade:") for line in lines):
                    lines = [line for line in lines if not line.lower().startswith(b"connection:")]
                    lines.append(b"Connection: close")
                if self.target.scheme == "http" and (self.target.username or self.target.password):
                    credentials = f"{self.target.username or ''}:{self.target.password or ''}".encode("utf-8")
                    lines.append(b"Proxy-Authorization: Basic " + base64.b64encode(credentials))
                initial = b"\r\n".join(lines) + b"\r\n\r\n" + body
            if initial:
                remote.sendall(initial)
            client.settimeout(0.5)
            self._relay(client, remote)
        except Exception as exc:
            if self._stop.is_set():
                return
            detail = str(exc or "").strip()
            self._last_error = f"{type(exc).__name__}: {detail[:240]}" if detail else type(exc).__name__
            logger.warning(
                "代理链连接失败 target=%s upstream=%s destination=%s error=%s",
                mask_proxy_url(self.target.raw),
                mask_proxy_url(self.upstream.raw) if self.upstream else "direct",
                "%s:%s" % destination_key if destination_key else "unknown",
                self._last_error,
            )
            if not connect_response_sent:
                self._send_http_error(client)
        finally:
            for conn in (client, remote):
                if conn is not None:
                    self._track(conn, False)
                    try:
                        conn.close()
                    except OSError:
                        pass

    def _relay(self, client: socket.socket, remote: socket.socket) -> None:
        sockets = [client, remote]
        while not self._stop.is_set():
            try:
                readable, _, exceptional = select.select(sockets, [], sockets, 0.5)
            except (OSError, ValueError):
                return
            if exceptional:
                return
            if not readable:
                continue
            for source in readable:
                try:
                    data = source.recv(64 * 1024)
                except socket.timeout:
                    # select 与 recv 之间状态可能变化，短暂空读不要断开整条隧道。
                    continue
                if not data:
                    return
                recipient = remote if source is client else client
                recipient.sendall(data)


# --------------------------------------------------------------------------- #
# 代理选择：直连探测 / 链式
# --------------------------------------------------------------------------- #
@dataclass
class ResolvedProxy:
    url: str
    geo: dict
    relay: object | None = None
    chained: bool = False
    note: str = ""

    def close(self) -> None:
        relay, self.relay = self.relay, None
        if relay is not None:
            try:
                relay.close()
            except Exception:
                pass


def default_chain_upstream() -> str:
    try:
        from dotenv import load_dotenv  # 可选
        load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
    except Exception:
        pass
    return (os.getenv("PROXY_POOL_UPSTREAM_PROXY") or DEFAULT_CHAIN_UPSTREAM).strip()


def _merge_geo(geo: dict, target: dict) -> dict:
    country = geo.get("country") or target.get("country") or ""
    return {
        "ok": True,
        "ip": geo.get("ip") or target.get("ip") or "",
        "country": country,
        "currency": geo.get("currency") or CURRENCY_BY_COUNTRY.get(country, ""),
        "source": geo.get("source") or target.get("source") or "",
        "error": "",
    }


def resolve_proxy(value: str, impersonate: str, timeout: int, proxy_type: str = "auto",
                  chain_upstream: str | None = None, chain_only: bool = False) -> ResolvedProxy:
    """返回可用的代理（可能是本地链式中继的 http://127.0.0.1:port）。"""
    candidates = proxy_candidates(value)
    kind = (proxy_type or "auto").strip().lower()
    if kind == "http":
        candidates = [c for c in candidates if urlsplit(c).scheme.lower() in {"http", "https"}]
    elif kind == "socks5":
        candidates = [c for c in candidates if urlsplit(c).scheme.lower() in {"socks5", "socks5h"}]
    if not candidates:
        raise ValueError(f"代理类型 {kind} 与填写的代理不匹配")

    upstream = (default_chain_upstream() if chain_upstream is None else str(chain_upstream)).strip()
    upstream, upstream_note = pick_local_upstream(upstream)
    if upstream_note:
        logger.warning(upstream_note)
    chain_candidates = [c for c in candidates if urlsplit(c).scheme.lower() in {"http", "socks5", "socks5h"}]
    # 代理商常见只开 SOCKS5，所以链式优先 socks5h -> socks5 -> http
    chain_candidates.sort(key=lambda c: {"socks5h": 0, "socks5": 1, "http": 2}.get(urlsplit(c).scheme.lower(), 9))

    if chain_only:
        if not upstream:
            raise RuntimeError("已选择只走链式，但链式上游代理为空")
        if not chain_candidates:
            raise ValueError("链式代理只支持 http://、socks5://、socks5h://")
        candidate = chain_candidates[0]
        relay = None
        try:
            relay = ProxyChainRelay(candidate, upstream=upstream, timeout=max(20, min(int(timeout), 30)))
            relay.start()
            return ResolvedProxy(relay.proxy_url, geo_hint(value), relay=relay, chained=True,
                                 note=upstream_note)
        except Exception as exc:
            if relay is not None:
                relay.close()
            raise RuntimeError(f"链路启动失败（{mask_proxy(candidate)}）: {type(exc).__name__}: {exc}") from exc

    errors = []
    probe_timeout = max(5, min(int(timeout), 15))
    for candidate in candidates:
        geo = detect_exit(candidate, impersonate, probe_timeout)
        target = probe_chatgpt(candidate, impersonate, probe_timeout)
        if target.get("ok"):
            return ResolvedProxy(candidate, _merge_geo(geo, target))
        errors.append(f"直连 {mask_proxy(candidate)}: {target.get('error') or geo.get('error') or '连接失败'}")

    if upstream:
        for candidate in chain_candidates:
            relay = None
            success = False
            try:
                relay = ProxyChainRelay(candidate, upstream=upstream, timeout=max(20, min(int(timeout), 30)))
                relay.start()
                local_url = relay.proxy_url
                target = probe_chatgpt(local_url, impersonate, probe_timeout)
                if target.get("ok"):
                    success = True
                    country = target.get("country") or ""
                    geo = {
                        "ok": True,
                        "ip": target.get("ip") or "",
                        "country": country,
                        "currency": CURRENCY_BY_COUNTRY.get(country, ""),
                        "source": "chatgpt-trace(chain)",
                        "error": "",
                    }
                    return ResolvedProxy(local_url, geo, relay=relay, chained=True, note=upstream_note)
                errors.append(f"链式 {mask_proxy(candidate)}: {target.get('error') or '连接失败'}")
            except Exception as exc:
                errors.append(f"链式 {mask_proxy(candidate)}: {type(exc).__name__}: {exc}")
            finally:
                if not success and relay is not None:
                    relay.close()

    raise RuntimeError(
        "代理无法访问 chatgpt.com（直连和链式均失败），已尝试 "
        + " / ".join(mask_proxy(c) for c in candidates)
        + "；请确认协议、端口、账号密码。最后错误: " + (errors[-1] if errors else "未知错误")
    )


# --------------------------------------------------------------------------- #
# session 解析
# --------------------------------------------------------------------------- #
def parse_session(raw: str) -> tuple[str, str]:
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        raise ValueError("session 为空")
    if text.lower().startswith("authorization:"):
        text = text.split(":", 1)[1].strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    token = ""
    account_id = ""
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"session 不是合法 JSON: {exc}") from exc
        token = _find_token(data)
        account_id = _find_account_id(data)
    if not token:
        match = re.search(r'["\']?access(?:T|_t)oken["\']?\s*[:=]\s*["\']?([A-Za-z0-9_.-]+)', text)
        if match:
            token = match.group(1)
    if not token:
        match = JWT_RE.search(text)
        if match:
            token = match.group(0)
    if not token and re.fullmatch(r"[A-Za-z0-9_.-]{8,}", text):
        token = text
    if not token:
        raise ValueError("没有识别到 accessToken，请粘贴 /api/auth/session 的完整 JSON 或原始 token")
    if not account_id:
        account_id = _account_id_from_jwt(token)
    return token, account_id


def _find_token(value) -> str:
    if isinstance(value, dict):
        for key in ("accessToken", "access_token", "token"):
            token = value.get(key)
            if isinstance(token, str) and token.strip():
                return token.strip()
        for item in value.values():
            token = _find_token(item)
            if token:
                return token
    elif isinstance(value, list):
        for item in value:
            token = _find_token(item)
            if token:
                return token
    return ""


def _find_account_id(value) -> str:
    if isinstance(value, dict):
        account = value.get("account")
        if isinstance(account, dict):
            account_id = account.get("id")
            if isinstance(account_id, str) and account_id.strip():
                return account_id.strip()
        for key in ("accountId", "account_id", "chatgpt_account_id"):
            account_id = value.get(key)
            if isinstance(account_id, str) and account_id.strip():
                return account_id.strip()
        for item in value.values():
            account_id = _find_account_id(item)
            if account_id:
                return account_id
    elif isinstance(value, list):
        for item in value:
            account_id = _find_account_id(item)
            if account_id:
                return account_id
    return ""


def _account_id_from_jwt(token: str) -> str:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        auth = data.get("https://api.openai.com/auth") or {}
        return str(auth.get("chatgpt_account_id") or "").strip()
    except Exception:
        return ""


def read_session(value: str | None) -> tuple[str, str]:
    if value:
        path = Path(value).expanduser()
        if path.is_file():
            return parse_session(path.read_text(encoding="utf-8", errors="ignore"))
        return parse_session(value)
    typed = input("session（JSON 或 token，也可直接填文件路径）: ")
    return parse_session(typed)


# --------------------------------------------------------------------------- #
# 出口探测
# --------------------------------------------------------------------------- #
def detect_exit(proxy: str, impersonate: str, timeout: int) -> dict:
    result = {"ok": False, "ip": "", "country": "", "currency": "", "source": "", "error": ""}
    try:
        resp = requests.get(GEO_URL, impersonate=impersonate, proxies=proxy_map(proxy), timeout=timeout)
        if resp.status_code < 300:
            data = resp.json()
            result.update({
                "ip": str(data.get("ip") or ""),
                "country": str(data.get("country_code") or data.get("country") or "").upper(),
                "currency": str(data.get("currency") or "").upper(),
                "source": "ipapi",
            })
            if result["country"]:
                result["ok"] = True
                return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    try:
        resp = requests.get(TRACE_URL, impersonate=impersonate, proxies=proxy_map(proxy), timeout=timeout)
        text = resp.text or ""
        ip_match = re.search(r"^ip=(.+)$", text, re.MULTILINE)
        loc_match = re.search(r"^loc=(.+)$", text, re.MULTILINE)
        if ip_match:
            result["ip"] = ip_match.group(1).strip()
        if loc_match:
            result["country"] = loc_match.group(1).strip().upper()
            result["source"] = "chatgpt-trace"
        if result["ip"] or result["country"]:
            result["ok"] = True
            result["error"] = ""
    except Exception as exc:
        if not result["error"]:
            result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def probe_chatgpt(proxy: str, impersonate: str, timeout: int) -> dict:
    try:
        resp = requests.get(TRACE_URL, impersonate=impersonate, proxies=proxy_map(proxy), timeout=timeout)
    except Exception as exc:
        return {"ok": False, "ip": "", "country": "", "source": "", "error": f"{type(exc).__name__}: {exc}"}
    text = resp.text or ""
    if resp.status_code >= 300:
        return {"ok": False, "ip": "", "country": "", "source": "", "error": f"HTTP {resp.status_code}"}
    ip_match = re.search(r"^ip=(.+)$", text, re.MULTILINE)
    loc_match = re.search(r"^loc=(.+)$", text, re.MULTILINE)
    ip = ip_match.group(1).strip() if ip_match else ""
    country = loc_match.group(1).strip().upper() if loc_match else ""
    if ip or country:
        return {"ok": True, "ip": ip, "country": country, "source": "chatgpt-trace", "error": ""}
    return {"ok": False, "ip": "", "country": "", "source": "", "error": "trace 响应缺少 ip/loc"}


# --------------------------------------------------------------------------- #
# 请求头 / 优惠查询
# --------------------------------------------------------------------------- #
def _base_headers(token: str, account_id: str, device_id: str, ua: str, referer: str,
                  originator: str, frontend: str) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": referer,
        "User-Agent": ua,
        "oai-device-id": device_id,
        "oai-did": device_id,
        "oai-language": "en-US",
        "oai-telemetry": "[1,null]",
        "originator": originator,
        "x-openai-checkout-source": "chatgpt_web_merged",
        "x-openai-codex-window-type": "browser",
        "x-openai-web-frontend": frontend,
        "x-openai-rate-limit-join-workspace": "1",
        "x-openai-rejoin-pro-cta": "1",
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id
    return headers


def query_coupon(token: str, account_id: str, device_id: str, promo: str, proxy: str,
                 impersonate: str, ua: str, originator: str, frontend: str, timeout: int,
                 retries: int = 1) -> dict:
    """返回 {ok, eligible, state, status, detail}；status=0 表示传输层失败（多为代理问题）。"""
    url = f"{COUPON_URL}?coupon={promo}&is_coupon_from_query_param=false"
    headers = _base_headers(token, account_id, device_id, ua, "https://chatgpt.com/", originator, frontend)
    last_exc = None
    resp = None
    attempts = max(0, int(retries)) + 1
    for attempt in range(attempts):
        try:
            resp = requests.get(url, headers=headers, impersonate=impersonate,
                                proxies=proxy_map(proxy), timeout=timeout)
            break
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(0.5)
    if resp is None:
        return {"ok": False, "eligible": False, "state": "", "status": 0,
                "detail": f"{type(last_exc).__name__}: {last_exc}"}
    text = resp.text or ""
    try:
        data = resp.json()
    except Exception:
        data = None
    state = str((data or {}).get("state") or "") if isinstance(data, dict) else ""
    if resp.status_code == 200 and isinstance(data, dict):
        return {"ok": True, "eligible": state.lower() == "eligible",
                "state": state, "status": resp.status_code, "detail": data}
    return {"ok": False, "eligible": False, "state": state, "status": resp.status_code,
            "detail": data if isinstance(data, dict) else text[:300]}


def check_promo(token: str, account_id: str, device_id: str, promo: str, proxy: str,
                impersonate: str, ua: str, originator: str, frontend: str, timeout: int) -> bool:
    """兼容旧接口：查询失败按"有优惠"处理，避免误拦。"""
    result = query_coupon(token, account_id, device_id, promo, proxy,
                          impersonate, ua, originator, frontend, timeout)
    if not result.get("ok"):
        return True
    return bool(result.get("eligible"))


# --------------------------------------------------------------------------- #
# sentinel
# --------------------------------------------------------------------------- #
def resolve_node_executable() -> str:
    """找到 Node.js 可执行文件；找不到返回空串。

    打包成 exe 后 PATH 可能和终端不同（例如 nvm 的 current 目录），
    所以额外探测常见安装位置。
    """
    override = str(os.getenv("NODE_EXECUTABLE") or "").strip()
    if override and Path(override).exists():
        return override
    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        return found
    candidates: list[Path] = []
    for env_name in ("NVM_SYMLINK", "NVM_HOME"):
        base = str(os.getenv(env_name) or "").strip()
        if base:
            candidates.append(Path(base) / "node.exe")
    program_files = os.getenv("ProgramFiles") or r"C:\Program Files"
    program_files_x86 = os.getenv("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    candidates += [
        Path(program_files) / "nodejs" / "node.exe",
        Path(program_files_x86) / "nodejs" / "node.exe",
        Path(os.getenv("LOCALAPPDATA") or "") / "Programs" / "nodejs" / "node.exe",
        Path(r"D:\nvm\current\node.exe"),
    ]
    for path in candidates:
        if str(path) and path.exists():
            return str(path)
    return ""


def sentinel_available() -> tuple[bool, str]:
    """sentinel 依赖是否就绪；返回 (可用, 说明)。"""
    try:
        import core.sentinel  # noqa: F401
        import core.sentinel_runner  # noqa: F401
    except Exception as exc:
        return False, f"缺少 sentinel 模块: {type(exc).__name__}: {exc}"
    if not resolve_node_executable():
        return False, "未找到 Node.js（sentinel 需要用 node 运行 sdk.js）"
    return True, ""


def build_sentinel_token(token: str, account_id: str, device_id: str, proxy: str,
                         impersonate: str, ua: str, timeout: int) -> tuple[str, str]:
    """生成 (openai-sentinel-token, openai-sentinel-so-token) 一对请求头值。

    checkout 建单必须带 sentinel；缺 token 会得到
    400 "Our systems have detected unusual activity"。
    SO 只在该 flow 返回时才有，没有就是空串。
    """
    node = resolve_node_executable()
    if node:
        # core.sentinel_runner 只认 "node" / NODE_EXECUTABLE，先固定成绝对路径。
        os.environ["NODE_EXECUTABLE"] = node
    try:
        from core.sentinel import generate_requirements_token
        from core.sentinel_runner import generate_sentinel_token
    except Exception as exc:
        raise RuntimeError(
            "sentinel 模块不可用（缺少 core/sentinel.py、core/sentinel_runner.py 或 config 包）: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not node:
        raise RuntimeError(
            "未找到 Node.js。sentinel 需要用 node 运行 sentinel/sdk.js；"
            "装个 Node.js 或把 node.exe 路径写进环境变量 NODE_EXECUTABLE 即可。"
        )

    flow = "chatgpt_checkout"
    p = generate_requirements_token(device_id)
    body = json.dumps({"p": p, "id": device_id, "flow": flow}, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": ua,
        "oai-device-id": device_id,
    }
    resp = requests.post(SENTINEL_URL, headers=headers, data=body, impersonate=impersonate,
                         proxies=proxy_map(proxy), timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"sentinel/req HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    challenge = resp.json()
    challenge["_request_p"] = p
    raw_token = generate_sentinel_token(
        challenge=challenge,
        flow=flow,
        device_id=device_id,
        user_agent=ua,
        page_url="https://chatgpt.com/",
        cookie=f"oai-did={device_id}",
    )
    parsed = json.loads(raw_token)
    so_value = parsed.pop("_so", None) or parsed.pop("so", None)
    header = json.dumps(parsed, separators=(",", ":"))
    so_header = ""
    if so_value:
        so_header = json.dumps(
            {"so": so_value, "c": parsed.get("c") or challenge.get("token", ""),
             "id": device_id, "flow": flow},
            separators=(",", ":"),
        )
    return header, so_header


# --------------------------------------------------------------------------- #
# checkout
# --------------------------------------------------------------------------- #
def build_payload(plan_name: str, country: str, currency: str, promo: str | None) -> dict:
    cancel_url = "https://chatgpt.com/?cta_tab=personal&highlight_plan=plus&codex_upgrade_location=sidebar+profile"
    if promo:
        cancel_url += f"&promo_campaign={promo}"
    payload = {
        "plan_name": plan_name,
        "checkout_source": "chatgpt_web_merged",
        "entry_point": "all_plans_pricing_modal",
        "billing_details": {"country": country, "currency": currency},
        "supports_saved_payment_link": True,
        "checkout_ui_mode": "custom",
        "checkout_appearance": "light",
        "cancel_url": cancel_url + "#pricing",
        "purchase_attempt_id": str(uuid.uuid4()),
    }
    if promo:
        payload["promo_campaign"] = {
            "promo_campaign_id": promo,
            "is_coupon_from_query_param": False,
        }
    return payload


def parse_checkout(data: dict, country: str) -> dict:
    session_id = ""
    for key in ("checkout_session_id", "session_id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            session_id = value.strip()
            break
    if not session_id:
        for key in ("success_url", "cancel_url", "return_url", "url", "stripe_hosted_url",
                    "checkout_url", "client_secret", "confirm_return_url"):
            match = SESSION_RE.search(str(data.get(key) or ""))
            if match:
                session_id = match.group(1)
                break
    if not session_id:
        match = SESSION_RE.search(json.dumps(data, ensure_ascii=False))
        if match:
            session_id = match.group(1)

    entity = str(data.get("processor_entity") or "").strip()
    if not entity:
        blob = " ".join(str(data.get(key) or "") for key in
                        ("success_url", "cancel_url", "url", "stripe_hosted_url",
                         "checkout_url", "client_secret", "confirm_return_url"))
        match = ENTITY_RE.search(blob)
        if match:
            entity = match.group(1)
    if not entity:
        entity = "openai_ie" if country.upper() in EU_COUNTRIES else "openai_llc"

    long_url = ""
    for key in ("url", "stripe_hosted_url", "checkout_url"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith("http"):
            long_url = value
            break

    short_url = f"https://chatgpt.com/checkout/{entity}/{session_id}" if session_id else ""
    return {"short_url": short_url, "long_url": long_url,
            "checkout_session_id": session_id, "processor_entity": entity}


def create_short_link(token: str, account_id: str, device_id: str, proxy: str, country: str,
                      currency: str, plan_name: str, promo: str | None, impersonate: str,
                      ua: str, originator: str, frontend: str, sentinel_mode: str, timeout: int) -> dict:
    referer = "https://chatgpt.com/?cta_tab=personal&highlight_plan=plus&codex_upgrade_location=sidebar+profile"
    if promo:
        referer += f"&promo_campaign={promo}"

    sentinel = ""
    sentinel_so = ""
    sentinel_error = ""
    if sentinel_mode != "off":
        try:
            sentinel, sentinel_so = build_sentinel_token(token, account_id, device_id, proxy, impersonate, ua, timeout)
        except Exception as exc:
            sentinel_error = f"{type(exc).__name__}: {exc}"
            if sentinel_mode == "require":
                raise RuntimeError(f"sentinel 生成失败: {sentinel_error}") from exc
            print(f"# 警告: sentinel 生成失败，继续无 sentinel 请求: {sentinel_error}", file=sys.stderr)

    headers = _base_headers(token, account_id, device_id, ua, referer, originator, frontend)
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    if sentinel_so:
        headers["openai-sentinel-so-token"] = sentinel_so

    payload = build_payload(plan_name, country, currency, promo)
    try:
        resp = requests.post(
            CHECKOUT_URL,
            headers=headers,
            json=payload,
            impersonate=impersonate,
            proxies=proxy_map(proxy),
            timeout=timeout,
        )
    except Exception as exc:
        raise RuntimeError(f"请求 checkout 失败（代理或网络问题）: {type(exc).__name__}: {exc}") from exc

    text = resp.text or ""
    try:
        data = resp.json()
    except Exception:
        data = None

    if resp.status_code == 401:
        raise RuntimeError("session/accessToken 已失效，请重新获取 /api/auth/session")
    if resp.status_code == 403:
        raise RuntimeError(f"被 Cloudflare/风控拦截（403），换代理出口或重试。响应: {text[:300]}")
    if resp.status_code < 200 or resp.status_code >= 300:
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("detail") or data.get("message") or "")
        detail = detail or text[:300]
        if resp.status_code == 400 and "unusual activity" in text.lower():
            if sentinel_error:
                reason = f"sentinel 没生成成功（{sentinel_error}），请求是裸奔去的"
            elif not sentinel:
                reason = "sentinel 为空"
            else:
                reason = "sentinel 已带上但网关仍判风控（多为出口 IP / 账号环境问题，换个代理再试）"
            raise RuntimeError(f"checkout HTTP 400 风控拦截: {detail.strip()} —— {reason}")
        raise RuntimeError(f"checkout HTTP {resp.status_code}: {detail}")
    if not isinstance(data, dict):
        raise RuntimeError(f"checkout 返回不是 JSON: {text[:300]}")

    parsed = parse_checkout(data, country)
    if not parsed["short_url"]:
        raise RuntimeError(f"响应里没有 checkout_session_id，字段: {', '.join(list(data)[:15])}")
    return {
        **parsed,
        "payload": payload,
        "sentinel_used": bool(sentinel),
        "sentinel_so_used": bool(sentinel_so),
        "sentinel_error": sentinel_error,
        "billing_details": data.get("billing_details") or payload.get("billing_details"),
        "promo_campaign": data.get("promo_campaign"),
        "checkout_state": data.get("checkout_state"),
        "status": data.get("status"),
        "payment_status": data.get("payment_status"),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="输入 ChatGPT session + 代理，生成该代理出口下的 Plus 支付短链",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--session", help="session JSON、原始 token，或包含它们的文件路径；不传则交互输入")
    parser.add_argument("--proxy", help="代理 URL、host:port 或 host:port:user:pass；不传则交互输入")
    parser.add_argument("--proxy-type", choices=["auto", "http", "socks5"], default="auto",
                        help="代理类型；默认 auto 自动探测 HTTP/SOCKS5")
    parser.add_argument("--chain-upstream", default=None,
                        help=f"链式上游代理；默认读环境变量 PROXY_POOL_UPSTREAM_PROXY，回退 {DEFAULT_CHAIN_UPSTREAM}；传空字符串禁用")
    parser.add_argument("--chain-only", dest="chain_only", action="store_true", default=True,
                        help="默认只走链式；如需允许直连用 --no-chain-only")
    parser.add_argument("--no-chain-only", dest="chain_only", action="store_false",
                        help="允许直连（直连失败仍会回退链式）")
    parser.add_argument("--country", help="强制计费国家，如 PH / JP / US；默认按代理出口自动识别")
    parser.add_argument("--currency", help="强制币种，如 PHP / JPY / USD；默认按国家推断")
    parser.add_argument("--plan-name", default="chatgptplusplan", help="套餐名，默认 chatgptplusplan")
    parser.add_argument("--promo", default=DEFAULT_PROMO,
                        help=f"活动 ID，默认 {DEFAULT_PROMO}；传空字符串关闭")
    parser.add_argument("--no-promo", action="store_true", help="关闭试用活动 promo_campaign")
    parser.add_argument("--sentinel", choices=["auto", "off", "require"], default="auto",
                        help="sentinel 模式，默认 auto")
    parser.add_argument("--impersonate", default=DEFAULT_IMPERSONATE, help="curl_cffi 浏览器指纹，默认 chrome")
    parser.add_argument("--user-agent", default=DEFAULT_UA, help="请求 UA")
    parser.add_argument("--originator", default="Codex Browser", help="originator 请求头")
    parser.add_argument("--frontend", default="codex_webview", help="x-openai-web-frontend 请求头")
    parser.add_argument("--timeout", type=int, default=60, help="请求超时秒数，默认 60")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON，而不是只输出短链")
    args = parser.parse_args()

    handle = None
    try:
        token, account_id = read_session(args.session)
        proxy_input = args.proxy or input("代理（URL / host:port / host:port:user:pass）: ")
        handle = resolve_proxy(proxy_input, args.impersonate, args.timeout,
                               args.proxy_type, args.chain_upstream, args.chain_only)
        proxy = handle.url
        geo = handle.geo
        country = (args.country or geo.get("country") or "US").upper()
        currency = (args.currency or geo.get("currency") or CURRENCY_BY_COUNTRY.get(country) or "USD").upper()

        device_id = str(uuid.uuid4())
        promo = None if args.no_promo else (args.promo or "").strip()
        if promo:
            coupon = query_coupon(
                token, account_id, device_id, promo, proxy,
                args.impersonate, args.user_agent, args.originator, args.frontend, args.timeout,
                0 if args.chain_only else 1,
            )
            if not coupon.get("ok"):
                detail = coupon.get("detail")
                if isinstance(detail, dict):
                    detail = detail.get("detail") or detail.get("error") or detail.get("message") or detail
                print(f"错误: 优惠查询失败（HTTP {coupon.get('status')}）: {detail}", file=sys.stderr)
                return 1
            if not coupon.get("eligible"):
                print(
                    f"错误: 无优惠（state={coupon.get('state') or 'ineligible'}），已停止生成短链；"
                    "可用 --no-promo 跳过优惠直接生成",
                    file=sys.stderr,
                )
                return 1

        result = create_short_link(
            token=token,
            account_id=account_id,
            device_id=device_id,
            proxy=proxy,
            country=country,
            currency=currency,
            plan_name=args.plan_name,
            promo=promo,
            impersonate=args.impersonate,
            ua=args.user_agent,
            originator=args.originator,
            frontend=args.frontend,
            sentinel_mode=args.sentinel,
            timeout=args.timeout,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if handle is not None:
            handle.close()

    if args.json:
        raw_state = result.get("checkout_state") if isinstance(result.get("checkout_state"), dict) else {}
        output = {
            "ok": True,
            "short_url": result["short_url"],
            "long_url": result["long_url"],
            "checkout_session_id": result["checkout_session_id"],
            "processor_entity": result["processor_entity"],
            "country": country,
            "currency": currency,
            "billing_details": result.get("billing_details"),
            "promo_applied": bool(promo),
            "promo_campaign": result.get("promo_campaign"),
            "proxy_ip": geo.get("ip"),
            "geo_source": geo.get("source"),
            "chained": bool(handle.chained) if handle else False,
            "sentinel_used": result.get("sentinel_used"),
            "sentinel_error": result.get("sentinel_error") or None,
            "email": raw_state.get("email") if isinstance(raw_state, dict) else None,
            "status": result.get("status"),
            "payment_status": result.get("payment_status"),
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        if geo.get("source"):
            print(f"# 代理出口: {geo.get('ip') or '-'} / {country} / {currency} ({geo.get('source')})",
                  file=sys.stderr)
        print(result["short_url"])
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
