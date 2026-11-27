# -*- coding: utf-8 -*-
"""校验账号是否已补齐密码 / TOTP，且 access token 仍然可用。

只做只读检查（不触发重认证，不会吊销 token）：

1. 本地：``password`` 非空、``totp_secret`` 可被 pyotp 解析、AT 未过期。
2. 远端：``/backend-api/me`` 200、``/accounts/mfa_info`` 显示 MFA 已启用、
   ``/accounts/change_password/eligibility`` 为 true（说明账号已有密码）。

用法::

    python tools/verify_accounts.py --from-id 683 --to-id 782
    python tools/verify_accounts.py --ids 683 684 690 --json logs/verify.json
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pyotp  # noqa: E402

from core import db  # noqa: E402
from core.chatgpt_plan import token_claims  # noqa: E402
from core.db import _extract_registration_password  # noqa: E402
from core.session import BrowserSession, close_browser_session  # noqa: E402


def _api(session: BrowserSession, token: str, path: str) -> tuple[int, dict | None]:
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/settings/security")
    headers["authorization"] = f"Bearer {token}"
    headers["oai-device-id"] = session.device_id
    headers["oai-language"] = session.navigator_language()
    resp = session.get("https://chatgpt.com/backend-api" + path, headers=headers)
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body


def check_account(acc_id: int, *, remote: bool = True) -> dict:
    acc = db.get_account(acc_id)
    if not acc:
        return {"id": acc_id, "status": "missing"}
    email = str(acc.get("email") or "")
    password = _extract_registration_password(acc) or str(acc.get("password") or "")
    totp_secret = str(acc.get("totp_secret") or "")
    token = str(acc.get("access_token") or "")

    out = {
        "id": acc_id,
        "email": email,
        "password": bool(password),
        "password_len": len(password),
        "totp": False,
        "at": bool(token),
        "at_expired": None,
        "me": None,
        "mfa_enabled": None,
        "has_password_remote": None,
        "status": "ok",
        "problems": [],
    }

    if password and not (12 <= len(password) <= 64):
        out["problems"].append("password_len")
    if totp_secret:
        try:
            pyotp.TOTP(totp_secret).now()
            out["totp"] = True
        except Exception:  # noqa: BLE001
            out["problems"].append("totp_invalid")
    else:
        out["problems"].append("no_totp")

    if token:
        claims = token_claims(token) or {}
        out["at_expired"] = claims.get("token_expired")
        if out["at_expired"]:
            out["problems"].append("at_expired")
    else:
        out["problems"].append("no_at")

    if remote and token and not out["at_expired"]:
        session = BrowserSession(
            proxy=acc.get("proxy_used") or None,
            fingerprint_seed=f"account:{email.lower()}",
        )
        try:
            code, body = _api(session, token, "/me")
            out["me"] = code
            if code != 200:
                out["problems"].append(f"me_{code}")
            code, body = _api(session, token, "/accounts/mfa_info")
            if code == 200 and body:
                out["mfa_enabled"] = bool(body.get("mfa_enabled"))
                if not out["mfa_enabled"]:
                    out["problems"].append("mfa_disabled")
            else:
                out["problems"].append(f"mfa_info_{code}")
            code, body = _api(session, token, "/accounts/change_password/eligibility")
            if code == 200 and body is not None:
                out["has_password_remote"] = bool(body.get("eligible"))
                if not out["has_password_remote"]:
                    out["problems"].append("no_password_remote")
            else:
                out["problems"].append(f"pwd_elig_{code}")
        finally:
            try:
                close_browser_session(session)
            except Exception:  # noqa: BLE001
                pass

    if out["problems"]:
        out["status"] = "failed"
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="校验账号密码 / TOTP / AT")
    parser.add_argument("--ids", nargs="+", type=int)
    parser.add_argument("--from-id", type=int)
    parser.add_argument("--to-id", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--no-remote", action="store_true")
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)

    if args.ids:
        ids = args.ids
    elif args.from_id is not None and args.to_id is not None:
        ids = list(range(args.from_id, args.to_id + 1))
    else:
        raise SystemExit("需要 --ids 或 --from-id/--to-id")

    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(check_account, i, remote=not args.no_remote): i for i in ids}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                results.append({"id": futures[future], "status": "error", "problems": [str(exc)[:160]]})

    results.sort(key=lambda r: r.get("id") or 0)
    ok = [r for r in results if r.get("status") == "ok"]
    bad = [r for r in results if r.get("status") != "ok"]

    print(f"总计 {len(results)}，通过 {len(ok)}，异常 {len(bad)}")
    for r in bad:
        print(f"  FAIL id={r.get('id')} email={r.get('email')} problems={r.get('problems')}")

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已写入 {args.json}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
