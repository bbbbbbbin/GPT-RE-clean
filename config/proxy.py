# -*- coding: utf-8 -*-
"""
代理池配置

每次注册随机抽取一个代理，保证不同 sid 之间彼此独立，避免风控关联。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）
"""
import logging
import random
import re
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlsplit, urlunsplit

from config.env_loader import apply_env_overrides

logger = logging.getLogger(__name__)


# 本地代理入口；实际出口地区以代理/分流规则为准。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
PROXY_POOL = [
    "socks5://127.0.0.1:7897",
]

# 日本池：PROXY_REGION=JP 时启用；留空则回退 PROXY_POOL。
PROXY_POOL_JP = []
PROXY_REGION = "US"

# 代理池使用的本地上游代理。填写后形成：本地上游 -> 代理池目标代理 -> ChatGPT；
# 留空则直接使用代理池中的目标代理，不启动链式中继。
PROXY_POOL_UPSTREAM_PROXY = ""

# 粘性代理 session 刷新：每个任务创建环境前 GET 一次刷新 URL，只替换 URL 里的
# session 参数（从当前代理用户名中的 -session-<id>- 提取），让该账号拿到新的出口 IP。
PROXY_POOL_REFRESH_ENABLED = True
PROXY_POOL_REFRESH_URL = ""
PROXY_POOL_REFRESH_URLS: list[str] = []
PROXY_POOL_REFRESH_URL_JP = ""
PROXY_POOL_REFRESH_TIMEOUT = 15.0

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；没有代理时才按 direct 运行
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = []

# 套餐查询 / Codex Agent Token 生成的上游代理。填写后形成：本地代理 -> 动态代理 -> ChatGPT。
# 例如 http://127.0.0.1:7897；留空则直接连接 PLAN_CHECK_PROXY。
PLAN_CHECK_UPSTREAM_PROXY = ""

# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 3
PLAN_CHECK_RETRY_DELAY = 2.0

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 1.0
PLAN_CHECK_JITTER = 0.8


def _valid_port(value: str) -> bool:
    return value.isdigit() and 1 <= int(value) <= 65535


def normalize_proxy_url(value: str, default_scheme: str = "http") -> str:
    """Normalize common proxy shorthand while preserving explicit URLs."""
    text = str(value or "").strip()
    if not text or "://" in text:
        return text
    parts = text.split(":", 3)
    if len(parts) == 4 and parts[0] and _valid_port(parts[1]) and parts[2] and parts[3]:
        host, port, username, password = parts
        return f"{default_scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    if len(parts) == 4 and parts[0] and parts[1] and parts[2] and _valid_port(parts[3]):
        username, password, host, port = parts
        return f"{default_scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    parsed = urlparse(f"//{text}")
    try:
        port = parsed.port
    except ValueError:
        port = None
    if parsed.hostname and port:
        return f"{default_scheme}://{text}"
    return text


def normalize_proxy_list(values, default_scheme: str = "http") -> list[str]:
    """Normalize a multiline proxy list and omit empty entries."""
    if values is None:
        return []
    if isinstance(values, str):
        values = values.splitlines()
    return [
        normalized
        for value in values
        if (normalized := normalize_proxy_url(value, default_scheme=default_scheme))
    ]


_PICK_LOCK = threading.Lock()
_PICK_CURSOR = 0


def active_proxy_pool() -> list[str]:
    """返回当前注册区域使用的代理池；JP 池为空时回退 US 池。"""
    region = str(PROXY_REGION or "US").strip().upper()
    if region == "JP" and PROXY_POOL_JP:
        return PROXY_POOL_JP
    return PROXY_POOL


def pick_proxy(region: str | None = None) -> str:
    """轮询代理池；region 显式指定 US/JP，默认按 PROXY_REGION。"""
    global _PICK_CURSOR
    if region:
        key = str(region).strip().upper()
        pool = PROXY_POOL_JP if key == "JP" and PROXY_POOL_JP else PROXY_POOL
    else:
        pool = active_proxy_pool()
    if not pool:
        return ""
    with _PICK_LOCK:
        proxy = pool[_PICK_CURSOR % len(pool)]
        _PICK_CURSOR += 1
    return proxy


def refresh_proxy_session(proxy_url: str) -> bool:
    """按代理用户名中的 session 调用刷新 URL，为该账号换取新的粘性出口 IP。"""
    if not bool(PROXY_POOL_REFRESH_ENABLED):
        return False
    region = str(PROXY_REGION or "US").strip().upper()
    template = str((PROXY_POOL_REFRESH_URL_JP if region == "JP" else PROXY_POOL_REFRESH_URL) or "").strip()
    if not template and not PROXY_POOL_REFRESH_URLS:
        return False
    try:
        parsed = urlparse(str(proxy_url or "").strip())
        username = unquote(parsed.username or "")
        match = re.search(r"-session-([A-Za-z0-9]+)", username)
        if not match:
            return False
        session = match.group(1)
        exact_url = ""
        for candidate in PROXY_POOL_REFRESH_URLS:
            try:
                query = parse_qs(urlsplit(candidate).query, keep_blank_values=True)
                if (query.get("session") or [""])[0] == session:
                    exact_url = str(candidate).strip()
                    break
            except Exception:
                continue
        if exact_url:
            url = exact_url
        else:
            parts = urlsplit(template)
            query = parse_qs(parts.query, keep_blank_values=True)
            query["session"] = [session]
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))
        upstream = str(PROXY_POOL_UPSTREAM_PROXY or "").strip()
        proxies = {"http": upstream, "https": upstream} if upstream else None
        import requests

        with requests.Session() as http:
            http.trust_env = False
            resp = http.get(url, proxies=proxies, timeout=float(PROXY_POOL_REFRESH_TIMEOUT))
        ok = 200 <= resp.status_code < 300
        logger.info(
            "[ProxyPool] 刷新粘性 session=%s HTTP %s %s",
            session, resp.status_code, (resp.text or "")[:120],
        )
        if ok:
            time.sleep(1.0)
        return ok
    except Exception as exc:
        logger.warning("[ProxyPool] 刷新粘性 session 失败，继续使用当前出口：%s: %s", type(exc).__name__, exc)
        return False


# 兼容入口：进程启动时先取一个，作为本次运行期的初始代理
PROXY = pick_proxy()

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'PROXY_POOL_JP': 'list_str_multiline',
    'PROXY_REGION': 'str',
    'PROXY_POOL_UPSTREAM_PROXY': 'str',
    'PROXY_POOL_REFRESH_ENABLED': 'bool',
    'PROXY_POOL_REFRESH_URL': 'str',
    'PROXY_POOL_REFRESH_URLS': 'list_str_multiline',
    'PROXY_POOL_REFRESH_URL_JP': 'str',
    'PROXY_POOL_REFRESH_TIMEOUT': 'float',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'list_str_multiline',
    'PLAN_CHECK_UPSTREAM_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
})
PROXY_POOL = normalize_proxy_list(PROXY_POOL)
PROXY_POOL_JP = normalize_proxy_list(PROXY_POOL_JP)
PLAN_CHECK_PROXY = normalize_proxy_list(PLAN_CHECK_PROXY)
PROXY = pick_proxy()
