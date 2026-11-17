#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输入 ChatGPT session + 代理，生成该代理出口下的 Plus 支付短链。

示例:
    python tools/plus_short_link.py --session session.json --proxy socks5h://127.0.0.1:10808
    python tools/plus_short_link.py --session '<session JSON>' --proxy http://user:pass@1.2.3.4:8080
    python tools/plus_short_link.py --session token.txt --proxy 1.2.3.4:1080 --country PH
    python tools/plus_short_link.py            # 交互式填写

不传 --country 时会先通过代理访问 ipapi.co 自动识别出口国家和币种。
默认带 plus-1-month-free 试用活动；--no-promo 关闭，--sentinel off 跳过 sentinel。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from curl_cffi import requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
COUPON_URL = "https://chatgpt.com/backend-api/promo_campaign/check_coupon"
SENTINEL_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
TRACE_URL = "https://chatgpt.com/cdn-cgi/trace"
GEO_URL = "https://ipapi.co/json/"
DEFAULT_IMPERSONATE = "chrome"
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
DEFAULT_PROMO = "plus-1-month-free"

SESSION_RE = re.compile(r"((?:cs_(?:live|test)_|oaics_)[A-Za-z0-9]+)")
ENTITY_RE = re.compile(r"(?:/checkout/|processor_entity=)([A-Za-z0-9_]+)")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")

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

EU_COUNTRIES = {"DE", "FR", "NL", "IE", "IT", "ES", "PT", "BE", "AT", "FI", "GR", "PL", "CZ", "RO", "SE", "DK", "NO", "CH"}


def proxy_candidates(value: str) -> list[str]:
    text = (value or "").strip()
    if not text:
        raise ValueError("代理不能为空")
    if "://" in text:
        scheme = urlsplit(text).scheme.lower()
        if scheme not in {"http", "https", "socks5", "socks5h"}:
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
        from core.proxy_utils import mask_proxy_url
        return mask_proxy_url(proxy)
    except Exception:
        return re.sub(r"://[^/@]+@", "://***:***@", str(proxy))


REGION_RE = re.compile(r"region[-_]([A-Za-z]{2})(?:[-_]|$)")


def geo_hint(value: str) -> dict:
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


def extract_access_token(raw: str) -> str:
    token, _ = parse_session(raw)
    return token


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


@dataclass
class ResolvedProxy:
    url: str
    geo: dict
    relay: object | None = None
    chained: bool = False

    def close(self) -> None:
        relay, self.relay = self.relay, None
        if relay is not None:
            try:
                relay.close()
            except Exception:
                pass


def default_chain_upstream() -> str:
    try:
        from config.env_loader import load_env
        load_env(override=False)
    except Exception:
        pass
    return (os.getenv("PROXY_POOL_UPSTREAM_PROXY") or "socks5://127.0.0.1:10808").strip()


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


def resolve_proxy(value: str, impersonate: str, timeout: int, proxy_type: str = "auto", chain_upstream: str | None = None, chain_only: bool = False) -> ResolvedProxy:
    candidates = proxy_candidates(value)
    kind = (proxy_type or "auto").strip().lower()
    if kind == "http":
        candidates = [c for c in candidates if urlsplit(c).scheme.lower() in {"http", "https"}]
    elif kind == "socks5":
        candidates = [c for c in candidates if urlsplit(c).scheme.lower() in {"socks5", "socks5h"}]
    if not candidates:
        raise ValueError(f"代理类型 {kind} 与填写的代理不匹配")

    upstream = (default_chain_upstream() if chain_upstream is None else str(chain_upstream)).strip()
    chain_candidates = [c for c in candidates if urlsplit(c).scheme.lower() in {"http", "socks5", "socks5h"}]
    chain_candidates.sort(key=lambda c: {"socks5h": 0, "socks5": 1, "http": 2}.get(urlsplit(c).scheme.lower(), 9))

    if chain_only:
        if not upstream:
            raise RuntimeError("已选择只走链式，但链式上游代理为空")
        if not chain_candidates:
            raise ValueError("链式代理只支持 http://、socks5://、socks5h://")
        try:
            from core.proxy_chain import ProxyChainRelay
        except Exception as exc:
            raise RuntimeError(f"链式代理不可用: {type(exc).__name__}: {exc}") from exc
        candidate = chain_candidates[0]
        relay = None
        try:
            relay = ProxyChainRelay(candidate, upstream=upstream, timeout=max(20, min(int(timeout), 30)))
            relay.start()
            return ResolvedProxy(relay.proxy_url, geo_hint(value), relay=relay, chained=True)
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
        try:
            from core.proxy_chain import ProxyChainRelay
        except Exception as exc:
            ProxyChainRelay = None
            errors.append(f"链式代理不可用: {type(exc).__name__}: {exc}")
        if ProxyChainRelay is not None:
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
                        geo = {
                            "ok": True,
                            "ip": target.get("ip") or "",
                            "country": target.get("country") or "",
                            "currency": CURRENCY_BY_COUNTRY.get(target.get("country") or "", ""),
                            "source": "chatgpt-trace(chain)",
                            "error": "",
                        }
                        return ResolvedProxy(local_url, geo, relay=relay, chained=True)
                    errors.append(f"链式 {mask_proxy(candidate)}: {target.get('error') or '连接失败'}")
                except Exception as exc:
                    errors.append(f"链式 {mask_proxy(candidate)}: {type(exc).__name__}: {exc}")
                finally:
                    if not success and relay is not None:
                        relay.close()

    raise RuntimeError(
        "代理无法访问 chatgpt.com（直连和链式均失败），已尝试 " + " / ".join(mask_proxy(c) for c in candidates) +
        "；请确认协议、端口、账号密码。最后错误: " + (errors[-1] if errors else "未知错误")
    )


