# -*- coding: utf-8 -*-
"""批量补密码 + 补 TOTP。

流程（每个账号独立 BrowserSession，复用注册时的代理与指纹种子）：

1. 通过 ChatGPT 设置页同款入口发起“添加密码”重认证：
   ``signin/openai?...&post_login_add_password=true`` ->
   ``authorize`` -> ``email-verification`` -> 邮箱 OTP ->
   ``/reset-password/new-password`` -> ``POST /api/accounts/password/add``。
2. 设置成功会立即吊销旧 access token，因此必须跟随响应里的 ``continue_url``
   重新建立登录态并写回新 AT。
3. 账号已有密码时服务端返回 ``password_already_set``，改为普通 OTP 登录刷新 AT。
4. 需要时用新 AT 走 ``backend-api/accounts/mfa/enroll`` + ``activate_enrollment``
   补 TOTP。

用法::

    python tools/password_backfill.py --from-id 683 --to-id 782 --workers 3
    python tools/password_backfill.py --ids 683 684 690 --totp-only
    python tools/password_backfill.py --from-id 683 --to-id 782 --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode

import pyotp

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import db  # noqa: E402
from core.db import _extract_registration_password  # noqa: E402
from core.account_export import (  # noqa: E402
    _activate_totp,
    _clear_twofa_session_circuit,
    _enroll_totp,
    _follow_reauth_with_retry,
    _trigger_reauth_with_retry,
    _validate_reauth_otp,
    fetch_session,
)
from core.chatgpt_bootstrap import authenticated_bootstrap  # noqa: E402
from core.email_provider import wait_for_otp  # noqa: E402
from core.humanize import delay as human_delay  # noqa: E402
from core.openai_auth import (  # noqa: E402
    build_sentinel_header,
    generate_registration_password,
    request_sentinel_token,
)
from core.session import BrowserSession, close_browser_session  # noqa: E402

LOG_PATH = ROOT / "logs" / "password_backfill.jsonl"
_LOG_LOCK = threading.Lock()
logger = logging.getLogger("password_backfill")


def _log(record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False)
    with _LOG_LOCK:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    print(line, flush=True)


def _signin_url(
    session: BrowserSession, email: str, *, add_password: bool = False,
    reset_password: bool = False, callback: str,
) -> str:
    csrf_resp = session.get(
        "https://chatgpt.com/api/auth/csrf",
        headers=session.get_nextauth_headers(referer="https://chatgpt.com/"),
    )
    csrf_resp.raise_for_status()
    csrf = csrf_resp.json()["csrfToken"]

    query = {
        "connection": "password",
        "login_hint": email,
        "reauth": "password",
        "max_age": "0",
        "ext-oai-did": session.device_id,
    }
    if add_password:
        query["post_login_add_password"] = "true"
    if reset_password:
        query["post_login_password_reset"] = "true"

    url = "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query)
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/")
    headers["content-type"] = "application/x-www-form-urlencoded"
    headers["origin"] = "https://chatgpt.com"
    body = urlencode({"callbackUrl": callback, "csrfToken": csrf, "json": "true"})
    resp = session.post(url, headers=headers, data=body)
    resp.raise_for_status()
    return resp.json()["url"]


def _chatgpt_password(acc: dict) -> str:
    """读取 ChatGPT 账号自身密码（``extra_json.registration_password``）。

    账号表顶层 ``password`` 是邮箱素材密码，不能当作 ChatGPT 密码。
    """
    return _extract_registration_password(acc or {})


def _send_email_otp(session: BrowserSession) -> int | None:
    """真正触发一封新的邮箱验证码。

    auth 前端在 ``email_otp_send`` 步骤会主动导航到该 URL；只打开
    ``/email-verification`` 页面并不会发信，必须显式请求一次。
    """
    headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/email-verification")
    try:
        resp = session.get(
            "https://auth.openai.com/api/accounts/email-otp/send",
            headers=headers,
            allow_redirects=False,
        )
        return resp.status_code
    except Exception as exc:  # noqa: BLE001
        logger.warning("触发邮箱验证码失败：%s: %s", type(exc).__name__, str(exc)[:160])
        return None


def _solve_mfa_challenge(session: BrowserSession, continue_url: str, totp_secret: str) -> str:
    """处理 reauth 中的 TOTP 挑战（账号已启用 2FA 时必经）。

    ``POST /api/accounts/mfa/verify`` body 为 ``{id, type, code}``，
    ``id`` 即 ``/mfa-challenge/<id>`` 里的 factor id。
    """
    if "mfa-challenge" not in continue_url:
        return continue_url
    if not totp_secret:
        raise RuntimeError("账号已启用 2FA 但本地没有 totp_secret，无法完成重认证")

    factor_id = continue_url.split("/mfa-challenge/", 1)[1].split("?")[0]
    page = session.get(
        continue_url,
        headers=session.get_auth_navigate_headers(referer="https://auth.openai.com/email-verification"),
    )
    referer = page.url or continue_url

    last_body = ""
    for _ in range(2):
        totp = pyotp.TOTP(totp_secret).now()
        headers = session.get_auth_headers(referer=referer)
        headers["content-type"] = "application/json"
        resp = session.post(
            "https://auth.openai.com/api/accounts/mfa/verify",
            headers=headers,
            data=json.dumps({"id": factor_id, "type": "totp", "code": totp}),
        )
        if resp.status_code == 200:
            data = resp.json()
            next_url = str(data.get("continue_url") or "")
            if not next_url:
                raise RuntimeError(f"mfa/verify 响应缺少 continue_url: {str(data)[:200]}")
            return next_url
        last_body = (resp.text or "")[:300]
        # 时钟偏差导致 6 位码过期时，等下一窗口再试一次。
        time.sleep(31)
    raise RuntimeError(f"mfa/verify 失败: {last_body}")


def _email_otp_step(session: BrowserSession, email: str, totp_secret: str) -> str:
    """触发并提交邮箱 OTP，必要时再完成 TOTP 挑战，返回后续 continue_url。"""
    after_ts = time.time() - 3
    _send_email_otp(session)
    code = wait_for_otp(email, after_ts=after_ts)
    human_delay("otp_input")
    continue_url = _validate_reauth_otp(session, code)
    return _solve_mfa_challenge(session, continue_url, totp_secret)


def _follow_continue(session: BrowserSession, continue_url: str, referer: str) -> dict:
    headers = session.get_auth_navigate_headers(referer=referer)
    resp = session.get(continue_url, headers=headers, allow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"continue_url 跟随失败 status={resp.status_code} body={(resp.text or '')[:200]}")
    return fetch_session(session)


def _password_login(session: BrowserSession, password: str) -> dict:
    """在 log-in/password 页面用已知密码登录，返回新的 ChatGPT session JSON。"""
    sentinel = request_sentinel_token(session, "password_verify")
    sentinel_header, so_header = build_sentinel_header(session, sentinel, "password_verify")
    headers = session.get_auth_headers(referer="https://auth.openai.com/log-in/password")
    headers["content-type"] = "application/json"
    headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
    resp = session.post(
        "https://auth.openai.com/api/accounts/password/verify",
        headers=headers,
        data=json.dumps({"password": password}),
    )
    if resp.status_code != 200:
        raise RuntimeError(f"密码登录失败 status={resp.status_code} body={(resp.text or '')[:200]}")
    data = resp.json()
    continue_url = str(data.get("continue_url") or "")
    if not continue_url:
        raise RuntimeError(f"密码登录响应缺少 continue_url: {data}")
    human_delay("api")
    return _follow_continue(session, continue_url, "https://auth.openai.com/log-in/password")


def _otp_login(session: BrowserSession, email: str, password: str | None = None, totp_secret: str = "") -> dict:
    """普通 OTP 登录（不改密码），返回新的 ChatGPT session JSON。"""
    auth_url = _trigger_reauth_with_retry(session, email)
    human_delay("api")
    landed = _follow_reauth_with_retry(session, auth_url)
    if "log-in/password" in landed:
        if not password:
            raise RuntimeError(f"账号已启用密码登录且未保存密码：landed={landed}")
        return _password_login(session, password)
    if "email-verification" not in landed:
        raise RuntimeError(f"未进入邮箱验证码页：landed={landed}")

    continue_url = _email_otp_step(session, email, totp_secret)
    return _follow_continue(session, continue_url, "https://auth.openai.com/email-verification")


def _set_password(
    session: BrowserSession,
    email: str,
    password: str,
    totp_secret: str = "",
    *,
    intent: str = "add",
) -> dict:
    """post_login_add_password / post_login_password_reset 流程设置密码。

    ``intent="add"`` 对应没有密码的账号；``intent="reset"`` 对应已有密码但
    密码未知的账号（相当于“修改密码”，新密码不能与旧密码相同）。
    """
    if intent not in {"add", "reset"}:
        raise ValueError(f"unknown intent: {intent}")
    auth_url = _signin_url(
        session,
        email,
        add_password=intent == "add",
        reset_password=intent == "reset",
        callback="https://chatgpt.com/settings/security",
    )
    human_delay("api")
    landed = _follow_reauth_with_retry(session, auth_url)
    if "email-verification" not in landed:
        return {"status": "unexpected_landing", "landed": landed}

    continue_url = _email_otp_step(session, email, totp_secret)
    if "new-password" not in continue_url:
        return {"status": "no_new_password_page", "continue_url": continue_url}

    human_delay("api")
    sentinel = request_sentinel_token(session, "password_reset")
    sentinel_header, so_header = build_sentinel_header(session, sentinel, "password_reset")
    headers = session.get_auth_headers(referer=continue_url)
    headers["content-type"] = "application/json"
    headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header

    resp = session.post(
        f"https://auth.openai.com/api/accounts/password/{intent}",
        headers=headers,
        data=json.dumps({"password": password}),
    )
    text = resp.text or ""
    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        new_session = None
        continue_after = str(data.get("continue_url") or data.get("redirect_uri") or "")
        if continue_after:
            try:
                new_session = _follow_continue(session, continue_after, continue_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s 密码已设置但 continue_url 跟随失败：%s", email, str(exc)[:160])
        if new_session is None:
            try:
                new_session = fetch_session(session)
            except Exception:  # noqa: BLE001
                new_session = None
        return {"status": "ok", "password": password, "session": new_session}
    if "password_already_set" in text:
        return {"status": "already_set"}
    return {"status": f"http_{resp.status_code}", "body": text[:400]}


def _add_password(session: BrowserSession, email: str, password: str, totp_secret: str = "") -> dict:
    return _set_password(session, email, password, totp_secret, intent="add")


def _mfa_info(session: BrowserSession, access_token: str) -> dict:
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/settings/security")
    headers["authorization"] = f"Bearer {access_token}"
    headers["oai-device-id"] = session.device_id
    headers["oai-language"] = session.navigator_language()
    resp = session.get("https://chatgpt.com/backend-api/accounts/mfa_info", headers=headers)
    if resp.status_code != 200:
        return {"_status": resp.status_code, "_body": (resp.text or "")[:200]}
    try:
        return resp.json()
    except ValueError:
        return {"_status": resp.status_code, "_body": (resp.text or "")[:200]}


def process_account(
    acc_id: int,
    *,
    want_password: bool,
    want_totp: bool,
    bootstrap: bool,
    force_reset: bool = False,
    refresh_at: bool = False,
) -> dict:
    acc = db.get_account(acc_id)
    if not acc:
        return {"id": acc_id, "status": "missing"}
    email = str(acc.get("email") or "").strip()
    at = str(acc.get("access_token") or "").strip()
    existing_password = _chatgpt_password(acc)
    existing_totp = str(acc.get("totp_secret") or "").strip()

    result = {
        "id": acc_id,
        "email": email,
        "password": "existing" if existing_password else None,
        "totp": "existing" if existing_totp else None,
        "status": "ok",
        "notes": [],
    }

    need_password = want_password and (force_reset or not existing_password)
    need_totp = want_totp and not existing_totp
    if not refresh_at and not need_password and not need_totp:
        result["status"] = "skipped"
        return result

    session = BrowserSession(
        proxy=acc.get("proxy_used") or None,
        fingerprint_seed=f"account:{email.lower()}",
    )
    try:
        if bootstrap and at:
            try:
                authenticated_bootstrap(session, at, strict=False)
            except Exception as exc:  # noqa: BLE001
                result["notes"].append(f"bootstrap:{type(exc).__name__}")
            finally:
                _clear_twofa_session_circuit(session, source="补密码预热")

        new_session: dict | None = None
        password = existing_password
        totp_secret: str | None = None
        totp_ok = False

        if refresh_at:
            new_session = _otp_login(session, email, existing_password or None, existing_totp)
            token = str((new_session or {}).get("accessToken") or "").strip()
            if not token:
                result["status"] = "no_access_token"
                _log({**result, "event": "no_access_token"})
                return result
            db.update_account_credentials(acc_id, access_token=token, session=new_session)
            result["status"] = "ok"
            _log({**result, "event": "at_refreshed"})
            return result

        if need_password:
            generated = generate_registration_password(16)
            outcome = _set_password(
                session, email, generated, existing_totp,
                intent="reset" if force_reset else "add",
            )
            if outcome["status"] == "ok":
                password = outcome["password"]
                new_session = outcome.get("session")
                result["password"] = "created"
                # 密码一旦设置成功必须立刻落库，后续登录/2FA 失败也不能丢。
                db.update_account_credentials(
                    acc_id,
                    password=password,
                    access_token=str((new_session or {}).get("accessToken") or "") or None,
                    session=new_session,
                )
                _log({**result, "event": "password_set"})
            elif outcome["status"] == "already_set":
                result["notes"].append("password_already_set")
                result["password"] = "unknown"
                _log({**result, "event": "password_already_set"})
            else:
                result["status"] = "password_failed"
                result["notes"].append(f"add_password:{outcome['status']}")
                result["detail"] = {k: v for k, v in outcome.items() if k != "password"}
                _log({**result, "event": "password_failed"})
                return result

        if not str((new_session or {}).get("accessToken") or "").strip():
            new_session = None
            try:
                new_session = _otp_login(session, email, password or None, existing_totp)
            except Exception as exc:  # noqa: BLE001
                result["status"] = "login_failed"
                result["notes"].append(f"otp_login:{type(exc).__name__}:{str(exc)[:160]}")
                if password:
                    result["partial_password"] = True
                _log({**result, "event": "login_failed"})
                return result

        token = str((new_session or {}).get("accessToken") or "").strip()
        if not token:
            result["status"] = "no_access_token"
            _log({**result, "event": "no_access_token"})
            return result

        if need_totp:
            info = _mfa_info(session, token)
            if info.get("mfa_enabled"):
                result["totp"] = "already_enabled"
            else:
                secret, session_id = _enroll_totp(session, token)
                _activate_totp(session, token, secret, session_id)
                result["totp"] = "created"
                totp_secret = secret
                totp_ok = True

        db.update_account_credentials(
            acc_id,
            password=password if result.get("password") == "created" else None,
            access_token=token,
            totp_secret=totp_secret,
            session=new_session,
            totp_ok=totp_ok,
            message="补密码脚本设置" if totp_ok else None,
        )
        _log({**result, "event": "done"})
        return result
    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["notes"].append(f"{type(exc).__name__}:{str(exc)[:200]}")
        _log({**result, "event": "error"})
        return result
    finally:
        try:
            close_browser_session(session)
        except Exception:  # noqa: BLE001
            pass


def _collect_ids(args) -> list[int]:
    if args.ids:
        return [int(x) for x in args.ids]
    start, end = args.from_id, args.to_id
    if start is None or end is None:
        raise SystemExit("需要 --ids 或 --from-id/--to-id")
    ids = []
    for acc_id in range(start, end + 1):
        acc = db.get_account(acc_id)
        if not acc:
            continue
        if (
            args.only_missing
            and not getattr(args, "force_reset", False)
            and not getattr(args, "refresh_at", False)
            and _chatgpt_password(acc)
            and acc.get("totp_secret")
        ):
            continue
        if args.totp_only and acc.get("totp_secret"):
            continue
        ids.append(acc_id)
    return ids


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="批量补密码 + 补 TOTP")
    parser.add_argument("--ids", nargs="+", help="账号 ID 列表")
    parser.add_argument("--from-id", type=int)
    parser.add_argument("--to-id", type=int)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--password-only", action="store_true")
    parser.add_argument("--totp-only", action="store_true")
    parser.add_argument("--only-missing", action="store_true", default=True)
    parser.add_argument("--no-bootstrap", action="store_true")
    parser.add_argument(
        "--force-reset",
        action="store_true",
        help="已有密码的账号也改密（走 password/reset），用于密码未知的账号",
    )
    parser.add_argument(
        "--refresh-at",
        action="store_true",
        help="只重新登录换新 access token（用已保存的密码/2FA），不改密码、不补 TOTP",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    )

    ids = _collect_ids(args)
    if args.limit:
        ids = ids[: args.limit]
    want_password = not args.totp_only
    want_totp = not args.password_only

    print(f"待处理账号 {len(ids)} 个：password={want_password} totp={want_totp} workers={args.workers}")
    if args.dry_run:
        print(ids)
        return 0

    stats = {"ok": 0, "skipped": 0, "failed": 0}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(
                process_account,
                acc_id,
                want_password=want_password,
                want_totp=want_totp,
                bootstrap=not args.no_bootstrap,
                force_reset=args.force_reset,
                refresh_at=args.refresh_at,
            ): acc_id
            for acc_id in ids
        }
        for future in as_completed(futures):
            acc_id = futures[future]
            try:
                res = future.result()
            except Exception as exc:  # noqa: BLE001
                res = {"id": acc_id, "status": "error", "notes": [str(exc)[:200]]}
            if res.get("status") in {"ok", "skipped"}:
                stats["ok" if res["status"] == "ok" else "skipped"] += 1
            else:
                stats["failed"] += 1
            print(
                f"[{stats['ok']}/{len(ids)}] id={res.get('id')} status={res.get('status')} "
                f"password={res.get('password')} totp={res.get('totp')} notes={res.get('notes')}",
                flush=True,
            )

    print("完成：", stats)
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
