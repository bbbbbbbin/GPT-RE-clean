# -*- coding: utf-8 -*-
"""RoxyBrowser API -> OpenBrowser 本地 API 兼容桥接。

项目里的 roxy 驱动只认 Roxy 的 /browser/create、open、close、delete 接口。
本桥接把这些接口映射到 OpenBrowser 的本地 API（127.0.0.1:50325）：

    create -> POST /api/v1/user/create         （新版创建环境接口）
    open   -> POST /api/v1/browser/start       （返回 CDP debug_port）
    close  -> POST /api/v1/browser/stop
    delete -> POST /api/v1/user/delete

每个任务创建新的 OpenBrowser 环境（随机 OS/分辨率/指纹 persona），
关闭后删除，不会复用或污染已有环境。

配置（项目根目录 .env）：
    OPENBROWSER_API_BASE="http://127.0.0.1:50325"
    OPENBROWSER_API_KEY="<OpenBrowser 设置里的本地 API key>"
    ROXY_API_BASE="http://127.0.0.1:50100"   # 本项目指向本桥接
"""
from __future__ import annotations

import logging
import socket
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests
from flask import Flask, jsonify, request
from werkzeug.serving import make_server

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPENBROWSER_API = (os.environ.get("OPENBROWSER_API_BASE") or "http://127.0.0.1:50325").rstrip("/")
OPENBROWSER_KEY = (os.environ.get("OPENBROWSER_API_KEY") or "").strip()
DRIVER_PATH = (os.environ.get("OPENBROWSER_DRIVER") or "").strip() or str(
    PROJECT_ROOT / "tools" / "drivers" / "chromedriver-149" / "chromedriver.exe"
)
HEADLESS = str(os.environ.get("ROXY_OPEN_HEADLESS") or "False").strip().lower() in ("1", "true", "yes", "on")
PROFILE_TIMEZONE = str(os.environ.get("OPENBROWSER_PROFILE_TIMEZONE") or "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("openbrowser-bridge")

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

_lock = threading.Lock()
_created: dict[str, dict] = {}
_by_name: dict[str, str] = {}


class OpenBrowserError(RuntimeError):
    pass


class OpenBrowserClient:
    def __init__(self, base: str, key: str):
        self.base = base.rstrip("/")
        self.api_key = key
        self.http = requests.Session()
        self.http.trust_env = False
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if key:
            headers["api-key"] = key
        self.http.headers.update(headers)

    def request(self, method: str, path: str, body: dict | None = None, timeout: int = 90) -> dict:
        url = f"{self.base}{path}"
        try:
            resp = self.http.request(method, url, json=body, timeout=timeout)
        except requests.RequestException as exc:
            raise OpenBrowserError(f"OpenBrowser 连接失败 {method} {path}: {exc}") from exc
        text = resp.text or ""
        if resp.status_code == 401:
            raise OpenBrowserError("OpenBrowser 鉴权失败（401）：请检查 .env 里的 OPENBROWSER_API_KEY")
        if not (200 <= resp.status_code < 300):
            raise OpenBrowserError(f"OpenBrowser {method} {path} HTTP {resp.status_code}: {text[:400]}")
        try:
            payload = resp.json() if text.strip() else {}
        except ValueError as exc:
            raise OpenBrowserError(f"OpenBrowser 返回非 JSON：{text[:200]}") from exc
        if isinstance(payload, dict) and payload.get("code") not in (None, 0):
            msg = payload.get("msg") or payload.get("error") or text[:400]
            raise OpenBrowserError(f"OpenBrowser {method} {path} 失败: {msg}")
        return payload if isinstance(payload, dict) else {"data": payload}


openbrowser = OpenBrowserClient(OPENBROWSER_API, OPENBROWSER_KEY)


def _ok(data: dict | None = None):
    return jsonify({"code": 0, "msg": "ok", "data": data or {}})


def _err(message: str, status: int = 502):
    LOG.warning("%s", message)
    return jsonify({"code": status, "msg": message, "data": None}), status


def _body() -> dict:
    raw = request.get_json(silent=True)
    if isinstance(raw, dict):
        return raw
    if request.form:
        return request.form.to_dict()
    return {}


def _profile_id(body: dict) -> str:
    for key in ("dirId", "profileId", "profile_id", "id"):
        value = body.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _proxy_info_to_url(info) -> str:
    if not isinstance(info, dict) or not info:
        return ""
    host = str(info.get("host") or "").strip()
    port = str(info.get("port") or "").strip()
    if not host or not port:
        return ""
    protocol = str(info.get("protocol") or info.get("proxyCategory") or "http").strip().upper()
    scheme = {
        "HTTP": "http",
        "HTTPS": "https",
        "SOCKS5": "socks5",
        "SOCKS5H": "socks5",
    }.get(protocol, protocol.lower())
    user = str(info.get("proxyUserName") or info.get("username") or "")
    password = str(info.get("proxyPassword") or info.get("password") or "")
    auth = ""
    if user or password:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    return f"{scheme}://{auth}{host}:{port}"


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "name": "openbrowser-roxy-bridge",
        "openbrowser_api": OPENBROWSER_API,
        "api_key_prefix": OPENBROWSER_KEY[:6] if OPENBROWSER_KEY else "",
        "api_key_len": len(OPENBROWSER_KEY),
    })


