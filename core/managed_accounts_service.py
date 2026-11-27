from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet, InvalidToken

from core import db

LOG = logging.getLogger(__name__)

_LOCK = threading.RLock()
_KEY_LOCK = threading.Lock()
_FERNET: Fernet | None = None
_LOGIN_THREADS: dict[int, threading.Thread] = {}
_DEVICE_LOCK = threading.RLock()
_DEVICE_SESSIONS: dict[int, dict] = {}
_CHATGPT_BASE = "https://chatgpt.com/backend-api"
_TABLE_READY = False


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _db_path() -> Path:
    return db.db_path()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _fernet() -> Fernet:
    global _FERNET
    if _FERNET is not None:
        return _FERNET
    with _KEY_LOCK:
        if _FERNET is not None:
            return _FERNET
        raw = str(os.getenv("MANAGED_ACCOUNTS_KEY") or "").strip()
        if not raw:
            path = Path.cwd() / "data" / "managed_accounts.key"
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(Fernet.generate_key())
                try:
                    os.chmod(path, 0o600)
                except Exception:
                    pass
            raw = path.read_text(encoding="ascii").strip()
        try:
            _FERNET = Fernet(raw.encode("ascii"))
        except Exception:
            digest = hashlib.sha256(raw.encode("utf-8")).digest()
            _FERNET = Fernet(base64.urlsafe_b64encode(digest))
        return _FERNET


def _enc(value: str) -> str:
    text = str(value or "")
    return _fernet().encrypt(text.encode("utf-8")).decode("ascii") if text else ""


