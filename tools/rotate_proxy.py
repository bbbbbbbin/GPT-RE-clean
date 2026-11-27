# -*- coding: utf-8 -*-
"""给账号换代理节点（proxy_used）。

场景：某些粘性代理节点失效——典型表现是

* ``Failed to perform, curl: (7) CONNECT tunnel failed, response 502``
* ``curl: (60) SSL certificate ... unable to get local issuer certificate``

换节点不影响 access token，只要新节点能正常访问 chatgpt.com 即可。

用法::

    python tools/rotate_proxy.py --ids 790 819 829 878
    python tools/rotate_proxy.py --bad-session 53826330          # 换掉所有用该节点的账号
    python tools/rotate_proxy.py --bad-session 53826330 --dry-run
"""
from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import proxy as _proxy_cfg  # noqa: E402
from core import db  # noqa: E402
from core.session import BrowserSession, close_browser_session  # noqa: E402


def _candidates(exclude: str) -> list[str]:
    pool = [str(x).strip() for x in (getattr(_proxy_cfg, "PROXY_POOL", None) or []) if str(x).strip()]
    if exclude:
        pool = [p for p in pool if exclude not in p]
    seen: set[str] = set()
    out: list[str] = []
    for p in pool:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _check(proxy: str) -> tuple[str, bool, str]:
    session = None
    try:
        session = BrowserSession(proxy=proxy, fingerprint_seed="proxy:healthcheck")
        resp = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers=session.get_nextauth_headers(referer="https://chatgpt.com/"),
        )
        ok = resp.status_code == 200
        return proxy, ok, f"{resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return proxy, False, f"{type(exc).__name__}: {str(exc)[:80]}"
    finally:
        if session is not None:
            try:
                close_browser_session(session)
            except Exception:  # noqa: BLE001
                pass


def healthy_candidates(exclude: str, workers: int = 4) -> list[str]:
    pool = _candidates(exclude)
    if not pool:
        raise SystemExit("PROXY_POOL 为空，无法换节点")
    good: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        for proxy, ok, detail in executor.map(_check, pool):
            label = (re.search(r"session-(\d+)", proxy) or [None, proxy[:32]])[1]
            print(f"  探测 {label}: {'OK' if ok else 'BAD'} ({detail})", flush=True)
            if ok:
                good.append(proxy)
    if not good:
        raise SystemExit("代理池里没有可用节点，先检查 10808 链路")
    return good


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="更换账号代理节点")
    parser.add_argument("--ids", nargs="+", type=int)
    parser.add_argument("--bad-session", default="", help="旧节点标识，例如 53826330")
    parser.add_argument("--from-id", type=int)
    parser.add_argument("--to-id", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)

    targets: list[tuple[int, str]] = []
    if args.ids:
        for acc_id in args.ids:
            acc = db.get_account(acc_id)
            if not acc:
                print(f"id={acc_id} 不存在")
                continue
            targets.append((acc_id, str(acc.get("proxy_used") or "")))
    else:
        match = args.bad_session.strip()
        if not match:
            raise SystemExit("需要 --ids 或 --bad-session")
        rows = db.list_accounts(limit=5000, archived=None)
        for acc in rows:
            acc_id = int(acc.get("id") or 0)
            if args.from_id is not None and acc_id < args.from_id:
                continue
            if args.to_id is not None and acc_id > args.to_id:
                continue
            proxy = str(acc.get("proxy_used") or "")
            if match in proxy:
                targets.append((acc_id, proxy))

    if not targets:
        print("没有匹配到需要换节点的账号")
        return 0

    print(f"待换节点账号 {len(targets)} 个：{', '.join(str(i) for i, _ in targets)}")
    exclude = args.bad_session.strip()
    if not exclude:
        # --ids 模式下也避免换回同一个坏节点：只有当所有目标节点一致时才排除它。
        unique = {p for _, p in targets}
        exclude = unique.pop() if len(unique) == 1 else ""

    print("探测可用节点：")
    good = healthy_candidates(exclude, workers=args.workers)
    print(f"可用节点 {len(good)} 个")

    if args.dry_run:
        for idx, (acc_id, old) in enumerate(targets):
            print(f"[dry] id={acc_id} {old[-40:]} -> {good[idx % len(good)][-40:]}")
        return 0

    changed = 0
    for idx, (acc_id, old) in enumerate(targets):
        new_proxy = good[idx % len(good)]
        if db.update_account_proxy_used(acc_id, new_proxy):
            changed += 1
            label = (re.search(r"session-(\d+)", new_proxy) or [None, "?"])[1]
            print(f"id={acc_id} 已换到节点 {label}", flush=True)
    print(f"完成：{changed}/{len(targets)} 个账号已换节点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
