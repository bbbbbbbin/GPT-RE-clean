# -*- coding: utf-8 -*-
"""AiLink VCC 开放 API 客户端。

文档：https://docs.ailinkvcc.com/
签名：signature = HMAC-SHA256(apiSecret, METHOD\\nPATH\\nTIMESTAMP\\nNONCE\\nSHA256_HEX(body))
敏感字段：base64url(iv + ciphertext + tag)，key = HMAC-SHA256(webhookSecret, "vcc-webhook-sensitive-v1")
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import socket
import time
from datetime import datetime
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEFAULT_BASE_URL = "https://card.ailinkvcc.com"
SENSITIVE_KEY_LABEL = b"vcc-webhook-sensitive-v1"

# 本机直连被重置时，记住自动探测到的本地代理，后续请求直接用。
_AUTO_PROXY = {"url": "", "direct_broken": False}

# 本地代理端口按顺序探测：Clash(7890/7891) → 其他客户端(10808/1080)
LOCAL_PROXY_CANDIDATES = (
    ("http", 7890),
    ("http", 7891),
    ("socks5h", 10808),
    ("http", 1080),
)

WEBHOOK_HELP = (
    "需要在 AiLink 商户端配置 Webhook Secret：登录商户端 → 右上角头像 → 「开发者配置」→ Webhook 区域，"
    "填一个公网地址并生成 Webhook Secret（明文只显示一次），再把 Secret 填到本工具「设置 → Webhook Secret」保存。"
    "地址随便填一个你自己的公网 https 地址即可（保存时只校验格式和是否公网，不校验能否连通，"
    "本工具所有数据都是主动查询，不依赖这个推送地址）。"
)


class VccError(RuntimeError):
    """接口返回的业务错误或网络错误。"""

    def __init__(self, message: str, *, status: int = 0, code: object = None, request_id: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id


def _b64url_decode(text: str) -> bytes:
    text = (text or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def derive_sensitive_key(webhook_secret: str) -> bytes:
    return hmac.new(webhook_secret.encode("utf-8"), SENSITIVE_KEY_LABEL, hashlib.sha256).digest()


def decrypt_sensitive(ciphertext: str, key: bytes) -> str:
    raw = _b64url_decode(ciphertext)
    if len(raw) < 12 + 16 + 1:
        raise ValueError("密文长度不足")
    iv, body, tag = raw[:12], raw[12:-16], raw[-16:]
    plain = AESGCM(key).decrypt(iv, body + tag, None)
    return plain.decode("utf-8")


def new_request_no(prefix: str = "kaika") -> str:
    """requestNo：8-64 位，字母开头，必须同时含字母和数字。"""
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    tail = secrets.token_hex(3)
    return f"{prefix}{stamp}{tail}"


class VccClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        webhook_secret: str = "",
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 40,
        proxy: str = "",
    ):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.webhook_secret = (webhook_secret or "").strip()
        self.timeout = max(5, int(timeout or 40))
        self.proxy = (proxy or "").strip()
        self.auto_proxy = False
        self.proxy_note = ""
        self.session = requests.Session()

    # ------------------------------------------------------------------ 底层
    @property
    def proxies(self) -> dict | None:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def _send(self, path: str, body: bytes, headers: dict, proxies: dict | None):
        return self.session.post(
            self.base_url + path,
            data=body,
            headers=headers,
            timeout=self.timeout,
            proxies=proxies,
        )

    @staticmethod
    def _port_open(port: int, host: str = "127.0.0.1") -> bool:
        try:
            with socket.create_connection((host, int(port)), timeout=0.4):
                return True
        except OSError:
            return False

    @classmethod
    def _auto_local_proxy(cls) -> str:
        """本机有本地代理就返回 URL（优先 7890），否则空串。"""
        for scheme, port in LOCAL_PROXY_CANDIDATES:
            if cls._port_open(port):
                return f"{scheme}://127.0.0.1:{port}"
        return ""

    @classmethod
    def _is_local_unreachable(cls, url: str) -> bool:
        """配置的是本地地址但端口没监听。"""
        try:
            parts = urlsplit(url if "://" in url else "http://" + url)
        except Exception:
            return False
        host = (parts.hostname or "").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return False
        port = parts.port or (1080 if parts.scheme.lower().startswith("socks") else 7890)
        return not cls._port_open(port)

    def _sign(self, path: str, body: bytes, timestamp: str, nonce: str) -> str:
        base = "\n".join(["POST", path, timestamp, nonce, sha256_hex(body)])
        return hmac.new(self.api_secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()

    def post(self, path: str, payload: dict | None = None) -> dict:
        if not self.api_key or not self.api_secret:
            raise VccError("请先在「设置」里填写 API Key 和 API Secret")
        body = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(time.time() * 1000))
        nonce = secrets.token_hex(8)
        headers = {
            "Content-Type": "application/json",
            "X-VCC-API-KEY": self.api_key,
            "X-VCC-TIMESTAMP": timestamp,
            "X-VCC-NONCE": nonce,
            "X-VCC-SIGNATURE": self._sign(path, body, timestamp, nonce),
            "X-VCC-REQUEST-ID": f"tool_{nonce}",
        }
        proxies = self.proxies
        if self.proxy and self._is_local_unreachable(self.proxy):
            # 配置里写的是本地代理，但这个端口没起（比如 Clash 没开 7890），换一个能用的
            alt = _AUTO_PROXY["url"] or self._auto_local_proxy()
            if alt:
                self.proxy_note = f"配置的代理 {self.proxy} 未监听，已自动改用 {alt}"
                self.proxy = alt
                self.auto_proxy = True
                proxies = {"http": alt, "https": alt}
        if proxies is None and _AUTO_PROXY["direct_broken"] and _AUTO_PROXY["url"]:
            proxies = {"http": _AUTO_PROXY["url"], "https": _AUTO_PROXY["url"]}
        try:
            resp = self._send(path, body, headers, proxies)
        except Exception as exc:
            # 本机常见情况：直连 card.ailinkvcc.com 被重置。若本地有代理，自动改走它。
            if self.proxy and proxies is not None:
                raise VccError(f"请求 AiLink 失败（代理 {self.proxy}）: {type(exc).__name__}: {exc}") from exc
            url = _AUTO_PROXY["url"] or self._auto_local_proxy()
            if not url:
                raise VccError(
                    f"请求 AiLink 失败（网络）: {type(exc).__name__}: {exc}。"
                    f"直连不通且本机 7890 / 7891 / 10808 / 1080 都没有代理，"
                    f"请在「设置 → 接口代理」填一个可用代理。"
                ) from exc
            try:
                resp = self._send(path, body, headers, {"http": url, "https": url})
            except Exception as exc2:
                raise VccError(
                    f"请求 AiLink 失败（直连和 {url} 都不通）: {type(exc2).__name__}: {exc2}"
                ) from exc2
            _AUTO_PROXY.update(url=url, direct_broken=True)
            self.proxy, self.auto_proxy = url, True

        text = resp.text or ""
        try:
            data = resp.json()
        except Exception:
            if resp.status_code in (403, 401, 406):
                raise VccError(
                    f"AiLink 拒绝访问（HTTP {resp.status_code}）。常见原因：商户后台配置了 API IP 白名单、"
                    f"当前出口 IP 被风控，或接口域名填错。可在「设置」里换一个接口代理再试。"
                    f"原始响应: {text[:160]}",
                    status=resp.status_code,
                ) from None
            raise VccError(f"AiLink 返回非 JSON（HTTP {resp.status_code}）: {text[:300]}",
                           status=resp.status_code) from None
        if not isinstance(data, dict):
            raise VccError(f"AiLink 返回格式异常: {text[:300]}", status=resp.status_code)

        code = data.get("code")
        msg = str(data.get("msg") or data.get("message") or "").strip()
        request_id = str(data.get("requestId") or "")
        if resp.status_code >= 400 or str(code) not in {"200", "0"}:
            detail = msg or f"HTTP {resp.status_code}"
            raise VccError(f"{detail}（code={code}）" if code is not None else detail,
                           status=resp.status_code, code=code, request_id=request_id)
        return data.get("data") or {}

    # ------------------------------------------------------------------ 查询
    def card_bins(self) -> dict:
        return self.post("/openapi/v1/card-bins/query", {})

    def account_balance(self) -> dict:
        return self.post("/openapi/v1/account/balance/query", {})

    def account_transactions(self, page_num: int = 1, page_size: int = 20, **filters) -> dict:
        payload = {"pageNum": page_num, "pageSize": page_size}
        payload.update({k: v for k, v in filters.items() if v not in (None, "")})
        return self.post("/openapi/v1/account/transactions/query", payload)

    def query_cards(self, page_num: int = 1, page_size: int = 20, **filters) -> dict:
        payload = {"pageNum": page_num, "pageSize": page_size}
        payload.update({k: v for k, v in filters.items() if v not in (None, "")})
        return self.post("/openapi/v1/cards/query", payload)

    def card_balances(self, card_ids: list[str]) -> dict:
        return self.post("/openapi/v1/cards/balances/query", {"cardIds": card_ids[:10]})

    def card_transactions(self, page_num: int = 1, page_size: int = 20, **filters) -> dict:
        payload = {"pageNum": page_num, "pageSize": page_size}
        payload.update({k: v for k, v in filters.items() if v not in (None, "")})
        return self.post("/openapi/v1/card-transactions/query", payload)

    def verification_code(self, card_id: str = "", card_no: str = "") -> dict:
        payload = {}
        if card_id:
            payload["cardId"] = card_id
        if card_no:
            payload["cardNo"] = card_no
        return self.post("/openapi/v1/cards/verification-code/query", payload)

    def cardholders(self, page_num: int = 1, page_size: int = 20, **filters) -> dict:
        payload = {"pageNum": page_num, "pageSize": page_size}
        payload.update({k: v for k, v in filters.items() if v not in (None, "")})
        return self.post("/openapi/v1/cardholders/query", payload)

    # ------------------------------------------------------------------ 开卡
    def apply_card(
        self,
        *,
        request_no: str,
        card_bin_id: int | str,
        recharge_amount: str,
        card_count: int = 1,
        holder_id: int | str | None = None,
        remark: str = "",
        card_type: str = "PREPAID",
    ) -> dict:
        payload = {
            "requestNo": request_no,
            "cardType": card_type,
            "cardBinId": card_bin_id,
            "cardCount": int(card_count or 1),
            "rechargeAmount": recharge_amount,
        }
        if holder_id not in (None, ""):
            payload["holderId"] = holder_id
        if remark:
            payload["remark"] = remark
        return self.post("/openapi/v1/cards/apply", payload)

    def apply_status(self, *, task_id=None, batch_no: str = "", request_no: str = "") -> dict:
        payload = {}
        if task_id not in (None, ""):
            payload["taskId"] = task_id
        if batch_no:
            payload["batchNo"] = batch_no
        if request_no:
            payload["requestNo"] = request_no
        if not payload:
            raise VccError("taskId / batchNo / requestNo 至少填一个")
        return self.post("/openapi/v1/cards/apply-status/query", payload)

    # ------------------------------------------------------------------ 敏感信息
    def private_info(self, card_id: str) -> dict:
        """返回 {cardId, raw, cardNumber, expiryDate, cvv}。

        AiLink 只给密文，必须用 Webhook Secret 派生密钥本地解密。
        """
        if not self.webhook_secret:
            raise VccError(WEBHOOK_HELP)
        try:
            data = self.post("/openapi/v1/cards/private-info/query", {"cardId": card_id})
        except VccError as exc:
            if "webhook secret" in str(exc).lower():
                raise VccError(f"{exc}\n\n{WEBHOOK_HELP}") from exc
            raise
        alg = str(data.get("sensitiveEncryptAlg") or "AES-256-GCM")
        if alg.upper() != "AES-256-GCM":
            raise VccError(f"不支持的加密算法: {alg}")
        key = derive_sensitive_key(self.webhook_secret)
        result = {"cardId": data.get("cardId") or card_id, "alg": alg, "raw": data}
        for field, target in (
            ("cardNumberCiphertext", "cardNumber"),
            ("expiryDateCiphertext", "expiryDate"),
            ("cvvCiphertext", "cvv"),
        ):
            cipher = data.get(field)
            if not cipher:
                result[target] = ""
                continue
            try:
                result[target] = decrypt_sensitive(cipher, key)
            except Exception as exc:
                raise VccError(
                    f"{target} 解密失败（Webhook Secret 是否与商户后台一致？）: {type(exc).__name__}"
                ) from exc
        return result


def mask_key(value: str) -> str:
    text = (value or "").strip()
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}***{text[-4:]}"
