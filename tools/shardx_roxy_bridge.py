# -*- coding: utf-8 -*-
"""RoxyBrowser API -> ShardX Launcher API 兼容桥接。

项目里的 roxy 驱动只认 RoxyBrowser 的本地接口（/browser/create、open、close、
delete、workspace）。本脚本在本机实现这些接口，并转发到 ShardBrowser 的
ShardX Launcher API，只创建 **temporary** Profile（浏览器关闭后自动删除），
不会读取或改动已有的持久 Profile。

配置（项目根目录 .env）：
    SHARDX_API="http://127.0.0.1:41000"
    SHARDX_TOKEN="<ShardBrowser 设置 -> Automation API 里的 Bearer token>"

然后把项目侧配置指向本桥接：
    ROXY_API_BASE="http://127.0.0.1:50100"
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request
from werkzeug.serving import make_server

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

SHARDX_API = (os.environ.get("SHARDX_API") or "http://127.0.0.1:41000").rstrip("/")
SHARDX_TOKEN = (os.environ.get("SHARDX_TOKEN") or "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("shardx-bridge")

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

_lock = threading.Lock()
_created: dict[str, dict] = {}
_by_name: dict[str, str] = {}


class ShardXError(RuntimeError):
    pass


class ShardXClient:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.http = requests.Session()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http.headers.update(headers)

    def request(self, method: str, path: str, body: dict | None = None, timeout: int = 90) -> dict:
        url = f"{self.base}{path}"
        try:
            resp = self.http.request(method, url, json=body, timeout=timeout)
        except requests.RequestException as exc:
            raise ShardXError(f"ShardX 连接失败 {method} {path}: {exc}") from exc
        text = resp.text or ""
        if resp.status_code == 401:
            raise ShardXError("ShardX 鉴权失败（401）：请检查 SHARDX_TOKEN 是否与 ShardBrowser 设置里的 token 一致")
        if not (200 <= resp.status_code < 300):
            raise ShardXError(f"ShardX {method} {path} HTTP {resp.status_code}: {text[:400]}")
        if not text.strip():
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise ShardXError(f"ShardX 返回非 JSON：{text[:200]}") from exc


shardx = ShardXClient(SHARDX_API, SHARDX_TOKEN)


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


def _map_platform(os_name) -> str:
    text = str(os_name or "").strip().lower()
    if text in ("macos", "mac", "darwin", "osx"):
        return "macOS"
    if text == "linux":
        return "Linux"
    return "Windows"


def _proxy_to_url(body: dict) -> str:
    direct = str(body.get("proxy") or "").strip()
    if direct:
        return direct
    info = body.get("proxyInfo")
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
        "SOCKS4": "socks4",
    }.get(protocol, protocol.lower())
    user = str(info.get("proxyUserName") or info.get("username") or "")
    password = str(info.get("proxyPassword") or info.get("password") or "")
    auth = ""
    if user or password:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    return f"{scheme}://{auth}{host}:{port}"


def _open_payload(cdp: dict) -> dict:
    http_url = str(cdp.get("http_url") or "").strip()
    host_port = http_url.replace("https://", "").replace("http://", "").strip("/")
    port = cdp.get("port")
    if not host_port and port:
        host_port = f"127.0.0.1:{port}"
    ws = str(cdp.get("web_socket_debugger_url") or "").strip()
    return {
        "debuggerAddress": host_port,
        "debuggingPort": port,
        "http": http_url,
        "wsEndpoint": ws,
        "ws": ws,
    }


def _fetch_running_cdp(profile_id: str) -> dict | None:
    try:
        data = shardx.request("GET", f"/profiles/{profile_id}", timeout=30)
    except Exception:
        return None
    cdp = data.get("cdp") if isinstance(data, dict) else None
    if isinstance(cdp, dict) and (cdp.get("http_url") or cdp.get("port")):
        return cdp
    return None


@app.get("/health")
def health():
    return jsonify({"ok": True, "name": "shardx-roxy-bridge", "shardx_api": SHARDX_API})


@app.route("/browser/workspace", methods=["GET", "POST"])
def workspace():
    return _ok(
        {
            "rows": [
                {
                    "id": "shardx",
                    "workspaceName": "ShardBrowser",
                    "project_details": [
                        {"projectId": "temporary", "projectName": "Temporary Profiles"}
                    ],
                }
            ]
        }
    )


@app.route("/browser/create", methods=["POST"])
def browser_create():
    body = _body()
    name = str(body.get("name") or "").strip() or f"shardx-{int(time.time() * 1000)}"
    with _lock:
        existing = _by_name.get(name)
        if existing and existing in _created:
            LOG.info("create 幂等命中：name=%s id=%s", name, existing)
            return _ok({"id": existing, "dirId": existing, "name": name})

    payload = {"platform": _map_platform(body.get("os")), "name": name}
    proxy_url = _proxy_to_url(body)
    if proxy_url:
        payload["proxy"] = proxy_url

    LOG.info(
        "创建临时 Profile：name=%s platform=%s proxy=%s",
        name,
        payload["platform"],
        "yes" if proxy_url else "no",
    )
    try:
        data = shardx.request("POST", "/profiles/temporary", payload, timeout=90)
    except ShardXError as exc:
        return _err(str(exc))
    profile_id = str(data.get("id") or "").strip()
    if not profile_id:
        return _err(f"ShardX 返回中缺少 profile id: {data}")

    with _lock:
        _created[profile_id] = {"name": name, "platform": payload["platform"], "running": False, "cdp": None}
        _by_name[name] = profile_id
    LOG.info("临时 Profile 已创建：id=%s", profile_id)
    return _ok({"id": profile_id, "dirId": profile_id, "name": name})


@app.route("/browser/open", methods=["POST", "GET"])
def browser_open():
    body = _body()
    profile_id = _profile_id(body)
    if not profile_id:
        return _err("open 缺少 dirId/profileId", 400)
    with _lock:
        meta = _created.get(profile_id)
        cached = meta.get("cdp") if meta and meta.get("running") else None
    if cached:
        return _ok(_open_payload(cached))
    if meta is None:
        return _err(f"profile {profile_id} 不是本桥接创建的临时环境，拒绝操作", 400)

    headless = bool(body.get("headless"))
    LOG.info("启动临时 Profile：id=%s headless=%s", profile_id, headless)
    try:
        data = shardx.request("POST", f"/profiles/{profile_id}/start", {"headless": headless}, timeout=120)
    except ShardXError as exc:
        cdp = _fetch_running_cdp(profile_id)
        if cdp:
            with _lock:
                _created[profile_id]["cdp"] = cdp
                _created[profile_id]["running"] = True
            return _ok(_open_payload(cdp))
        return _err(str(exc))

    cdp = data.get("cdp")
    if not isinstance(cdp, dict) or not (cdp.get("http_url") or cdp.get("port")):
        return _err(f"ShardX 未返回 CDP 调试地址：{data.get('cdp_error') or data}")
    with _lock:
        _created[profile_id]["cdp"] = cdp
        _created[profile_id]["running"] = True
        _created[profile_id]["headless"] = headless
    LOG.info("临时 Profile 已启动：id=%s cdp=%s", profile_id, cdp.get("http_url"))
    return _ok(_open_payload(cdp))


@app.route("/browser/close", methods=["POST", "GET"])
def browser_close():
    body = _body()
    profile_id = _profile_id(body)
    if not profile_id:
        return _err("close 缺少 dirId/profileId", 400)
    with _lock:
        meta = _created.get(profile_id)
    if meta is None:
        return _ok({"closed": False})
    try:
        shardx.request("POST", f"/profiles/{profile_id}/stop", timeout=30)
    except ShardXError as exc:
        LOG.warning("关闭临时 Profile 失败：id=%s %s", profile_id, exc)
    with _lock:
        meta["running"] = False
        meta["cdp"] = None
    LOG.info("临时 Profile 已关闭：id=%s", profile_id)
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
            LOG.info("跳过删除：%s 不是本桥接创建的临时环境", profile_id)
            continue
        try:
            # 临时 Profile 在浏览器关闭时由 ShardX 自动删除。
            # 这里不再调用 DELETE：显式删除可能把临时环境归档进 ShardBrowser 的回收站。
            shardx.request("POST", f"/profiles/{profile_id}/stop", timeout=30)
        except ShardXError as exc:
            LOG.info("停止临时 Profile 返回：id=%s %s", profile_id, exc)
        with _lock:
            _created.pop(profile_id, None)
            _by_name.pop(meta.get("name"), None)
        deleted.append(profile_id)
        LOG.info("临时 Profile 已删除：id=%s", profile_id)
    return _ok({"deleted": True, "dirIds": deleted})


def main() -> None:
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "50100"))
    LOG.info("ShardX 桥接启动：http://%s:%s -> %s", host, port, SHARDX_API)
    if not SHARDX_TOKEN:
        LOG.warning("SHARDX_TOKEN 为空：请在 .env 填写 ShardBrowser 的 Automation API token")
    server = make_server(host, port, app, threaded=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