def _dec(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        return _fernet().decrypt(text.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def _ensure_table() -> None:
    global _TABLE_READY
    if _TABLE_READY:
        return
    with _LOCK, closing(_conn()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS managed_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_enc TEXT NOT NULL DEFAULT '',
                totp_secret_enc TEXT NOT NULL DEFAULT '',
                proxy_used TEXT NOT NULL DEFAULT '',
                email_source TEXT NOT NULL DEFAULT '',
                auth_mode TEXT NOT NULL DEFAULT 'password_totp',
                tag TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                last_error TEXT NOT NULL DEFAULT '',
                codex_status TEXT NOT NULL DEFAULT '',
                account_id INTEGER,
                rt_filename TEXT NOT NULL DEFAULT '',
                plan_type TEXT NOT NULL DEFAULT '',
                quota_json TEXT NOT NULL DEFAULT '',
                reset_cards_json TEXT NOT NULL DEFAULT '',
                quota_checked_at TEXT NOT NULL DEFAULT '',
                login_started_at TEXT NOT NULL DEFAULT '',
                login_finished_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_managed_accounts_email ON managed_accounts(email COLLATE NOCASE)")
        cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(managed_accounts)").fetchall()}
        if "tag" not in cols:
            conn.execute("ALTER TABLE managed_accounts ADD COLUMN tag TEXT NOT NULL DEFAULT ''")
    _TABLE_READY = True


def _normalize_totp(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().startswith("otpauth://"):
        try:
            query = parse_qs(urlparse(text).query)
            text = str((query.get("secret") or [""])[0]).strip()
        except Exception:
            return ""
    return re.sub(r"\s+", "", text).upper()


def _public_row(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    quota = {}
    cards = {}
    try:
        quota = json.loads(item.get("quota_json") or "{}")
    except Exception:
        quota = {}
    try:
        cards = json.loads(item.get("reset_cards_json") or "{}")
    except Exception:
        cards = {}
    quota_summary = _summary(quota, cards) if quota else {}
    return {
        "id": item.get("id"),
        "email": item.get("email"),
        "proxy_used": item.get("proxy_used") or "",
        "email_source": item.get("email_source") or "",
        "auth_mode": item.get("auth_mode") or "password_totp",
        "tag": item.get("tag") or "",
        "status": item.get("status") or "new",
        "last_error": item.get("last_error") or "",
        "codex_status": item.get("codex_status") or "",
        "account_id": item.get("account_id"),
        "has_password": bool(item.get("password_enc")),
        "has_totp": bool(item.get("totp_secret_enc")),
        "rt_filename": item.get("rt_filename") or "",
        "has_rt": bool(item.get("rt_filename")),
        "plan_type": item.get("plan_type") or "",
        "quota": quota,
        "quota_summary": quota_summary,
        "reset_cards": cards,
        "quota_checked_at": item.get("quota_checked_at") or "",
        "login_started_at": item.get("login_started_at") or "",
        "login_finished_at": item.get("login_finished_at") or "",
        "created_at": item.get("created_at") or "",
        "updated_at": item.get("updated_at") or "",
    }


def _secret_row(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    item["password"] = _dec(item.get("password_enc") or "")
    item["totp_secret"] = _dec(item.get("totp_secret_enc") or "")
    return item



def _used_proxies() -> list[str]:
    with _LOCK, closing(_conn()) as conn:
        rows = conn.execute("SELECT proxy_used FROM managed_accounts WHERE proxy_used<>''").fetchall()
    return [str(row["proxy_used"]).strip() for row in rows]


def _choose_pool_proxy(used: list[str] | None = None) -> str:
    try:
        from config import proxy as proxy_cfg
        pool = [str(item).strip() for item in (proxy_cfg.active_proxy_pool() or []) if str(item).strip()]
    except Exception:
        pool = []
    if not pool:
        return ""
    occupied = list(used or _used_proxies())
    for candidate in pool:
        if candidate not in occupied:
            return candidate
    return pool[len(occupied) % len(pool)]

def upsert_account(
    email: str,
    password: str = "",
    totp_secret: str = "",
    proxy_used: str = "",
    email_source: str = "",
    auth_mode: str = "password_totp",
    tag: str = "",
) -> int:
    _ensure_table()
    clean_email = str(email or "").strip()
    if not clean_email or "@" not in clean_email:
        raise ValueError("邮箱格式无效")
    clean_totp = _normalize_totp(totp_secret)
    now = _now()
    with _LOCK, closing(_conn()) as conn, conn:
        row = conn.execute("SELECT * FROM managed_accounts WHERE email=?", (clean_email,)).fetchone()
        if row is None:
            proxy_value = str(proxy_used or "").strip() or _choose_pool_proxy()
            cur = conn.execute(
                "INSERT INTO managed_accounts(email,password_enc,totp_secret_enc,proxy_used,email_source,auth_mode,tag,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    clean_email,
                    _enc(password),
                    _enc(clean_totp),
                    proxy_value,
                    str(email_source or "").strip(),
                    str(auth_mode or "password_totp").strip() or "password_totp",
                    str(tag or "").strip()[:200],
                    "new",
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)
        proxy_value = str(proxy_used or "").strip() or str(row["proxy_used"] or "").strip() or _choose_pool_proxy()
        conn.execute(
            "UPDATE managed_accounts SET "
            "password_enc=CASE WHEN ?<>'' THEN ? ELSE password_enc END,"
            "totp_secret_enc=CASE WHEN ?<>'' THEN ? ELSE totp_secret_enc END,"
            "proxy_used=?, email_source=?, auth_mode=?, tag=CASE WHEN ?<>'' THEN ? ELSE tag END, updated_at=? WHERE id=?",
            (
                password,
                _enc(password),
                clean_totp,
                _enc(clean_totp),
                proxy_value,
                str(email_source or "").strip(),
                str(auth_mode or "password_totp").strip() or "password_totp",
                str(tag or "").strip()[:200],
                str(tag or "").strip()[:200],
                now,
                int(row["id"]),
            ),
        )
        return int(row["id"])


def import_text(text: str, proxy_text: str = "") -> dict:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("导入内容为空")
    proxies = [line.strip() for line in str(proxy_text or "").splitlines() if line.strip()]
    records: list[dict] = []
    if raw.startswith("["):
        loaded = json.loads(raw)
        if not isinstance(loaded, list):
            raise ValueError("JSON 必须是数组")
        records = [item for item in loaded if isinstance(item, dict)]
    elif "----" in raw and not raw.lstrip().lower().startswith(("email,", "email\t")):
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [part.strip() for part in line.split("----")]
            if len(parts) < 3:
                records.append({"email": "", "invalid": True})
                continue
            records.append({
                "email": parts[0],
                "password": parts[1],
                "totp_secret": parts[2],
                "proxy_used": parts[3] if len(parts) > 3 else "",
            })
    else:
        reader = csv.DictReader(io.StringIO(raw))
        if not reader.fieldnames:
            raise ValueError("缺少 CSV 表头")
        records = [dict(row) for row in reader]
    inserted = 0
    updated = 0
    skipped = 0
    for idx, item in enumerate(records):
        email = str(item.get("email") or item.get("mail") or "").strip()
        if not email or item.get("invalid"):
            skipped += 1
            continue
        before = get_by_email(email)
        proxy = str(item.get("proxy_used") or item.get("proxy") or "").strip()
        if not proxy and proxies:
            proxy = proxies[idx % len(proxies)]
        upsert_account(
            email=email,
            password=str(item.get("password") or item.get("login_password") or "").strip(),
            totp_secret=str(item.get("totp_secret") or item.get("totp") or item.get("2fa") or "").strip(),
            proxy_used=proxy,
            email_source=str(item.get("email_source") or "").strip(),
            auth_mode=str(item.get("auth_mode") or "password_totp").strip() or "password_totp",
            tag=str(item.get("tag") or item.get("label") or item.get("note") or item.get("remark") or "").strip(),
        )
        if before:
            updated += 1
        else:
            inserted += 1
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def list_accounts() -> list[dict]:
    _ensure_table()
    with _LOCK, closing(_conn()) as conn:
        rows = conn.execute("SELECT * FROM managed_accounts ORDER BY id DESC").fetchall()
    return [_public_row(row) for row in rows]


def get_account(managed_id: int) -> dict | None:
    _ensure_table()
    with _LOCK, closing(_conn()) as conn:
        row = conn.execute("SELECT * FROM managed_accounts WHERE id=?", (int(managed_id),)).fetchone()
    return _secret_row(row) if row else None


def get_by_email(email: str) -> dict | None:
    _ensure_table()
    with _LOCK, closing(_conn()) as conn:
        row = conn.execute("SELECT * FROM managed_accounts WHERE email=?", (str(email or "").strip(),)).fetchone()
    return _public_row(row) if row else None


def update_tag(managed_id: int, tag: str) -> dict:
    _ensure_table()
    value = str(tag or "").strip()[:200]
    with _LOCK, closing(_conn()) as conn, conn:
        cur = conn.execute(
            "UPDATE managed_accounts SET tag=?, updated_at=? WHERE id=?",
            (value, _now(), int(managed_id)),
        )
        if cur.rowcount <= 0:
            raise ValueError("账号不存在")
    return {"ok": True, "id": int(managed_id), "tag": value}


def delete_account(managed_id: int) -> bool:
    _ensure_table()
    with _LOCK, closing(_conn()) as conn, conn:
        cur = conn.execute("DELETE FROM managed_accounts WHERE id=?", (int(managed_id),))
        return cur.rowcount > 0


def _sync_registration_account(account: dict) -> dict:
    email = str(account.get("email") or "").strip()
    password = str(account.get("password") or "").strip()
    totp_secret = str(account.get("totp_secret") or "").strip()
    proxy_used = str(account.get("proxy_used") or "").strip()
    email_source = str(account.get("email_source") or "managed").strip() or "managed"
    with db._LOCK:
        rows = db._load_accounts()
        row = db._find_by_email(rows, email)
        now = db._now()
        if row is None:
            row = {
                "id": db._next_account_id(),
                "email": email,
                "created_at": now,
                "updated_at": now,
                "access_token": "",
                "totp_secret": totp_secret,
                "registration_password": password,
                "password": password,
                "proxy_used": proxy_used or None,
                "email_source": email_source,
                "codex_status": "",
                "codex_error": None,
                "extra_json": json.dumps({"managed_account": True}, ensure_ascii=False),
                "user_name": "Managed Account",
            }
            rows.append(row)
        else:
            row["registration_password"] = password or row.get("registration_password") or ""
            row["password"] = password or row.get("password") or ""
            row["totp_secret"] = totp_secret or row.get("totp_secret") or ""
            row["proxy_used"] = proxy_used or row.get("proxy_used")
            row["email_source"] = email_source or row.get("email_source")
            row["updated_at"] = now
        db._save_accounts(rows)
    return db.get_account_by_email(email) or row


def _update_managed(managed_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    keys = list(fields.keys())
    values = [fields[key] for key in keys]
    with _LOCK, closing(_conn()) as conn, conn:
        conn.execute(
            f"UPDATE managed_accounts SET {', '.join(f'{key}=?' for key in keys)} WHERE id=?",
            (*values, int(managed_id)),
        )


def _plan_from_message(message: str) -> str:
    match = re.search(r"plan\s*=\s*([a-zA-Z0-9_-]+)", str(message or ""))
    return match.group(1).lower() if match else ""


def _close_managed_browser(profile_id: str) -> None:
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        return
    try:
        from core.roxybrowser_client import RoxyBrowserClient

        RoxyBrowserClient().close_profile(profile_id)
    except Exception:
        pass


def _login_worker(managed_id: int) -> None:
    account = get_account(managed_id)
    if not account:
        return
    email = str(account.get("email") or "").strip()
    _update_managed(managed_id, status="running", last_error="", login_started_at=_now(), login_finished_at="")
    from core import codex_retry_service, roxy_codex_oauth
    from config import roxybrowser as roxy_cfg
    old_headless = getattr(roxy_cfg, "ROXY_OPEN_HEADLESS", True)
    old_semi = getattr(roxy_codex_oauth, "SEMI_AUTO_SMS", False)
    old_env = os.environ.get("MANAGED_LOGIN_SEMI_AUTO")
    reserved = False
    try:
        _sync_registration_account(account)
        roxy_cfg.ROXY_OPEN_HEADLESS = False
        roxy_codex_oauth.SEMI_AUTO_SMS = True
        os.environ["MANAGED_LOGIN_SEMI_AUTO"] = "1"
        if not codex_retry_service.reserve(email):
            raise RuntimeError("该账号正在补跑 Codex，请稍候")
        reserved = True
        opened, profile_id = _ensure_managed_profile(account)
        log_path = Path.cwd() / "注册日志" / f"managed-login-{email.replace('@', '_at_')}.log"
        result = codex_retry_service.run_worker(
            email,
            clear_log=True,
            target_log_path=log_path,
            existing_opened=opened,
            reuse_existing_profile=True,
        )
        file_path = str(result.get("file_path") or "")
        filename = file_path.rsplit("/", 1)[-1] if file_path else ""
        status = "success" if result.get("ok") else ("stopped" if result.get("status") == "stopped" else "failed")
        _update_managed(
            managed_id,
            status=status,
            codex_status=str(result.get("status") or ""),
            rt_filename=filename,
            plan_type=_plan_from_message(str(result.get("message") or "")),
            last_error="" if result.get("ok") else str(result.get("message") or "")[:500],
            login_finished_at=_now(),
        )
    except Exception as exc:
        _update_managed(
            managed_id,
            status="failed",
            last_error=f"{type(exc).__name__}: {exc}"[:500],
            login_finished_at=_now(),
        )
    finally:
        if reserved:
            try:
                codex_retry_service.release(email)
            except Exception:
                pass
        roxy_cfg.ROXY_OPEN_HEADLESS = old_headless
        roxy_codex_oauth.SEMI_AUTO_SMS = old_semi
        if old_env is None:
            os.environ.pop("MANAGED_LOGIN_SEMI_AUTO", None)
        else:
            os.environ["MANAGED_LOGIN_SEMI_AUTO"] = old_env
        with _LOCK:
            _LOGIN_THREADS.pop(int(managed_id), None)


def start_login(managed_id: int) -> dict:
    _ensure_table()
    account = get_account(managed_id)
    if not account:
        raise ValueError("账号不存在")
    if not account.get("password") and not account.get("password_enc") and not account.get("totp_secret") and not account.get("totp_secret_enc"):
        raise ValueError("账号缺少密码和 TOTP")
    _sync_registration_account(account)
    with _LOCK:
        thread = _LOGIN_THREADS.get(int(managed_id))
        if thread and thread.is_alive():
            return {"ok": True, "status": "running", "message": "登录任务已在运行"}
        thread = threading.Thread(target=_login_worker, args=(int(managed_id),), daemon=True, name=f"managed-login-{managed_id}")
        _LOGIN_THREADS[int(managed_id)] = thread
        thread.start()
    return {"ok": True, "status": "running", "message": "已启动半自动登录；出现手机页时请在浏览器中手动接码"}


def _codex_credential(account: dict) -> dict:
    filename = str(account.get("rt_filename") or "").strip()
    if filename:
        _ensure_table()
        with db._LOCK, closing(db._sqlite_conn()) as conn:
            row = conn.execute("SELECT payload FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
        if row:
            try:
                payload = json.loads(row["payload"])
                tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
                if tokens:
                    payload = dict(payload)
                    payload["access_token"] = payload.get("access_token") or tokens.get("access_token") or ""
                    payload["account_id"] = payload.get("account_id") or tokens.get("account_id") or ""
                    payload["refresh_token"] = payload.get("refresh_token") or tokens.get("refresh_token") or ""
                return payload
            except Exception:
                pass
    email = str(account.get("email") or "").strip()
    row = db.get_account_by_email(email) or {}
    tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
    return {
        "access_token": row.get("access_token") or tokens.get("access_token") or "",
        "account_id": row.get("account_id") or tokens.get("account_id") or "",
        "plan_type": row.get("plan_type") or "",
    }


def _open_proxy(account: dict):
    from core.proxy_chain import open_proxy_pool_proxy
    target = str(account.get("proxy_used") or "").strip() or None
    transport, relay = open_proxy_pool_proxy(target)
    return transport, relay


def _http_json(method: str, path: str, account: dict, body: dict | None = None) -> dict:
    credential = _codex_credential(account)
    access_token = str(credential.get("access_token") or "").strip()
    account_id = str(credential.get("account_id") or credential.get("chatgpt_account_id") or "").strip()
    if not access_token or not account_id:
        raise RuntimeError("缺少 Codex access_token/account_id，请先完成登录并导出 RT")
    transport, relay = _open_proxy(account)
    try:
        from curl_cffi import requests
        session = requests.Session(impersonate="chrome")
        session.trust_env = False
        if transport:
            session.proxies = {"http": transport, "https": transport}
        headers = {
            "Authorization": f"Bearer {access_token}",
            "ChatGPT-Account-Id": account_id,
            "Accept": "application/json",
            "User-Agent": "codex-reset-card",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = session.request(
            method,
            f"{_CHATGPT_BASE}{path}",
            headers=headers,
            json=body,
            timeout=30,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"ChatGPT 后端 HTTP {response.status_code}: {response.text[:200]}")
        return response.json()
    finally:
        try:
            if "session" in locals():
                session.close()
        except Exception:
            pass
        if relay is not None:
            try:
                relay.close()
            except Exception:
                pass


def _summary(quota: dict, cards: dict) -> dict:
    usage = quota.get("usage") if isinstance(quota.get("usage"), dict) else {}
    rate_limit = usage.get("rate_limit") if isinstance(usage.get("rate_limit"), dict) else {}
    windows = {}
    for key in ("primary_window", "secondary_window"):
        item = rate_limit.get(key) if isinstance(rate_limit.get(key), dict) else {}
        used = item.get("used_percent")
        seconds = item.get("limit_window_seconds")
        if not isinstance(used, (int, float)):
            continue
        minutes = int(seconds) // 60 if isinstance(seconds, (int, float)) and seconds else None
        if minutes == 300:
            label = "five_hour"
        elif minutes == 10080:
            label = "weekly"
        elif minutes == 43200:
            label = "monthly"
        elif minutes is not None:
            label = f"window_{minutes}m"
        else:
            label = "five_hour" if key == "primary_window" else "weekly"
        windows[label] = {
            "used_percent": round(float(used), 2),
            "remaining_percent": round(max(0.0, 100.0 - float(used)), 2),
            "limit_window_seconds": seconds,
            "reset_at": item.get("reset_at"),
            "reset_after_seconds": item.get("reset_after_seconds"),
        }
    return {
        "windows": windows,
        "plan_type": usage.get("plan_type") or quota.get("plan_type") or "",
        "reset_available": cards.get("available_count"),
        "reset_cards": cards.get("credits") or [],
        "reached_type": rate_limit.get("rate_limit_reached_type"),
    }



def rotate_proxy(managed_id: int) -> dict:
    account = get_account(managed_id)
    if not account:
        raise ValueError("账号不存在")
    current = str(account.get("proxy_used") or "").strip()
    used = [item for item in _used_proxies() if item != current]
    pool: list[str] = []
    try:
        from config import proxy as proxy_cfg
        pool = [str(item).strip() for item in (proxy_cfg.active_proxy_pool() or []) if str(item).strip()]
    except Exception:
        pool = []
    candidates = [item for item in pool if item != current and item not in used]
    if not candidates:
        candidates = [item for item in pool if item != current]
    if not candidates:
        candidates = [item for item in pool if item != current] or pool
    new_proxy = candidates[0] if candidates else _choose_pool_proxy(used)
    if not new_proxy:
        raise RuntimeError("主项目代理池为空，无法切换")
    _update_managed(managed_id, proxy_used=new_proxy, last_error="")
    profile_id = _account_roxy_profile_id(account)
    proxy_updated = False
    if profile_id:
        item = _PROFILE_RELAYS.pop(profile_id, None)
        if item and item.get("relay") is not None:
            try:
                item["relay"].close()
            except Exception:
                pass
        try:
            from core.proxy_chain import open_proxy_pool_proxy

            transport, relay = open_proxy_pool_proxy(new_proxy)
            if relay is not None:
                _PROFILE_RELAYS[profile_id] = {"transport": str(transport or ""), "relay": relay, "target": new_proxy}
            proxy_updated = _openbrowser_update_proxy(profile_id, str(transport or ""))
        except Exception:
            proxy_updated = False
    return {"ok": True, "proxy_used": new_proxy, "previous": current, "profile_id": profile_id, "proxy_updated": proxy_updated}


def reset_stale_running_statuses() -> int:
    """WebUI 重启后清理已不可能存活的后台任务状态。"""
    _ensure_table()
    with _LOCK, closing(_conn()) as conn, conn:
        cur = conn.execute(
            "UPDATE managed_accounts SET status='idle', last_error='stale running cleared on restart', updated_at=? WHERE status='running'",
            (_now(),),
        )
        return int(cur.rowcount or 0)


def _device_stage(session: dict, stage: str, detail: str = "") -> None:
    session["stage"] = stage
    session["stage_at"] = _now()
    logs = session.setdefault("stage_log", [])
    logs.append({"stage": stage, "at": session["stage_at"], "detail": str(detail)[:300]})
    session["stage_log"] = logs[-40:]


def _device_public(session: dict) -> dict:
    return {
        "ok": True,
        "status": session.get("status") or "starting",
        "stage": session.get("stage") or "",
        "stage_at": session.get("stage_at") or "",
        "stages": session.get("stage_log") or [],
        "code": session.get("code") or "",
        "url": session.get("url") or "https://auth.openai.com/codex/device",
        "page_url": session.get("page_url") or "",
        "message": session.get("message") or "",
        "rt_filename": session.get("rt_filename") or "",
        "plan_type": session.get("plan_type") or "",
        "started_at": session.get("started_at") or "",
        "browser": session.get("browser") or "",
        "preflight": session.get("preflight") or "",
        "login": session.get("login") or "",
        "account_select": session.get("account_select") or "",
        "out_path": session.get("out_path") or "",
        "log": (session.get("log") or "")[-1200:],
        "tail_error": session.get("tail_error") or "",
    }



def _openbrowser_profile_exists(profile_id: str) -> bool | None:
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        return False
    try:
        import requests
        from dotenv import dotenv_values

        cfg = dotenv_values(Path.cwd() / ".env")
        base = str(cfg.get("OPENBROWSER_API_BASE") or "http://127.0.0.1:50327").rstrip("/")
        key = str(cfg.get("OPENBROWSER_API_KEY") or "").strip()
        headers = {"Accept": "application/json"}
        if key:
            headers["api-key"] = key
        session = requests.Session()
        session.trust_env = False
        session.headers.update(headers)
        resp = session.get(f"{base}/api/v1/user/list", timeout=8)
        payload = resp.json() if resp.content else {}
        items = ((payload.get("data") or {}).get("list") or []) if isinstance(payload, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            ids = {str(item.get(k) or "").strip() for k in ("id", "user_id", "profile_id")}
            if profile_id in ids:
                return True
        return False
    except Exception:
        return None


def _enable_continue_where_left_off(profile_id: str) -> bool:
    profile_id = str(profile_id or "").strip()
    appdata = str(os.environ.get("APPDATA") or "").strip()
    if not profile_id or not appdata:
        return False
    prefs_path = Path(appdata) / "openbrowser" / "browser-profiles-v2" / profile_id / "Default" / "Preferences"
    if not prefs_path.exists():
        return False
    try:
        data = json.loads(prefs_path.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            data = {}
        session = data.get("session") if isinstance(data.get("session"), dict) else {}
        session["restore_on_startup"] = 1
        session["startup_urls"] = []
        data["session"] = session
        prof = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        prof["exit_type"] = "Normal"
        prof["exited_cleanly"] = True
        data["profile"] = prof
        prefs_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        return True
    except Exception:
        return False


def _save_roxy_profile_id(email: str, profile_id: str) -> None:
    profile_id = str(profile_id or "").strip()
    if not email or not profile_id:
        return
    with db._LOCK:
        rows = db._load_accounts()
        row = db._find_by_email(rows, email)
        if row is None:
            return
        try:
            extra = json.loads(row.get("extra_json") or "{}")
        except Exception:
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
        roxy = extra.get("roxybrowser") if isinstance(extra.get("roxybrowser"), dict) else {}
        roxy["profile_id"] = profile_id
        roxy.setdefault("bound_by", "managed_device")
        extra["roxybrowser"] = roxy
        row["extra_json"] = json.dumps(extra, ensure_ascii=False)
        db._save_accounts(rows)


def _openbrowser_update_proxy(profile_id: str, proxy_url: str) -> bool:
    profile_id = str(profile_id or "").strip()
    proxy_url = str(proxy_url or "").strip()
    if not profile_id or not proxy_url:
        return False
    try:
        import requests
        from dotenv import dotenv_values
        from core.roxybrowser_client import _proxy_url_to_roxy_info

        cfg = dotenv_values(Path.cwd() / ".env")
        base = str(cfg.get("ROXY_API_BASE") or "http://127.0.0.1:50100").rstrip("/")
        payload = {"profile_id": profile_id, "proxyInfo": _proxy_url_to_roxy_info(proxy_url)}
        resp = requests.post(f"{base}/browser/proxy", json=payload, timeout=60)
        return bool(resp.ok)
    except Exception:
        return False


_PROFILE_RELAYS: dict[str, dict] = {}
_PROXY_HEALTH: dict[str, tuple[bool, float]] = {}
_PROXY_HEALTH_TTL = 60.0
_PROXY_PROBE_URL = "https://auth.openai.com/log-in"


def _probe_proxy_transport(transport: str, timeout: float = 12.0) -> bool:
    """通过本地中继访问授权页，确认该出口能被 OpenAI 接受。"""
    transport = str(transport or "").strip()
    if not transport:
        return True
    try:
        import requests

        with requests.Session() as http:
            http.trust_env = False
            resp = http.get(
                _PROXY_PROBE_URL,
                proxies={"http": transport, "https": transport},
                timeout=(8.0, timeout),
                allow_redirects=False,
                stream=True,
            )
            status = int(resp.status_code)
            resp.close()
        return status < 400
    except Exception:
        return False


def _proxy_target_healthy(
    target: str,
    transport: str,
    *,
    force: bool = False,
    timeout: float = 12.0,
) -> bool:
    target = str(target or "").strip()
    if not target:
        return True
    now = time.time()
    if not force:
        cached = _PROXY_HEALTH.get(target)
        if cached and now - cached[1] < _PROXY_HEALTH_TTL:
            return bool(cached[0])
    ok = _probe_proxy_transport(transport, timeout=timeout)
    _PROXY_HEALTH[target] = (ok, now)
    return ok


def _alternative_proxy_targets(exclude: str) -> list[str]:
    try:
        from config import proxy as proxy_cfg

        pool = [str(item).strip() for item in (proxy_cfg.active_proxy_pool() or []) if str(item).strip()]
    except Exception:
        return []
    used = set(_used_proxies())
    fresh = [item for item in pool if item != exclude and item not in used]
    others = [item for item in pool if item != exclude and item in used]
    return fresh + others


def _persist_account_proxy(account: dict, target: str) -> None:
    target = str(target or "").strip()
    if not target:
        return
    account["proxy_used"] = target
    managed_id = account.get("id")
    if managed_id:
        try:
            _update_managed(int(managed_id), proxy_used=target)
        except Exception:
            pass
    try:
        _sync_registration_account(account)
    except Exception:
        pass


def _open_healthy_proxy(account: dict, target: str):
    """打开代理中继并验证出口；失效时先刷新 session，再切换代理池。"""
    from core.proxy_chain import open_proxy_pool_proxy

    target = str(target or "").strip()
    if not target:
        return "", "", None
    transport, relay = open_proxy_pool_proxy(target)
    if _proxy_target_healthy(target, str(transport or "")):
        return target, str(transport or ""), relay
    if relay is not None:
        try:
            relay.close()
        except Exception:
            pass
    try:
        from core.proxy_utils import mask_proxy_url

        masked = mask_proxy_url(target)
    except Exception:
        masked = "***"
    LOG.warning("代理出口不可用，尝试刷新粘性 session：%s", masked)
    try:
        from config import proxy as proxy_cfg

        for attempt in range(2):
            proxy_cfg.refresh_proxy_session(target)
            transport, relay = open_proxy_pool_proxy(target)
            if _proxy_target_healthy(target, str(transport or ""), force=True):
                LOG.info("刷新后代理出口恢复：%s", masked)
                return target, str(transport or ""), relay
            if relay is not None:
                try:
                    relay.close()
                except Exception:
                    pass
            LOG.warning("第 %s 次刷新后仍不可用：%s", attempt + 1, masked)
    except Exception:
        pass
    for candidate in _alternative_proxy_targets(target):
        transport, relay = open_proxy_pool_proxy(candidate)
        if _proxy_target_healthy(candidate, str(transport or ""), force=True, timeout=8.0):
            LOG.warning("代理出口切换：%s -> 新 session", masked)
            _persist_account_proxy(account, candidate)
            return candidate, str(transport or ""), relay
        if relay is not None:
            try:
                relay.close()
            except Exception:
                pass
    LOG.error("代理池全部不可用，暂用原代理继续：%s", masked)
    transport, relay = open_proxy_pool_proxy(target)
    return target, str(transport or ""), relay


def _managed_profile_transport(profile_id: str, account: dict) -> dict:
    profile_id = str(profile_id or "").strip()
    target = str(account.get("proxy_used") or "").strip()
    item = _PROFILE_RELAYS.get(profile_id) if profile_id else None
    if item and item.get("target") == target and item.get("relay") is not None:
        if _proxy_target_healthy(target, str(item.get("transport") or "")):
            return item
        try:
            item["relay"].close()
        except Exception:
            pass
        _PROFILE_RELAYS.pop(profile_id, None)
        item = None
    if item and item.get("relay") is not None:
        try:
            item["relay"].close()
        except Exception:
            pass
        _PROFILE_RELAYS.pop(profile_id, None)
    if not target:
        return {"transport": "", "relay": None, "target": ""}
    target, transport, relay = _open_healthy_proxy(account, target)
    item = {"transport": str(transport or ""), "relay": relay, "target": target}
    if profile_id:
        _PROFILE_RELAYS[profile_id] = item
    return item


def _ensure_managed_profile(account: dict):
    from core.roxybrowser_client import RoxyBrowserClient, _proxy_url_to_roxy_info
    from config import roxybrowser as roxy_cfg

    email = str(account.get("email") or "").strip()
    profile_id = _account_roxy_profile_id(account)
    if profile_id and _openbrowser_profile_exists(profile_id) is False:
        profile_id = ""
    client = RoxyBrowserClient()
    one_per_account = bool(getattr(roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", False))

    def _open_existing(pid: str):
        _enable_continue_where_left_off(pid)
        roxy_cfg.ROXY_ONE_PROFILE_PER_ACCOUNT = False
        try:
            return client.open_profile(pid)
        finally:
            roxy_cfg.ROXY_ONE_PROFILE_PER_ACCOUNT = one_per_account

    if profile_id:
        item = _managed_profile_transport(profile_id, account)
        # 先关闭旧窗口再写入代理：运行中的 Chrome 不会重新读取代理配置，
        # 桥接/服务重启后复用旧窗口会得到已失效的本地中继端口。
        try:
            client.close_profile(profile_id)
        except Exception:
            pass
        if item.get("transport"):
            _openbrowser_update_proxy(profile_id, str(item["transport"]))
        return _open_existing(profile_id), profile_id

    payload = {}
    target = str(account.get("proxy_used") or "").strip()
    relay = None
    transport = ""
    if target:
        target, transport, relay = _open_healthy_proxy(account, target)
        payload["proxyInfo"] = _proxy_url_to_roxy_info(transport)
    profile_id = client.create_profile(payload or None)
    if relay is not None:
        _PROFILE_RELAYS[profile_id] = {"transport": str(transport or ""), "relay": relay, "target": target}
    _save_roxy_profile_id(email, profile_id)
    return _open_existing(profile_id), profile_id


def _account_roxy_profile_id(account: dict) -> str:
    try:
        row = db.get_account_by_email(str(account.get("email") or "")) or {}
        extra = row.get("extra_json") or "{}"
        data = json.loads(extra) if isinstance(extra, str) else (extra if isinstance(extra, dict) else {})
        roxy = data.get("roxybrowser") if isinstance(data.get("roxybrowser"), dict) else {}
        profile = roxy.get("profile_id") or ((roxy.get("open_result") or {}).get("dirId") if isinstance(roxy.get("open_result"), dict) else "")
        return str(profile or "").strip()
    except Exception:
        return ""


def _auto_login_chatgpt(driver, account: dict) -> str:
    """用托管账号密码 + TOTP 登录 ChatGPT；返回 success/needs_login/needs_otp/error。"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    email = str(account.get("email") or "").strip()
    if not email:
        return "needs_login"
    try:
        driver.get("https://chatgpt.com/auth/login")
        time.sleep(4)
        current = str(driver.current_url or "")
        if "chatgpt.com" in current and "auth" not in current and "login" not in current:
            return "success"
        email_selectors = [
            "input[type='email']", "input[name='email']", "input[id='email']",
            "input[autocomplete='username']", "input[inputmode='email']",
        ]
        email_input = None
        for selector in email_selectors:
            try:
                el = driver.find_element(By.CSS_SELECTOR, selector)
                if el.is_displayed():
                    email_input = el
                    break
            except Exception:
                continue
        if email_input is None:
            text = (driver.page_source or "").lower()
            if "check your email" in text or "verification code" in text or "验证码" in text:
                return "needs_otp"
            return "needs_login"
        email_input.clear()
        email_input.send_keys(email)
        email_input.send_keys(Keys.ENTER)
        time.sleep(5)
        from core import roxy_codex_oauth as oauth
        pw_state = oauth._fill_login_password_if_present(driver, email, timeout=20)
        if pw_state:
            time.sleep(4)
        if oauth._wait_for_mfa_after_email(driver, timeout=15):
            if not oauth._fill_mfa_challenge_if_present(driver, email, timeout=45):
                return "needs_login"
            time.sleep(4)
        time.sleep(3)
        current = str(driver.current_url or "")
        text = (driver.page_source or "").lower()
        if "check your email" in text or "verification code" in text or "验证码" in text:
            return "needs_otp"
        if "auth.openai.com" in current or "/auth/" in current:
            return "needs_login"
        return "success" if "chatgpt.com" in current else "needs_login"
    except Exception as exc:
        return f"error: {type(exc).__name__}: {str(exc)[:160]}"


def _ensure_device_code_authorization(driver) -> str:
    """只操作 #security-device-code-sign-in，校验开启成功。"""
    from selenium.webdriver.common.by import By

    try:
        driver.get("https://chatgpt.com/settings/security")
        deadline = time.time() + 30
        switch = None
        while time.time() < deadline:
            current = str(driver.current_url or "")
            if "auth.openai.com" in current or "/auth/login" in current:
                return "needs_login"
            try:
                candidate = driver.find_element(By.CSS_SELECTOR, "#security-device-code-sign-in button[role='switch']")
                if candidate.is_displayed():
                    switch = candidate
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if switch is None:
            return "not_found: #security-device-code-sign-in not found at " + str(driver.current_url or "")
        checked = str(switch.get_attribute("aria-checked") or "").lower()
        if checked == "true":
            return "already"
        for _ in range(3):
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", switch)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", switch)
                time.sleep(2)
                checked = str(switch.get_attribute("aria-checked") or "").lower()
                if checked == "true":
                    return "enabled"
            except Exception:
                time.sleep(1)
        return "not_found: device code switch did not turn on"
    except Exception as exc:
        return f"error: {type(exc).__name__}: {str(exc)[:160]}"


def _device_account_monitor(driver, account: dict, session: dict) -> None:
    """设备授权页出现 Choose an account 时，自动选择当前托管账号并继续。"""
    from selenium.webdriver.common.by import By

    email = str(account.get("email") or "").strip().lower()
    while not session.get("monitor_stop"):
        if session.get("status") in {"success", "failed"}:
            return
        if session.get("driver") is not driver:
            return
        try:
            url = str(driver.current_url or "")
            body = str(driver.page_source or "")
            low = body.lower()
            session["page_url"] = url
            expired = any(k in low for k in (
                "session expired", "session has expired", "sign in again", "log in again",
                "登录已失效", "登录过期", "重新登录", "登录已过期", "your session",
            ))
            if expired:
                _device_stage(session, "reauth", "session expired page")
                session["device_reauth"] = True
                for label in ("Continue", "Sign in", "Log in", "继续", "重新登录", "登录"):
                    try:
                        nodes = driver.find_elements(By.XPATH, f"//button[contains(normalize-space(.), '{label}')] | //a[contains(normalize-space(.), '{label}')]")
                        for node in nodes:
                            if node.is_displayed() and node.is_enabled():
                                driver.execute_script("arguments[0].click();", node)
                                time.sleep(2)
                                break
                    except Exception:
                        continue
            if expired or session.get("device_reauth") or (("email address" in low and "email is required" in low) or "/log-in" in url):
                if not session.get("device_login_retry") or session.get("device_reauth"):
                    _device_stage(session, "login", "login page detected")
                    session["device_login_retry"] = True
                    session["account_select"] = "auto_login"
                    state = _auto_login_chatgpt(driver, account)
                    session["login"] = state
                    time.sleep(2)
                    if state == "success":
                        session["device_reauth"] = False
                        driver.get("https://auth.openai.com/codex/device")
                        time.sleep(2)
                continue
            if "signed in to codex" in low or "you may now close this page" in low or "signed in to chatgpt" in low:
                _device_stage(session, "signed_in", "device authorization success page")
                session["device_signed_in"] = True
                session["account_select"] = "signed_in"
                time.sleep(1)
                try:
                    _close_device_browser(session)
                except Exception:
                    pass
                return
            chooser = (
                "choose an account" in low
                or "welcome back" in low
                or "choose-an-account" in url
                or "continue to codex" in low
            )
            if chooser:
                buttons = driver.find_elements(By.CSS_SELECTOR, "button[name='session_id']")
                clicked = False
                for btn in buttons:
                    try:
                        text = (str(btn.text or "") + " " + str(btn.get_attribute("aria-label") or "")).lower()
                        if email and email in text:
                            btn.click()
                            session["account_select"] = "clicked"
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked and len(buttons) == 1:
                    try:
                        buttons[0].click()
                        session["account_select"] = "clicked_first"
                        clicked = True
                    except Exception:
                        pass
                if clicked:
                    _device_stage(session, "account_selected", "selected account chooser entry")
                    time.sleep(2)
                    continue
            for label in ("Continue", "Allow", "Authorize", "Confirm", "Select", "继续", "允许", "确认"):
                try:
                    nodes = driver.find_elements(
                        By.XPATH,
                        f"//button[contains(normalize-space(.), '{label}') or @aria-label='{label}']",
                    )
                    for node in nodes:
                        if node.is_displayed() and node.is_enabled():
                            node.click()
                            session["account_select"] = "continued"
                            time.sleep(2)
                            break
                except Exception:
                    continue
        except Exception:
            return
        time.sleep(1.2)


def _open_device_browser(managed_id: int, session: dict, account: dict) -> None:
    client = None
    relay = None
    driver = None
    try:
        from core.roxybrowser_client import RoxyBrowserClient, _proxy_url_to_roxy_info
        from core.roxy_codex_oauth import _build_driver
        from config import roxybrowser as roxy_cfg
        profile_id = _account_roxy_profile_id(account)
        if profile_id and _openbrowser_profile_exists(profile_id) is False:
            session["saved_profile_missing"] = profile_id
            profile_id = ""
        client = RoxyBrowserClient()
        created = False
        one_per_account = bool(getattr(roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", False))
        if profile_id:
            item = _managed_profile_transport(profile_id, account)
            relay = item.get("relay")
            try:
                client.close_profile(profile_id)
            except Exception:
                pass
            if item.get("transport"):
                _openbrowser_update_proxy(profile_id, str(item["transport"]))
            _enable_continue_where_left_off(profile_id)
            roxy_cfg.ROXY_ONE_PROFILE_PER_ACCOUNT = False
            try:
                opened = client.open_profile(profile_id)
            finally:
                roxy_cfg.ROXY_ONE_PROFILE_PER_ACCOUNT = one_per_account
            session["browser"] = "opening existing profile"
        else:
            payload = {}
            target = str(account.get("proxy_used") or "").strip()
            transport = ""
            relay = None
            if target:
                target, transport, relay = _open_healthy_proxy(account, target)
                payload["proxyInfo"] = _proxy_url_to_roxy_info(transport)
            profile_id = client.create_profile(payload or None)
            if relay is not None:
                _PROFILE_RELAYS[profile_id] = {"transport": str(transport or ""), "relay": relay, "target": target}
            session["profile_id"] = profile_id
            session["profile_created"] = True
            _save_roxy_profile_id(str(account.get("email") or ""), profile_id)
            _enable_continue_where_left_off(profile_id)
            roxy_cfg.ROXY_ONE_PROFILE_PER_ACCOUNT = False
            try:
                opened = client.open_profile(profile_id)
            finally:
                roxy_cfg.ROXY_ONE_PROFILE_PER_ACCOUNT = one_per_account
            created = True
            session["browser"] = "opening new profile"
        driver = _build_driver(opened)
        try:
            _sync_registration_account(account)
        except Exception:
            pass
        login_state = _auto_login_chatgpt(driver, account)
        session["login"] = login_state
        preflight = _ensure_device_code_authorization(driver)
        session["preflight"] = preflight
        _device_stage(session, "browser_ready", f"preflight={preflight}")
        session["setting_ready"] = True
        driver.get("https://auth.openai.com/codex/device")
        session.update({
            "browser": "opened",
            "driver": driver,
            "client": client,
            "profile_id": opened.profile_id,
            "profile_created": created,
            "browser_relay": relay,
        })
        monitor = threading.Thread(
            target=_device_account_monitor,
            args=(driver, account, session),
            daemon=True,
            name=f"managed-device-account-monitor-{managed_id}",
        )
        session["account_monitor"] = monitor
        monitor.start()
    except Exception as exc:
        session["browser"] = f"failed: {type(exc).__name__}: {str(exc)[:180]}"
        session["setting_ready"] = True
        try:
            if driver is not None:
                driver.quit()
            if client is not None and session.get("profile_id"):
                client.close_profile(str(session.get("profile_id")))
        except Exception:
            pass
        if relay is not None:
            try:
                relay.close()
            except Exception:
                pass


def _close_device_browser(session: dict) -> None:
    session["monitor_stop"] = True
    driver = session.pop("driver", None)
    client = session.pop("client", None)
    profile_id = str(session.pop("profile_id", "") or "")
    created = bool(session.pop("profile_created", False))
    relay = None
    try:
        if driver is not None:
            driver.quit()
    except Exception:
        pass
    try:
        if client is not None and profile_id:
            client.close_profile(profile_id)
    except Exception:
        pass
    if relay is not None:
        try:
            relay.close()
        except Exception:
            pass


def _normalize_codex_auth(raw: dict, email: str) -> dict:
    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}
    id_token = str(raw.get("id_token") or tokens.get("id_token") or "").strip()
    access_token = str(raw.get("access_token") or tokens.get("access_token") or "").strip()
    refresh_token = str(raw.get("refresh_token") or tokens.get("refresh_token") or "").strip()
    account_id = str(raw.get("account_id") or tokens.get("account_id") or "").strip()
    claims = {}
    try:
        from core.codex_oauth import _parse_id_token
        claims = _parse_id_token(id_token) if id_token else {}
    except Exception:
        claims = {}
    effective_email = str(claims.get("email") or email or "").strip()
    plan_type = str(claims.get("plan_type") or claims.get("chatgpt_plan_type") or raw.get("plan_type") or "").strip().lower()
    return {
        "type": "codex",
        "email": effective_email,
        "account_id": account_id,
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "last_refresh": raw.get("last_refresh") or "",
        "plan_type": plan_type,
    }


def _device_worker(managed_id: int, session: dict, home: Path, email: str, account: dict) -> None:
    process = None
    out_fh = None
    auth_path = home / "auth.json"
    backup = home / f"auth.json.device-backup-{uuid.uuid4().hex}"
    had_auth = auth_path.exists()
    out_path = Path.cwd() / "data" / "codex-device" / f"{int(managed_id)}-{uuid.uuid4().hex}.log"
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if had_auth:
            shutil.copy2(auth_path, backup)
        wait_until = time.time() + 120
        while not session.get("setting_ready") and time.time() < wait_until:
            if session.get("status") in {"failed", "success"}:
                return
            time.sleep(0.5)
        exe = shutil.which("codex") or r"D:\nvm\current\codex.cmd"
        if str(exe).lower().endswith((".cmd", ".bat")):
            command = [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(exe), "login", "--device-auth"]
        else:
            command = [str(exe), "login", "--device-auth"]
        env = dict(os.environ)
        env["CODEX_HOME"] = str(home)
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out_fh = out_path.open("w", encoding="utf-8", errors="replace")
        process = subprocess.Popen(
            command,
            cwd=str(Path.cwd()),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out_fh,
            stderr=subprocess.STDOUT,
            text=False,
            creationflags=flags,
        )
        session["process"] = process
        session["out_path"] = str(out_path)
        url_re = re.compile(r"https://auth\.openai\.com/codex/device", re.I)
        code_re = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,8})\b")
        log_lines: list[str] = []
        seen = 0
        deadline = time.time() + 900
        while process.poll() is None and time.time() < deadline:
            text = ""
            if out_path.exists():
                try:
                    text = out_path.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:
                    session["tail_error"] = f"{type(exc).__name__}: {exc}"[:300]
                    text = ""
            lines = text.splitlines()
            if len(lines) >= seen:
                for line in lines[seen:]:
                    line = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
                    if not line:
                        continue
                    log_lines.append(line)
                    session["log"] = "\n".join(log_lines[-80:])
                    if url_re.search(line):
                        session["url"] = "https://auth.openai.com/codex/device"
                    match_code = code_re.search(line)
                    if match_code:
                        session["code"] = match_code.group(1)
                        session["status"] = "waiting"
                        _device_stage(session, "code_ready", "device code parsed from CLI")
                        _update_managed(managed_id, status="waiting", last_error="")
                        session["event"].set()
                        if not session.get("browser_thread"):
                            session["browser_thread"] = threading.Thread(
                                target=_open_device_browser,
                                args=(managed_id, session, account),
                                daemon=True,
                                name=f"managed-device-browser-{managed_id}",
                            )
                            session["browser_thread"].start()
                seen = len(lines)
            time.sleep(0.4)
        try:
            text = out_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        for line in text.splitlines():
            line = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
            match_code = code_re.search(line)
            if match_code:
                session["code"] = match_code.group(1)
                _device_stage(session, "code_ready", "device code parsed from CLI")
                _update_managed(managed_id, status="waiting", last_error="")
                if not session.get("browser_thread"):
                    session["status"] = "waiting"
                    session["event"].set()
                    session["browser_thread"] = threading.Thread(
                        target=_open_device_browser,
                        args=(managed_id, session, account),
                        daemon=True,
                        name=f"managed-device-browser-{managed_id}",
                    )
                    session["browser_thread"].start()
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=6)
        except Exception:
            pass
        rc = process.poll()
        if rc not in (0, None):
            raise RuntimeError(f"codex login exited {rc}: {session.get('log','')[-300:]}")
        if not auth_path.exists():
            raise RuntimeError("codex login 成功但未生成 auth.json")
        _update_managed(
            managed_id,
            status="device_ok",
            codex_status="device_ok",
            last_error="",
            login_finished_at=_now(),
        )
        _device_stage(session, "success", "device login completed")
        session.update({
            "status": "success",
            "rt_filename": str(account.get("rt_filename") or ""),
            "plan_type": str(account.get("plan_type") or ""),
            "message": "device authorization completed; existing SMS RT kept",
        })
        _close_device_browser(session)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"[:500]
        _update_managed(managed_id, status="failed", last_error=message, login_finished_at=_now())
        _device_stage(session, "failed", message)
        session.update({"status": "failed", "message": message})
        _close_device_browser(session)
    finally:
        session["event"].set()
        try:
            if out_fh is not None:
                out_fh.close()
        except Exception:
            pass
        try:
            if process is not None and process.poll() is None:
                process.terminate()
        except Exception:
            pass
        try:
            if had_auth and backup.exists():
                shutil.move(str(backup), str(auth_path))
            elif not had_auth:
                auth_path.unlink(missing_ok=True)
        except Exception:
            pass


def start_device_login(managed_id: int) -> dict:
    account = get_account(managed_id)
    if not account:
        raise ValueError("账号不存在")
    with _DEVICE_LOCK:
        existing = _DEVICE_SESSIONS.get(int(managed_id))
        if existing and existing.get("status") in {"starting", "waiting"}:
            proc = existing.get("process")
            if proc is not None and proc.poll() is None:
                return _device_public(existing)
            _close_device_browser(existing)
            _DEVICE_SESSIONS.pop(int(managed_id), None)
        home = Path.home() / ".codex"
        home.mkdir(parents=True, exist_ok=True)
        session = {
            "status": "starting",
            "code": "",
            "url": "https://auth.openai.com/codex/device",
            "message": "starting codex login --device-auth",
            "started_at": _now(),
            "event": threading.Event(),
            "log": "",
        }
        _device_stage(session, "starting", "creating device login session")
        _DEVICE_SESSIONS[int(managed_id)] = session
        thread = threading.Thread(
            target=_device_worker,
            args=(int(managed_id), session, home, str(account.get("email") or ""), account),
            daemon=True,
            name=f"managed-device-login-{managed_id}",
        )
        session["thread"] = thread
        _update_managed(managed_id, status="running", last_error="", login_started_at=_now())
        thread.start()
        browser_thread = threading.Thread(
            target=_open_device_browser,
            args=(int(managed_id), session, account),
            daemon=True,
            name=f"managed-device-browser-{managed_id}",
        )
        session["browser_thread"] = browser_thread
        browser_thread.start()
        session["event"].wait(12)
        return _device_public(session)


def device_login_status(managed_id: int) -> dict:
    with _DEVICE_LOCK:
        session = _DEVICE_SESSIONS.get(int(managed_id))
        if not session:
            return {"ok": False, "status": "idle", "message": "没有设备码登录会话"}
        return _device_public(session)


def open_environment(managed_id: int) -> dict:
    account = get_account(managed_id)
    if not account:
        raise ValueError("账号不存在")
    opened, profile_id = _ensure_managed_profile(account)
    return {
        "ok": True,
        "profile_id": profile_id,
        "message": "Roxy environment opened",
    }


def cancel_device_login(managed_id: int, reason: str = "cleared by user") -> dict:
    with _DEVICE_LOCK:
        session = _DEVICE_SESSIONS.pop(int(managed_id), None)
    if session is not None:
        session["monitor_stop"] = True
        _device_stage(session, "cancelled", reason)
        try:
            proc = session.get("process")
            if proc is not None and proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        _close_device_browser(session)
    _update_managed(managed_id, status="idle", last_error="")
    return {"ok": True, "status": "cancelled", "message": reason}


def restart_device_login(managed_id: int) -> dict:
    cancel_device_login(managed_id, "restart requested")
    time.sleep(1)
    return start_device_login(managed_id)


def continue_device_login(managed_id: int) -> dict:
    from selenium.webdriver.common.by import By

    with _DEVICE_LOCK:
        session = _DEVICE_SESSIONS.get(int(managed_id))
    if not session:
        return {"ok": False, "status": "idle", "message": "没有设备码登录会话"}
    driver = session.get("driver")
    if driver is None:
        _device_stage(session, "continue_failed", "browser driver is not available")
        return _device_public(session)
    account = get_account(managed_id) or {}
    try:
        url = str(driver.current_url or "")
        body = str(driver.page_source or "")
        low = body.lower()
        session["page_url"] = url
        if any(k in low for k in ("email address", "email is required", "log-in", "session expired", "登录已失效")):
            _device_stage(session, "login", "manual continue: login page")
            state = _auto_login_chatgpt(driver, account)
            session["login"] = state
            if state == "success":
                driver.get("https://auth.openai.com/codex/device")
            return _device_public(session)
        if "choose an account" in low or "welcome back" in low or "choose-an-account" in url:
            email = str(account.get("email") or "").strip().lower()
            for btn in driver.find_elements(By.CSS_SELECTOR, "button[name='session_id']"):
                text = (str(btn.text or "") + " " + str(btn.get_attribute("aria-label") or "")).lower()
                if not email or email in text:
                    driver.execute_script("arguments[0].click();", btn)
                    _device_stage(session, "account_selected", "manual continue: account selected")
                    time.sleep(2)
                    return _device_public(session)
        clicked = False
        for label in ("Continue", "Allow", "Authorize", "Confirm", "继续", "允许", "确认"):
            for node in driver.find_elements(By.XPATH, f"//button[contains(normalize-space(.), '{label}')] | //a[contains(normalize-space(.), '{label}')]"):
                if node.is_displayed() and node.is_enabled():
                    driver.execute_script("arguments[0].click();", node)
                    _device_stage(session, "continued", f"manual continue: clicked {label}")
                    clicked = True
                    time.sleep(2)
                    break
            if clicked:
                break
        if not clicked:
            _device_stage(session, "continue_waiting", "manual continue: no actionable button found")
    except Exception as exc:
        _device_stage(session, "continue_failed", f"{type(exc).__name__}: {exc}")
    return _device_public(session)


def check_quota(managed_id: int) -> dict:
    account = get_account(managed_id)
    if not account:
        raise ValueError("账号不存在")
    credits = _http_json("GET", "/wham/rate-limit-reset-credits", account)
    usage = _http_json("GET", "/wham/usage", account)
    quota = {"usage": usage, "credits": credits}
    cards = {
        "available_count": credits.get("available_count"),
        "credits": credits.get("credits") or [],
    }
    summary = _summary(quota, cards)
    _update_managed(
        managed_id,
        status="quota_ok",
        plan_type=str(summary.get("plan_type") or ""),
        quota_json=json.dumps(quota, ensure_ascii=False),
        reset_cards_json=json.dumps(cards, ensure_ascii=False),
        quota_checked_at=_now(),
        last_error="",
    )
    return {"ok": True, **summary, "checked_at": _now()}


def consume_reset_card(managed_id: int, credit_id: str = "") -> dict:
    account = get_account(managed_id)
    if not account:
        raise ValueError("账号不存在")
    credits = _http_json("GET", "/wham/rate-limit-reset-credits", account)
    items = credits.get("credits") or []
    target = None
    wanted = str(credit_id or "").strip()
    for item in items:
        if not isinstance(item, dict):
            continue
        if wanted and str(item.get("id") or "") != wanted:
            continue
        if not wanted and str(item.get("status") or "").lower() != "available":
            continue
        target = item
        break
    if target is None:
        raise RuntimeError("没有可用的 Codex 重置卡")
    result = _http_json(
        "POST",
        "/wham/rate-limit-reset-credits/consume",
        account,
        {
            "redeem_request_id": str(uuid.uuid4()),
            "credit_id": str(target.get("id") or ""),
        },
    )
    code = str(result.get("code") or "").strip()
    if code not in {"reset", "nothing_to_reset", "no_credit", "already_redeemed"}:
        raise RuntimeError(f"未知重置结果: {result}")
    refreshed = None
    try:
        refreshed = check_quota(managed_id)
    except Exception:
        refreshed = None
    return {"ok": code == "reset", "code": code, "credit_id": target.get("id"), "quota": refreshed}


def reset_cards(managed_id: int) -> dict:
    account = get_account(managed_id)
    if not account:
        raise ValueError("账号不存在")
    credits = _http_json("GET", "/wham/rate-limit-reset-credits", account)
    cards = {
        "available_count": credits.get("available_count"),
        "credits": credits.get("credits") or [],
    }
    _update_managed(
        managed_id,
        reset_cards_json=json.dumps(cards, ensure_ascii=False),
        quota_checked_at=_now(),
    )
    return {"ok": True, **cards}


def rt_json(managed_id: int) -> str:
    account = get_account(managed_id)
    if not account:
        raise ValueError("账号不存在")
    credential = _codex_credential(account)
    return json.dumps(credential, ensure_ascii=False, indent=2)
