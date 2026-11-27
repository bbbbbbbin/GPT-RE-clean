# -*- coding: utf-8 -*-
"""TOTP（RFC 6238）本地生成，用于 ChatGPT 账号两步验证。

支持直接粘贴 Base32 密钥，也支持 otpauth://totp/... 链接（会读取其中的
secret / digits / period / algorithm）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time
import uuid
from urllib.parse import parse_qs, unquote, urlparse

DEFAULT_DIGITS = 6
DEFAULT_PERIOD = 30

_ALGOS = {
    "SHA1": hashlib.sha1,
    "SHA256": hashlib.sha256,
    "SHA512": hashlib.sha512,
}


def normalize_secret(text: str) -> str:
    """去掉空格/横线，统一大写，并校验 base32 字母表。"""
    raw = re.sub(r"[\s\-_]", "", (text or "")).upper().rstrip("=")
    if not raw:
        raise ValueError("2FA 密钥为空")
    if not re.fullmatch(r"[A-Z2-7]+", raw):
        raise ValueError("2FA 密钥只能是 Base32 字符（A-Z、2-7），请检查是否复制完整")
    if len(raw) < 8:
        raise ValueError("2FA 密钥太短，正常应为 16 位或 32 位 Base32")
    try:
        base64.b32decode(raw + "=" * (-len(raw) % 8))
    except Exception as exc:
        raise ValueError(f"2FA 密钥不是合法的 Base32: {exc}") from exc
    return raw


def parse_input(text: str) -> dict:
    """接受 otpauth:// 链接或裸密钥，返回账号参数。"""
    text = (text or "").strip()
    if text.lower().startswith("otpauth://"):
        parsed = urlparse(text)
        query = parse_qs(parsed.query)
        secret = normalize_secret((query.get("secret") or [""])[0])
        label = unquote(parsed.path.lstrip("/")) or "ChatGPT"
        digits = int((query.get("digits") or [DEFAULT_DIGITS])[0])
        period = int((query.get("period") or [DEFAULT_PERIOD])[0])
        algorithm = ((query.get("algorithm") or ["SHA1"])[0] or "SHA1").upper()
        if algorithm not in _ALGOS:
            algorithm = "SHA1"
        return {
            "secret": secret,
            "label": label[:60],
            "digits": min(10, max(6, digits)),
            "period": min(120, max(10, period)),
            "algorithm": algorithm,
        }
    return {
        "secret": normalize_secret(text),
        "label": "",
        "digits": DEFAULT_DIGITS,
        "period": DEFAULT_PERIOD,
        "algorithm": "SHA1",
    }


def hotp(secret: str, counter: int, digits: int = DEFAULT_DIGITS, algorithm: str = "SHA1") -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(counter)), _ALGOS.get(algorithm, hashlib.sha1)).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def totp(secret: str, digits: int = DEFAULT_DIGITS, period: int = DEFAULT_PERIOD,
         algorithm: str = "SHA1", at: float | None = None) -> tuple[str, int]:
    """返回 (验证码, 剩余秒数)。"""
    now = time.time() if at is None else float(at)
    counter = int(now // period)
    remaining = int(period - (now % period))
    if remaining <= 0:
        remaining = period
    return hotp(secret, counter, digits, algorithm), remaining


def new_account_id() -> str:
    return "otp_" + uuid.uuid4().hex[:10]


def account_code(account: dict, at: float | None = None) -> dict:
    """给前端用的账号视图（含当前验证码）。"""
    base = {
        "id": account.get("id") or "",
        "label": account.get("label") or "ChatGPT",
        "digits": int(account.get("digits") or DEFAULT_DIGITS),
        "period": int(account.get("period") or DEFAULT_PERIOD),
        "algorithm": account.get("algorithm") or "SHA1",
    }
    try:
        code, remaining = totp(
            normalize_secret(account.get("secret") or ""),
            base["digits"], base["period"], base["algorithm"], at,
        )
        base["code"] = code
        base["remaining"] = remaining
    except Exception as exc:
        base["code"] = ""
        base["remaining"] = 0
        base["error"] = str(exc)
    return base