def _base_headers(token: str, account_id: str, device_id: str, ua: str, referer: str, originator: str, frontend: str) -> dict:
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


def query_coupon(token: str, account_id: str, device_id: str, promo: str, proxy: str, impersonate: str, ua: str, originator: str, frontend: str, timeout: int, retries: int = 1) -> dict:
    url = f"{COUPON_URL}?coupon={promo}&is_coupon_from_query_param=false"
    headers = _base_headers(token, account_id, device_id, ua, "https://chatgpt.com/", originator, frontend)
    last_exc = None
    resp = None
    attempts = max(0, int(retries)) + 1
    for attempt in range(attempts):
        try:
            resp = requests.get(url, headers=headers, impersonate=impersonate, proxies=proxy_map(proxy), timeout=timeout)
            break
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(0.5)
    if resp is None:
        return {"ok": False, "eligible": False, "state": "", "status": 0, "detail": f"{type(last_exc).__name__}: {last_exc}"}
    text = resp.text or ""
    try:
        data = resp.json()
    except Exception:
        data = None
    state = str((data or {}).get("state") or "") if isinstance(data, dict) else ""
    if resp.status_code == 200 and isinstance(data, dict):
        return {"ok": True, "eligible": state.lower() == "eligible", "state": state, "status": resp.status_code, "detail": data}
    return {"ok": False, "eligible": False, "state": state, "status": resp.status_code, "detail": data if isinstance(data, dict) else text[:300]}


def check_promo(token: str, account_id: str, device_id: str, promo: str, proxy: str, impersonate: str, ua: str, originator: str, frontend: str, timeout: int) -> bool:
    result = query_coupon(token, account_id, device_id, promo, proxy, impersonate, ua, originator, frontend, timeout)
    if not result.get("ok"):
        return True
    return bool(result.get("eligible"))


