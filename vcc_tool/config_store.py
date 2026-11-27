# -*- coding: utf-8 -*-
"""本地配置：API 凭据保存在 %APPDATA%\\PlusVCC\\config.json。

Windows 下 Secret 用 DPAPI（当前用户）加密，换机器/换用户无法解密；
非 Windows 或加密失败时退化为 base64（仅防误读，不是加密）。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path

APP_NAME = "PlusVCC"
IS_WINDOWS = sys.platform.startswith("win")

DEFAULTS = {
    "base_url": "https://card.ailinkvcc.com",
    "api_key": "",
    "api_secret": "",
    "webhook_secret": "",
    "timeout": 40,
    "api_proxy": "",
    "chain_upstream": "",
    "totp_accounts": [],
}


def config_path() -> Path:
    root = os.getenv("APPDATA") or str(Path.home())
    return Path(root) / APP_NAME / "config.json"


# ---------------------------------------------------------------- DPAPI
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if protect:
        fn = crypt32.CryptProtectData
        flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    else:
        fn = crypt32.CryptUnprotectData
        flags = 0x1
    src = _blob(data)
    out = _Blob()
    desc = ctypes.c_wchar_p("PlusVCC")
    ok = fn(ctypes.byref(src), desc if protect else None, None, None, None, flags, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI 调用失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect_text(text: str) -> str:
    if not text:
        return ""
    raw = text.encode("utf-8")
    if IS_WINDOWS:
        try:
            return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode("ascii")
        except Exception:
            pass
    return "b64:" + base64.b64encode(raw).decode("ascii")


def unprotect_text(text: str) -> str:
    if not text:
        return ""
    try:
        if text.startswith("dpapi:"):
            raw = _dpapi(base64.b64decode(text[6:]), False)
            return raw.decode("utf-8")
        if text.startswith("b64:"):
            return base64.b64decode(text[4:]).decode("utf-8")
    except Exception:
        return ""
    return text


# ---------------------------------------------------------------- 读写
def load() -> dict:
    data = dict(DEFAULTS)
    path = config_path()
    if not path.exists():
        return data
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return data
    if isinstance(raw, dict):
        for key in DEFAULTS:
            if key in raw:
                data[key] = raw[key]
        data["api_secret"] = unprotect_text(str(raw.get("api_secret_prot") or "")) or str(raw.get("api_secret") or "")
        data["webhook_secret"] = (
            unprotect_text(str(raw.get("webhook_secret_prot") or "")) or str(raw.get("webhook_secret") or "")
        )
        accounts = []
        for item in raw.get("totp_accounts") or []:
            if not isinstance(item, dict):
                continue
            accounts.append({
                "id": str(item.get("id") or ""),
                "label": str(item.get("label") or "ChatGPT"),
                "digits": int(item.get("digits") or 6),
                "period": int(item.get("period") or 30),
                "algorithm": str(item.get("algorithm") or "SHA1"),
                "secret": unprotect_text(str(item.get("secret_prot") or "")) or str(item.get("secret") or ""),
            })
        data["totp_accounts"] = accounts
    return data


def save(values: dict) -> dict:
    current = load()
    for key in DEFAULTS:
        if key in values and values[key] is not None:
            current[key] = values[key]
    current["timeout"] = int(current.get("timeout") or 40)
    payload = {
        "base_url": current["base_url"],
        "api_key": current["api_key"],
        "api_secret_prot": protect_text(current["api_secret"]),
        "webhook_secret_prot": protect_text(current["webhook_secret"]),
        "timeout": current["timeout"],
        "api_proxy": current["api_proxy"],
        "chain_upstream": current["chain_upstream"],
        "totp_accounts": [
            {
                "id": item.get("id") or "",
                "label": item.get("label") or "ChatGPT",
                "digits": int(item.get("digits") or 6),
                "period": int(item.get("period") or 30),
                "algorithm": item.get("algorithm") or "SHA1",
                "secret_prot": protect_text(item.get("secret") or ""),
            }
            for item in (current.get("totp_accounts") or [])
            if item.get("secret")
        ],
    }
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return current


def public_view(values: dict) -> dict:
    """给前端的安全视图：不回传密钥明文。"""
    from vcc_tool.vcc_client import mask_key

    return {
        "base_url": values.get("base_url") or DEFAULTS["base_url"],
        "api_key": values.get("api_key") or "",
        "api_key_masked": mask_key(values.get("api_key") or ""),
        "api_secret_set": bool(values.get("api_secret")),
        "webhook_secret_set": bool(values.get("webhook_secret")),
        "api_secret_masked": mask_key(values.get("api_secret") or ""),
        "webhook_secret_masked": mask_key(values.get("webhook_secret") or ""),
        "timeout": int(values.get("timeout") or 40),
        "api_proxy": values.get("api_proxy") or "",
        "chain_upstream": values.get("chain_upstream") or "",
        "totp_accounts": [
            {
                "id": item.get("id") or "",
                "label": item.get("label") or "ChatGPT",
                "digits": int(item.get("digits") or 6),
                "period": int(item.get("period") or 30),
                "algorithm": item.get("algorithm") or "SHA1",
                "secret_masked": mask_key(item.get("secret") or ""),
            }
            for item in (values.get("totp_accounts") or [])
        ],
        "config_path": str(config_path()),
        "encrypted": IS_WINDOWS,
    }