@app.route("/browser/workspace", methods=["GET", "POST"])
def workspace():
    return _ok(
        {
            "rows": [
                {
                    "id": "openbrowser",
                    "workspaceName": "OpenBrowser",
                    "project_details": [{"projectId": "default", "projectName": "Automation"}],
                }
            ]
        }
    )


@app.route("/browser/create", methods=["POST"])
def browser_create():
    body = _body()
    name = str(body.get("name") or "").strip() or f"ob-{int(time.time() * 1000)}"
    with _lock:
        existing = _by_name.get(name)
        if existing and existing in _created:
            LOG.info("create 幂等命中：name=%s id=%s", name, existing)
            return _ok({"id": existing, "dirId": existing, "name": name})

    proxy_url = _proxy_info_to_url(body.get("proxyInfo"))
    timezone = PROFILE_TIMEZONE
    privacy = dict(body.get("privacy") or {}) if isinstance(body.get("privacy"), dict) else {}
    privacy.update(
        {
            "timezoneMode": "custom" if timezone else "ip",
            "timezone": timezone,
            "timezoneFromIp": not bool(timezone),
            "languageMode": "ip",
            "langFromIp": True,
            "uiLanguage": "profile",
            "deviceProfile": "default",
            "webgl": "noise",
            "webglMeta": "noise",
            "canvas": "noise",
            "audio": "noise",
        }
    )
    payload = {
        "name": name,
        "title": name,
        "headless": HEADLESS,
        "language": "en-US",
        "privacy": privacy,
    }
    if proxy_url:
        payload["proxy"] = proxy_url
    LOG.info(
        "创建 OpenBrowser 环境：name=%s headless=%s tz=%s proxy=%s",
        name,
        HEADLESS,
        timezone or "ip",
        "yes" if proxy_url else "no",
    )
    try:
        data = openbrowser.request("POST", "/api/v1/user/create", payload, timeout=60)
    except OpenBrowserError as exc:
        return _err(str(exc))
    result = data.get("data") or data
    profile_id = str(result.get("profile_id") or result.get("user_id") or result.get("id") or "").strip()
    if not profile_id:
        return _err(f"OpenBrowser 返回中缺少 profile id: {data}")
    with _lock:
        _created[profile_id] = {"name": name, "running": False, "port": None}
        _by_name[name] = profile_id
    LOG.info("OpenBrowser 环境已创建：id=%s", profile_id)
    return _ok({"id": profile_id, "dirId": profile_id, "name": name})


def _live_debug_port(profile_id: str):
    try:
        data = openbrowser.request("GET", "/api/v1/user/list", timeout=15)
        items = ((data.get("data") or {}).get("list") or []) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            ids = {str(item.get(k) or "").strip() for k in ("id", "user_id", "profile_id")}
            if profile_id not in ids:
                continue
            status = str(item.get("status") or "").strip().lower()
            port = item.get("debug_port")
            if port and status not in ("inactive", "closed", "stopped", ""):
                return int(port)
    except Exception:
        pass
    return None


@app.route("/browser/open", methods=["POST", "GET"])
def browser_open():
    body = _body()
    profile_id = _profile_id(body)
    if not profile_id:
        return _err("open 缺少 dirId/profileId", 400)
    with _lock:
        meta = _created.get(profile_id)
        cached_port = meta.get("port") if meta and meta.get("running") else None
    if cached_port:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.4)
                if probe.connect_ex(("127.0.0.1", int(cached_port))) == 0:
                    return _ok(_open_payload(profile_id, cached_port))
        except Exception:
            pass
        with _lock:
            item = _created.get(profile_id)
            if item is not None:
                item["running"] = False
                item["port"] = None
        LOG.warning("缓存 debug port 已失效，重新启动环境：id=%s port=%s", profile_id, cached_port)
    live_port = _live_debug_port(profile_id)
    if live_port:
        with _lock:
            item = _created.get(profile_id)
            if item is None:
                item = {"name": "", "running": True, "port": live_port, "external": True}
                _created[profile_id] = item
            else:
                item["port"] = live_port
                item["running"] = True
        LOG.info("复用已运行 OpenBrowser 环境：id=%s debug_port=%s", profile_id, live_port)
        return _ok(_open_payload(profile_id, live_port))

    LOG.info("启动 OpenBrowser 环境：id=%s", profile_id)
    try:
        data = openbrowser.request("POST", "/api/v1/browser/start", {"profile_id": profile_id}, timeout=150)
    except OpenBrowserError as exc:
        return _err(str(exc))
    result = data.get("data") or data
    port = result.get("debug_port")
    if not port:
        return _err(f"OpenBrowser 未返回 debug_port: {data}")
    with _lock:
        item = _created.get(profile_id)
        if item is None:
            item = {"name": "", "running": False, "port": None, "external": True}
            _created[profile_id] = item
        item["port"] = int(port)
        item["running"] = True
    LOG.info("OpenBrowser 环境已启动：id=%s debug_port=%s", profile_id, port)
    return _ok(_open_payload(profile_id, port))