def build_sentinel_token(token: str, account_id: str, device_id: str, proxy: str, impersonate: str, ua: str, timeout: int) -> str:
    from core.sentinel import generate_requirements_token
    from core.sentinel_runner import generate_sentinel_token

    p = generate_requirements_token(device_id)
    body = json.dumps({"p": p, "id": device_id, "flow": "chatgpt_checkout"}, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": ua,
        "oai-device-id": device_id,
    }
    resp = requests.post(SENTINEL_URL, headers=headers, data=body, impersonate=impersonate, proxies=proxy_map(proxy), timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"sentinel/req HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    challenge = resp.json()
    challenge["_request_p"] = p
    raw_token = generate_sentinel_token(
        challenge=challenge,
        flow="chatgpt_checkout",
        device_id=device_id,
        user_agent=ua,
        page_url="https://chatgpt.com/",
        cookie=f"oai-did={device_id}",
    )
    parsed = json.loads(raw_token)
    parsed.pop("_so", None)
    parsed.pop("so", None)
    return json.dumps(parsed, separators=(",", ":"))


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
        for key in ("success_url", "cancel_url", "return_url", "url", "stripe_hosted_url", "checkout_url", "client_secret", "confirm_return_url"):
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
        blob = " ".join(str(data.get(key) or "") for key in ("success_url", "cancel_url", "url", "stripe_hosted_url", "checkout_url", "client_secret", "confirm_return_url"))
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
    return {"short_url": short_url, "long_url": long_url, "checkout_session_id": session_id, "processor_entity": entity}


def create_short_link(token: str, account_id: str, device_id: str, proxy: str, country: str, currency: str, plan_name: str, promo: str | None, impersonate: str, ua: str, originator: str, frontend: str, sentinel_mode: str, timeout: int) -> dict:
    referer = "https://chatgpt.com/?cta_tab=personal&highlight_plan=plus&codex_upgrade_location=sidebar+profile"
    if promo:
        referer += f"&promo_campaign={promo}"

    sentinel = ""
    sentinel_error = ""
    if sentinel_mode != "off":
        try:
            sentinel = build_sentinel_token(token, account_id, device_id, proxy, impersonate, ua, timeout)
        except Exception as exc:
            sentinel_error = f"{type(exc).__name__}: {exc}"
            if sentinel_mode == "require":
                raise RuntimeError(f"sentinel 生成失败: {sentinel_error}") from exc
            print(f"# 警告: sentinel 生成失败，继续无 sentinel 请求: {sentinel_error}", file=sys.stderr)

    headers = _base_headers(token, account_id, device_id, ua, referer, originator, frontend)
    if sentinel:
        headers["openai-sentinel-token"] = sentinel

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
        raise RuntimeError(f"checkout HTTP {resp.status_code}: {detail or text[:300]}")
    if not isinstance(data, dict):
        raise RuntimeError(f"checkout 返回不是 JSON: {text[:300]}")

    parsed = parse_checkout(data, country)
    if not parsed["short_url"]:
        raise RuntimeError(f"响应里没有 checkout_session_id，字段: {', '.join(list(data)[:15])}")
    return {
        **parsed,
        "payload": payload,
        "sentinel_used": bool(sentinel),
        "sentinel_error": sentinel_error,
        "billing_details": data.get("billing_details") or payload.get("billing_details"),
        "promo_campaign": data.get("promo_campaign"),
        "checkout_state": data.get("checkout_state"),
        "status": data.get("status"),
        "payment_status": data.get("payment_status"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="输入 ChatGPT session + 代理，生成该代理出口下的 Plus 支付短链",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--session", help="session JSON、原始 token，或包含它们的文件路径；不传则交互输入")
    parser.add_argument("--proxy", help="代理 URL、host:port 或 host:port:user:pass；不传则交互输入")
    parser.add_argument("--proxy-type", choices=["auto", "http", "socks5"], default="auto", help="代理类型；默认 auto 自动探测 HTTP/SOCKS5")
    parser.add_argument("--chain-upstream", default=None, help="链式上游代理；默认读取 .env 的 PROXY_POOL_UPSTREAM_PROXY，回退 socks5://127.0.0.1:10808；传空字符串禁用")
    parser.add_argument("--chain-only", dest="chain_only", action="store_true", default=True, help="默认只走链式；如需允许直连用 --no-chain-only")
    parser.add_argument("--no-chain-only", dest="chain_only", action="store_false", help="允许直连（直连失败仍会回退链式）")
    parser.add_argument("--country", help="强制计费国家，如 PH / JP / US；默认按代理出口自动识别")
    parser.add_argument("--currency", help="强制币种，如 PHP / JPY / USD；默认按国家推断")
    parser.add_argument("--plan-name", default="chatgptplusplan", help="套餐名，默认 chatgptplusplan")
    parser.add_argument("--promo", default=DEFAULT_PROMO, help=f"活动 ID，默认 {DEFAULT_PROMO}；传空字符串关闭")
    parser.add_argument("--no-promo", action="store_true", help="关闭试用活动 promo_campaign")
    parser.add_argument("--sentinel", choices=["auto", "off", "require"], default="auto", help="sentinel 模式，默认 auto")
    parser.add_argument("--impersonate", default=DEFAULT_IMPERSONATE, help="curl_cffi 浏览器指纹，默认 chrome")
    parser.add_argument("--user-agent", default=DEFAULT_UA, help="请求 UA，默认与 HAR 一致的 Chrome 149")
    parser.add_argument("--originator", default="Codex Browser", help="originator 请求头，默认 Codex Browser")
    parser.add_argument("--frontend", default="codex_webview", help="x-openai-web-frontend 请求头，默认 codex_webview")
    parser.add_argument("--timeout", type=int, default=60, help="请求超时秒数，默认 60")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON，而不是只输出短链")
    args = parser.parse_args()

    handle = None
    try:
        token, account_id = read_session(args.session)
        proxy_input = args.proxy or input("代理（URL / host:port / host:port:user:pass）: ")
        handle = resolve_proxy(proxy_input, args.impersonate, args.timeout, args.proxy_type, args.chain_upstream, args.chain_only)
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
            "sentinel_used": result.get("sentinel_used"),
            "sentinel_error": result.get("sentinel_error") or None,
            "email": raw_state.get("email") if isinstance(raw_state, dict) else None,
            "status": result.get("status"),
            "payment_status": result.get("payment_status"),
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        if geo.get("source"):
            print(f"# 代理出口: {geo.get('ip') or '-'} / {country} / {currency} ({geo.get('source')})", file=sys.stderr)
        print(result["short_url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
