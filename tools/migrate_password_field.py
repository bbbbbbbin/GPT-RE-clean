# -*- coding: utf-8 -*-
"""把历史上误写到账号顶层 ``password`` 的 ChatGPT 密码迁到规范位置。

账号表顶层 ``password`` 是邮箱素材（Outlook）密码；批量补密码脚本早期版本把
ChatGPT 密码写到了这里。本工具把它搬到 ``extra_json.registration_password``，
并清空顶层字段（Outlook 来源账号跳过，避免破坏邮箱密码）。

用法::

    python tools/migrate_password_field.py --from-id 683 --to-id 782
    python tools/migrate_password_field.py --ids 683 684 --dry-run
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import db  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="迁移账号密码字段位置")
    parser.add_argument("--ids", nargs="+", type=int)
    parser.add_argument("--from-id", type=int)
    parser.add_argument("--to-id", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.ids:
        ids = args.ids
    elif args.from_id is not None and args.to_id is not None:
        ids = list(range(args.from_id, args.to_id + 1))
    else:
        raise SystemExit("需要 --ids 或 --from-id/--to-id")

    stats: Counter[str] = Counter()
    for acc_id in ids:
        acc = db.get_account(acc_id)
        if not acc:
            stats["missing"] += 1
            continue
        top = str(acc.get("password") or "").strip()
        if not top:
            stats["empty"] += 1
            continue
        if args.dry_run:
            print(f"[dry] id={acc_id} source={acc.get('email_source')} top_password_len={len(top)}")
            stats["pending"] += 1
            continue
        result = db.migrate_account_password_field(acc_id)
        stats[result] += 1
        if result == "written":
            print(f"id={acc_id} 已迁移（{len(top)} 位）", flush=True)

    print("完成：", dict(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