@app.route("/browser/proxy", methods=["POST", "GET"])
def browser_proxy():
    body = _body()
    profile_id = _profile_id(body)
    if not profile_id:
        return _err("proxy 缺少 dirId/profileId", 400)
    proxy_info = body.get("proxyInfo") if isinstance(body.get("proxyInfo"), dict) else None
    proxy_url = _proxy_info_to_url(proxy_info) if proxy_info else str(body.get("proxy") or "").strip()
    if not proxy_url:
        return _err("proxy 缺少代理地址", 400)
    parsed = urlparse(proxy_url)
    if not parsed.hostname or not parsed.port:
        return _err(f"代理格式缺少 host/port: {proxy_url}", 400)
    proxy_type = (parsed.scheme or "http").lower()
    if proxy_type == "socks":
        proxy_type = "socks5"
    payload = {
        "profile_id": profile_id,
        "network_mode": "proxy",
        "user_proxy_config": {
            "proxy_type": proxy_type,
            "proxy_host": parsed.hostname,
            "proxy_port": int(parsed.port),
            "proxy_user": unquote(parsed.username or ""),
            "proxy_password": unquote(parsed.password or ""),
        },
    }
    try:
        data = openbrowser.request("POST", "/api/v2/browser-profile/update", payload, timeout=60)
    except OpenBrowserError as exc:
        return _err(str(exc))
    LOG.info("更新 OpenBrowser 环境代理：id=%s type=%s host=%s port=%s", profile_id, proxy_type, parsed.hostname, parsed.port)
    return _ok(data.get("data") or data)


def _open_payload(profile_id: str, port) -> dict:
    addr = f"127.0.0.1:{port}"
    return {
        "dirId": profile_id,
        "debuggerAddress": addr,
        "debuggingPort": int(port),
        "driver": DRIVER_PATH,
        "wsEndpoint": f"http://127.0.0.1:{port}",
        "ws": f"http://127.0.0.1:{port}",
    }


@app.route("/browser/close", methods=["POST", "GET"])
def browser_close():
    body = _body()
    profile_id = _profile_id(body)
    if not profile_id:
        return _err("close 缺少 dirId/profileId", 400)
    with _lock:
        meta = _created.get(profile_id)
    try:
        openbrowser.request("POST", "/api/v1/browser/stop", {"profile_id": profile_id}, timeout=60)
    except OpenBrowserError as exc:
        LOG.warning("关闭 OpenBrowser 环境失败：id=%s %s", profile_id, exc)
    if meta is not None:
        with _lock:
            meta["running"] = False
            meta["port"] = None
    LOG.info("OpenBrowser 环境已停止：id=%s", profile_id)
    return _ok({"closed": True})


@app.route("/browser/delete", methods=["POST", "GET"])
def browser_delete():
    body = _body()
    ids = body.get("dirIds")
    if not isinstance(ids, list) or not ids:
        single = _profile_id(body)
        ids = [single] if single else []
    deleted = []
    for raw in ids:
        profile_id = str(raw).strip()
        with _lock:
            meta = _created.get(profile_id)
        if meta is None:
            LOG.info("跳过删除：%s 不是本桥接创建的环境", profile_id)
            continue
        try:
            try:
                openbrowser.request("POST", "/api/v1/browser/stop", {"profile_id": profile_id}, timeout=30)
            except OpenBrowserError:
                pass
            openbrowser.request("POST", "/api/v1/user/delete", {"id": profile_id, "profile_id": profile_id, "user_id": profile_id}, timeout=60)
        except OpenBrowserError as exc:
            LOG.info("删除 OpenBrowser 环境返回：id=%s %s", profile_id, exc)
        with _lock:
            _created.pop(profile_id, None)
            _by_name.pop(meta.get("name"), None)
        deleted.append(profile_id)
        LOG.info("OpenBrowser 环境已删除：id=%s", profile_id)
    return _ok({"deleted": True, "dirIds": deleted})


def main() -> None:
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "50100"))
    LOG.info("OpenBrowser 桥接启动：http://%s:%s -> %s", host, port, OPENBROWSER_API)
    if not OPENBROWSER_KEY:
        LOG.warning("OPENBROWSER_API_KEY 为空：请填写 OpenBrowser 本地 API key")
    server = make_server(host, port, app, threaded=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

